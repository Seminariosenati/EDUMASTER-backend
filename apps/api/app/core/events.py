"""Bus de eventos en proceso + patrón Outbox. Los eventos de negocio se guardan en la misma transacción y un
despachador los publica después del commit; el canal SSE filtra por organización y permisos."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import OutboxEvent
from app.db.session import SessionLocal

log = logging.getLogger("edumaster.events")


def envelope(event_type: str, organization_id: str, payload: dict, aggregate_id: str | None = None,
             correlation_id: str | None = None, event_id: str | None = None,
             occurred_at: datetime | None = None) -> dict:
    return {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": (occurred_at or datetime.now(timezone.utc)).isoformat(),
        "organization_id": organization_id,
        "aggregate_id": aggregate_id,
        "correlation_id": correlation_id,
        "schema_version": "1",
        "payload": payload,
    }


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue
    organization_id: str
    role: str
    student_id: str | None
    course_ids: set[str] | None = field(default=None)
    user_id: str | None = None

    def accepts(self, event: dict) -> bool:
        if event["organization_id"] != self.organization_id:
            return False
        p = event.get("payload", {})
        # Eventos privados (mensajes directos): solo para los usuarios indicados.
        if "user_ids" in p:
            return self.user_id in p["user_ids"]
        if self.student_id is not None and p.get("student_id") and p["student_id"] != self.student_id:
            return False
        if self.course_ids is not None and p.get("course_id") and p["course_id"] not in self.course_ids:
            return False
        roles = p.get("audience_roles")
        return not roles or self.role in roles


class EventBus:
    def __init__(self) -> None:
        self.subscribers: set[Subscriber] = set()
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def subscribe(self, sub: Subscriber) -> None:
        self.subscribers.add(sub)

    def unsubscribe(self, sub: Subscriber) -> None:
        self.subscribers.discard(sub)

    def publish(self, event: dict) -> None:
        for sub in list(self.subscribers):
            if sub.accepts(event):
                if sub.queue.full():
                    sub.queue.get_nowait()
                sub.queue.put_nowait(event)

    def publish_threadsafe(self, event: dict) -> None:
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.publish, event)


bus = EventBus()


def emit(db: Session, organization_id: str, event_type: str, payload: dict, aggregate_id: str | None = None,
         correlation_id: str | None = None) -> None:
    """Registra el evento en la tabla outbox dentro de la transacción actual."""
    db.add(OutboxEvent(organization_id=organization_id, event_type=event_type, payload=payload,
                       aggregate_id=aggregate_id, correlation_id=correlation_id))


def _dispatch_batch(limit: int = 200) -> list[dict]:
    with SessionLocal() as db:
        rows = list(db.scalars(select(OutboxEvent).where(OutboxEvent.dispatched_at.is_(None))
                               .order_by(OutboxEvent.created_at).limit(limit)))
        events = [envelope(r.event_type, r.organization_id, r.payload, r.aggregate_id, r.correlation_id,
                           event_id=r.id, occurred_at=r.created_at) for r in rows]
        now = datetime.now(timezone.utc)
        for r in rows:
            r.dispatched_at = now
        db.commit()
        return events


async def outbox_dispatcher(interval: float = 0.5) -> None:
    while True:
        try:
            for event in await asyncio.to_thread(_dispatch_batch):
                bus.publish(event)
        except Exception:  # noqa: BLE001 - el despachador no debe morir por un error puntual
            log.exception("Error despachando outbox")
        await asyncio.sleep(interval)
