"""
Inception-of-Wisdom (IoW) — Part 1: Observer
Event Manager: Central Event Hub & Sliding Window Deduplication
"""

import time
import uuid
import hashlib
import logging
import threading
from typing import Optional, List, Dict, Callable, Any
from dataclasses import dataclass, field, asdict

from p1.docker_monitor import DockerMonitor, ContainerState
from p1.log_streamer import LogStreamer
from p1.http_probe import HttpProbeManager, ProbeResult

logger = logging.getLogger("p1.event_manager")


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
        crash_window_seconds: float = 10.0,
        suggestion_window_seconds: float = 60.0
    ):
        self.docker_monitor = docker_monitor
        self.log_streamer = log_streamer
        self.http_probe = http_probe

        self.crash_window_seconds = crash_window_seconds
        self.suggestion_window_seconds = suggestion_window_seconds

        # Storage for events
        self._events: List[ObserverEvent] = []
        # Mapping signature -> ObserverEvent for fast active window lookup
        self._active_signatures: Dict[str, ObserverEvent] = {}

        self._lock = threading.Lock()
        self._listeners: List[Callable[[ObserverEvent], None]] = []

        # Wire up callbacks if components are supplied
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
            # Check if this exact signature already fired within its deduplication window
            existing = self._active_signatures.get(sig)
            if existing:
                if (now - existing.last_seen) < window:
                    existing.count += 1
                    existing.last_seen = now
                    logger.debug(
                        f"Deduplicated {event_type} event [{sig}]: count={existing.count} (window={window}s)"
                    )
                    return existing
                else:
                    # Window expired, allow a new event instance
                    del self._active_signatures[sig]

            # Create a brand new event
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
            raw_key = f"{state.name}:{state.status}:{state.exit_code}:{state.error_reason}"
            return self.emit_event(
                event_type="crash",
                source="docker",
                raw_key=raw_key,
                summary=state.error_reason or f"Container {state.name} crashed with status {state.status}",
                details=state.to_dict()
            )
        return None

    def handle_log_error(self, matched_line: str, context_excerpt: str) -> Optional[ObserverEvent]:
        """Processes a matching error pattern from LogStreamer."""
        raw_key = matched_line
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
        raw_key = f"{probe.url}:{probe.status_code}:{probe.error_message}"
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

