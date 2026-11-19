"""Inferencia en vivo: una predicción real cada N segundos por organización, compartida por SSE con todos los
espectadores autorizados. Cada pulso del visualizador corresponde a esta inferencia, no a una animación de adorno."""

import asyncio
import logging
import time

from sqlalchemy import func, select

from app.core.events import bus, envelope
from app.core.security import ADMIN, ANALYST, AUDITOR, COORDINATOR, INSTRUCTOR, ML_ENGINEER, STUDENT, SUPERADMIN
from app.db.models import Course, Enrollment, Skill, Student
from app.db.session import SessionLocal
from app.services import ml

log = logging.getLogger("edumaster.live")
# Los estudiantes no ven predicciones de otros estudiantes.
AUDIENCE = [SUPERADMIN, ADMIN, COORDINATOR, INSTRUCTOR, ANALYST, ML_ENGINEER, AUDITOR]


def run_once(organization_id: str) -> dict | None:
    started = time.perf_counter()
    with SessionLocal() as db:
        mv = ml.production_version(db, organization_id)
        if not mv:
            return None
        pick = db.execute(select(Enrollment.student_id, Enrollment.course_id).where(
            Enrollment.organization_id == organization_id, Enrollment.completed_at.is_(None))
            .order_by(func.random()).limit(1)).first()
        if not pick:
            return None
        rows = [r for r in ml.current_features(db, organization_id, [pick.student_id]) if r["course_id"] == pick.course_id]
        preds = ml.predict_rows(ml.get_predictor(mv), rows, with_activations=True)
        if not preds:
            return None
        p = preds[0]
        own = p["course_skills"]
        top_skill = max(own, key=lambda sid: p["skill_scores"].get(sid, 0)) if own else None
        skill = db.get(Skill, top_skill) if top_skill else None
        course = db.get(Course, pick.course_id)
        student = db.get(Student, pick.student_id)
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        return envelope("model.inference.live", organization_id, {
            "audience_roles": AUDIENCE,
            "student_id": student.id,
            "course_id": course.id,
            "student_code": student.external_code,
            "course_title": course.title,
            "model_version": mv.version,
            "completion_probability": p["completion_probability"],
            "risk_probability": p["risk_probability"],
            "mastery_level": p["mastery_level"],
            "skill_to_reinforce": skill.name if skill else None,
            "activations": p["activations"],
            "latency_ms": latency_ms,
        })


async def live_inference_loop(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        orgs = {s.organization_id for s in bus.subscribers if s.role != STUDENT}
        for org in orgs:
            try:
                event = await asyncio.to_thread(run_once, org)
                if event:
                    bus.publish(event)
            except Exception:  # noqa: BLE001 - un fallo puntual no debe detener el ciclo
                log.exception("Fallo en la inferencia en vivo")
