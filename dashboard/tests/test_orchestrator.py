"""The orchestrator that joins the parts: which crash it heals, how a review's outcome
reaches the heal history, GitOps wiring, and the routes the dashboard serves."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import types
import unittest
from typing import Any
from unittest import mock

from p1.docker_monitor import ContainerState
from p1.event_manager import ObserverEvent
from p1.http_probe import ProbeResult
from p3.tests.helpers import TempRepo

# dashboard.app builds its orchestrator at import: every path it touches points at a
# throwaway place first - no real repository, index, store or container.
_REPO = TempRepo()
_RUNTIME = tempfile.mkdtemp(prefix="iow-dashboard-")
os.environ.update({
    "IOW_MODE": "full",
    "IOW_REDEPLOY_MODE": "docker",
    "IOW_REPO_PATH": _REPO.path,
    "IOW_TARGET_DIR": os.path.join(_REPO.path, "demo_app"),
    "IOW_CONFIG_PATH": os.path.join(_REPO.path, "demo_app", "iow.config.yml"),
    "CHROMA_CACHE_DIR": os.path.join(_RUNTIME, "chroma"),
    "IOW_CLASSIFIER_STORE": os.path.join(_RUNTIME, "classifier.json"),
    "IOW_PR_STORE": os.path.join(_RUNTIME, "prs.json"),
    "GITEA_CREDS_FILE": os.path.join(_RUNTIME, "none.env"),
    "TARGET_CONTAINER": "iow-test-no-such-container",
    "TARGET_URL": "http://127.0.0.1:9",
})
from dashboard import app as dashboard  # noqa: E402


def tearDownModule() -> None:
    _REPO.cleanup()
    shutil.rmtree(_RUNTIME, ignore_errors=True)


def _crash(signature: str = "sig-1") -> ObserverEvent:
    now = time.time()
    return ObserverEvent(id="e1", type="crash", source="http_probe", signature=signature,
                         summary="Target unreachable", details={}, first_seen=now, last_seen=now)


class HealTriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.orch = dashboard.orchestrator
        self.orch.safety_manager = type(self.orch.safety_manager)(self.orch.config_path)

    def _event_with(self, state: ContainerState) -> Any:
        with mock.patch.object(self.orch.docker_monitor, "inspect", return_value=state), \
                mock.patch.object(dashboard.threading, "Thread") as thread:
            self.orch._on_observer_event(_crash())
        return thread

    def test_a_target_no_patch_can_fix_takes_no_heal(self) -> None:
        """L11 and the stopped target: removed, daemon down, pod never started, or stopped /
        paused on purpose (seen live: after a `docker stop` the healthcheck was rewritten)."""
        for state in (
            ContainerState(name="t", status="not_found"),
            ContainerState(name="t", status="daemon_unavailable"),
            ContainerState(name="t", status="not_started"),
            ContainerState(name="t", status="exited", exit_code=137, stopped=True),
            ContainerState(name="t", status="paused", stopped=True),
        ):
            self._event_with(state).assert_not_called()
        self.assertEqual(self.orch.safety_manager.get_status()["lifetime_heal_count"], 0)

    def test_a_suggestion_never_starts_a_heal(self) -> None:
        """T4: subject V.1, a 4xx is "never patched on its own". The refusal of the API had a
        test, the autonomous path had none: a mutation run removed the check and stayed green."""
        now = time.time()
        suggestion = ObserverEvent(id="s1", type="suggestion", source="http_probe", signature="sig-4xx",
                                   summary="HTTP 404", details={}, first_seen=now, last_seen=now)
        crashed = ContainerState(name="t", status="running")
        with mock.patch.object(self.orch.docker_monitor, "inspect", return_value=crashed), \
                mock.patch.object(dashboard.threading, "Thread") as thread:
            self.orch._on_observer_event(suggestion)
        thread.assert_not_called()
        self.assertEqual(self.orch.safety_manager.get_status()["lifetime_heal_count"], 0)

    def test_a_refused_slot_starts_no_heal(self) -> None:
        """T4: subject V.3, "one heal at a time": a crash raised while a heal runs is refused by
        the bounds before any thread starts (a mutation run let it start one and every suite
        stayed green)."""
        self.assertTrue(self.orch.safety_manager.acquire_heal_slot("the-running-heal")[0])
        try:
            thread = self._event_with(ContainerState(name="t", status="restarting", is_crash=True))
        finally:
            self.orch.safety_manager.release_heal_slot("the-running-heal")
        thread.assert_not_called()

    def test_a_crashed_target_is_healed(self) -> None:
        thread = self._event_with(ContainerState(name="t", status="restarting", is_crash=True))
        thread.assert_called_once()
        self.assertEqual(self.orch.safety_manager.get_status()["lifetime_heal_count"], 1)
        self.orch.safety_manager.release_heal_slot("sig-1")


class HealBudgetTests(unittest.TestCase):
    """What a heal costs: only a heal that ran counts, and a failed one holds its outage."""

    def setUp(self) -> None:
        self.orch = dashboard.InceptionOrchestrator(config_path=dashboard.orchestrator.config_path)

    def _heal(self, signature: str, present: Any, result: Any = None) -> Any:
        allowed, _ = self.orch.safety_manager.acquire_heal_slot(signature)
        self.assertTrue(allowed)
        running = ContainerState(name="t", status="running")
        with mock.patch.object(self.orch.docker_monitor, "inspect", return_value=running), \
                mock.patch.object(self.orch.verifier, "symptom_present", return_value=present), \
                mock.patch.object(self.orch.wisdom_loop, "execute_heal", return_value=result) as heal:
            self.orch.run_heal_cycle(_logged_crash(signature), grace_period=0.0)
        self.orch.safety_manager.release_heal_slot(signature)
        return heal

    def test_a_crash_already_gone_spends_nothing(self) -> None:
        """L22, seen live: heals of old events and a stray error line read 5/5 after two real
        heals, and a real crash was refused."""
        heal = self._heal("sig-gone", (False, "every probe answers"))
        heal.assert_not_called()
        status = self.orch.safety_manager.get_status()
        self.assertEqual((status["hourly_heal_count"], status["lifetime_heal_count"]), (0, 0))
        self.assertEqual(status["active_signatures_in_cooldown"], 0)

    def test_a_verified_heal_ends_its_incident(self) -> None:
        """T4: the same crash coming back after a verified heal is a new one, not a repeat the
        old incident's window folds away."""
        with mock.patch.object(self.orch.event_manager, "close_incidents") as close:
            self._heal("sig-fixed", (True, "failing"), mock.Mock(status="healed", attempts=[]))
        close.assert_called_once_with("crash")

    def test_a_heal_that_ran_is_counted(self) -> None:
        self._heal("sig-run", (True, "failing"), mock.Mock(status="healed", attempts=[]))
        self.assertEqual(self.orch.safety_manager.get_status()["lifetime_heal_count"], 1)

    def test_a_failed_heal_holds_the_outage_until_the_target_recovers(self) -> None:
        """L23, seen live on the defense's own rollback demo: one planted crash with the model
        stopped, two full cycles six seconds apart, the second through a sibling signature."""
        self._heal("sig-exit", (True, "failing"), mock.Mock(status="rolled_back", attempts=[]))
        allowed, reason = self.orch.safety_manager.acquire_heal_slot("sig-restart-loop")
        self.assertFalse(allowed)
        self.assertIn("outage the last heal failed on", reason or "")
        ok = [ProbeResult(url="http://t/", status_code=200, is_healthy=True)]
        self.orch.event_manager.handle_probe_round(ok)
        self.orch.event_manager.handle_probe_round(ok)         # the Observer: it serves again
        self.assertTrue(self.orch.safety_manager.acquire_heal_slot("sig-new")[0])
        self.orch.safety_manager.release_heal_slot("sig-new")

    def test_analyst_model_in_the_file_is_the_model(self) -> None:
        """M11: compose always set OLLAMA_MODEL, so `analyst.model` was a dead key under
        `make up`. An empty override is no override; a set one still wins."""
        path = self.orch.config_path
        self.addCleanup(_REPO.write, "demo_app/iow.config.yml", _REPO.read("demo_app/iow.config.yml"))
        with open(path, "a", encoding="utf-8") as f:
            f.write('analyst:\n  model: "tiny-test:1b"\n')
        with mock.patch.dict(os.environ, {"OLLAMA_MODEL": ""}):
            self.assertEqual(dashboard.InceptionOrchestrator(path).diagnostician.model_name, "tiny-test:1b")
        with mock.patch.dict(os.environ, {"OLLAMA_MODEL": "other:1b"}):
            self.assertEqual(dashboard.InceptionOrchestrator(path).diagnostician.model_name, "other:1b")


