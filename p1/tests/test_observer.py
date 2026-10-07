"""Part 1 - Observer: exit-code signal, deduplication, suppression, log replay."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest import mock

from p1.docker_monitor import ContainerState, DockerMonitor, target_facts_from
from p1.event_manager import EventManager, normalize_incident_key
from p1.http_probe import HttpProbeManager, ProbeResult
from p1.log_streamer import LogStreamer, latest_error_block
from p1.observer_api import create_observer_router


def _event(action: str, nano: int, exit_code: str = "", signal: str = "") -> dict:
    attrs = {}
    if exit_code:
        attrs["exitCode"] = exit_code
    if signal:
        attrs["signal"] = signal
    return {"Action": action, "timeNano": nano, "Actor": {"Attributes": attrs}}


class DockerExitEventTests(unittest.TestCase):
    """The 'die' event is the only reliable non-zero-exit signal under a restart policy."""

    def setUp(self) -> None:
        self.monitor = DockerMonitor(container_name="target")

    def test_unrequested_nonzero_exit_is_a_crash(self) -> None:
        state = self.monitor._interpret_event(_event("die", 1, exit_code="1"))
        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(state.is_crash)
        self.assertEqual(state.exit_code, 1)
        self.assertEqual(state.status, "exited")

    def test_clean_exit_is_not_a_crash(self) -> None:
        self.assertIsNone(self.monitor._interpret_event(_event("die", 1, exit_code="0")))

    def test_docker_restart_sequence_is_not_a_crash(self) -> None:
        # docker restart: kill(15) ... kill(9) -> die(137) -> start -> restart
        seq = [
            _event("kill", 1, signal="15"),
            _event("kill", 2, signal="9"),
            _event("stop", 3),
            _event("die", 4, exit_code="137"),
            _event("start", 5),
            _event("restart", 6),
        ]
        self.assertTrue(all(self.monitor._interpret_event(e) is None for e in seq))

    def test_a_kill_explains_only_one_exit(self) -> None:
        """The restarted code crashing right after our restart is still reported."""
        self.assertIsNone(self.monitor._interpret_event(_event("kill", 1, signal="2")))
        self.assertIsNone(self.monitor._interpret_event(_event("die", 2, exit_code="0")))
        crash = self.monitor._interpret_event(_event("die", 3, exit_code="1"))
        self.assertIsNotNone(crash)

    def test_healthcheck_exec_events_are_ignored(self) -> None:
        self.assertIsNone(self.monitor._interpret_event(_event("exec_die", 1, exit_code="1")))

    def test_replayed_event_is_ignored(self) -> None:
        self.assertIsNotNone(self.monitor._interpret_event(_event("die", 10, exit_code="3")))
        self.assertIsNone(self.monitor._interpret_event(_event("die", 10, exit_code="3")))

    def test_oom_is_a_crash(self) -> None:
        state = self.monitor._interpret_event(_event("oom", 1))
        self.assertIsNotNone(state)
        assert state is not None
        self.assertTrue(state.is_crash)

    def test_last_crash_exit_since(self) -> None:
        before = time.time()
        self.monitor._interpret_event(_event("die", time.time_ns(), exit_code="2"))
        self.assertEqual(self.monitor.last_crash_exit_since(before), 2)
        self.assertIsNone(self.monitor.last_crash_exit_since(time.time() + 1))

    def test_the_target_facts_never_carry_secrets_or_base_image_noise(self) -> None:
        facts = target_facts_from(
            ["5000", 5000, "tcp"],
            [("PORT", "5000"), ("TZ", "UTC"), ("PATH", "/usr/bin"), ("GPG_KEY", "A0"),
             ("PYTHON_SHA256", "91bc"), ("API_TOKEN", "s3cret"), ("DB_PASSWORD", "hunter2")],
        )
        self.assertEqual(facts, {"ports": [5000], "env": {"PORT": "5000", "TZ": "UTC"}})

    def test_an_old_process_dying_after_the_redeploy_is_not_the_new_code(self) -> None:
        """Seen live: the restart policy started the old code just before the patch
        landed; it died a moment after the redeploy began, and a correct fix was failed
        as "exited with code 1 right after the restart" (and rolled back)."""
        redeploy = time.time()
        ns = int(redeploy * 1e9)
        self.monitor._interpret_event(_event("start", ns - 300_000_000))       # old code
        self.monitor._interpret_event(_event("die", ns + 200_000_000, exit_code="1"))
        self.assertEqual(self.monitor.last_crash_exit_since(redeploy), 1)     # a crash, yes
        self.assertIsNone(self.monitor.last_crash_exit_since(redeploy, started_after=redeploy))
        self.monitor._interpret_event(_event("start", ns + 300_000_000))       # the new code
        self.monitor._interpret_event(_event("die", ns + 800_000_000, exit_code="1"))
        self.assertEqual(self.monitor.last_crash_exit_since(redeploy, started_after=redeploy), 1)


class _Container:
    def __init__(self, status: str, exit_code: int, policy: str) -> None:
        self.status = status
        self.attrs = {"State": {"Status": status, "ExitCode": exit_code},
                      "HostConfig": {"RestartPolicy": {"Name": policy}}, "RestartCount": 0}

    def reload(self) -> None:
        pass


class StoppedTargetTests(unittest.TestCase):
    """Seen live: after a `docker stop` the loop rewrote working routes and called it healed."""

    def _state(self, status: str, exit_code: int, policy: str = "unless-stopped") -> ContainerState:
        monitor = DockerMonitor(container_name="target")
        container = _Container(status, exit_code, policy)
        client = type("Client", (), {})()
        client.containers = type("Containers", (), {"get": lambda _s, name: container})()
        monitor._get_client = lambda: client  # type: ignore[method-assign]
        return monitor.inspect()

    def test_a_stopped_killed_or_paused_target_is_down_but_not_crashed(self) -> None:
        for status, code in (("exited", 0), ("exited", 137), ("paused", 0)):   # stop, kill, pause
            state = self._state(status, code)
            self.assertTrue(state.stopped, (status, code))
            self.assertFalse(state.is_crash, (status, code))
            self.assertIn("docker", state.error_reason or "")

    def test_a_crash_exit_the_restart_policy_does_not_cover_is_still_a_crash(self) -> None:
        state = self._state("exited", 1, policy="no")
        self.assertTrue(state.is_crash)
        self.assertFalse(state.stopped)
        self.assertFalse(self._state("restarting", 1).stopped)


class DaemonOutageTests(unittest.TestCase):
    def test_an_outage_is_reported_once_not_on_every_poll(self) -> None:
        """O6: a 30 s outage used to log 36 warnings."""
        from p1 import docker_monitor
        monitor = DockerMonitor(container_name="target")
        down = mock.Mock(from_env=mock.Mock(side_effect=OSError("no socket")),
                         DockerClient=mock.Mock(side_effect=OSError("no socket")))
        with mock.patch.object(docker_monitor, "docker", down), \
                mock.patch.object(docker_monitor.logger, "warning") as warning:
            states = [monitor.inspect().status for _ in range(10)]
        self.assertEqual(set(states), {"daemon_unavailable"})          # not a crash
        self.assertEqual(warning.call_count, 1)

    def test_the_crash_window_outlasts_dockers_restart_backoff(self) -> None:
        """R6: a 60 s window re-raised a long crash loop every minute (Docker backs off up to 60 s)."""
        import os
        import yaml
        from p3.safety import LIVE_SETTINGS
        config = os.path.join(os.path.dirname(__file__), "..", "..", "demo_app", "iow.config.yml")
        with open(config, encoding="utf-8") as f:
            shipped = yaml.safe_load(f)["target"]["crash_dedup_window_seconds"]
        default = next(d for _, key, _, d, _ in LIVE_SETTINGS if key == "crash_dedup_window_seconds")
        self.assertGreater(min(shipped, default, EventManager().crash_window_seconds), 60)


class _Connected:
    async def is_disconnected(self) -> bool:
        return False


class SseFanOutTests(unittest.TestCase):
    def test_a_worker_thread_wakes_the_clients_stream_at_once(self) -> None:
        """O4: the log / probe / Docker threads put into the clients' asyncio queues directly -
        not thread-safe, and a waiting stream only saw the line on its next 1 s heartbeat."""
        streamer = mock.Mock()
        router = create_observer_router(log_streamer=streamer)
        on_line = streamer.add_line_listener.call_args[0][0]

        async def scenario() -> tuple:
            stream = (await router.stream_events(_Connected())).body_iterator
            connected = await stream.__anext__()
            put_at = []

            def log_line() -> None:                        # arrives while the stream waits
                put_at.append(time.monotonic())
                on_line("Traceback (most recent call last):", True)

            worker = threading.Timer(0.2, log_line)
            worker.start()
            line = await asyncio.wait_for(stream.__anext__(), timeout=3)
            delay = time.monotonic() - put_at[0]
            worker.join()
            await stream.aclose()
            return connected, line, delay

        connected, line, delay = asyncio.run(scenario())
        self.assertIn("event: connected", connected)
        self.assertIn("event: log", line)
        self.assertIn("Traceback", line)
        self.assertLess(delay, 0.5)


class LogRouteTests(unittest.TestCase):
    def test_clear_empties_the_server_buffer_too(self) -> None:
        """D4: *Clear* emptied the view only - the next refresh brought every line back."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        streamer = LogStreamer(_NoLogs())
        streamer._process_chunk("Traceback (most recent call last):\nKeyError: 'X'\n")
        app = FastAPI()
        app.include_router(create_observer_router(log_streamer=streamer))
        client = TestClient(app)
        self.assertEqual(client.get("/api/observer/logs").json()["count"], 2)
        self.assertEqual(client.post("/api/observer/logs/clear").json()["status"], "cleared")
        self.assertEqual(client.get("/api/observer/logs").json()["count"], 0)
        self.assertEqual(streamer.get_recent_logs(), [])


