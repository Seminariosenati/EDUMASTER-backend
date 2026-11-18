"""Reglas de negocio académicas: registro de eventos, avance, finalización y certificación."""

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.events import emit
from app.db.models import (Assessment, AssessmentAttempt, Certificate, Course, CourseSkill, Enrollment,
                           LearningEvent, Notification, SkillEvidence)

COMPLETION_ACTIVITY_RATIO = 10 / 12
COMPLETION_MIN_ATTEMPTS = 2
COMPLETION_MIN_AVERAGE = 55
CERTIFICATE_MIN_AVERAGE = 60
EVENT_TYPES = {"lesson_viewed", "activity_started", "activity_completed", "assessment_submitted", "course_completed",
               "session_started", "session_ended"}


def course_average(db: Session, student_id: str, course_id: str) -> tuple[float, int]:
    row = db.execute(select(func.avg(AssessmentAttempt.score), func.count(AssessmentAttempt.id))
                     .join(Assessment, Assessment.id == AssessmentAttempt.assessment_id)
                     .where(AssessmentAttempt.student_id == student_id, Assessment.course_id == course_id)).one()
    return float(row[0] or 0), int(row[1] or 0)


def record_event(db: Session, enrollment: Enrollment, event_type: str, occurred_at: datetime | None = None,
                 payload: dict | None = None, source: str = "web", event_id: str | None = None) -> tuple[LearningEvent, bool]:
    """Inserta un evento de aprendizaje de forma idempotente. Devuelve (evento, creado)."""
    if event_type not in EVENT_TYPES:
        raise HTTPException(422, f"Tipo de evento no soportado: {event_type}")
    event_id = event_id or str(uuid.uuid4())
    existing = db.get(LearningEvent, event_id)
    if existing:
        return existing, False
    occurred_at = occurred_at or datetime.now(timezone.utc)
    if occurred_at > datetime.now(timezone.utc):
        raise HTTPException(422, "La fecha del evento no puede ser futura")
    payload = dict(payload or {})
    duration = payload.get("duration_seconds")
    if duration is not None and not (0 <= float(duration) <= 4 * 3600):
        raise HTTPException(422, "duration_seconds fuera de rango (0–14400)")
    ev = LearningEvent(event_id=event_id, organization_id=enrollment.organization_id, student_id=enrollment.student_id,
                       course_id=enrollment.course_id, enrollment_id=enrollment.id, event_type=event_type,
                       occurred_at=occurred_at, payload=payload, source=source)
    db.add(ev)
    if event_type == "activity_completed" and enrollment.status != "completado":
        course = db.get(Course, enrollment.course_id)
        enrollment.progress = min(enrollment.progress + 1 / max(course.n_activities, 1), 1.0)
    if enrollment.status == "inactivo":
        enrollment.status = "activo"
    db.flush()
    emit(db, enrollment.organization_id, "learning.progress.updated",
         {"student_id": enrollment.student_id, "course_id": enrollment.course_id, "enrollment_id": enrollment.id,
          "event_type": event_type, "progress": round(enrollment.progress, 4)}, aggregate_id=enrollment.id)
    check_completion(db, enrollment)
    return ev, True


