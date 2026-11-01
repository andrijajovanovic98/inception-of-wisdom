"""Part 3 - Verifier: healed only if the target stays up for the grace period."""

from __future__ import annotations

import time
import unittest
from typing import List, Optional
from unittest import mock

from p1.docker_monitor import STABLE_UPTIME_S, ContainerState
from p1.event_manager import EventManager
from p1.http_probe import ProbeResult
from p1.log_streamer import incident_excerpt
from p3.verifier import TargetVerifier, failing_requests_from_log


class FakeContainer:
    def __init__(self) -> None:
        self.status = "running"
        self.crash_exit_at: Optional[float] = None
        self.started_at = 0.0                     # when the crashing process was started
        self.restarts = 0
        self.logs = ""

    def restart_target(self, timeout: int = 10) -> bool:
        self.restarts += 1
        self.started_at = time.time()             # a redeploy starts a new process
        return True

    def inspect(self) -> ContainerState:
        return ContainerState(name="t", status=self.status, timestamp=time.time())

    def fetch_logs(self, since: Optional[float] = None, tail: int = 60) -> str:
        return self.logs

    def last_crash_exit_since(self, since: float, started_after: Optional[float] = None) -> Optional[int]:
        if self.crash_exit_at is not None and self.crash_exit_at >= since:
            if started_after is None or self.started_at >= started_after:
                return 1
        return None


def _result(code: Optional[int]) -> ProbeResult:
    return ProbeResult(
        url="http://t/", status_code=code,
        is_healthy=code is not None and code < 300,
        is_crash=code is None or code >= 500,
        is_suggestion=code is not None and 400 <= code < 500,
        error_message=None if code == 200 else f"HTTP {code}",
    )


class FakeProbe:
    def __init__(self, codes: List[Optional[int]], replay_code: int = 200) -> None:
        self.codes = codes
        self.replay_code = replay_code
        self.replayed: List[str] = []

    def probe_all_once(self) -> List[ProbeResult]:
        return [_result(code) for code in self.codes]

    def request_once(self, method: str, path: str) -> ProbeResult:
        self.replayed.append(f"{method} {path}")
        return _result(self.replay_code)


class ScriptedProbe(FakeProbe):
    """One status code per probe round, the last repeating: round 1 is the boot check, the rest
    are the grace period's."""

    def __init__(self, rounds: List[Optional[int]]) -> None:
        super().__init__([200])
        self.rounds = list(rounds)
        self.round = 0

    def probe_all_once(self) -> List[ProbeResult]:
        code = self.rounds[min(self.round, len(self.rounds) - 1)]
        self.round += 1
        return [_result(code)]


def _verifier(container: FakeContainer, probe: FakeProbe) -> TargetVerifier:
    return TargetVerifier(
        docker_monitor=container, http_probe=probe, boot_timeout_seconds=20,  # type: ignore[arg-type]
    )


