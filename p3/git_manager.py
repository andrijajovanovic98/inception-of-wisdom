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
# Only these paths may be rewritten / rolled back by the Wisdom Loop.
# Agent source (p3/, dashboard/, bonus/, …) must never be wiped by heal rollback.
DEFAULT_HEAL_PATHS = ("demo_app",)


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

    def __init__(
        self,
        repo_dir: Optional[str] = None,
        heal_branch: str = DEFAULT_HEAL_BRANCH,
        heal_paths: Optional[Tuple[str, ...]] = None,
    ):
        self.repo_dir = os.path.abspath(repo_dir) if repo_dir else os.getcwd()
        self.heal_branch = heal_branch
        self.heal_paths: Tuple[str, ...] = heal_paths or DEFAULT_HEAL_PATHS
        self._pre_loop_hash: Optional[str] = None
        self._initial_branch: Optional[str] = None

    def _is_heal_path(self, rel_path: str) -> bool:
        """True if rel_path is under an allowed heal target directory."""
        norm = rel_path.replace("\\", "/").lstrip("./")
        for root in self.heal_paths:
            root_n = root.replace("\\", "/").rstrip("/")
            if norm == root_n or norm.startswith(root_n + "/"):
                return True
        return False

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
        """Ensure we are on iow/auto-heal without destroying uncommitted agent WIP.

        Never uses `checkout -B <branch> <hash>` - that resets the whole working tree
        and would wipe uncommitted edits under p3/, dashboard/, bonus/, etc.

        Does not snapshot: caller must snapshot AFTER this returns so pre_loop_hash
        is the heal-branch tip (not main).
        """
        current = self.get_current_branch()
        if current == self.heal_branch:
            logger.info(f"Already on dedicated heal branch '{self.heal_branch}'.")
            return True

        logger.info(f"Checking out dedicated branch '{self.heal_branch}' (preserving local WIP)...")
        code, _, err = self._run_git(["checkout", self.heal_branch], check=False)
        if code != 0:
            # Create branch from current HEAD.
            code, _, err = self._run_git(
                ["checkout", "-b", self.heal_branch],
                check=False,
            )
            if code != 0:
                logger.error(f"Failed to create/switch to branch '{self.heal_branch}': {err}")
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
            # Atomic file application (heal-target paths only)
            for file_patch in patch.files:
                rel_path = file_patch.path
                if not self._is_heal_path(rel_path):
                    raise RuntimeError(
                        f"Refusing to patch '{rel_path}': outside heal paths {self.heal_paths}"
                    )
                full_path = os.path.join(self.repo_dir, rel_path)
                op = file_patch.op

                os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)

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
            logger.error(f"Atomic patch application failed: {e}. Restoring heal-target files only...")
            if modified_files:
                self._run_git(["checkout", "HEAD", "--", *modified_files], check=False)
            else:
                self._restore_heal_paths_from(self._pre_loop_hash or "HEAD")
            return CommitResult(
                success=False,
                error_message=str(e),
                files_modified=modified_files
            )

    def _restore_heal_paths_from(self, revision: str) -> bool:
        """Restore only heal-target paths from revision; leave agent WIP untouched."""
        if not revision:
            return False
        paths = list(self.heal_paths)
        code, _, err = self._run_git(["checkout", revision, "--", *paths], check=False)
        if code != 0:
            logger.error(f"Failed restoring heal paths {paths} from {revision[:8]}: {err}")
            return False
        # Drop untracked junk under heal paths only (never repo-wide clean).
        self._run_git(["clean", "-fd", "--", *paths], check=False)
        return True

    def rollback_to_pre_loop(self) -> bool:
        """Rewind heal-branch tip + restore demo_app only.

        Subject wants a hard rewind of the *target* after failed heals. A full
        `git reset --hard` / `git clean -fd` would also delete uncommitted agent
        development (p3/, dashboard/, bonus/) in this monorepo - that is forbidden.
        """
        if not self._pre_loop_hash:
            logger.error("Cannot rollback: no pre-loop snapshot hash recorded.")
            return False

        current_branch = self.get_current_branch()
        if current_branch not in (self.heal_branch,):
            logger.error(
                f"Refusing rollback on branch '{current_branch}': "
                f"only '{self.heal_branch}' may be rewound (protects local WIP)."
            )
            return False

        pre = self._pre_loop_hash
        logger.warning(
            f"INITIATING ROLLBACK: Moving '{self.heal_branch}' tip to {pre[:8]} "
            f"and restoring only {list(self.heal_paths)} (agent WIP preserved)..."
        )

        # 1) Move branch tip without touching unrelated working-tree files.
        code1, _, err1 = self._run_git(["reset", "--mixed", pre], check=False)
        if code1 != 0:
            logger.error(f"Rollback git reset --mixed failed: {err1}")
            return False

        # 2) Force heal-target paths back to the pre-loop bytes.
        if not self._restore_heal_paths_from(pre):
            return False

        current_head = self.get_current_head()
        if current_head == pre:
            logger.info(
                f"ROLLBACK COMPLETE: Branch tip {current_head[:8]}; "
                f"restored paths {list(self.heal_paths)}; agent source left intact."
            )
            return True

        logger.error(
            f"Rollback verification mismatch: HEAD is {current_head}, expected {pre}"
        )
        return False

    def rollback_to(self, revision: str) -> Optional[str]:
        """Rewind heal branch tip + restore heal paths only (manual emergency)."""
        if not revision:
            return None
        current_branch = self.get_current_branch()
        if current_branch not in (self.heal_branch,):
            logger.error(
                f"Refusing rollback_to on branch '{current_branch}': "
                f"only '{self.heal_branch}' may be rewound."
            )
            return None
        code, _, err = self._run_git(["reset", "--mixed", revision], check=False)
        if code != 0:
            logger.error(f"rollback_to failed: {err}")
            return None
        if not self._restore_heal_paths_from(revision):
            return None
        return self.get_current_head()

    def get_pre_loop_hash(self) -> Optional[str]:
        """Returns the active pre-loop snapshot hash."""
        return self._pre_loop_hash