class DeduplicationTests(unittest.TestCase):
    def test_same_incident_is_one_event(self) -> None:
        em = EventManager()
        tb = (
            'Traceback (most recent call last):\n'
            '  File "/app/app.py", line 20, in <module>\n'
            "NameError: name 'Flask' is not defined"
        )
        first = em.handle_log_error("NameError: name 'Flask' is not defined", tb)
        second = em.handle_log_error("NameError: name 'Flask' is not defined", tb)
        self.assertIs(first, second)
        self.assertEqual(len(em.get_events()), 1)
        self.assertEqual(em.get_events()[0]["count"], 2)

    def test_occurrence_listeners_also_hear_deduplicated_repeats(self) -> None:
        """The Verifier must see the same crash come back inside the window."""
        em = EventManager()
        new_events: list = []
        occurrences: list = []
        em.add_listener(new_events.append)
        em.add_listener(occurrences.append, every_occurrence=True)
        for _ in range(3):
            em.handle_log_error("RuntimeError: boom", "")
        self.assertEqual(len(new_events), 1)
        self.assertEqual(len(occurrences), 3)
        em.remove_listener(occurrences.append)
        em.handle_log_error("RuntimeError: boom", "")
        self.assertEqual(len(occurrences), 3)

    def test_every_exit_of_a_crash_loop_shares_one_signature(self) -> None:
        em = EventManager()
        for code in (1, 1, 1):
            em.handle_docker_state(ContainerState(
                name="t", status="exited", exit_code=code, is_crash=True,
                error_reason="Container exited with a non-zero status",
            ))
        events = em.get_events(event_type="crash")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["details"]["kind"], "exit")

    def test_pod_states_are_classified_like_container_states(self) -> None:
        """gitops mode: KubeTargetMonitor reports in Kubernetes' own words."""
        em = EventManager()
        em.handle_docker_state(ContainerState(
            name="ns/app", status="restarting", exit_code=1, is_crash=True,
            error_reason="Pod entered CrashLoopBackOff (restarts: 4)",
        ))
        em.handle_docker_state(ContainerState(
            name="ns/app", status="running", exit_code=1, is_crash=True,
            error_reason="Pod container terminated with exit code 1 (Error)",
        ))
        kinds = sorted(e["details"]["kind"] for e in em.get_events(event_type="crash"))
        self.assertEqual(kinds, ["exit", "restart_loop"])

    def test_unreachable_target_is_one_event_not_one_per_url(self) -> None:
        em = EventManager()
        for url in ("http://t/", "http://t/healthz", "http://t/api/x"):
            em.handle_http_crash(ProbeResult(
                url=url, is_crash=True, error_message=f"Connection refused to {url}",
            ))
        self.assertEqual(len(em.get_events(event_type="crash")), 1)

    def test_5xx_stays_per_route(self) -> None:
        em = EventManager()
        em.handle_http_crash(ProbeResult(url="http://t/a", status_code=500, is_crash=True))
        em.handle_http_crash(ProbeResult(url="http://t/b", status_code=500, is_crash=True))
        self.assertEqual(len(em.get_events(event_type="crash")), 2)

    def test_suggestion_is_not_a_crash(self) -> None:
        em = EventManager()
        em.handle_http_suggestion(ProbeResult(url="http://t/x", status_code=404, is_suggestion=True))
        self.assertEqual(em.get_events()[0]["type"], "suggestion")
        self.assertEqual(em.get_events(event_type="crash"), [])

    def test_restart_window_suppresses_container_signals(self) -> None:
        em = EventManager()
        em.suppress_events_for(30)
        em.handle_docker_state(ContainerState(
            name="t", status="exited", exit_code=137, is_crash=True, error_reason="x",
        ))
        self.assertEqual(em.get_events(), [])
        em.resume_events()
        em.handle_docker_state(ContainerState(
            name="t", status="exited", exit_code=1, is_crash=True, error_reason="x",
        ))
        self.assertEqual(len(em.get_events()), 1)

    def test_a_verified_heal_ends_the_incident(self) -> None:
        """Seen live: the same crash planted again right after its heal was folded into
        the healed incident and raised nothing for three minutes."""
        em = EventManager(crash_window_seconds=60.0)
        state = ContainerState(name="t", status="exited", exit_code=1, is_crash=True, error_reason="x")
        em.handle_docker_state(state)
        em.handle_http_suggestion(ProbeResult(url="http://t/x", status_code=404, is_suggestion=True))
        self.assertEqual(em.close_incidents("crash"), 1)
        em.handle_docker_state(state)                      # the crash is back: a new incident
        self.assertEqual(len(em.get_events(event_type="crash")), 2)
        em.handle_http_suggestion(ProbeResult(url="http://t/x", status_code=404, is_suggestion=True))
        self.assertEqual(len(em.get_events(event_type="suggestion")), 1)   # suggestions untouched

    def test_a_target_serving_again_ends_the_outage_but_not_a_soft_crash(self) -> None:
        """Seen live: after a rollback and `make demo-reset` the same crash planted again
        raised nothing for 207 s - its repeats kept extending the old incident."""
        em = EventManager(crash_window_seconds=60.0)
        down = ContainerState(name="t", status="exited", exit_code=1, is_crash=True, error_reason="x")
        em.handle_docker_state(down)
        em.handle_log_error("RuntimeError: boom", "RuntimeError: boom")        # a soft crash
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        em.handle_probe_round(ok)
        em.handle_docker_state(down)                        # one healthy round is not enough
        self.assertEqual(len(em.get_events(event_type="crash")), 2)
        em.handle_probe_round(ok)
        em.handle_probe_round(ok)                           # back for two rounds: outage over
        em.handle_docker_state(down)
        em.handle_log_error("RuntimeError: boom", "RuntimeError: boom")
        kinds = [e["details"]["kind"] for e in em.get_events(event_type="crash")]
        self.assertEqual(kinds.count("exit"), 2)            # the crash came back: new event
        self.assertEqual(kinds.count("log_pattern"), 1)     # the soft crash still folds in

    def test_window_expiry_allows_a_new_event(self) -> None:
        em = EventManager(crash_window_seconds=0.05)
        em.handle_log_error("RuntimeError: boom", "")
        time.sleep(0.1)
        em.handle_log_error("RuntimeError: boom", "")
        self.assertEqual(len(em.get_events()), 2)

    def test_an_older_traceback_in_the_context_does_not_split_the_incident(self) -> None:
        """Seen live: one SyntaxError showed up under two signatures."""
        syntax = [
            '  File "/app/app.py", line 21',
            "    app = Flask(__name__",
            "SyntaxError: '(' was never closed",
        ]
        older = [
            "Traceback (most recent call last):",
            '  File "/app/app.py", line 92, in <module>',
            "KeyError: 'IOW_DEMO_PORT'",
        ]
        headline = "SyntaxError: '(' was never closed"
        self.assertEqual(
            normalize_incident_key(headline, "\n".join(syntax)),
            normalize_incident_key(headline, "\n".join(older + ["INFO x"] + syntax)),
        )

    def test_colour_codes_are_stripped(self) -> None:
        streamer = LogStreamer(_NoLogs())
        streamer._process_chunk('"\x1b[33mGET /api/missing-route HTTP/1.1\x1b[0m" 404 -\n')
        self.assertEqual(streamer.get_recent_logs(), ['"GET /api/missing-route HTTP/1.1" 404 -'])

    def test_incident_key_ignores_volatile_numbers(self) -> None:
        a = normalize_incident_key("RuntimeError: worker 12 died at 0x7f00aa")
        b = normalize_incident_key("RuntimeError: worker 99 died at 0x7f11bb")
        self.assertEqual(a, b)


