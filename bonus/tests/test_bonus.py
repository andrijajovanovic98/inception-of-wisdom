"""Bonus - classifier fast path, second-opinion vote, human-in-the-loop PRs, GitOps.

No model, no Gitea and no cluster: the classifier is pure, the two opinions are
scripted, the PR manager runs against a throwaway repository with no forge configured,
and the GitOps checks read pod JSON and a stubbed CLI.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from bonus.classifier import TinySymptomClassifier
from bonus.consensus import SecondOpinionEngine
from bonus.pr_manager import PullRequestManager
from p3.git_manager import GitManager
from p3.patcher import StructuredPatch
from p3.tests.helpers import TempRepo

CRASH = (
    "Traceback (most recent call last):\n"
    '  File "/app/app.py", line 78, in trigger_crash\n'
    '    raise RuntimeError("boom at 0x7f00aa")\n'
    "RuntimeError: boom at 0x7f00aa\n"
)
PATCH = {
    "summary": "stop raising in trigger_crash",
    "files": [{"path": "demo_app/app.py", "op": "modify", "content": "print('fixed')\n"}],
}


class ClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="iow-clf-")
        self.store = os.path.join(self.dir, "store.json")

    def tearDown(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_known_symptom_reuses_its_verified_patch(self) -> None:
        TinySymptomClassifier(self.store).record_successful_heal(CRASH, PATCH)
        # A restarted agent, and the same crash with a different address and line.
        reloaded = TinySymptomClassifier(self.store)
        match = reloaded.classify(CRASH.replace("0x7f00aa", "0x55ffee").replace("line 78", "line 80"))
        self.assertTrue(match.matched)
        self.assertEqual(match.cached_patch, PATCH)
        patch = StructuredPatch.from_dict(match.cached_patch or {})
        self.assertTrue(patch.success)
        self.assertEqual(patch.files[0].path, "demo_app/app.py")
        self.assertEqual(reloaded.get_stats()["total_fast_path_bypasses"], 1)

    def test_unknown_symptom_goes_to_the_model(self) -> None:
        clf = TinySymptomClassifier(self.store)
        clf.record_successful_heal(CRASH, PATCH)
        other = (
            "Traceback (most recent call last):\n"
            '  File "/app/config.py", line 3, in <module>\n'
            "KeyError: 'PORT'\n"
        )
        self.assertFalse(clf.classify(other).matched)

    def test_the_symptom_is_keyed_on_the_application_frame_not_the_stdlib(self) -> None:
        """Seen live: a KeyError was learned as '<frozen os>:__getitem__'."""
        log = (
            "Traceback (most recent call last):\n"
            '  File "/app/app.py", line 92, in <module>\n'
            '  File "<frozen os>", line 679, in __getitem__\n'
            "KeyError: 'IOW_DEMO_PORT'\n"
        )
        features = TinySymptomClassifier(self.store).extract_features(log)
        self.assertEqual(features.offending_file, "/app/app.py")

    def test_a_cached_patch_that_fails_loses_trust(self) -> None:
        clf = TinySymptomClassifier(self.store)
        key = clf.record_successful_heal(CRASH, PATCH)
        clf.record_failure(key)
        self.assertLess(clf.entries[key]["confidence"], 1.0)
        clf.record_failure(key)
        clf.record_failure(key)
        self.assertNotIn(key, clf.entries)          # evicted: back to the LLM
        self.assertFalse(clf.classify(CRASH).matched)

    def test_a_failed_fast_path_is_penalised_although_it_is_not_the_last_attempt(self) -> None:
        """The dashboard only looked at the last of three attempts - the cached patch is
        attempt 1, so no entry was ever penalised and none was ever evicted."""
        clf = TinySymptomClassifier(self.store)
        key = clf.record_successful_heal(CRASH, PATCH, pre_contents={"demo_app/app.py": "broken\n"})
        cycle = _Cycle("rolled_back", [
            _Attempt("classifier_fast_path", healed=False),
            _Attempt("llm", healed=False),
            _Attempt("llm", healed=False),
        ])
        clf.learn_from_cycle(cycle, CRASH, None, used_symptom=key, pre_loop_file=lambda p: None)
        self.assertEqual(clf.entries[key]["confidence"], 0.75)

    def test_the_entry_that_supplied_the_patch_is_the_one_penalised(self) -> None:
        """A fuzzy match: the matched entry's key is not the hash of the new crash."""
        clf = TinySymptomClassifier(self.store)
        key = clf.record_successful_heal(CRASH, PATCH, pre_contents={"demo_app/app.py": "broken\n"})
        other_log = CRASH.replace("boom at", "kaboom near")
        cycle = _Cycle("rolled_back", [_Attempt("classifier_fast_path", healed=False)])
        clf.learn_from_cycle(cycle, other_log, None, used_symptom=key, pre_loop_file=lambda p: None)
        self.assertLess(clf.entries[key]["confidence"], 1.0)

    def test_a_cached_patch_only_replaces_the_code_it_was_learned_on(self) -> None:
        """A cached patch is complete files: replayed onto code that changed elsewhere it
        would silently revert that change."""
        clf = TinySymptomClassifier(self.store)
        clf.record_successful_heal(CRASH, PATCH, pre_contents={"demo_app/app.py": "broken\n"})
        same = clf.classify(CRASH, read_file=lambda p: "broken\n")
        self.assertTrue(same.matched)
        edited = clf.classify(CRASH, read_file=lambda p: "broken\nEDITED = 1\n")
        self.assertFalse(edited.matched)
        self.assertIn("differs", edited.explanation or "")
        # An entry learned without that record cannot prove it fits.
        legacy = TinySymptomClassifier(os.path.join(self.dir, "legacy.json"))
        legacy.record_successful_heal(CRASH, PATCH)
        self.assertFalse(legacy.classify(CRASH, read_file=lambda p: "broken\n").matched)

    def test_a_verified_heal_is_learned_with_its_pre_loop_code(self) -> None:
        clf = TinySymptomClassifier(self.store)
        cycle = _Cycle("healed", [_Attempt("llm", healed=True, patch=PATCH)])
        clf.learn_from_cycle(cycle, CRASH, None, used_symptom=None,
                             pre_loop_file=lambda p: "broken\n" if p == "demo_app/app.py" else None)
        self.assertTrue(clf.classify(CRASH, read_file=lambda p: "broken\n").matched)

    def test_a_new_verified_patch_starts_with_full_trust(self) -> None:
        clf = TinySymptomClassifier(self.store)
        key = clf.record_successful_heal(CRASH, PATCH)
        clf.record_failure(key)
        better = {"summary": "s", "files": [{"path": "demo_app/app.py", "op": "modify", "content": "ok\n"}]}
        clf.record_successful_heal(CRASH, better)
        self.assertEqual(clf.entries[key]["confidence"], 1.0)
        self.assertEqual(clf.entries[key]["patch"], better)


