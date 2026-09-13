"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Log Streamer: Real-Time Container Log Streaming & Error Pattern Detection

Two properties matter here and both are enforced with the log's own timestamps
rather than with guesswork:

* Replayed history never raises a crash event. Docker hands back old lines whenever
  the stream reattaches (after a restart, a daemon blip, a container recreate); every
  line carries an RFC3339 timestamp, so anything at or before the last line we already
  consumed is buffered for the dashboard but never re-detected.
* One incident produces one event. A traceback arrives as a burst of matching lines;
  they are collected for a short debounce window and handed to the Observer as a
  single error with the block as context.
"""

from __future__ import annotations

import re
import time
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, List, Optional, Protocol, Tuple

logger = logging.getLogger("p1.log_streamer")

DEFAULT_ERROR_PATTERNS = [
    r"Traceback \(most recent call last\):",
    r"CRITICAL:",
    # \w* (not \b): there is no word boundary inside "RuntimeError", so "\bError:"
    # would silently fail to match the most diagnostic line of a traceback.
    r"\w*(?:Error|Exception):",
    r"ZeroDivisionError",
    r"RuntimeError",
    r"SyntaxError",
    r"ImportError",
    r"ModuleNotFoundError",
]

# Docker prefixes each line with an RFC3339 timestamp when timestamps=True.
_TS_LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T[0-9:.]+Z?)\s?(.*)$", re.DOTALL)
# The most specific line of a traceback is the final "SomeError: message" line.
_EXC_LINE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception)\b\s*:")


class LogSource(Protocol):
    """Anything that can hand out a followed log stream for the target."""

    container_name: str

    def get_log_stream(
        self, tail: Optional[int] = None, since: Optional[int] = None
    ) -> Optional[Any]:
        ...


def _parse_log_line(raw: str) -> Tuple[Optional[float], str]:
    """Split a Docker log line into (epoch seconds, message)."""
    match = _TS_LINE_RE.match(raw)
    if not match:
        return None, raw
    stamp, message = match.group(1), match.group(2)
    try:
        normalised = stamp.rstrip("Z")
        if "." in normalised:
            head, frac = normalised.split(".", 1)
            normalised = f"{head}.{frac[:6]}"
        parsed = datetime.fromisoformat(normalised).replace(tzinfo=timezone.utc)
        return parsed.timestamp(), message
    except ValueError:
        return None, message


class LogStreamer:
    """Streams standard output and standard error from the target in near real time.
    Maintains a rolling ring buffer of recent logs and inspects each line for error
    signatures.
    """

    def __init__(
        self,
        docker_monitor: LogSource,
        error_patterns: Optional[List[str]] = None,
        max_buffer_lines: int = 500,
        on_error_detected: Optional[Callable[[str, str], Any]] = None,
        incident_debounce_seconds: float = 1.0,
        initial_tail: int = 100,
    ):
        self.docker_monitor = docker_monitor
        self.max_buffer_lines = max_buffer_lines
        self.on_error_detected = on_error_detected
        self.incident_debounce_seconds = max(0.0, float(incident_debounce_seconds))
        self.initial_tail = initial_tail
        self.on_line: Optional[Callable[[str, bool], None]] = None
        self._line_listeners: List[Callable[[str, bool], None]] = []

        self.compiled_patterns = self._compile_patterns(
            error_patterns if error_patterns is not None else DEFAULT_ERROR_PATTERNS
        )

        self.log_buffer: Deque[str] = deque(maxlen=max_buffer_lines)
        self._lock = threading.Lock()

        # Newest log timestamp already consumed. Lines at or before it are replays.
        self._last_log_ts: Optional[float] = None
        # Lines written before the agent started are history, never fresh crashes.
        self._agent_start_ts: float = time.time()

        # Incident debouncing
        self._incident_lock = threading.Lock()
        self._pending_error_lines: List[str] = []
        self._incident_timer: Optional[threading.Timer] = None

        # Explicit suppression window, used around a deliberate restart.
        self._suppress_until: float = 0.0

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Patterns
    # ------------------------------------------------------------------
    @staticmethod
    def _compile_patterns(patterns: List[str]) -> List[re.Pattern]:
        """Compile configured patterns, falling back to a literal match if invalid.

        A peer editing iow.config.yml should never be able to crash the Observer with
        an unbalanced bracket.
        """
        compiled: List[re.Pattern] = []
        for pattern in patterns:
            try:
                compiled.append(re.compile(pattern))
            except re.error as err:
                logger.warning(
                    "Invalid error_pattern %r (%s) - matching it literally instead.",
                    pattern, err,
                )
                compiled.append(re.compile(re.escape(pattern)))
        return compiled

    def set_error_patterns(self, patterns: List[str]) -> None:
        """Swap the configured patterns at runtime."""
        self.compiled_patterns = self._compile_patterns(patterns)

    # ------------------------------------------------------------------
    # Listeners / lifecycle
    # ------------------------------------------------------------------
    def add_line_listener(self, callback: Callable[[str, bool], None]) -> None:
        """Subscribe to every stdout/stderr line (SSE dashboard terminal)."""
        if callback not in self._line_listeners:
            self._line_listeners.append(callback)

    def start(self) -> None:
        """Starts the background log streaming worker thread."""
        if self._worker_thread is not None and self._worker_thread.is_alive():
            logger.debug("LogStreamer worker thread is already running.")
            return

        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._stream_loop,
            name="LogStreamerWorker",
            daemon=True
        )
        self._worker_thread.start()
        logger.info(f"LogStreamer started for target '{self.docker_monitor.container_name}'.")

    def stop(self, timeout: float = 2.0) -> None:
        """Stops the streaming thread cleanly."""
        self._stop_event.set()
        with self._incident_lock:
            if self._incident_timer is not None:
                self._incident_timer.cancel()
                self._incident_timer = None
            self._pending_error_lines.clear()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        self._worker_thread = None
        logger.info("LogStreamer worker stopped.")

    def restart(self) -> None:
        """Reattach the log follow (after container recreate / stuck stream)."""
        self.stop(timeout=2.0)
        self.start()

    def suppress_events_for(self, seconds: float) -> None:
        """Ignore error patterns for a moment - used around a deliberate restart."""
        self._suppress_until = max(self._suppress_until, time.time() + max(0.0, seconds))

    def resume_events(self) -> None:
        """End any active suppression window immediately."""
        self._suppress_until = 0.0

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------
    def _stream_loop(self) -> None:
        """Continuous loop that follows the target's logs across restarts."""
        backoff = 1.0
        while not self._stop_event.is_set():
            attached_at = time.time()
            try:
                if self._last_log_ts is None:
                    stream = self.docker_monitor.get_log_stream(tail=self.initial_tail)
                else:
                    # int() truncates downward, so the boundary second is re-sent;
                    # the per-line timestamp check below drops those duplicates.
                    stream = self.docker_monitor.get_log_stream(
                        since=int(self._last_log_ts)
                    )

                if stream is None:
                    self._stop_event.wait(1.0)
                    continue

                logger.info(f"Streaming logs from '{self.docker_monitor.container_name}'...")
                for chunk in stream:
                    if self._stop_event.is_set():
                        break
                    self._process_chunk(chunk.decode("utf-8", errors="replace"))

            except Exception as e:
                logger.warning(f"Log streaming interrupted ({e}).")

            # A stream that ends the moment it opens means the target is not ready
            # (no pod yet, container restarting). Back off instead of hot-looping
            # reattach attempts and flooding the log.
            if time.time() - attached_at < 1.0:
                self._stop_event.wait(backoff)
                backoff = min(backoff * 2.0, 15.0)
            else:
                backoff = 1.0
                self._stop_event.wait(0.5)

    def _process_chunk(self, chunk: str) -> None:
        """Splits incoming log chunks by newline and processes each line."""
        for raw_line in chunk.splitlines():
            if not raw_line.strip():
                continue

            line_ts, message = _parse_log_line(raw_line)
            cleaned = message.strip()
            if not cleaned:
                continue

            # A line we have already consumed is a replay of history.
            is_replay = (
                line_ts is not None
                and self._last_log_ts is not None
                and line_ts <= self._last_log_ts
            )
            # Anything written before the agent came up is pre-existing history.
            is_history = line_ts is not None and line_ts < self._agent_start_ts

            if line_ts is not None:
                self._last_log_ts = max(self._last_log_ts or 0.0, line_ts)

            with self._lock:
                self.log_buffer.append(cleaned)

            is_error = any(p.search(cleaned) for p in self.compiled_patterns)
            self._emit_line(cleaned, is_error)

            if not is_error:
                continue
            if is_replay or is_history:
                logger.debug(f"Ignoring replayed error line: {cleaned[:90]}")
                continue
            if time.time() < self._suppress_until:
                logger.debug(f"Suppressed error line during restart window: {cleaned[:90]}")
                continue

            self._queue_error_line(cleaned)

    def _emit_line(self, line: str, is_error: bool) -> None:
        """Push a live line to on_line + SSE listeners."""
        listeners = list(self._line_listeners)
        if self.on_line:
            listeners.append(self.on_line)
        for cb in listeners:
            try:
                cb(line, is_error)
            except Exception as err:
                logger.error(f"Error in log line listener: {err}")

    # ------------------------------------------------------------------
    # Incident debouncing
    # ------------------------------------------------------------------
    def _queue_error_line(self, line: str) -> None:
        """Collect matching lines so one traceback yields one incident."""
        logger.warning(f"Error pattern matched in log: '{line}'")
        if self.incident_debounce_seconds <= 0:
            self._flush_incident([line])
            return

        with self._incident_lock:
            self._pending_error_lines.append(line)
            if self._incident_timer is None:
                self._incident_timer = threading.Timer(
                    self.incident_debounce_seconds, self._flush_pending_incident
                )
                self._incident_timer.daemon = True
                self._incident_timer.start()

    def _flush_pending_incident(self) -> None:
        with self._incident_lock:
            lines = list(self._pending_error_lines)
            self._pending_error_lines.clear()
            self._incident_timer = None
        if lines:
            self._flush_incident(lines)

    def _flush_incident(self, lines: List[str]) -> None:
        """Hand the whole burst to the Observer as a single detected error."""
        if not self.on_error_detected:
            return
        headline = self._pick_headline(lines)
        context_excerpt = self.get_recent_logs_as_text(tail=40)
        try:
            self.on_error_detected(headline, context_excerpt)
        except Exception as err:
            logger.error(f"Error in on_error_detected callback: {err}")

    @staticmethod
    def _pick_headline(lines: List[str]) -> str:
        """The most diagnostic line of a burst: the exception line when present."""
        for line in reversed(lines):
            if _EXC_LINE_RE.search(line):
                return line
        return lines[-1] if lines else ""

    # ------------------------------------------------------------------
    # Buffer access
    # ------------------------------------------------------------------
    def get_recent_logs(self, count: int = 100) -> List[str]:
        """Returns the most recent log lines as a list."""
        with self._lock:
            logs = list(self.log_buffer)
        return logs[-count:]

    def get_recent_logs_as_text(self, tail: int = 50) -> str:
        """Returns the most recent log lines concatenated as a single string."""
        return "\n".join(self.get_recent_logs(tail))

    def clear(self) -> None:
        """Clears the internal log ring buffer."""
        with self._lock:
            self.log_buffer.clear()
