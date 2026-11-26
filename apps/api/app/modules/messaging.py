"""Mensajería directa 1 a 1 en tiempo real (SSE dirigido por usuario)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.events import emit
from app.core.security import INSTRUCTOR, STUDENT, Principal, current_user, hash_password
from app.db.models import (Conversation, ConversationMember, Course, DirectMessage, Enrollment, Student, User)
from app.db.session import get_db

router = APIRouter(prefix="/messages", tags=["mensajería"])


class StartIn(BaseModel):
    user_id: str


class MessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=2000)


def contacts(db: Session, user: Principal) -> dict[str, dict]:
    """Usuarios con los que este usuario puede conversar, con el contexto que los une."""
    out: dict[str, dict] = {}

    def add(u: User, context: str) -> None:
        if u.id != user.user_id and u.status == "activo" and u.organization_id == user.organization_id:
            out.setdefault(u.id, {"id": u.id, "name": u.full_name, "role": u.role, "context": context})

    if user.role == STUDENT:
        my_courses = list(db.scalars(select(Enrollment.course_id).where(Enrollment.student_id == user.student_id)))
        for c in db.scalars(select(Course).where(Course.id.in_(my_courses))):
            if c.instructor_id:
                add(db.get(User, c.instructor_id), f"Docente de {c.title}")
        rows = db.execute(select(User, Course.title).join(Student, Student.user_id == User.id)
                          .join(Enrollment, Enrollment.student_id == Student.id)
                          .join(Course, Course.id == Enrollment.course_id)
                          .where(Enrollment.course_id.in_(my_courses))).all()
        for u, title in rows:
            add(u, f"Compañero de {title}")
        return out
    if user.role == INSTRUCTOR:
        rows = db.execute(select(User, Course.title).join(Student, Student.user_id == User.id)
                          .join(Enrollment, Enrollment.student_id == Student.id)
                          .join(Course, Course.id == Enrollment.course_id)
                          .where(Course.instructor_id == user.user_id)).all()
        for u, title in rows:
            add(u, f"Estudiante de {title}")
        for u in db.scalars(select(User).where(User.organization_id == user.organization_id, User.role != STUDENT)):
            add(u, "Personal de la institución")
        return out
    for u in db.scalars(select(User).where(User.organization_id == user.organization_id)):
        add(u, "Institución")
    return out


def _member(db: Session, conversation_id: str, user: Principal) -> Conversation:
    conv = db.get(Conversation, conversation_id)
    if not conv or conv.organization_id != user.organization_id or not db.get(ConversationMember, (conv.id, user.user_id)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversación no encontrada")
    return conv


def _members(db: Session, conversation_id: str) -> list[ConversationMember]:
    return list(db.scalars(select(ConversationMember).where(ConversationMember.conversation_id == conversation_id)))


def _msg_out(m: DirectMessage, names: dict[str, str]) -> dict:
    return {"id": m.id, "conversation_id": m.conversation_id, "sender_id": m.sender_id,
            "sender_name": names.get(m.sender_id, ""), "body": m.body, "created_at": m.created_at}


def _unread(db: Session, conversation_id: str, user_id: str, last_read: datetime | None) -> int:
    stmt = select(func.count(DirectMessage.id)).where(DirectMessage.conversation_id == conversation_id,
                                                      DirectMessage.sender_id != user_id)
    if last_read:
        stmt = stmt.where(DirectMessage.created_at > last_read)
    return db.scalar(stmt) or 0


@router.get("/contacts")
def list_contacts(q: str | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    items = sorted(contacts(db, user).values(), key=lambda c: (c["role"] != INSTRUCTOR, c["name"]))
    if q:
        items = [c for c in items if q.lower() in c["name"].lower()]
    return items[:100]


@router.get("/conversations")
def conversations(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.execute(select(Conversation, ConversationMember.last_read_at)
                      .join(ConversationMember, ConversationMember.conversation_id == Conversation.id)
                      .where(ConversationMember.user_id == user.user_id)
                      .order_by(Conversation.last_message_at.desc())).all()
    out = []
    for conv, last_read in rows:
        other_id = next((m.user_id for m in _members(db, conv.id) if m.user_id != user.user_id), None)
        other = db.get(User, other_id) if other_id else None
        last = db.scalar(select(DirectMessage).where(DirectMessage.conversation_id == conv.id)
                         .order_by(DirectMessage.created_at.desc()).limit(1))
        out.append({"id": conv.id, "other": {"id": other.id, "name": other.full_name, "role": other.role} if other else None,
                    "last_message": {"body": last.body, "created_at": last.created_at, "mine": last.sender_id == user.user_id} if last else None,
                    "unread": _unread(db, conv.id, user.user_id, last_read), "last_message_at": conv.last_message_at})
    return out


@router.get("/unread")
def unread_total(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    total = 0
    for m in db.scalars(select(ConversationMember).where(ConversationMember.user_id == user.user_id)):
        total += _unread(db, m.conversation_id, user.user_id, m.last_read_at)
    return {"unread": total}


@router.post("/conversations", status_code=201)
def start(body: StartIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    if body.user_id not in contacts(db, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No puedes escribir a este usuario")
    mine = select(ConversationMember.conversation_id).where(ConversationMember.user_id == user.user_id)
    existing = db.scalar(select(ConversationMember.conversation_id).where(
        ConversationMember.user_id == body.user_id, ConversationMember.conversation_id.in_(mine)).limit(1))
    if existing:
        return {"id": existing, "created": False}
    conv = Conversation(organization_id=user.organization_id)
    db.add(conv)
    db.flush()
    db.add_all([ConversationMember(conversation_id=conv.id, user_id=user.user_id, last_read_at=datetime.now(timezone.utc)),
                ConversationMember(conversation_id=conv.id, user_id=body.user_id)])
    db.commit()
    return {"id": conv.id, "created": True}


@router.get("/conversations/{conversation_id}")
def thread(conversation_id: str, limit: int = Query(80, le=300), user: Principal = Depends(current_user),
           db: Session = Depends(get_db)):
    conv = _member(db, conversation_id, user)
    members = _members(db, conv.id)
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([m.user_id for m in members])))}
    names = {uid: u.full_name for uid, u in users.items()}
    msgs = list(db.scalars(select(DirectMessage).where(DirectMessage.conversation_id == conv.id)
                           .order_by(DirectMessage.created_at.desc()).limit(limit)))
    other = next((users[m.user_id] for m in members if m.user_id != user.user_id), None)
    other_read = next((m.last_read_at for m in members if m.user_id != user.user_id), None)
    return {"id": conv.id, "other": {"id": other.id, "name": other.full_name, "role": other.role} if other else None,
            "other_last_read_at": other_read, "messages": [_msg_out(m, names) for m in reversed(msgs)]}


@router.post("/conversations/{conversation_id}/messages", status_code=201)
def send(conversation_id: str, body: MessageIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    conv = _member(db, conversation_id, user)
    members = _members(db, conv.id)
    now = datetime.now(timezone.utc)
    m = DirectMessage(organization_id=user.organization_id, conversation_id=conv.id, sender_id=user.user_id,
                      body=body.body.strip(), created_at=now)
    db.add(m)
    conv.last_message_at = now
    for mm in members:
        if mm.user_id == user.user_id:
            mm.last_read_at = now
    db.flush()
    out = _msg_out(m, {user.user_id: user.full_name})
    emit(db, user.organization_id, "message.created",
         {"user_ids": [mm.user_id for mm in members], "conversation_id": conv.id, "message": {**out, "created_at": now.isoformat()}},
         aggregate_id=conv.id)
    audit(db, user, "enviar_mensaje", "conversation", conv.id)
    db.commit()
    return out


@router.post("/conversations/{conversation_id}/read")
def mark_read(conversation_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    conv = _member(db, conversation_id, user)
    member = db.get(ConversationMember, (conv.id, user.user_id))
    member.last_read_at = datetime.now(timezone.utc)
    others = [m.user_id for m in _members(db, conv.id) if m.user_id != user.user_id]
    emit(db, user.organization_id, "message.read",
         {"user_ids": others, "conversation_id": conv.id, "reader_id": user.user_id,
          "read_at": member.last_read_at.isoformat()}, aggregate_id=conv.id)
    db.commit()
    return {"ok": True}


DEMO_CLASSMATES = 3


def ensure_demo_students(db: Session, organization_id: str, password: str = "Demo2026!") -> int:
    """Cuentas DEMO de compañeros de la estudiante DEMO, para probar comunidad y mensajes. Idempotente."""
    main_user = db.scalar(select(User).where(User.email == "estudiante@edumaster.demo"))
    if not main_user or main_user.organization_id != organization_id:
        return 0
    me = db.scalar(select(Student).where(Student.user_id == main_user.id))
    if not me:
        return 0
    created = 0
    my_courses = list(db.scalars(select(Enrollment.course_id).where(Enrollment.student_id == me.id)))
    candidates = list(db.scalars(select(Student).join(Enrollment, Enrollment.student_id == Student.id)
                                 .where(Enrollment.course_id.in_(my_courses), Student.id != me.id,
                                        Student.user_id.is_(None)).order_by(Student.external_code).distinct()))
    pw = hash_password(password)
    for i in range(2, 2 + DEMO_CLASSMATES):
        email = f"estudiante{i}@edumaster.demo"
        if db.scalar(select(User.id).where(User.email == email)) or not candidates:
            continue
        st = candidates.pop(0)
        u = User(organization_id=organization_id, email=email, full_name=st.full_name, role=STUDENT, password_hash=pw)
        db.add(u)
        db.flush()
        st.user_id = u.id
        created += 1
        if i == 2:
            conv = Conversation(organization_id=organization_id)
            db.add(conv)
            db.flush()
            db.add_all([ConversationMember(conversation_id=conv.id, user_id=main_user.id),
                        ConversationMember(conversation_id=conv.id, user_id=u.id)])
            db.add(DirectMessage(organization_id=organization_id, conversation_id=conv.id, sender_id=u.id,
                                 body="¡Hola! ¿Ya hiciste la evaluación? Si quieres repasamos juntos."))
    db.commit()
    return created
