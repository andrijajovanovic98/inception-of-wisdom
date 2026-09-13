"""
Inception-of-Wisdom (IoW) — Part 3: Wisdom Loop
Safety Guardrails: Single-Flight, Cooldown, Rate Limit, Hard Cap & Kill-Switch
"""

from __future__ import annotations

import os
import time
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment,misc]

logger = logging.getLogger("p3.safety")

DEFAULT_CONFIG_PATH = "demo_app/iow.config.yml"


@dataclass
class SafetyCheckResult:
    """Outcome of safety check before allowing a heal cycle to start."""
    allowed: bool
    refusal_reason: Optional[str] = None
    config_snapshot: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class SafetyManager:
    """Enforces the 5 non-negotiable safety bounds from iow.config.yml:
    1. One heal at a time (single-flight).
    2. Cooldown per crash signature.
    3. Rate limit per hour.
    4. Hard cap over process lifetime.
    5. Global kill-switch.
    Dynamic: Re-reads iow.config.yml automatically when modified on disk.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = os.path.abspath(config_path or DEFAULT_CONFIG_PATH)
        self._last_mtime: float = 0.0
        self._config: Dict[str, Any] = self._load_defaults()

        # Operational state
        self._single_flight_active: bool = False
        self._signature_last_healed: Dict[str, float] = {}
        self._heal_timestamps_history: List[float] = []
        self._lifetime_heal_count: int = 0

        # Load initial config
        self.reload_config()

    def _load_defaults(self) -> Dict[str, Any]:
        return {
            "grace_period": 20,
            "cooldown": 300,
            "rate_limit_per_hour": 5,
            "hard_cap": 20,
            "kill_switch": False
        }

    def reload_config(self) -> Dict[str, Any]:
        """Reads iow.config.yml if modified on disk without restarting the agent."""
        if not os.path.isfile(self.config_path):
            logger.warning(f"Configuration file not found at {self.config_path}. Using defaults.")
            return self._config

        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime > self._last_mtime:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    if yaml is not None:
                        parsed = yaml.safe_load(f) or {}
                    else:
                        # Fallback simple parser for essential keys
                        parsed = {}
                        for line in f:
                            if ":" in line and not line.strip().startswith("#"):
                                k, v = line.split(":", 1)
                                k = k.strip()
                                v = v.strip().split("#")[0].strip()
                                if v.lower() == "true":
                                    parsed[k] = True
                                elif v.lower() == "false":
                                    parsed[k] = False
                                elif v.isdigit():
                                    parsed[k] = int(v)

                # Merge with defaults
                for key in self._load_defaults():
                    if key in parsed:
                        self._config[key] = parsed[key]

                self._last_mtime = mtime
                logger.info(
                    f"Reloaded configuration from {os.path.basename(self.config_path)}: "
                    f"grace_period={self._config['grace_period']}s, "
                    f"cooldown={self._config['cooldown']}s, "
                    f"rate_limit={self._config['rate_limit_per_hour']}/h, "
                    f"hard_cap={self._config['hard_cap']}, "
                    f"kill_switch={self._config['kill_switch']}"
                )
        except Exception as e:
            logger.error(f"Error reading configuration file {self.config_path}: {e}")

        return self._config

    def check_heal_allowed(self, signature: str) -> SafetyCheckResult:
        """Evaluates all 5 safety bounds to decide if a new heal is permitted."""
        cfg = self.reload_config()
        now = time.time()

        # 1. Global Kill-Switch check
        if cfg.get("kill_switch", False):
            reason = "Refused: global kill_switch is active (kill_switch=true in iow.config.yml)."
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        # 2. Single-flight check
        if self._single_flight_active:
            reason = "Refused: single-flight constraint violated (another heal cycle is currently running)."
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        # 3. Hard cap check
        hard_cap = cfg.get("hard_cap", 20)
        if self._lifetime_heal_count >= hard_cap:
            reason = (
                f"Refused: lifetime hard cap reached "
                f"({self._lifetime_heal_count}/{hard_cap} heals started)."
            )
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        # 4. Cooldown check per crash signature
        cooldown = cfg.get("cooldown", 300)
        last_heal_time = self._signature_last_healed.get(signature)
        if last_heal_time is not None:
            time_since_last = now - last_heal_time
            if time_since_last < cooldown:
                remaining = round(cooldown - time_since_last, 1)
                reason = f"Refused: cooldown active for signature '{signature}' ({remaining}s remaining)."
                logger.warning(f"Safety constraint triggered: {reason}")
                return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        # 5. Rate limit per hour check (rolling 3600-second window)
        rate_limit = cfg.get("rate_limit_per_hour", 5)
        one_hour_ago = now - 3600.0
        # Prune older timestamps
        self._heal_timestamps_history = [
            t for t in self._heal_timestamps_history if t > one_hour_ago
        ]

        if len(self._heal_timestamps_history) >= rate_limit:
            hourly = len(self._heal_timestamps_history)
            reason = (
                f"Refused: hourly rate limit reached ({hourly}/{rate_limit} in past hour)."
            )
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        # All 5 safety bounds passed!
        return SafetyCheckResult(allowed=True, config_snapshot=cfg)

    def acquire_heal_slot(self, signature: str) -> Tuple[bool, Optional[str]]:
        """Acquires the single-flight slot and increments counters if check passes."""
        check = self.check_heal_allowed(signature)
        if not check.allowed:
            return False, check.refusal_reason

        now = time.time()
        self._single_flight_active = True
        self._heal_timestamps_history.append(now)
        self._lifetime_heal_count += 1
        self._signature_last_healed[signature] = now

        logger.info(
            f"Heal slot acquired for [{signature}]. "
            f"Lifetime count={self._lifetime_heal_count}, Hourly={len(self._heal_timestamps_history)}"
        )
        return True, None

    def release_heal_slot(self, signature: Optional[str] = None) -> None:
        """Releases the single-flight execution lock."""
        self._single_flight_active = False
        if signature:
            # Update cooldown timestamp to the completion moment
            self._signature_last_healed[signature] = time.time()
        logger.info("Single-flight heal slot released.")

    def get_status(self) -> Dict[str, Any]:
        """Returns the current safety statistics for the Dashboard."""
        cfg = self.reload_config()
        now = time.time()
        one_hour_ago = now - 3600.0
        hourly_count = len([t for t in self._heal_timestamps_history if t > one_hour_ago])

        return {
            "config": cfg,
            "single_flight_active": self._single_flight_active,
            "lifetime_heal_count": self._lifetime_heal_count,
            "hourly_heal_count": hourly_count,
            "active_signatures_in_cooldown": len(self._signature_last_healed)
        }

    def get_grace_period(self) -> float:
        """Returns the active grace period from config."""
        return float(self.reload_config().get("grace_period", 20.0))
