"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Wisdom Loop Controller: 3-Attempt Healing Orchestrator & Automatic Rollback

Every attempt learns from the previous one: a verification failure hands the new
error log back to the Analyst, and a refused patch (sanity violation, truncation,
unreadable suspect file) hands the refusal reason back to the Patcher. No attempt is
ever a byte-identical repeat of the one before it.
"""

from __future__ import annotations

import uuid
import time
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
from p3.verifier import TargetVerifier, VerificationResult

logger = logging.getLogger("p3.loop")


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
    status: str                                  # 'in_progress', 'healed', 'rolled_back', 'refused'
    initial_crash: Dict[str, Any]
    attempts: List[AttemptRecord] = field(default_factory=list)
    final_commit_hash: Optional[str] = None
    failure_reason: Optional[str] = None
    start_time: float = 0.0
    end_time: Optional[float] = None

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
            "end_time": self.end_time
        }


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

    def _record(self, cycle: HealCycleRecord) -> None:
        self.history.append(cycle)
        if len(self.history) > self.max_history:
            del self.history[: len(self.history) - self.max_history]

    # ------------------------------------------------------------------
    def execute_heal(
        self,
        crash_event: ObserverEvent,
        grace_period: float = 20.0,
        cycle_progress_cb: Optional[Callable[[str, int, int], None]] = None,
        preset_diagnosis: Optional[DiagnosticReport] = None,
        fast_path_patch: Optional[StructuredPatch] = None,
    ) -> HealCycleRecord:
        """Runs the complete self-healing cycle up to 3 attempts.

        preset_diagnosis reuses a diagnosis already agreed by the consensus engine.
        fast_path_patch applies a classifier-cached patch on attempt 1 without asking
        the model at all; if it fails verification the loop falls back to the LLM.
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

        # Point the heal branch at the live HEAD, then snapshot it. Nothing is checked
        # out, so the peer's branch and working tree stay exactly where they were.
        try:
            if not self.git_manager.prepare_heal_branch():
                raise RuntimeError(
                    f"Could not point heal branch '{self.git_manager.heal_branch}' at HEAD"
                )
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
            start_time=start_now
        )
        self._record(cycle_record)

        logger.info(
            f"=== STARTING WISDOM LOOP CYCLE [{cycle_id}] "
            f"(pre-loop hash: {pre_loop_hash[:8]}, branch: {self.git_manager.heal_branch}) ==="
        )

        current_error_log = (
            crash_event.details.get("context_excerpt")
            or crash_event.details.get("matched_line")
            or crash_event.details.get("log_excerpt")
            or crash_event.summary
        )
        current_exit_code = crash_event.details.get("exit_code")
        current_status = crash_event.details.get("status", "crashed")

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
                        diagnosis = self.diagnostician.diagnose_crash(
                            log_excerpt=current_error_log,
                            exit_code=current_exit_code,
                            status=current_status
                        )
                    attempt_record.diagnosis = diagnosis.to_dict()

                    if not diagnosis.success:
                        msg = f"Diagnosis failed: {diagnosis.error_message}"
                        logger.warning(f"Attempt {attempt_num}: {msg}")
                        attempt_record.error_message = msg
                        last_failure = msg
                        refusal_hint = None
                        continue

                    # Beat 3: Act - generate a structured JSON patch.
                    patch = self.patcher.generate_patch(
                        diagnosis=diagnosis,
                        log_excerpt=current_error_log,
                        refusal_hint=refusal_hint,
                    )
                    attempt_record.patch = patch.to_dict()

                    if not patch.success:
                        msg = f"Patch generation failed: {patch.error_message}"
                        logger.warning(f"Attempt {attempt_num}: {msg}")
                        attempt_record.error_message = msg
                        last_failure = msg
                        refusal_hint = patch.error_message
                        continue

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
                commit_res: CommitResult = self.git_manager.apply_patch_atomically(patch)
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
                    grace_period=grace_period
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
                refusal_hint = (
                    "the patch was applied but the target still failed verification "
                    f"({verify_res.status})"
                )
                if use_fast_path:
                    fast_path_patch = None

            if not is_healed:
                logger.error(
                    f"All {self.max_attempts} attempts failed. Executing ROLLBACK "
                    f"to pre-loop revision {pre_loop_hash[:8]}..."
                )
                rollback_success = self.git_manager.rollback_to_pre_loop()

                # Restart the target on the original code.
                self.verifier.restart_and_verify(grace_period=0.0)

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
            self.docker_monitor.restart_target(timeout=10)
            cycle_record.status = "rolled_back"
            cycle_record.failure_reason = f"Unexpected error: {err}"
            cycle_record.end_time = time.time()

        finally:
            self._release_cycle()
            logger.info(
                f"=== WISDOM LOOP CYCLE [{cycle_id}] FINISHED "
                f"(Status: {cycle_record.status.upper()}) ===")

        return cycle_record

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the history of heal cycles formatted for the Dashboard."""
        return [c.to_dict() for c in self.history[-limit:]]

    def get_latest_cycle(self) -> Optional[HealCycleRecord]:
        """Returns the most recent heal cycle record."""
        return self.history[-1] if self.history else None
