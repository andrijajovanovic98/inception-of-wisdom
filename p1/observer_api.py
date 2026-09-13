"""
Inception-of-Wisdom (IoW) - Part 1: Observer
Observer API & SSE Streaming: GET /status, GET /logs, GET /events (SSE)
"""

from __future__ import annotations

import json
import time
import asyncio
import logging
from typing import Optional, List, Dict, Any

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False

from p1.docker_monitor import DockerMonitor
from p1.log_streamer import LogStreamer
from p1.http_probe import HttpProbeManager
from p1.event_manager import EventManager, ObserverEvent

logger = logging.getLogger("p1.observer_api")


def create_observer_router(
    docker_monitor: Optional[DockerMonitor] = None,
    log_streamer: Optional[LogStreamer] = None,
    http_probe: Optional[HttpProbeManager] = None,
    event_manager: Optional[EventManager] = None
) -> Any:
    """Factory function that creates and wires the FastAPI APIRouter for the Observer."""
    if not FASTAPI_AVAILABLE:
        logger.warning("FastAPI is not installed in the current environment.")
        return None

    router = APIRouter(prefix="/api/observer", tags=["Observer"])

    # Track active SSE subscriber queues (woken on Ctrl+C so uvicorn can exit)
    sse_queues: List[asyncio.Queue] = []
    sse_closing = False

    def shutdown_sse() -> None:
        """Wake / drop live SSE clients so one Ctrl+C can finish without force-cancel noise."""
        nonlocal sse_closing
        sse_closing = True
        for q in list(sse_queues):
            try:
                q.put_nowait({"type": "shutdown", "data": {}})
            except Exception:
                pass

    router.shutdown_sse = shutdown_sse  # type: ignore[attr-defined]

    def _enqueue_sse(payload: Dict[str, Any]) -> None:
        for q in list(sse_queues):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    def _on_event_broadcast(event: ObserverEvent) -> None:
        _enqueue_sse({
            "type": "event",
            "data": event.to_dict()
        })

    if event_manager:
        event_manager.add_listener(_on_event_broadcast)

    def _on_log_line(line: str, is_error: bool) -> None:
        _enqueue_sse({
            "type": "log",
            "data": {
                "line": line,
                "is_error": bool(is_error),
                "timestamp": time.time(),
            },
        })

    if log_streamer:
        log_streamer.add_line_listener(_on_log_line)

    @router.get("/status")
    def get_status() -> Dict[str, Any]:
        """Subject requirement: GET /status
        Returns target container status, health, and probe summaries.
        """
        container_data: Dict[str, Any] = {
            "name": "unknown",
            "status": "unknown",
            "exit_code": None,
            "restart_count": 0,
            "is_crash": False
        }
        if docker_monitor:
            state = docker_monitor.inspect()
            container_data = state.to_dict()

        probe_data: Dict[str, Any] = {}
        is_service_healthy = False
        if http_probe:
            probe_data = http_probe.get_latest_results()
            is_service_healthy = http_probe.is_target_healthy()

        overall_healthy = (
            container_data.get("status") == "running"
            and not container_data.get("is_crash", False)
            and is_service_healthy
        )

        return {
            "overall_healthy": overall_healthy,
            "container": container_data,
            "http_probes": probe_data
        }

    @router.get("/logs")
    def get_logs(tail: int = 100) -> Dict[str, Any]:
        """Subject requirement: GET /logs
        Returns the most recent log lines streamed from the target container.
        """
        logs: List[str] = []
        if log_streamer:
            logs = log_streamer.get_recent_logs(count=tail)
        return {
            "tail": tail,
            "count": len(logs),
            "logs": logs
        }

    @router.post("/logs/clear")
    def clear_logs() -> Dict[str, Any]:
        """Clear the Observer ring buffer (Dashboard Clear button)."""
        if log_streamer:
            log_streamer.clear()
        return {"status": "cleared", "count": 0}

    @router.post("/logs/restart")
    def restart_logs() -> Dict[str, Any]:
        """Reattach Docker log follow without restarting the whole agent."""
        if log_streamer:
            log_streamer.restart()
            return {"status": "restarted", "container": log_streamer.docker_monitor.container_name}
        return {"status": "unavailable"}

    @router.get("/events")
    def get_events(
        limit: int = 50,
        type: Optional[str] = None
    ) -> Dict[str, Any]:
        """Returns the deduplicated historical crash and suggestion events."""
        events: List[Dict[str, Any]] = []
        if event_manager:
            events = event_manager.get_events(event_type=type, limit=limit)
        return {
            "limit": limit,
            "count": len(events),
            "events": events
        }

    @router.get("/stream")
    async def stream_events(request: Request) -> Any:
        """Subject requirement: GET /events over Server-Sent Events (SSE).
        Streams live state changes, new logs, and crash events to the Dashboard.

        Uses plain StreamingResponse (not sse-starlette) so Ctrl+C cancel is quiet.
        """
        if StreamingResponse is None:
            return JSONResponse(
                {"error": "SSE streaming not supported in current environment"},
                status_code=500,
            )

        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        sse_queues.append(queue)

        async def text_stream():
            try:
                connected = {"status": "connected", "message": "Observer SSE connected"}
                yield f"event: connected\ndata: {json.dumps(connected)}\n\n"

                while not sse_closing:
                    try:
                        if await request.is_disconnected():
                            break
                    except asyncio.CancelledError:
                        break

                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=1.0)
                    except asyncio.TimeoutError:
                        last_state = (
                            docker_monitor.get_last_state()
                            if docker_monitor else None
                        )
                        container_status = (
                            last_state.status if last_state else "unknown"
                        )
                        snapshot = {
                            "type": "heartbeat",
                            "container_status": container_status,
                            "target_healthy": (
                                http_probe.is_target_healthy() if http_probe else False
                            ),
                        }
                        yield f"event: heartbeat\ndata: {json.dumps(snapshot)}\n\n"
                        continue
                    except asyncio.CancelledError:
                        break

                    if payload.get("type") == "shutdown" or sse_closing:
                        break

                    yield (
                        f"event: {payload.get('type', 'message')}\n"
                        f"data: {json.dumps(payload.get('data', {}))}\n\n"
                    )
            except asyncio.CancelledError:
                # Normal on Ctrl+C while browser still holds /events open
                pass
            finally:
                if queue in sse_queues:
                    sse_queues.remove(queue)

        return StreamingResponse(
            text_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # Expose SSE handler for root /events alias (IoC / subject-compatible path)
    router.stream_events = stream_events  # type: ignore[attr-defined]
    return router