class VerifierTests(unittest.TestCase):
    def test_stays_up_for_the_grace_period(self) -> None:
        container = FakeContainer()
        result = _verifier(container, FakeProbe([200, 200, 404])).restart_and_verify(grace_period=0.6)
        self.assertTrue(result.is_healed)
        self.assertGreaterEqual(result.elapsed_seconds, 0.6)
        self.assertEqual(container.restarts, 1)

    def test_two_dropped_requests_in_a_row_are_noise_three_are_an_outage(self) -> None:
        """T4: "one dropped request is noise; several in a row means the service really
        stopped answering" - a mutation run failed the verdict on the first dropped request
        and every suite stayed green."""
        noisy = _verifier(FakeContainer(), ScriptedProbe([200, 500, 500, 200])).restart_and_verify(
            grace_period=2.0)
        self.assertTrue(noisy.is_healed, noisy.error_log)
        down = _verifier(FakeContainer(), ScriptedProbe([200, 500, 500, 500])).restart_and_verify(
            grace_period=5.0)
        self.assertEqual(down.status, "http_failure")
        self.assertEqual(down.details["consecutive_failures"], 3)

    def test_what_the_replaced_process_printed_is_history(self) -> None:
        """Seen live: the old process's KeyboardInterrupt (the restart's own SIGINT)
        reached the stream after the redeploy began and failed a correct patch."""
        class Streamer:
            def __init__(self) -> None:
                self.not_before: List[float] = []

            def clear(self) -> None:
                pass

            def ignore_lines_logged_before(self, ts: float) -> None:
                self.not_before.append(ts)

        container = FakeContainer()
        container.last_start_time = lambda: container.started_at   # type: ignore[attr-defined]
        streamer = Streamer()
        verifier = TargetVerifier(
            docker_monitor=container, http_probe=FakeProbe([200]),  # type: ignore[arg-type]
            log_streamer=streamer, boot_timeout_seconds=20,  # type: ignore[arg-type]
        )
        self.assertTrue(verifier.restart_and_verify(grace_period=0.2).is_healed)
        self.assertEqual(streamer.not_before[-1], container.started_at)   # the new process's start

    def test_serving_only_errors_fails_fast_not_after_the_boot_timeout(self) -> None:
        started = time.time()
        result = _verifier(FakeContainer(), FakeProbe([500, 500])).restart_and_verify(grace_period=5)
        self.assertFalse(result.is_healed)
        self.assertEqual(result.status, "http_failure")
        self.assertLess(time.time() - started, 8)       # boot window is 20 s

    def test_a_failed_redeploy_reports_the_exit_code_of_the_new_process(self) -> None:
        """L26: the next attempt's diagnosis must read this crash, not the event's."""
        container = FakeContainer()
        container.crash_exit_at = time.time() + 60
        result = _verifier(container, FakeProbe([None])).restart_and_verify(grace_period=1)
        self.assertEqual((result.status, result.details.get("exit_code")), ("restart_failed", 1))

    def test_the_same_crash_inside_the_dedup_window_still_fails_the_attempt(self) -> None:
        """T4 for L6: folded into the old event by deduplication, a patched target crashing the
        same way raises no new event - the Verifier listens to every occurrence."""
        events = EventManager(crash_window_seconds=60.0)
        boom = "RuntimeError: still broken"
        events.handle_log_error(boom, boom)                      # the incident being healed

        class CrashesAgain(FakeProbe):
            def request_once(self, method: str, path: str) -> ProbeResult:
                events.handle_log_error(boom, boom)              # the same crash, deduplicated
                return super().request_once(method, path)

        verifier = TargetVerifier(
            docker_monitor=FakeContainer(), http_probe=CrashesAgain([200]),  # type: ignore[arg-type]
            event_manager=events, boot_timeout_seconds=20,
        )
        result = verifier.restart_and_verify(grace_period=1, replay=[("GET", "/api/data")])
        self.assertEqual(len(events.get_events()), 1)
        self.assertEqual(result.status, "crashed")
        self.assertFalse(result.is_healed)

    def test_exit_right_after_restart_fails_fast(self) -> None:
        container = FakeContainer()
        container.crash_exit_at = time.time() + 3600  # "after the restart" for any since
        started = time.time()
        result = _verifier(container, FakeProbe([None])).restart_and_verify(grace_period=5)
        self.assertFalse(result.is_healed)
        self.assertIn("exited with code 1", result.error_log or "")
        self.assertLess(time.time() - started, 5)

    def test_a_container_that_loops_during_the_grace_period_fails_the_attempt(self) -> None:
        """T4: subject V.3, "stays up for the grace period and no new crash event fires" - the
        probes may answer while the container side reports a restart loop."""
        container = FakeContainer()
        looping = ContainerState(name="t", status="running", is_crash=True, timestamp=time.time(),
                                 error_reason="Container entered a restart loop")
        with mock.patch.object(container, "inspect", return_value=looping):
            result = _verifier(container, FakeProbe([200])).restart_and_verify(grace_period=3)
        self.assertEqual((result.is_healed, result.status), (False, "crashed"))
        self.assertIn("restart loop", result.error_log or "")

    def test_an_exit_the_restart_policy_papered_over_fails_the_grace_period(self) -> None:
        """T4: the exit state lasts milliseconds under a restart policy; the daemon's 'die' event
        is the evidence, and one after the boot fails the attempt even though the probes answer."""
        class ExitsAfterBoot(FakeContainer):
            armed = False

            def last_crash_exit_since(
                self, since: float, started_after: Optional[float] = None
            ) -> Optional[int]:
                return 1 if self.armed else None

        container = ExitsAfterBoot()

        class ArmsAfterBoot(FakeProbe):
            def probe_all_once(self) -> List[ProbeResult]:
                booted = super().probe_all_once()
                container.armed = True                       # the boot check is over: now it dies
                return booted

        result = _verifier(container, ArmsAfterBoot([200])).restart_and_verify(grace_period=3)
        self.assertEqual((result.is_healed, result.status), (False, "crashed"))
        self.assertIn("exited with code 1 during the grace period", result.error_log or "")

    def test_a_container_that_is_not_running_at_the_end_is_not_healed(self) -> None:
        """T4: "stayed up" - a target whose container is paused when the grace period ends did not."""
        container = FakeContainer()
        container.status = "paused"
        result = _verifier(container, FakeProbe([200])).restart_and_verify(grace_period=0.6)
        self.assertEqual((result.is_healed, result.status), (False, "crashed"))
        self.assertIn("paused", result.error_log or "")

    def test_the_failing_request_is_replayed_and_must_not_fail_again(self) -> None:
        """Soft crash on a route no probe covers: 'stayed up' is not enough."""
        probe = FakeProbe([200, 200], replay_code=500)
        result = _verifier(FakeContainer(), probe).restart_and_verify(
            grace_period=5, replay=[("POST", "/api/crash")]
        )
        self.assertFalse(result.is_healed)
        self.assertEqual(probe.replayed, ["POST /api/crash"])
        self.assertIn("still fails", result.error_log or "")

    def test_a_fixed_request_passes(self) -> None:
        probe = FakeProbe([200], replay_code=200)
        result = _verifier(FakeContainer(), probe).restart_and_verify(
            grace_period=0.5, replay=[("POST", "/api/crash")]
        )
        self.assertTrue(result.is_healed)

    def test_a_failed_boot_hands_back_the_new_traceback(self) -> None:
        """The next attempt's Analyst needs *this* crash, read since the restart."""
        container = FakeContainer()
        container.crash_exit_at = time.time() + 3600
        container.logs = (
            "Traceback (most recent call last):\n"
            '  File "/app/app.py", line 93, in <module>\n'
            "ValueError: Environment variable IOW_DEMO_PORT is not set\n"
        )
        result = _verifier(container, FakeProbe([None])).restart_and_verify(grace_period=5)
        self.assertIn("ValueError: Environment variable IOW_DEMO_PORT is not set", result.error_log or "")

    def test_a_failed_gitops_rollout_hands_back_the_new_pods_traceback(self) -> None:
        """Seen live: attempt 2 only got "rollout status timed out" and patched the wrong chunk."""
        from unittest import mock
        container = FakeContainer()
        container.logs = (
            "Traceback (most recent call last):\n"
            '  File "/src/demo_app/app.py", line 56, in healthcheck\n'
            "NameError: name 'jsonify' is not defined\n"
        )
        with mock.patch("bonus.gitops.redeploy_mode", lambda: "gitops"), \
                mock.patch("bonus.gitops.wait_for_gitops_redeploy",
                           lambda **_: (False, "kubectl rollout status failed: timed out")):
            result = _verifier(container, FakeProbe([None])).restart_and_verify(grace_period=5)
        self.assertEqual(result.status, "restart_failed")
        self.assertIn("rollout status failed", result.error_log or "")
        self.assertIn("NameError: name 'jsonify' is not defined", result.error_log or "")

    def test_a_probe_crash_event_hands_back_the_traceback_too(self) -> None:
        """Seen live: the next attempt got only 'HTTP 500 server error on /healthz'."""
        from p1.event_manager import EventManager

        container = FakeContainer()
        container.logs = (
            "Traceback (most recent call last):\n"
            '  File "/app/app.py", line 55, in healthcheck\n'
            "NameError: name 'jsonify' is not defined\n"
        )
        events = EventManager()
        probe = FakeProbe([200])
        verifier = TargetVerifier(
            docker_monitor=container, http_probe=probe, event_manager=events,  # type: ignore[arg-type]
        )

        def fire_probe_crash() -> None:
            time.sleep(0.3)
            events.handle_http_crash(ProbeResult(
                url="http://t/healthz", status_code=500, is_crash=True,
                error_message="HTTP 500 server error on http://t/healthz",
            ))

        import threading
        threading.Thread(target=fire_probe_crash).start()
        result = verifier.restart_and_verify(grace_period=3)
        self.assertFalse(result.is_healed)
        self.assertIn("NameError: name 'jsonify' is not defined", result.error_log or "")

    def test_container_gone_during_grace_is_not_healed(self) -> None:
        container = FakeContainer()
        probe = FakeProbe([200])
        verifier = _verifier(container, probe)
        original = container.inspect

        def later_exited() -> ContainerState:
            state = original()
            if container.restarts and time.time() - started > 0.3:
                state.status = "exited"
            return state

        container.inspect = later_exited  # type: ignore[method-assign]
        started = time.time()
        result = verifier.restart_and_verify(grace_period=2)
        self.assertFalse(result.is_healed)


