"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Docker Monitor: Container State Inspection & Lifecycle Management
"""

from __future__ import annotations

import time
import logging
import threading
from collections import deque
from typing import Any, Deque, Dict, Iterator, Optional, Tuple
from dataclasses import dataclass, asdict

try:
    import docker
    from docker.errors import DockerException, NotFound, APIError
except ImportError:
    docker = None
    DockerException = Exception
    NotFound = Exception
    APIError = Exception

logger = logging.getLogger("p1.docker_monitor")

# A restart loop is repeated restarts in a short window - not the single transient
# 'restarting' state every ordinary `docker restart` passes through.
RESTART_LOOP_WINDOW_S = 60.0
RESTART_LOOP_THRESHOLD = 2
# Docker backs off exponentially between policy restarts, so a container that is
# genuinely crash-looping eventually sits in 'restarting' with a static RestartCount.
# A deliberate `docker restart` clears that state again within a second or two.
RESTART_STUCK_S = 12.0


@dataclass
class ContainerState:
    """Represents the current snapshot of a monitored container."""
    name: str
    status: str            # 'running', 'restarting', 'exited', 'not_found', 'daemon_unavailable'
    exit_code: Optional[int] = None
    restart_count: int = 0
    is_crash: bool = False
    error_reason: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DockerMonitor:
    """Observes the target container lifecycle via the host Docker daemon.
    Resilient to daemon disconnects and container recreation.
    """

    def __init__(self, container_name: str = "iow_demo_target", base_url: Optional[str] = None):
        self.container_name = container_name
        self.base_url = base_url or "unix:///var/run/docker.sock"
        self._client: Optional[Any] = None
        self._client_lock = threading.Lock()
        self._last_state: Optional[ContainerState] = None
        # (timestamp, restart_count) samples used to detect a real restart loop.
        self._restart_samples: Deque[Tuple[float, int]] = deque(maxlen=64)
        # When the container first entered 'restarting' and stayed there.
        self._restarting_since: Optional[float] = None

    def _get_client(self) -> Optional[Any]:
        """Lazy and fault-tolerant Docker client initializer."""
        if docker is None:
            logger.error("The 'docker' python library is not installed.")
            return None

        with self._client_lock:
            if self._client is not None:
                try:
                    self._client.ping()
                    return self._client
                except Exception:
                    logger.warning("Docker daemon ping failed. Attempting to reconnect...")
                    self._client = None

            try:
                # Prefer env (DOCKER_HOST) then the unix socket - campus users run a
                # rootless daemon whose socket is not /var/run/docker.sock.
                try:
                    self._client = docker.from_env()
                    self._client.ping()
                except Exception:
                    self._client = docker.DockerClient(base_url=self.base_url)
                    self._client.ping()
                logger.info("Connected to Docker daemon")
                return self._client
            except Exception as e:
                logger.warning(
                    "Unable to connect to Docker daemon (%s). "
                    "Is the process allowed to access the socket "
                    "(DOCKER_HOST / 'docker' group), or is the daemon running?",
                    e,
                )
                self._client = None
                return None

    def _is_restart_loop(self, now: float, restart_count: int) -> bool:
        """True when the container restarted repeatedly inside the recent window."""
        self._restart_samples.append((now, restart_count))
        cutoff = now - RESTART_LOOP_WINDOW_S
        recent = [c for ts, c in self._restart_samples if ts >= cutoff]
        if len(recent) < 2:
            return False
        return (max(recent) - min(recent)) >= RESTART_LOOP_THRESHOLD

    def inspect(self) -> ContainerState:
        """Inspects the target container and determines if a crash condition exists.
        Subject requirement: detect non-zero exit, restart loop, or a dead container.
        """
        now = time.time()
        client = self._get_client()

        if client is None:
            state = ContainerState(
                name=self.container_name,
                status="daemon_unavailable",
                error_reason="Cannot connect to the Docker daemon (check DOCKER_HOST)",
                timestamp=now
            )
            self._last_state = state
            return state

        try:
            container = client.containers.get(self.container_name)
            container.reload()
            attrs = container.attrs or {}
            c_state = attrs.get("State", {})

            status = str(c_state.get("Status", "unknown")).lower()
            exit_code = c_state.get("ExitCode", 0)
            restart_count = int(attrs.get("RestartCount", 0) or 0)

            is_crash = False
            error_reason = None

            # Crash condition 1: container exited with a non-zero exit code
            if status == "exited" and exit_code != 0:
                is_crash = True
                error_reason = f"Container terminated with non-zero exit code {exit_code}"

            # Crash condition 2: container is dead or was OOM-killed
            elif c_state.get("Dead", False) or c_state.get("OOMKilled", False):
                is_crash = True
                oom = " (OOMKilled)" if c_state.get("OOMKilled") else ""
                error_reason = f"Container is dead{oom} with exit code {exit_code}"

            # Crash condition 3: a genuine restart loop. The transient 'restarting'
            # state alone is not a crash - every `docker restart`, including the one
            # the Wisdom Loop issues itself, passes through it. Two signals qualify:
            #   a) the restart counter climbing inside the window, and
            #   b) the container *stuck* in 'restarting', which is what a crash loop
            #      looks like once Docker's exponential backoff stretches out.
            if status == "restarting":
                if self._restarting_since is None:
                    self._restarting_since = now
            else:
                self._restarting_since = None

            climbing = self._is_restart_loop(now, restart_count) and status in (
                "restarting", "running"
            )
            stuck = (
                self._restarting_since is not None
                and (now - self._restarting_since) >= RESTART_STUCK_S
            )

            if climbing or stuck:
                is_crash = True
                # The reason text is deliberately constant: the live counters are
                # carried in restart_count / status, and baking them into the message
                # would give the same ongoing loop a new dedup signature every poll.
                error_reason = "Container entered a restart loop"

            state = ContainerState(
                name=self.container_name,
                status=status,
                exit_code=exit_code if status == "exited" else None,
                restart_count=restart_count,
                is_crash=is_crash,
                error_reason=error_reason,
                timestamp=now
            )
            self._last_state = state
            return state

        except NotFound:
            self._restart_samples.clear()
            self._restarting_since = None
            state = ContainerState(
                name=self.container_name,
                status="not_found",
                error_reason=f"Container '{self.container_name}' not found",
                timestamp=now
            )
            self._last_state = state
            return state

        except Exception as e:
            logger.error(f"Error inspecting container {self.container_name}: {e}")
            state = ContainerState(
                name=self.container_name,
                status="error",
                error_reason=str(e),
                timestamp=now
            )
            self._last_state = state
            return state

    def is_running(self) -> bool:
        """True when the target is up right now."""
        return self.inspect().status == "running"

    def get_log_stream(
        self,
        tail: Optional[int] = None,
        since: Optional[int] = None,
    ) -> Optional[Iterator[bytes]]:
        """Follow the target's stdout/stderr with RFC3339 timestamps.

        Returns None when the daemon or container is unavailable, so the caller can
        retry instead of crashing.
        """
        client = self._get_client()
        if client is None:
            return None
        try:
            container = client.containers.get(self.container_name)
            container.reload()
            if container.status not in ("running", "restarting"):
                return None
            kwargs: Dict[str, Any] = {
                "stream": True,
                "follow": True,
                "stdout": True,
                "stderr": True,
                "timestamps": True,
            }
            if since is not None:
                kwargs["since"] = since
            if tail is not None:
                kwargs["tail"] = tail
            return container.logs(**kwargs)
        except Exception as e:
            logger.debug(f"Could not open log stream for {self.container_name}: {e}")
            return None

    def restart_target(self, timeout: int = 10) -> bool:
        """Restarts the target container. Used by the Wisdom Loop after patching."""
        client = self._get_client()
        if client is None:
            logger.error("Cannot restart target: Docker daemon unavailable")
            return False

        try:
            container = client.containers.get(self.container_name)
            logger.info(f"Restarting target container '{self.container_name}' (timeout={timeout}s)...")
            container.restart(timeout=timeout)
            # A deliberate restart is not evidence of a loop.
            self._restart_samples.clear()
            self._restarting_since = None
            logger.info(f"Container '{self.container_name}' restart command sent successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to restart container '{self.container_name}': {e}")
            return False

    def get_last_state(self) -> Optional[ContainerState]:
        """Returns the most recently observed container state."""
        return self._last_state
