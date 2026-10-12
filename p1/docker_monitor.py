"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Docker Monitor: Container State Inspection & Lifecycle Management
"""

from __future__ import annotations

import re
import time
import logging
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, Iterable, Iterator, Optional, Tuple
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
# `docker stop` / `docker restart` announce themselves with a 'kill' event before the
# container dies. That kill explains exactly one following 'die' (if it comes within
# this window); any later exit - e.g. the restarted code crashing - is a real crash.
DELIBERATE_STOP_WINDOW_S = 30.0


_NOISE_ENV_RE = re.compile(r"^(PATH|HOME|HOSTNAME|TERM|LANG|LC_\w+|PYTHON_\w+|PIP_\w+|GPG_\w+)$")
_SECRET_ENV_RE = re.compile(r"KEY|TOKEN|SECRET|PASS|CRED", re.IGNORECASE)


def target_facts_from(ports: Iterable[Any], env: Iterable[Tuple[str, str]]) -> Dict[str, Any]:
    """Ports and environment of the target worth telling the model - never a secret.

    Base-image bookkeeping (PATH, PYTHON_*, GPG_*) is left out; anything that looks like
    a credential is never shown, even to the local model.
    """
    port_list = sorted({int(p) for p in ports if str(p).isdigit()})
    shown = {
        name: value[:60] for name, value in env
        if name and not _NOISE_ENV_RE.match(name) and not _SECRET_ENV_RE.search(name)
    }
    return {"ports": port_list, "env": shown}


@dataclass
class ContainerState:
    """Represents the current snapshot of a monitored container."""
    name: str
    # running | restarting | exited | not_found | daemon_unavailable, and in gitops mode
    # not_started (the pod never ran the app)
    status: str
    exit_code: Optional[int] = None
    restart_count: int = 0
    is_crash: bool = False
    error_reason: Optional[str] = None
    timestamp: float = 0.0
    # Down, but not crashed: someone stopped or paused it. No code change can fix that.
    stopped: bool = False

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
        # Start of the current daemon outage (None while connected).
        self._outage_since: Optional[float] = None
        self._last_state: Optional[ContainerState] = None
        # (timestamp, restart_count) samples used to detect a real restart loop.
        self._restart_samples: Deque[Tuple[float, int]] = deque(maxlen=64)
        # When the container first entered 'restarting' and stayed there.
        self._restarting_since: Optional[float] = None
        # Event stream bookkeeping (see watch_events).
        self._events_stream: Optional[Any] = None
        # Set by a 'kill' event (or by our own restart); consumed by the next 'die'.
        self._pending_deliberate_ts: float = 0.0
        self._last_event_nano: int = 0
        # When the container's current process was started (the daemon's 'start' event).
        self._last_start_ts: float = 0.0
        # (exit time, exit code, start time of that process) of the most recent exit
        # nobody asked for - both times as the daemon stamped them.
        self._last_crash_exit: Optional[Tuple[float, int, float]] = None

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
                if self._outage_since is not None:
                    logger.info(
                        "Reconnected to Docker daemon after %.0fs outage",
                        time.time() - self._outage_since,
                    )
                else:
                    logger.info("Connected to Docker daemon")
                self._outage_since = None
                return self._client
            except Exception as e:
                # Every poller asks several times a second during an outage: say it
                # once, then keep quiet until the daemon is back.
                if self._outage_since is None:
                    self._outage_since = time.time()
                    logger.warning(
                        "Unable to connect to Docker daemon (%s). "
                        "Is the process allowed to access the socket "
                        "(DOCKER_HOST / 'docker' group), or is the daemon running? "
                        "Retrying quietly until it is back.",
                        e,
                    )
                else:
                    logger.debug(f"Docker daemon still unreachable: {e}")
                self._client = None
                return None

    def forget_restart_history(self) -> None:
        """The target serves again: the restarts counted so far belong to an outage that is over.

        The restart-loop check reads the restart counter over the last minute, and only the
        agent's own restart used to clear it. A crash loop ended by any other restart
        (`make demo-reset`, a hand fix and `docker restart`) kept "climbing" in that window:
        a restart loop on a target that served again, raised anew on every poll once the
        Observer had closed the outage (seen live: nine events in 24 s, and a heal started
        on the healthy target).
        """
        self._restart_samples.clear()
        self._restarting_since = None

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

            # Stopped on purpose. Under a policy that restarts crashed containers (always /
            # unless-stopped) Docker marks a crash 'restarting' before it even reports the
            # exit, and leaves only a stopped container exited - `docker stop` (exit 0) and
            # `docker kill` (exit 137) alike (measured). The outage is real, but no code
            # change can end it: healed anyway, the model rewrote working routes (seen live).
            policy = str(((attrs.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "")
            stopped = status == "paused" or (
                status == "exited" and (exit_code == 0 or policy in ("always", "unless-stopped"))
            )
            if status == "paused":
                error_reason = (
                    f"Paused (docker pause) - not a crash; `docker unpause {self.container_name}` "
                    "resumes it"
                )
            elif stopped:
                error_reason = (
                    f"Stopped (exit code {exit_code}) and not restarted by its restart policy: stopped "
                    f"on purpose, not crashed; `docker start {self.container_name}` brings it back"
                )

            # Crash condition 1: container exited with a non-zero exit code
            elif status == "exited" and exit_code != 0:
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
                timestamp=now,
                stopped=stopped,
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

    def target_facts(self) -> Dict[str, Any]:
        """Exposed ports and (non-secret) environment of the target container.

        A fix that falls back to a default port has to pick the one the target is
        probed on: seen live, the model chose 8080 for a service exposed on 5000.
        """
        client = self._get_client()
        if client is None:
            return {}
        try:
            config = (client.containers.get(self.container_name).attrs or {}).get("Config") or {}
        except Exception as e:
            logger.debug(f"Could not read the configuration of {self.container_name}: {e}")
            return {}
        ports = [key.split("/")[0] for key in (config.get("ExposedPorts") or {})]
        env = [tuple(item.split("=", 1)) for item in config.get("Env") or [] if "=" in item]
        return target_facts_from(ports, env)  # type: ignore[arg-type]

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
            # An exited (or dead) container's stream is finite - the daemon sends what it
            # logged and ends it - but that output is the crash itself: without a restart
            # policy the target stayed down and its traceback never reached the Observer (seen
            # live: no crash in the log view, no log signal). Paused or never-started
            # containers have nothing new to say.
            if container.status not in ("running", "restarting", "exited", "dead"):
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

    def fetch_logs(self, since: Optional[float] = None, tail: int = 60) -> str:
        """The target's recent output straight from the daemon (no follow).

        The Verifier needs *the new* error log right after a redeploy failed, and the
        followed stream may still be re-attaching at that moment.
        """
        client = self._get_client()
        if client is None:
            return ""
        try:
            container = client.containers.get(self.container_name)
            kwargs: Dict[str, Any] = {"stdout": True, "stderr": True, "tail": tail}
            if since is not None:
                kwargs["since"] = int(since)
            raw = container.logs(**kwargs)
            return raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        except Exception as e:
            logger.debug(f"Could not fetch logs of {self.container_name}: {e}")
            return ""

    # ------------------------------------------------------------------
    # Docker event stream: the exit-code signal
    # ------------------------------------------------------------------
    def watch_events(
        self,
        on_state: Callable[[ContainerState], Any],
        stop_event: threading.Event,
    ) -> None:
        """Follow the daemon's event stream and report exits nobody asked for.

        Polling cannot see a non-zero exit when the restart policy brings the
        container straight back: the 'exited' state lasts a few milliseconds and
        State.ExitCode is reset on the next start. The 'die' event carries the exit
        code, so this is the only reliable source for that crash signal.

        Blocks until stop_event is set; survives daemon disconnects with backoff.
        """
        backoff = 1.0
        while not stop_event.is_set():
            client = self._get_client()
            if client is None:
                stop_event.wait(backoff)
                backoff = min(backoff * 2.0, 15.0)
                continue
            try:
                # Resume at the last event we handled, so a reconnect neither loses a
                # crash nor replays one (the timeNano check drops the repeat); start
                # from 'now' on the first connection.
                ns = self._last_event_nano
                since = (
                    f"{ns // 1_000_000_000}.{ns % 1_000_000_000:09d}" if ns
                    else str(int(time.time()))
                )
                stream = client.events(
                    decode=True,
                    since=since,
                    filters={"type": "container", "container": self.container_name},
                )
                self._events_stream = stream
                backoff = 1.0
                for event in stream:
                    if stop_event.is_set():
                        break
                    state = self._interpret_event(event)
                    if state is None:
                        continue
                    try:
                        on_state(state)
                    except Exception as err:
                        logger.error(f"Error handling Docker event: {err}")
            except Exception as e:
                if stop_event.is_set():
                    break
                logger.warning("Docker event stream interrupted (%s) - reconnecting.", e)
                stop_event.wait(backoff)
                backoff = min(backoff * 2.0, 15.0)
            finally:
                self.close_event_stream()

    def close_event_stream(self) -> None:
        """Unblock watch_events (used on shutdown)."""
        stream, self._events_stream = self._events_stream, None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

    def _interpret_event(self, event: Dict[str, Any]) -> Optional[ContainerState]:
        """Map one Docker event onto a crash state, or None when it is not one."""
        time_nano = int(event.get("timeNano") or 0)
        if time_nano and time_nano <= self._last_event_nano:
            return None
        if time_nano:
            self._last_event_nano = time_nano

        action = str(event.get("Action") or event.get("status") or "")
        attrs = (event.get("Actor") or {}).get("Attributes") or {}
        now = time.time()
        # When it happened, not when this thread got to it.
        event_ts = time_nano / 1e9 if time_nano else now

        if action == "kill":
            self._pending_deliberate_ts = now
            return None
        if action == "start":
            self._last_start_ts = event_ts
            return None

        restart_count = self._last_state.restart_count if self._last_state else 0
        if action == "oom":
            return ContainerState(
                name=self.container_name,
                status="dead",
                restart_count=restart_count,
                is_crash=True,
                error_reason="Container was OOM-killed",
                timestamp=now,
            )
        if action != "die":
            return None

        # A stop/restart explains the one exit that follows it - never more, so the
        # restarted code crashing a second later is still reported.
        deliberate = (
            self._pending_deliberate_ts > 0.0
            and (now - self._pending_deliberate_ts) < DELIBERATE_STOP_WINDOW_S
        )
        self._pending_deliberate_ts = 0.0

        try:
            exit_code = int(attrs.get("exitCode", 0))
        except (TypeError, ValueError):
            exit_code = 0
        if exit_code == 0:
            return None
        if deliberate:
            logger.debug(
                "Ignoring exit %s of '%s': it follows a deliberate stop/restart.",
                exit_code, self.container_name,
            )
            return None

        self._last_crash_exit = (event_ts, exit_code, self._last_start_ts)
        return ContainerState(
            name=self.container_name,
            status="exited",
            exit_code=exit_code,
            restart_count=restart_count,
            is_crash=True,
            # The exit code itself travels in exit_code; the reason stays constant so
            # every exit of one crash loop shares a single dedup signature.
            error_reason="Container exited with a non-zero status",
            timestamp=now,
        )

    def mark_deliberate(self) -> None:
        """Declare that the next exit is ours (a restart we are about to issue).

        Normally the daemon's own 'kill' event says so; this covers the case where
        the event stream was reconnecting at that moment.
        """
        self._pending_deliberate_ts = time.time()

    def last_start_time(self) -> float:
        """When the container's current process was started (daemon time, 0 if unseen)."""
        return self._last_start_ts

    def last_crash_exit_since(
        self, since: float, started_after: Optional[float] = None
    ) -> Optional[int]:
        """Exit code of an unrequested non-zero exit after `since`, if any.

        started_after: only count a process started at or after that moment. While a
        heal is prepared the restart policy keeps starting the crash-looping container;
        a process that read the old code just before the patch landed can die just after
        the redeploy began (seen live: a correct fix failed as "exited with code 1 right
        after the restart" on the old code's traceback, and was rolled back).
        """
        if not self._last_crash_exit:
            return None
        exited_at, code, started_at = self._last_crash_exit
        if exited_at < since:
            return None
        if started_after is not None and started_at < started_after:
            return None
        return code

    def restart_target(self, timeout: int = 10) -> bool:
        """Restarts the target container. Used by the Wisdom Loop after patching."""
        client = self._get_client()
        if client is None:
            logger.error("Cannot restart target: Docker daemon unavailable")
            return False

        try:
            container = client.containers.get(self.container_name)
            logger.info(f"Restarting target container '{self.container_name}' (timeout={timeout}s)...")
            # The stop half of a restart is ours: its exit must not read as a crash.
            # Only a running container has a process to stop - marking a crash-looping
            # one would make the *next* genuine crash look deliberate.
            if container.status == "running":
                self.mark_deliberate()
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
