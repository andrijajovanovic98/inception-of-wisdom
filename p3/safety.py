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
from typing import Dict, List, Optional, Any, Set, Tuple
from dataclasses import dataclass, asdict, field

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment,misc]

from p1.log_streamer import DEFAULT_ERROR_PATTERNS

logger = logging.getLogger("p3.safety")

DEFAULT_CONFIG_PATH = "demo_app/iow.config.yml"

# name -> (python type, minimum allowed value)
_BOUND_SPECS: Dict[str, Tuple[type, float]] = {
    "grace_period": (float, 0.0),
    "cooldown": (float, 0.0),
    "rate_limit_per_hour": (int, 1),
    "hard_cap": (int, 1),
    # How long a redeployed target may take to answer before the attempt fails.
    "boot_timeout": (float, 1.0),
}

_TRUE_WORDS = ("true", "yes", "on", "1")
_FALSE_WORDS = ("false", "no", "off", "0")

# The target:/analyst: settings that apply without a restart: (section, key, type,
# default, minimum). container_name, service_url, model and embedding_model are read
# once at start-up.
LIVE_SETTINGS: Tuple[Tuple[str, str, type, float, float], ...] = (
    ("target", "probe_interval_seconds", float, 3.0, 0.5),
    ("target", "probe_timeout_seconds", float, 2.0, 0.1),
    ("target", "incident_debounce_seconds", float, 1.0, 0.0),
    ("target", "crash_dedup_window_seconds", float, 90.0, 1.0),
    ("target", "suggestion_dedup_window_seconds", float, 300.0, 1.0),
    ("analyst", "watch_interval_seconds", float, 3.0, 0.5),
    ("analyst", "top_k", int, 3, 1),
)
LIVE_LISTS: Tuple[Tuple[str, List[str]], ...] = (
    ("probe_urls", ["/", "/healthz"]),
    ("error_patterns", list(DEFAULT_ERROR_PATTERNS)),
)


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
        self._failed_mtime: float = -1.0
        self._config: Dict[str, Any] = self._load_defaults()
        self._raw: Dict[str, Any] = {}
        # Bumped on every successful (re)load, so other readers of the file's sections
        # (the Observer and Analyst settings) know when to re-apply them.
        self.version: int = 0

        # Operational state. The lock makes acquire_heal_slot a single atomic
        # test-and-set, so two threads can never both win the single-flight slot.
        self._state_lock = threading.RLock()
        self._single_flight_active: bool = False
        self._signature_last_healed: Dict[str, float] = {}
        self._heal_timestamps_history: List[float] = []
        self._lifetime_heal_count: int = 0
        # Signatures refused by single-flight during the running heal (see release).
        self._flight_echoes: Set[str] = set()
        # Most recent refusal, so the dashboard can say *why* nothing happened.
        self.last_refusal: Optional[Dict[str, Any]] = None

        self.reload_config()

    def _load_defaults(self) -> Dict[str, Any]:
        return {
            "grace_period": 20.0,
            "cooldown": 300.0,
            "rate_limit_per_hour": 5,
            "hard_cap": 20,
            "boot_timeout": 45.0,
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
            word = str(value).strip().lower()
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
            # A typo must never silently disarm the kill switch.
            logger.warning(
                "iow.config.yml: 'kill_switch: %r' is not a boolean - keeping %r",
                value, self._config.get("kill_switch"),
            )
            return None

        caster, minimum = _BOUND_SPECS[key]
        if isinstance(value, bool):
            logger.warning(
                "iow.config.yml: '%s: %r' is not a number - keeping %r",
                key, value, self._config.get(key),
            )
            return None
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
                parsed = yaml.safe_load(f)
                # An editor saving with truncate+write briefly exposes an empty file;
                # reading that as "all defaults" would disarm an active kill switch.
                if not isinstance(parsed, dict) or not parsed:
                    raise ValueError("configuration is empty or not a YAML mapping")
                return parsed

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
            if mtime in (self._last_mtime, self._failed_mtime):
                return self._config

            parsed = self._parse_file()

            # The file is the source of truth: a key the peer deleted goes back to
            # its default instead of silently keeping the last value it had.
            fresh = self._load_defaults()
            for key in fresh:
                if key in parsed:
                    coerced = self._coerce_bound(key, parsed[key])
                    fresh[key] = coerced if coerced is not None else self._config.get(key, fresh[key])
            self._config = fresh

            # Everything else in the file (target:, analyst:, …) stays available.
            self._raw = parsed
            self._last_mtime = mtime
            self._failed_mtime = -1.0
            self.version += 1
            logger.info(
                f"Reloaded configuration from {os.path.basename(self.config_path)}: "
                f"grace_period={self._config['grace_period']}s, "
                f"cooldown={self._config['cooldown']}s, "
                f"rate_limit={self._config['rate_limit_per_hour']}/h, "
                f"hard_cap={self._config['hard_cap']}, "
                f"boot_timeout={self._config['boot_timeout']}s, "
                f"kill_switch={self._config['kill_switch']}"
                + (f", sections={sorted(k for k in parsed if isinstance(parsed[k], dict))}"
                   if parsed else "")
            )
        except Exception as e:
            # Keep the last good values; report the broken file once, not per poll.
            try:
                self._failed_mtime = os.path.getmtime(self.config_path)
            except OSError:
                pass
            logger.error(
                f"Error reading configuration file {self.config_path}: {e} "
                "- keeping the previous values."
            )

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
                self.last_refusal = {
                    "signature": signature,
                    "reason": check.refusal_reason,
                    "timestamp": now,
                }
                if self._single_flight_active:
                    # Raised while a heal is running: another face of the same incident
                    # (exit code / log line / probe of one crash). It shares the cooldown.
                    self._flight_echoes.add(signature)
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

    def acquire_exclusive_slot(self, label: str) -> Tuple[bool, Optional[str]]:
        """Single-flight for an operator action that patches and restarts the target
        (landing an approved PR).

        The kill switch and single-flight apply; it is not an autonomous heal, so it is
        not counted against the hourly rate limit or the lifetime cap (a human approval
        cannot turn into a loop - and an approval refused for a rate limit spent by the
        proposal itself was seen live). Released with release_heal_slot().
        """
        cfg = self.reload_config()
        with self._state_lock:
            now = time.time()
            reason: Optional[str] = None
            if cfg.get("kill_switch", False):
                reason = "Refused: global kill_switch is active (kill_switch=true in iow.config.yml)."
            elif self._single_flight_active:
                reason = (
                    "Refused: single-flight constraint violated "
                    "(another heal cycle is currently running)."
                )
            if reason:
                self.last_refusal = {"signature": label, "reason": reason, "timestamp": now}
                logger.warning(f"Safety constraint triggered for '{label}': {reason}")
                return False, reason
            self._single_flight_active = True
        logger.info(f"Exclusive slot acquired for [{label}] (not counted as a heal).")
        return True, None

    def release_heal_slot(self, signature: Optional[str] = None) -> None:
        """Releases the single-flight execution lock."""
        with self._state_lock:
            self._single_flight_active = False
            now = time.time()
            if signature:
                # Cooldown counts from the completion moment.
                self._signature_last_healed[signature] = now
            # One crash raises several signatures (container exit, log pattern, dead
            # probe). Whatever was refused while this heal ran belongs to the same
            # incident: without this, a failed heal was retried straight away through
            # a sibling signature - the symptom retriggering the loop on its own.
            echoes = sorted(self._flight_echoes)
            for echo in echoes:
                self._signature_last_healed[echo] = now
            self._flight_echoes.clear()
        if echoes:
            logger.info(
                f"Single-flight heal slot released; cooldown also covers {len(echoes)} "
                "sibling signature(s) raised during the heal."
            )
        else:
            logger.info("Single-flight heal slot released.")

    def get_status(self) -> Dict[str, Any]:
        """Returns the current safety statistics for the Dashboard."""
        cfg = self.reload_config()
        now = time.time()
        one_hour_ago = now - 3600.0
        with self._state_lock:
            hourly_count = len([t for t in self._heal_timestamps_history if t > one_hour_ago])
            cooldown = float(cfg.get("cooldown", 300.0))
            in_cooldown = sum(
                1 for ts in self._signature_last_healed.values() if now - ts < cooldown
            )
            return {
                "config": dict(cfg),
                "config_path": self.config_path,
                "single_flight_active": self._single_flight_active,
                "lifetime_heal_count": self._lifetime_heal_count,
                "hourly_heal_count": hourly_count,
                "active_signatures_in_cooldown": in_cooldown,
                "last_refusal": dict(self.last_refusal) if self.last_refusal else None,
            }

    def get_grace_period(self) -> float:
        """Returns the active grace period from config."""
        return float(self.reload_config().get("grace_period", 20.0))

    def get_boot_timeout(self) -> float:
        """How long a redeployed target may take to answer (iow.config.yml)."""
        return float(self.reload_config().get("boot_timeout", 45.0))

    def read_live_settings(self, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The live target:/analyst: settings (LIVE_SETTINGS, LIVE_LISTS), validated.

        An unusable value keeps the previous one (the default at start-up) - it used to
        raise in the dashboard's constructor, so one typo stopped the agent from starting.
        """
        sections = {"target": self.get_section("target"), "analyst": self.get_section("analyst")}
        fallback = previous or {}
        settings: Dict[str, Any] = {}
        for section, key, cast, default, minimum in LIVE_SETTINGS:
            keep = fallback.get(key, default)
            raw = sections[section].get(key, default)
            try:
                if isinstance(raw, bool):
                    raise ValueError(raw)
                value = cast(raw)
                if value < minimum:
                    raise ValueError(raw)
            except (TypeError, ValueError):
                logger.warning(
                    "iow.config.yml: '%s.%s: %r' is not usable (minimum %s) - keeping %r",
                    section, key, raw, minimum, keep,
                )
                value = keep
            settings[key] = value
        for key, default_list in LIVE_LISTS:
            raw = sections["target"].get(key, default_list)
            if isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
                settings[key] = list(raw)
            else:
                keep = fallback.get(key, default_list)
                logger.warning(
                    "iow.config.yml: 'target.%s' must be a non-empty list of strings - keeping %r",
                    key, keep,
                )
                settings[key] = keep
        return settings