class _NoLogs:
    container_name = "t"

    def get_log_stream(self, tail=None, since=None):  # pragma: no cover - never called
        return None


class LogStreamerTests(unittest.TestCase):
    def _streamer(self, sink: list) -> LogStreamer:
        streamer = LogStreamer(
            _NoLogs(),
            on_error_detected=lambda line, ctx: sink.append(line),
            incident_debounce_seconds=0,
        )
        streamer._agent_start_ts = 1000.0
        return streamer

    def test_history_before_agent_start_never_raises(self) -> None:
        sink: list = []
        streamer = self._streamer(sink)
        streamer._process_chunk("1970-01-01T00:10:00.000000000Z RuntimeError: old\n")
        self.assertEqual(sink, [])
        self.assertIn("RuntimeError: old", streamer.get_recent_logs())

    def test_replayed_lines_are_not_detected_twice(self) -> None:
        sink: list = []
        streamer = self._streamer(sink)
        line = "2030-01-01T00:00:00.000000000Z RuntimeError: fresh\n"
        streamer._process_chunk(line)
        streamer._process_chunk(line)  # reattach replays the same line
        self.assertEqual(sink, ["RuntimeError: fresh"])
        self.assertEqual(streamer.get_recent_logs(), ["RuntimeError: fresh"])  # shown once

    def test_lines_microseconds_apart_are_not_mistaken_for_replays(self) -> None:
        """Docker stamps a traceback's lines 1-4 µs apart; a float epoch (or a µs cut)
        cannot tell them apart, and the later lines would be dropped as replays."""
        streamer = self._streamer([])
        streamer._process_chunk(
            "2030-01-01T00:00:00.671535353Z Traceback (most recent call last):\n"
            "2030-01-01T00:00:00.671535753Z   File \"/app/app.py\", line 21, in <module>\n"
            "2030-01-01T00:00:00.671536001Z NameError: name 'Flask' is not defined\n"
        )
        self.assertEqual(len(streamer.get_recent_logs()), 3)

    def test_backlog_logged_before_a_restart_is_not_a_fresh_crash(self) -> None:
        """The old code's crash-loop tracebacks arrive after the stream re-attaches;
        only what the restarted code logs may count."""
        sink: list = []
        streamer = self._streamer(sink)
        streamer._process_chunk("2030-01-01T00:00:00.000000000Z NameError: old code\n")
        restart = 1893456060.0  # 2030-01-01T00:01:00Z
        streamer.ignore_lines_logged_before(restart)
        streamer._process_chunk("2030-01-01T00:00:30.000000000Z NameError: backlog\n")
        streamer._process_chunk("2030-01-01T00:01:05.000000000Z NameError: new code\n")
        self.assertEqual(sink, ["NameError: old code", "NameError: new code"])

    def test_traceback_indentation_survives_the_buffer(self) -> None:
        """Seen live: stripped lines left the Analyst with a bare 'KeyError' line."""
        streamer = self._streamer([])
        streamer._process_chunk(
            "2030-01-01T00:00:00.000000000Z Traceback (most recent call last):\n"
            '2030-01-01T00:00:00.000000001Z   File "/app/app.py", line 93, in <module>\n'
            "2030-01-01T00:00:00.000000002Z     port = int(os.environ['IOW_DEMO_PORT'])\n"
            "2030-01-01T00:00:00.000000003Z KeyError: 'IOW_DEMO_PORT'\n"
        )
        block = latest_error_block(streamer.get_recent_logs())
        self.assertEqual(len(block), 4)
        self.assertTrue(block[1].startswith('  File "/app/app.py", line 93'))

    def test_invalid_configured_pattern_matches_literally(self) -> None:
        streamer = LogStreamer(_NoLogs(), error_patterns=["Traceback (unbalanced"])
        self.assertTrue(any(p.search("Traceback (unbalanced") for p in streamer.compiled_patterns))

    def test_a_process_stopped_by_its_stop_signal_did_not_crash(self) -> None:
        """Seen live: the Wisdom Loop's own restart sent SIGINT (the target's STOPSIGNAL)
        to the old process mid-import; its KeyboardInterrupt traceback failed the
        verification of a correct patch."""
        sink: list = []
        streamer = LogStreamer(                     # the debounce collects the whole traceback
            _NoLogs(), on_error_detected=lambda line, ctx: sink.append(line),
            incident_debounce_seconds=0.2,
        )
        streamer._agent_start_ts = 1000.0
        streamer._process_chunk(
            "2030-01-01T00:00:00.1Z Traceback (most recent call last):\n"
            '2030-01-01T00:00:00.2Z   File "/app/app.py", line 9, in <module>\n'
            "2030-01-01T00:00:00.3Z     import logging\n"
            "2030-01-01T00:00:00.4Z KeyboardInterrupt\n"
        )
        time.sleep(0.4)
        self.assertEqual(sink, [])
        streamer._process_chunk(                    # a real crash is still one
            "2030-01-01T00:00:01.1Z Traceback (most recent call last):\n"
            '2030-01-01T00:00:01.2Z   File "/app/app.py", line 21, in <module>\n'
            "2030-01-01T00:00:01.3Z NameError: name 'Flask' is not defined\n"
        )
        time.sleep(0.4)
        self.assertEqual(sink, ["NameError: name 'Flask' is not defined"])

    def test_an_incident_still_debouncing_at_a_restart_is_history(self) -> None:
        """Seen live: flushed after the Verifier cleared the buffer, a queued NameError
        became a second event with no context and a different signature."""
        sink: list = []
        streamer = LogStreamer(
            _NoLogs(), on_error_detected=lambda line, ctx: sink.append((line, ctx)),
            incident_debounce_seconds=0.3,
        )
        streamer._agent_start_ts = 1000.0
        streamer._process_chunk("2030-01-01T00:00:00.0Z NameError: name 'Flask' is not defined\n")
        streamer.clear()
        streamer.ignore_lines_logged_before(time.time())
        time.sleep(0.5)
        self.assertEqual(sink, [])

    def test_restarting_the_stream_leaves_exactly_one_reader(self) -> None:
        """Seen live: after one "Restart Stream" every line reached the dashboard twice -
        the old worker was still blocked on its stream and got revived."""
        source = _FollowedLogs()
        streamer = LogStreamer(source, incident_debounce_seconds=0)
        streamer.start()
        _wait_for(lambda: source.opened == 1)
        old_worker = streamer._worker_thread
        streamer.restart()
        _wait_for(lambda: source.opened == 2)
        _wait_for(lambda: old_worker is not None and not old_worker.is_alive())
        self.assertTrue(source.streams[0].closed.is_set())
        self.assertFalse(old_worker is not None and old_worker.is_alive())
        streamer.stop()
        self.assertTrue(source.streams[1].closed.is_set())


