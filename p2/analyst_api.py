"""
Inception-of-Wisdom (IoW) — Part 2: Analyst
Analyst API: Free-Text Retrieval & Structured Crash Diagnosis Endpoints
"""

from __future__ import annotations

import logging
from typing import Optional, List, Dict, Any

try:
    from fastapi import APIRouter, HTTPException
    from pydantic import BaseModel
except ImportError:
    APIRouter = object
    HTTPException = Exception
    BaseModel = object

from p2.db import ChromaVectorDB
from p2.retriever import CodeRetriever
from p2.diagnostician import CrashDiagnostician, DiagnosticReport
from p2.chunker import AstChunker

logger = logging.getLogger("p2.analyst_api")


class RetrieveRequest(BaseModel):
    query: str
    top_k: Optional[int] = 3
    file_path: Optional[str] = None


class DiagnoseRequest(BaseModel):
    log_excerpt: str
    exit_code: Optional[int] = None
    status: Optional[str] = "crashed"
    top_k: Optional[int] = 3


def create_analyst_router(
    vector_db: Optional[ChromaVectorDB] = None,
    retriever: Optional[CodeRetriever] = None,
    diagnostician: Optional[CrashDiagnostician] = None,
    chunker: Optional[AstChunker] = None,
    target_dir: str = "demo_app"
) -> Any:
    """Factory function that creates and wires the FastAPI APIRouter for the Analyst tab."""
    if APIRouter is object:
        logger.warning("FastAPI is not installed in the current environment.")
        return None

    router = APIRouter(prefix="/api/analyst", tags=["Analyst"])

    @router.get("/status")
    async def get_analyst_status() -> Dict[str, Any]:
        """Returns the current status of the vector index and local models."""
        count = vector_db.count() if vector_db else 0
        emb_model = vector_db.embedding_model_name if vector_db else "unknown"
        llm_model = diagnostician.model_name if diagnostician else "unknown"

        return {
            "indexed_chunks_count": count,
            "embedding_model": emb_model,
            "llm_model": llm_model,
            "target_dir": target_dir
        }

    @router.post("/sync")
    async def sync_index() -> Dict[str, Any]:
        """Walks the target directory, chunks source files with AST, and updates ChromaDB."""
        if not chunker or not vector_db:
            return {"status": "error", "message": "Chunker or VectorDB not configured"}

        chunks = chunker.chunk_directory(target_dir)
        stats = vector_db.index_chunks(chunks)

        return {
            "status": "success",
            "total_chunks_found": len(chunks),
            "stats": stats,
            "total_indexed_in_db": vector_db.count()
        }

    @router.post("/retrieve")
    async def retrieve_chunks(req: RetrieveRequest) -> Dict[str, Any]:
        """Subject requirement: Free-text retrieval over the index.
        Returns top-k closest chunks with their similarity score.
        """
        if not retriever:
            return {"query": req.query, "count": 0, "chunks": []}

        chunks = retriever.retrieve_by_query(
            query=req.query,
            top_k=req.top_k,
            file_path=req.file_path
        )

        return {
            "query": req.query,
            "count": len(chunks),
            "chunks": chunks
        }

    @router.post("/diagnose")
    async def diagnose_crash(req: DiagnoseRequest) -> Dict[str, Any]:
        """Subject requirement: Structured diagnosis of a crash event.
        Returns JSON with summary and suspect files for Part 3.
        """
        if not diagnostician:
            return {
                "success": False,
                "summary": "Diagnostician not configured",
                "files": [],
                "error_message": "Diagnostician is unavailable"
            }

        report: DiagnosticReport = diagnostician.diagnose_crash(
            log_excerpt=req.log_excerpt,
            exit_code=req.exit_code,
            status=req.status or "crashed",
            top_k=req.top_k or 3
        )

        return report.to_dict()

    return router

