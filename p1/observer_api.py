"""
Inception-of-Wisdom (IoW) — Part 1: Observer
Observer API & SSE Streaming: GET /status, GET /logs, GET /events (SSE)
"""

from __future__ import annotations

import json
import asyncio
import logging
from typing import Optional, List, Dict, Any

try:
    from fastapi import APIRouter, Query, Request
    from fastapi.responses import JSONResponse, StreamingResponse
except ImportError:
    APIRouter = object
    Query = None
    Request = None
    JSONResponse = None
    StreamingResponse = None

try:
    from sse_starlette.sse import EventSourceResponse
except ImportError:
    EventSourceResponse = None

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
    if APIRouter is object:
        logger.warning("FastAPI is not installed in the current environment.")
        return None

    router = APIRouter(prefix="/api/observer", tags=["Observer"])

    # Track active SSE subscriber queues
    sse_queues: List[asyncio.Queue] = []

    # Listen to newly emitted events from EventManager and push to all active SSE queues
    def _on_event_broadcast(event: ObserverEvent) -> None:
        payload = {
            "type": "event",
            "data": event.to_dict()
        }
        for q in list(sse_queues):
            try:
                q.put_nowait(payload)
            except Exception:
                pass

    if event_manager:
        event_manager.add_listener(_on_event_broadcast)

    @router.get("/status")
    async def get_status() -> Dict[str, Any]:
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
    async def get_logs(tail: int = 100) -> Dict[str, Any]:
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

    @router.get("/events")
    async def get_events(
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
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        sse_queues.append(queue)

        async def event_generator():
            try:
                # Send immediate connection handshake
                yield {
                    "event": "connected",
                    "data": json.dumps({"status": "connected", "message": "Observer SSE connected"})
                }

                while True:
                    if request and await request.is_disconnected():
                        break

                    try:
                        # Wait for a new event or timeout to send a heartbeat ping
                        payload = await asyncio.wait_for(queue.get(), timeout=2.0)
                        yield {
                            "event": payload.get("type", "message"),
                            "data": json.dumps(payload.get("data", {}))
                        }
                    except asyncio.TimeoutError:
                        # Heartbeat snapshot to keep connection alive and update dashboard
                        snapshot = {
                            "type": "heartbeat",
                            "container_status": docker_monitor.get_last_state().status if docker_monitor and docker_monitor.get_last_state() else "unknown",
                            "target_healthy": http_probe.is_target_healthy() if http_probe else False
                        }
                        yield {
                            "event": "heartbeat",
                            "data": json.dumps(snapshot)
                        }

            finally:
                if queue in sse_queues:
                    sse_queues.remove(queue)

        if EventSourceResponse is not None:
            return EventSourceResponse(event_generator())
        elif StreamingResponse is not None:
            async def text_stream():
                async for item in event_generator():
                    yield f"event: {item.get('event')}\ndata: {item.get('data')}\n\n"
            return StreamingResponse(text_stream(), media_type="text/event-stream")
        else:
            return JSONResponse(
                {"error": "SSE streaming not supported in current environment"},
                status_code=500
            )

    return router

