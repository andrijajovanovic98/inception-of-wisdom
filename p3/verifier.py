"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Verifier: Container Restart & Dynamic Grace Period Health Verification

The restart window is handled explicitly rather than with a fixed sleep: the target
is *expected* to be unreachable while it comes back, so Observer events are suppressed
until it answers once, and only then does the grace period start. From then on the
subject's rule applies as written: any new crash event fails the attempt - a failed
probe raises one, so one refused or 5xx probe is enough. The request that crashed the
target is re-issued first, so a soft crash on a route no probe covers is checked too.
"""

from __future__ import annotations

import re
import time
import logging
from typing import Optional, Dict, Any, Callable, List, Sequence, Tuple
from dataclasses import dataclass, asdict, field

from p1.docker_monitor import DockerMonitor
from p1.http_probe import HttpProbeManager
from p1.event_manager import EventManager, ObserverEvent
from p1.log_streamer import LogStreamer, latest_error_block, latest_error_span

logger = logging.getLogger("p3.verifier")

# How long the target may take to come back up before we call the restart failed.
DEFAULT_BOOT_TIMEOUT_S = 45.0
# Consecutive probe failures required to fail verification once the target booted.
PROBE_FAILURE_THRESHOLD = 3
# Requests re-issued after a redeploy, at most.
MAX_REPLAYS = 2

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
# werkzeug / uvicorn access lines: "POST /api/crash HTTP/1.1" 500
_ACCESS_RE = re.compile(r'"(GET|POST|PUT|PATCH|DELETE|HEAD) (/[^\s"]*) HTTP/[0-9.]+" (\d{3})')
# Flask names the request whose handler raised, right above the traceback it logs:
# "ERROR in app: Exception on /api/crash [POST]"
_FLASK_EXC_RE = re.compile(r"Exception on (/[^\s\[]*) \[(GET|POST|PUT|PATCH|DELETE|HEAD)\]")
# How far around an error report the lines naming its request are looked for.
_REQUEST_LINES_BEFORE = 2
_REQUEST_LINES_AFTER = 10


def failing_requests_from_log(
    log_text: str, ignore_paths: Sequence[str] = ()
) -> List[Tuple[str, str]]:
    """The request(s) that crashed the target, read off its own log.

    A request answered 5xx - or named by Flask as the one whose handler raised - is
    the symptom itself. Without one, the last request logged before a hard exit is
    the prime suspect (the process died serving it). The Observer's own probe paths
    are never "the failing request".
    """
    ignored = set(ignore_paths)
    requests: List[Tuple[str, str]] = []
    last_request: Optional[Tuple[str, str]] = None
    for raw in (log_text or "").splitlines():
        line = _ANSI_RE.sub("", raw)
        raised = _FLASK_EXC_RE.search(line)
        if raised:
            request = (raised.group(2), raised.group(1))
            if raised.group(1) not in ignored and request not in requests:
                requests.append(request)
            continue
        match = _ACCESS_RE.search(line)
        if not match or match.group(2) in ignored:
            continue
        request = (match.group(1), match.group(2))
        last_request = request
        if match.group(3).startswith("5") and request not in requests:
            requests.append(request)
    if not requests and last_request is not None:
        requests.append(last_request)
    return requests[:MAX_REPLAYS]


def request_lines_around(lines: Sequence[str], start: int, end: int) -> Tuple[List[str], List[str]]:
    """The lines naming the request behind the error report lines[start:end].

    (before, after): Flask's "Exception on <path> [<METHOD>]" just above the traceback,
    and the 5xx access line the server writes just below it. Trimming a crash down
    to its traceback used to drop both, so the request that crashed the target was
    never replayed - and a patch that still crashed it passed as healed.
    Nothing is taken past the next error report, so an older or newer incident's
    request can never be replayed for this one.
    """
    before = [
        line for line in lines[max(0, start - _REQUEST_LINES_BEFORE):start]
        if _FLASK_EXC_RE.search(_ANSI_RE.sub("", line))
    ]
    after: List[str] = []
    for line in lines[end:end + _REQUEST_LINES_AFTER]:
        clean = _ANSI_RE.sub("", line)
        if "Traceback (most recent call last)" in clean or _FLASK_EXC_RE.search(clean):
            break
        match = _ACCESS_RE.search(clean)
        if match and match.group(3).startswith("5"):
            after.append(line)
    return before, after


def incident_excerpt(lines: Sequence[str], header: Optional[str] = None) -> str:
    """The log text a crash hands to the Analyst: its latest error report only.

    An older traceback still in the buffer can never steer the diagnosis, and the
    lines naming the request that raised this one stay, because that request is the
    symptom re-issued after every redeploy. Without an error report (a hard exit that
    printed nothing) the tail is kept - its last request is the prime suspect.
    """
    lines = list(lines)
    span = latest_error_span(lines)
    if span is None:
        body = latest_error_block(lines)
    else:
        before, after = request_lines_around(lines, *span)
        body = before + lines[span[0]:span[1]] + after
    if not body:
        return ""
    return "\n".join(([header] if header else []) + body)


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
        boot_timeout_provider: Optional[Callable[[], float]] = None,
    ):
        self.docker_monitor = docker_monitor
        self.http_probe = http_probe
        self.event_manager = event_manager
        self.log_streamer = log_streamer
        self.boot_timeout_seconds = boot_timeout_seconds
        # iow.config.yml is re-read on every verification, so the peer can tune the
        # boot window mid-defense like every other timing value.
        self.boot_timeout_provider = boot_timeout_provider
        # Needed in gitops mode: the heal commit has to be pushed to the repository
        # Argo CD watches, otherwise the cluster can never see the fix.
        self.git_manager = git_manager
        # When the current redeploy started (logs "since the restart").
        self._restart_started: Optional[float] = None

    def _refresh_boot_timeout(self) -> None:
        if self.boot_timeout_provider is None:
            return
        try:
            value = float(self.boot_timeout_provider())
            if value > 0:
                self.boot_timeout_seconds = value
        except Exception as exc:
            logger.debug(f"boot_timeout provider failed, keeping {self.boot_timeout_seconds}s: {exc}")

    # ------------------------------------------------------------------
    def symptom_present(self, replay: Sequence[Tuple[str, str]] = ()) -> Tuple[bool, str]:
        """Is the crash still there, before anything is patched? (present, why)

        Healing a crash that is already gone - a transient blip, an old event picked
        by hand, a sibling signature of a crash an earlier heal fixed - can only change
        working code, and a model told that returning the code unchanged is not a fix
        does change it (seen live: a heal with no crash context rewrote /api/crash).
        When this cannot be told, the answer is "present": the loop runs as before.
        """
        request_once = getattr(self.http_probe, "request_once", None)
        if replay and callable(request_once):
            started = time.time()
            for method, path in replay:
                answer = request_once(method, path)
                if answer.is_crash:
                    return True, (
                        f"{method} {path} still fails "
                        f"({answer.status_code or answer.error_message})"
                    )
            # A request that kills the process answers first and exits right after.
            exit_since = getattr(self.docker_monitor, "last_crash_exit_since", None)
            if callable(exit_since):
                time.sleep(1.5)
                code = exit_since(started)
                if code is not None:
                    return True, f"the target exited with code {code} after {replay[0][0]} {replay[0][1]}"
            return False, f"{', '.join(f'{m} {p}' for m, p in replay)} succeeds now"

        state = self.docker_monitor.inspect()
        if state.status != "running":
            return True, f"the target is {state.status}"
        paths = list(getattr(self.http_probe, "probe_paths", None) or [])
        if not callable(request_once) or not paths:
            return True, "no probe can tell whether the target is healthy"
        results = [request_once("GET", path) for path in paths]
        failing = [r for r in results if r.is_crash]
        if failing or not any(r.is_healthy for r in results):
            first = failing[0] if failing else results[0]
            return True, f"{first.url} answers {first.status_code or first.error_message}"
        return False, "the target is running and every probe answers"

    def restart_and_verify(
        self,
        grace_period: float = 20.0,
        progress_callback: Optional[Callable[[float, float, str], None]] = None,
        replay: Optional[Sequence[Tuple[str, str]]] = None,
    ) -> VerificationResult:
        """Redeploys the target and monitors health until the grace period expires.

        replay: (method, path) requests that crashed the target. They are re-issued
        once it is back, so a soft crash on a route no probe covers cannot pass as
        healed merely because nobody called that route again.
        """
        now = time.time()
        mode = "docker"
        wait_for_gitops_redeploy = None
        try:
            from bonus.gitops import redeploy_mode, wait_for_gitops_redeploy as _wait
            mode = redeploy_mode()
            wait_for_gitops_redeploy = _wait
        except Exception as exc:
            logger.debug(f"GitOps helpers unavailable, using docker restart: {exc}")

        self._refresh_boot_timeout()
        logger.info(
            f"Initiating target redeploy/verify (mode={mode}, grace_period={grace_period}s, "
            f"boot_timeout={self.boot_timeout_seconds}s)..."
        )
        restart_started = time.time()
        self._restart_started = restart_started

        # The target is deliberately going away: do not let the restart itself look
        # like the crash that failed this attempt.
        suppression = self.boot_timeout_seconds + 5.0
        if self.event_manager:
            self.event_manager.suppress_events_for(suppression)
        if self.log_streamer:
            self.log_streamer.clear()
            # Only what the redeployed code logs may fail this attempt - not the
            # crash-loop backlog of the old code that arrives once the stream re-attaches.
            self.log_streamer.ignore_lines_logged_before(restart_started)

        try:
            if mode == "gitops" and wait_for_gitops_redeploy is not None:
                ok, detail = wait_for_gitops_redeploy(
                    grace_period=grace_period, git_manager=self.git_manager
                )
                if not ok:
                    logger.error(f"GitOps redeploy failed: {detail}")
                    # The rollout usually fails because the new pod crashed: hand its
                    # output back, not just kubectl's "timed out waiting".
                    return VerificationResult(
                        is_healed=False,
                        status="restart_failed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=0.0,
                        error_log=f"GitOps redeploy failed: {detail}\n{self._tail_logs()}".rstrip(),
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

            booted, boot_detail = self._wait_for_boot(progress_callback, restart_started)
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
            # What the replaced process printed on its way out - a traceback of the old
            # code, a KeyboardInterrupt from the stop signal - can reach the stream after
            # the redeploy began, but it was logged before the new process started.
            last_start = getattr(self.docker_monitor, "last_start_time", None)
            new_start = last_start() if callable(last_start) else 0.0
            if self.log_streamer and new_start >= restart_started:
                self.log_streamer.ignore_lines_logged_before(new_start)
        finally:
            # From here on, real crashes must be visible again.
            if self.event_manager:
                self.event_manager.resume_events()

        return self._observe_grace_period(grace_period, mode, progress_callback, replay or ())

    # ------------------------------------------------------------------
    def _wait_for_boot(
        self,
        progress_callback: Optional[Callable[[float, float, str], None]],
        restart_started: Optional[float] = None,
    ) -> tuple[bool, str]:
        """Block until the target answers once, or the boot timeout expires."""
        deadline = time.time() + self.boot_timeout_seconds
        last_detail = "target did not answer after redeploy"
        exit_since = getattr(self.docker_monitor, "last_crash_exit_since", None)
        answering_rounds = 0

        while time.time() < deadline:
            elapsed = self.boot_timeout_seconds - (deadline - time.time())
            if progress_callback:
                try:
                    progress_callback(elapsed, self.boot_timeout_seconds, "starting")
                except Exception:
                    pass

            # The restarted code already died on its own: no point waiting out the
            # whole boot window for a container the restart policy keeps killing.
            if restart_started is not None and callable(exit_since):
                # Only a process started by this redeploy: an old-code process the
                # restart policy launched just before the patch landed may die now.
                crashed_with = exit_since(restart_started, started_after=restart_started)
                if crashed_with is not None:
                    return False, (
                        f"Target exited with code {crashed_with} right after the restart.\n"
                        f"{self._tail_logs()}"
                    )

            state = self.docker_monitor.inspect()
            if state.status == "exited":
                exit_code = state.exit_code
                if exit_code not in (None, 0):
                    return False, (
                        f"Target exited with code {exit_code} instead of starting.\n"
                        f"{self._tail_logs()}"
                    )
                last_detail = "target exited cleanly instead of serving"
            elif state.status in ("not_found", "daemon_unavailable", "error", "not_started"):
                last_detail = state.error_reason or state.status

            if self.http_probe is not None:
                results = self.http_probe.probe_all_once()
                if any(r.is_healthy for r in results):
                    return True, f"healthy after {elapsed:.1f}s"
                if results and all(r.status_code is not None for r in results):
                    # Serving, but every route errors (a half-fixed patch). That is
                    # booted: the grace period fails it on the real 5xx within a
                    # second instead of this loop waiting out the whole boot window.
                    answering_rounds += 1
                    if answering_rounds >= 3:
                        return True, f"answering with HTTP errors after {elapsed:.1f}s"
                else:
                    answering_rounds = 0
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
        replay: Sequence[Tuple[str, str]] = (),
    ) -> VerificationResult:
        """Watch a booted target for the full grace period."""
        new_crash_events: List[ObserverEvent] = []

        def _verification_crash_listener(event: ObserverEvent) -> None:
            if event.type == "crash":
                new_crash_events.append(event)

        if self.event_manager:
            # Every occurrence: the same crash coming back inside the dedup window is
            # folded into the old event, but it still means this patch did not work.
            self.event_manager.add_listener(_verification_crash_listener, every_occurrence=True)

        start_time = time.time()
        poll_interval = 0.5
        consecutive_probe_failures = 0
        last_probe_error: Optional[str] = None
        container_status = "unknown"

        try:
            # Re-issue the request(s) that crashed the target: the symptom itself.
            request_once = getattr(self.http_probe, "request_once", None)
            replays = list(replay) if callable(request_once) else []
            for method, path in replays:
                answer = request_once(method, path)  # type: ignore[misc]
                logger.info(
                    f"Replayed the failing request {method} {path}: "
                    f"{answer.status_code or answer.error_message}"
                )
                if answer.is_crash:
                    return VerificationResult(
                        is_healed=False,
                        status="crashed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(time.time() - start_time, 2),
                        error_log=(
                            f"The request that crashed the target still fails after the "
                            f"patch: {method} {path} -> "
                            f"{answer.status_code or answer.error_message}\n{self._tail_logs()}"
                        ),
                        container_status=self.docker_monitor.inspect().status,
                        details={"replayed": f"{method} {path}", "redeploy_mode": mode},
                        timestamp=time.time(),
                    )

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
                        or ""
                    )
                    if "Traceback" not in error_log and "Error" not in error_log:
                        # A probe / exit event knows *that* it failed; the traceback
                        # is in what the redeployed code printed since the restart.
                        error_log = f"{latest_crash.summary}\n{self._tail_logs()}".strip()
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

                # Check 2a: an exit the restart policy already papered over.
                exit_since = getattr(self.docker_monitor, "last_crash_exit_since", None)
                crashed_with = (
                    exit_since(start_time, started_after=self._restart_started or start_time)
                    if callable(exit_since) else None
                )
                if crashed_with is not None:
                    reason = f"target exited with code {crashed_with} during the grace period"
                    logger.warning(f"Verification FAILED at {elapsed:.1f}s: {reason}")
                    return VerificationResult(
                        is_healed=False,
                        status="crashed",
                        grace_period_seconds=grace_period,
                        elapsed_seconds=round(elapsed, 2),
                        error_log=f"{reason}\n{self._tail_logs()}",
                        container_status="exited",
                        details={"exit_code": crashed_with, "redeploy_mode": mode},
                        timestamp=time.time()
                    )

                # Check 2b: container state
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
    def _tail_logs(self, tail: int = 30, wait_for_error: float = 3.0) -> str:
        """What the redeployed code printed - "the new error log" for the Analyst.

        Read from the daemon since the restart when possible (waiting briefly for the
        traceback to be written): right after a failed boot the followed stream is
        often still re-attaching, and its buffer was cleared at the restart.
        """
        fetch = getattr(self.docker_monitor, "fetch_logs", None)
        if callable(fetch) and self._restart_started is not None:
            deadline = time.time() + wait_for_error
            text = ""
            while True:
                text = (fetch(since=self._restart_started, tail=tail) or "").strip()
                if "Traceback" in text or "Error" in text or time.time() >= deadline:
                    break
                time.sleep(0.3)
            if text:
                return text
        if not self.log_streamer:
            return ""
        return self.log_streamer.get_recent_logs_as_text(tail=tail)
