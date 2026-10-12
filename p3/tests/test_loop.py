"""Part 3 - the Wisdom Loop end to end, with a scripted model and verifier."""

from __future__ import annotations

import threading
import time
import unittest
from typing import List, Optional

from p3.tests.helpers import TempRepo
from p1.docker_monitor import ContainerState
from p1.event_manager import ObserverEvent
from p2.diagnostician import DiagnosticReport
from p3.git_manager import GitManager
from p3.loop import WisdomLoop, commit_message_for
from p3.patcher import FilePatch, StructuredPatch
from p3.sanity import SanityChecker
from p3.verifier import VerificationResult


class FakeDiagnostician:
    def __init__(self) -> None:
        self.logs: List[str] = []

    def diagnose_crash(self, log_excerpt: str, exit_code=None, status: str = "crashed", top_k: int = 3,
                       exclude_files=None):
        self.logs.append(log_excerpt)
        return DiagnosticReport(
            success=True,
            summary="The Flask import was removed. Restoring it fixes the NameError.",
            files=["demo_app/app.py"],
        )


class FakePatcher:
    def __init__(self, contents: List[str]) -> None:
        self.contents = list(contents)
        self.hints: List[Optional[str]] = []
        self.temperatures: List[float] = []

    def generate_patch(self, diagnosis, log_excerpt: str, refusal_hint: Optional[str] = None, goal=None,
                       temperature: float = 0.1, facts: str = ""):
        self.hints.append(refusal_hint)
        self.temperatures.append(temperature)
        return StructuredPatch(
            summary="restore the import",
            files=[FilePatch("demo_app/app.py", "modify", self.contents.pop(0))],
        )


class FakeVerifier:
    def __init__(self, outcomes: List[bool], gate: Optional[threading.Event] = None) -> None:
        self.outcomes = list(outcomes)
        self.gate = gate
        self.calls = 0

    def restart_and_verify(self, grace_period: float = 20.0, progress_callback=None, replay=None):
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        healed = self.outcomes.pop(0) if self.outcomes else True
        return VerificationResult(
            is_healed=healed,
            status="healed" if healed else "crashed",
            grace_period_seconds=grace_period,
            elapsed_seconds=0.0,
            error_log=None if healed else f"NameError: attempt {self.calls} still broken",
            container_status="running" if healed else "restarting",
        )


class FakeDocker:
    def restart_target(self, timeout: int = 10) -> bool:
        return True


def _crash() -> ObserverEvent:
    now = time.time()
    return ObserverEvent(
        id="e1", type="crash", source="log", signature="sig-1",
        summary="NameError: name 'Flask' is not defined",
        details={"context_excerpt": "NameError: name 'Flask' is not defined"},
        first_seen=now, last_seen=now,
    )


def _fixed(repo: TempRepo, marker: str) -> str:
    return repo.read("demo_app/app.py").replace('"ok"', f'"{marker}"')


def _as_patch(content: str) -> StructuredPatch:
    return StructuredPatch(summary="fix", files=[FilePatch("demo_app/app.py", "modify", content)])


class WisdomLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.gm = GitManager(repo_dir=self.repo.path)
        self.sanity = SanityChecker(base_dir=self.repo.path, path_guard=self.gm._is_heal_path)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def _loop(self, patcher, verifier, diagnostician=None) -> WisdomLoop:
        return WisdomLoop(
            git_manager=self.gm,
            diagnostician=diagnostician or FakeDiagnostician(),  # type: ignore[arg-type]
            patcher=patcher,
            sanity_checker=self.sanity,
            verifier=verifier,
            docker_monitor=FakeDocker(),  # type: ignore[arg-type]
        )

    def test_heals_on_the_second_attempt_with_the_new_error_log(self) -> None:
        diag = FakeDiagnostician()
        patcher = FakePatcher([_fixed(self.repo, "a1"), _fixed(self.repo, "a2")])
        loop = self._loop(patcher, FakeVerifier([False, True]), diag)
        record = loop.execute_heal(_crash(), grace_period=0.0)
        self.assertEqual(record.status, "healed")
        self.assertEqual(len(record.attempts), 2)
        # Attempt 2 saw the failure log of attempt 1, not the original crash again.
        self.assertIn("attempt 1 still broken", diag.logs[1])
        self.assertIsNotNone(patcher.hints[1])
        # Commits stack, and the heal is the branch tip.
        c1, c2 = record.attempts[0].commit_hash, record.attempts[1].commit_hash
        self.assertEqual(self.repo.git("rev-parse", f"{c2}^"), c1)
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), c2)
        subject = self.repo.git("log", "-1", "--format=%s", str(c2))
        self.assertTrue(subject.startswith("fix(auto-heal): The Flask import was removed."))
        patch = record.attempts[1].patch
        assert patch is not None
        self.assertIn("+", patch["diff"])

    def test_three_failures_roll_back_byte_for_byte(self) -> None:
        self.repo.write("demo_app/app.py", self.repo.read("demo_app/app.py").replace(
            "from flask import Flask", "# from flask import Flask"))  # planted, uncommitted
        before = self.repo.snapshot_files()
        head = self.repo.git("rev-parse", "HEAD")
        patcher = FakePatcher([_fixed(self.repo, f"try{i}") for i in range(3)])
        verifier = FakeVerifier([False, False, False, True])
        record = self._loop(patcher, verifier).execute_heal(_crash(), grace_period=0.0)

        self.assertEqual(record.status, "rolled_back")
        self.assertEqual(len(record.attempts), 3)
        self.assertEqual(self.repo.snapshot_files(), before)       # working tree = pre-loop bytes
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), record.pre_loop_hash)
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), head)  # HEAD never moved
        self.assertTrue(record.rollback_check and record.rollback_check["identical"])
        self.assertEqual(verifier.calls, 4)                         # 3 attempts + restart on original code
        self.assertIsNotNone(record.post_rollback)
        for attempt in record.attempts:                             # agent commits unreachable
            self.assertNotIn(str(attempt.commit_hash), self.repo.git("rev-list", "iow/auto-heal"))
        self.assertIsNone(self.gm.interrupted_cycle())              # finished: nothing to recover
        self.assertEqual(patcher.temperatures, [0.1, 0.4, 0.7])     # retries sample more freely

    def test_a_failure_the_patch_itself_raises_asks_for_a_fallback(self) -> None:
        """Seen live on `make break-env`: the model's fix raised its own ValueError when the
        variable was missing, and two retries handed the same change back."""
        diff = (
            "+++ b/demo_app/app.py\n"
            "+    if 'IOW_DEMO_PORT' not in os.environ:\n"
            '+        raise ValueError("Environment variable IOW_DEMO_PORT is not set")\n'
        )
        own = VerificationResult(
            is_healed=False, status="restart_failed", grace_period_seconds=5, elapsed_seconds=0,
            error_log="Target exited with code 1 right after the restart.\nTraceback (most recent "
                      "call last):\nValueError: Environment variable IOW_DEMO_PORT is not set",
        )
        hint = WisdomLoop._verification_hint(own, diff)
        self.assertIn("code your change added", hint)
        self.assertIn("safe fallback", hint)
        # A different, genuine failure after a partial fix gets no such advice.
        other = VerificationResult(
            is_healed=False, status="restart_failed", grace_period_seconds=5, elapsed_seconds=0,
            error_log="Traceback (most recent call last):\nNameError: name 'jsonify' is not defined",
        )
        plain = WisdomLoop._verification_hint(other, "+from flask import Flask\n")
        self.assertNotIn("fallback", plain)
        self.assertIn("NameError", WisdomLoop._verification_hint(
            VerificationResult(is_healed=False, status="crashed", grace_period_seconds=5,
                               elapsed_seconds=0, error_log="NameError: x"), ""))

    def test_a_cycle_cut_short_by_an_agent_stop_is_undone_on_the_next_start(self) -> None:
        """Seen live: the unverified patch stayed, and the next cycle took it as pre-loop."""
        self.repo.write("demo_app/app.py", self.repo.read("demo_app/app.py").replace(
            "from flask import Flask", "# from flask import Flask"))  # planted, uncommitted
        before = self.repo.snapshot_files()
        # The previous agent: snapshot, first patch written and committed - then gone.
        pre = self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_as_patch(_fixed(self.repo, "unverified")))
        self.assertNotEqual(self.repo.snapshot_files(), before)

        self.gm = GitManager(repo_dir=self.repo.path)               # the next agent start
        verifier = FakeVerifier([True])
        record = self._loop(FakePatcher([]), verifier).recover_interrupted_cycle()

        assert record is not None
        self.assertEqual(record.status, "rolled_back")
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), pre)
        self.assertEqual(verifier.calls, 1)                         # restarted on the pre-loop code
        self.assertIsNone(self.gm.interrupted_cycle())
        self.assertIsNone(self._loop(FakePatcher([]), verifier).recover_interrupted_cycle())

    def test_the_restart_after_a_recovery_can_wait_for_later(self) -> None:
        """Start-up restores the files at once and restarts the target in the background."""
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_as_patch(_fixed(self.repo, "unverified")))
        self.gm = GitManager(repo_dir=self.repo.path)
        verifier = FakeVerifier([False])                            # the pre-loop code still crashes
        loop = self._loop(FakePatcher([]), verifier)
        record = loop.recover_interrupted_cycle(redeploy=False)
        assert record is not None
        self.assertEqual(verifier.calls, 0)
        self.assertIsNone(record.post_rollback)
        loop.redeploy_recovered(record)
        self.assertEqual(verifier.calls, 1)
        self.assertEqual((record.post_rollback or {}).get("target_up"), False)
        self.assertFalse(loop.is_active())

    def test_files_changed_after_the_interruption_are_not_overwritten(self) -> None:
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_as_patch(_fixed(self.repo, "unverified")))
        self.repo.write("demo_app/app.py", "OPERATOR = 'reset by hand'\n")   # e.g. make demo-reset
        self.gm = GitManager(repo_dir=self.repo.path)
        record = self._loop(FakePatcher([]), FakeVerifier([True])).recover_interrupted_cycle()
        assert record is not None
        self.assertIn("left as they are", record.failure_reason or "")
        self.assertEqual(self.repo.read("demo_app/app.py"), "OPERATOR = 'reset by hand'\n")
        self.assertIsNone(self.gm.interrupted_cycle())

    def test_an_operator_config_edit_does_not_stop_the_recovery(self) -> None:
        """L18: the agent stopped mid-cycle and the operator shortened grace_period meanwhile
        (the subject's own example of a live edit). The tree no longer matched either side,
        so the unverified patch was "left as it is" - and the next cycle would have taken it
        as its pre-loop state."""
        before = self.repo.read("demo_app/app.py")
        pre = self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_as_patch(_fixed(self.repo, "unverified")))
        self.repo.write("demo_app/iow.config.yml", "grace_period: 5\n")
        self.gm = GitManager(repo_dir=self.repo.path)               # the next agent start
        record = self._loop(FakePatcher([]), FakeVerifier([True])).recover_interrupted_cycle()
        assert record is not None
        self.assertEqual(record.status, "rolled_back")
        self.assertNotIn("left as they are", record.failure_reason or "")
        self.assertEqual(self.repo.read("demo_app/app.py"), before)
        self.assertEqual(self.repo.read("demo_app/iow.config.yml"), "grace_period: 5\n")   # kept
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), pre)
        self.assertIsNone(self.gm.interrupted_cycle())

    def test_an_unexpected_error_rolls_back_and_redeploys_through_the_verifier(self) -> None:
        """A bare container restart here never published the rewound branch in GitOps mode,
        so the cluster kept the unverified patch."""
        self.repo.write("demo_app/app.py", self.repo.read("demo_app/app.py").replace(
            "from flask import Flask", "# from flask import Flask"))  # planted, uncommitted
        before = self.repo.snapshot_files()

        class Exploding(FakeVerifier):
            def restart_and_verify(self, grace_period: float = 20.0, progress_callback=None, replay=None):
                if self.calls == 0:
                    self.calls += 1
                    raise RuntimeError("daemon went away mid-redeploy")
                return super().restart_and_verify(grace_period, progress_callback, replay)

        verifier = Exploding([False])
        record = self._loop(FakePatcher([_fixed(self.repo, "a1")]), verifier).execute_heal(
            _crash(), grace_period=0.0
        )
        self.assertEqual(record.status, "rolled_back")
        self.assertIn("daemon went away", record.failure_reason or "")
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertEqual(verifier.calls, 2)                         # the redeploy after the rollback
        self.assertEqual((record.post_rollback or {}).get("target_up"), False)
        self.assertIsNone(self.gm.interrupted_cycle())

    def test_refused_patch_is_never_written(self) -> None:
        before = self.repo.snapshot_files()
        patcher = FakePatcher(["pass\n", "pass\n", "pass\n"])     # a stub every time
        record = self._loop(patcher, FakeVerifier([True])).execute_heal(_crash(), grace_period=0.0)
        self.assertEqual(record.status, "rolled_back")
        self.assertTrue(all(a.commit_hash is None for a in record.attempts))
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertIn("placeholder", str(patcher.hints[1]))

    def test_single_flight(self) -> None:
        gate = threading.Event()
        loop = self._loop(FakePatcher([_fixed(self.repo, "x")]), FakeVerifier([True], gate=gate))
        worker = threading.Thread(target=loop.execute_heal, args=(_crash(), 0.0))
        worker.start()
        deadline = time.time() + 5
        while not loop.is_active() and time.time() < deadline:
            time.sleep(0.01)
        second = loop.execute_heal(_crash(), grace_period=0.0)
        gate.set()
        worker.join(5)
        self.assertEqual(second.status, "refused")

    def test_a_diagnose_fn_decides_every_attempt_not_only_the_first(self) -> None:
        """Second-opinion mode: attempts 2 and 3 used to patch on a single opinion."""
        votes: List[str] = []

        def disagree(log_excerpt: str, **_kw) -> DiagnosticReport:
            votes.append(log_excerpt)
            return DiagnosticReport(success=False, summary="", files=[],
                                    error_message="The two opinions disagreed.", mode="diverged")

        before = self.repo.snapshot_files()
        agreed = DiagnosticReport(success=True, summary="Restore the import.", files=["demo_app/app.py"])
        patcher = FakePatcher([_fixed(self.repo, "a1")])
        record = self._loop(patcher, FakeVerifier([False])).execute_heal(
            _crash(), grace_period=0.0, preset_diagnosis=agreed, diagnose_fn=disagree,
        )
        self.assertEqual(record.status, "rolled_back")
        self.assertEqual(len(votes), 2)                            # attempts 2 and 3 were voted on
        self.assertIn("attempt 1 still broken", votes[0])          # on the new error log
        self.assertIsNone(record.attempts[1].commit_hash)          # no agreement, no patch
        self.assertIn("disagreed", record.attempts[2].error_message or "")
        self.assertEqual(self.repo.snapshot_files(), before)

    def test_a_crash_that_is_already_gone_changes_nothing(self) -> None:
        """An old event picked by hand, or a sibling signature of a crash an earlier heal
        fixed: the model would be asked to change working code."""
        class HealthyNow(FakeVerifier):
            def symptom_present(self, replay=()):
                return False, "the target is running and every probe answers"

        before = self.repo.snapshot_files()
        tip = self.gm.get_branch_tip("iow/auto-heal")
        diag = FakeDiagnostician()
        verifier = HealthyNow([True])
        record = self._loop(FakePatcher([_fixed(self.repo, "x")]), verifier, diag).execute_heal(
            _crash(), grace_period=0.0
        )
        self.assertEqual(record.status, "not_needed")
        self.assertIn("symptom is gone", record.failure_reason or "")
        self.assertEqual(diag.logs, [])                       # the model was never asked
        self.assertEqual(verifier.calls, 0)                   # nothing restarted
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), tip)
        self.assertFalse(self._loop(FakePatcher([]), verifier).is_active())

    def test_a_target_stopped_on_purpose_is_never_patched(self) -> None:
        """Seen live: after a `docker stop` the model replaced the healthcheck and 'healed'."""
        class Stopped(FakeDocker):
            def inspect(self):
                return ContainerState(name="t", status="exited", exit_code=0, stopped=True,
                                      error_reason="Stopped (exit code 0) - not crashed")

        before = self.repo.snapshot_files()
        diag = FakeDiagnostician()
        verifier = FakeVerifier([True])
        loop = self._loop(FakePatcher([_fixed(self.repo, "x")]), verifier, diag)
        loop.docker_monitor = Stopped()  # type: ignore[assignment]
        record = loop.execute_heal(_crash(), grace_period=0.0)
        self.assertEqual(record.status, "refused")
        self.assertIn("Nothing to patch", record.failure_reason or "")
        self.assertEqual((diag.logs, verifier.calls), ([], 0))       # no model, no restart
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertFalse(loop.is_active())

    def test_a_human_decision_on_a_review_pr_shows_on_its_cycle(self) -> None:
        """Seen live: an approved and verified review stayed 'awaiting_review' in the history."""
        loop = self._loop(FakePatcher([]), FakeVerifier([True]))
        waiting = loop.record_refused_cycle(
            _crash(), "sent for review", status="awaiting_review",
            review_pr="pr-abc", pre_loop_hash="base123",
        )
        other = loop.record_refused_cycle(_crash(), "withheld")
        self.assertEqual(waiting.pre_loop_hash, "base123")
        self.assertIsNone(loop.resolve_review("pr-unknown", "healed", "x"))
        self.assertIs(loop.resolve_review("pr-abc", "healed", "Approved and verified"), waiting)
        self.assertEqual((waiting.status, waiting.failure_reason), ("healed", "Approved and verified"))
        self.assertEqual(waiting.to_dict()["review_pr"], "pr-abc")
        self.assertEqual(other.status, "refused")
        self.assertIsNone(loop.resolve_review("pr-abc", "refused", "again"))   # decided once

    def test_commit_message_is_generated_from_the_diagnosis(self) -> None:
        diag = DiagnosticReport(success=True, summary="Bad PORT env var. It is not an int.", files=["x"])
        patch = StructuredPatch(summary="cast safely", files=[FilePatch("demo_app/app.py", "modify", "x")])
        msg = commit_message_for(diag, patch, "abcd1234", 2, 3)
        self.assertTrue(msg.startswith("fix(auto-heal): Bad PORT env var."))
        self.assertIn("Cycle: abcd1234 attempt 2/3", msg)


if __name__ == "__main__":
    unittest.main()
