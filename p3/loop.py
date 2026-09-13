"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Wisdom Loop Controller: 3-Attempt Healing Orchestrator & Automatic Rollback
"""

from __future__ import annotations

import uuid
import time
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
        max_attempts: int = 3
    ):
        self.git_manager = git_manager
        self.diagnostician = diagnostician
        self.patcher = patcher
        self.sanity_checker = sanity_checker
        self.verifier = verifier
        self.docker_monitor = docker_monitor
        self.max_attempts = max_attempts

        self.history: List[HealCycleRecord] = []
        self._is_running_cycle: bool = False

    def is_active(self) -> bool:
        """Returns True if a heal cycle is currently in progress."""
        return self._is_running_cycle

    def execute_heal(
        self,
        crash_event: ObserverEvent,
        grace_period: float = 20.0,
        cycle_progress_cb: Optional[Callable[[str, int, int], None]] = None
    ) -> HealCycleRecord:
        """Runs the complete self-healing cycle up to 3 attempts.
        Subject requirement: On 3 failures, rewind auto-heal branch to pre-loop revision,
        restart target on original code, and surface clean failure to dashboard.
        """
        if self._is_running_cycle:
            logger.warning(
                "A heal cycle is already running (single-flight enforced). "
                "Rejecting concurrent run."
            )
            refused_cycle = HealCycleRecord(
                cycle_id=str(uuid.uuid4())[:8],
                pre_loop_hash=self.git_manager.get_current_head() or "unknown",
                status="refused",
                initial_crash=crash_event.to_dict(),
                start_time=time.time(),
                end_time=time.time()
            )
            return refused_cycle

        self._is_running_cycle = True
        cycle_id = str(uuid.uuid4())[:8]
        start_now = time.time()

        # Step 1: Snapshot pre-loop revision
        try:
            pre_loop_hash = self.git_manager.snapshot_pre_loop()
        except Exception as e:
            logger.error(f"Failed to snapshot pre-loop revision: {e}")
            self._is_running_cycle = False
            raise

        cycle_record = HealCycleRecord(
            cycle_id=cycle_id,
            pre_loop_hash=pre_loop_hash,
            status="in_progress",
            initial_crash=crash_event.to_dict(),
            start_time=start_now
        )
        self.history.append(cycle_record)

        logger.info(f"=== STARTING WISDOM LOOP CYCLE [{cycle_id}] (pre-loop hash: {pre_loop_hash[:8]}) ===")

        # Extract initial error log from event details
        current_error_log = (
            crash_event.details.get("context_excerpt")
            or crash_event.details.get("matched_line")
            or crash_event.summary
        )
        current_exit_code = crash_event.details.get("exit_code")
        current_status = crash_event.details.get("status", "crashed")

        is_healed = False

        try:
            for attempt_num in range(1, self.max_attempts + 1):
                logger.info(f"\n--- HEAL ATTEMPT {attempt_num}/{self.max_attempts} ---")
                if cycle_progress_cb:
                    cycle_progress_cb("attempt_start", attempt_num, self.max_attempts)

                attempt_record = AttemptRecord(
                    attempt_number=attempt_num,
                    timestamp=time.time()
                )
                cycle_record.attempts.append(attempt_record)

                # Beat 2: Think - Retrieve code & ask LLM for structured diagnosis
                diagnosis: DiagnosticReport = self.diagnostician.diagnose_crash(
                    log_excerpt=current_error_log,
                    exit_code=current_exit_code,
                    status=current_status
                )
                attempt_record.diagnosis = diagnosis.to_dict()

                if not diagnosis.success:
                    logger.warning(f"Attempt {attempt_num}: Diagnosis failed: {diagnosis.error_message}")
                    attempt_record.error_message = f"Diagnosis failed: {diagnosis.error_message}"
                    continue

                # Beat 3: Act - Generate structured JSON patch
                patch: StructuredPatch = self.patcher.generate_patch(
                    diagnosis=diagnosis,
                    log_excerpt=current_error_log
                )
                attempt_record.patch = patch.to_dict()

                if not patch.success:
                    logger.warning(f"Attempt {attempt_num}: Patch generation failed: {patch.error_message}")
                    attempt_record.error_message = f"Patch generation failed: {patch.error_message}"
                    continue

                # Sanity Bounds Check (Pre-disk write)
                sanity_res: SanityResult = self.sanity_checker.check_patch(patch)
                attempt_record.sanity = sanity_res.to_dict()

                if not sanity_res.passed:
                    logger.warning(
                        f"Attempt {attempt_num}: Sanity check REFUSED patch: {sanity_res.reasons}"
                    )
                    attempt_record.error_message = f"Sanity check failed: {'; '.join(sanity_res.reasons)}"
                    continue

                # Apply patch atomically and create git commit on iow/auto-heal branch
                commit_res: CommitResult = self.git_manager.apply_patch_atomically(patch)
                attempt_record.commit_hash = commit_res.commit_hash

                if not commit_res.success:
                    logger.error(f"Attempt {attempt_num}: Atomic commit failed: {commit_res.error_message}")
                    attempt_record.error_message = f"Git commit failed: {commit_res.error_message}"
                    continue

                # Beat 4: Verify - Restart target container and observe grace period
                verify_res: VerificationResult = self.verifier.restart_and_verify(
                    grace_period=grace_period
                )
                attempt_record.verification = verify_res.to_dict()

                if verify_res.is_healed:
                    # SUCCESS: Target container stayed healthy!
                    commit_label = (commit_res.commit_hash or "?")[:8]
                    logger.info(
                        f"🎉 HEAL SUCCESS on attempt {attempt_num}! "
                        f"Target healthy for full {grace_period}s on commit {commit_label}."
                    )
                    cycle_record.status = "healed"
                    cycle_record.final_commit_hash = commit_res.commit_hash
                    cycle_record.end_time = time.time()
                    is_healed = True
                    break
                else:
                    # Failure: hand the new error log back to the next attempt
                    logger.warning(
                        f"Attempt {attempt_num} FAILED: Target crashed during verification. "
                        f"Status: {verify_res.status}"
                    )
                    fallback_log = "Target failed to stay up during verification."
                    current_error_log = verify_res.error_log or fallback_log
                    err_snip = current_error_log[:150]
                    attempt_record.error_message = (
                        f"Verification failed ({verify_res.status}): {err_snip}"
                    )

            # If all 3 attempts failed: Execute Rollback
            if not is_healed:
                logger.error(
                    f"All {self.max_attempts} attempts failed. Executing ROLLBACK "
                    f"to pre-loop revision {pre_loop_hash[:8]}..."
                )
                rollback_success = self.git_manager.rollback_to_pre_loop()

                # Restart container on the original code
                self.docker_monitor.restart_target(timeout=10)

                cycle_record.status = "rolled_back"
                cycle_record.final_commit_hash = pre_loop_hash
                cycle_record.end_time = time.time()

                if not rollback_success:
                    logger.critical(
                        "ROLLBACK ENCOUNTERED AN ERROR! Repository may require manual inspection."
                    )

        except Exception as err:
            logger.exception(
                f"Unexpected exception during heal cycle: {err}. Executing emergency rollback..."
            )
            self.git_manager.rollback_to_pre_loop()
            self.docker_monitor.restart_target(timeout=10)
            cycle_record.status = "rolled_back"
            cycle_record.end_time = time.time()

        finally:
            self._is_running_cycle = False
            status = cycle_record.status.upper()
            logger.info(f"=== WISDOM LOOP CYCLE [{cycle_id}] FINISHED (Status: {status}) ===\n")

        return cycle_record

    def get_history(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the history of heal cycles formatted for the Dashboard."""
        return [c.to_dict() for c in self.history[-limit:]]

    def get_latest_cycle(self) -> Optional[HealCycleRecord]:
        """Returns the most recent heal cycle record."""
        return self.history[-1] if self.history else None
