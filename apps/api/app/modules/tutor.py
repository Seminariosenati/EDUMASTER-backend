from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import INSTRUCTOR, STAFF, STUDENT, Principal, current_user, require, visible_course_ids
from app.db.models import Course, Enrollment, Student, TutorMessage
from app.db.session import get_db
from app.services import tutor

router = APIRouter(prefix="/tutor", tags=["ia tutor"])


class AskIn(BaseModel):
    message: str = Field(min_length=2, max_length=600)
    course_id: str | None = None


def _msg_out(m: TutorMessage) -> dict:
    return {"id": m.id, "role": m.role, "content": m.content, "course_id": m.course_id, "payload": m.payload,
            "created_at": m.created_at}


@router.get("/context")
def context(user: Principal = Depends(require(STUDENT)), db: Session = Depends(get_db)):
    return tutor.student_context(db, user.organization_id, user.student_id)


@router.get("/history")
def history(course_id: str | None = None, limit: int = Query(40, le=200), user: Principal = Depends(require(STUDENT)),
            db: Session = Depends(get_db)):
    stmt = select(TutorMessage).where(TutorMessage.student_id == user.student_id)
    if course_id:
        stmt = stmt.where(TutorMessage.course_id == course_id)
    rows = list(db.scalars(stmt.order_by(TutorMessage.created_at.desc()).limit(limit)))
    return [_msg_out(m) for m in reversed(rows)]


@router.post("/ask", status_code=201)
def ask(body: AskIn, user: Principal = Depends(require(STUDENT)), db: Session = Depends(get_db)):
    if body.course_id:
        enrolled = db.scalar(select(Enrollment.id).where(Enrollment.student_id == user.student_id,
                                                         Enrollment.course_id == body.course_id))
        if not enrolled:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No estás matriculado en ese curso")
    reply = tutor.answer(db, user.organization_id, user.student_id, body.course_id, body.message)
    course_id = (reply.get("context") or {}).get("course_id") or body.course_id
    q = TutorMessage(organization_id=user.organization_id, student_id=user.student_id, course_id=course_id,
                     role="student", content=body.message.strip())
    a = TutorMessage(organization_id=user.organization_id, student_id=user.student_id, course_id=course_id,
                     role="tutor", content=reply["text"], payload=reply)
    db.add_all([q, a])
    db.flush()
    audit(db, user, "consultar_tutor", "tutor_message", a.id, course_id=course_id,
          intenciones=(reply.get("context") or {}).get("intents"))
    db.commit()
    return {"question": _msg_out(q), "answer": _msg_out(a)}


@router.get("/questions")
def recent_questions(limit: int = Query(30, le=200), user: Principal = Depends(require(*STAFF, INSTRUCTOR)),
                     db: Session = Depends(get_db)):
    """Dudas recientes de los estudiantes, para que el docente detecte temas difíciles."""
    stmt = (select(TutorMessage, Student.full_name, Course.title)
            .join(Student, Student.id == TutorMessage.student_id)
            .outerjoin(Course, Course.id == TutorMessage.course_id)
            .where(TutorMessage.organization_id == user.organization_id, TutorMessage.role == "student"))
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(TutorMessage.course_id.in_(allowed))
    rows = db.execute(stmt.order_by(TutorMessage.created_at.desc()).limit(limit)).all()
    return [{"id": m.id, "student": name, "course": title, "question": m.content, "created_at": m.created_at}
            for m, name, title in rows]
