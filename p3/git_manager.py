"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Git Manager: Atomic Patch Application, iow/auto-heal Branching & Snapshot Rollback

The heal branch is written with git plumbing (hash-object / commit-tree / update-ref)
so HEAD and the working tree are never moved. Only heal-target paths (demo_app/) are
ever written or restored; the peer's checked-out branch and WIP are left untouched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import logging
from typing import Dict, List, Optional, Sequence, Tuple
from dataclasses import dataclass, field

from p3.patcher import StructuredPatch

logger = logging.getLogger("p3.git_manager")

DEFAULT_HEAL_BRANCH = "iow/auto-heal"
COMMIT_NAME = "IoW Auto-Heal Agent"
COMMIT_EMAIL = "auto-heal@iow.local"
# Only these paths may be rewritten / rolled back by the Wisdom Loop.
# Agent source (p3/, dashboard/, bonus/, …) must never be touched by a heal.
DEFAULT_HEAL_PATHS = ("demo_app",)
# Operator territory inside the heal tree. The agent may never patch these, and a
# rollback must not revert them either: iow.config.yml is the peer's control surface
# (grace period, kill switch, probes) and losing their edit mid-defense is not a
# rollback, it is a regression.
DEFAULT_PROTECTED_PATHS = ("demo_app/iow.config.yml",)


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
        protected_paths: Optional[Tuple[str, ...]] = None,
    ):
        self.repo_dir = os.path.realpath(os.path.abspath(repo_dir or os.getcwd()))
        self.heal_branch = heal_branch
        self.heal_paths: Tuple[str, ...] = heal_paths or DEFAULT_HEAL_PATHS
        self.protected_paths: Tuple[str, ...] = (
            DEFAULT_PROTECTED_PATHS if protected_paths is None else protected_paths
        )
        self._pre_loop_hash: Optional[str] = None
        self._initial_branch: Optional[str] = None
        # Paths created (not merely modified) by the running cycle - only these may
        # ever be removed during a rollback.
        self._created_paths: List[str] = []

    # ------------------------------------------------------------------
    # Path safety
    # ------------------------------------------------------------------
    def _is_heal_path(self, rel_path: str) -> bool:
        """True only if rel_path resolves *inside* an allowed heal directory.

        Resolution happens before the comparison, so 'demo_app/../../etc/passwd'
        is rejected even though it starts with an allowed prefix.
        """
        if not rel_path or os.path.isabs(rel_path):
            return False
        candidate = os.path.realpath(os.path.join(self.repo_dir, rel_path))

        # Operator-owned files are inside the heal tree but off limits to the agent.
        for protected in self.protected_paths:
            if candidate == os.path.realpath(os.path.join(self.repo_dir, protected)):
                return False

        for root in self.heal_paths:
            root_abs = os.path.realpath(os.path.join(self.repo_dir, root))
            try:
                if os.path.commonpath([candidate, root_abs]) == root_abs:
                    return True
            except ValueError:
                # Unrelated roots (different drives) - not a heal path.
                continue
        return False

    def heal_paths_list(self) -> List[str]:
        """Allowed heal roots, relative to the repository."""
        return list(self.heal_paths)

    # ------------------------------------------------------------------
    # git invocation
    # ------------------------------------------------------------------
    def _git_env(self, index_file: Optional[str] = None) -> Dict[str, str]:
        env = dict(os.environ)
        env.update({
            "GIT_AUTHOR_NAME": COMMIT_NAME,
            "GIT_AUTHOR_EMAIL": COMMIT_EMAIL,
            "GIT_COMMITTER_NAME": COMMIT_NAME,
            "GIT_COMMITTER_EMAIL": COMMIT_EMAIL,
        })
        if index_file:
            env["GIT_INDEX_FILE"] = index_file
        return env

    def _run_git(
        self,
        args: Sequence[str],
        check: bool = True,
        env: Optional[Dict[str, str]] = None,
        stdin_data: Optional[str] = None,
    ) -> Tuple[int, str, str]:
        """Runs a git command in the target repository directory."""
        cmd = ["git"] + list(args)
        try:
            res = subprocess.run(
                cmd,
                cwd=self.repo_dir,
                input=stdin_data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=check,
                env=env if env is not None else self._git_env(),
            )
            return res.returncode, res.stdout.strip(), res.stderr.strip()
        except subprocess.CalledProcessError as e:
            logger.error(f"Git command failed: {' '.join(cmd)} -> {(e.stderr or '').strip()}")
            return e.returncode, (e.stdout or "").strip(), (e.stderr or "").strip()
        except Exception as e:
            logger.error(f"Execution error running git: {e}")
            return -1, "", str(e)

    # ------------------------------------------------------------------
    # Repository inspection
    # ------------------------------------------------------------------
    def get_current_head(self) -> Optional[str]:
        """Returns the current commit hash (HEAD)."""
        code, out, _ = self._run_git(["rev-parse", "HEAD"], check=False)
        return out if code == 0 and out else None

    def get_current_branch(self) -> Optional[str]:
        """Returns the name of the currently checked-out branch."""
        code, out, _ = self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return out if code == 0 and out else None

    def get_branch_tip(self, branch: str) -> Optional[str]:
        """Returns the commit a branch points at, or None when it does not exist."""
        code, out, _ = self._run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], check=False
        )
        return out if code == 0 and out else None

    # ------------------------------------------------------------------
    # Snapshot / branch preparation
    # ------------------------------------------------------------------
    def prepare_heal_branch(self) -> bool:
        """Point iow/auto-heal at the current HEAD without checking anything out.

        The peer stays on whatever branch they had checked out; the heal branch is
        simply a ref this cycle stacks commits onto. Because it is re-based on the live
        HEAD each cycle, a stale heal branch can never drag the working tree (or the
        agent's own source) backwards.
        """
        head = self.get_current_head()
        if not head:
            logger.error("Cannot prepare heal branch: repository has no HEAD commit.")
            return False

        if self.get_branch_tip(self.heal_branch) == head:
            logger.info(f"Heal branch '{self.heal_branch}' already at HEAD {head[:8]}.")
            return True

        code, _, err = self._run_git(
            ["update-ref", f"refs/heads/{self.heal_branch}", head], check=False
        )
        if code != 0:
            logger.error(f"Failed to point '{self.heal_branch}' at {head[:8]}: {err}")
            return False

        logger.info(
            f"Heal branch '{self.heal_branch}' re-based on HEAD {head[:8]} "
            "(working tree untouched)."
        )
        return True

    def snapshot_pre_loop(self) -> str:
        """Subject requirement: snapshot the original revision before the loop starts."""
        current_hash = self.get_current_head()
        if not current_hash:
            raise RuntimeError("Cannot snapshot: repository has no valid HEAD commit.")

        self._pre_loop_hash = current_hash
        self._initial_branch = self.get_current_branch()
        self._created_paths = []
        logger.info(
            f"Recorded pre-loop revision snapshot: {self._pre_loop_hash[:8]} "
            f"on branch '{self._initial_branch}'"
        )
        return self._pre_loop_hash

    # ------------------------------------------------------------------
    # Plumbing commit (never moves HEAD or the working tree)
    # ------------------------------------------------------------------
    def _blob_mode(self, rel_path: str) -> str:
        full = os.path.join(self.repo_dir, rel_path)
        if os.path.isfile(full) and os.access(full, os.X_OK):
            return "100755"
        return "100644"

    def commit_blobs_to_branch(
        self,
        branch: str,
        entries: Sequence[Tuple[str, Optional[str]]],
        message: str,
        parent: Optional[str] = None,
    ) -> Optional[str]:
        """Commit file contents onto a branch without touching HEAD or the work tree.

        entries: sequence of (rel_path, content). A content of None deletes the path.
        Returns the new commit hash, or None on failure.
        """
        if not entries:
            return None

        parent = parent or self.get_branch_tip(branch) or self.get_current_head()
        tmp_dir = tempfile.mkdtemp(prefix="iow-index-")
        tmp_index = os.path.join(tmp_dir, "index")
        env = self._git_env(index_file=tmp_index)

        try:
            if parent:
                code, _, err = self._run_git(["read-tree", parent], check=False, env=env)
                if code != 0:
                    logger.error(f"read-tree {parent[:8]} failed: {err}")
                    return None
            else:
                self._run_git(["read-tree", "--empty"], check=False, env=env)

            for rel_path, content in entries:
                if content is None:
                    self._run_git(
                        ["update-index", "--force-remove", "--", rel_path],
                        check=False, env=env,
                    )
                    continue

                code, sha, err = self._run_git(
                    ["hash-object", "-w", "--stdin", "--path", rel_path],
                    check=False, stdin_data=content,
                )
                if code != 0 or not sha:
                    logger.error(f"hash-object failed for {rel_path}: {err}")
                    return None

                code, _, err = self._run_git(
                    ["update-index", "--add", "--cacheinfo",
                     f"{self._blob_mode(rel_path)},{sha},{rel_path}"],
                    check=False, env=env,
                )
                if code != 0:
                    logger.error(f"update-index failed for {rel_path}: {err}")
                    return None

            code, tree, err = self._run_git(["write-tree"], check=False, env=env)
            if code != 0 or not tree:
                logger.error(f"write-tree failed: {err}")
                return None

            commit_args = ["commit-tree", tree]
            if parent:
                commit_args += ["-p", parent]
            commit_args += ["-m", message]
            code, commit, err = self._run_git(commit_args, check=False, env=env)
            if code != 0 or not commit:
                logger.error(f"commit-tree failed: {err}")
                return None

            code, _, err = self._run_git(
                ["update-ref", f"refs/heads/{branch}", commit], check=False
            )
            if code != 0:
                logger.error(f"update-ref refs/heads/{branch} failed: {err}")
                return None

            return commit
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Patch application
    # ------------------------------------------------------------------
    def apply_patch_atomically(self, patch: StructuredPatch) -> CommitResult:
        """Write the patch to the target's working tree and commit it on the heal branch.

        Files are written through a temp file + os.replace so a reader never observes a
        half-written file. Every path is validated against the heal roots first, so a
        model-proposed path can never escape demo_app/.
        """
        if not patch.files:
            return CommitResult(success=False, error_message="Patch contains no files to apply.")

        if not self.prepare_heal_branch():
            return CommitResult(
                success=False,
                error_message=f"Could not point heal branch '{self.heal_branch}' at HEAD.",
            )

        for file_patch in patch.files:
            if not self._is_heal_path(file_patch.path):
                return CommitResult(
                    success=False,
                    error_message=(
                        f"Refusing to patch '{file_patch.path}': resolves outside "
                        f"heal paths {list(self.heal_paths)}"
                    ),
                )

        modified_files: List[str] = []
        created_files: List[str] = []
        # Original bytes of every file we touch, so a mid-patch failure restores exactly.
        originals: Dict[str, Optional[str]] = {}
        commit_entries: List[Tuple[str, Optional[str]]] = []

        try:
            for file_patch in patch.files:
                rel_path = file_patch.path
                full_path = os.path.join(self.repo_dir, rel_path)
                op = file_patch.op

                if os.path.isfile(full_path):
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        originals[rel_path] = f.read()
                else:
                    originals[rel_path] = None

                if op in ("create", "modify"):
                    if originals[rel_path] is None:
                        created_files.append(rel_path)
                    os.makedirs(os.path.dirname(full_path) or self.repo_dir, exist_ok=True)
                    temp_path = full_path + ".iow_tmp"
                    with open(temp_path, "w", encoding="utf-8") as f:
                        f.write(file_patch.content)
                    os.replace(temp_path, full_path)
                    modified_files.append(rel_path)
                    commit_entries.append((rel_path, file_patch.content))
                    logger.debug(f"Atomically wrote {op} to {rel_path}")

                elif op == "delete":
                    if os.path.exists(full_path):
                        os.remove(full_path)
                    modified_files.append(rel_path)
                    commit_entries.append((rel_path, None))
                    logger.debug(f"Deleted file {rel_path}")

            clean_summary = patch.summary.strip().replace("\n", " ")
            commit_msg = f"fix(auto-heal): {clean_summary[:80]}"

            new_hash = self.commit_blobs_to_branch(
                branch=self.heal_branch,
                entries=commit_entries,
                message=commit_msg,
            )
            if not new_hash:
                raise RuntimeError("Could not create commit on the heal branch.")

            self._created_paths.extend(created_files)
            logger.info(
                f"Committed auto-heal patch {new_hash[:8]} on '{self.heal_branch}' "
                f"('{commit_msg}') without moving HEAD."
            )

            return CommitResult(
                success=True,
                commit_hash=new_hash,
                branch=self.heal_branch,
                files_modified=modified_files,
                commit_message=commit_msg,
            )

        except Exception as e:
            logger.error(f"Atomic patch application failed: {e}. Restoring touched files...")
            self._restore_originals(originals, created_files)
            return CommitResult(
                success=False,
                error_message=str(e),
                files_modified=modified_files,
            )

    def _restore_originals(
        self,
        originals: Dict[str, Optional[str]],
        created_files: Sequence[str],
    ) -> None:
        """Put back the exact bytes captured before writing."""
        for rel_path, content in originals.items():
            full_path = os.path.join(self.repo_dir, rel_path)
            try:
                if content is None:
                    if rel_path in created_files and os.path.exists(full_path):
                        os.remove(full_path)
                else:
                    temp_path = full_path + ".iow_tmp"
                    with open(temp_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    os.replace(temp_path, full_path)
            except Exception as err:
                logger.error(f"Could not restore {rel_path}: {err}")

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------
    def _restore_heal_paths_from(self, revision: str) -> bool:
        """Restore heal-target paths from revision.

        Only files this cycle created are removed - untracked files the peer placed
        under demo_app/ survive, so a failed heal never eats their work.
        """
        if not revision:
            return False

        # Snapshot operator-owned files so the restore cannot roll back their edits.
        preserved: Dict[str, Optional[str]] = {}
        for protected in self.protected_paths:
            full = os.path.join(self.repo_dir, protected)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        preserved[protected] = f.read()
                except OSError as exc:
                    logger.warning(f"Could not preserve {protected}: {exc}")

        paths = list(self.heal_paths)
        code, _, err = self._run_git(["checkout", revision, "--", *paths], check=False)
        if code != 0:
            logger.error(f"Failed restoring heal paths {paths} from {revision[:8]}: {err}")
            return False

        for protected, content in preserved.items():
            full = os.path.join(self.repo_dir, protected)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    if f.read() == content:
                        continue
                temp = full + ".iow_tmp"
                with open(temp, "w", encoding="utf-8") as f:
                    f.write(content or "")
                os.replace(temp, full)
                logger.info(f"Kept operator-owned {protected} across the rollback.")
            except OSError as exc:
                logger.error(f"Could not restore operator-owned {protected}: {exc}")

        # `git checkout <rev> -- <paths>` also stages them; unstage so the peer's index
        # is exactly as they left it.
        self._run_git(["reset", "--quiet", "--", *paths], check=False)

        for rel_path in list(self._created_paths):
            if not self._is_heal_path(rel_path):
                continue
            full_path = os.path.join(self.repo_dir, rel_path)
            code, _, _ = self._run_git(
                ["cat-file", "-e", f"{revision}:{rel_path}"], check=False
            )
            if code != 0 and os.path.isfile(full_path):
                try:
                    os.remove(full_path)
                    logger.info(f"Removed file created by this cycle: {rel_path}")
                except OSError as err:
                    logger.error(f"Could not remove {rel_path}: {err}")
        self._created_paths = []
        return True

    def rollback_to_pre_loop(self) -> bool:
        """Rewind the heal branch ref and restore demo_app/ to the pre-loop bytes.

        HEAD is never moved, so the peer's branch, index and WIP outside demo_app/ are
        untouched - and there is no branch to switch back to afterwards.
        """
        if not self._pre_loop_hash:
            logger.error("Cannot rollback: no pre-loop snapshot hash recorded.")
            return False

        pre = self._pre_loop_hash
        logger.warning(
            f"INITIATING ROLLBACK: rewinding '{self.heal_branch}' to {pre[:8]} "
            f"and restoring {list(self.heal_paths)} (HEAD and agent source untouched)..."
        )

        code, _, err = self._run_git(
            ["update-ref", f"refs/heads/{self.heal_branch}", pre], check=False
        )
        if code != 0:
            logger.error(f"Rollback update-ref failed: {err}")
            return False

        if not self._restore_heal_paths_from(pre):
            return False

        tip = self.get_branch_tip(self.heal_branch)
        if tip == pre:
            logger.info(
                f"ROLLBACK COMPLETE: '{self.heal_branch}' at {pre[:8]}; "
                f"restored {list(self.heal_paths)} byte for byte."
            )
            return True

        logger.error(f"Rollback verification mismatch: branch tip {tip}, expected {pre}")
        return False

    def rollback_to(self, revision: str) -> Optional[str]:
        """Rewind heal branch + restore heal paths to an explicit revision."""
        if not revision:
            return None
        code, resolved, err = self._run_git(
            ["rev-parse", "--verify", f"{revision}^{{commit}}"], check=False
        )
        if code != 0 or not resolved:
            logger.error(f"rollback_to: unknown revision '{revision}': {err}")
            return None

        code, _, err = self._run_git(
            ["update-ref", f"refs/heads/{self.heal_branch}", resolved], check=False
        )
        if code != 0:
            logger.error(f"rollback_to update-ref failed: {err}")
            return None
        if not self._restore_heal_paths_from(resolved):
            return None
        return resolved

    def get_pre_loop_hash(self) -> Optional[str]:
        """Returns the active pre-loop snapshot hash."""
        return self._pre_loop_hash

    def get_initial_branch(self) -> Optional[str]:
        """Branch the peer had checked out when the cycle started."""
        return self._initial_branch
