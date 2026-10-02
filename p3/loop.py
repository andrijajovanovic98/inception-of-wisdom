"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Wisdom Loop Controller: 3-Attempt Healing Orchestrator & Automatic Rollback

Every attempt learns from the previous one: a verification failure hands the new
error log back to the Analyst, and a refused patch (sanity violation, truncation,
unreadable suspect file) hands the refusal reason back to the Patcher. No attempt is
ever a byte-identical repeat of the one before it.
"""

from __future__ import annotations

import os
import re
import uuid
import time
import difflib
import threading
import logging
from typing import Optional, List, Dict, Any, Callable
from dataclasses import dataclass, field, asdict

from p1.event_manager import ObserverEvent
from p1.docker_monitor import DockerMonitor
from p2.diagnostician import CrashDiagnostician, DiagnosticReport
from p3.patcher import CodePatcher, StructuredPatch
from p3.sanity import SanityChecker, SanityResult
from p3.git_manager import GitManager, CommitResult
from p3.verifier import TargetVerifier, VerificationResult, failing_requests_from_log

logger = logging.getLogger("p3.loop")

# Sampling temperature of the patch request, per attempt.
PATCH_TEMPERATURES = (0.1, 0.4, 0.7)
# Quoted text in code: how a patch's own error message is recognised in the next log.
_STRING_LITERAL_RE = re.compile(r"""["']([^"'\n{}]+)["']""")


@dataclass
class AttemptRecord:
    """Detailed log of a single heal attempt within a cycle."""
    attempt_number: int                          # 1, 2, or 3
    diagnosis: Optional[Dict[str, Any]] = None   # Structured diagnosis JSON
    patch: Optional[Dict[str, Any]] = None       # Structured patch JSON
    commit_hash: Optional[str] = None            # Git commit sha on iow/auto-heal
    sanity: Optional[Dict[str, Any]] = None      # Sanity check results
    verification: Optional[Dict[str, Any]] = None  # Post-restart health status
    source: str = "llm"                          # 'llm' or 'classifier_fast_path'
    error_message: Optional[str] = None
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HealCycleRecord:
    """Historical record of an entire heal cycle (up to 3 attempts)."""
    cycle_id: str
    pre_loop_hash: str
    status: str  # 'in_progress', 'healed', 'rolled_back', 'refused', 'awaiting_review', 'not_needed'
    initial_crash: Dict[str, Any]
    attempts: List[AttemptRecord] = field(default_factory=list)
    final_commit_hash: Optional[str] = None
    failure_reason: Optional[str] = None
    start_time: float = 0.0
    end_time: Optional[float] = None
    heal_branch: Optional[str] = None
    # The request(s) that crashed the target, re-issued after every redeploy.
    symptom_check: List[str] = field(default_factory=list)
    # After a rollback: did the working tree hash back to the pre-loop revision, and
    # did the target come back on it?
    rollback_check: Optional[Dict[str, Any]] = None
    post_rollback: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "pre_loop_hash": self.pre_loop_hash,
            "status": self.status,
            "initial_crash": self.initial_crash,
            "attempts": [a.to_dict() for a in self.attempts],
            "final_commit_hash": self.final_commit_hash,
            "failure_reason": self.failure_reason,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "heal_branch": self.heal_branch,
            "symptom_check": list(self.symptom_check),
            "rollback_check": self.rollback_check,
            "post_rollback": self.post_rollback,
        }


def _first_sentence(text: str, limit: int = 72) -> str:
    """Short commit subject out of a one-paragraph diagnosis."""
    flat = " ".join((text or "").split())
    match = re.match(r"(.+?[.!?])(\s|$)", flat)
    sentence = match.group(1) if match else flat
    if len(sentence) > limit:
        sentence = sentence[: limit - 3].rstrip() + "..."
    return sentence or "repair crashing target"


def commit_message_for(
    diagnosis: Optional[DiagnosticReport],
    patch: StructuredPatch,
    cycle_id: str,
    attempt: int,
    max_attempts: int,
) -> str:
    """Subject: "a short message generated from the diagnosis"."""
    subject_src = diagnosis.summary if diagnosis and diagnosis.summary else patch.summary
    lines = [f"fix(auto-heal): {_first_sentence(subject_src)}", ""]
    if diagnosis and diagnosis.summary:
        lines.append(f"Diagnosis: {' '.join(diagnosis.summary.split())}")
    if patch.summary:
        lines.append(f"Patch: {' '.join(patch.summary.split())}")
    lines.append("Files: " + ", ".join(f"{f.path} ({f.op})" for f in patch.files))
    lines.append(f"Cycle: {cycle_id} attempt {attempt}/{max_attempts}")
    return "\n".join(lines)


class WisdomLoop:
    """Orchestrates the 4 beats of autonomous healing:
    Observe (P1) -> Think (P2) -> Act (P3) -> Verify (P3).
    Limits heal attempts to 3; on failure, performs an immediate hard rollback.
    """

    def __init__(
        self,
        git_manager: GitManager,
        diagnostician: CrashDiagnostician,
        patcher: CodePatcher,
        sanity_checker: SanityChecker,
        verifier: TargetVerifier,
        docker_monitor: DockerMonitor,
        max_attempts: int = 3,
        max_history: int = 50,
    ):
        self.git_manager = git_manager
        self.diagnostician = diagnostician
        self.patcher = patcher
        self.sanity_checker = sanity_checker
        self.verifier = verifier
        self.docker_monitor = docker_monitor
        self.max_attempts = max_attempts
        self.max_history = max_history

        self.history: List[HealCycleRecord] = []
        self._run_lock = threading.Lock()
        self._is_running_cycle: bool = False

    def is_active(self) -> bool:
        """Returns True if a heal cycle is currently in progress."""
        with self._run_lock:
            return self._is_running_cycle

    def _try_claim_cycle(self) -> bool:
        """Atomic test-and-set so two threads can never run a cycle at once."""
        with self._run_lock:
            if self._is_running_cycle:
                return False
            self._is_running_cycle = True
            return True

    def _release_cycle(self) -> None:
        with self._run_lock:
            self._is_running_cycle = False

    def claim(self) -> bool:
        """Take the cycle slot for work that writes the target's tree outside a heal
        cycle (landing an approved PR), so a heal can never interleave with it."""
        return self._try_claim_cycle()

    def release(self) -> None:
        """Give back a slot taken with claim()."""
        self._release_cycle()

    def _record(self, cycle: HealCycleRecord) -> None:
        self.history.append(cycle)
        if len(self.history) > self.max_history:
            del self.history[: len(self.history) - self.max_history]

    def recover_interrupted_cycle(self, redeploy: bool = True) -> Optional[HealCycleRecord]:
        """On start-up: undo a cycle the previous agent did not live to finish.

        A stop or crash between the first patch and the verdict leaves an unverified
        patch in the target's tree; without this the next cycle snapshots it as the
        pre-loop state, and the rollback guarantee is gone (seen live, twice).

        The git side is instant. Restarting the target can wait out a whole boot
        timeout - the pre-loop code is often the broken one - so a caller that must
        stay responsive passes redeploy=False and calls redeploy_recovered() later.
        """
        if not self.git_manager.interrupted_cycle() or not self._try_claim_cycle():
            return None
        try:
            recovered = self.git_manager.recover_interrupted_cycle()
            if not recovered:
                return None
            pre = recovered["revision"]
            record = HealCycleRecord(
                cycle_id="recovery",
                pre_loop_hash=pre,
                status="rolled_back",
                initial_crash={
                    "source": "agent",
                    "summary": "The agent stopped in the middle of a heal cycle",
                },
                start_time=time.time(),
                heal_branch=self.git_manager.heal_branch,
                final_commit_hash=pre,
                rollback_check=recovered.get("rollback_check"),
                failure_reason=(
                    "Interrupted: the agent stopped before this cycle finished. "
                    + ("Its unverified patch was rolled back on the next start."
                       if recovered["outcome"] == "rolled_back" else
                       "The target's files had changed since, so they were left as they are.")
                ),
            )
            record.end_time = time.time()
            self._record(record)
        finally:
            self._release_cycle()
        if redeploy and recovered["outcome"] == "rolled_back":
            self.redeploy_recovered(record)
        return record

    def redeploy_recovered(self, record: HealCycleRecord) -> None:
        """Restart the target on the pre-loop code a recovery put back."""
        if not self._try_claim_cycle():
            return
        try:
            post = self.verifier.restart_and_verify(grace_period=0.0)
            record.post_rollback = {
                "restarted_on": record.pre_loop_hash,
                "target_up": post.is_healed,
                "status": post.status,
                "container_status": post.container_status,
            }
            record.end_time = time.time()
        finally:
            self._release_cycle()

    # ------------------------------------------------------------------
    def execute_heal(
        self,
        crash_event: ObserverEvent,
        grace_period: float = 20.0,
        cycle_progress_cb: Optional[Callable[[str, int, int], None]] = None,
        preset_diagnosis: Optional[DiagnosticReport] = None,
        fast_path_patch: Optional[StructuredPatch] = None,
        diagnose_fn: Optional[Callable[..., DiagnosticReport]] = None,
    ) -> HealCycleRecord:
        """Runs the complete self-healing cycle up to 3 attempts.

        preset_diagnosis reuses a diagnosis already agreed by the consensus engine.
        fast_path_patch applies a classifier-cached patch on attempt 1 without asking
        the model at all; if it fails verification the loop falls back to the LLM.
        diagnose_fn replaces the Analyst for every attempt that needs a diagnosis (the
        second-opinion mode: each retry is voted on too, not only the first attempt).
        """
        if not self._try_claim_cycle():
            logger.warning(
                "A heal cycle is already running (single-flight enforced). "
                "Rejecting concurrent run."
            )
            return HealCycleRecord(
                cycle_id=str(uuid.uuid4())[:8],
                pre_loop_hash=self.git_manager.get_current_head() or "unknown",
                status="refused",
                initial_crash=crash_event.to_dict(),
                failure_reason="Another heal cycle is already running.",
                start_time=time.time(),
                end_time=time.time()
            )

        cycle_id = str(uuid.uuid4())[:8]
        start_now = time.time()

        current_error_log = (
            crash_event.details.get("context_excerpt")
            or crash_event.details.get("matched_line")
            or crash_event.details.get("log_excerpt")
            or crash_event.summary
        )
        current_exit_code = crash_event.details.get("exit_code")
        current_status = crash_event.details.get("status", "crashed")
        # "Decides whether the symptom is gone": the request that crashed the target
        # is replayed after each redeploy (see TargetVerifier).
        probe_paths = getattr(getattr(self.verifier, "http_probe", None), "probe_paths", None) or []
        replay = failing_requests_from_log(current_error_log or "", ignore_paths=probe_paths)

        # A crash that is already gone has nothing left to heal: patching now could only
        # change working code (a transient blip, an old event picked by hand, a sibling
        # signature of a crash an earlier heal fixed).
        check = getattr(self.verifier, "symptom_present", None)
        if callable(check):
            present, why = check(replay)
            if not present:
                logger.info(f"Nothing to heal: the symptom is gone ({why}). Nothing was changed.")
                self._release_cycle()
                return self.record_refused_cycle(
                    crash_event,
                    f"The symptom is gone ({why}), so nothing was patched or restarted.",
                    status="not_needed",
                )

        # Record the exact state the target runs right now (a snapshot commit when the
        # working tree differs) and point the heal branch at it. Nothing is checked
        # out, so the peer's branch, index and working tree stay where they were.
        try:
            pre_loop_hash = self.git_manager.snapshot_pre_loop()
        except Exception as e:
            logger.error(f"Failed to prepare heal branch / pre-loop snapshot: {e}")
            self._release_cycle()
            raise

        cycle_record = HealCycleRecord(
            cycle_id=cycle_id,
            pre_loop_hash=pre_loop_hash,
            status="in_progress",
            initial_crash=crash_event.to_dict(),
            start_time=start_now,
            heal_branch=self.git_manager.heal_branch,
        )
        cycle_record.symptom_check = [f"{method} {path}" for method, path in replay]
        self._record(cycle_record)

        logger.info(
            f"=== STARTING WISDOM LOOP CYCLE [{cycle_id}] "
            f"(pre-loop hash: {pre_loop_hash[:8]}, branch: {self.git_manager.heal_branch}) ==="
        )
        goal = (
            f"after your change {', '.join(cycle_record.symptom_check)} must succeed: no "
            "exception, no error or CRITICAL log line, and a normal 2xx response."
            if replay else
            "after your change the service must start and keep running without this error."
        )

        target_facts = self._target_facts()
        is_healed = False
        last_failure: Optional[str] = None
        # Why the previous attempt was rejected, fed back into the next prompt.
        refusal_hint: Optional[str] = None

        try:
            for attempt_num in range(1, self.max_attempts + 1):
                logger.info(f"--- HEAL ATTEMPT {attempt_num}/{self.max_attempts} ---")
                if cycle_progress_cb:
                    cycle_progress_cb("attempt_start", attempt_num, self.max_attempts)

                attempt_record = AttemptRecord(
                    attempt_number=attempt_num,
                    timestamp=time.time()
                )
                cycle_record.attempts.append(attempt_record)

                use_fast_path = attempt_num == 1 and fast_path_patch is not None
                diagnosis: Optional[DiagnosticReport] = None

                if use_fast_path:
                    attempt_record.source = "classifier_fast_path"
                    patch = fast_path_patch
                    assert patch is not None
                    attempt_record.patch = patch.to_dict()
                    logger.info(
                        "Attempt %d uses the classifier's cached patch - the LLM is "
                        "skipped entirely for this known symptom.", attempt_num
                    )
                else:
                    # Beat 2: Think - retrieve code & ask the LLM for a diagnosis.
                    if preset_diagnosis is not None and attempt_num == 1:
                        diagnosis = preset_diagnosis
                        logger.info("Attempt 1 reuses the consensus-agreed diagnosis.")
                    else:
                        diagnose = diagnose_fn or self.diagnostician.diagnose_crash
                        diagnosis = diagnose(
                            log_excerpt=current_error_log,
                            exit_code=current_exit_code,
                            status=current_status,
                            exclude_files=getattr(self.git_manager, "protected_paths", ()),
                        )
                    attempt_record.diagnosis = diagnosis.to_dict()

                    if not diagnosis.success:
                        msg = f"Diagnosis failed: {diagnosis.error_message}"
                        logger.warning(f"Attempt {attempt_num}: {msg}")
                        attempt_record.error_message = msg
                        last_failure = msg
                        refusal_hint = None
                        continue

                    # Beat 3: Act - generate a structured JSON patch. A retry samples
                    # more freely: at a fixed low temperature a nearly identical prompt
                    # gets the same answer again (seen live: the same no-op twice).
                    patch = self.patcher.generate_patch(
                        diagnosis=diagnosis,
                        log_excerpt=current_error_log,
                        refusal_hint=refusal_hint,
                        goal=goal,
                        temperature=PATCH_TEMPERATURES[
                            min(attempt_num, len(PATCH_TEMPERATURES)) - 1
                        ],
                        facts=target_facts,
                    )
                    attempt_record.patch = patch.to_dict()

                    if not patch.success:
                        msg = f"Patch generation failed: {patch.error_message}"
                        logger.warning(f"Attempt {attempt_num}: {msg}")
                        attempt_record.error_message = msg
                        last_failure = msg
                        refusal_hint = patch.error_message
                        continue

                # The reviewable form of the change (bonus: patch diff in the dashboard).
                if attempt_record.patch is not None:
                    attempt_record.patch["diff"] = self._render_diff(patch)

                # Sanity bounds check (pre-disk write)
                sanity_res: SanityResult = self.sanity_checker.check_patch(patch)
                attempt_record.sanity = sanity_res.to_dict()

                if not sanity_res.passed:
                    reasons = "; ".join(sanity_res.reasons)
                    logger.warning(f"Attempt {attempt_num}: Sanity check REFUSED patch: {reasons}")
                    attempt_record.error_message = f"Sanity check failed: {reasons}"
                    last_failure = attempt_record.error_message
                    refusal_hint = reasons
                    if use_fast_path:
                        # The cached patch no longer fits the code; ask the model.
                        fast_path_patch = None
                    continue

                # Apply atomically and commit on the heal branch.
                commit_res: CommitResult = self.git_manager.apply_patch_atomically(
                    patch,
                    message=commit_message_for(
                        diagnosis, patch, cycle_id, attempt_num, self.max_attempts
                    ),
                )
                attempt_record.commit_hash = commit_res.commit_hash

                if not commit_res.success:
                    msg = f"Git commit failed: {commit_res.error_message}"
                    logger.error(f"Attempt {attempt_num}: {msg}")
                    attempt_record.error_message = msg
                    last_failure = msg
                    refusal_hint = commit_res.error_message
                    if use_fast_path:
                        fast_path_patch = None
                    continue

                # Beat 4: Verify - redeploy the target and watch the grace period.
                verify_res: VerificationResult = self.verifier.restart_and_verify(
                    grace_period=grace_period, replay=replay
                )
                attempt_record.verification = verify_res.to_dict()

                if verify_res.is_healed:
                    commit_label = (commit_res.commit_hash or "?")[:8]
                    logger.info(
                        f"HEAL SUCCESS on attempt {attempt_num}: target healthy for the "
                        f"full {grace_period}s on commit {commit_label}."
                    )
                    cycle_record.status = "healed"
                    cycle_record.final_commit_hash = commit_res.commit_hash
                    cycle_record.end_time = time.time()
                    is_healed = True
                    break

                # Failure: hand the new error log back to the Analyst.
                logger.warning(
                    f"Attempt {attempt_num} FAILED: target did not stay healthy "
                    f"(status: {verify_res.status})"
                )
                current_error_log = (
                    verify_res.error_log or "Target failed to stay up during verification."
                )
                current_status = verify_res.container_status or "crashed"
                attempt_record.error_message = (
                    f"Verification failed ({verify_res.status}): {current_error_log[:150]}"
                )
                last_failure = attempt_record.error_message
                refusal_hint = self._verification_hint(
                    verify_res, (attempt_record.patch or {}).get("diff", "")
                )
                if use_fast_path:
                    fast_path_patch = None

            if not is_healed:
                logger.error(
                    f"All {self.max_attempts} attempts failed. Executing ROLLBACK "
                    f"to pre-loop revision {pre_loop_hash[:8]}..."
                )
                rollback_success = self.git_manager.rollback_to_pre_loop()
                cycle_record.rollback_check = self.git_manager.last_rollback_check

                # Restart the target on the original code.
                post = self.verifier.restart_and_verify(grace_period=0.0)
                cycle_record.post_rollback = {
                    "restarted_on": pre_loop_hash,
                    "target_up": post.is_healed,
                    "status": post.status,
                    "container_status": post.container_status,
                }

                cycle_record.status = "rolled_back"
                cycle_record.final_commit_hash = pre_loop_hash
                cycle_record.failure_reason = last_failure or "All heal attempts failed."
                cycle_record.end_time = time.time()

                if not rollback_success:
                    logger.critical(
                        "ROLLBACK ENCOUNTERED AN ERROR! Repository may require manual inspection."
                    )
                    cycle_record.failure_reason = (
                        (cycle_record.failure_reason or "") + " | Rollback reported an error."
                    )

        except Exception as err:
            logger.exception(
                f"Unexpected exception during heal cycle: {err}. Executing emergency rollback..."
            )
            self.git_manager.rollback_to_pre_loop()
            cycle_record.rollback_check = self.git_manager.last_rollback_check
            self.docker_monitor.restart_target(timeout=10)
            cycle_record.status = "rolled_back"
            cycle_record.final_commit_hash = pre_loop_hash
            cycle_record.failure_reason = f"Unexpected error: {err}"
            cycle_record.end_time = time.time()

        finally:
            self.git_manager.mark_cycle_finished()
            self._release_cycle()
            logger.info(
                f"=== WISDOM LOOP CYCLE [{cycle_id}] FINISHED "
                f"(Status: {cycle_record.status.upper()}) ===")

        return cycle_record

    def _target_facts(self) -> str:
        """The target's exposed ports and environment, as a fact for the patch prompt.

        Read from the container (or pod), not asked of the model: a fix that falls back
        to a default port must use the one the target is probed on (seen live: 8080 for
        a service exposed on 5000, so a correct fallback never answered a probe).
        """
        read = getattr(self.docker_monitor, "target_facts", None)
        try:
            facts = read() if callable(read) else {}
        except Exception as e:
            logger.debug(f"Could not read the target's configuration: {e}")
            return ""
        parts = []
        if facts.get("ports"):
            parts.append("the service is reached on port " + ", ".join(str(p) for p in facts["ports"]))
        if facts.get("env"):
            parts.append("its environment sets " + ", ".join(f"{k}={v}" for k, v in facts["env"].items()))
        return f"\nFACT (read from the target's container): {'; '.join(parts)}.\n" if parts else ""

    @staticmethod
    def _verification_hint(result: VerificationResult, patch_diff: str = "") -> str:
        """Why the deployed patch failed, told to the next attempt in words.

        "failed verification (restart_failed)" alone was seen live to leave the model
        handing its own non-working change back unchanged twice: it had turned the crash
        into "log an error and exit" (or "raise ValueError(...) when it is missing"),
        which moves the failure without fixing anything.
        """
        log = (result.error_log or "").strip()
        why = log.splitlines()[0] if log else result.status
        hint = (
            f"your previous change was deployed and the target still failed ({result.status}: "
            f"{why}). The code shown is that change - it does not work yet."
        )
        # Its own words in the new failure: a message the patch itself added.
        added = "\n".join(
            line[1:] for line in patch_diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        own = next(
            (text for text in _STRING_LITERAL_RE.findall(added) if len(text) >= 12 and text in log),
            None,
        )
        if own:
            hint += (
                f" The new failure comes from code your change added (\"{own}\"): it refuses "
                "to run instead of fixing the cause."
            )
        if own or (result.status == "restart_failed" and "Traceback" not in log):
            hint += (
                " The service must still start and keep running: do not raise, exit, return "
                "early or skip starting it when something is missing - use a safe fallback "
                "instead."
            )
        return hint

    def _render_diff(self, patch: StructuredPatch, max_chars: int = 20000) -> str:
        """Unified diff of the patch against what is on disk right now."""
        chunks: List[str] = []
        base_dir = getattr(self.git_manager, "repo_dir", os.getcwd())
        for file_patch in patch.files:
            full_path = os.path.join(base_dir, file_patch.path)
            old = ""
            if os.path.isfile(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                        old = f.read()
                except OSError:
                    old = ""
            new = "" if file_patch.op == "delete" else file_patch.content
            diff = difflib.unified_diff(
                old.splitlines(keepends=True),
                new.splitlines(keepends=True),
                fromfile=f"a/{file_patch.path}",
                tofile=f"b/{file_patch.path}" if file_patch.op != "delete" else "/dev/null",
            )
            chunks.append("".join(diff) or f"(no textual change in {file_patch.path})\n")
        text = "".join(chunks)
        return text if len(text) <= max_chars else text[:max_chars] + "\n... (diff truncated)\n"

    def record_refused_cycle(
        self, crash_event: ObserverEvent, reason: str, status: str = "refused"
    ) -> HealCycleRecord:
        """Put a heal that was deliberately not run into the history, with its reason.

        The dashboard is where a peer looks for "why did nothing happen?" - a heal that
        was held back for a human (HITL / second-opinion divergence) belongs there.
        """
        now = time.time()
        record = HealCycleRecord(
            cycle_id=str(uuid.uuid4())[:8],
            pre_loop_hash=self.git_manager.get_current_head() or "unknown",
            status=status,
            initial_crash=crash_event.to_dict(),
            failure_reason=reason,
            start_time=now,
            end_time=now,
            heal_branch=self.git_manager.heal_branch,
        )
        self._record(record)
        return record

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the history of heal cycles formatted for the Dashboard."""
        return [c.to_dict() for c in self.history[-limit:]]

    def get_latest_cycle(self) -> Optional[HealCycleRecord]:
        """Returns the most recent heal cycle record."""
        return self.history[-1] if self.history else None
