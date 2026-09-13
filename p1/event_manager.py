"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Event Manager: Central Event Hub & Sliding Window Deduplication

Deduplication works on the *incident*, not on the text of a single log line: a crash
signature is derived from the exception type, the offending file and the offending
function, so every line of one traceback collapses into one event.
"""

import re
import os
import time
import uuid
import hashlib
import logging
import threading
from collections import deque
from typing import Optional, List, Dict, Deque, Callable, Any
from dataclasses import dataclass, field, asdict

from p1.docker_monitor import DockerMonitor, ContainerState
from p1.log_streamer import LogStreamer
from p1.http_probe import HttpProbeManager, ProbeResult

logger = logging.getLogger("p1.event_manager")

MAX_STORED_EVENTS = 500
MAX_ACTIVE_SIGNATURES = 500

_TRACEBACK_FRAME_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)(?:,\s+in\s+([A-Za-z0-9_]+))?')
_EXCEPTION_RE = re.compile(r'([A-Za-z0-9_.]*(?:Error|Exception|CRITICAL|Fatal))\s*:\s*(.*)')
# Volatile pieces that differ between two reports of the same incident.
_TIMESTAMP_RE = re.compile(r'\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*\]?')
_HEXADDR_RE = re.compile(r'0x[0-9a-fA-F]+')
# Plain \d+ (not \b\d+\b): a counter glued to a unit like "33s" has no trailing word
# boundary, so a bounded pattern would leave the digits in the key and every poll of
# the same ongoing incident would hash to a different signature.
_NUMBER_RE = re.compile(r'\d+')
_IP_RE = re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b')


@dataclass
class ObserverEvent:
    """Represents an observable event raised by the Observer."""
    id: str
    type: str                  # 'crash', 'suggestion', 'info'
    source: str                # 'docker', 'log', 'http_probe'
    signature: str             # Unique signature hash for deduplication
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)
    count: int = 1             # Number of occurrences within the deduplication window
    first_seen: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def normalize_incident_key(headline: str, context_excerpt: str = "") -> str:
    """Collapse one crash incident to a stable key.

    Prefers the structured facts of a Python traceback (exception type + offending
    file + function). Falls back to the headline with timestamps, IPs, addresses and
    numbers stripped, so the same message logged twice keys identically.
    """
    blob = f"{context_excerpt}\n{headline}" if context_excerpt else headline

    error_type = None
    error_msg = ""
    for line in reversed(blob.splitlines()):
        match = _EXCEPTION_RE.search(line.strip())
        if match:
            error_type = match.group(1)
            error_msg = match.group(2)
            break

    frames = _TRACEBACK_FRAME_RE.findall(blob)
    offending_file = os.path.basename(frames[-1][0]) if frames else None
    offending_func = frames[-1][2] if frames and frames[-1][2] else None

    if error_type:
        norm_msg = _HEXADDR_RE.sub("0xADDR", error_msg)
        norm_msg = _NUMBER_RE.sub("N", norm_msg).strip()
        return f"{error_type}:{offending_file}:{offending_func}:{norm_msg}"

    generic = _TIMESTAMP_RE.sub("", headline)
    generic = _IP_RE.sub("IP", generic)
    generic = _HEXADDR_RE.sub("0xADDR", generic)
    generic = _NUMBER_RE.sub("N", generic)
    return " ".join(generic.split())


class EventManager:
    """Central nervous system of the Observer.
    Aggregates signals from Docker, Logs, and HTTP probes, applies sliding-window
    deduplication, and broadcasts events to listeners (e.g., SSE and Wisdom Loop).
    """

    def __init__(
        self,
        docker_monitor: Optional[DockerMonitor] = None,
        log_streamer: Optional[LogStreamer] = None,
        http_probe: Optional[HttpProbeManager] = None,
        crash_window_seconds: float = 60.0,
        suggestion_window_seconds: float = 300.0,
        docker_poll_seconds: float = 3.0,
        max_events: int = MAX_STORED_EVENTS,
    ):
        self.docker_monitor = docker_monitor
        self.log_streamer = log_streamer
        self.http_probe = http_probe

        self.crash_window_seconds = crash_window_seconds
        self.suggestion_window_seconds = suggestion_window_seconds
        self.docker_poll_seconds = docker_poll_seconds

        # Bounded so a long defense session cannot grow the agent without limit.
        self._events: Deque[ObserverEvent] = deque(maxlen=max_events)
        # Mapping signature -> ObserverEvent for fast active window lookup
        self._active_signatures: Dict[str, ObserverEvent] = {}

        self._lock = threading.Lock()
        self._listeners: List[Callable[[ObserverEvent], None]] = []

        self._docker_poll_stop: Optional[threading.Event] = None
        self._docker_poll_thread: Optional[threading.Thread] = None

        self._wire_components()

    def _wire_components(self) -> None:
        """Connects component callbacks to this event manager."""
        if self.log_streamer:
            self.log_streamer.on_error_detected = self.handle_log_error

        if self.http_probe:
            self.http_probe.on_crash = self.handle_http_crash
            self.http_probe.on_suggestion = self.handle_http_suggestion

    def add_listener(self, callback: Callable[[ObserverEvent], None]) -> None:
        """Subscribes an external listener to newly emitted events."""
        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[ObserverEvent], None]) -> None:
        """Unsubscribes an external listener."""
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _generate_signature(self, source: str, event_type: str, raw_key: str) -> str:
        """Creates a deterministic hash identifying the crash/suggestion signature."""
        normalized = f"{source}:{event_type}:{raw_key.strip().lower()}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    def _evict_expired_locked(self, now: float) -> None:
        """Drop signatures whose window has passed. Caller holds the lock."""
        expired = [
            sig for sig, ev in self._active_signatures.items()
            if (now - ev.last_seen) >= (
                self.suggestion_window_seconds if ev.type == "suggestion"
                else self.crash_window_seconds
            )
        ]
        for sig in expired:
            del self._active_signatures[sig]

        # Hard ceiling as well as the time window: a pathological burst of unique
        # signatures must not be able to grow the table without limit. Oldest first.
        overflow = len(self._active_signatures) - MAX_ACTIVE_SIGNATURES
        if overflow > 0:
            oldest = sorted(self._active_signatures.items(), key=lambda kv: kv[1].last_seen)
            for sig, _ in oldest[:overflow]:
                del self._active_signatures[sig]

    def emit_event(
        self,
        event_type: str,
        source: str,
        raw_key: str,
        summary: str,
        details: Optional[Dict[str, Any]] = None
    ) -> Optional[ObserverEvent]:
        """Emits an event with sliding-window deduplication.
        Subject requirement: A single bad start produces 1 event, not 1 per second.
        """
        now = time.time()
        sig = self._generate_signature(source, event_type, raw_key)
        window = self.suggestion_window_seconds if event_type == "suggestion" else self.crash_window_seconds

        with self._lock:
            self._evict_expired_locked(now)

            existing = self._active_signatures.get(sig)
            if existing is not None and (now - existing.last_seen) < window:
                existing.count += 1
                existing.last_seen = now
                logger.debug(
                    f"Deduplicated {event_type} event [{sig}]: count={existing.count} (window={window}s)"
                )
                return existing

            event = ObserverEvent(
                id=str(uuid.uuid4())[:8],
                type=event_type,
                source=source,
                signature=sig,
                summary=summary,
                details=details or {},
                count=1,
                first_seen=now,
                last_seen=now
            )

            self._active_signatures[sig] = event
            self._events.append(event)
            listeners_to_notify = list(self._listeners)

        logger.info(f"EMITTED {event_type.upper()} event [{sig}]: {summary}")

        # Broadcast outside the lock to prevent deadlocks
        for listener in listeners_to_notify:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Error in event listener: {e}")

        return event

    def handle_docker_state(self, state: ContainerState) -> Optional[ObserverEvent]:
        """Processes a container state change from DockerMonitor."""
        if state.is_crash:
            raw_key = normalize_incident_key(
                f"{state.name}:{state.status}:{state.exit_code}:{state.error_reason}"
            )
            return self.emit_event(
                event_type="crash",
                source="docker",
                raw_key=raw_key,
                summary=state.error_reason or f"Container {state.name} crashed with status {state.status}",
                details=state.to_dict()
            )
        return None

    def handle_log_error(self, matched_line: str, context_excerpt: str) -> Optional[ObserverEvent]:
        """Processes one detected error incident from LogStreamer."""
        raw_key = normalize_incident_key(matched_line, context_excerpt)
        summary = f"Error in container logs: {matched_line[:100]}"
        return self.emit_event(
            event_type="crash",
            source="log",
            raw_key=raw_key,
            summary=summary,
            details={
                "matched_line": matched_line,
                "context_excerpt": context_excerpt
            }
        )

    def handle_http_crash(self, probe: ProbeResult) -> Optional[ObserverEvent]:
        """Processes a 5xx or connection crash from HttpProbeManager."""
        raw_key = f"{probe.url}:{probe.status_code}"
        summary = probe.error_message or f"HTTP probe failed on {probe.url}"
        return self.emit_event(
            event_type="crash",
            source="http_probe",
            raw_key=raw_key,
            summary=summary,
            details=probe.to_dict()
        )

    def handle_http_suggestion(self, probe: ProbeResult) -> Optional[ObserverEvent]:
        """Processes a 4xx suggestion from HttpProbeManager."""
        raw_key = f"{probe.url}:{probe.status_code}"
        summary = probe.error_message or f"HTTP 4xx suggestion on {probe.url}"
        return self.emit_event(
            event_type="suggestion",
            source="http_probe",
            raw_key=raw_key,
            summary=summary,
            details=probe.to_dict()
        )

    def start(self) -> None:
        """Starts wired observer background services."""
        if self.log_streamer:
            self.log_streamer.start()
        if self.http_probe:
            self.http_probe.start()
        # Poll Docker state so container crashes also become table events
        if self._docker_poll_thread is None or not self._docker_poll_thread.is_alive():
            self._docker_poll_stop = threading.Event()
            self._docker_poll_thread = threading.Thread(
                target=self._docker_poll_loop,
                name="DockerStatePoll",
                daemon=True,
            )
            self._docker_poll_thread.start()

    def _docker_poll_loop(self) -> None:
        """Periodically inspect the target and emit crash events."""
        stop_ev = self._docker_poll_stop
        if stop_ev is None:
            return
        while not stop_ev.is_set():
            try:
                if self.docker_monitor:
                    state = self.docker_monitor.inspect()
                    self.handle_docker_state(state)
            except Exception as e:
                logger.debug(f"Docker state poll error: {e}")
            stop_ev.wait(self.docker_poll_seconds)

    def stop(self) -> None:
        """Stops wired observer background services."""
        if self._docker_poll_stop is not None:
            self._docker_poll_stop.set()
        if self._docker_poll_thread is not None and self._docker_poll_thread.is_alive():
            self._docker_poll_thread.join(timeout=2.0)
        self._docker_poll_thread = None
        if self.http_probe:
            self.http_probe.stop()
        if self.log_streamer:
            self.log_streamer.stop()

    def suppress_events_for(self, seconds: float) -> None:
        """Ask every source to stay quiet during a deliberate restart."""
        if self.log_streamer:
            self.log_streamer.suppress_events_for(seconds)
        if self.http_probe:
            self.http_probe.suppress_events_for(seconds)

    def resume_events(self) -> None:
        """End any active suppression window."""
        if self.log_streamer:
            self.log_streamer.resume_events()
        if self.http_probe:
            self.http_probe.resume_events()

    def get_recent_events(self, limit: int = 50) -> List[ObserverEvent]:
        """Returns the most recent raw ObserverEvent objects."""
        with self._lock:
            return list(self._events)[-limit:]

    def get_events(self, event_type: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns the most recent events, optionally filtered by type."""
        with self._lock:
            if event_type:
                filtered = [e for e in self._events if e.type == event_type]
            else:
                filtered = list(self._events)
        return [e.to_dict() for e in filtered[-limit:]]

    def get_latest_crash(self) -> Optional[ObserverEvent]:
        """Returns the most recent crash event if any."""
        with self._lock:
            for event in reversed(self._events):
                if event.type == "crash":
                    return event
        return None

    def clear_events(self) -> None:
        """Clears the event store and signature table."""
        with self._lock:
            self._events.clear()
            self._active_signatures.clear()
