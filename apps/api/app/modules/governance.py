from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.security import ADMIN, AUDITOR, STUDENT, SUPERADMIN, Principal, current_user, require
from app.db.models import AuditLog, Notification, User
from app.db.session import get_db

router = APIRouter(tags=["gobierno"])


@router.get("/audit-logs")
def audit_logs(action: str | None = None, resource: str | None = None, limit: int = Query(100, le=500),
               user: Principal = Depends(require(AUDITOR, ADMIN, SUPERADMIN)), db: Session = Depends(get_db)):
    stmt = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.actor_id).where(
        AuditLog.organization_id == user.organization_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource:
        stmt = stmt.where(AuditLog.resource == resource)
    rows = db.execute(stmt.order_by(AuditLog.occurred_at.desc()).limit(limit)).all()
    return [{"id": a.id, "action": a.action, "resource": a.resource, "resource_id": a.resource_id,
             "actor": email or "sistema", "detail": a.detail, "occurred_at": a.occurred_at} for a, email in rows]


@router.get("/notifications")
def notifications(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Notification).where(Notification.organization_id == user.organization_id,
                                      or_(Notification.user_id.is_(None), Notification.user_id == user.user_id))
    if user.role == STUDENT:
        stmt = stmt.where(Notification.user_id == user.user_id)
    return [{"id": n.id, "type": n.type, "title": n.title, "body": n.body, "read": n.read_at is not None,
             "created_at": n.created_at} for n in db.scalars(stmt.order_by(Notification.created_at.desc()).limit(50))]


@router.post("/notifications/{notification_id}/read")
def mark_read(notification_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    n = db.get(Notification, notification_id)
    if not n or n.organization_id != user.organization_id or (n.user_id and n.user_id != user.user_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notificación no encontrada")
    n.read_at = datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}
