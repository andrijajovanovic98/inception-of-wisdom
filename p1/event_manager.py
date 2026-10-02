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
from p1.log_streamer import LogStreamer, latest_error_block
from p1.http_probe import HttpProbeManager, ProbeResult

logger = logging.getLogger("p1.event_manager")

MAX_STORED_EVENTS = 500
MAX_ACTIVE_SIGNATURES = 500
# Signal kinds that mean "the target is down": that incident is over once it serves again.
DOWN_KINDS = frozenset({"exit", "restart_loop", "oom", "dead", "unreachable"})
# Consecutive healthy probe rounds (container running) that end a down incident.
RECOVERY_ROUNDS = 2

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
    # Only the latest error report counts: the context window can still hold an older
    # traceback, whose frames used to give one incident several signatures.
    if context_excerpt:
        context_excerpt = "\n".join(latest_error_block(context_excerpt.splitlines()))
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
        crash_window_seconds: float = 90.0,
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
        # Called on every occurrence, deduplicated repeats included (the Verifier: a
        # patched target crashing the *same* way inside the window is still a crash).
        self._occurrence_listeners: List[Callable[[ObserverEvent], None]] = []

        self._docker_poll_stop: Optional[threading.Event] = None
        self._docker_poll_thread: Optional[threading.Thread] = None
        self._docker_events_stop: Optional[threading.Event] = None
        self._docker_events_thread: Optional[threading.Thread] = None
        # Container-side suppression while a deliberate restart is in flight (the
        # probe and log sources keep their own windows).
        self._suppress_until: float = 0.0
        self._healthy_rounds = 0

        self._wire_components()

    def _wire_components(self) -> None:
        """Connects component callbacks to this event manager."""
        if self.log_streamer:
            self.log_streamer.on_error_detected = self.handle_log_error

        if self.http_probe:
            self.http_probe.on_crash = self.handle_http_crash
            self.http_probe.on_suggestion = self.handle_http_suggestion
            self.http_probe.on_round = self.handle_probe_round

    def add_listener(
        self, callback: Callable[[ObserverEvent], None], every_occurrence: bool = False
    ) -> None:
        """Subscribes a listener to newly emitted events - or, with every_occurrence,
        to every occurrence including the repeats deduplication folds into one event."""
        with self._lock:
            target = self._occurrence_listeners if every_occurrence else self._listeners
            if callback not in target:
                target.append(callback)

    def remove_listener(self, callback: Callable[[ObserverEvent], None]) -> None:
        """Unsubscribes an external listener."""
        with self._lock:
            for target in (self._listeners, self._occurrence_listeners):
                if callback in target:
                    target.remove(callback)

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
                event = existing
                is_new = False
                listeners_to_notify = list(self._occurrence_listeners)
            else:
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
                is_new = True
                listeners_to_notify = list(self._listeners) + list(self._occurrence_listeners)

        if is_new:
            logger.info(f"EMITTED {event_type.upper()} event [{sig}]: {summary}")

        # Broadcast outside the lock to prevent deadlocks
        for listener in listeners_to_notify:
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Error in event listener: {e}")

        return event

    def _recent_log_tail(self, lines: int = 40) -> str:
        return self.log_streamer.get_recent_logs_as_text(tail=lines) if self.log_streamer else ""

    @staticmethod
    def _docker_crash_kind(state: ContainerState) -> str:
        reason = (state.error_reason or "").lower()
        # Docker's wording, and Kubernetes' (KubeTargetMonitor in gitops mode).
        if "restart loop" in reason or "crashloopbackoff" in reason:
            return "restart_loop"
        if "oom" in reason:
            return "oom"
        if state.status == "exited" or "terminated with exit code" in reason:
            return "exit"
        return "dead"

    def handle_docker_state(self, state: ContainerState) -> Optional[ObserverEvent]:
        """Processes a container state change from DockerMonitor (poll or event stream)."""
        if not state.is_crash:
            return None
        if time.time() < self._suppress_until:
            logger.debug(f"Suppressed docker crash signal during restart window: {state.error_reason}")
            return None
        kind = self._docker_crash_kind(state)
        raw_key = normalize_incident_key(f"{state.name}:{kind}:{state.error_reason}")
        details = state.to_dict()
        details["kind"] = kind
        # The container side knows *that* it died, the log knows *why*: carry the
        # tail along so the Analyst never starts from a bare "exited 1".
        details["context_excerpt"] = self._recent_log_tail()
        summary = state.error_reason or f"Container {state.name} crashed with status {state.status}"
        if kind == "exit" and state.exit_code is not None:
            summary = f"Container exited with status {state.exit_code}"
        return self.emit_event(
            event_type="crash",
            source="docker",
            raw_key=raw_key,
            summary=summary,
            details=details,
        )

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
                "kind": "log_pattern",
                "matched_line": matched_line,
                "context_excerpt": context_excerpt
            }
        )

    def handle_http_crash(self, probe: ProbeResult) -> Optional[ObserverEvent]:
        """Processes a 5xx or connection crash from HttpProbeManager."""
        details = probe.to_dict()
        if probe.status_code is None:
            # Refused / reset / timed out: the *target* is down, whichever URL
            # noticed first. One incident, one event - not one per probe path.
            raw_key = "target-unreachable"
            details["kind"] = "unreachable"
            summary = f"Target unreachable: {probe.error_message or probe.url}"
        else:
            raw_key = f"{probe.url}:{probe.status_code}"
            details["kind"] = "http_5xx"
            summary = probe.error_message or f"HTTP probe failed on {probe.url}"
        details["context_excerpt"] = self._recent_log_tail()
        return self.emit_event(
            event_type="crash",
            source="http_probe",
            raw_key=raw_key,
            summary=summary,
            details=details,
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
        # Follow the daemon's event stream for exits the poll is too slow to see.
        watch = getattr(self.docker_monitor, "watch_events", None)
        if callable(watch) and (
            self._docker_events_thread is None or not self._docker_events_thread.is_alive()
        ):
            stop_ev = threading.Event()
            self._docker_events_stop = stop_ev
            self._docker_events_thread = threading.Thread(
                target=watch,
                args=(self.handle_docker_state, stop_ev),
                name="DockerEventStream",
                daemon=True,
            )
            self._docker_events_thread.start()

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
        if self._docker_events_stop is not None:
            self._docker_events_stop.set()
            closer = getattr(self.docker_monitor, "close_event_stream", None)
            if callable(closer):
                closer()
        if self._docker_events_thread is not None and self._docker_events_thread.is_alive():
            self._docker_events_thread.join(timeout=2.0)
        self._docker_events_thread = None
        if self.http_probe:
            self.http_probe.stop()
        if self.log_streamer:
            self.log_streamer.stop()

    def suppress_events_for(self, seconds: float) -> None:
        """Ask every source to stay quiet during a deliberate restart."""
        self._suppress_until = max(self._suppress_until, time.time() + max(0.0, seconds))
        if self.log_streamer:
            self.log_streamer.suppress_events_for(seconds)
        if self.http_probe:
            self.http_probe.suppress_events_for(seconds)

    def resume_events(self) -> None:
        """End any active suppression window."""
        self._suppress_until = 0.0
        if self.log_streamer:
            self.log_streamer.resume_events()
        if self.http_probe:
            self.http_probe.resume_events()

    def find_event(self, signature: str) -> Optional[ObserverEvent]:
        """Most recent event with this signature (manual heal of a picked crash)."""
        with self._lock:
            for event in reversed(self._events):
                if event.signature == signature:
                    return event
        return None

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

    def handle_probe_round(self, results: List[ProbeResult]) -> None:
        """End the "target down" incidents once the target serves again.

        However it came back - healed, fixed by hand, restarted - the outage is over,
        and the same crash coming back is a new bad start. Otherwise every repeat of a
        crash loop planted again within the window extended the old incident, and no
        event was raised until Docker's restart backoff outgrew the window (seen live:
        207 s after a rollback and a `make demo-reset`). Tracebacks and 5xx keep their
        time window, so a soft crash repeated by hand still folds into one event.
        """
        healthy = (
            bool(results)
            and any(r.is_healthy for r in results)
            and not any(r.is_crash for r in results)
        )
        state = self.docker_monitor.get_last_state() if self.docker_monitor else None
        if state is not None and state.status != "running":
            healthy = False
        self._healthy_rounds = self._healthy_rounds + 1 if healthy else 0
        if self._healthy_rounds < RECOVERY_ROUNDS:
            return
        with self._lock:
            closed = [
                sig for sig, ev in self._active_signatures.items()
                if ev.type == "crash" and ev.details.get("kind") in DOWN_KINDS
            ]
            for sig in closed:
                del self._active_signatures[sig]
        if closed:
            logger.info(f"Target serves again: closed {len(closed)} 'target down' incident(s).")

    def close_incidents(self, event_type: str = "crash") -> int:
        """End every active incident of this type (the events stay in the history).

        Called when the target was verified healthy: the incident is over, and the same
        crash coming back is a new bad start. Without this a crash planted again within
        the window was folded into the healed incident - every repeat extended the
        sliding window - and raised no event until Docker's restart backoff outgrew it
        (seen live: three minutes of silence).
        """
        with self._lock:
            closed = [sig for sig, ev in self._active_signatures.items() if ev.type == event_type]
            for sig in closed:
                del self._active_signatures[sig]
        if closed:
            logger.info(f"Closed {len(closed)} {event_type} incident(s): the target was verified healthy.")
        return len(closed)

    def clear_events(self) -> None:
        """Clears the event store and signature table."""
        with self._lock:
            self._events.clear()
            self._active_signatures.clear()
