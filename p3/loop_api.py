"""
Inception-of-Wisdom (IoW) - Part 3: Wisdom Loop
Wisdom Loop API: Healing Execution, History Log, Safety Status & Rollback Endpoints
"""

from __future__ import annotations

import time
import uuid
import logging
from typing import Optional, List, Dict, Any, Callable

try:
    from fastapi import APIRouter, HTTPException, BackgroundTasks
    from pydantic import BaseModel

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from p1.event_manager import EventManager, ObserverEvent
from p3.loop import WisdomLoop
from p3.safety import SafetyManager

logger = logging.getLogger("p3.loop_api")

if FASTAPI_AVAILABLE:

    class TriggerHealRequest(BaseModel):
        signature: Optional[str] = None
        summary: Optional[str] = None
        log_excerpt: Optional[str] = None
        run_async: Optional[bool] = True

    class RollbackRequest(BaseModel):
        target_revision: Optional[str] = None


def create_loop_router(
    wisdom_loop: Optional[WisdomLoop] = None,
    safety_manager: Optional[SafetyManager] = None,
    event_manager: Optional[EventManager] = None,
    heal_executor: Optional[Callable[[ObserverEvent, float], None]] = None,
    loop_enabled: bool = True,
    mode: str = "full",
) -> Any:
    """Factory function that creates and wires the FastAPI APIRouter for the Wisdom Loop."""
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI is not installed in the current environment.")
        return None

    router = APIRouter(prefix="/api/loop", tags=["Wisdom Loop"])

    def _require_loop_stage() -> None:
        """Part 3 is not unlocked before `make p3` - refuse instead of patching."""
        if not loop_enabled:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Wisdom Loop is locked in mode '{mode}'. "
                    "Start the agent with `make p3`, `make bonus` or `make run` to "
                    "enable patching, committing and restarting."
                ),
            )

    @router.get("/status")
    def get_loop_status() -> Dict[str, Any]:
        """Returns the real-time operational status of the Wisdom Loop and safety bounds."""
        is_active = wisdom_loop.is_active() if wisdom_loop else False
        history_len = len(wisdom_loop.history) if wisdom_loop else 0
        last_cycle = wisdom_loop.history[-1].to_dict() if (wisdom_loop and wisdom_loop.history) else None

        git_head = None
        git_branch = None
        if wisdom_loop and hasattr(wisdom_loop, "git_manager") and wisdom_loop.git_manager:
            try:
                git_head = wisdom_loop.git_manager.get_current_head()
                git_branch = wisdom_loop.git_manager.get_current_branch()
            except Exception as e:
                logger.error(f"Error fetching git status for loop API: {e}")

        safety_status = safety_manager.get_status() if safety_manager else {}

        return {
            "active": is_active,
            "git_branch": git_branch,
            "git_head": git_head,
            "history_count": history_len,
            "last_cycle": last_cycle,
            "safety": safety_status
        }

    @router.get("/history")
    def get_heal_history(limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the recorded history of heal cycles, newest first."""
        if not wisdom_loop:
            return []
        records = wisdom_loop.history[-limit:]
        return [r.to_dict() for r in reversed(records)]

    @router.get("/history/{cycle_id}")
    def get_heal_cycle(cycle_id: str) -> Dict[str, Any]:
        """Returns detailed attempt logs and patches for a specific heal cycle."""
        if not wisdom_loop:
            raise HTTPException(status_code=404, detail="Wisdom Loop not initialized")

        for r in wisdom_loop.history:
            if r.cycle_id == cycle_id:
                return r.to_dict()

        raise HTTPException(status_code=404, detail=f"Heal cycle '{cycle_id}' not found")

    @router.get("/safety")
    def get_safety_status() -> Dict[str, Any]:
        """Returns detailed safety guardrail configuration and dynamic counters."""
        if not safety_manager:
            return {"status": "safety_manager_not_configured"}
        return safety_manager.get_status()

    def _run_heal_in_background(event: ObserverEvent, sig: str, grace_period: float) -> None:
        """Background worker executing heal (HITL-aware if heal_executor provided)."""
        if not wisdom_loop and heal_executor is None:
            logger.error("Background heal skipped: Wisdom Loop not configured.")
            if safety_manager:
                safety_manager.release_heal_slot(sig)
            return
        try:
            logger.info(f"Background heal cycle started for signature [{sig}]")
            if heal_executor is not None:
                heal_executor(event, grace_period)
            elif wisdom_loop is not None:
                wisdom_loop.execute_heal(event, grace_period=grace_period)
            else:
                logger.error("Background heal skipped: no heal executor or Wisdom Loop.")
        except Exception as e:
            logger.error(f"Unexpected error in background heal cycle: {e}")
        finally:
            if safety_manager:
                safety_manager.release_heal_slot(sig)
                logger.info(f"Released safety slot for signature [{sig}]")

    @router.post("/trigger")
    def trigger_heal(
        background_tasks: BackgroundTasks,
        payload: Optional[TriggerHealRequest] = None,
    ) -> Dict[str, Any]:
        """Triggers a self-healing cycle for a crash event, subject to the 5 safety bounds."""
        _require_loop_stage()
        if not wisdom_loop:
            raise HTTPException(status_code=500, detail="Wisdom Loop controller is not configured.")

        req = payload or TriggerHealRequest()
        sig = req.signature
        summary = req.summary
        log_excerpt = req.log_excerpt
        run_async = True if req.run_async is None else req.run_async

        # Heal a picked crash (its signature) or, by default, the latest one. Either
        # way the Analyst gets that event's own log context, never an empty string.
        source_event: Optional[ObserverEvent] = None
        if event_manager:
            if sig:
                source_event = event_manager.find_event(sig)
            else:
                source_event = event_manager.get_latest_crash()
        if source_event is not None and source_event.type != "crash":
            # Subject V.1: a 4xx is surfaced for a human and never patched.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Event {source_event.signature[:8]} is a {source_event.type}, not a "
                    "crash: a functional gap is for a human to decide on, the loop never "
                    "patches it."
                ),
            )
        if source_event is None and not (log_excerpt or "").strip():
            # With no crash and no log there is nothing to diagnose - and a model told
            # that "returning the code unchanged is not a fix" would edit healthy code.
            raise HTTPException(
                status_code=409,
                detail=(
                    "No crash observed and no error log given - there is nothing to heal. "
                    "Trigger a crash first (make break-import / break-env / break-syntax)."
                ),
            )
        if source_event is not None:
            sig = source_event.signature
            summary = summary or source_event.summary
            # Observer events carry the traceback under context_excerpt /
            # matched_line; reading a non-existent "log_excerpt" key used to
            # hand the Analyst an empty log.
            log_excerpt = (
                source_event.details.get("context_excerpt")
                or source_event.details.get("matched_line")
                or log_excerpt
                or source_event.summary
                or ""
            )

        if not sig:
            sig = f"manual_trigger_{int(time.time())}"
            summary = summary or "Manual heal triggered via API"

        if safety_manager:
            allowed, refusal_reason = safety_manager.acquire_heal_slot(sig)
            if not allowed:
                return {
                    "status": "refused",
                    "reason": refusal_reason,
                    "signature": sig,
                    "safety": safety_manager.get_status()
                }

        now = time.time()
        details: Dict[str, Any] = dict(source_event.details) if source_event else {}
        details.update({
            "log_excerpt": log_excerpt or "",
            "context_excerpt": log_excerpt or "",
            "status": details.get("status") or "crashed",
            "triggered_by": "api",
        })
        crash_event = ObserverEvent(
            id=str(uuid.uuid4())[:8],
            type="crash",
            source=source_event.source if source_event else "api",
            signature=sig,
            summary=summary or f"Crash event {sig[:8]}",
            details=details,
            first_seen=now,
            last_seen=now
        )

        grace_period = safety_manager.get_grace_period() if safety_manager else 20.0

        if run_async:
            background_tasks.add_task(_run_heal_in_background, crash_event, sig, grace_period)
            return {
                "status": "accepted",
                "message": "Heal cycle started in background.",
                "signature": sig,
                "grace_period": grace_period
            }

        try:
            if heal_executor is not None:
                # The same path as the background run (crash context, bonus gates).
                heal_executor(crash_event, grace_period)
                latest = wisdom_loop.get_latest_cycle()
                result_dict = latest.to_dict() if latest is not None else {}
            else:
                result_dict = wisdom_loop.execute_heal(crash_event, grace_period=grace_period).to_dict()
            return {
                "status": "completed",
                "result": result_dict,
            }
        finally:
            if safety_manager:
                safety_manager.release_heal_slot(sig)

    @router.post("/rollback")
    def emergency_rollback(payload: Optional[RollbackRequest] = None) -> Dict[str, Any]:
        """Manually forces a git rollback to pre-loop revision and restarts target container.
        Only operates on the iow/auto-heal branch when a pre-loop snapshot exists -
        refuses to wipe local WIP on main.
        """
        _require_loop_stage()
        if not wisdom_loop or not wisdom_loop.git_manager:
            raise HTTPException(status_code=500, detail="Git manager not configured on Wisdom Loop.")
        if not wisdom_loop.claim():
            # A heal cycle owns the working tree and the heal branch right now; a
            # concurrent rollback would race its writes. It rolls back on its own
            # if it fails. Holding the slot also keeps a heal from starting while this
            # rollback restores and restarts the target.
            raise HTTPException(
                status_code=409,
                detail=(
                    "A heal cycle is running - wait for it to finish "
                    "(it rolls back on its own on failure)."
                ),
            )

        gm = wisdom_loop.git_manager
        target_revision = payload.target_revision if payload else None

        try:
            if target_revision:
                restored_hash = gm.rollback_to(target_revision)
                if not restored_hash:
                    raise HTTPException(status_code=400, detail="rollback_to failed.")
            else:
                if not gm.get_pre_loop_hash():
                    raise HTTPException(
                        status_code=400,
                        detail="No pre-loop snapshot - nothing to roll back (start a heal first).",
                    )
                rollback_ok = gm.rollback_to_pre_loop()
                restored_hash = gm.get_pre_loop_hash() if rollback_ok else None
                if not rollback_ok:
                    raise HTTPException(status_code=500, detail="Pre-loop rollback failed.")

            # Redeploy through the verifier: in GitOps mode that also publishes the
            # rewound heal branch, so the cluster really returns to the old revision.
            restart_success = False
            target_up: Optional[bool] = None
            if getattr(wisdom_loop, "verifier", None) is not None:
                result = wisdom_loop.verifier.restart_and_verify(grace_period=0.0)
                # "restart_failed" in the boot phase means the restart happened but the
                # restored code did not come up - which is what a planted bug does.
                restart_success = (
                    result.status != "restart_failed" or result.details.get("phase") == "boot"
                )
                target_up = result.is_healed
            elif wisdom_loop.docker_monitor:
                restart_success = bool(wisdom_loop.docker_monitor.restart_target())

            return {
                "status": "rolled_back",
                "target_revision": restored_hash,
                "container_restarted": restart_success,
                "target_up": target_up,
                "rollback_check": gm.last_rollback_check if not target_revision else None,
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Manual rollback failed: {e}")
            raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")
        finally:
            wisdom_loop.release()

    return router
