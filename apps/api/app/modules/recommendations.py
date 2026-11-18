from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import (INSTRUCTOR, ML_ENGINEER, STAFF, STUDENT, Principal, current_user, require,
                               visible_course_ids)
from app.db.models import Course, Enrollment, Recommendation, Skill, Student
from app.db.session import get_db
from app.services import recommender

router = APIRouter(tags=["recomendaciones"])


class FeedbackIn(BaseModel):
    status: str | None = Field(default=None, pattern="^(aceptada|descartada|completada)$")
    useful: bool | None = None


class GenerateIn(BaseModel):
    student_ids: list[str] | None = None


def _out(db: Session, r: Recommendation) -> dict:
    course = db.get(Course, r.course_id) if r.course_id else None
    skill = db.get(Skill, r.skill_id) if r.skill_id else None
    student = db.get(Student, r.student_id)
    return {"id": r.id, "type": r.type, "title": r.title, "rationale": r.rationale, "impact": r.impact,
            "score": r.score, "status": r.status, "useful": r.useful, "created_at": r.created_at,
            "student": {"id": student.id, "full_name": student.full_name},
            "course": {"id": course.id, "title": course.title} if course else None,
            "skill": {"id": skill.id, "name": skill.name} if skill else None,
            "model_version_id": r.model_version_id}


@router.get("/recommendations")
def list_recommendations(status_: str | None = Query(default=None, alias="status"), student_id: str | None = None,
                         limit: int = Query(30, le=200), user: Principal = Depends(current_user),
                         db: Session = Depends(get_db)):
    stmt = select(Recommendation).where(Recommendation.organization_id == user.organization_id)
    if user.role == STUDENT:
        stmt = stmt.where(Recommendation.student_id == user.student_id)
    elif student_id:
        stmt = stmt.where(Recommendation.student_id == student_id)
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Recommendation.student_id.in_(
            select(Enrollment.student_id).where(Enrollment.course_id.in_(allowed))))
    if status_:
        stmt = stmt.where(Recommendation.status == status_)
    rows = db.scalars(stmt.order_by(Recommendation.created_at.desc(), Recommendation.score.desc()).limit(limit))
    return [_out(db, r) for r in rows]


@router.post("/recommendations/{rec_id}/feedback")
def feedback(rec_id: str, body: FeedbackIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    rec = db.get(Recommendation, rec_id)
    if not rec or rec.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Recomendación no encontrada")
    if user.role == STUDENT and rec.student_id != user.student_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No es su recomendación")
    if user.role not in (STUDENT, INSTRUCTOR, *STAFF):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Rol sin permiso")
    if body.status:
        rec.status = body.status
    if body.useful is not None:
        rec.useful = body.useful
    if user.role != STUDENT:
        rec.reviewed_by = user.user_id
    audit(db, user, "feedback_recomendacion", "recommendation", rec.id, status=body.status, useful=body.useful)
    db.commit()
    return _out(db, rec)


@router.post("/recommendations/generate")
def generate(body: GenerateIn, user: Principal = Depends(require(*STAFF, INSTRUCTOR, ML_ENGINEER)),
             db: Session = Depends(get_db)):
    ids = body.student_ids
    stmt = select(Student.id).where(Student.organization_id == user.organization_id)
    if ids:
        stmt = stmt.where(Student.id.in_(ids))
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Student.id.in_(select(Enrollment.student_id).where(Enrollment.course_id.in_(allowed))))
    student_ids = list(db.scalars(stmt))
    created = recommender.generate(db, user.organization_id, student_ids)
    audit(db, user, "generar_recomendaciones", "recommendation", None, estudiantes=len(student_ids),
          creadas=len(created))
    db.commit()
    return {"students": len(student_ids), "created": len(created)}
