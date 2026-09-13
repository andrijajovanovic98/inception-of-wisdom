"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Git Manager: Atomic Patch Application, iow/auto-heal Branching & Snapshot Rollback
"""

from __future__ import annotations

import os
import subprocess
import logging
from typing import List, Optional, Tuple
from dataclasses import dataclass, field

from p3.patcher import StructuredPatch

logger = logging.getLogger("p3.git_manager")

DEFAULT_HEAL_BRANCH = "iow/auto-heal"
COMMIT_AUTHOR = "IoW Auto-Heal Agent <auto-heal@iow.local>"


@dataclass
class CommitResult:
    """Outcome of an atomic patch application and git commit."""
    success: bool
    commit_hash: Optional[str] = None
    branch: str = DEFAULT_HEAL_BRANCH
    files_modified: List[str] = field(default_factory=list)
    commit_message: Optional[str] = None
    error_message: Optional[str] = None


class GitManager:
    """Manages atomic disk writes, dedicated branch isolation, and snapshot rollbacks.
    Subject requirement:
    - Apply patch atomically to target's working tree.
    - Commit on dedicated branch (iow/auto-heal) with message from diagnosis.
    - Rollback rewinds auto-heal branch to pre-loop revision, matching byte for byte.
    """

    def __init__(self, repo_dir: Optional[str] = None, heal_branch: str = DEFAULT_HEAL_BRANCH):
        self.repo_dir = os.path.abspath(repo_dir) if repo_dir else os.getcwd()
        self.heal_branch = heal_branch
        self._pre_loop_hash: Optional[str] = None
        self._initial_branch: Optional[str] = None

    def _run_git(self, args: List[str], check: bool = True) -> Tuple[int, str, str]:
        """Runs a git command in the target repository directory."""
        cmd = ["git"] + args
        try:
            res = subprocess.run(
                cmd,
                cwd=self.repo_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=check
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"Git command failed: {' '.join(cmd)} -> {e.stderr.strip()}")
            return e.returncode, e.stdout.strip(), e.stderr.strip()
        except Exception as e:
            logger.error(f"Execution error running git: {e}")
            return -1, "", str(e)

    def get_current_head(self) -> Optional[str]:
        """Returns the current commit hash (HEAD)."""
        code, out, _ = self._run_git(["rev-parse", "HEAD"], check=False)
        return out if code == 0 else None

    def get_current_branch(self) -> Optional[str]:
        """Returns the name of the currently checked-out branch."""
        code, out, _ = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return out if code == 0 else None

    def snapshot_pre_loop(self) -> str:
        """Subject requirement: Snapshot the original commit hash before heal loop starts.
        Records the pre-loop revision used for byte-for-byte rollback.
        """
        current_hash = self.get_current_head()
        if not current_hash:
            raise RuntimeError("Cannot snapshot: repository has no valid HEAD commit.")

        self._pre_loop_hash = current_hash
        self._initial_branch = self.get_current_branch()
        logger.info(
            f"Recorded pre-loop revision snapshot: {self._pre_loop_hash[:8]} "
            f"on branch '{self._initial_branch}'"
        )
        return self._pre_loop_hash

    def _write_file_atomically(self, rel_path: str, content: str) -> None:
        """Write content to rel_path via temp file + atomic rename."""
        full_path = os.path.join(self.repo_dir, rel_path)
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        temp_path = full_path + ".iow_tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(temp_path, full_path)

    def prepare_heal_branch(self) -> bool:
        """Checks out the dedicated auto-heal branch starting from the pre-loop revision."""
        if not self._pre_loop_hash:
            self.snapshot_pre_loop()

        pre_hash = self._pre_loop_hash
        if not pre_hash:
            return False

        # Switch to dedicated auto-heal branch
        logger.info(f"Checking out dedicated branch '{self.heal_branch}'...")
        code, out, err = self._run_git(["checkout", "-B", self.heal_branch, pre_hash], check=False)
        if code != 0:
            logger.error(f"Failed to switch to branch '{self.heal_branch}': {err}")
            return False

        logger.info(f"Successfully on dedicated heal branch '{self.heal_branch}'.")
        return True

    def apply_patch_atomically(self, patch: StructuredPatch) -> CommitResult:
        """Applies patch files atomically using filesystem renames, stages them,
        and creates a git commit on the dedicated auto-heal branch.
        """
        if not patch.files:
            return CommitResult(success=False, error_message="Patch contains no files to apply.")

        # Ensure we are operating on the dedicated auto-heal branch
        if not self.prepare_heal_branch():
            return CommitResult(success=False, error_message="Could not switch to dedicated heal branch.")

        modified_files: List[str] = []

        try:
            # Atomic file application
            for file_patch in patch.files:
                rel_path = file_patch.path
                full_path = os.path.join(self.repo_dir, rel_path)
                op = file_patch.op

                os.makedirs(os.path.dirname(full_path), exist_ok=True)

                if op in ["create", "modify"]:
                    # Write to temporary file in the exact same directory, then atomic rename
                    temp_path = full_path + ".iow_tmp"
                    with open(temp_path, "w", encoding="utf-8") as f:
                        f.write(file_patch.content)
                    os.replace(temp_path, full_path)
                    modified_files.append(rel_path)
                    logger.debug(f"Atomically wrote {op} to {rel_path}")

                elif op == "delete":
                    if os.path.exists(full_path):
                        os.remove(full_path)
                    modified_files.append(rel_path)
                    logger.debug(f"Deleted file {rel_path}")

            # Stage modified files in git
            for rel_path in modified_files:
                code, _, err = self._run_git(["add", rel_path], check=False)
                if code != 0:
                    raise RuntimeError(f"git add failed for {rel_path}: {err}")

            # Create commit with message derived from diagnosis
            clean_summary = patch.summary.strip().replace("\n", " ")
            commit_msg = f"fix(auto-heal): {clean_summary[:80]}"

            commit_cmd = [
                "-c", "user.name=IoW Auto-Heal Agent",
                "-c", "user.email=auto-heal@iow.local",
                "commit",
                "-m", commit_msg
            ]

            code, _, err = self._run_git(commit_cmd, check=False)
            if code != 0:
                raise RuntimeError(f"git commit failed: {err}")

            new_hash = self.get_current_head()
            hash_label = (new_hash or "?")[:8]
            logger.info(f"Successfully committed auto-heal patch: {hash_label} ('{commit_msg}')")

            return CommitResult(
                success=True,
                commit_hash=new_hash,
                branch=self.heal_branch,
                files_modified=modified_files,
                commit_message=commit_msg
            )

        except Exception as e:
            logger.error(f"Atomic patch application failed: {e}. Rewinding working tree...")
            self._run_git(["checkout", "--", "."], check=False)
            return CommitResult(
                success=False,
                error_message=str(e),
                files_modified=modified_files
            )

    def rollback_to_pre_loop(self) -> bool:
        """Subject requirement:
        'After three failed attempts, the Wisdom Loop rewinds the auto-heal branch
        to the pre-loop revision (the agent's commits become unreachable), restarts
        the target on the original code, and surfaces a clean failure to the dashboard.
        Rollback has to leave the repository in the exact state it was in before the
        loop started. Hard reset.'
        """
        if not self._pre_loop_hash:
            logger.error("Cannot rollback: no pre-loop snapshot hash recorded.")
            return False

        current_branch = self.get_current_branch()
        # Never hard-reset main/master - that wipes local WIP outside heal attempts
        if current_branch not in (self.heal_branch,):
            logger.error(
                f"Refusing rollback on branch '{current_branch}': "
                f"only '{self.heal_branch}' may be hard-reset (protects local WIP)."
            )
            return False

        logger.warning(
            f"INITIATING ROLLBACK: Rewinding working tree and branch '{self.heal_branch}' "
            f"to pre-loop revision {self._pre_loop_hash[:8]}..."
        )

        # 1. Hard reset to pre-loop revision
        code1, _, err1 = self._run_git(["reset", "--hard", self._pre_loop_hash], check=False)
        if code1 != 0:
            logger.error(f"Rollback git reset --hard failed: {err1}")
            return False

        # 2. Clean any newly created untracked files (heal branch only)
        code2, _, err2 = self._run_git(["clean", "-fd"], check=False)
        if code2 != 0:
            logger.warning(f"Rollback git clean -fd warning: {err2}")

        current_head = self.get_current_head()
        if current_head == self._pre_loop_hash:
            logger.info(
                f"ROLLBACK COMPLETE: Working tree byte-for-byte restored "
                f"to pre-loop revision {current_head[:8]}."
            )
            return True
        else:
            logger.error(
                f"Rollback verification mismatch: HEAD is {current_head}, "
                f"expected {self._pre_loop_hash}"
            )
            return False

    def rollback_to(self, revision: str) -> Optional[str]:
        """Hard-reset heal branch to an explicit revision (manual emergency only)."""
        if not revision:
            return None
        current_branch = self.get_current_branch()
        if current_branch not in (self.heal_branch,):
            logger.error(
                f"Refusing rollback_to on branch '{current_branch}': "
                f"only '{self.heal_branch}' may be hard-reset."
            )
            return None
        code, _, err = self._run_git(["reset", "--hard", revision], check=False)
        if code != 0:
            logger.error(f"rollback_to failed: {err}")
            return None
        self._run_git(["clean", "-fd"], check=False)
        return self.get_current_head()

    def get_pre_loop_hash(self) -> Optional[str]:
        """Returns the active pre-loop snapshot hash."""
        return self._pre_loop_hash