class SymptomCheckTests(unittest.TestCase):
    """Before anything is patched: is the crash still there?"""

    def test_a_replayed_request_that_still_fails_is_present(self) -> None:
        verifier = _verifier(FakeContainer(), FakeProbe([200], replay_code=500))
        present, why = verifier.symptom_present([("POST", "/api/crash")])
        self.assertTrue(present)
        self.assertIn("POST /api/crash still fails", why)

    def test_a_replayed_request_that_succeeds_now_is_gone(self) -> None:
        verifier = _verifier(FakeContainer(), FakeProbe([200], replay_code=200))
        present, _ = verifier.symptom_present([("POST", "/api/crash")])
        self.assertFalse(present)

    def test_a_request_that_kills_the_process_after_answering_is_present(self) -> None:
        container = FakeContainer()
        container.crash_exit_at = time.time() + 0.5          # /api/crash-hard: 202, then exit 1
        verifier = _verifier(container, FakeProbe([200], replay_code=202))
        present, why = verifier.symptom_present([("POST", "/api/crash-hard")])
        self.assertTrue(present)
        self.assertIn("exited with code 1", why)

    def test_a_restart_loop_is_present_even_while_it_answers(self) -> None:
        """L25, seen live: a target that dies a few seconds after every start was reported
        "the symptom is gone" whenever the check found it up - the subject's own example of
        an outage, written off as gone and its heal never run."""
        container = FakeContainer()
        probe = FakeProbe([200], replay_code=200)
        probe.probe_paths = ["/"]                            # type: ignore[attr-defined]
        looping = ContainerState(name="t", status="running", is_crash=True, timestamp=time.time(),
                                 error_reason="Container entered a restart loop")
        with mock.patch.object(container, "inspect", return_value=looping):
            verifier = _verifier(container, probe)
            self.assertEqual(verifier.symptom_present(),
                             (True, "Container entered a restart loop"))
            self.assertTrue(verifier.symptom_present([("GET", "/api/data")])[0])
        self.assertEqual(probe.replayed, [])                 # known before anything is re-issued

    def test_a_restart_loop_the_process_has_outlived_is_over(self) -> None:
        """L25: Docker keeps counting a loop's restarts for a while after a fix in place ended
        it. A process that has stayed up is over its loop, whatever the counter still says, and
        the target is judged by what answers - as before the loop check existed."""
        container = FakeContainer()
        probe = FakeProbe([200], replay_code=200)
        probe.probe_paths = ["/"]                            # type: ignore[attr-defined]
        looping = ContainerState(name="t", status="running", is_crash=True, timestamp=time.time(),
                                 error_reason="Container entered a restart loop")
        container.last_start_time = lambda: time.time() - STABLE_UPTIME_S - 5   # type: ignore[attr-defined]
        with mock.patch.object(container, "inspect", return_value=looping):
            present, why = _verifier(container, probe).symptom_present()
            self.assertFalse(present, why)
            container.last_start_time = lambda: time.time() - 5                 # type: ignore[attr-defined]
            self.assertTrue(_verifier(container, probe).symptom_present()[0])   # up for 5 s: live

    def test_one_restart_in_place_is_no_restart_loop(self) -> None:
        """L25 on Kubernetes: the monitor marks any pod that restarted in place since it was last
        seen serving. Taken as a loop, a transient restart (OOM, node pressure) that was over made
        the heal go on to patch a healthy pod - the very thing this check is there to stop."""
        container = FakeContainer()
        probe = FakeProbe([200], replay_code=200)
        probe.probe_paths = ["/"]                            # type: ignore[attr-defined]
        restarted = ContainerState(
            name="t", status="running", is_crash=True, exit_code=137, restart_count=1,
            timestamp=time.time(), error_reason="Pod container terminated with exit code 137 (OOMKilled)",
        )
        with mock.patch.object(container, "inspect", return_value=restarted):
            present, why = _verifier(container, probe).symptom_present()
            self.assertFalse(present, why)
            self.assertFalse(_verifier(container, probe).symptom_present([("GET", "/api/data")])[0])
            probe.replay_code = 500                          # but a request that still fails is
            self.assertTrue(_verifier(container, probe).symptom_present([("GET", "/api/data")])[0])

    def test_one_failing_route_is_the_symptom_while_another_answers(self) -> None:
        """T4: the check is "is every probe answering", not "does any": a half-fixed target that
        still errors on /healthz is not healed because / serves."""
        class PerRoute(FakeProbe):
            def request_once(self, method: str, path: str) -> ProbeResult:
                return _result(500 if path == "/healthz" else 200)

        probe = PerRoute([200])
        probe.probe_paths = ["/", "/healthz"]                # type: ignore[attr-defined]
        present, why = _verifier(FakeContainer(), probe).symptom_present()
        self.assertTrue(present, why)

    def test_a_down_target_is_present_and_a_healthy_one_is_not(self) -> None:
        container = FakeContainer()
        probe = FakeProbe([200], replay_code=200)
        probe.probe_paths = ["/", "/healthz"]                # type: ignore[attr-defined]
        self.assertFalse(_verifier(container, probe).symptom_present()[0])
        container.status = "restarting"
        self.assertTrue(_verifier(container, probe).symptom_present()[0])
        container.status = "running"
        probe.replay_code = 500                              # running, but every route errors
        self.assertTrue(_verifier(container, probe).symptom_present()[0])


