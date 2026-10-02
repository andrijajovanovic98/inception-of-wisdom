"""Part 3 - Verifier: healed only if the target stays up for the grace period."""

from __future__ import annotations

import time
import unittest
from typing import List, Optional

from p1.docker_monitor import ContainerState
from p1.http_probe import ProbeResult
from p3.verifier import TargetVerifier, failing_requests_from_log, incident_excerpt


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

    def test_exit_right_after_restart_fails_fast(self) -> None:
        container = FakeContainer()
        container.crash_exit_at = time.time() + 3600  # "after the restart" for any since
        started = time.time()
        result = _verifier(container, FakeProbe([None])).restart_and_verify(grace_period=5)
        self.assertFalse(result.is_healed)
        self.assertIn("exited with code 1", result.error_log or "")
        self.assertLess(time.time() - started, 5)

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
