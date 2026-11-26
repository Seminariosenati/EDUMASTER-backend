import asyncio
import json

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.core.events import Subscriber, bus
from app.core.security import principal_from_token, visible_course_ids
from app.db.session import SessionLocal

router = APIRouter(tags=["tiempo real"])
HEARTBEAT_SECONDS = 15


@router.get("/events/stream")
async def stream(request: Request, token: str | None = Query(default=None)):
    """Canal SSE autorizado por organización, rol y propiedad. EventSource envía el token por query string."""
    header = request.headers.get("authorization", "")
    raw = header[7:] if header.lower().startswith("bearer ") else token
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta el token de acceso")
    # La sesión se cierra antes de empezar a transmitir para no retener una conexión del pool por cliente.
    with SessionLocal() as db:
        user = principal_from_token(raw, db)
        allowed = visible_course_ids(db, user)
    sub = Subscriber(queue=asyncio.Queue(maxsize=500), organization_id=user.organization_id, role=user.role,
                     student_id=user.student_id, course_ids=set(allowed) if allowed is not None else None,
                     user_id=user.user_id)
    bus.subscribe(sub)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            yield f"event: ready\ndata: {json.dumps({'role': user.role})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=HEARTBEAT_SECONDS)
                    yield f"id: {ev['event_id']}\nevent: {ev['event_type']}\ndata: {json.dumps(ev, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
