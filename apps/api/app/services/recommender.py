"""Motor de recomendaciones: combina la salida del modelo con reglas de negocio del catálogo."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.events import emit
from app.db.models import Course, CourseSkill, Enrollment, Prediction, Recommendation, Skill
from app.services import ml

LEVEL_ORDER = {"basico": 0, "intermedio": 1, "avanzado": 2}
RISK_THRESHOLD = 0.5


def generate(db: Session, organization_id: str, student_ids: list[str]) -> list[Recommendation]:
    mv = ml.production_version(db, organization_id)
    if not mv:
        return []
    predictor = ml.get_predictor(mv)
    preds = ml.predict_rows(predictor, ml.current_features(db, organization_id, student_ids))
    skills = {s.id: s for s in db.scalars(select(Skill).where(Skill.organization_id == organization_id))}
    courses = {c.id: c for c in db.scalars(select(Course).where(Course.organization_id == organization_id))}
    skill_courses: dict[str, list[str]] = {}
    for cs in db.scalars(select(CourseSkill)):
        if cs.course_id in courses:
            skill_courses.setdefault(cs.skill_id, []).append(cs.course_id)
    enrolled: dict[str, set[str]] = {}
    for e in db.scalars(select(Enrollment).where(Enrollment.student_id.in_(student_ids))):
        enrolled.setdefault(e.student_id, set()).add(e.course_id)
    pending = {(r.student_id, r.title) for r in db.scalars(select(Recommendation).where(
        Recommendation.student_id.in_(student_ids), Recommendation.status == "nueva"))}

    created = []
    for p in preds:
        course = courses[p["course_id"]]
        db.add(Prediction(organization_id=organization_id, student_id=p["student_id"], course_id=course.id,
                          model_version_id=mv.id, completion_probability=p["completion_probability"],
                          risk_probability=p["risk_probability"], mastery_level=p["mastery_level"],
                          payload={"explanation": p["explanation"], "skill_scores": p["skill_scores"]}))
        own_skills = [sid for sid in p["course_skills"] if sid in skills]
        if not own_skills:
            continue
        top_skill = max(own_skills, key=lambda sid: p["skill_scores"].get(sid, 0))
        factors = ", ".join(f"{f['descripcion']} ({f['efecto']})" for f in p["explanation"]["riesgo"][:2])
        candidates = []
        if p["risk_probability"] >= RISK_THRESHOLD:
            candidates.append(("acompanamiento", f"Retoma {course.title}", "alto", p["risk_probability"],
                               f"Señal de desconexión estimada en {p['risk_probability']:.0%}. Factores: {factors}. "
                               "Se sugiere contacto del docente; no es una sanción.", course.id))
        else:
            candidates.append(("completar", f"Completa {course.title}", "medio", p["completion_probability"],
                               f"Probabilidad estimada de finalizar en 28 días: {p['completion_probability']:.0%}. "
                               f"Nivel de dominio estimado: {p['mastery_level']}.", course.id))
        next_courses = sorted(
            (c for cid in skill_courses.get(top_skill, []) if (c := courses[cid]).status == "publicado"
             and cid not in enrolled.get(p["student_id"], set())
             and LEVEL_ORDER.get(c.level, 0) <= LEVEL_ORDER.get(course.level, 0) + 1),
            key=lambda c: LEVEL_ORDER.get(c.level, 0))
        if next_courses:
            nxt = next_courses[0]
            candidates.append(("ruta", f"Sigue: {nxt.title}", "medio", p["skill_scores"][top_skill],
                               f"Competencia a reforzar: {skills[top_skill].name}. Curso del catálogo publicado que "
                               "no tienes inscrito y respeta la secuencia de niveles.", nxt.id))
        else:
            candidates.append(("refuerzo", f"Repasa: {skills[top_skill].name}", "bajo", p["skill_scores"][top_skill],
                               f"La red prioriza reforzar {skills[top_skill].name} dentro de {course.title}.",
                               course.id))
        for kind, title, impact, score, rationale, target in candidates:
            if (p["student_id"], title) in pending:
                continue
            rec = Recommendation(organization_id=organization_id, student_id=p["student_id"], course_id=target,
                                 skill_id=top_skill, model_version_id=mv.id, type=kind, title=title,
                                 rationale=rationale, impact=impact, score=round(float(score), 4))
            db.add(rec)
            db.flush()
            pending.add((p["student_id"], title))
            created.append(rec)
            emit(db, organization_id, "recommendation.created",
                 {"recommendation_id": rec.id, "student_id": rec.student_id, "course_id": target, "title": title,
                  "type": kind}, aggregate_id=rec.id)
    db.commit()
    return created