class _Attempt:
    def __init__(self, source: str, healed: bool, patch: Optional[Dict[str, Any]] = None) -> None:
        self.source = source
        self.verification = {"is_healed": healed}
        self.patch = patch


class _Cycle:
    def __init__(self, status: str, attempts: List[_Attempt]) -> None:
        self.status = status
        self.attempts = attempts


class _Retriever:
    def retrieve_for_crash(self, log: str, top_k: int = 3) -> Tuple[List[Dict[str, Any]], Dict]:
        chunks = [
            {"file_path": "demo_app/app.py", "start_line": 72, "end_line": 78,
             "content": "def trigger_crash(): ..."},
            {"file_path": "demo_app/config.py", "start_line": 1, "end_line": 9, "content": "PORT = ..."},
        ]
        return chunks, {}


class ScriptedOpinions(SecondOpinionEngine):
    def __init__(self, answers: List[Dict[str, Any]]) -> None:
        super().__init__(retriever=_Retriever())  # type: ignore[arg-type]
        self.answers = list(answers)

    def _query_llm_prompt(self, prompt: str, system_prompt: str, temperature: float = 0.0) -> Optional[str]:
        return json.dumps(self.answers.pop(0))


class SecondOpinionTests(unittest.TestCase):
    def test_agreement_triggers_the_patch(self) -> None:
        engine = ScriptedOpinions([
            {"summary": "trigger_crash raises on purpose.", "files": ["demo_app/app.py"]},
            {"summary": "The route has no error contract.", "files": ["app.py"]},
        ])
        report = engine.evaluate_consensus(CRASH)
        self.assertTrue(report.consensus_reached)
        self.assertEqual(report.agreed_files, ["demo_app/app.py"])

    def test_divergence_blocks_the_patch(self) -> None:
        engine = ScriptedOpinions([
            {"summary": "trigger_crash raises.", "files": ["demo_app/app.py"]},
            {"summary": "The port config is wrong.", "files": ["demo_app/config.py"]},
        ])
        report = engine.evaluate_consensus(CRASH)
        self.assertFalse(report.consensus_reached)
        self.assertEqual(report.agreed_files, [])

    def test_an_opinion_citing_nothing_real_cannot_agree(self) -> None:
        engine = ScriptedOpinions([
            {"summary": "x", "files": ["demo_app/app.py"]},
            {"summary": "y", "files": ["src/invented.py"]},
        ])
        self.assertFalse(engine.evaluate_consensus(CRASH).consensus_reached)


