"""
Inception-of-Wisdom (IoW) — Part 3: Wisdom Loop
Wisdom Loop API: Healing Execution, History Log, Safety Status & Rollback Endpoints
"""

from __future__ import annotations

import time
import uuid
import logging
from typing import Optional, List, Dict, Any

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
    event_manager: Optional[EventManager] = None
) -> Any:
    """Factory function that creates and wires the FastAPI APIRouter for the Wisdom Loop."""
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI is not installed in the current environment.")
        return None

    router = APIRouter(prefix="/api/loop", tags=["Wisdom Loop"])

    @router.get("/status")
    async def get_loop_status() -> Dict[str, Any]:
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
    async def get_heal_history(limit: int = 20) -> List[Dict[str, Any]]:
        """Returns the recorded history of heal cycles, newest first."""
        if not wisdom_loop:
            return []
        records = wisdom_loop.history[-limit:]
        return [r.to_dict() for r in reversed(records)]

    @router.get("/history/{cycle_id}")
    async def get_heal_cycle(cycle_id: str) -> Dict[str, Any]:
        """Returns detailed attempt logs and patches for a specific heal cycle."""
        if not wisdom_loop:
            raise HTTPException(status_code=404, detail="Wisdom Loop not initialized")

        for r in wisdom_loop.history:
            if r.cycle_id == cycle_id:
                return r.to_dict()

        raise HTTPException(status_code=404, detail=f"Heal cycle '{cycle_id}' not found")

    @router.get("/safety")
    async def get_safety_status() -> Dict[str, Any]:
        """Returns detailed safety guardrail configuration and dynamic counters."""
        if not safety_manager:
            return {"status": "safety_manager_not_configured"}
        return safety_manager.get_status()

    def _run_heal_in_background(event: ObserverEvent, sig: str, grace_period: float) -> None:
        """Background worker executing the 3-attempt cycle and managing safety slot."""
        if not wisdom_loop:
            logger.error("Background heal skipped: Wisdom Loop not configured.")
            if safety_manager:
                safety_manager.release_heal_slot(sig)
            return
        try:
            logger.info(f"Background heal cycle started for signature [{sig}]")
            wisdom_loop.execute_heal(event, grace_period=grace_period)
        except Exception as e:
            logger.error(f"Unexpected error in background heal cycle: {e}")
        finally:
            if safety_manager:
                safety_manager.release_heal_slot(sig)
                logger.info(f"Released safety slot for signature [{sig}]")

    @router.post("/trigger")
    async def trigger_heal(
        background_tasks: BackgroundTasks,
        payload: Optional[TriggerHealRequest] = None,
    ) -> Dict[str, Any]:
        """Triggers a self-healing cycle for a crash event, subject to the 5 safety bounds."""
        if not wisdom_loop:
            raise HTTPException(status_code=500, detail="Wisdom Loop controller is not configured.")

        req = payload or TriggerHealRequest()
        sig = req.signature
        summary = req.summary
        log_excerpt = req.log_excerpt
        run_async = True if req.run_async is None else req.run_async

        if not sig and event_manager:
            latest_crash = event_manager.get_latest_crash()
            if latest_crash:
                sig = latest_crash.signature
                summary = latest_crash.summary
                log_excerpt = latest_crash.details.get("log_excerpt", "")

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
        crash_event = ObserverEvent(
            id=str(uuid.uuid4())[:8],
            type="crash",
            source="api",
            signature=sig,
            summary=summary or f"Crash event {sig[:8]}",
            details={
                "log_excerpt": log_excerpt or "",
                "status": "crashed",
                "triggered_by": "api"
            },
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
            result = wisdom_loop.execute_heal(crash_event, grace_period=grace_period)
            return {
                "status": "completed",
                "result": result.to_dict()
            }
        finally:
            if safety_manager:
                safety_manager.release_heal_slot(sig)

    @router.post("/rollback")
    async def emergency_rollback(payload: Optional[RollbackRequest] = None) -> Dict[str, Any]:
        """Manually forces a git rollback to pre-loop revision and restarts target container.
        Only operates on the iow/auto-heal branch when a pre-loop snapshot exists —
        refuses to wipe local WIP on main.
        """
        if not wisdom_loop or not wisdom_loop.git_manager:
            raise HTTPException(status_code=500, detail="Git manager not configured on Wisdom Loop.")

        gm = wisdom_loop.git_manager
        target_revision = payload.target_revision if payload else None
        branch = gm.get_current_branch()
        if branch != gm.heal_branch:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Emergency rollback refused on branch '{branch}'. "
                    f"Switch to '{gm.heal_branch}' after a heal attempt; "
                    "this protects uncommitted work on main."
                ),
            )

        try:
            if target_revision:
                restored_hash = gm.rollback_to(target_revision)
                if not restored_hash:
                    raise HTTPException(status_code=400, detail="rollback_to failed.")
            else:
                if not gm.get_pre_loop_hash():
                    raise HTTPException(
                        status_code=400,
                        detail="No pre-loop snapshot — nothing to roll back (start a heal first).",
                    )
                rollback_ok = gm.rollback_to_pre_loop()
                restored_hash = gm.get_pre_loop_hash() if rollback_ok else None
                if not rollback_ok:
                    raise HTTPException(status_code=500, detail="Pre-loop rollback failed.")

            restart_success = False
            if wisdom_loop.docker_monitor:
                restart_fn = getattr(
                    wisdom_loop.docker_monitor,
                    "restart_target",
                    getattr(wisdom_loop.docker_monitor, "restart_container", None),
                )
                if callable(restart_fn):
                    restart_success = bool(restart_fn())

            return {
                "status": "rolled_back",
                "target_revision": restored_hash,
                "container_restarted": restart_success
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Manual rollback failed: {e}")
            raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")

    return router
