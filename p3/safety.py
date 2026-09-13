"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Safety Guardrails: Single-Flight, Cooldown, Rate Limit, Hard Cap & Kill-Switch

Also the single reader of iow.config.yml. The five timing bounds are validated and
merged into the safety config; every other section of the file (target:, analyst:, …)
is kept verbatim and served through get_section() so nothing in the file is dead.
"""

from __future__ import annotations

import os
import time
import threading
import logging
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, asdict, field

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment,misc]

logger = logging.getLogger("p3.safety")

DEFAULT_CONFIG_PATH = "demo_app/iow.config.yml"

# name -> (python type, minimum allowed value)
_BOUND_SPECS: Dict[str, Tuple[type, float]] = {
    "grace_period": (float, 0.0),
    "cooldown": (float, 0.0),
    "rate_limit_per_hour": (int, 1),
    "hard_cap": (int, 1),
}


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
    Dynamic: re-reads iow.config.yml automatically when modified on disk.
    """

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = os.path.abspath(config_path or DEFAULT_CONFIG_PATH)
        self._last_mtime: float = -1.0
        self._config: Dict[str, Any] = self._load_defaults()
        self._raw: Dict[str, Any] = {}

        # Operational state. The lock makes acquire_heal_slot a single atomic
        # test-and-set, so two threads can never both win the single-flight slot.
        self._state_lock = threading.RLock()
        self._single_flight_active: bool = False
        self._signature_last_healed: Dict[str, float] = {}
        self._heal_timestamps_history: List[float] = []
        self._lifetime_heal_count: int = 0

        self.reload_config()

    def _load_defaults(self) -> Dict[str, Any]:
        return {
            "grace_period": 20.0,
            "cooldown": 300.0,
            "rate_limit_per_hour": 5,
            "hard_cap": 20,
            "kill_switch": False,
        }

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _coerce_bound(self, key: str, value: Any) -> Optional[Any]:
        """Validate one timing bound. Returns None when the value is unusable."""
        if key == "kill_switch":
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "on", "1")
            return bool(value)

        caster, minimum = _BOUND_SPECS[key]
        try:
            cast = caster(value)
        except (TypeError, ValueError):
            logger.warning(
                "iow.config.yml: '%s: %r' is not a number - keeping %r",
                key, value, self._config.get(key),
            )
            return None
        if cast < minimum:
            logger.warning(
                "iow.config.yml: '%s: %r' is below the minimum %s - keeping %r",
                key, value, minimum, self._config.get(key),
            )
            return None
        return cast

    def _parse_file(self) -> Dict[str, Any]:
        with open(self.config_path, "r", encoding="utf-8") as f:
            if yaml is not None:
                parsed = yaml.safe_load(f) or {}
                return parsed if isinstance(parsed, dict) else {}

            # Minimal fallback parser for the flat bounds only (PyYAML absent).
            flat: Dict[str, Any] = {}
            for line in f:
                if ":" not in line or line.strip().startswith("#") or line.startswith((" ", "\t", "-")):
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.split("#")[0].strip()
                if not v:
                    continue
                if v.lower() in ("true", "false"):
                    flat[k] = v.lower() == "true"
                else:
                    try:
                        flat[k] = float(v) if "." in v else int(v)
                    except ValueError:
                        flat[k] = v
            return flat

    def reload_config(self) -> Dict[str, Any]:
        """Reads iow.config.yml when it changed on disk, without restarting the agent."""
        if not os.path.isfile(self.config_path):
            logger.warning(f"Configuration file not found at {self.config_path}. Using defaults.")
            return self._config

        try:
            mtime = os.path.getmtime(self.config_path)
            if mtime == self._last_mtime:
                return self._config

            parsed = self._parse_file()

            for key in self._load_defaults():
                if key in parsed:
                    coerced = self._coerce_bound(key, parsed[key])
                    if coerced is not None:
                        self._config[key] = coerced

            # Everything else in the file (target:, analyst:, …) stays available.
            self._raw = parsed
            self._last_mtime = mtime
            logger.info(
                f"Reloaded configuration from {os.path.basename(self.config_path)}: "
                f"grace_period={self._config['grace_period']}s, "
                f"cooldown={self._config['cooldown']}s, "
                f"rate_limit={self._config['rate_limit_per_hour']}/h, "
                f"hard_cap={self._config['hard_cap']}, "
                f"kill_switch={self._config['kill_switch']}"
                + (f", sections={sorted(k for k in parsed if isinstance(parsed[k], dict))}"
                   if parsed else "")
            )
        except Exception as e:
            logger.error(f"Error reading configuration file {self.config_path}: {e}")

        return self._config

    def get_section(self, name: str) -> Dict[str, Any]:
        """Return a nested mapping from iow.config.yml (e.g. 'target', 'analyst')."""
        self.reload_config()
        section = self._raw.get(name)
        return dict(section) if isinstance(section, dict) else {}

    def get_raw_config(self) -> Dict[str, Any]:
        """The full parsed configuration file."""
        self.reload_config()
        return dict(self._raw)

    # ------------------------------------------------------------------
    # Bounds
    # ------------------------------------------------------------------
    def _check_locked(self, signature: str, now: float, cfg: Dict[str, Any]) -> SafetyCheckResult:
        """Evaluate all 5 bounds. Caller must hold _state_lock."""
        if cfg.get("kill_switch", False):
            reason = "Refused: global kill_switch is active (kill_switch=true in iow.config.yml)."
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        if self._single_flight_active:
            reason = "Refused: single-flight constraint violated (another heal cycle is currently running)."
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        hard_cap = int(cfg.get("hard_cap", 20))
        if self._lifetime_heal_count >= hard_cap:
            reason = (
                f"Refused: lifetime hard cap reached "
                f"({self._lifetime_heal_count}/{hard_cap} heals started)."
            )
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        cooldown = float(cfg.get("cooldown", 300.0))
        last_heal_time = self._signature_last_healed.get(signature)
        if last_heal_time is not None:
            time_since_last = now - last_heal_time
            if time_since_last < cooldown:
                remaining = round(cooldown - time_since_last, 1)
                reason = f"Refused: cooldown active for signature '{signature}' ({remaining}s remaining)."
                logger.warning(f"Safety constraint triggered: {reason}")
                return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        rate_limit = int(cfg.get("rate_limit_per_hour", 5))
        one_hour_ago = now - 3600.0
        self._heal_timestamps_history = [
            t for t in self._heal_timestamps_history if t > one_hour_ago
        ]
        if len(self._heal_timestamps_history) >= rate_limit:
            hourly = len(self._heal_timestamps_history)
            reason = f"Refused: hourly rate limit reached ({hourly}/{rate_limit} in past hour)."
            logger.warning(f"Safety constraint triggered: {reason}")
            return SafetyCheckResult(allowed=False, refusal_reason=reason, config_snapshot=cfg)

        return SafetyCheckResult(allowed=True, config_snapshot=cfg)

    def check_heal_allowed(self, signature: str) -> SafetyCheckResult:
        """Evaluates all 5 safety bounds to decide if a new heal is permitted."""
        cfg = self.reload_config()
        with self._state_lock:
            return self._check_locked(signature, time.time(), cfg)

    def acquire_heal_slot(self, signature: str) -> Tuple[bool, Optional[str]]:
        """Atomically checks the bounds and takes the single-flight slot."""
        cfg = self.reload_config()
        with self._state_lock:
            now = time.time()
            check = self._check_locked(signature, now, cfg)
            if not check.allowed:
                return False, check.refusal_reason

            self._single_flight_active = True
            self._heal_timestamps_history.append(now)
            self._lifetime_heal_count += 1
            self._signature_last_healed[signature] = now
            lifetime = self._lifetime_heal_count
            hourly = len(self._heal_timestamps_history)

        logger.info(
            f"Heal slot acquired for [{signature}]. "
            f"Lifetime count={lifetime}, Hourly={hourly}"
        )
        return True, None

    def release_heal_slot(self, signature: Optional[str] = None) -> None:
        """Releases the single-flight execution lock."""
        with self._state_lock:
            self._single_flight_active = False
            if signature:
                # Cooldown counts from the completion moment.
                self._signature_last_healed[signature] = time.time()
        logger.info("Single-flight heal slot released.")

    def get_status(self) -> Dict[str, Any]:
        """Returns the current safety statistics for the Dashboard."""
        cfg = self.reload_config()
        now = time.time()
        one_hour_ago = now - 3600.0
        with self._state_lock:
            hourly_count = len([t for t in self._heal_timestamps_history if t > one_hour_ago])
            return {
                "config": dict(cfg),
                "config_path": self.config_path,
                "single_flight_active": self._single_flight_active,
                "lifetime_heal_count": self._lifetime_heal_count,
                "hourly_heal_count": hourly_count,
                "active_signatures_in_cooldown": len(self._signature_last_healed),
            }

    def get_grace_period(self) -> float:
        """Returns the active grace period from config."""
        return float(self.reload_config().get("grace_period", 20.0))