class _BlockingStream:
    """A followed log: yields its lines, then blocks like Docker until closed."""

    def __init__(self, lines: list) -> None:
        self.lines = lines
        self.closed = threading.Event()

    def __iter__(self):
        yield from self.lines
        self.closed.wait(10)

    def close(self) -> None:
        self.closed.set()


class _FollowedLogs:
    container_name = "t"

    def __init__(self) -> None:
        self.opened = 0
        self.streams: list = []

    def get_log_stream(self, tail=None, since=None):
        self.opened += 1
        stream = _BlockingStream([f"2030-01-01T00:00:0{self.opened}.0Z line {self.opened}\n".encode()])
        self.streams.append(stream)
        return stream


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while not condition() and time.time() < deadline:
        time.sleep(0.02)


class ProbeListTests(unittest.TestCase):
    def test_a_removed_probe_url_stops_counting(self) -> None:
        """iow.config.yml edits swap the probe list live; a stale 5xx must not linger."""
        probe = HttpProbeManager(service_url="http://t", probe_paths=["/", "/broken"])
        probe._latest_results = {
            "http://t/": ProbeResult(url="http://t/", status_code=200, is_healthy=True),
            "http://t/broken": ProbeResult(url="http://t/broken", status_code=500, is_crash=True),
        }
        self.assertFalse(probe.is_target_healthy())
        probe.set_probe_paths(["/"])
        self.assertTrue(probe.is_target_healthy())
        self.assertEqual(list(probe.get_latest_results()), ["http://t/"])


class LatestErrorBlockTests(unittest.TestCase):
    OLD = [
        "Traceback (most recent call last):",
        '  File "/app/app.py", line 92, in <module>',
        "KeyError: 'IOW_DEMO_PORT'",
        "INFO some request",
    ]

    def test_a_syntax_error_without_traceback_header_is_not_mixed_with_an_older_report(self) -> None:
        """Seen live: the old KeyError traceback steered a SyntaxError heal to line 92."""
        new = [
            '  File "/app/app.py", line 21',
            "    app = Flask(__name__",
            "               ^",
            "SyntaxError: '(' was never closed",
        ]
        self.assertEqual(latest_error_block(self.OLD + new), new)

    def test_a_traceback_is_taken_from_its_header(self) -> None:
        new = [
            "Traceback (most recent call last):",
            '  File "/app/app.py", line 78, in trigger_crash',
            "RuntimeError: boom",
        ]
        self.assertEqual(latest_error_block(self.OLD + ["INFO x"] + new + ["INFO after"]), new)

    def test_no_error_keeps_the_tail(self) -> None:
        self.assertEqual(latest_error_block(["a", "b"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
