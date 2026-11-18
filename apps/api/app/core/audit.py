from sqlalchemy.orm import Session

from app.core.security import Principal
from app.db.models import AuditLog


def audit(db: Session, user: Principal | None, action: str, resource: str, resource_id: str | None = None,
          organization_id: str | None = None, **detail) -> None:
    db.add(AuditLog(organization_id=organization_id or user.organization_id, actor_id=user.user_id if user else None,
                    action=action, resource=resource, resource_id=resource_id, detail=detail))