class PullRequestTests(unittest.TestCase):
    """Human-in-the-loop: nothing lands before approval, approval lands exactly the patch."""

    def setUp(self) -> None:
        self.repo = TempRepo()
        self.dir = tempfile.mkdtemp(prefix="iow-pr-")
        self.gm = GitManager(repo_dir=self.repo.path)
        # No forge in the test: no creds file, no GITEA_* overrides.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GITEA_")}
        self._env = mock.patch.dict(os.environ, env, clear=True)
        self._env.start()
        self.prs = self._manager()

    def tearDown(self) -> None:
        self._env.stop()
        self.repo.cleanup()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _manager(self) -> PullRequestManager:
        return PullRequestManager(
            self.gm,
            store_path=os.path.join(self.dir, "prs.json"),
            creds_file=os.path.join(self.dir, "no-gitea.env"),
        )

    def _open(self, path: str = "demo_app/app.py") -> Any:
        return self.prs.create_pull_request(
            cycle_id="c1",
            diagnosis_summary="trigger_crash raises on purpose",
            patch_files=[{"path": path, "op": "modify", "content": "print('fixed')\n"}],
            base_revision=self.gm.working_tree_commit(),
        )

    def test_review_branch_holds_only_the_patch_and_nothing_is_applied(self) -> None:
        before = self.repo.snapshot_files()
        pr = self._open()
        self.assertEqual(pr.status, "pending_review")
        self.assertIsNone(pr.remote_pr_number)            # no forge configured here
        changed = self.repo.git("diff", "--name-only", pr.base_revision, pr.branch_name)
        self.assertEqual(changed, "demo_app/app.py")
        self.assertIn("+print('fixed')", pr.unified_diff)
        self.assertEqual(self.repo.snapshot_files(), before)

    def test_each_pr_has_its_own_base_and_a_single_spaced_diff(self) -> None:
        """Seen live: PR #2 force-pushed the shared base and emptied PR #1; the stored
        diff had a blank line after every line."""
        pr = self._open()
        self.assertEqual(pr.base_branch, "iow/base/c1")
        body = [line for line in pr.unified_diff.splitlines()[2:] if not line.startswith("@@")]
        self.assertNotIn("", body)

    def test_paths_outside_the_heal_scope_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._open("p3/agent.py")

    def test_approval_lands_the_patch_without_touching_the_index(self) -> None:
        pr = self._open()
        merged: List[str] = []
        ok, _ = self.prs.merge_pr(pr.pr_id, on_merged=lambda rec: merged.append(rec.pr_id))
        self.assertTrue(ok)
        self.assertEqual(self.repo.read("demo_app/app.py"), "print('fixed')\n")
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), self.gm.get_branch_tip(pr.branch_name))
        self.assertEqual(self.repo.git("diff", "--cached", "--name-only"), "")
        self.assertEqual(merged, [pr.pr_id])
        reloaded = self._manager().get_pr(pr.pr_id)                          # persisted
        self.assertEqual(reloaded.status if reloaded else None, "merged")

    def test_rejection_discards_the_review(self) -> None:
        before = self.repo.snapshot_files()
        pr = self._open()
        ok, _ = self.prs.reject_pr(pr.pr_id, reason="not like this")
        self.assertTrue(ok)
        self.assertIsNone(self.gm.get_branch_tip(pr.branch_name))
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertFalse(self.prs.merge_pr(pr.pr_id)[0])      # a rejected PR cannot land

    def test_a_stale_pr_is_refused_instead_of_reverting_later_changes(self) -> None:
        """A PR is complete files built on its base: landing it after the code moved on
        (a later heal, an edit) silently reverted that change."""
        pr = self._open()
        later = self.repo.read("demo_app/app.py").replace('"ok"', '"later heal"')
        self.repo.write("demo_app/app.py", later)
        ok, message = self.prs.merge_pr(pr.pr_id)
        self.assertFalse(ok)
        self.assertIn("stale", message)
        self.assertEqual(self.repo.read("demo_app/app.py"), later)
        self.assertEqual(self.prs.get_pr(pr.pr_id).status, "pending_review")  # type: ignore[union-attr]

    def test_a_pr_already_running_is_not_redeployed(self) -> None:
        """A verified heal's record PR: re-verifying it could only risk a transient
        failure rolling a verified heal back to its broken base."""
        pr = self._open()
        self.repo.write("demo_app/app.py", "print('fixed')\n")      # already on disk
        deployed: List[str] = []

        def deploy(rec: Any) -> Dict[str, Any]:
            deployed.append(rec.pr_id)
            return {}

        ok, _ = self.prs.merge_pr(pr.pr_id, on_merged=deploy)
        self.assertTrue(ok)
        self.assertEqual(deployed, [])
        self.assertEqual(self.prs.get_pr(pr.pr_id).status, "merged")  # type: ignore[union-attr]

    def test_an_operator_config_edit_does_not_make_a_pr_stale(self) -> None:
        pr = self._open()
        self.repo.write("demo_app/iow.config.yml", "grace_period: 5\n")
        self.assertTrue(self.prs.merge_pr(pr.pr_id)[0])
        self.assertEqual(self.repo.read("demo_app/iow.config.yml"), "grace_period: 5\n")

    def test_a_landing_that_fails_is_recorded_as_reverted(self) -> None:
        pr = self._open()
        ok, message = self.prs.merge_pr(
            pr.pr_id, on_merged=lambda rec: {"healed": False, "status": "crashed", "reverted": True}
        )
        self.assertFalse(ok)
        self.assertIn("rolled back", message)
        reloaded = self._manager().get_pr(pr.pr_id)
        assert reloaded is not None
        self.assertEqual(reloaded.status, "reverted")
        self.assertEqual((reloaded.landing or {}).get("status"), "crashed")


