"""Part 3 - the Wisdom Loop API: what a manual trigger or rollback must refuse."""

from __future__ import annotations

import unittest
from typing import List

from fastapi import FastAPI
from fastapi.testclient import TestClient

from p1.event_manager import EventManager, ObserverEvent
from p3.git_manager import GitManager
from p3.loop import WisdomLoop
from p3.loop_api import create_loop_router
from p3.sanity import SanityChecker
from p3.tests.helpers import TempRepo
from p3.tests.test_loop import FakeDiagnostician, FakeDocker, FakePatcher, FakeVerifier


class LoopApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        gm = GitManager(repo_dir=self.repo.path)
        self.loop = WisdomLoop(
            git_manager=gm,
            diagnostician=FakeDiagnostician(),  # type: ignore[arg-type]
            patcher=FakePatcher([]),  # type: ignore[arg-type]
            sanity_checker=SanityChecker(base_dir=self.repo.path, path_guard=gm._is_heal_path),
            verifier=FakeVerifier([True]),  # type: ignore[arg-type]
            docker_monitor=FakeDocker(),  # type: ignore[arg-type]
        )
        self.events = EventManager()
        self.heals: List[ObserverEvent] = []
        app = FastAPI()
        app.include_router(create_loop_router(
            wisdom_loop=self.loop,
            event_manager=self.events,
            heal_executor=lambda event, grace: self.heals.append(event),
        ))
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_nothing_to_heal_is_refused(self) -> None:
        """No crash, no log: the model would be asked to change healthy code."""
        answer = self.client.post("/api/loop/trigger", json={"run_async": False})
        self.assertEqual(answer.status_code, 409)
        self.assertIn("nothing to heal", answer.json()["detail"])
        self.assertEqual(self.heals, [])

    def test_a_suggestion_is_never_patched(self) -> None:
        event = self.events.emit_event("suggestion", "http_probe", "k", "HTTP 404 on /x", {})
        assert event is not None
        answer = self.client.post("/api/loop/trigger", json={"signature": event.signature})
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(self.heals, [])

    def test_a_picked_crash_is_healed_with_its_own_context(self) -> None:
        event = self.events.emit_event(
            "crash", "log", "k2", "NameError", {"context_excerpt": "NameError: name 'x'"}
        )
        assert event is not None
        answer = self.client.post(
            "/api/loop/trigger", json={"signature": event.signature, "run_async": False}
        )
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(len(self.heals), 1)
        self.assertEqual(self.heals[0].details["context_excerpt"], "NameError: name 'x'")

    def test_a_rollback_waits_for_the_running_cycle_and_holds_the_slot(self) -> None:
        self.assertTrue(self.loop.claim())                 # a heal cycle is running
        self.assertEqual(self.client.post("/api/loop/rollback", json={}).status_code, 409)
        self.loop.release()
        # Nothing to roll back yet - and the slot is given back either way.
        self.assertEqual(self.client.post("/api/loop/rollback", json={}).status_code, 400)
        self.assertFalse(self.loop.is_active())


if __name__ == "__main__":
    unittest.main()