class ReviewOutcomeTests(unittest.TestCase):
    """L16: an approved, verified review stayed 'awaiting_review' in the heal history."""

    def _decide(self, status: str, landing: Any = None) -> Any:
        orch = dashboard.orchestrator
        record = orch.wisdom_loop.record_refused_cycle(
            _crash(), "sent for review", status="awaiting_review", review_pr=f"pr-{status}",
            pre_loop_hash="base1234",
        )
        pr = types.SimpleNamespace(status=status, landing=landing, base_revision="base1234",
                                   reviewer_comment="not like this")
        with mock.patch.object(orch.pr_manager, "get_pr", return_value=pr):
            orch._resolve_review(f"pr-{status}")
        return record

    def test_approved_rejected_and_reverted_reviews(self) -> None:
        self.assertEqual(self._decide("merged", {"healed": True}).status, "healed")
        rejected = self._decide("rejected")
        self.assertEqual(rejected.status, "refused")
        self.assertIn("not like this", rejected.failure_reason)
        self.assertEqual(self._decide("reverted", {"status": "crashed"}).status, "rolled_back")


_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/app/app.py", line 3, in <module>\n'
    "NameError: name 'Flask' is not defined"
)


def _logged_crash(signature: str = "sig-log") -> ObserverEvent:
    now = time.time()
    return ObserverEvent(id="e2", type="crash", source="log", signature=signature,
                         summary="NameError", details={"context_excerpt": _TRACEBACK},
                         first_seen=now, last_seen=now)