def register_grade(db: Session, assessment: Assessment, student_id: str, score: float, graded_by: str,
                   submitted_at: datetime | None = None) -> AssessmentAttempt:
    if not 0 <= score <= assessment.max_score:
        raise HTTPException(422, f"La nota debe estar entre 0 y {assessment.max_score}")
    enrollment = db.scalar(select(Enrollment).where(Enrollment.student_id == student_id,
                                                    Enrollment.course_id == assessment.course_id))
    if not enrollment:
        raise HTTPException(422, "El estudiante no está matriculado en el curso")
    attempts = db.scalar(select(func.count(AssessmentAttempt.id)).where(
        AssessmentAttempt.assessment_id == assessment.id, AssessmentAttempt.student_id == student_id)) or 0
    if attempts >= assessment.max_attempts:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Se alcanzó el máximo de {assessment.max_attempts} intentos")
    submitted_at = submitted_at or datetime.now(timezone.utc)
    normalized = score / assessment.max_score * 100
    attempt = AssessmentAttempt(organization_id=assessment.organization_id, assessment_id=assessment.id,
                                student_id=student_id, attempt_number=attempts + 1, score=round(normalized, 1),
                                is_late=submitted_at > assessment.due_at, submitted_at=submitted_at)
    db.add(attempt)
    for cs in db.scalars(select(CourseSkill).where(CourseSkill.course_id == assessment.course_id)):
        db.add(SkillEvidence(organization_id=assessment.organization_id, student_id=student_id, skill_id=cs.skill_id,
                             source_type="evaluacion", score=round(normalized / 100, 3), observed_at=submitted_at,
                             validated_by=graded_by))
    db.flush()
    emit(db, assessment.organization_id, "assessment.submitted",
         {"student_id": student_id, "course_id": assessment.course_id, "assessment_id": assessment.id,
          "attempt_id": attempt.id, "score": attempt.score, "is_late": attempt.is_late}, aggregate_id=attempt.id)
    record_event(db, enrollment, "assessment_submitted", submitted_at, {"score": attempt.score}, source="web")
    return attempt


def check_completion(db: Session, enrollment: Enrollment) -> bool:
    if enrollment.status == "completado" or enrollment.progress < COMPLETION_ACTIVITY_RATIO - 1e-6:
        return False
    avg, n = course_average(db, enrollment.student_id, enrollment.course_id)
    if n < COMPLETION_MIN_ATTEMPTS or avg < COMPLETION_MIN_AVERAGE:
        return False
    enrollment.status, enrollment.completed_at = "completado", datetime.now(timezone.utc)
    emit(db, enrollment.organization_id, "course.completed",
         {"student_id": enrollment.student_id, "course_id": enrollment.course_id, "average": round(avg, 1)},
         aggregate_id=enrollment.id)
    if avg >= CERTIFICATE_MIN_AVERAGE:
        issue_certificate(db, enrollment, None)
    return True


def issue_certificate(db: Session, enrollment: Enrollment, issued_by: str | None) -> Certificate:
    if enrollment.status != "completado":
        raise HTTPException(422, "El curso aún no cumple los criterios de finalización")
    avg, _ = course_average(db, enrollment.student_id, enrollment.course_id)
    if avg < CERTIFICATE_MIN_AVERAGE:
        raise HTTPException(422,
                            f"Promedio {avg:.1f} menor al mínimo de {CERTIFICATE_MIN_AVERAGE} para certificar")
    if db.scalar(select(Certificate.id).where(Certificate.student_id == enrollment.student_id,
                                              Certificate.course_id == enrollment.course_id,
                                              Certificate.status == "emitido")):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe un certificado vigente para este logro")
    year = datetime.now(timezone.utc).year
    cert = Certificate(organization_id=enrollment.organization_id, student_id=enrollment.student_id,
                       course_id=enrollment.course_id, folio=f"EDU-{year}-{secrets.token_hex(4).upper()}",
                       verification_code=secrets.token_urlsafe(8)[:10].upper(), final_score=round(avg, 1),
                       issued_by=issued_by)
    db.add(cert)
    db.flush()
    course = db.get(Course, enrollment.course_id)
    db.add(Notification(organization_id=enrollment.organization_id, type="certificado",
                        title="Certificado emitido", body=f"Se emitió el certificado {cert.folio} de {course.title}."))
    emit(db, enrollment.organization_id, "notification.created",
         {"student_id": enrollment.student_id, "course_id": enrollment.course_id, "certificate_id": cert.id,
          "folio": cert.folio, "title": f"Certificado emitido: {course.title}"}, aggregate_id=cert.id)
    return cert