class FailingRequestTests(unittest.TestCase):
    PROBES = ["/", "/healthz"]

    def test_a_5xx_access_line_is_the_symptom(self) -> None:
        log = (
            '172.18.0.1 - - [25/Sep/2026 12:02:30] "GET /healthz HTTP/1.1" 200 -\n'
            'Traceback (most recent call last):\n'
            "RuntimeError: boom\n"
            '172.18.0.1 - - [25/Sep/2026 12:02:31] "\x1b[35m\x1b[1mPOST /api/crash HTTP/1.1\x1b[0m" 500 -\n'
        )
        self.assertEqual(failing_requests_from_log(log, self.PROBES), [("POST", "/api/crash")])

    def test_without_a_5xx_the_last_request_before_the_exit(self) -> None:
        log = (
            '"GET /api/data HTTP/1.1" 200 -\n'
            '"POST /api/crash-hard HTTP/1.1" 202 -\n'
            '"GET /healthz HTTP/1.1" 200 -\n'   # the Observer's own probe - never the culprit
        )
        self.assertEqual(failing_requests_from_log(log, self.PROBES), [("POST", "/api/crash-hard")])

    def test_a_startup_crash_has_no_request_to_replay(self) -> None:
        log = "Traceback (most recent call last):\nNameError: name 'Flask' is not defined\n"
        self.assertEqual(failing_requests_from_log(log, self.PROBES), [])

    def test_flask_naming_the_request_that_raised_is_the_symptom(self) -> None:
        log = "[ERROR] in app: Exception on /api/crash [POST]\nRuntimeError: boom\n"
        self.assertEqual(failing_requests_from_log(log, self.PROBES), [("POST", "/api/crash")])


