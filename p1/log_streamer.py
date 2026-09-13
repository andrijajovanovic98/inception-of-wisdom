"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Log Streamer: Real-Time Container Log Streaming & Error Pattern Detection
"""

import re
import time
import logging
import threading
from collections import deque
from typing import Optional, List, Callable, Any

from p1.docker_monitor import DockerMonitor

logger = logging.getLogger("p1.log_streamer")

DEFAULT_ERROR_PATTERNS = [
    r"Traceback \(most recent call last\):",
    r"CRITICAL:",
    r"Error:",
    r"Exception:",
    r"ZeroDivisionError",
    r"RuntimeError",
    r"SyntaxError",
    r"ImportError",
    r"ModuleNotFoundError"
]


class LogStreamer:
    """Streams standard output and standard error from the target container in near real time.
    Maintains a rolling ring buffer of recent logs and inspects each line for error signatures.
    """

    def __init__(
        self,
        docker_monitor: DockerMonitor,
        error_patterns: Optional[List[str]] = None,
        max_buffer_lines: int = 500,
        on_error_detected: Optional[Callable[[str, str], Any]] = None
    ):
        self.docker_monitor = docker_monitor
        self.max_buffer_lines = max_buffer_lines
        self.on_error_detected = on_error_detected
        self.on_line: Optional[Callable[[str, bool], None]] = None
        self._line_listeners: List[Callable[[str, bool], None]] = []

        # Compile error patterns into regular expressions
        patterns = error_patterns if error_patterns is not None else DEFAULT_ERROR_PATTERNS
        self.compiled_patterns = [re.compile(p) for p in patterns]

        # Thread-safe ring buffer storing recent log lines
        self.log_buffer: deque = deque(maxlen=max_buffer_lines)
        self._lock = threading.Lock()

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

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
        logger.info(f"LogStreamer started for container '{self.docker_monitor.container_name}'.")

    def stop(self, timeout: float = 2.0) -> None:
        """Stops the streaming thread cleanly."""
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
        self._worker_thread = None
        logger.info("LogStreamer worker stopped.")

    def restart(self) -> None:
        """Reattach Docker log follow (after container recreate / stuck stream)."""
        self.stop(timeout=2.0)
        self.start()

    def _stream_loop(self) -> None:
        """Continuous loop that connects to Docker log stream and handles container restarts."""
        while not self._stop_event.is_set():
            client = self.docker_monitor._get_client()
            if client is None:
                time.sleep(1.0)
                continue

            try:
                container = client.containers.get(self.docker_monitor.container_name)
                container.reload()
                # Only stream if container is running or restarting
                if container.status not in ["running", "restarting"]:
                    time.sleep(1.0)
                    continue

                logger.info(f"Streaming logs from '{container.name}'...")
                # stream=True, follow=True delivers lines in near real time
                log_stream = container.logs(
                    stream=True,
                    follow=True,
                    stdout=True,
                    stderr=True,
                    tail=100
                )

                for chunk in log_stream:
                    if self._stop_event.is_set():
                        break
                    line = chunk.decode("utf-8", errors="replace")
                    self._process_chunk(line)

            except Exception as e:
                # Container stopped, restarted, or daemon temporarily dropped
                logger.warning(f"Log streaming interrupted ({e}). Retrying in 1.5s...")
                time.sleep(1.5)

    def _process_chunk(self, chunk: str) -> None:
        """Splits incoming log chunks by newline and processes each line."""
        lines = chunk.splitlines()
        for line in lines:
            cleaned = line.strip()
            if not cleaned:
                continue

            with self._lock:
                self.log_buffer.append(cleaned)

            is_error = any(p.search(cleaned) for p in self.compiled_patterns)
            self._emit_line(cleaned, is_error)
            if is_error:
                self._check_for_errors(cleaned)

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

    def _check_for_errors(self, line: str) -> None:
        """Triggers crash-context callback when an error pattern matched."""
        logger.warning(f"Error pattern matched in log: '{line}'")
        if self.on_error_detected:
            context_excerpt = self.get_recent_logs_as_text(tail=15)
            try:
                self.on_error_detected(line, context_excerpt)
            except Exception as err:
                logger.error(f"Error in on_error_detected callback: {err}")

    def get_recent_logs(self, count: int = 100) -> List[str]:
        """Returns the most recent log lines as a list."""
        with self._lock:
            logs = list(self.log_buffer)
        return logs[-count:]

    def get_recent_logs_as_text(self, tail: int = 50) -> str:
        """Returns the most recent log lines concatenated as a single string."""
        recent = self.get_recent_logs(tail)
        return "\n".join(recent)

    def clear(self) -> None:
        """Clears the internal log ring buffer."""
        with self._lock:
            self.log_buffer.clear()
