"""Generador del dataset DEMO. Los datos son sintéticos y deben mostrarse siempre como DEMO."""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

REGIONS = ["Lima", "Arequipa", "Cusco", "La Libertad", "Piura", "Junín", "Lambayeque", "Loreto"]

SKILLS = [
    ("IA", "Inteligencia Artificial"),
    ("ML", "Machine Learning"),
    ("DS", "Ciencia de Datos"),
    ("WEB", "Desarrollo Web"),
    ("CLOUD", "Nube y DevOps"),
]

LEVELS = ["basico", "intermedio", "avanzado"]

# code, title, level, {skill_code: weight}
COURSES = [
    ("PY101", "Fundamentos de Python", "basico", {"DS": 0.5, "WEB": 0.5}),
    ("DS201", "Análisis de Datos con Pandas", "intermedio", {"DS": 1.0}),
    ("ML201", "Machine Learning Básico", "intermedio", {"ML": 0.7, "DS": 0.3}),
    ("ML301", "Machine Learning Avanzado", "avanzado", {"ML": 1.0}),
    ("IA301", "Inteligencia Artificial Avanzada", "avanzado", {"IA": 0.7, "ML": 0.3}),
    ("IA201", "Redes Neuronales con PyTorch", "intermedio", {"IA": 1.0}),
    ("WEB101", "HTML, CSS y JavaScript", "basico", {"WEB": 1.0}),
    ("WEB201", "React y TypeScript", "intermedio", {"WEB": 1.0}),
    ("CLD101", "Linux y Docker", "basico", {"CLOUD": 1.0}),
    ("CLD201", "Despliegue en la Nube", "intermedio", {"CLOUD": 0.8, "WEB": 0.2}),
]

ACTIVITIES_PER_COURSE = 12
ASSESSMENT_WEEKS = [3, 6, 9, 12]
WEEKS = 12

FIRST_NAMES = ["Ana", "Luis", "María", "José", "Lucía", "Carlos", "Sofía", "Diego", "Valeria", "Jorge",
               "Camila", "Miguel", "Daniela", "Andrés", "Paula", "Renzo", "Fernanda", "Bruno", "Ximena", "Piero"]
LAST_NAMES = ["Quispe", "Flores", "Rojas", "Huamán", "Mendoza", "Torres", "Vargas", "Castillo", "Ramos",
              "Chávez", "Gutiérrez", "Paredes", "Salazar", "Cárdenas", "Ríos", "Vega"]


@dataclass
class DemoData:
    start: datetime
    students: list[dict] = field(default_factory=list)
    courses: list[dict] = field(default_factory=list)
    skills: list[dict] = field(default_factory=list)
    course_skills: list[dict] = field(default_factory=list)
    assessments: list[dict] = field(default_factory=list)
    enrollments: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    skill_evidence: list[dict] = field(default_factory=list)


def _uid() -> str:
    return str(uuid.uuid4())


def _sigmoid(x: float) -> float:
    return 1 / (1 + math.exp(-x))


def generate(n_students: int = 240, seed: int = 42, now: datetime | None = None) -> DemoData:
    rng = random.Random(seed)
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(weeks=WEEKS)).replace(hour=0, minute=0, second=0, microsecond=0)
    data = DemoData(start=start)

    skill_ids = {}
    for code, name in SKILLS:
        sid = _uid()
        skill_ids[code] = sid
        data.skills.append({"id": sid, "code": code, "name": name})

    for code, title, level, skills in COURSES:
        cid = _uid()
        data.courses.append({"id": cid, "code": code, "title": title, "level": level,
                             "n_activities": ACTIVITIES_PER_COURSE})
        for scode, weight in skills.items():
            data.course_skills.append({"course_id": cid, "skill_id": skill_ids[scode], "weight": weight})
        for i, week in enumerate(ASSESSMENT_WEEKS, start=1):
            data.assessments.append({
                "id": _uid(), "course_id": cid, "title": f"Evaluación {i} – {title}",
                "type": "opcion_multiple" if i % 2 else "practica", "max_score": 100.0,
                "due_at": start + timedelta(weeks=week, hours=-1), "weight": 0.25,
            })

    assessments_by_course: dict[str, list[dict]] = {}
    for a in data.assessments:
        assessments_by_course.setdefault(a["course_id"], []).append(a)
    skills_by_course: dict[str, list[dict]] = {}
    for cs in data.course_skills:
        skills_by_course.setdefault(cs["course_id"], []).append(cs)
    level_penalty = {"basico": 0.0, "intermedio": 0.08, "avanzado": 0.16}

    for i in range(n_students):
        sid = _uid()
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        engagement = rng.betavariate(2.2, 1.6)
        ability = rng.betavariate(3, 2)
        data.students.append({
            "id": sid, "full_name": name, "external_code": f"DEMO-{i + 1:04d}",
            "region": rng.choices(REGIONS, weights=[40, 12, 10, 9, 8, 8, 7, 6])[0],
            "cohort": rng.choice(["2026-A", "2026-B"]),
        })
        for course in rng.sample(data.courses, k=rng.choice([1, 2, 2, 3])):
            _simulate_enrollment(rng, data, sid, course, engagement, ability, level_penalty[course["level"]],
                                 assessments_by_course[course["id"]], skills_by_course[course["id"]], now)
    data.events.sort(key=lambda e: e["occurred_at"])
    return data


