"""Server-Sent Events stream of live changes"""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from app.events import event_bus

router = APIRouter()

HEARTBEAT_SECONDS = 15


@router.get("")
async def stream_events(request: Request):
    """Live events: {"kind": "vm" | "network" | "pool" | "task" | "connection", ...}"""
    queue = event_bus.subscribe()

    async def stream():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            event_bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        # no-transform stops compression middlewares (CRA dev proxy, nginx) from buffering the stream
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