# What the Observer's 40-line tail holds after `make break` (Flask + werkzeug).
SOFT_CRASH_TAIL = [
    "[INFO] in _internal: 172.18.0.1 - - [29/Sep/2026 12:00:00] \"GET / HTTP/1.1\" 200 -",
    "[ERROR] in app: CRITICAL: Intentional crash triggered via /api/crash!",
    "[ERROR] in app: Exception on /api/crash [POST]",
    "Traceback (most recent call last):",
    '  File "/app/app.py", line 78, in trigger_crash',
    '    raise RuntimeError("Intentional target service crash for IoW verification")',
    "RuntimeError: Intentional target service crash for IoW verification",
    "[INFO] in _internal: 172.18.0.1 - - [29/Sep/2026 12:00:01] \"POST /api/crash HTTP/1.1\" 500 -",
    "[INFO] in _internal: 127.0.0.1 - - [29/Sep/2026 12:00:02] \"GET /healthz HTTP/1.1\" 200 -",
]


class IncidentExcerptTests(unittest.TestCase):
    """The excerpt handed to the loop is also where the replayed request comes from."""

    PROBES = ["/", "/healthz"]

    def test_the_request_that_raised_survives_the_trim(self) -> None:
        """Seen live: the soft crash reached the loop as a bare traceback, nothing was
        replayed, and a patch that still crashed POST /api/crash would have passed."""
        excerpt = incident_excerpt(SOFT_CRASH_TAIL)
        self.assertIn("RuntimeError: Intentional target service crash", excerpt)
        self.assertNotIn("CRITICAL", excerpt)            # still only this error report
        self.assertEqual(failing_requests_from_log(excerpt, self.PROBES), [("POST", "/api/crash")])

    def test_an_older_soft_crash_is_not_replayed_for_a_startup_crash(self) -> None:
        tail = SOFT_CRASH_TAIL + [
            "Traceback (most recent call last):",
            '  File "/app/app.py", line 21, in <module>',
            "NameError: name 'Flask' is not defined",
        ]
        excerpt = incident_excerpt(tail, header="Container exited with status 1")
        self.assertTrue(excerpt.startswith("Container exited with status 1\nTraceback"))
        self.assertEqual(failing_requests_from_log(excerpt, self.PROBES), [])

    def test_a_silent_hard_exit_keeps_the_tail_and_its_last_request(self) -> None:
        tail = ['"GET /api/data HTTP/1.1" 200 -', '"POST /api/crash-hard HTTP/1.1" 202 -']
        excerpt = incident_excerpt(tail, header="Container exited with status 1")
        self.assertEqual(failing_requests_from_log(excerpt, self.PROBES), [("POST", "/api/crash-hard")])


if __name__ == "__main__":
    unittest.main()
