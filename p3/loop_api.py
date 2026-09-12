"""
Inception-of-Wisdom (IoW) — Part 3: Wisdom Loop
Wisdom Loop API: Healing Execution, History Log, Safety Status & Rollback Endpoints
"""

from __future__ import annotations

import os
import time
import uuid
import logging
import asyncio
from typing import Optional, List, Dict, Any

try:
    from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError:
    APIRouter = object
    HTTPException = Exception
    Query = None
    BackgroundTasks = None
    JSONResponse = None
    BaseModel = object

from p1.event_manager import EventManager, ObserverEvent
from p3.loop import WisdomLoop, HealCycleRecord
from p3.safety import SafetyManager

logger = logging.getLogger("p3.loop_api")


class TriggerHealRequest(BaseModel if BaseModel is not object else object):
    signature: Optional[str] = None
    summary: Optional[str] = None
    log_excerpt: Optional[str] = None
    run_async: Optional[bool] = True


class RollbackRequest(BaseModel if BaseModel is not object else object):
    target_revision: Optional[str] = None


def create_loop_router(
    wisdom_loop: Optional[WisdomLoop] = None,
    safety_manager: Optional[SafetyManager] = None,
    event_manager: Optional[EventManager] = None
) -> Any:
    """Factory function that creates and wires the FastAPI APIRouter for the Wisdom Loop."""
    if APIRouter is object:
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
        payload: Optional[Dict[str, Any]] = None,
        background_tasks: Optional[BackgroundTasks] = None
    ) -> Dict[str, Any]:
        """Triggers a self-healing cycle for a crash event, subject to the 5 safety bounds."""
        if not wisdom_loop:
            raise HTTPException(status_code=500, detail="Wisdom Loop controller is not configured.")

        # Extract parameters from optional body
        req_data = payload or {}
        sig = req_data.get("signature")
        summary = req_data.get("summary")
        log_excerpt = req_data.get("log_excerpt")
        run_async = req_data.get("run_async", True)

        # If no explicit crash provided, look for latest unresolved crash from EventManager
        if not sig and event_manager:
            recent_events = event_manager.get_recent_events(limit=10)
            for ev in reversed(recent_events):
                if ev.event_type == "crash":
                    sig = ev.signature
                    summary = ev.summary
                    log_excerpt = ev.details.get("log_excerpt", "")
                    break

        if not sig:
            sig = f"manual_trigger_{int(time.time())}"
            summary = summary or "Manual heal triggered via API"

        # Check safety guardrails before starting
        if safety_manager:
            allowed, refusal_reason = safety_manager.acquire_heal_slot(sig)
            if not allowed:
                return {
                    "status": "refused",
                    "reason": refusal_reason,
                    "signature": sig,
                    "safety": safety_manager.get_status()
                }

        crash_event = ObserverEvent(
            event_type="crash",
            summary=summary or f"Crash event {sig[:8]}",
            signature=sig,
            details={
                "log_excerpt": log_excerpt or "",
                "status": "crashed",
                "triggered_by": "api"
            }
        )

        grace_period = safety_manager.get_grace_period() if safety_manager else 20.0

        if run_async and background_tasks is not None:
            background_tasks.add_task(_run_heal_in_background, crash_event, sig, grace_period)
            return {
                "status": "accepted",
                "message": "Heal cycle started in background.",
                "signature": sig,
                "grace_period": grace_period
            }
        else:
            # Synchronous execution
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
    async def emergency_rollback(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Manually forces a git rollback to pre-loop revision and restarts target container."""
        if not wisdom_loop or not wisdom_loop.git_manager:
            raise HTTPException(status_code=500, detail="Git manager not configured on Wisdom Loop.")

        req_data = payload or {}
        target_revision = req_data.get("target_revision")

        try:
            if target_revision:
                restored_hash = wisdom_loop.git_manager.rollback_to(target_revision)
            else:
                restored_hash = wisdom_loop.git_manager.rollback_to_pre_loop()

            # Restart target container on the rolled-back code
            restart_success = False
            if wisdom_loop.docker_monitor:
                restart_success = wisdom_loop.docker_monitor.restart_container()

            return {
                "status": "rolled_back",
                "target_revision": restored_hash,
                "container_restarted": restart_success
            }
        except Exception as e:
            logger.error(f"Manual rollback failed: {e}")
            raise HTTPException(status_code=500, detail=f"Rollback failed: {str(e)}")

    return router