class ReviewPathTests(unittest.TestCase):
    """Bonus 1 under review: the same checks as the autonomous loop, and bonus 3 still holds."""

    def setUp(self) -> None:
        self.orch = dashboard.orchestrator
        flags = mock.patch.dict(dashboard.bonus_flags, {
            "human_in_the_loop": True, "second_opinion_mandatory": False, "fast_path_classifier": True,
        })
        flags.start()
        self.addCleanup(flags.stop)

    def _run(self, state: ContainerState, present: Any) -> Any:
        with mock.patch.object(self.orch.docker_monitor, "inspect", return_value=state), \
                mock.patch.object(self.orch.verifier, "symptom_present", return_value=present) as check, \
                mock.patch.object(self.orch.diagnostician, "diagnose_crash") as diagnose, \
                mock.patch.object(self.orch, "_propose_hitl_patch", return_value=None) as propose:
            diagnose.return_value = mock.Mock(success=False, error_message="(stub)")
            self.orch.run_heal_cycle(_logged_crash(), grace_period=0.0)
        return types.SimpleNamespace(check=check, diagnose=diagnose, propose=propose,
                                     record=self.orch.wisdom_loop.history[-1])

    def test_nothing_to_patch_gets_no_review(self) -> None:
        """B17, seen live: with review on, a heal of an old event of a crash already fixed went
        to the model, and Gitea #14 proposed "exception handling" for the healthy get_data."""
        gone = self._run(ContainerState(name="t", status="running"), (False, "every probe answers"))
        self.assertEqual((gone.diagnose.called, gone.propose.called), (False, False))
        self.assertEqual(gone.record.status, "not_needed")
        stopped = self._run(ContainerState(name="t", status="exited", exit_code=0, stopped=True,
                                           error_reason="Stopped (exit code 0)"), (True, "exited"))
        self.assertEqual((stopped.diagnose.called, stopped.propose.called), (False, False))
        self.assertIn("Nothing to patch", stopped.record.failure_reason or "")
        crashed = self._run(ContainerState(name="t", status="restarting", is_crash=True),
                            (True, "still failing"))
        self.assertTrue(crashed.diagnose.called)                     # a real crash still goes on
        self.assertEqual(crashed.check.call_count, 1)                # asked once, not per gate

    def test_a_known_symptom_is_proposed_without_the_model(self) -> None:
        """B20: with review on, a crash the classifier knows on exactly this code still went to
        the model - while the classifier counted a bypass."""
        current = _REPO.read("demo_app/app.py")
        fixed = current + "\n# restored\n"
        key = self.orch.classifier.record_successful_heal(
            _TRACEBACK, {"summary": "restore the import",
                         "files": [{"path": "demo_app/app.py", "op": "modify", "content": fixed}]},
            pre_contents={"demo_app/app.py": current},
        )
        self.addCleanup(self.orch.classifier.entries.pop, key, None)    # the orchestrator is shared
        known = self._run(ContainerState(name="t", status="restarting", is_crash=True), (True, "failing"))
        self.assertFalse(known.diagnose.called)
        known.propose.assert_called_once()
        files = known.propose.call_args.args[3]
        self.assertEqual(files, [{"path": "demo_app/app.py", "op": "modify", "content": fixed}])
        self.assertIn("model was not asked", known.propose.call_args.args[2])

    def test_no_vote_is_not_routed_to_review(self) -> None:
        """B19: with the model down the second opinion "diverged", and the heal was withheld for
        review; there is no vote, and the loop reports the explicit failure on every attempt."""
        from bonus.consensus import ConsensusReport
        no_vote = ConsensusReport(
            consensus_reached=False, agreed_files=[], confidence=0.0,
            consensus_summary="No vote: neither prompt produced a usable diagnosis",
            opinion_a={"mode": "unavailable"}, opinion_b={"mode": "unavailable"}, diverged=False,
        )
        crashed = ContainerState(name="t", status="restarting", is_crash=True)
        vote_only = {"human_in_the_loop": False, "second_opinion_mandatory": True}
        with mock.patch.dict(dashboard.bonus_flags, vote_only), \
                mock.patch.object(self.orch.docker_monitor, "inspect", return_value=crashed), \
                mock.patch.object(self.orch.verifier, "symptom_present", return_value=(True, "failing")), \
                mock.patch.object(self.orch.consensus_engine, "evaluate_consensus", return_value=no_vote), \
                mock.patch.object(self.orch, "_propose_hitl_patch") as propose, \
                mock.patch.object(self.orch.wisdom_loop, "execute_heal") as heal:
            heal.return_value = mock.Mock(status="rolled_back", attempts=[])
            self.orch.run_heal_cycle(_logged_crash("sig-vote"), grace_period=0.0)
        propose.assert_not_called()
        self.assertIsNotNone(heal.call_args.kwargs["diagnose_fn"])
        self.assertFalse(heal.call_args.kwargs["check_first"])
        diagnosis = dashboard.InceptionOrchestrator._diagnosis_from_consensus(no_vote)
        self.assertEqual((diagnosis.success, diagnosis.mode), (False, "unavailable"))


