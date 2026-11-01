"""Part 1 - Observer: exit-code signal, deduplication, suppression, log replay."""

from __future__ import annotations

import asyncio
import http.server
import socket
import threading
import time
import unittest
from typing import Any, Dict, List
from unittest import mock

from p1 import event_manager
from p1.docker_monitor import (
    RESTART_STUCK_S, STABLE_UPTIME_S, ContainerState, DockerMonitor, target_facts_from,
)
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

    def test_our_own_restart_explains_the_exit_that_follows_it(self) -> None:
        """T4: the daemon's 'kill' event announces a restart, but the event stream may be
        reconnecting at that moment: restart_target() says so itself, and only for the one exit."""
        class Restartable(_Container):
            def restart(self, timeout: int = 10) -> None:
                pass

        monitor, _ = _monitored(Restartable("running", 0, "unless-stopped"))
        self.assertTrue(monitor.restart_target())
        self.assertIsNone(monitor._interpret_event(_event("die", 1, exit_code="137")))
        self.assertIsNotNone(monitor._interpret_event(_event("die", 2, exit_code="1")))

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
        self.attrs: Dict[str, Any] = {"State": {"Status": status, "ExitCode": exit_code},
                                      "HostConfig": {"RestartPolicy": {"Name": policy}}, "RestartCount": 0}

    def reload(self) -> None:
        pass


def _monitored(container: _Container) -> tuple:
    """A real DockerMonitor reading this container (monitor, container)."""
    monitor = DockerMonitor(container_name="t")
    client = type("Client", (), {})()
    client.containers = type("Containers", (), {"get": lambda _s, name: container})()
    monitor._get_client = lambda: client  # type: ignore[method-assign]
    return monitor, container


