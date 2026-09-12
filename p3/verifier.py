"""
Inception-of-Wisdom (IoW) — Part 3: Wisdom Loop
Verifier: Container Restart & Dynamic Grace Period Health Verification
"""

from __future__ import annotations

import time
import logging
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass, asdict

from p1.docker_monitor import DockerMonitor
from p1.http_probe import HttpProbeManager
from p1.event_manager import EventManager, ObserverEvent
from p1.log_streamer import LogStreamer

logger = logging.getLogger("p3.verifier")


@dataclass
class VerificationResult:
    """Outcome of a post-patch container restart and grace period observation."""
    is_healed: bool
    status: str                    # 'healed', 'crashed', 'http_failure', 'restart_failed'
    grace_period_seconds: float
    elapsed_seconds: float
    error_log: Optional[str] = None
    container_status: str = "unknown"
    details: Dict[str, Any] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class TargetVerifier:
    """Restarts the target container and observes its health over a configurable grace period.
    Subject requirement: The target is considered healed if it stays up for at least
    the configurable grace_period and no new crash event fires.
    """

    def __init__(
        self,
        docker_monitor: DockerMonitor,
        http_probe: Optional[HttpProbeManager] = None,
        event_manager: Optional[EventManager] = None,
        log_streamer: Optional[LogStreamer] = None
    ):
        self.docker_monitor = docker_monitor
        self.http_probe = http_probe
        self.event_manager = event_manager
        self.log_streamer = log_streamer

    def restart_and_verify(
        self,
        grace_period: float = 20.0,
        progress_callback: Optional[Callable[[float, float, str], None]] = None
    ) -> VerificationResult:
        """Restarts the target container and continuously monitors health until
        the grace_period expires or an immediate crash occurs.
        """
        now = time.time()
        logger.info(f"Initiating target restart and verification (grace_period={grace_period}s)...")

        # Step 1: Clear old log buffers and event traces for clean observation
        if self.log_streamer:
            self.log_streamer.clear()

        # Step 2: Restart the container via Docker socket
        restart_ok = self.docker_monitor.restart_target(timeout=10)
        if not restart_ok:
            logger.error("Failed to execute docker restart on target container.")
            return VerificationResult(
                is_healed=False,
                status="restart_failed",
                grace_period_seconds=grace_period,
                elapsed_seconds=0.0,
                error_log="Docker restart command failed",
                container_status="error",
                timestamp=now
            )

        # Allow initial container bootstrap time (1.5s)
        time.sleep(1.5)

        # Step 3: Track new crash events specifically during this verification window
        new_crash_events: list[ObserverEvent] = []

        def _verification_crash_listener(event: ObserverEvent):
            if event.type == "crash":
                new_crash_events.append(event)

        if self.event_manager:
            self.event_manager.add_listener(_verification_crash_listener)

        start_time = time.time()
        poll_interval = 0.5  # Check twice per second for rapid response

        try:
            while True:
                elapsed = time.time() - start_time
                remaining = max(0.0, grace_period - elapsed)

                if progress_callback:
                    try:
                        progress_callback(min(elapsed, grace_period), grace_period, "verifying")
                    except Exception:
                        pass

                # Check 1: Did an event-based crash fire during verification?
                if new_crash_events:
                    latest_crash = new_crash_events[-1]
                    logger.warning(
                        f"Verification FAILED at {elapsed:.1f}s: Crash event fired: {latest_crash.summary}"
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
                        details={"event": latest_crash.to_dict()},
                        timestamp=time.time()
                    )

                # Check 2: Inspect Docker container state
                c_state = self.docker_monitor.inspect()
                if c_state.is_crash:
                    logger.warning(f"Verification FAILED at {elapsed:.1f}s: Docker container crashed: {c_state.error_reason}")
                    error_log = c_state.error_reason
                    if self.log_streamer:
                        error_log += f"\n{self.log_streamer.get_recent_logs_as_text(tail=15)}"
                    return VerificationResult(
                        is_healed=False,
                        status="crashed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(elapsed, 2),
                        error_log=error_log,
                        container_status=c_state.status,
                        details={"container_state": c_state.to_dict()},
                        timestamp=time.time()
                    )

                # Check 3: Check HTTP probes if available
                if self.http_probe:
                    probe_results = self.http_probe.probe_all_once()
                    for pr in probe_results:
                        if pr.is_crash:
                            logger.warning(
                                f"Verification FAILED at {elapsed:.1f}s: HTTP probe failed on {pr.url} ({pr.error_message})"
                            )
                            return VerificationResult(
                                is_healed=False,
                                status="http_failure",
                                grace_period_seconds=grace_period,
                                elapsed_seconds=round(elapsed, 2),
                                error_log=pr.error_message,
                                container_status=c_state.status,
                                details={"probe_result": pr.to_dict()},
                                timestamp=time.time()
                            )

                # Grace period expired with no crash events!
                if elapsed >= grace_period:
                    logger.info(
                        f"Verification SUCCESS: Target container stayed healthy for full {grace_period}s grace period!"
                    )
                    if progress_callback:
                        progress_callback(grace_period, grace_period, "healed")
                    return VerificationResult(
                        is_healed=True,
                        status="healed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(elapsed, 2),
                        container_status="running",
                        timestamp=time.time()
                    )

                time.sleep(poll_interval)

        finally:
            if self.event_manager:
                self.event_manager.remove_listener(_verification_crash_listener)

