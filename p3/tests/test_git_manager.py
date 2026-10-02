"""Part 3 - heal branch, atomic apply and byte-for-byte rollback."""

from __future__ import annotations

import os
import stat
import unittest

from p3.tests.helpers import TempRepo
from p3.git_manager import GitManager
from p3.patcher import FilePatch, StructuredPatch


def _patch(*files: FilePatch) -> StructuredPatch:
    return StructuredPatch(summary="fix", files=list(files))


class GitManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = TempRepo()
        self.head = self.repo.git("rev-parse", "HEAD")
        self.gm = GitManager(repo_dir=self.repo.path)

    def tearDown(self) -> None:
        self.repo.cleanup()

    def test_clean_tree_uses_head_as_pre_loop(self) -> None:
        self.assertEqual(self.gm.snapshot_pre_loop(), self.head)
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), self.head)

    def test_uncommitted_planted_bug_is_the_pre_loop_state(self) -> None:
        self.repo.write("demo_app/app.py", "# from flask import Flask\nBROKEN = True\n")
        before = self.repo.snapshot_files()
        pre = self.gm.snapshot_pre_loop()
        self.assertNotEqual(pre, self.head)
        self.assertEqual(self.repo.git("rev-parse", f"{pre}^"), self.head)
        self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "FIXED = 1\n")))
        self.assertTrue(self.gm.rollback_to_pre_loop())
        # Exactly the state before the loop - the planted bug, not HEAD's version.
        self.assertEqual(self.repo.snapshot_files(), before)
        self.assertTrue(self.gm.last_rollback_check and self.gm.last_rollback_check["identical"])

    def test_attempts_stack_and_rollback_makes_them_unreachable(self) -> None:
        pre = self.gm.snapshot_pre_loop()
        a1 = self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "A1 = 1\n")))
        a2 = self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/new.py", "create", "N = 1\n")))
        self.assertEqual(self.repo.git("rev-parse", f"{a1.commit_hash}^"), pre)
        self.assertEqual(self.repo.git("rev-parse", f"{a2.commit_hash}^"), a1.commit_hash)
        self.assertTrue(self.gm.rollback_to_pre_loop())
        self.assertEqual(self.gm.get_branch_tip("iow/auto-heal"), pre)
        reachable = self.repo.git("rev-list", "iow/auto-heal")
        self.assertNotIn(str(a2.commit_hash), reachable)
        self.assertFalse(os.path.exists(self.repo.abs("demo_app/new.py")))

    def test_rollback_restores_deleted_files_and_modes(self) -> None:
        before = self.repo.snapshot_files()
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_patch(
            FilePatch("demo_app/run.sh", "modify", "#!/bin/sh\necho patched\n"),
            FilePatch("demo_app/notes.txt", "delete", ""),
        ))
        self.assertTrue(os.stat(self.repo.abs("demo_app/run.sh")).st_mode & stat.S_IXUSR)
        self.assertTrue(self.gm.rollback_to_pre_loop())
        self.assertEqual(self.repo.snapshot_files(), before)

    def test_head_index_and_agent_wip_are_never_touched(self) -> None:
        self.repo.write("p3/agent.py", "AGENT = 'wip'\n")
        self.repo.git("add", "p3/agent.py")
        staged = self.repo.git("diff", "--cached", "--name-only")
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "X = 1\n")))
        self.gm.rollback_to_pre_loop()
        self.assertEqual(self.repo.git("rev-parse", "HEAD"), self.head)
        self.assertEqual(self.repo.git("rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(self.repo.git("diff", "--cached", "--name-only"), staged)
        self.assertEqual(self.repo.read("p3/agent.py"), "AGENT = 'wip'\n")

    def test_operator_config_edit_survives_rollback(self) -> None:
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "X = 1\n")))
        self.repo.write("demo_app/iow.config.yml", "grace_period: 3\n")
        self.assertTrue(self.gm.rollback_to_pre_loop())
        self.assertEqual(self.repo.read("demo_app/iow.config.yml"), "grace_period: 3\n")
        assert self.gm.last_rollback_check is not None
        self.assertEqual(self.gm.last_rollback_check["differing_paths"], ["demo_app/iow.config.yml"])

    def test_an_edit_made_during_the_cycle_is_restored_over_but_kept_in_git(self) -> None:
        """Seen during the audit: a rollback restored the pre-loop file over an edit made
        while the cycle ran, and the edit was simply gone."""
        before = self.repo.snapshot_files()
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "X = 1\n")))
        self.repo.write("demo_app/app.py", "OPERATOR = 'mid-cycle edit'\n")
        self.repo.write("demo_app/iow.config.yml", "grace_period: 3\n")   # operator-owned: no save
        self.assertTrue(self.gm.rollback_to_pre_loop())
        after = self.repo.snapshot_files()
        self.assertEqual(after["demo_app/app.py"], before["demo_app/app.py"])     # pre-loop bytes
        saved = (self.gm.last_rollback_check or {}).get("outside_edits") or {}
        self.assertEqual(saved.get("paths"), ["demo_app/app.py"])
        ref = str(saved.get("saved_as"))
        self.assertTrue(ref.startswith("refs/iow/overwritten/"))
        self.assertEqual(self.repo.git("show", f"{ref}:demo_app/app.py"), "OPERATOR = 'mid-cycle edit'")

    def test_nothing_is_saved_when_only_the_agent_touched_the_tree(self) -> None:
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "X = 1\n")))
        self.assertTrue(self.gm.rollback_to_pre_loop())
        self.assertIsNone((self.gm.last_rollback_check or {}).get("outside_edits"))
        self.assertEqual(self.repo.git("for-each-ref", "refs/iow/overwritten"), "")

    def test_paths_outside_the_heal_scope_are_refused(self) -> None:
        result = self.gm.apply_patch_atomically(_patch(FilePatch("p3/agent.py", "modify", "x\n")))
        self.assertFalse(result.success)
        self.assertEqual(self.repo.read("p3/agent.py"), "AGENT = 1\n")

    def test_no_temp_files_are_left_behind(self) -> None:
        self.gm.snapshot_pre_loop()
        self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "X = 1\n")))
        leftovers = [f for f in os.listdir(self.repo.abs("demo_app")) if "iow" in f and "tmp" in f]
        self.assertEqual(leftovers, [])

    def test_verified_heal_stays_on_the_branch_for_the_next_cycle(self) -> None:
        self.gm.snapshot_pre_loop()
        healed = self.gm.apply_patch_atomically(_patch(FilePatch("demo_app/app.py", "modify", "OK = 1\n")))
        # Next cycle: the deployed tree is the heal -> it is the new pre-loop state.
        self.assertEqual(self.gm.snapshot_pre_loop(), healed.commit_hash)


if __name__ == "__main__":
    unittest.main()
