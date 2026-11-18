import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import (ALL_ROLES, MANAGERS, SUPERADMIN, Principal, create_token, current_user,
                               hash_password, require, verify_password)
from app.db.models import Organization, Student, User
from app.db.session import get_db

router = APIRouter(tags=["identidad"])
_attempts: dict[str, deque] = defaultdict(deque)
MAX_ATTEMPTS, WINDOW = 8, 60


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class UserOut(BaseModel):
    id: str
    email: str
    full_name: str
    role: str
    status: str
    organization_id: str


class MeOut(UserOut):
    organization_name: str
    organization_is_demo: bool
    student_id: str | None = None


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    role: str
    password: str = Field(min_length=8, max_length=200)


class UserUpdate(BaseModel):
    role: str | None = None
    status: str | None = Field(default=None, pattern="^(activo|inactivo)$")


def _rate_limit(key: str) -> None:
    now, q = time.monotonic(), _attempts[key]
    while q and now - q[0] > WINDOW:
        q.popleft()
    if len(q) >= MAX_ATTEMPTS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Demasiados intentos; espere un minuto")
    q.append(now)


@router.post("/auth/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    _rate_limit(request.client.host if request.client else "anon")
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if not user or not verify_password(body.password, user.password_hash):
        if user:
            audit(db, None, "login_fallido", "user", user.id, organization_id=user.organization_id)
            db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Correo o contraseña incorrectos")
    if user.status != "activo":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Usuario inactivo")
    principal = Principal(user.id, user.organization_id, user.role, user.full_name, user.email)
    audit(db, principal, "login", "user", user.id)
    db.commit()
    return {"access_token": create_token(user), "token_type": "bearer", "user": _me(db, principal)}


def _me(db: Session, user: Principal) -> MeOut:
    u = db.get(User, user.user_id)
    org = db.get(Organization, u.organization_id)
    student_id = db.scalar(select(Student.id).where(Student.user_id == u.id))
    return MeOut(id=u.id, email=u.email, full_name=u.full_name, role=u.role, status=u.status,
                 organization_id=u.organization_id, organization_name=org.name, organization_is_demo=org.is_demo,
                 student_id=student_id)


@router.get("/auth/me", response_model=MeOut)
def me(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    return _me(db, user)


@router.get("/users", response_model=list[UserOut])
def list_users(role: str | None = None, user: Principal = Depends(require(*MANAGERS)), db: Session = Depends(get_db)):
    stmt = select(User).where(User.organization_id == user.organization_id).order_by(User.role, User.email)
    if role:
        stmt = stmt.where(User.role == role)
    return [UserOut.model_validate(u, from_attributes=True) for u in db.scalars(stmt)]


@router.post("/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, user: Principal = Depends(require(*MANAGERS)), db: Session = Depends(get_db)):
    if body.role not in ALL_ROLES or (body.role == SUPERADMIN and user.role != SUPERADMIN):
        raise HTTPException(422, "Rol no permitido")
    if db.scalar(select(User.id).where(User.email == body.email.lower())):
        raise HTTPException(status.HTTP_409_CONFLICT, "El correo ya está registrado")
    u = User(organization_id=user.organization_id, email=body.email.lower(), full_name=body.full_name,
             role=body.role, password_hash=hash_password(body.password))
    db.add(u)
    db.flush()
    audit(db, user, "crear_usuario", "user", u.id, role=body.role)
    db.commit()
    return UserOut.model_validate(u, from_attributes=True)


@router.patch("/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, body: UserUpdate, user: Principal = Depends(require(*MANAGERS)),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if not u or u.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Usuario no encontrado")
    if body.role:
        if body.role not in ALL_ROLES or (body.role == SUPERADMIN and user.role != SUPERADMIN):
            raise HTTPException(422, "Rol no permitido")
        audit(db, user, "cambiar_rol", "user", u.id, anterior=u.role, nuevo=body.role)
        u.role = body.role
    if body.status:
        audit(db, user, "cambiar_estado", "user", u.id, anterior=u.status, nuevo=body.status)
        u.status = body.status
    db.commit()
    return UserOut.model_validate(u, from_attributes=True)
