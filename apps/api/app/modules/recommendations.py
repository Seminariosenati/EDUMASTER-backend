from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import (INSTRUCTOR, ML_ENGINEER, STAFF, STUDENT, Principal, current_user, require,
                               visible_course_ids)
from app.core.events import emit
from app.db.models import Course, Enrollment, Notification, Recommendation, Skill, Student
from app.db.session import get_db
from app.services import recommender

router = APIRouter(tags=["recomendaciones"])
EXPIRY_DAYS = 14


class FeedbackIn(BaseModel):
    status: str | None = Field(default=None, pattern="^(aceptada|descartada|completada)$")
    useful: bool | None = None


class GenerateIn(BaseModel):
    student_ids: list[str] | None = None


def _out(db: Session, r: Recommendation) -> dict:
    course = db.get(Course, r.course_id) if r.course_id else None
    skill = db.get(Skill, r.skill_id) if r.skill_id else None
    student = db.get(Student, r.student_id)
    enrolled = None
    if r.type == "ruta" and course:
        enrolled = db.scalar(select(Enrollment.id).where(Enrollment.student_id == student.id,
                                                         Enrollment.course_id == course.id)) is not None
    return {"id": r.id, "type": r.type, "title": r.title, "rationale": r.rationale, "impact": r.impact,
            "score": r.score, "status": r.status, "useful": r.useful, "created_at": r.created_at,
            "student": {"id": student.id, "full_name": student.full_name},
            "course": {"id": course.id, "title": course.title} if course else None,
            "skill": {"id": skill.id, "name": skill.name} if skill else None,
            "enrolled": enrolled, "model_version_id": r.model_version_id}


@router.get("/recommendations")
def list_recommendations(status_: str | None = Query(default=None, alias="status"), student_id: str | None = None,
                         limit: int = Query(30, le=1000), user: Principal = Depends(current_user),
                         db: Session = Depends(get_db)):
    expire_stale(db, user.organization_id)
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
    action = None
    if body.status:
        if rec.status == "expirada" and body.status == "aceptada":
            raise HTTPException(status.HTTP_409_CONFLICT, "La recomendación expiró; genera una nueva con el modelo vigente")
        own_route = user.role == STUDENT and rec.type == "ruta" and rec.status == "aceptada"
        if body.status == "aceptada" and (rec.status == "nueva" or own_route):
            action = apply_action(db, rec, user)
        rec.status = body.status
    if body.useful is not None:
        rec.useful = body.useful
    if user.role != STUDENT:
        rec.reviewed_by = user.user_id
    audit(db, user, "feedback_recomendacion", "recommendation", rec.id, status=body.status, useful=body.useful,
          accion=action)
    db.commit()
    return {**_out(db, rec), "action": action}


def _notify(db: Session, rec: Recommendation, user_id: str | None, title: str, body: str) -> None:
    db.add(Notification(organization_id=rec.organization_id, user_id=user_id, type="recomendacion", title=title, body=body))
    emit(db, rec.organization_id, "notification.created",
         {"student_id": rec.student_id, "course_id": rec.course_id, "title": title}, aggregate_id=rec.id)


