"""KPIs, series, mapa regional, dominio de habilidades, feed y exportaciones, siempre filtrados por permisos."""

import csv
import io
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import INSTRUCTOR, STAFF, STUDENT, Principal, current_user, require, visible_course_ids
from app.db.models import (Certificate, Course, CourseSkill, Enrollment, LearningEvent, Organization, OutboxEvent,
                           Prediction, Skill, SkillEvidence, Student)
from app.db.session import get_db
from app.services import ml
from edumaster_ml.dataset import MASTERY_LEVELS, mastery_bucket

router = APIRouter(tags=["analítica"])
_cache: dict[tuple, tuple[float, object]] = {}
CACHE_TTL = 20


def _cached(key: tuple, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    value = fn()
    _cache[key] = (time.monotonic(), value)
    return value


class Scope:
    def __init__(self, db: Session, user: Principal, course_id: str | None = None, cohort: str | None = None):
        self.org = user.organization_id
        self.student_id = user.student_id if user.role == STUDENT else None
        allowed = visible_course_ids(db, user)
        self.course_ids = allowed
        if course_id:
            self.course_ids = [course_id] if allowed is None or course_id in allowed else []
        self.cohort = cohort
        self.key = (self.org, self.student_id, tuple(self.course_ids) if self.course_ids is not None else None, cohort)

    def apply(self, stmt, model):
        stmt = stmt.where(model.organization_id == self.org)
        if self.student_id and hasattr(model, "student_id"):
            stmt = stmt.where(model.student_id == self.student_id)
        if self.course_ids is not None and hasattr(model, "course_id"):
            stmt = stmt.where(model.course_id.in_(self.course_ids))
        if self.cohort and hasattr(model, "student_id"):
            stmt = stmt.where(model.student_id.in_(select(Student.id).where(Student.cohort == self.cohort)))
        return stmt


def scope_dep(course_id: str | None = None, cohort: str | None = None, user: Principal = Depends(current_user),
              db: Session = Depends(get_db)) -> Scope:
    return Scope(db, user, course_id, cohort)


def _events(db: Session, scope: Scope, since: datetime) -> list[tuple]:
    stmt = scope.apply(select(LearningEvent.student_id, LearningEvent.event_type, LearningEvent.occurred_at,
                              LearningEvent.payload).where(LearningEvent.occurred_at >= since), LearningEvent)
    return db.execute(stmt).all()


def _kpi(key, label, value, previous, unit="", as_of=None, source="base transaccional"):
    change = None if previous in (None, 0) else round((value - previous) / previous, 4)
    return {"key": key, "label": label, "value": value, "previous": previous, "change": change, "unit": unit,
            "as_of": (as_of or datetime.now(timezone.utc)).isoformat(), "source": source}


@router.get("/dashboard/summary")
def dashboard_summary(days: int = Query(30, ge=7, le=180), scope: Scope = Depends(scope_dep),
                      db: Session = Depends(get_db)):
    def compute():
        now = datetime.now(timezone.utc)
        start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
        events = _events(db, scope, prev_start)
        cur = [e for e in events if e.occurred_at >= start]
        prev = [e for e in events if e.occurred_at < start]
        hours = lambda rows: round(sum((e.payload or {}).get("duration_seconds", 0) for e in rows) / 3600, 1)
        enr = db.execute(scope.apply(select(Enrollment.status, Enrollment.completed_at), Enrollment)).all()
        completed = [e for e in enr if e.status == "completado"]
        course_stmt = select(func.count(Course.id)).where(Course.organization_id == scope.org,
                                                          Course.status == "publicado")
        if scope.course_ids is not None:
            course_stmt = course_stmt.where(Course.id.in_(scope.course_ids))
        if scope.student_id:
            course_stmt = course_stmt.where(Course.id.in_(select(Enrollment.course_id).where(
                Enrollment.student_id == scope.student_id)))
        certs = db.execute(scope.apply(select(Certificate.issued_at).where(Certificate.status == "emitido"),
                                       Certificate)).all()
        own = scope.student_id is not None
        active = (lambda rows: len({e.occurred_at.date() for e in rows})) if own \
            else (lambda rows: len({e.student_id for e in rows}))
        kpis = [
            _kpi("active_students", "Mis días activos" if own else "Estudiantes activos", active(cur), active(prev)),
            _kpi("courses", "Mis cursos" if own else "Cursos", db.scalar(course_stmt) or 0, None),
            _kpi("learning_hours", "Horas de aprendizaje", hours(cur), hours(prev), "h"),
            _kpi("certificates", "Mis certificados" if own else "Certificaciones", len(certs), len([c for c in certs if c.issued_at < start])),
            _kpi("completion_rate", "Mis cursos completados" if own else "Tasa de finalización", round(len(completed) / len(enr), 4) if enr else 0,
                 round(len([c for c in completed if c.completed_at and c.completed_at < start]) / len(enr), 4)
                 if enr else None, "%"),
        ]
        mv = ml.production_version(db, scope.org)
        model = None
        if mv:
            test = mv.metrics.get("test", {})
            model = {"version": mv.version, "deployed_at": mv.deployed_at, "dataset_demo": mv.model_card.get(
                "datos", {}).get("es_demo"), "completion_pr_auc": test.get("finalizacion", {}).get("pr_auc"),
                "completion_roc_auc": test.get("finalizacion", {}).get("roc_auc"),
                "risk_pr_auc": test.get("riesgo", {}).get("pr_auc"),
                "supera_baseline": mv.model_card.get("supera_baseline")}
        org = db.get(Organization, scope.org)
        return {"period_days": days, "kpis": kpis, "model": model, "is_demo": org.is_demo,
                "generated_at": now.isoformat()}

    return _cached(("summary", days, *scope.key), compute)


@router.get("/analytics/progress")
def progress_series(weeks: int = Query(12, ge=2, le=52), scope: Scope = Depends(scope_dep),
                    db: Session = Depends(get_db)):
    def compute():
        now = datetime.now(timezone.utc)
        start = (now - timedelta(weeks=weeks)).replace(hour=0, minute=0, second=0, microsecond=0)
        buckets = [{"week_start": (start + timedelta(weeks=i)).date().isoformat(), "active_students": set(),
                    "events": 0, "activities": 0, "scores": [], "completions": 0, "hours": 0.0}
                   for i in range(weeks)]
        for e in _events(db, scope, start):
            i = min(int((e.occurred_at - start).days // 7), weeks - 1)
            b = buckets[i]
            b["active_students"].add(e.student_id)
            b["events"] += 1
            p = e.payload or {}
            b["hours"] += p.get("duration_seconds", 0) / 3600
            if e.event_type == "activity_completed":
                b["activities"] += 1
            elif e.event_type == "assessment_submitted" and "score" in p:
                b["scores"].append(p["score"])
            elif e.event_type == "course_completed":
                b["completions"] += 1
        return [{"week_start": b["week_start"], "active_students": len(b["active_students"]), "events": b["events"],
                 "activities": b["activities"], "completions": b["completions"], "hours": round(b["hours"], 1),
                 "avg_score": round(sum(b["scores"]) / len(b["scores"]), 1) if b["scores"] else None}
                for b in buckets]

    return _cached(("progress", weeks, *scope.key), compute)


@router.get("/analytics/completion")
def completion(scope: Scope = Depends(scope_dep), db: Session = Depends(get_db)):
    counts = Counter()
    for status_, progress in db.execute(scope.apply(select(Enrollment.status, Enrollment.progress), Enrollment)):
        if status_ == "completado":
            counts["completado"] += 1
        elif status_ == "inactivo":
            counts["inactivo"] += 1
        elif progress > 0:
            counts["en_progreso"] += 1
        else:
            counts["no_iniciado"] += 1
    total = sum(counts.values())
    return {"total": total, "segments": [{"key": k, "count": counts[k], "share": round(counts[k] / total, 4) if total else 0}
                                         for k in ("completado", "en_progreso", "no_iniciado", "inactivo")]}


@router.get("/analytics/regions")
def regions(scope: Scope = Depends(scope_dep), db: Session = Depends(get_db)):
    def compute():
        since = datetime.now(timezone.utc) - timedelta(days=30)
        active = {e.student_id for e in _events(db, scope, since)}
        rows = db.execute(scope.apply(select(Enrollment.student_id, Enrollment.status, Student.region)
                                      .join(Student, Student.id == Enrollment.student_id), Enrollment)).all()
        agg = defaultdict(lambda: {"students": set(), "active": set(), "enrollments": 0, "completed": 0})
        for sid, status_, region in rows:
            a = agg[region]
            a["students"].add(sid)
            a["enrollments"] += 1
            a["completed"] += status_ == "completado"
            if sid in active:
                a["active"].add(sid)
        return sorted(({"region": r, "students": len(a["students"]), "active_30d": len(a["active"]),
                        "completion_rate": round(a["completed"] / a["enrollments"], 4) if a["enrollments"] else 0}
                       for r, a in agg.items()), key=lambda x: -x["students"])

    return _cached(("regions", *scope.key), compute)


@router.get("/analytics/skill-mastery")
def skill_mastery(scope: Scope = Depends(scope_dep), db: Session = Depends(get_db)):
    def compute():
        skills = list(db.scalars(select(Skill).where(Skill.organization_id == scope.org).order_by(Skill.code)))
        evidence = db.execute(scope.apply(select(SkillEvidence.student_id, SkillEvidence.skill_id,
                                                 SkillEvidence.score), SkillEvidence)).all()
        per_student = defaultdict(list)
        for sid, skill_id, score in evidence:
            per_student[(sid, skill_id)].append(score)
        validated = defaultdict(Counter)
        for (sid, skill_id), scores in per_student.items():
            validated[skill_id][MASTERY_LEVELS[mastery_bucket(sum(scores) / len(scores) * 100)]] += 1
        course_skills = defaultdict(list)
        for cs in db.scalars(select(CourseSkill)):
            course_skills[cs.course_id].append(cs.skill_id)
        estimated = defaultdict(Counter)
        latest = {}
        for p in db.scalars(scope.apply(select(Prediction).order_by(Prediction.created_at), Prediction)):
            latest[(p.student_id, p.course_id)] = p.mastery_level
        for (sid, cid), level in latest.items():
            for skill_id in course_skills[cid]:
                estimated[skill_id][level] += 1

        def row(counter):
            total = sum(counter.values())
            return [{"level": lvl, "count": counter[lvl], "share": round(counter[lvl] / total, 4) if total else 0}
                    for lvl in MASTERY_LEVELS]

        return {"levels": MASTERY_LEVELS,
                "skills": [{"skill_id": s.id, "code": s.code, "name": s.name, "validated": row(validated[s.id]),
                            "estimated_ia": row(estimated[s.id])} for s in skills],
                "note": "‘validated’ proviene de evaluaciones calificadas; ‘estimated_ia’ es una estimación del modelo."}

    return _cached(("skills", *scope.key), compute)


FEED_TYPES = ["course.completed", "assessment.submitted", "recommendation.created", "notification.created",
              "model.training.completed", "model.deployed"]


@router.get("/activity")
def activity(limit: int = Query(20, le=100), user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    scope = Scope(db, user)
    stmt = select(OutboxEvent).where(OutboxEvent.organization_id == user.organization_id,
                                     OutboxEvent.event_type.in_(FEED_TYPES)).order_by(OutboxEvent.created_at.desc())
    students = {}
    courses = {c.id: c.title for c in db.scalars(select(Course).where(Course.organization_id == user.organization_id))}
    out = []
    for ev in db.scalars(stmt.limit(limit * 5)):
        p = ev.payload
        if scope.student_id and p.get("student_id") not in (None, scope.student_id):
            continue
        if scope.course_ids is not None and p.get("course_id") and p["course_id"] not in scope.course_ids:
            continue
        if scope.student_id and ev.event_type.startswith("model."):
            continue
        sid = p.get("student_id")
        if sid and sid not in students:
            st = db.get(Student, sid)
            students[sid] = st.full_name if st else "Estudiante"
        out.append({"id": ev.id, "type": ev.event_type, "occurred_at": ev.created_at,
                    "student": students.get(sid), "course": courses.get(p.get("course_id")), "payload": p})
        if len(out) >= limit:
            break
    return out


@router.get("/analytics/export")
def export(kind: str = Query("progress", pattern="^(progress|students)$"),
           user: Principal = Depends(require(*STAFF, INSTRUCTOR)), db: Session = Depends(get_db)):
    scope = Scope(db, user)
    buf = io.StringIO()
    writer = csv.writer(buf)
    generated = datetime.now(timezone.utc).isoformat()
    writer.writerow([f"# EduMaster export '{kind}' generado {generated} por {user.email}"])
    if kind == "progress":
        writer.writerow(["semana", "estudiantes_activos", "eventos", "actividades", "finalizaciones", "horas",
                         "nota_promedio"])
        for r in progress_series(12, scope, db):
            writer.writerow([r["week_start"], r["active_students"], r["events"], r["activities"], r["completions"],
                             r["hours"], r["avg_score"]])
    else:
        writer.writerow(["codigo", "region", "cohorte", "curso", "estado", "avance"])
        rows = db.execute(scope.apply(select(Student.external_code, Student.region, Student.cohort, Course.code,
                                             Enrollment.status, Enrollment.progress)
                                      .join(Student, Student.id == Enrollment.student_id)
                                      .join(Course, Course.id == Enrollment.course_id), Enrollment))
        for r in rows:
            writer.writerow([*r[:5], round(r[5], 3)])
    audit(db, user, "exportar", "analytics", None, tipo=kind)
    db.commit()
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="edumaster-{kind}.csv"'})
