from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import Course, Student, User
from app.db.session import get_db

SUPERADMIN = "superadmin"
ADMIN = "admin"
COORDINATOR = "coordinador"
INSTRUCTOR = "docente"
STUDENT = "estudiante"
ANALYST = "analista"
ML_ENGINEER = "ml_engineer"
AUDITOR = "auditor"

ALL_ROLES = [SUPERADMIN, ADMIN, COORDINATOR, INSTRUCTOR, STUDENT, ANALYST, ML_ENGINEER, AUDITOR]
STAFF = [SUPERADMIN, ADMIN, COORDINATOR, ANALYST]
MANAGERS = [SUPERADMIN, ADMIN]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def create_token(user: User) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    claims = {"sub": user.id, "org": user.organization_id, "role": user.role, "iat": now,
              "exp": now + timedelta(minutes=s.jwt_expire_minutes)}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


@dataclass
class Principal:
    user_id: str
    organization_id: str
    role: str
    full_name: str
    email: str
    student_id: str | None = None

    def has(self, *roles: str) -> bool:
        return self.role in roles


def principal_from_token(token: str, db: Session) -> Principal:
    try:
        claims = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sesión inválida o expirada")
    user = db.get(User, claims["sub"])
    if not user or user.status != "activo":
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Usuario inactivo")
    student_id = db.scalar(select(Student.id).where(Student.user_id == user.id)) if user.role == STUDENT else None
    return Principal(user.id, user.organization_id, user.role, user.full_name, user.email, student_id)


def current_user(request: Request, db: Session = Depends(get_db)) -> Principal:
    header = request.headers.get("authorization", "")
    raw = header[7:] if header.lower().startswith("bearer ") else None
    if not raw:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Falta el token de acceso")
    return principal_from_token(raw, db)


def require(*roles: str):
    def checker(user: Principal = Depends(current_user)) -> Principal:
        if user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No tiene permisos para esta acción")
        return user

    return checker


def visible_course_ids(db: Session, user: Principal) -> list[str] | None:
    """Cursos que el usuario puede ver; None significa todos los de su organización."""
    if user.role == INSTRUCTOR:
        return list(db.scalars(select(Course.id).where(Course.organization_id == user.organization_id,
                                                       Course.instructor_id == user.user_id)))
    return None