def apply_action(db: Session, rec: Recommendation, user: Principal) -> str:
    """Ejecuta la acción asociada al aceptar una recomendación y devuelve qué se hizo."""
    student = db.get(Student, rec.student_id)
    course = db.get(Course, rec.course_id) if rec.course_id else None
    if rec.type == "ruta" and course:
        enrolled = db.scalar(select(Enrollment.id).where(Enrollment.student_id == student.id,
                                                         Enrollment.course_id == course.id))
        if enrolled:
            return f"{student.full_name} ya tenía matrícula en {course.title}"
        if course.status != "publicado":
            return f"{course.title} no está publicado; no se matriculó"
        # Solo el propio estudiante decide inscribirse; el personal únicamente se lo recomienda.
        if user.role != STUDENT:
            if not student.user_id:
                return (f"Recomendación registrada para {student.full_name}; no tiene cuenta de acceso para recibirla "
                        "ni inscribirse")
            _notify(db, rec, student.user_id, f"Te recomiendan el curso {course.title}",
                    "Revisa la recomendación en EduMaster y decide si quieres inscribirte.")
            return f"Se recomendó {course.title} a {student.full_name}; el estudiante decide si se inscribe"
        enr = Enrollment(organization_id=rec.organization_id, student_id=student.id, course_id=course.id)
        db.add(enr)
        db.flush()
        emit(db, rec.organization_id, "learning.progress.updated",
             {"student_id": student.id, "course_id": course.id, "enrollment_id": enr.id, "event_type": "enrolled",
              "progress": 0}, aggregate_id=enr.id)
        reviewer = db.scalar(select(Course.instructor_id).where(Course.id == course.id))
        if reviewer:
            _notify(db, rec, reviewer, f"{student.full_name} se inscribió en {course.title}",
                    "Inscripción aceptada por el estudiante a partir de una recomendación de la IA.")
        return f"Te inscribiste en {course.title}"
    if rec.type == "acompanamiento":
        if course and course.instructor_id and course.instructor_id != user.user_id:
            _notify(db, rec, course.instructor_id, f"Acompañar a {student.full_name}", rec.rationale)
        if student.user_id:
            _notify(db, rec, student.user_id, f"Tu docente te contactará sobre {course.title if course else 'tu curso'}",
                    "Queremos ayudarte a retomar tu avance.")
        return f"Se notificó el acompañamiento de {student.full_name}" + (f" al docente de {course.title}" if course else "")
    if student.user_id:
        _notify(db, rec, student.user_id, rec.title, rec.rationale)
        return f"Se envió la recomendación a {student.full_name}"
    return f"Recomendación registrada para {student.full_name} (sin cuenta de acceso para notificarle)"


def expire_stale(db: Session, organization_id: str) -> None:
    limit = datetime.now(timezone.utc) - timedelta(days=EXPIRY_DAYS)
    result = db.execute(update(Recommendation).where(Recommendation.organization_id == organization_id,
                                                     Recommendation.status == "nueva",
                                                     Recommendation.created_at < limit).values(status="expirada"))
    if result.rowcount:
        db.commit()


@router.get("/recommendations/metrics")
def metrics(user: Principal = Depends(require(*STAFF, INSTRUCTOR, ML_ENGINEER)), db: Session = Depends(get_db)):
    expire_stale(db, user.organization_id)
    stmt = select(Recommendation.type, Recommendation.status, Recommendation.useful).where(
        Recommendation.organization_id == user.organization_id)
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Recommendation.student_id.in_(
            select(Enrollment.student_id).where(Enrollment.course_id.in_(allowed))))
    rows = db.execute(stmt).all()
    reviewed = [r for r in rows if r.status in ("aceptada", "descartada", "completada")]
    accepted = [r for r in reviewed if r.status in ("aceptada", "completada")]
    rated = [r for r in rows if r.useful is not None]
    by_type = {}
    for t in ("acompanamiento", "completar", "ruta", "refuerzo"):
        rt = [r for r in reviewed if r.type == t]
        by_type[t] = {"total": sum(r.type == t for r in rows), "revisadas": len(rt),
                      "aceptacion": round(sum(r.status in ("aceptada", "completada") for r in rt) / len(rt), 4) if rt else None}
    return {"total": len(rows), "nuevas": sum(r.status == "nueva" for r in rows),
            "expiradas": sum(r.status == "expirada" for r in rows), "revisadas": len(reviewed),
            "tasa_aceptacion": round(len(accepted) / len(reviewed), 4) if reviewed else None,
            "tasa_utilidad": round(sum(bool(r.useful) for r in rated) / len(rated), 4) if rated else None,
            "valoradas": len(rated), "por_tipo": by_type, "expira_en_dias": EXPIRY_DAYS}


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
