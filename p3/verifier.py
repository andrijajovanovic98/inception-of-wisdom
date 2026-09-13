"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Verifier: Container Restart & Dynamic Grace Period Health Verification

The restart window is handled explicitly rather than with a fixed sleep: the target
is *expected* to be unreachable while it comes back, so Observer events are suppressed
until it answers once, and only then does the grace period start. A single transient
probe miss afterwards is not a failure either - it takes consecutive misses, or a
container that is genuinely not running, to fail an attempt.
"""

from __future__ import annotations

import time
import logging
from typing import Optional, Dict, Any, Callable, List
from dataclasses import dataclass, asdict, field

from p1.docker_monitor import DockerMonitor
from p1.http_probe import HttpProbeManager
from p1.event_manager import EventManager, ObserverEvent
from p1.log_streamer import LogStreamer

logger = logging.getLogger("p3.verifier")

# How long the target may take to come back up before we call the restart failed.
DEFAULT_BOOT_TIMEOUT_S = 45.0
# Consecutive probe failures required to fail verification once the target booted.
PROBE_FAILURE_THRESHOLD = 3


@dataclass
class VerificationResult:
    """Outcome of a post-patch container restart and grace period observation."""
    is_healed: bool
    status: str                    # 'healed', 'crashed', 'http_failure', 'restart_failed'
    grace_period_seconds: float
    elapsed_seconds: float
    error_log: Optional[str] = None
    container_status: str = "unknown"
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class TargetVerifier:
    """Restarts the target and observes its health over a configurable grace period.
    Subject requirement: The target is considered healed if it stays up for at least
    the configurable grace_period and no new crash event fires.
    """

    def __init__(
        self,
        docker_monitor: DockerMonitor,
        http_probe: Optional[HttpProbeManager] = None,
        event_manager: Optional[EventManager] = None,
        log_streamer: Optional[LogStreamer] = None,
        boot_timeout_seconds: float = DEFAULT_BOOT_TIMEOUT_S,
        git_manager: Any = None,
    ):
        self.docker_monitor = docker_monitor
        self.http_probe = http_probe
        self.event_manager = event_manager
        self.log_streamer = log_streamer
        self.boot_timeout_seconds = boot_timeout_seconds
        # Needed in gitops mode: the heal commit has to be pushed to the repository
        # Argo CD watches, otherwise the cluster can never see the fix.
        self.git_manager = git_manager

    # ------------------------------------------------------------------
    def restart_and_verify(
        self,
        grace_period: float = 20.0,
        progress_callback: Optional[Callable[[float, float, str], None]] = None
    ) -> VerificationResult:
        """Redeploys the target and monitors health until the grace period expires."""
        now = time.time()
        mode = "docker"
        wait_for_gitops_redeploy = None
        try:
            from bonus.gitops import redeploy_mode, wait_for_gitops_redeploy as _wait
            mode = redeploy_mode()
            wait_for_gitops_redeploy = _wait
        except Exception as exc:
            logger.debug(f"GitOps helpers unavailable, using docker restart: {exc}")

        logger.info(
            f"Initiating target redeploy/verify (mode={mode}, grace_period={grace_period}s)..."
        )

        # The target is deliberately going away: do not let the restart itself look
        # like the crash that failed this attempt.
        suppression = self.boot_timeout_seconds + 5.0
        if self.event_manager:
            self.event_manager.suppress_events_for(suppression)
        if self.log_streamer:
            self.log_streamer.clear()

        try:
            if mode == "gitops" and wait_for_gitops_redeploy is not None:
                ok, detail = wait_for_gitops_redeploy(
                    grace_period=grace_period, git_manager=self.git_manager
                )
                if not ok:
                    logger.error(f"GitOps redeploy failed: {detail}")
                    return VerificationResult(
                        is_healed=False,
                        status="restart_failed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=0.0,
                        error_log=f"GitOps redeploy failed: {detail}",
                        container_status="error",
                        details={"redeploy_mode": mode},
                        timestamp=now,
                    )
                logger.info(f"GitOps redeploy OK: {detail}")
            else:
                if not self.docker_monitor.restart_target(timeout=10):
                    logger.error("Failed to execute docker restart on target container.")
                    return VerificationResult(
                        is_healed=False,
                        status="restart_failed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=0.0,
                        error_log="Docker restart command failed",
                        container_status="error",
                        details={"redeploy_mode": mode},
                        timestamp=now
                    )

            booted, boot_detail = self._wait_for_boot(progress_callback)
            if not booted:
                return VerificationResult(
                    is_healed=False,
                    status="restart_failed",
                    grace_period_seconds=grace_period,
                    elapsed_seconds=0.0,
                    error_log=boot_detail,
                    container_status=self.docker_monitor.inspect().status,
                    details={"redeploy_mode": mode, "phase": "boot"},
                    timestamp=time.time(),
                )
            logger.info(f"Target answered after redeploy ({boot_detail}); starting grace period.")
        finally:
            # From here on, real crashes must be visible again.
            if self.event_manager:
                self.event_manager.resume_events()

        return self._observe_grace_period(grace_period, mode, progress_callback)

    # ------------------------------------------------------------------
    def _wait_for_boot(
        self,
        progress_callback: Optional[Callable[[float, float, str], None]],
    ) -> tuple[bool, str]:
        """Block until the target answers once, or the boot timeout expires."""
        deadline = time.time() + self.boot_timeout_seconds
        last_detail = "target did not answer after redeploy"

        while time.time() < deadline:
            elapsed = self.boot_timeout_seconds - (deadline - time.time())
            if progress_callback:
                try:
                    progress_callback(elapsed, self.boot_timeout_seconds, "starting")
                except Exception:
                    pass

            state = self.docker_monitor.inspect()
            if state.status == "exited":
                exit_code = state.exit_code
                if exit_code not in (None, 0):
                    return False, (
                        f"Target exited with code {exit_code} instead of starting.\n"
                        f"{self._tail_logs()}"
                    )
                last_detail = "target exited cleanly instead of serving"
            elif state.status in ("not_found", "daemon_unavailable", "error"):
                last_detail = state.error_reason or state.status

            if self.http_probe is not None:
                if any(r.is_healthy for r in self.http_probe.probe_all_once()):
                    return True, f"healthy after {elapsed:.1f}s"
                last_detail = "target is not answering HTTP probes"
            elif state.status == "running":
                # No probes configured: a running container is the only signal.
                return True, f"running after {elapsed:.1f}s"

            time.sleep(0.5)

        return False, f"{last_detail} within {self.boot_timeout_seconds:.0f}s\n{self._tail_logs()}"

    # ------------------------------------------------------------------
    def _observe_grace_period(
        self,
        grace_period: float,
        mode: str,
        progress_callback: Optional[Callable[[float, float, str], None]],
    ) -> VerificationResult:
        """Watch a booted target for the full grace period."""
        new_crash_events: List[ObserverEvent] = []

        def _verification_crash_listener(event: ObserverEvent) -> None:
            if event.type == "crash":
                new_crash_events.append(event)

        if self.event_manager:
            self.event_manager.add_listener(_verification_crash_listener)

        start_time = time.time()
        poll_interval = 0.5
        consecutive_probe_failures = 0
        last_probe_error: Optional[str] = None
        container_status = "unknown"

        try:
            while True:
                elapsed = time.time() - start_time

                if progress_callback:
                    try:
                        progress_callback(min(elapsed, grace_period), grace_period, "verifying")
                    except Exception:
                        pass

                # Check 1: did a real crash event fire during the grace period?
                if new_crash_events:
                    latest_crash = new_crash_events[-1]
                    logger.warning(
                        f"Verification FAILED at {elapsed:.1f}s: crash event fired: {latest_crash.summary}"
                    )
                    error_log = (
                        latest_crash.details.get("context_excerpt")
                        or latest_crash.details.get("matched_line")
                        or latest_crash.summary
                    )
                    return VerificationResult(
                        is_healed=False,
                        status="crashed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(elapsed, 2),
                        error_log=error_log,
                        container_status="crashed",
                        details={"event": latest_crash.to_dict(), "redeploy_mode": mode},
                        timestamp=time.time()
                    )

                # Check 2: container state
                c_state = self.docker_monitor.inspect()
                container_status = c_state.status
                if c_state.is_crash or c_state.status in ("exited", "dead"):
                    reason = c_state.error_reason or f"container status is '{c_state.status}'"
                    logger.warning(f"Verification FAILED at {elapsed:.1f}s: {reason}")
                    return VerificationResult(
                        is_healed=False,
                        status="crashed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(elapsed, 2),
                        error_log=f"{reason}\n{self._tail_logs()}",
                        container_status=c_state.status,
                        details={"container_state": c_state.to_dict(), "redeploy_mode": mode},
                        timestamp=time.time()
                    )

                # Check 3: HTTP probes. One dropped request is noise; several in a
                # row means the service really stopped answering.
                if self.http_probe:
                    failed = [pr for pr in self.http_probe.probe_all_once() if pr.is_crash]
                    if failed:
                        consecutive_probe_failures += 1
                        last_probe_error = failed[-1].error_message
                        if consecutive_probe_failures >= PROBE_FAILURE_THRESHOLD:
                            logger.warning(
                                f"Verification FAILED at {elapsed:.1f}s: "
                                f"{consecutive_probe_failures} consecutive probe failures "
                                f"({last_probe_error})"
                            )
                            return VerificationResult(
                                is_healed=False,
                                status="http_failure",
                                grace_period_seconds=grace_period,
                                elapsed_seconds=round(elapsed, 2),
                                error_log=f"{last_probe_error}\n{self._tail_logs()}",
                                container_status=c_state.status,
                                details={
                                    "probe_result": failed[-1].to_dict(),
                                    "consecutive_failures": consecutive_probe_failures,
                                    "redeploy_mode": mode,
                                },
                                timestamp=time.time()
                            )
                    else:
                        consecutive_probe_failures = 0

                # Grace period expired with the target still up.
                if elapsed >= grace_period:
                    if container_status not in ("running", "restarting"):
                        # Subject: healed means it *stayed up*. A container that
                        # exited cleanly is not healed.
                        logger.warning(
                            f"Verification FAILED: grace period elapsed but container "
                            f"status is '{container_status}', not running."
                        )
                        return VerificationResult(
                            is_healed=False,
                            status="crashed",
                            grace_period_seconds=grace_period,
                            elapsed_seconds=round(elapsed, 2),
                            error_log=(
                                f"Container is '{container_status}' after the grace "
                                f"period; the target did not stay up.\n{self._tail_logs()}"
                            ),
                            container_status=container_status,
                            details={"redeploy_mode": mode},
                            timestamp=time.time()
                        )

                    if grace_period > 0:
                        logger.info(
                            f"Verification SUCCESS: target stayed healthy for the full "
                            f"{grace_period}s grace period."
                        )
                    else:
                        logger.info("Target is back up and answering on the original code.")
                    if progress_callback:
                        progress_callback(grace_period, grace_period, "healed")
                    return VerificationResult(
                        is_healed=True,
                        status="healed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(elapsed, 2),
                        container_status=container_status,
                        details={"redeploy_mode": mode},
                        timestamp=time.time()
                    )

                time.sleep(poll_interval)

        finally:
            if self.event_manager:
                self.event_manager.remove_listener(_verification_crash_listener)

    # ------------------------------------------------------------------
    def _tail_logs(self, tail: int = 20) -> str:
        if not self.log_streamer:
            return ""
        return self.log_streamer.get_recent_logs_as_text(tail=tail)