class GitOpsWiringTests(unittest.TestCase):
    def test_gitops_mode_stamps_commits_and_keeps_one_crash_loop_one_event(self) -> None:
        """B2 / G2: commits carry the source stamp; Kubernetes backs off up to 300 s."""
        with mock.patch.dict(os.environ, {"IOW_REDEPLOY_MODE": "gitops"}):
            orch = dashboard.InceptionOrchestrator(config_path=dashboard.orchestrator.config_path)
        self.assertEqual(orch.git_manager.manifest_stamp, "k8s/demo-app/deployment.yaml")
        self.assertGreater(orch.event_manager.crash_window_seconds, 300)
        self.assertIsNone(dashboard.orchestrator.git_manager.manifest_stamp)   # docker mode: none


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi.testclient import TestClient
        assert dashboard.app is not None
        self.client = TestClient(dashboard.app)       # no lifespan: nothing starts

    def test_the_page_and_its_favicon(self) -> None:
        """The favicon 404 was the last console error of the page."""
        self.assertEqual(self.client.get("/favicon.ico").status_code, 204)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn('window.IOW_MODE = "full"', page.text)

    def test_auto_heal_cannot_be_switched_on_before_part_3(self) -> None:
        """L11: in p1 / p2 it burned safety slots on heals that were locked anyway."""
        with mock.patch.object(dashboard, "LOOP_ENABLED", False):
            self.assertEqual(self.client.post("/api/auto-heal/toggle?enable=true").status_code, 409)

    def test_a_heal_with_nothing_to_heal_is_refused(self) -> None:
        """R2: a trigger with no crash and no log made the model rewrite working code."""
        self.assertEqual(self.client.post("/api/loop/trigger", json={}).status_code, 409)


if __name__ == "__main__":
    unittest.main()
