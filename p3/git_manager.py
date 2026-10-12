"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Git Manager: Atomic Patch Application, iow/auto-heal Branching & Snapshot Rollback

The heal branch is written with git plumbing (hash-object / commit-tree / update-ref)
so HEAD, the index and the checked-out branch are never moved. Only heal-target paths
(demo_app/) are ever written or restored; the peer's WIP elsewhere is left untouched.

The pre-loop revision is the state the target was *actually running*: when demo_app/
differs from the commit the heal branch starts from (an uncommitted planted bug, an
earlier heal that was never merged), that working tree is first recorded as a snapshot
commit. Rollback restores exactly that revision, byte for byte, through git.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple
from dataclasses import dataclass, field

from p3.patcher import StructuredPatch

logger = logging.getLogger("p3.git_manager")

DEFAULT_HEAL_BRANCH = "iow/auto-heal"
# The pre-loop revision of the cycle in progress - kept in git itself, because an
# agent that stops or crashes mid-cycle loses its memory and the repository does not.
CYCLE_REF = "refs/iow/pre-loop"
# Edits made to the heal paths outside the loop while a cycle ran, kept before the
# rollback restores the pre-loop bytes (one ref per rollback).
OVERWRITTEN_REF_PREFIX = "refs/iow/overwritten"
# GitOps: the pod-template annotation every agent commit sets to the git tree hash of the
# heal paths, so a code change is a manifest change and Argo CD's sync alone rolls the pod.
SOURCE_STAMP_RE = re.compile(r'^([ \t]*iow\.42/source:[ \t]*)"[^"\n]*"', re.MULTILINE)
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
        # Outcome of the last rollback's byte-for-byte check (dashboard / history).
        self.last_rollback_check: Optional[Dict[str, Any]] = None
        # GitOps mode: the manifest whose pod template carries the code's tree hash.
        self.manifest_stamp: Optional[str] = None

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

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        code, _, _ = self._run_git(
            ["merge-base", "--is-ancestor", ancestor, descendant], check=False
        )
        return code == 0

    def _tree_of(self, revision: str) -> Optional[str]:
        code, out, _ = self._run_git(["rev-parse", f"{revision}^{{tree}}"], check=False)
        return out if code == 0 and out else None

    def read_blob(self, revision: str, rel_path: str) -> Optional[str]:
        """The exact text of rel_path at revision, or None when it does not exist there.

        Not through _run_git: that strips its output, and a file's trailing newline is
        part of its content.
        """
        try:
            res = subprocess.run(
                ["git", "cat-file", "blob", f"{revision}:{rel_path}"],
                cwd=self.repo_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                check=False, env=self._git_env(),
            )
        except Exception as e:
            logger.error(f"Could not read {rel_path} at {revision[:8]}: {e}")
            return None
        if res.returncode != 0:
            return None
        return res.stdout.decode("utf-8", errors="replace")

    # ------------------------------------------------------------------
    # Snapshot / branch preparation
    # ------------------------------------------------------------------
    def _heal_base(self) -> Optional[str]:
        """The commit a new cycle builds on.

        The heal branch keeps stacking while it still sits on top of HEAD (earlier
        verified heals stay in its history). Once HEAD has moved elsewhere - the peer
        committed or merged - the branch is re-based on the live HEAD, so a stale heal
        line can never drag the target backwards.
        """
        head = self.get_current_head()
        if not head:
            return None
        tip = self.get_branch_tip(self.heal_branch)
        if tip and tip != head and self._is_ancestor(head, tip):
            return tip
        return head

    def prepare_heal_branch(self) -> bool:
        """Point iow/auto-heal at the cycle's base commit without checking anything out."""
        base = self._heal_base()
        if not base:
            logger.error("Cannot prepare heal branch: repository has no HEAD commit.")
            return False
        if self.get_branch_tip(self.heal_branch) == base:
            return True
        code, _, err = self._run_git(
            ["update-ref", f"refs/heads/{self.heal_branch}", base], check=False
        )
        if code != 0:
            logger.error(f"Failed to point '{self.heal_branch}' at {base[:8]}: {err}")
            return False
        logger.info(
            f"Heal branch '{self.heal_branch}' based on {base[:8]} (working tree untouched)."
        )
        return True

    def _working_tree_snapshot(self, base: str) -> Optional[str]:
        """Tree of `base` with the heal paths replaced by their working-tree state.

        Built in a throwaway index (`git add -A` honours .gitignore), so neither the
        real index nor the working tree is touched.
        """
        tmp_dir = tempfile.mkdtemp(prefix="iow-snapshot-")
        env = self._git_env(index_file=os.path.join(tmp_dir, "index"))
        try:
            code, _, err = self._run_git(["read-tree", base], check=False, env=env)
            if code != 0:
                logger.error(f"read-tree {base[:8]} failed: {err}")
                return None
            code, _, err = self._run_git(
                ["add", "-A", "--", *self.heal_paths], check=False, env=env
            )
            if code != 0:
                logger.error(f"Could not stage the heal paths for the snapshot: {err}")
                return None
            code, tree, err = self._run_git(["write-tree"], check=False, env=env)
            if code != 0 or not tree:
                logger.error(f"write-tree failed for the snapshot: {err}")
                return None
            return tree
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def working_tree_commit(self, base: Optional[str] = None) -> str:
        """A commit whose heal paths are exactly what is on disk right now.

        Returns `base` itself when the working tree matches it; otherwise a snapshot
        commit on top of it. No ref is moved.
        """
        base = base or self._heal_base()
        if not base:
            raise RuntimeError("Cannot snapshot: repository has no valid HEAD commit.")
        tree = self._working_tree_snapshot(base)
        if tree is None:
            raise RuntimeError("Cannot snapshot the working tree of the heal paths.")
        if tree == self._tree_of(base):
            return base
        tree = self._stamped_tree(tree)
        code, commit, err = self._run_git(
            ["commit-tree", tree, "-p", base, "-m",
             f"iow: pre-loop snapshot of {', '.join(self.heal_paths)}/\n\n"
             "The working tree differed from the heal branch base when the Wisdom "
             "Loop started;\nthis commit records that exact state so a rollback can "
             "restore it."],
            check=False,
        )
        if code != 0 or not commit:
            raise RuntimeError(f"Could not record the pre-loop snapshot: {err}")
        logger.info(
            f"Working tree of {list(self.heal_paths)} differs from {base[:8]}: "
            f"recorded it as snapshot {commit[:8]}."
        )
        return commit

    def snapshot_pre_loop(self) -> str:
        """Subject requirement: snapshot the original revision before the loop starts.

        Returns the pre-loop commit - the exact state of the heal paths the target is
        running right now - and points the heal branch at it. When the working tree
        matches the base commit no extra commit is made.
        """
        pre_loop = self.working_tree_commit()

        code, _, err = self._run_git(
            ["update-ref", f"refs/heads/{self.heal_branch}", pre_loop], check=False
        )
        if code != 0:
            raise RuntimeError(f"Could not point '{self.heal_branch}' at {pre_loop[:8]}: {err}")
        # Kept in git, not only in memory: see recover_interrupted_cycle.
        self._run_git(["update-ref", CYCLE_REF, pre_loop], check=False)

        self._pre_loop_hash = pre_loop
        self._initial_branch = self.get_current_branch()
        self._created_paths = []
        logger.info(
            f"Recorded pre-loop revision {pre_loop[:8]} "
            f"(HEAD stays on '{self._initial_branch}', heal branch '{self.heal_branch}')."
        )
        return pre_loop

    # ------------------------------------------------------------------
    # GitOps: the commit alone redeploys
    # ------------------------------------------------------------------
    def _stamp_manifest(self, env: Dict[str, str]) -> None:
        """Set the manifest's source stamp to the heal paths' tree hash, in this index.

        The init container clones the code at pod start, so a heal changed no manifest:
        Argo CD had nothing to roll, and the agent restarted the rollout itself. With
        the stamp the pod template changes with the code - the commit alone is the
        redeploy, Argo CD's own sync rolls the pod. Only commits are stamped, never the
        working tree.
        """
        if not self.manifest_stamp:
            return
        code, source, _ = self._run_git(
            ["write-tree", f"--prefix={self.heal_paths[0]}/"], check=False, env=env
        )
        code2, entry, _ = self._run_git(
            ["ls-files", "-s", "--", self.manifest_stamp], check=False, env=env
        )
        if code != 0 or code2 != 0 or not source or not entry:
            return
        mode, blob = entry.split()[:2]
        text = self._cat_blob(blob)
        if text is None:
            return
        stamped, count = SOURCE_STAMP_RE.subn(lambda m: f'{m.group(1)}"{source}"', text)
        if not count or stamped == text:
            return
        code, sha, err = self._run_git(
            ["hash-object", "-w", "--stdin", "--path", self.manifest_stamp],
            check=False, stdin_data=stamped,
        )
        if code == 0 and sha:
            self._run_git(
                ["update-index", "--cacheinfo", f"{mode},{sha},{self.manifest_stamp}"],
                check=False, env=env,
            )
        else:
            logger.warning(f"Could not stamp {self.manifest_stamp}: {err}")

    def _stamped_tree(self, tree: str) -> str:
        """`tree` with the manifest stamped (unchanged when there is nothing to stamp)."""
        if not self.manifest_stamp:
            return tree
        tmp_dir = tempfile.mkdtemp(prefix="iow-stamp-")
        env = self._git_env(index_file=os.path.join(tmp_dir, "index"))
        try:
            if self._run_git(["read-tree", tree], check=False, env=env)[0] != 0:
                return tree
            self._stamp_manifest(env)
            code, stamped, _ = self._run_git(["write-tree"], check=False, env=env)
            return stamped if code == 0 and stamped else tree
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _cat_blob(self, blob: str) -> Optional[str]:
        """A blob's exact text (not through _run_git, which strips the trailing newline)."""
        try:
            res = subprocess.run(
                ["git", "cat-file", "blob", blob], cwd=self.repo_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=self._git_env(),
            )
        except Exception:
            return None
        return res.stdout.decode("utf-8", errors="replace") if res.returncode == 0 else None

    def source_stamp(self, revision: str) -> Optional[str]:
        """The manifest's source stamp at `revision` (None without a manifest or stamp)."""
        if not self.manifest_stamp:
            return None
        text = self.read_blob(revision, self.manifest_stamp)
        match = SOURCE_STAMP_RE.search(text or "")
        return match.group(0).split('"')[1] if match else None

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

            self._stamp_manifest(env)
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
    @staticmethod
    def _atomic_write(full_path: str, content: str) -> None:
        """Write via <file>.iow.tmp + fsync + os.replace, keeping the file's mode.

        A reader (the target reloading) never sees a half-written file, and an
        executable script stays executable.
        """
        os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
        temp_path = full_path + ".iow.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            if os.path.exists(full_path):
                shutil.copymode(full_path, temp_path)
            os.replace(temp_path, full_path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def apply_patch_atomically(
        self, patch: StructuredPatch, message: Optional[str] = None
    ) -> CommitResult:
        """Write the patch to the target's working tree and commit it on the heal branch.

        Every path is validated against the heal roots first, so a model-proposed path
        can never escape demo_app/. Commits stack on the heal branch: attempt 2 builds
        on attempt 1, so the branch tip always matches what is deployed.
        """
        if not patch.files:
            return CommitResult(success=False, error_message="Patch contains no files to apply.")

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
                    self._atomic_write(full_path, file_patch.content)
                    modified_files.append(rel_path)
                    commit_entries.append((rel_path, file_patch.content))
                    logger.debug(f"Atomically wrote {op} to {rel_path}")

                elif op == "delete":
                    if os.path.exists(full_path):
                        os.remove(full_path)
                    modified_files.append(rel_path)
                    commit_entries.append((rel_path, None))
                    logger.debug(f"Deleted file {rel_path}")

            if message and message.strip():
                commit_msg = message.strip()
            else:
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
                f"('{commit_msg.splitlines()[0]}') without moving HEAD."
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
                    self._atomic_write(full_path, content)
            except Exception as err:
                logger.error(f"Could not restore {rel_path}: {err}")

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------
    def _checkout_heal_paths(self, revision: str) -> bool:
        """Write the heal paths of `revision` into the working tree, byte for byte.

        Uses a throwaway index (read-tree + checkout-index), so the peer's real index
        - and everything outside the heal paths - is never touched. Operator-owned
        files keep whatever the operator has right now.
        """
        preserved: Dict[str, str] = {}
        for protected in self.protected_paths:
            full = os.path.join(self.repo_dir, protected)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        preserved[protected] = f.read()
                except OSError as exc:
                    logger.warning(f"Could not preserve {protected}: {exc}")

        tmp_dir = tempfile.mkdtemp(prefix="iow-restore-")
        env = self._git_env(index_file=os.path.join(tmp_dir, "index"))
        try:
            code, _, err = self._run_git(["read-tree", revision], check=False, env=env)
            if code != 0:
                logger.error(f"read-tree {revision[:8]} failed: {err}")
                return False
            code, listing, err = self._run_git(
                ["ls-files", "-z", "--", *self.heal_paths], check=False, env=env
            )
            if code != 0:
                logger.error(f"Could not list heal paths of {revision[:8]}: {err}")
                return False
            if listing:
                # -z output is NUL-terminated already (NUL is not stripped as space).
                stdin_data = listing if listing.endswith("\0") else listing + "\0"
                code, _, err = self._run_git(
                    ["checkout-index", "-f", "-z", "--stdin"],
                    check=False, env=env, stdin_data=stdin_data,
                )
                if code != 0:
                    logger.error(
                        f"Failed restoring {list(self.heal_paths)} from {revision[:8]}: {err}"
                    )
                    return False
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        for protected, content in preserved.items():
            full = os.path.join(self.repo_dir, protected)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    if f.read() == content:
                        continue
                self._atomic_write(full, content)
                logger.info(f"Kept operator-owned {protected} as the operator left it.")
            except OSError as exc:
                logger.error(f"Could not restore operator-owned {protected}: {exc}")
        return True

    def materialize_heal_paths(self, revision: str) -> bool:
        """Bring the heal paths in the working tree to `revision` (e.g. a merged PR)."""
        return bool(revision) and self._checkout_heal_paths(revision)

    def _restore_heal_paths_from(self, revision: str) -> bool:
        """Restore heal-target paths from revision.

        Only files this cycle created are removed - untracked files the peer placed
        under demo_app/ survive, so a failed heal never eats their work.
        """
        if not revision:
            return False

        if not self._checkout_heal_paths(revision):
            return False

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

    # ------------------------------------------------------------------
    # A cycle the agent did not live to finish
    # ------------------------------------------------------------------
    def mark_cycle_finished(self) -> None:
        """The cycle ended - healed or rolled back: nothing left to recover."""
        self._run_git(["update-ref", "-d", CYCLE_REF], check=False)

    def interrupted_cycle(self) -> Optional[str]:
        """The pre-loop revision of a cycle that started and never finished, if any."""
        code, out, _ = self._run_git(
            ["rev-parse", "--verify", "--quiet", f"{CYCLE_REF}^{{commit}}"], check=False
        )
        return out.strip() or None if code == 0 else None

    def recover_interrupted_cycle(self) -> Optional[Dict[str, Any]]:
        """Undo a cycle the agent stopped or crashed in the middle of.

        Its patch - written, maybe committed, never verified - is still in the working
        tree, and the next cycle would snapshot it as the state to restore (seen live).
        The tree is put back only when it is exactly what the agent left (its last
        commit) or already the pre-loop state; anything else means someone changed it
        since, and their work is not overwritten. The heal branch is rewound either way:
        its tip must never be an unverified attempt.
        """
        pre = self.interrupted_cycle()
        if not pre:
            return None
        tip = self.get_branch_tip(self.heal_branch)
        # Operator-owned files aside: an edit to iow.config.yml (a shorter grace period is
        # the subject's own example) made the tree look like someone else's work, and the
        # unverified patch stayed on disk for the next cycle to take as pre-loop (seen in
        # this audit). The rollback keeps the operator's file anyway.
        agent_state = bool(tip) and self.heal_paths_at(tip or pre)
        untouched = self.heal_paths_at(pre)

        self._pre_loop_hash = pre
        self._created_paths = []
        if agent_state or untouched:
            logger.warning(
                f"A heal cycle was interrupted (the agent stopped mid-cycle): rolling back "
                f"to its pre-loop revision {pre[:8]}."
            )
            outcome = "rolled_back" if self.rollback_to_pre_loop() else "rollback_failed"
        else:
            self._run_git(["update-ref", f"refs/heads/{self.heal_branch}", pre], check=False)
            outcome = "left_alone"
            logger.warning(
                f"A heal cycle was interrupted, but {list(self.heal_paths)} changed since "
                f"(neither its pre-loop state nor the agent's last patch): left as it is; "
                f"only '{self.heal_branch}' was rewound to {pre[:8]}."
            )
        self.mark_cycle_finished()
        return {
            "revision": pre,
            "outcome": outcome,
            "rollback_check": self.last_rollback_check if outcome == "rolled_back" else None,
        }

    def _save_outside_edits(self, agent_state: str) -> Optional[Dict[str, Any]]:
        """Keep edits made to the heal paths outside the loop before a rollback.

        The rollback has to restore the pre-loop bytes (subject), so an edit someone made
        while the cycle ran cannot stay on disk (seen during the audit: it was simply
        gone). It need not be lost either: whatever differs from the agent's own last
        commit is committed under refs/iow/overwritten/<time> first.
        """
        same, differing = self.verify_heal_paths_match(agent_state)
        edits = [
            path for path in differing
            if path not in self.protected_paths and not path.startswith("<")
        ]
        if same or not edits:
            return None
        try:
            saved = self.working_tree_commit(base=agent_state)
        except RuntimeError as err:
            logger.error(f"Could not save the edits made outside the loop ({edits}): {err}")
            return {"paths": edits, "saved_as": None}
        ref = f"{OVERWRITTEN_REF_PREFIX}/{int(time.time())}"
        self._run_git(["update-ref", ref, saved], check=False)
        logger.warning(
            f"{edits} changed outside the loop while the cycle ran: saved as {ref} "
            f"({saved[:8]}) before the rollback restores the pre-loop bytes."
        )
        return {"paths": edits, "saved_as": ref, "commit": saved}

    def rollback_to_pre_loop(self) -> bool:
        """Rewind the heal branch ref and restore demo_app/ to the pre-loop bytes.

        HEAD is never moved, so the peer's branch, index and WIP outside demo_app/ are
        untouched - and there is no branch to switch back to afterwards. Edits someone
        made to demo_app/ during the cycle are committed out of harm's way first.
        """
        if not self._pre_loop_hash:
            logger.error("Cannot rollback: no pre-loop snapshot hash recorded.")
            return False

        tip = self.get_branch_tip(self.heal_branch)
        outside_edits = self._save_outside_edits(tip) if tip else None
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
        if tip != pre:
            logger.error(f"Rollback verification mismatch: branch tip {tip}, expected {pre}")
            return False

        identical, differing = self.verify_heal_paths_match(pre)
        self.last_rollback_check = {
            "revision": pre,
            "identical": identical,
            "differing_paths": differing,
            "outside_edits": outside_edits,
        }
        if identical:
            logger.info(
                f"ROLLBACK COMPLETE: '{self.heal_branch}' at {pre[:8]}; working tree of "
                f"{list(self.heal_paths)} verified identical to it (git tree hash)."
            )
        else:
            logger.warning(
                f"ROLLBACK COMPLETE at {pre[:8]}, but these paths differ from it: "
                f"{differing} (operator-owned edits or files added during the cycle)."
            )
        return True

    def heal_paths_at(self, revision: str) -> bool:
        """Whether the heal paths on disk are exactly `revision` - operator-owned files aside."""
        same, differing = self.verify_heal_paths_match(revision)
        return same or (bool(differing) and all(path in self.protected_paths for path in differing))

    def verify_heal_paths_match(self, revision: str) -> Tuple[bool, List[str]]:
        """Does the working tree of the heal paths hash to exactly `revision`'s tree?"""
        tree = self._working_tree_snapshot(revision)
        expected = self._tree_of(revision)
        if tree is None or expected is None:
            return False, ["<could not compute tree>"]
        if tree == expected:
            return True, []
        code, out, _ = self._run_git(
            ["diff-tree", "-r", "--name-only", expected, tree], check=False
        )
        return False, [line for line in out.splitlines() if line] if code == 0 else ["<diff failed>"]

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