class BonusRouterTests(unittest.TestCase):
    def test_every_action_is_locked_outside_the_bonus_stages(self) -> None:
        """The README locks the Bonus in make p1/p2/p3; only the UI used to."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from bonus.bonus_api import create_bonus_router

        app = FastAPI()
        flags = {"human_in_the_loop": False, "second_opinion_mandatory": False, "fast_path_classifier": True}
        app.include_router(create_bonus_router(flags=flags, enabled=False, mode="p3"))
        client = TestClient(app)
        for path in ("/api/bonus/flags/toggle?flag_name=human_in_the_loop", "/api/bonus/prs/pr-x/merge",
                     "/api/bonus/prs/pr-x/reject", "/api/bonus/consensus/evaluate"):
            self.assertEqual(client.post(path, json={"log_excerpt": "x"}).status_code, 409, path)
        self.assertFalse(flags["human_in_the_loop"])
        self.assertEqual(client.get("/api/bonus/prs").status_code, 200)       # reading stays open

    def test_a_pr_created_through_the_api_passes_the_sanity_gate(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from bonus.bonus_api import create_bonus_router

        app = FastAPI()
        app.include_router(create_bonus_router(
            pr_manager=mock.Mock(), patch_gate=lambda files: "Replaces non-empty file with a placeholder",
        ))
        answer = TestClient(app).post("/api/bonus/prs/create", json={
            "cycle_id": "c", "diagnosis_summary": "d",
            "patch_files": [{"path": "demo_app/app.py", "op": "modify", "content": "pass\n"}],
        })
        self.assertEqual(answer.status_code, 422)
        self.assertIn("placeholder", answer.json()["detail"])


def _pod(init_state: Dict[str, Any], init_last: Optional[Dict[str, Any]] = None,
         app_state: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"status": {
        "phase": "Pending",
        "initContainerStatuses": [{"name": "fetch-source", "state": init_state,
                                   "lastState": init_last or {}}],
        "containerStatuses": [{"name": "demo", "restartCount": 0,
                               "state": app_state or {"waiting": {"reason": "PodInitializing"}}}],
    }}


class GitOpsTests(unittest.TestCase):
    def test_a_pod_that_never_started_the_app_is_not_a_code_crash(self) -> None:
        """Seen live: the source fetch could not resolve Gitea, and the loop 'healed' good code."""
        from bonus.gitops import _start_blocker
        fetch_failing = _pod({"waiting": {"reason": "CrashLoopBackOff"}},
                             {"terminated": {"exitCode": 128, "reason": "Error"}})
        self.assertIn("fetch-source", _start_blocker(fetch_failing) or "")
        retrying = _pod({"running": {}}, {"terminated": {"exitCode": 128, "reason": "Error"}})
        self.assertIsNotNone(_start_blocker(retrying))
        no_image = _pod({"terminated": {"exitCode": 0}},
                        app_state={"waiting": {"reason": "ImagePullBackOff"}})
        self.assertIn("ImagePullBackOff", _start_blocker(no_image) or "")

    def test_a_started_or_crashing_app_is_left_to_the_crash_path(self) -> None:
        from bonus.gitops import _start_blocker
        fetched = {"terminated": {"exitCode": 0, "reason": "Completed"}}
        self.assertIsNone(_start_blocker(_pod(fetched, app_state={"running": {}})))
        crashing = _pod(fetched, app_state={"waiting": {"reason": "CrashLoopBackOff"}})
        self.assertIsNone(_start_blocker(crashing))              # the app's own crash

    def test_the_newest_pod_is_the_one_watched(self) -> None:
        """Seen live: a crash-looping new revision hid behind the old pod that still served."""
        from bonus.gitops import _app_has_run, _newest_pod
        old = {"metadata": {"name": "old", "creationTimestamp": "2026-09-26T10:45:57Z"},
               "status": {"containerStatuses": [{"state": {"running": {}}}]}}
        new = {"metadata": {"name": "new", "creationTimestamp": "2026-09-26T10:46:44Z"},
               "status": {"containerStatuses": [{
                   "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                   "lastState": {"terminated": {"exitCode": 1}}}]}}
        leaving = {"metadata": {"name": "leaving", "creationTimestamp": "2026-09-26T10:50:00Z",
                                "deletionTimestamp": "2026-09-26T10:51:00Z"}}
        newest = _newest_pod([old, new, leaving]) or {}
        self.assertEqual(newest["metadata"]["name"], "new")
        self.assertTrue(_app_has_run(new))                       # its traceback can be read
        self.assertFalse(_app_has_run(_pod({"running": {}})))    # still fetching the source

    def test_a_timed_out_command_never_echoes_its_arguments(self) -> None:
        import sys
        from bonus.gitops import _run
        code, _, err = _run([sys.executable, "-c", "import time; time.sleep(5)",
                             "--password", "s3cret"], timeout=0.2)
        self.assertEqual(code, -1)
        self.assertIn("timed out", err)
        self.assertNotIn("s3cret", err)

    def test_argocd_is_spoken_to_over_tls_unless_told_otherwise(self) -> None:
        from bonus import gitops
        calls: List[List[str]] = []

        def fake_run(cmd: List[str], timeout: float = 60.0) -> Tuple[int, str, str]:
            calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(gitops, "_run", fake_run), \
                mock.patch.object(gitops, "_which", lambda name: "/bin/argocd"), \
                mock.patch.object(gitops, "_is_argocd", lambda server, plaintext: True), \
                mock.patch.dict(os.environ, {"IOW_ARGOCD_PASSWORD": "pw"}, clear=False):
            os.environ.pop("IOW_ARGOCD_PLAINTEXT", None)
            self.assertTrue(gitops._argocd_sync("iow-demo", 30)[0])
            self.assertTrue(all("--plaintext" not in c and "--insecure" in c for c in calls))
            calls.clear()
            with mock.patch.dict(os.environ, {"IOW_ARGOCD_PLAINTEXT": "1"}):
                gitops._argocd_sync("iow-demo", 30)
            self.assertTrue(all("--plaintext" in c for c in calls))

    def test_the_admin_password_only_goes_to_argo_cd(self) -> None:
        """8080 is a popular port: when something else holds it, `argocd login` there
        would hand the admin password to that other service."""
        from bonus import gitops
        calls: List[List[str]] = []

        def fake_run(cmd: List[str], timeout: float = 60.0) -> Tuple[int, str, str]:
            calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(gitops, "_run", fake_run), \
                mock.patch.object(gitops, "_which", lambda name: "/bin/argocd"), \
                mock.patch.object(gitops, "_is_argocd", lambda server, plaintext: False), \
                mock.patch.dict(os.environ, {"IOW_ARGOCD_PASSWORD": "pw",
                                             "IOW_ARGOCD_SERVER": "127.0.0.1:8080"}):
            ok, detail = gitops._argocd_sync("iow-demo", 30)
            with mock.patch.dict(os.environ, {"IOW_ARGOCD_SERVER": ""}):
                self.assertIn("no Argo CD API", gitops._argocd_sync("iow-demo", 30)[1])
        self.assertFalse(ok)
        self.assertIn("does not answer like Argo CD", detail)
        self.assertEqual(calls, [])                          # no login, no password on argv

    def test_a_followed_pod_log_can_be_closed_from_another_thread(self) -> None:
        """The Observer's "Restart Stream" closes the stream a worker is blocked on; a
        generator cannot be closed while another thread runs inside it."""
        import subprocess
        import threading
        import time
        from bonus.gitops import _ProcessLines

        proc = subprocess.Popen(["sh", "-c", "echo one; echo two; exec sleep 30"], stdout=subprocess.PIPE)
        stream = _ProcessLines(proc)
        lines: List[bytes] = []
        reader = threading.Thread(target=lambda: lines.extend(stream), daemon=True)
        reader.start()
        deadline = time.time() + 5
        while len(lines) < 2 and time.time() < deadline:
            time.sleep(0.02)
        stream.close()                      # the reader is blocked on the silent pipe
        reader.join(5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(lines, [b"one\n", b"two\n"])


if __name__ == "__main__":
    unittest.main()
