"""Carga el entorno DEMO: organización, usuarios por rol, catálogo y actividad sintética marcada como DEMO."""

import secrets
from collections import defaultdict

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.core.security import (ADMIN, ANALYST, AUDITOR, COORDINATOR, INSTRUCTOR, ML_ENGINEER, STUDENT, SUPERADMIN,
                               hash_password)
from app.db.models import (OutboxEvent, Assessment, AssessmentAttempt, AuditLog, Certificate, Course, CourseSkill, Enrollment,
                           LearningEvent, LearningPath, LearningPathStep, Notification, Organization, Skill,
                           SkillEvidence, Student, User)
from edumaster_ml import synthetic

DEMO_PASSWORD = "Demo2026!"

DEMO_USERS = [
    (SUPERADMIN, "superadmin@edumaster.demo", "Superadministrador DEMO"),
    (ADMIN, "admin@edumaster.demo", "Administración Institucional"),
    (COORDINATOR, "coordinador@edumaster.demo", "Coordinación Académica"),
    (INSTRUCTOR, "docente@edumaster.demo", "Dra. Lucía López"),
    (INSTRUCTOR, "docente2@edumaster.demo", "Ing. Carlos Ramos"),
    (INSTRUCTOR, "docente3@edumaster.demo", "Mg. Sofía Vega"),
    (STUDENT, "estudiante@edumaster.demo", None),
    (ANALYST, "analista@edumaster.demo", "Analista de Datos"),
    (ML_ENGINEER, "ml@edumaster.demo", "ML Engineer"),
    (AUDITOR, "auditor@edumaster.demo", "Auditoría"),
]


def seed_demo(db: Session, n_students: int = 240) -> Organization | None:
    if db.scalar(select(Organization.id).limit(1)):
        return None
    data = synthetic.generate(n_students=n_students)
    org = Organization(name="Instituto EduMaster (DEMO)", code="DEMO", is_demo=True)
    other = Organization(name="Institución Aliada (DEMO)", code="ALIADA", is_demo=True)
    db.add_all([org, other])
    db.flush()

    pw = hash_password(DEMO_PASSWORD)
    users = {}
    active_by_student = defaultdict(int)
    for e in data.enrollments:
        if e["completed_at"] is None and e["status"] == "activo":
            active_by_student[e["student_id"]] += 1
    first_student = max(data.students, key=lambda s: active_by_student[s["id"]])
    for role, email, name in DEMO_USERS:
        u = User(organization_id=org.id, email=email, full_name=name or first_student["full_name"], role=role,
                 password_hash=pw)
        db.add(u)
        users[email] = u
    db.add(User(organization_id=other.id, email="admin@aliada.demo", full_name="Admin Aliada", role=ADMIN,
                password_hash=pw))
    db.flush()
    instructors = [users[e] for e in ("docente@edumaster.demo", "docente2@edumaster.demo", "docente3@edumaster.demo")]

    for s in data.skills:
        db.add(Skill(id=s["id"], organization_id=org.id, code=s["code"], name=s["name"]))
    db.flush()
    for i, c in enumerate(data.courses):
        db.add(Course(id=c["id"], organization_id=org.id, code=c["code"], title=c["title"], level=c["level"],
                      description=f"Curso DEMO de nivel {c['level']}.", status="publicado",
                      instructor_id=instructors[i % len(instructors)].id, n_activities=c["n_activities"]))
    db.flush()
    db.execute(insert(CourseSkill), data.course_skills)
    db.execute(insert(Assessment), [{**a, "organization_id": org.id} for a in data.assessments])
    db.execute(insert(Student), [{**s, "organization_id": org.id,
                                  "user_id": users["estudiante@edumaster.demo"].id if s is first_student else None}
                                 for s in data.students])

    activities = defaultdict(int)
    for e in data.events:
        if e["event_type"] == "activity_completed":
            activities[e["enrollment_id"]] += 1
    db.execute(insert(Enrollment), [{**e, "organization_id": org.id,
                                     "progress": min(activities[e["id"]] / synthetic.ACTIVITIES_PER_COURSE, 1.0)}
                                    for e in data.enrollments])
    for i in range(0, len(data.events), 5000):
        db.execute(insert(LearningEvent), [{**e, "organization_id": org.id} for e in data.events[i:i + 5000]])
    db.execute(insert(AssessmentAttempt), [{**a, "organization_id": org.id} for a in data.attempts])
    db.execute(insert(SkillEvidence), [{**ev, "organization_id": org.id} for ev in data.skill_evidence])

    assessment_course = {a["id"]: a["course_id"] for a in data.assessments}
    scores = defaultdict(list)
    for a in data.attempts:
        scores[(a["student_id"], assessment_course[a["assessment_id"]])].append(a["score"])
    for e in data.enrollments:
        if e["completed_at"]:
            avg = sum(scores[(e["student_id"], e["course_id"])]) / max(len(scores[(e["student_id"], e["course_id"])]), 1)
            if avg >= 60:
                db.add(Certificate(organization_id=org.id, student_id=e["student_id"], course_id=e["course_id"],
                                   folio=f"EDU-DEMO-{secrets.token_hex(4).upper()}",
                                   verification_code=secrets.token_urlsafe(8)[:10].upper(),
                                   final_score=round(avg, 1), issued_at=e["completed_at"]))

    feed_types = {"course_completed": "course.completed", "assessment_submitted": "assessment.submitted"}
    recent = [e for e in data.events if e["event_type"] in feed_types][-40:]
    db.execute(insert(OutboxEvent), [{
        "organization_id": org.id, "event_type": feed_types[e["event_type"]], "aggregate_id": e["enrollment_id"],
        "payload": {"student_id": e["student_id"], "course_id": e["course_id"], **({"score": e["payload"]["score"]}
                                                                                  if "score" in e["payload"] else {})},
        "created_at": e["occurred_at"], "dispatched_at": e["occurred_at"]} for e in recent])

    by_code = {c["code"]: c["id"] for c in data.courses}
    path = LearningPath(organization_id=org.id, title="Ruta: Ciencia de Datos e IA",
                        objective="Pasar de Python básico a redes neuronales.", status="publicado")
    path.steps = [LearningPathStep(position=i, course_id=by_code[code])
                  for i, code in enumerate(["PY101", "DS201", "ML201", "IA201"], start=1)]
    db.add(path)
    db.add(Notification(organization_id=org.id, user_id=None, type="sistema", title="Entorno DEMO cargado",
                        body="Los datos visibles son sintéticos y están marcados como DEMO."))
    db.add(AuditLog(organization_id=org.id, action="seed_demo", resource="organization", resource_id=org.id,
                    detail={"students": n_students, "events": len(data.events)}))
    db.commit()
    return org
