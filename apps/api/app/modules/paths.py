"""Planes y rutas de aprendizaje (manual 4.2 y 11.2).

Flujo: el coordinador crea la ruta (borrador) → define pasos, duración, criterio de logro y competencias →
el sistema valida prerrequisitos → publica → asigna a cohorte o estudiantes → el estudiante inicia cada paso
cuando cumple el criterio del anterior → el avance se calcula con el progreso real de sus matrículas.
Cada cambio queda en el historial versionado y en auditoría.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.events import emit
from app.core.security import (COORDINATOR, INSTRUCTOR, MANAGERS, STUDENT, Principal, current_user, require,
                               visible_course_ids)
from app.db.models import (Course, CourseSkill, Enrollment, LearningPath, LearningPathStep, LearningPathStepRule,
                           Notification, PathAssignment, PathHistory, Skill, Student, User)
from app.db.session import get_db

router = APIRouter(prefix="/learning-paths", tags=["rutas"])
EDITORS = (*MANAGERS, COORDINATOR)
LEVEL_ORDER = {"basico": 1, "intermedio": 2, "avanzado": 3}


class StepIn(BaseModel):
    course_id: str
    required: bool = True
    duration_weeks: int = Field(default=4, ge=1, le=52)
    min_progress: float = Field(default=0.8, ge=0.1, le=1.0)
    criteria: str = Field(default="", max_length=300)


class PathIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    objective: str = Field(default="", max_length=600)
    steps: list[StepIn] = Field(default_factory=list, max_length=20)


class AssignIn(BaseModel):
    cohort: str | None = None
    student_ids: list[str] = Field(default_factory=list, max_length=500)


# ---------- utilidades ----------

def _path(db: Session, user: Principal, path_id: str) -> LearningPath:
    p = db.get(LearningPath, path_id)
    if not p or p.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta no encontrada")
    return p


def _rules(db: Session, path_id: str) -> dict[int, LearningPathStepRule]:
    return {r.position: r for r in db.scalars(select(LearningPathStepRule).where(LearningPathStepRule.path_id == path_id))}


def _skills_by_course(db: Session, course_ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not course_ids:
        return out
    for cid, name in db.execute(select(CourseSkill.course_id, Skill.name).join(Skill, Skill.id == CourseSkill.skill_id)
                                .where(CourseSkill.course_id.in_(course_ids))):
        out.setdefault(cid, []).append(name)
    return out


def _steps(db: Session, p: LearningPath) -> list[dict]:
    rules = _rules(db, p.id)
    courses = {c.id: c for c in db.scalars(select(Course).where(Course.id.in_([s.course_id for s in p.steps])))}
    skills = _skills_by_course(db, list(courses))
    out = []
    for s in p.steps:
        c, r = courses[s.course_id], rules.get(s.position)
        out.append({"position": s.position, "course_id": c.id, "code": c.code, "title": c.title, "level": c.level,
                    "course_status": c.status, "required": s.required, "skills": skills.get(c.id, []),
                    "duration_weeks": r.duration_weeks if r else 4, "min_progress": r.min_progress if r else 0.8,
                    "criteria": r.criteria if r else ""})
    return out


def _snapshot(db: Session, p: LearningPath) -> dict:
    return {"title": p.title, "objective": p.objective, "status": p.status,
            "steps": [{k: s[k] for k in ("position", "code", "title", "required", "duration_weeks", "min_progress")}
                      for s in _steps(db, p)]}


def _history(db: Session, p: LearningPath, user: Principal | None, action: str, summary: str) -> None:
    db.add(PathHistory(path_id=p.id, version=p.version, action=action, actor_id=user.user_id if user else None,
                       summary=summary, snapshot=_snapshot(db, p)))


def validate(db: Session, p: LearningPath) -> dict:
    """Revisa prerrequisitos y disponibilidad antes de publicar o asignar."""
    errors, warnings = [], []
    steps = _steps(db, p)
    if not steps:
        errors.append("La ruta no tiene pasos.")
    if steps and not any(s["required"] for s in steps):
        errors.append("Al menos un paso debe ser obligatorio.")
    seen = set()
    for s in steps:
        if s["course_id"] in seen:
            errors.append(f"El curso {s['code']} está repetido.")
        seen.add(s["course_id"])
        if s["course_status"] != "publicado":
            errors.append(f"El curso {s['code']} no está publicado ({s['course_status']}).")
        if not s["skills"]:
            warnings.append(f"El curso {s['code']} no tiene competencias vinculadas.")
    for prev, cur in zip(steps, steps[1:]):
        if LEVEL_ORDER.get(cur["level"], 0) < LEVEL_ORDER.get(prev["level"], 0):
            errors.append(f"Prerrequisito inconsistente: {prev['code']} ({prev['level']}) va antes que "
                          f"{cur['code']} ({cur['level']}). Ordena de menor a mayor nivel.")
    total = sum(s["duration_weeks"] for s in steps)
    if total > 52:
        warnings.append(f"La ruta dura {total} semanas; considera dividirla.")
    return {"ok": not errors, "errors": errors, "warnings": warnings, "total_weeks": total}


def _enrollments(db: Session, student_ids: list[str], course_ids: list[str]) -> dict[tuple[str, str], Enrollment]:
    if not student_ids or not course_ids:
        return {}
    return {(e.student_id, e.course_id): e for e in db.scalars(select(Enrollment).where(
        Enrollment.student_id.in_(student_ids), Enrollment.course_id.in_(course_ids)))}


def student_progress(steps: list[dict], enr: dict[tuple[str, str], Enrollment], student_id: str) -> dict:
    """Estado de cada paso para un estudiante y su avance total en la ruta."""
    out, blocked_by, done_req, total_req, acc = [], None, 0, 0, 0.0
    next_step = None
    for s in steps:
        e = enr.get((student_id, s["course_id"]))
        progress = e.progress if e else 0.0
        met = bool(e) and (e.status == "completado" or progress >= s["min_progress"])
        if met:
            state = "completado"
        elif blocked_by:
            state = "bloqueado"
        elif e:
            state = "en_curso"
        else:
            state = "disponible"
        if next_step is None and state in ("en_curso", "disponible"):
            next_step = s["position"]
        out.append({"position": s["position"], "state": state, "progress": progress,
                    "enrolled": bool(e), "blocked_by": blocked_by if state == "bloqueado" else None})
        if s["required"]:
            total_req += 1
            done_req += met
            acc += 1.0 if met else min(1.0, progress / s["min_progress"])
            if not met and not blocked_by:
                blocked_by = s["code"]
    pct = acc / total_req if total_req else 0.0
    state = "completada" if total_req and done_req == total_req else "en_curso" if acc > 0 else "sin_iniciar"
    return {"steps": out, "progress": round(pct, 4), "completed_steps": done_req, "required_steps": total_req,
            "state": state, "next_step": next_step}


def _notify(db: Session, org_id: str, user_id: str | None, title: str, body: str, path_id: str) -> None:
    if not user_id:
        return
    db.add(Notification(organization_id=org_id, user_id=user_id, type="ruta", title=title, body=body))
    emit(db, org_id, "notification.created", {"title": title, "user_ids": [user_id]}, aggregate_id=path_id)


def _summary_row(db: Session, user: Principal, p: LearningPath) -> dict:
    steps = _steps(db, p)
    row = {"id": p.id, "title": p.title, "objective": p.objective, "status": p.status, "version": p.version,
           "created_at": p.created_at, "steps": steps, "total_weeks": sum(s["duration_weeks"] for s in steps)}
    if user.role == STUDENT:
        a = db.scalar(select(PathAssignment).where(PathAssignment.path_id == p.id,
                                                   PathAssignment.student_id == user.student_id))
        row["assigned"] = bool(a and a.status == "activa")
        if row["assigned"]:
            enr = _enrollments(db, [user.student_id], [s["course_id"] for s in steps])
            row["me"] = student_progress(steps, enr, user.student_id)
    else:
        ids = list(db.scalars(select(PathAssignment.student_id).where(PathAssignment.path_id == p.id,
                                                                      PathAssignment.status == "activa")))
        enr = _enrollments(db, ids, [s["course_id"] for s in steps])
        progs = [student_progress(steps, enr, sid) for sid in ids]
        row["stats"] = {"assigned": len(ids), "completed": sum(x["state"] == "completada" for x in progs),
                        "avg_progress": round(sum(x["progress"] for x in progs) / len(progs), 4) if progs else 0.0}
    return row


def _set_steps(db: Session, user: Principal, p: LearningPath, steps: list[StepIn]) -> None:
    allowed = visible_course_ids(db, user)
    for s in steps:
        c = db.get(Course, s.course_id)
        if not c or c.organization_id != user.organization_id or (allowed is not None and c.id not in allowed):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Curso no válido en la ruta")
    for r in db.scalars(select(LearningPathStepRule).where(LearningPathStepRule.path_id == p.id)):
        db.delete(r)
    p.steps.clear()
    db.flush()
    for i, s in enumerate(steps, start=1):
        p.steps.append(LearningPathStep(position=i, course_id=s.course_id, required=s.required))
        db.add(LearningPathStepRule(path_id=p.id, position=i, duration_weeks=s.duration_weeks,
                                    min_progress=s.min_progress, criteria=s.criteria.strip()))
    db.flush()


# ---------- endpoints ----------

@router.get("")
def list_paths(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(LearningPath).where(LearningPath.organization_id == user.organization_id)
    if user.role == STUDENT:
        stmt = stmt.where(LearningPath.status == "publicado")
    rows = [_summary_row(db, user, p) for p in db.scalars(stmt.order_by(LearningPath.created_at))]
    if user.role == STUDENT:
        rows.sort(key=lambda r: not r["assigned"])
    return rows


@router.get("/cohorts")
def cohorts(user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    rows = db.execute(select(Student.cohort, Student.id).where(Student.organization_id == user.organization_id)).all()
    count: dict[str, int] = {}
    for c, _ in rows:
        count[c] = count.get(c, 0) + 1
    return [{"cohort": c, "students": n} for c, n in sorted(count.items())]


@router.post("", status_code=201)
def create_path(body: PathIn, user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    p = LearningPath(organization_id=user.organization_id, title=body.title.strip(), objective=body.objective.strip())
    db.add(p)
    db.flush()
    _set_steps(db, user, p, body.steps)
    _history(db, p, user, "crear", f"Ruta creada con {len(body.steps)} pasos")
    audit(db, user, "crear_ruta", "learning_path", p.id, pasos=len(body.steps))
    db.commit()
    return {"id": p.id}


@router.get("/{path_id}")
def detail(path_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    p = _path(db, user, path_id)
    if user.role == STUDENT and p.status != "publicado":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ruta no encontrada")
    row = _summary_row(db, user, p)
    if user.role == STUDENT:
        return row
    row["validation"] = validate(db, p)
    assigns = list(db.scalars(select(PathAssignment).where(PathAssignment.path_id == p.id,
                                                           PathAssignment.status == "activa")))
    students = {s.id: s for s in db.scalars(select(Student).where(Student.id.in_([a.student_id for a in assigns])))}
    if user.role == INSTRUCTOR:
        mine = set(visible_course_ids(db, user) or [])
        my_students = set(db.scalars(select(Enrollment.student_id).where(Enrollment.course_id.in_(mine))))
        assigns = [a for a in assigns if a.student_id in my_students]
    enr = _enrollments(db, [a.student_id for a in assigns], [s["course_id"] for s in row["steps"]])
    out = []
    for a in assigns:
        st = students[a.student_id]
        prog = student_progress(row["steps"], enr, a.student_id)
        out.append({"id": a.id, "student_id": st.id, "name": st.full_name, "code": st.external_code,
                    "cohort": st.cohort, "via": f"cohorte {a.cohort}" if a.cohort else "individual",
                    "assigned_at": a.assigned_at, **prog})
    out.sort(key=lambda x: (-x["progress"], x["name"]))
    row["assignments"] = out
    actors = {u.id: u.full_name for u in db.scalars(select(User).where(User.organization_id == user.organization_id))}
    row["history"] = [{"id": h.id, "version": h.version, "action": h.action, "summary": h.summary,
                       "actor": actors.get(h.actor_id, "Sistema"), "created_at": h.created_at, "snapshot": h.snapshot}
                      for h in db.scalars(select(PathHistory).where(PathHistory.path_id == p.id)
                                          .order_by(PathHistory.created_at.desc()))]
    return row


@router.put("/{path_id}")
def update_path(path_id: str, body: PathIn, user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    p = _path(db, user, path_id)
    if p.status == "archivado":
        raise HTTPException(status.HTTP_409_CONFLICT, "La ruta está archivada; restáurala para editarla")
    before = _snapshot(db, p)
    p.title, p.objective = body.title.strip(), body.objective.strip()
    _set_steps(db, user, p, body.steps)
    if p.status == "publicado":
        v = validate(db, p)
        if not v["ok"]:
            db.rollback()
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No se puede guardar: " + " ".join(v["errors"]))
    after = _snapshot(db, p)
    changes = []
    if before["title"] != after["title"] or before["objective"] != after["objective"]:
        changes.append("datos generales")
    if before["steps"] != after["steps"]:
        changes.append(f"pasos ({len(before['steps'])} → {len(after['steps'])})")
    if not changes:
        db.rollback()
        return {"id": p.id, "version": p.version, "changed": False}
    p.version += 1
    _history(db, p, user, "editar", "Cambió " + " y ".join(changes))
    audit(db, user, "editar_ruta", "learning_path", p.id, version=p.version)
    db.commit()
    return {"id": p.id, "version": p.version, "changed": True}


@router.post("/{path_id}/publish")
def publish(path_id: str, user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    p = _path(db, user, path_id)
    v = validate(db, p)
    if not v["ok"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No se puede publicar: " + " ".join(v["errors"]))
    p.status = "publicado"
    _history(db, p, user, "publicar", "Ruta publicada")
    audit(db, user, "publicar_ruta", "learning_path", p.id)
    db.commit()
    return {"status": p.status}


@router.post("/{path_id}/archive")
def archive(path_id: str, user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    p = _path(db, user, path_id)
    p.status = "archivado" if p.status != "archivado" else "borrador"
    _history(db, p, user, "archivar" if p.status == "archivado" else "restaurar",
             "Ruta archivada" if p.status == "archivado" else "Ruta restaurada a borrador")
    audit(db, user, "archivar_ruta" if p.status == "archivado" else "restaurar_ruta", "learning_path", p.id)
    db.commit()
    return {"status": p.status}


@router.post("/{path_id}/assignments", status_code=201)
def assign(path_id: str, body: AssignIn, user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    p = _path(db, user, path_id)
    if p.status != "publicado":
        raise HTTPException(status.HTTP_409_CONFLICT, "Solo se asignan rutas publicadas")
    targets: dict[str, str | None] = {}
    if body.cohort:
        for sid in db.scalars(select(Student.id).where(Student.organization_id == user.organization_id,
                                                       Student.cohort == body.cohort)):
            targets[sid] = body.cohort
    for sid in body.student_ids:
        st = db.get(Student, sid)
        if not st or st.organization_id != user.organization_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Estudiante no válido")
        targets.setdefault(sid, None)
    if not targets:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Elige una cohorte o al menos un estudiante")
    existing = {a.student_id: a for a in db.scalars(select(PathAssignment).where(
        PathAssignment.path_id == p.id, PathAssignment.student_id.in_(list(targets))))}
    added = 0
    first = p.steps[0].course_id if p.steps else None
    first_title = db.get(Course, first).title if first else ""
    for sid, cohort in targets.items():
        a = existing.get(sid)
        if a and a.status == "activa":
            continue
        if a:
            a.status, a.cohort, a.assigned_by = "activa", cohort, user.user_id
        else:
            db.add(PathAssignment(organization_id=user.organization_id, path_id=p.id, student_id=sid, cohort=cohort,
                                  assigned_by=user.user_id))
        st = db.get(Student, sid)
        _notify(db, user.organization_id, st.user_id, f"Te asignaron la ruta «{p.title}»",
                f"Empieza por {first_title}. Revisa tus pasos en Planes de aprendizaje.", p.id)
        added += 1
    target = f"cohorte {body.cohort}" if body.cohort else f"{len(body.student_ids)} estudiante(s)"
    if added:
        _history(db, p, user, "asignar", f"Asignada a {target}: {added} estudiante(s) nuevos")
        audit(db, user, "asignar_ruta", "learning_path", p.id, cohorte=body.cohort, nuevos=added)
    db.commit()
    return {"added": added, "skipped": len(targets) - added,
            "message": f"Ruta asignada a {added} estudiante(s)" + (f"; {len(targets) - added} ya la tenían" if len(targets) - added else "")}


@router.delete("/{path_id}/assignments/{assignment_id}", status_code=204)
def unassign(path_id: str, assignment_id: str, user: Principal = Depends(require(*EDITORS)),
             db: Session = Depends(get_db)):
    p = _path(db, user, path_id)
    a = db.get(PathAssignment, assignment_id)
    if not a or a.path_id != p.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Asignación no encontrada")
    a.status = "retirada"
    st = db.get(Student, a.student_id)
    _history(db, p, user, "retirar", f"Se retiró a {st.full_name} de la ruta")
    audit(db, user, "retirar_ruta", "learning_path", p.id, student_id=a.student_id)
    db.commit()


@router.post("/{path_id}/join", status_code=201)
def join(path_id: str, user: Principal = Depends(require(STUDENT)), db: Session = Depends(get_db)):
    """El estudiante se une por su cuenta a una ruta publicada."""
    p = _path(db, user, path_id)
    if p.status != "publicado" or not user.student_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "La ruta no está disponible")
    a = db.scalar(select(PathAssignment).where(PathAssignment.path_id == p.id,
                                               PathAssignment.student_id == user.student_id))
    if a and a.status == "activa":
        return {"message": "Ya sigues esta ruta"}
    if a:
        a.status, a.assigned_by, a.cohort = "activa", user.user_id, None
    else:
        db.add(PathAssignment(organization_id=user.organization_id, path_id=p.id, student_id=user.student_id,
                              assigned_by=user.user_id))
    _history(db, p, user, "unirse", f"{user.full_name} se unió a la ruta")
    audit(db, user, "unirse_ruta", "learning_path", p.id)
    db.commit()
    return {"message": f"Te uniste a «{p.title}»"}


@router.post("/{path_id}/steps/{position}/start", status_code=201)
def start_step(path_id: str, position: int, user: Principal = Depends(require(STUDENT)),
               db: Session = Depends(get_db)):
    """El estudiante inicia un paso: valida asignación y prerrequisitos, y lo matricula en el curso."""
    p = _path(db, user, path_id)
    a = db.scalar(select(PathAssignment).where(PathAssignment.path_id == p.id,
                                               PathAssignment.student_id == user.student_id,
                                               PathAssignment.status == "activa"))
    if not a:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Esta ruta no está asignada a ti")
    steps = _steps(db, p)
    step = next((s for s in steps if s["position"] == position), None)
    if not step:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paso no encontrado")
    enr = _enrollments(db, [user.student_id], [s["course_id"] for s in steps])
    state = student_progress(steps, enr, user.student_id)["steps"][position - 1]
    if state["state"] == "bloqueado":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Primero cumple el criterio de {state['blocked_by']}")
    if state["enrolled"]:
        return {"message": f"Ya estás en {step['title']}", "course_id": step["course_id"]}
    if step["course_status"] != "publicado":
        raise HTTPException(status.HTTP_409_CONFLICT, "El curso no está disponible")
    e = Enrollment(organization_id=user.organization_id, student_id=user.student_id, course_id=step["course_id"])
    db.add(e)
    db.flush()
    course = db.get(Course, step["course_id"])
    _notify(db, user.organization_id, course.instructor_id, "Nuevo estudiante desde una ruta",
            f"{user.full_name} inició {course.title} (paso {position} de «{p.title}»).", p.id)
    emit(db, user.organization_id, "learning.progress.updated",
         {"student_id": user.student_id, "course_id": course.id, "enrollment_id": e.id, "event_type": "enrolled",
          "progress": 0}, aggregate_id=e.id)
    audit(db, user, "iniciar_paso_ruta", "learning_path", p.id, paso=position, course_id=course.id)
    db.commit()
    return {"message": f"Te inscribiste en {course.title}", "course_id": course.id}


def ensure_paths_demo(db: Session, organization_id: str) -> int:
    """Completa la ruta DEMO con reglas, historial y asignación a la cohorte de la estudiante DEMO. Idempotente."""
    p = db.scalar(select(LearningPath).where(LearningPath.organization_id == organization_id)
                  .order_by(LearningPath.created_at).limit(1))
    if not p:
        return 0
    changed = 0
    rules = _rules(db, p.id)
    criteria = ["Dominar sintaxis, funciones y estructuras de datos.", "Limpiar y explorar un dataset real con pandas.",
                "Entrenar y evaluar un modelo supervisado.", "Construir y entrenar una red en PyTorch."]
    for s in p.steps:
        if s.position not in rules:
            db.add(LearningPathStepRule(path_id=p.id, position=s.position, duration_weeks=4, min_progress=0.8,
                                        criteria=criteria[s.position - 1] if s.position <= len(criteria) else ""))
            changed += 1
    db.flush()
    if not db.scalar(select(PathHistory.id).where(PathHistory.path_id == p.id).limit(1)):
        _history(db, p, None, "crear", f"Ruta DEMO creada con {len(p.steps)} pasos")
        changed += 1
    if not db.scalar(select(PathAssignment.id).where(PathAssignment.path_id == p.id).limit(1)):
        demo_user = db.scalar(select(User).where(User.email == "estudiante@edumaster.demo"))
        me = demo_user and db.scalar(select(Student).where(Student.user_id == demo_user.id))
        if me:
            for sid in db.scalars(select(Student.id).where(Student.organization_id == organization_id,
                                                           Student.cohort == me.cohort)):
                db.add(PathAssignment(organization_id=organization_id, path_id=p.id, student_id=sid, cohort=me.cohort))
            p.version = max(p.version, 1)
            db.flush()
            _history(db, p, None, "asignar", f"Asignada a la cohorte {me.cohort}")
            changed += 1
    db.commit()
    return changed
