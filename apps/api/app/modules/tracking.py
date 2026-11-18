from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import (ANALYST, INSTRUCTOR, ML_ENGINEER, STAFF, STUDENT, Principal, current_user, require,
                               visible_course_ids)
from app.db.models import Course, Enrollment, LearningEvent, Student
from app.db.session import get_db
from app.services import academic

router = APIRouter(tags=["seguimiento"])


class EventIn(BaseModel):
    event_id: str | None = Field(default=None, description="UUID del cliente para idempotencia")
    event_type: str
    course_id: str
    student_id: str | None = None
    occurred_at: datetime | None = None
    payload: dict = Field(default_factory=dict)
    source: str = Field(default="web", pattern="^(web|mobile|import|integration)$")


@router.post("/learning-events", status_code=201)
def ingest_event(body: EventIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    student_id = user.student_id if user.role == STUDENT else body.student_id
    if user.role == STUDENT and body.student_id and body.student_id != user.student_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Un estudiante solo registra sus propios eventos")
    if not student_id:
        raise HTTPException(422, "student_id es obligatorio")
    if user.role not in (STUDENT, *STAFF, INSTRUCTOR):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Rol sin permiso para registrar eventos")
    enrollment = db.scalar(select(Enrollment).where(Enrollment.student_id == student_id,
                                                    Enrollment.course_id == body.course_id,
                                                    Enrollment.organization_id == user.organization_id))
    if not enrollment:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Matrícula no encontrada para ese estudiante y curso")
    allowed = visible_course_ids(db, user)
    if allowed is not None and body.course_id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Curso no asignado")
    ev, created = academic.record_event(db, enrollment, body.event_type, body.occurred_at, body.payload, body.source,
                                        body.event_id)
    db.commit()
    return {"event_id": ev.event_id, "created": created, "progress": enrollment.progress, "status": enrollment.status}


@router.get("/students")
def list_students(q: str | None = None, region: str | None = None, cohort: str | None = None,
                  limit: int = Query(50, le=200), offset: int = 0,
                  user: Principal = Depends(require(*STAFF, INSTRUCTOR, ML_ENGINEER)), db: Session = Depends(get_db)):
    pseudonymize = user.role in (ANALYST, ML_ENGINEER)
    stmt = select(Student).where(Student.organization_id == user.organization_id)
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Student.id.in_(select(Enrollment.student_id).where(Enrollment.course_id.in_(allowed))))
    if q:
        stmt = stmt.where(Student.external_code.ilike(f"%{q}%") if pseudonymize
                          else Student.full_name.ilike(f"%{q}%") | Student.external_code.ilike(f"%{q}%"))
    if region:
        stmt = stmt.where(Student.region == region)
    if cohort:
        stmt = stmt.where(Student.cohort == cohort)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = [{"id": s.id, "full_name": "Seudónimo" if pseudonymize else s.full_name, "external_code": s.external_code,
              "region": s.region,
              "cohort": s.cohort, "status": s.enrollment_status}
             for s in db.scalars(stmt.order_by(Student.full_name).limit(limit).offset(offset))]
    return {"total": total, "items": items}


@router.get("/students/{student_id}/progress")
def student_progress(student_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == STUDENT and student_id != user.student_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo puede consultar su propio progreso")
    student = db.get(Student, student_id)
    if not student or student.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Estudiante no encontrado")
    allowed = visible_course_ids(db, user)
    stmt = select(Enrollment, Course).join(Course, Course.id == Enrollment.course_id).where(
        Enrollment.student_id == student_id)
    if allowed is not None:
        stmt = stmt.where(Course.id.in_(allowed))
    courses = []
    for enr, course in db.execute(stmt).all():
        avg, n = academic.course_average(db, student_id, course.id)
        last = db.scalar(select(func.max(LearningEvent.occurred_at)).where(LearningEvent.enrollment_id == enr.id))
        courses.append({"course_id": course.id, "code": course.code, "title": course.title, "status": enr.status,
                        "progress": enr.progress, "average": round(avg, 1), "attempts": n, "last_activity": last,
                        "completed_at": enr.completed_at})
    return {"student": {"id": student.id, "full_name": student.full_name, "external_code": student.external_code,
                        "region": student.region, "cohort": student.cohort}, "courses": courses}