class ContainerStateTests(unittest.TestCase):
    """T4: the subject's container states, read from a real DockerMonitor."""

    def test_a_dead_or_oom_killed_container_is_a_crash(self) -> None:
        dead = _Container("dead", 137, "no")
        dead.attrs["State"]["Dead"] = True
        state = _monitored(dead)[0].inspect()
        self.assertTrue(state.is_crash)
        self.assertIn("dead", state.error_reason or "")
        killed = _Container("running", 0, "no")
        killed.attrs["State"]["OOMKilled"] = True
        state = _monitored(killed)[0].inspect()
        self.assertTrue(state.is_crash)
        self.assertIn("OOMKilled", state.error_reason or "")

    def test_a_process_running_before_the_agent_attached_has_its_age(self) -> None:
        """O15, seen live: no 'start' event names a process that was already running when the agent
        attached, and its unknown age counted as stable - two recovery callbacks in the first two
        seconds of a target that dies every six. The container's own record of its start stands in."""
        def stamp(ago: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - ago)) + ".123456789Z"

        for started, stable in ((stamp(5), False), (stamp(120), True),
                                ("0001-01-01T00:00:00Z", True), ("garbage", True)):
            container = _Container("running", 0, "unless-stopped")
            container.attrs["State"]["StartedAt"] = started
            monitor, _ = _monitored(container)
            monitor.inspect()
            self.assertEqual(monitor.forget_restart_history(), stable, started)
        container = _Container("running", 0, "unless-stopped")
        container.attrs["State"]["StartedAt"] = stamp(120)
        monitor, _ = _monitored(container)
        monitor._interpret_event(_event("start", time.time_ns()))        # an event knows better
        monitor.inspect()
        self.assertFalse(monitor.forget_restart_history())

    def test_a_container_stuck_restarting_is_a_restart_loop(self) -> None:
        """Docker's backoff stretches a crash loop until the container sits in 'restarting' with
        a counter that no longer climbs: a restart is transient, being stuck there is not."""
        monitor, _ = _monitored(_Container("restarting", 1, "unless-stopped"))
        self.assertFalse(monitor.inspect().is_crash)               # passing through, as in docker restart
        monitor._restarting_since = time.time() - RESTART_STUCK_S - 1
        state = monitor.inspect()
        self.assertTrue(state.is_crash)
        self.assertIn("restart loop", state.error_reason or "")


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

    def test_one_exit_seen_by_the_poll_and_the_event_stream_is_one_event(self) -> None:
        """O12: without a restart policy (`docker update --restart=no`) the poll sees the exited
        container as well as the event stream's 'die', and the two worded the same exit
        differently: one crash was raised as two exit events."""
        monitor = DockerMonitor(container_name="t")
        container = _Container("exited", 1, "no")
        client = type("Client", (), {})()
        client.containers = type("Containers", (), {"get": lambda _s, name: container})()
        monitor._get_client = lambda: client  # type: ignore[method-assign]
        em = EventManager(docker_monitor=monitor)
        died = monitor._interpret_event(_event("die", 10**18, exit_code="1"))     # the event stream
        assert died is not None
        em.handle_docker_state(died)
        em.handle_docker_state(monitor.inspect())                                   # the poll
        events = em.get_events(event_type="crash")
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["details"]["kind"], events[0]["count"]), ("exit", 2))

    def test_a_target_that_stayed_down_has_its_last_output_streamed(self) -> None:
        """O13: the log stream followed only running or restarting containers. Without a restart
        policy a crashed target stays exited, and its traceback never reached the Observer (seen
        live: the log view ended before the crash, and no log signal was raised)."""
        traceback = (b"2030-01-01T00:00:00.000000001Z Traceback (most recent call last):\n"
                     b"2030-01-01T00:00:00.000000002Z NameError: name 'Flask' is not defined\n")
        container = _Container("exited", 1, "no")
        container.logs = lambda **kw: iter([traceback])  # type: ignore[attr-defined]
        monitor = DockerMonitor(container_name="t")
        client = type("Client", (), {})()
        client.containers = type("Containers", (), {"get": lambda _s, name: container})()
        monitor._get_client = lambda: client  # type: ignore[method-assign]
        stream = monitor.get_log_stream(tail=100)
        assert stream is not None
        found: list = []
        streamer = LogStreamer(monitor, on_error_detected=lambda line, ctx: found.append(line),
                               incident_debounce_seconds=0)
        streamer._agent_start_ts = 1000.0
        for chunk in stream:
            streamer._process_chunk(chunk.decode())
        self.assertIn("NameError: name 'Flask' is not defined", found)
        container.status = "paused"
        self.assertIsNone(monitor.get_log_stream(tail=100))         # nothing new while paused

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

    def test_a_dead_probe_ends_with_the_outage_too(self) -> None:
        """T4: every "target down" kind - exit, restart loop, OOM, dead, unreachable - ends when
        the target serves again; a 5xx on one route is a soft crash and does not."""
        em = EventManager(crash_window_seconds=60.0)
        refused = ProbeResult(url="http://t/", status_code=None, is_crash=True, error_message="refused")
        em.handle_http_crash(refused)
        em.handle_http_crash(ProbeResult(url="http://t/x", status_code=500, is_crash=True))
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        em.handle_probe_round(ok)
        em.handle_probe_round(ok)
        em.handle_http_crash(refused)                       # down again: a new incident
        em.handle_http_crash(ProbeResult(url="http://t/x", status_code=500, is_crash=True))
        kinds = [e["details"]["kind"] for e in em.get_events(event_type="crash")]
        self.assertEqual(kinds.count("unreachable"), 2)
        self.assertEqual(kinds.count("http_5xx"), 1)        # still folded into the first one

    def test_an_oom_kill_is_its_own_signal(self) -> None:
        """T4: the subject's crash signals stay apart in the dashboard (Figure V.1)."""
        em = EventManager()
        event = em.handle_docker_state(ContainerState(
            name="t", status="dead", is_crash=True, error_reason="Container was OOM-killed"))
        assert event is not None
        self.assertEqual(event.details["kind"], "oom")

    def test_a_crash_loop_ended_by_an_outside_restart_is_over(self) -> None:
        """O10, seen live after `make demo-reset`: the restart counter still climbed inside the
        last minute, so the target that served again was a "restart loop" - raised anew on
        every poll once the outage was closed (nine events in 24 s), and a heal started on
        the healthy target."""
        monitor, container = _monitored(_Container("restarting", 1, "unless-stopped"))
        em = EventManager(docker_monitor=monitor)
        for restarts in (1, 2, 3):                                    # the crash loop
            container.attrs["RestartCount"] = restarts
            em.handle_docker_state(monitor.inspect())
        self.assertEqual(len(em.get_events(event_type="crash")), 1)
        container.attrs["State"] = {"Status": "running", "ExitCode": 0}   # reset from outside
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        for _ in range(6):                                            # it serves, polled
            em.handle_docker_state(monitor.inspect())
            em.handle_probe_round(ok)
        self.assertEqual(len(em.get_events(event_type="crash")), 1)
        self.assertFalse(monitor.inspect().is_crash)
        container.attrs["State"] = {"Status": "restarting", "ExitCode": 1}   # planted again
        for restarts in (4, 5):
            container.attrs["RestartCount"] = restarts
            em.handle_docker_state(monitor.inspect())
        self.assertEqual(len(em.get_events(event_type="crash")), 2)   # a new bad start is new

    def test_one_restart_of_a_healthy_target_is_no_restart_loop(self) -> None:
        """O14, seen live: `docker restart` resets the counter, and read as a difference the
        drop from 2 to 0 was the counter climbing - a restart loop on a healthy target that
        had crashed twice long ago, and a heal started on it."""
        monitor, container = _monitored(_Container("running", 0, "unless-stopped"))
        em = EventManager(docker_monitor=monitor)
        container.attrs["RestartCount"] = 2
        em.handle_docker_state(monitor.inspect())
        em.handle_docker_state(monitor.inspect())
        container.attrs["RestartCount"] = 0                      # one plain docker restart
        state = monitor.inspect()
        self.assertFalse(state.is_crash)
        em.handle_docker_state(state)
        self.assertEqual(em.get_events(event_type="crash"), [])

    def test_a_target_that_dies_every_few_seconds_stays_a_restart_loop(self) -> None:
        """O15, seen live: a target serving for six seconds at a time answered the two healthy
        probe rounds between its crashes, so its restarts were forgotten each time and the
        restart loop was never raised - the outage read as "the symptom is gone"."""
        monitor, container = _monitored(_Container("running", 0, "unless-stopped"))
        em = EventManager(docker_monitor=monitor)
        served: list = []
        em.on_target_serving = lambda: served.append(True)
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        for restarts in (1, 2, 3):
            monitor._last_start_ts = time.time()                 # just started again
            container.attrs["RestartCount"] = restarts
            em.handle_docker_state(monitor.inspect())
            em.handle_probe_round(ok)
            em.handle_probe_round(ok)                            # it answers until it dies
        self.assertTrue(monitor.inspect().is_crash)
        kinds = [e["details"]["kind"] for e in em.get_events(event_type="crash")]
        self.assertEqual(kinds, ["restart_loop"])                # still one open incident
        self.assertEqual(served, [])                             # and not recovered

    def test_the_restart_loop_ends_once_the_target_has_stayed_up(self) -> None:
        """O15: the counter is forgotten when the process has lasted, and the incident ends."""
        monitor, container = _monitored(_Container("running", 0, "unless-stopped"))
        em = EventManager(docker_monitor=monitor)
        served: list = []
        em.on_target_serving = lambda: served.append(True)
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        for restarts in (1, 2, 3):
            container.attrs["RestartCount"] = restarts
            em.handle_docker_state(monitor.inspect())
        monitor._last_start_ts = time.time() - STABLE_UPTIME_S - 1
        for _ in range(3):
            em.handle_docker_state(monitor.inspect())
            em.handle_probe_round(ok)
        self.assertFalse(monitor.inspect().is_crash)
        self.assertEqual(len(em.get_events(event_type="crash")), 1)
        self.assertTrue(served)

    def test_a_log_event_carries_its_own_report_not_the_crash_before_it(self) -> None:
        """A11, seen live (break-import, demo-reset, break-env): the KeyError event held the
        NameError traceback above it, and picked in the Analyst tab it was diagnosed as the
        missing import. The request lines stay: the Verifier replays them."""
        em = EventManager()
        tail = "\n".join([
            "Traceback (most recent call last):",
            '  File "/app/app.py", line 21, in <module>',
            "NameError: name 'Flask' is not defined",
            "INFO starting",
            "ERROR in app: Exception on /api/data [GET]",
            "Traceback (most recent call last):",
            '  File "/app/app.py", line 92, in <module>',
            "KeyError: 'IOW_DEMO_PORT'",
            '172.18.0.1 - - "GET /api/data HTTP/1.1" 500 -',
        ])
        event = em.handle_log_error("KeyError: 'IOW_DEMO_PORT'", tail)
        assert event is not None
        context = event.details["context_excerpt"]
        self.assertNotIn("NameError", context)
        self.assertIn("KeyError", context)
        self.assertIn("Exception on /api/data [GET]", context)
        self.assertIn('"GET /api/data HTTP/1.1" 500', context)

    def test_the_report_that_follows_replaces_the_crash_before_it(self) -> None:
        """A11: raised first, an exit event's tail held the previous crash's traceback, which
        kept the new one from being lent to it."""
        streamer = LogStreamer(_NoLogs())
        streamer.log_buffer.append("NameError: name 'Flask' is not defined")
        em = EventManager(log_streamer=streamer)
        exited = em.handle_docker_state(ContainerState(
            name="t", status="exited", exit_code=1, is_crash=True,
            error_reason="Container exited with a non-zero status",
        ))
        assert exited is not None
        self.assertIn("NameError", exited.details["context_excerpt"])
        em.handle_log_error("KeyError: 'IOW_DEMO_PORT'", "KeyError: 'IOW_DEMO_PORT'")
        self.assertEqual(exited.details["context_excerpt"], "KeyError: 'IOW_DEMO_PORT'")

    def test_a_soft_crash_is_lent_only_to_the_5xx_of_its_own_route(self) -> None:
        """A11, by scope, seen live: an unrelated RuntimeError in /api/crash replaced the report of
        the ongoing 5xx on /healthz, and became the diagnosis picked for it. The process survived
        a request that raised, so that report explains neither an exit nor a dead target."""
        def soft_crash(exc: str, route: str) -> str:
            return "\n".join([
                f"[2026-11-01 10:00:00,000] ERROR in app: Exception on {route}",
                "Traceback (most recent call last):",
                '  File "/app/app.py", line 9, in handler',
                exc,
            ])

        healthz = soft_crash("NameError: name 'HEALTH_STATE' is not defined", "/healthz [GET]")
        other = soft_crash("RuntimeError: manual crash", "/api/crash [POST]")
        streamer = LogStreamer(_NoLogs())
        for line in healthz.splitlines():
            streamer.log_buffer.append(line)
        em = EventManager(log_streamer=streamer)
        probe = ProbeResult(url="http://t:5000/healthz", status_code=500, is_crash=True)
        ongoing = em.handle_http_crash(probe)
        assert ongoing is not None
        hard_exit = em.handle_docker_state(ContainerState(
            name="t", status="exited", exit_code=1, is_crash=True,
            error_reason="Container exited with a non-zero status",
        ))
        assert hard_exit is not None
        tail_of_the_exit = hard_exit.details["context_excerpt"]

        em.handle_log_error("RuntimeError: manual crash", other)             # another route
        self.assertIn("NameError", ongoing.details["context_excerpt"])
        self.assertEqual(hard_exit.details["context_excerpt"], tail_of_the_exit)

        ongoing.details["context_excerpt"] = "INFO healthy access log"        # its report not streamed yet
        em.handle_log_error("NameError: name 'HEALTH_STATE' is not defined", healthz)   # its own route
        self.assertIn("NameError", ongoing.details["context_excerpt"])
        self.assertEqual(hard_exit.details["context_excerpt"], tail_of_the_exit)

        # A hard exit prints nothing: its tail (the request that killed it) is its evidence, and
        # no soft crash, however close, is its report.
        request_line = '172.18.0.1 - - "POST /api/crash-hard HTTP/1.1" 202 -'
        quiet = LogStreamer(_NoLogs())
        quiet.log_buffer.append(request_line)
        em2 = EventManager(log_streamer=quiet)
        exited = em2.handle_docker_state(ContainerState(
            name="t", status="exited", exit_code=1, is_crash=True,
            error_reason="Container exited with a non-zero status",
        ))
        assert exited is not None
        em2.handle_log_error("RuntimeError: manual crash", other)
        self.assertEqual(exited.details["context_excerpt"], request_line)
        startup = ("Traceback (most recent call last):\n"
                   '  File "/app/app.py", line 3, in <module>\nKeyError: \'PORT\'')
        em2.handle_log_error("KeyError: 'PORT'", startup)                   # a start-up crash
        self.assertIn("KeyError", exited.details["context_excerpt"])        # that one is its report

    def test_nothing_closes_while_the_container_side_still_counts_a_crash(self) -> None:
        """O16: Kubernetes reports a pod that restarted in place as an exit, not as a restart loop.
        The healthy rounds closed that incident and the next poll raised it again, a new event on
        every poll for as long as the pod had not stayed up."""
        class RestartedInPlace:
            def __init__(self) -> None:
                self.state = ContainerState(
                    name="t", status="running", is_crash=True, exit_code=1, restart_count=1,
                    error_reason="Pod container terminated with exit code 1 (Error)",
                )
                self.stayed_up = False

            def get_last_state(self) -> ContainerState:
                return self.state

            def forget_restart_history(self) -> bool:
                return self.stayed_up

        pod = RestartedInPlace()
        em = EventManager(docker_monitor=pod)                    # type: ignore[arg-type]
        served: List[bool] = []
        em.on_target_serving = lambda: served.append(True)
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        crashed = pod.state
        for _ in range(4):                                       # polls and probe rounds, serving
            em.handle_docker_state(crashed)
            em.handle_probe_round(ok)
        self.assertEqual(len(em.get_events(event_type="crash")), 1)
        self.assertEqual(served, [])
        pod.stayed_up = True                                     # and now it has stayed up
        pod.state = ContainerState(name="t", status="running")
        em.handle_probe_round(ok)
        em.handle_probe_round(ok)
        self.assertTrue(served)
        em.handle_docker_state(crashed)                          # a crash after that is a new one
        self.assertEqual(len(em.get_events(event_type="crash")), 2)

    def test_a_container_exit_gets_the_traceback_that_follows_it(self) -> None:
        """O11, seen live: a crash's exit is raised before its traceback is streamed (the stream
        re-attaches only after the restart). The exit event kept the healthy tail before it, and
        picked in the Analyst tab it was diagnosed from access logs (a missing import blamed on
        get_data)."""
        streamer = LogStreamer(_NoLogs())
        streamer.log_buffer.append('172.18.0.1 - - "GET /healthz HTTP/1.1" 200 -')
        em = EventManager(log_streamer=streamer)
        exited = em.handle_docker_state(ContainerState(
            name="t", status="exited", exit_code=1, is_crash=True,
            error_reason="Container exited with a non-zero status",
        ))
        refused = em.handle_http_crash(ProbeResult(url="http://t/", is_crash=True, error_message="refused"))
        earlier = em.handle_http_crash(ProbeResult(url="http://t/x", status_code=500, is_crash=True))
        assert exited is not None and refused is not None and earlier is not None
        earlier.last_seen -= 60                                  # an earlier, unrelated incident
        traceback = (
            "Traceback (most recent call last):\n"
            '  File "/app/app.py", line 11, in <module>\n'
            "NameError: name 'Flask' is not defined"
        )
        em.handle_log_error("NameError: name 'Flask' is not defined", traceback)
        self.assertEqual(exited.details["context_excerpt"], traceback)
        self.assertEqual(refused.details["context_excerpt"], traceback)
        self.assertNotIn("NameError", earlier.details["context_excerpt"])

    def test_suggestions_are_folded_on_their_own_longer_window(self) -> None:
        """T4: subject V.1, "on a longer window". Past the crash window but inside its own, the
        same missing route stays one event; past its own it is a new one."""
        em = EventManager(crash_window_seconds=0.05, suggestion_window_seconds=0.4)
        missing = ProbeResult(url="http://t/x", status_code=404, is_suggestion=True)
        first = em.handle_http_suggestion(missing)
        time.sleep(0.1)
        again = em.handle_http_suggestion(missing)
        assert first is not None and again is not None
        self.assertIs(first, again)
        self.assertEqual(first.count, 2)
        time.sleep(0.5)
        self.assertIsNot(em.handle_http_suggestion(missing), first)

    def test_the_signature_table_is_bounded(self) -> None:
        """T4: a burst of unique signatures cannot grow the table without limit, oldest first
        (a long defense session must not grow the agent); the history keeps every event."""
        em = EventManager()
        with mock.patch.object(event_manager, "MAX_ACTIVE_SIGNATURES", 5):
            for i in range(20):
                em.handle_log_error(f"ValueError: {chr(97 + i) * 3}", "")
        events = em.get_events(event_type="crash")
        self.assertEqual(len(events), 20)
        self.assertLessEqual(len(em._active_signatures), 5 + 1)   # trimmed before each insert
        self.assertIn(events[-1]["signature"], em._active_signatures)
        self.assertNotIn(events[0]["signature"], em._active_signatures)

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

    def test_the_exception_line_is_the_headline_of_a_burst(self) -> None:
        """T4: one traceback arrives as a burst of matching lines; the event is named after the
        most diagnostic one, the exception line, not whichever matched last."""
        self.assertEqual(
            LogStreamer._pick_headline([
                "Traceback (most recent call last):", "ValueError: boom", "CRITICAL: shutting down",
            ]),
            "ValueError: boom",
        )
        self.assertEqual(LogStreamer._pick_headline(["CRITICAL: a", "CRITICAL: b"]), "CRITICAL: b")

    def test_error_lines_during_a_restart_window_raise_nothing(self) -> None:
        """T4: around our own restart the old process's tracebacks are not a crash; the lines
        stay in the log view, and detection comes back with the window."""
        sink: list = []
        streamer = self._streamer(sink)
        streamer.suppress_events_for(30)
        streamer._process_chunk("2030-01-01T00:00:00.000000000Z RuntimeError: while restarting\n")
        self.assertEqual(sink, [])
        self.assertIn("RuntimeError: while restarting", streamer.get_recent_logs())
        streamer.resume_events()
        streamer._process_chunk("2030-01-01T00:00:01.000000000Z RuntimeError: after the restart\n")
        self.assertEqual(sink, ["RuntimeError: after the restart"])

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


