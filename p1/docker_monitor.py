"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Docker Monitor: Container State Inspection & Lifecycle Management
"""

from __future__ import annotations

import time
import logging
import threading
from typing import Optional, Dict, Any
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
    """Observes the target container lifecycle via /var/run/docker.sock.
    Resilient to daemon disconnects and container recreation.
    """

    def __init__(self, container_name: str = "iow_demo_target", base_url: Optional[str] = None):
        self.container_name = container_name
        self.base_url = base_url or "unix:///var/run/docker.sock"
        self._client: Optional[docker.DockerClient] = None
        self._client_lock = threading.Lock()
        self._last_state: Optional[ContainerState] = None

    def _get_client(self) -> Optional[docker.DockerClient]:
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
                # Prefer env (DOCKER_HOST) then unix socket - campus users often lack
                # docker-group access to /var/run/docker.sock from the Python client.
                try:
                    self._client = docker.from_env()
                    self._client.ping()
                except Exception:
                    self._client = docker.DockerClient(base_url=self.base_url)
                    self._client.ping()
                logger.info(f"Connected to Docker daemon at {self.base_url}")
                return self._client
            except Exception as e:
                logger.warning(
                    "Unable to connect to Docker daemon (%s). "
                    "Is the process allowed to access the socket "
                    "(user in 'docker' group), or is the daemon running?",
                    e,
                )
                self._client = None
                return None

    def inspect(self) -> ContainerState:
        """Inspects the target container and determines if a crash condition exists.
        Subject requirement: Detect non-zero exit, restart loop, or stopped container.
        """
        now = time.time()
        client = self._get_client()

        if client is None:
            state = ContainerState(
                name=self.container_name,
                status="daemon_unavailable",
                error_reason="Cannot connect to Docker daemon via /var/run/docker.sock",
                timestamp=now
            )
            self._last_state = state
            return state

        try:
            container = client.containers.get(self.container_name)
            # Reload to get the freshest attributes from Docker
            container.reload()
            attrs = container.attrs or {}
            c_state = attrs.get("State", {})

            status = c_state.get("Status", "unknown").lower()
            exit_code = c_state.get("ExitCode", 0)
            restart_count = attrs.get("RestartCount", 0)

            is_crash = False
            error_reason = None

            # Crash condition 1: Container exited with non-zero exit code
            if status == "exited" and exit_code != 0:
                is_crash = True
                error_reason = f"Container terminated with non-zero exit code {exit_code}"

            # Crash condition 2: Container entered an active restart loop
            elif status == "restarting":
                is_crash = True
                error_reason = f"Container entered restart loop (restart count: {restart_count})"

            # Crash condition 3: Container has crashed and dead
            elif c_state.get("Dead", False) or c_state.get("OOMKilled", False):
                is_crash = True
                oom = " (OOMKilled)" if c_state.get("OOMKilled") else ""
                error_reason = f"Container is dead{oom} with exit code {exit_code}"

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

    def restart_target(self, timeout: int = 10) -> bool:
        """Restarts the target container. Used by Wisdom Loop after patching.
        Returns True if successful, False otherwise.
        """
        client = self._get_client()
        if client is None:
            logger.error("Cannot restart target: Docker daemon unavailable")
            return False

        try:
            container = client.containers.get(self.container_name)
            logger.info(f"Restarting target container '{self.container_name}' (timeout={timeout}s)...")
            container.restart(timeout=timeout)
            logger.info(f"Container '{self.container_name}' restart command sent successfully.")
            return True
        except Exception as e:
            logger.error(f"Failed to restart container '{self.container_name}': {e}")
            return False

    def get_last_state(self) -> Optional[ContainerState]:
        """Returns the most recently observed container state."""
        return self._last_state
