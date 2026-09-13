"""
Inception-of-Wisdom (IoW) - Chapter VI: Bonus Part
Bonus API: Tiny Classifier, Second Opinion Consensus & Human-in-the-Loop PR Router
"""

from __future__ import annotations

import logging
from typing import Optional, List, Dict, Any

try:
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import JSONResponse
    from pydantic import BaseModel
except ImportError:
    APIRouter = object  # type: ignore[assignment,misc]
    HTTPException = Exception  # type: ignore[assignment,misc]
    Query = None  # type: ignore[assignment,misc]
    JSONResponse = None  # type: ignore[assignment,misc]
    BaseModel = object  # type: ignore[assignment,misc]

from bonus.classifier import TinySymptomClassifier
from bonus.consensus import SecondOpinionEngine
from bonus.pr_manager import PullRequestManager

logger = logging.getLogger("bonus.bonus_api")


class ConsensusRequest(BaseModel if BaseModel is not object else object):  # type: ignore[misc,valid-type]
    log_excerpt: str
    top_k: Optional[int] = 3


class PRActionRequest(BaseModel if BaseModel is not object else object):  # type: ignore[misc,valid-type]
    comment: Optional[str] = None


def create_bonus_router(
    classifier: Optional[TinySymptomClassifier] = None,
    consensus_engine: Optional[SecondOpinionEngine] = None,
    pr_manager: Optional[PullRequestManager] = None
) -> Any:
    """Factory function that creates and wires the FastAPI APIRouter for the Chapter VI Bonus Suite."""
    if APIRouter is object:
        logger.warning("FastAPI is not installed in the current environment.")
        return None

    router = APIRouter(prefix="/api/bonus", tags=["Bonus"])

    # Operational flags
    flags = {
        "human_in_the_loop": False,
        "second_opinion_mandatory": True,
        "fast_path_classifier": True
    }

    @router.get("/status")
    async def get_bonus_status() -> Dict[str, Any]:
        """Returns overall configuration and statistics of all Bonus features."""
        clf_stats = classifier.get_stats() if classifier else {}
        pr_count = len(pr_manager.prs) if pr_manager else 0
        pending_prs = len(pr_manager.list_prs(status_filter="pending_review")) if pr_manager else 0

        return {
            "flags": flags,
            "classifier": clf_stats,
            "second_opinion": {
                "active": consensus_engine is not None,
                "model": consensus_engine.model_name if consensus_engine else "unknown"
            },
            "pull_requests": {
                "total": pr_count,
                "pending": pending_prs
            }
        }

    @router.post("/flags/toggle")
    async def toggle_bonus_flag(flag_name: str) -> Dict[str, Any]:
        """Toggle bonus flags (human_in_the_loop, second_opinion_mandatory, fast_path_classifier)."""
        if flag_name not in flags:
            available = list(flags.keys())
            raise HTTPException(
                status_code=400,
                detail=f"Invalid flag '{flag_name}'. Available: {available}",
            )

        flags[flag_name] = not flags[flag_name]
        logger.info(f"Bonus flag [{flag_name}] set to {flags[flag_name]}")
        return {"flags": flags}

    # ==========================================================================
    # Classifier Endpoints
    # ==========================================================================
    @router.get("/classifier/stats")
    async def get_classifier_stats() -> Dict[str, Any]:
        """Returns learned crash symptoms and fast-path bypass metrics."""
        if not classifier:
            raise HTTPException(status_code=404, detail="Classifier not initialized")
        return classifier.get_stats()

    # ==========================================================================
    # Second Opinion Consensus Endpoints
    # ==========================================================================
    @router.post("/consensus/evaluate")
    async def evaluate_second_opinion(payload: Dict[str, Any]) -> Dict[str, Any]:
        """Runs dual-prompt diagnostic evaluation and reports whether consensus is reached."""
        if not consensus_engine:
            raise HTTPException(status_code=404, detail="Second Opinion consensus engine not initialized")

        log_excerpt = payload.get("log_excerpt", "")
        if not log_excerpt:
            raise HTTPException(status_code=400, detail="log_excerpt cannot be empty")

        top_k = payload.get("top_k", 3)
        report = consensus_engine.evaluate_consensus(log_excerpt, top_k=top_k)
        return report.to_dict()

    # ==========================================================================
    # Pull Request & Review Endpoints
    # ==========================================================================
    @router.get("/prs")
    async def list_pull_requests(status: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lists all review Pull Requests, newest first."""
        if not pr_manager:
            return []
        return pr_manager.list_prs(status_filter=status)

    @router.get("/prs/{pr_id}")
    async def get_pull_request(pr_id: str) -> Dict[str, Any]:
        """Returns detailed Pull Request metadata and Unified Diff."""
        if not pr_manager:
            raise HTTPException(status_code=404, detail="PR Manager not initialized")

        pr = pr_manager.get_pr(pr_id)
        if not pr:
            raise HTTPException(status_code=404, detail=f"PR '{pr_id}' not found")
        return pr.to_dict()

    @router.post("/prs/{pr_id}/merge")
    async def merge_pull_request(pr_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Human operator approval: merges review PR into active auto-heal branch."""
        if not pr_manager:
            raise HTTPException(status_code=404, detail="PR Manager not initialized")

        comment = (payload or {}).get("comment")
        success, message = pr_manager.merge_pr(pr_id, comment=comment)
        if not success:
            raise HTTPException(status_code=400, detail=message)

        merged = pr_manager.get_pr(pr_id)
        return {
            "status": "merged",
            "message": message,
            "pr": merged.to_dict() if merged else {},
        }

    @router.post("/prs/{pr_id}/reject")
    async def reject_pull_request(pr_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Human operator rejection: disposes of review branch."""
        if not pr_manager:
            raise HTTPException(status_code=404, detail="PR Manager not initialized")

        reason = (payload or {}).get("comment") or (payload or {}).get("reason")
        success, message = pr_manager.reject_pr(pr_id, reason=reason)
        if not success:
            raise HTTPException(status_code=400, detail=message)

        rejected = pr_manager.get_pr(pr_id)
        return {
            "status": "rejected",
            "message": message,
            "pr": rejected.to_dict() if rejected else {},
        }

    return router