class _Target(http.server.BaseHTTPRequestHandler):
    """A real HTTP target: /status/<code> answers that code, /slow outlasts the probe's timeout."""

    def do_GET(self) -> None:
        if self.path == "/slow":
            time.sleep(1.0)
            code = 200
        else:
            code = int(self.path.rsplit("/", 1)[1])
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args: Any) -> None:
        pass


class ProbeClassificationTests(unittest.TestCase):
    """T4: the subject's split between hard and soft signals, over real connections. It had no
    test: a mutation run turned each of these around and every suite stayed green."""

    server: http.server.ThreadingHTTPServer
    url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Target)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _probe(self, path: str, service_url: str = "", timeout: float = 2.0) -> ProbeResult:
        probe = HttpProbeManager(service_url=service_url or self.url, timeout_seconds=timeout)
        return probe._probe_single_url(f"{service_url or self.url}{path}")

    def test_a_2xx_is_healthy(self) -> None:
        result = self._probe("/status/200")
        self.assertEqual((result.is_healthy, result.is_crash, result.is_suggestion), (True, False, False))

    def test_a_4xx_is_a_suggestion_and_never_a_crash(self) -> None:
        for code in (400, 404, 405, 499):
            result = self._probe(f"/status/{code}")
            self.assertEqual((result.is_healthy, result.is_crash, result.is_suggestion),
                             (False, False, True), code)

    def test_a_5xx_is_a_crash(self) -> None:
        for code in (500, 502, 503):
            result = self._probe(f"/status/{code}")
            self.assertEqual((result.is_healthy, result.is_crash, result.is_suggestion),
                             (False, True, False), code)

    def test_a_redirect_is_neither(self) -> None:
        result = self._probe("/status/302")
        self.assertEqual((result.is_healthy, result.is_crash, result.is_suggestion), (False, False, False))

    def test_a_refused_connection_is_a_crash(self) -> None:
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        result = self._probe("/", service_url=f"http://127.0.0.1:{port}")
        self.assertEqual((result.is_crash, result.status_code), (True, None))
        self.assertIn("refused", result.error_message or "")

    def test_a_timeout_is_a_crash(self) -> None:
        result = self._probe("/slow", timeout=0.2)
        self.assertEqual((result.is_crash, result.status_code), (True, None))
        self.assertIn("timed out", result.error_message or "")

    def test_no_event_is_raised_while_a_restart_is_in_flight(self) -> None:
        """T4: the target is expected to be unreachable around a deliberate restart: the round
        is still probed and reported, but no crash event comes of it, until the window ends."""
        crashes: List[ProbeResult] = []
        rounds: List[List[ProbeResult]] = []
        probe = HttpProbeManager(service_url=self.url, probe_paths=["/status/500"], on_crash=crashes.append)
        probe.on_round = rounds.append
        probe.suppress_events_for(30)
        probe.probe_all_once()
        self.assertEqual((len(crashes), len(rounds)), (0, 1))
        self.assertEqual(probe.get_latest_results()[f"{self.url}/status/500"]["status_code"], 500)
        probe.resume_events()
        probe.probe_all_once()
        self.assertEqual(len(crashes), 1)

    def test_each_class_reaches_its_own_callback(self) -> None:
        crashes: List[ProbeResult] = []
        suggestions: List[ProbeResult] = []
        probe = HttpProbeManager(service_url=self.url,
                                 probe_paths=["/status/200", "/status/404", "/status/500"],
                                 on_crash=crashes.append, on_suggestion=suggestions.append)
        probe.probe_all_once()
        self.assertEqual([r.status_code for r in crashes], [500])
        self.assertEqual([r.status_code for r in suggestions], [404])


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