def _simulate_enrollment(rng, data, student_id, course, engagement, ability, penalty, assessments, course_skills, now):
    start = data.start
    enroll_week = rng.choice([0, 0, 0, 1, 2])
    eng = min(max(engagement + rng.gauss(0, 0.12), 0.02), 0.98)
    abil = min(max(ability - penalty + rng.gauss(0, 0.08), 0.05), 0.98)
    drop_prob = _sigmoid(-5 * (eng - 0.35))
    drop_week = rng.randint(enroll_week + 2, WEEKS) if rng.random() < drop_prob else None

    eid = _uid()
    enrollment = {"id": eid, "student_id": student_id, "course_id": course["id"],
                  "enrolled_at": start + timedelta(weeks=enroll_week), "status": "activo", "completed_at": None}
    data.enrollments.append(enrollment)

    completed_activities = 0
    scores: list[float] = []
    for week in range(enroll_week, WEEKS):
        if drop_week is not None and week >= drop_week:
            break
        week_start = start + timedelta(weeks=week)
        n_sessions = _poisson(rng, 0.6 + 4.5 * eng)
        for _ in range(n_sessions):
            session_id = _uid()
            t = week_start + timedelta(days=rng.randint(0, 6), hours=rng.randint(8, 22), minutes=rng.randint(0, 59))
            if t > now:
                continue
            for _ in range(rng.randint(1, 3)):
                data.events.append(_event(eid, student_id, course["id"], "lesson_viewed", t, session_id,
                                          duration=int(rng.uniform(180, 1500) * (0.5 + eng))))
                t += timedelta(minutes=rng.randint(5, 25))
            if completed_activities < ACTIVITIES_PER_COURSE and rng.random() < 0.25 + 0.6 * eng:
                completed_activities += 1
                data.events.append(_event(eid, student_id, course["id"], "activity_completed", t, session_id,
                                          duration=int(rng.uniform(300, 1800))))
        for a in assessments:
            due = a["due_at"]
            if week_start <= due < week_start + timedelta(weeks=1) and due <= now:
                if rng.random() < 0.35 + 0.6 * eng:
                    late = rng.random() < (1 - eng) * 0.45
                    submitted = due + (timedelta(days=rng.randint(1, 4)) if late else -timedelta(hours=rng.randint(2, 70)))
                    if submitted > now:
                        continue
                    score = min(max(abil * 100 + rng.gauss(0, 10) - (8 if late else 0), 0), 100)
                    scores.append(score)
                    data.attempts.append({"id": _uid(), "assessment_id": a["id"], "student_id": student_id,
                                          "score": round(score, 1), "submitted_at": submitted, "is_late": late,
                                          "attempt_number": 1})
                    data.events.append(_event(eid, student_id, course["id"], "assessment_submitted", submitted,
                                              None, score=round(score, 1)))
                    for cs in course_skills:
                        evidence = min(max(score / 100 * (0.85 + rng.gauss(0, 0.08)), 0), 1)
                        data.skill_evidence.append({"id": _uid(), "student_id": student_id, "skill_id": cs["skill_id"],
                                                    "source_type": "evaluacion", "score": round(evidence, 3),
                                                    "observed_at": submitted})
        if (enrollment["completed_at"] is None and completed_activities >= ACTIVITIES_PER_COURSE - 2
                and len(scores) >= 2 and sum(scores) / len(scores) >= 55):
            done = week_start + timedelta(days=6, hours=20)
            if done <= now:
                enrollment["completed_at"] = done
                enrollment["status"] = "completado"
                data.events.append(_event(eid, student_id, course["id"], "course_completed", done, None))
                break
    if drop_week is not None and enrollment["status"] != "completado":
        enrollment["status"] = "inactivo"


def _event(enrollment_id, student_id, course_id, event_type, occurred_at, session_id, duration=None, score=None):
    payload = {"session_id": session_id}
    if duration is not None:
        payload["duration_seconds"] = duration
    if score is not None:
        payload["score"] = score
    return {"event_id": _uid(), "enrollment_id": enrollment_id, "student_id": student_id, "course_id": course_id,
            "event_type": event_type, "occurred_at": occurred_at, "payload": payload, "source": "demo"}


def _poisson(rng: random.Random, lam: float) -> int:
    threshold, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1
