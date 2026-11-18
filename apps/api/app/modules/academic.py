from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.events import emit
from app.core.security import (ADMIN, COORDINATOR, INSTRUCTOR, MANAGERS, STAFF, STUDENT, SUPERADMIN, Principal,
                               current_user, require, visible_course_ids)
from app.db.models import (Assessment, AssessmentAttempt, Course, CourseSkill, Enrollment, LearningPath,
                           LearningPathStep, Skill, Student, User)
from app.db.session import get_db
from app.services import academic

router = APIRouter(tags=["académico"])
LEVELS = "^(basico|intermedio|avanzado)$"


class CourseIn(BaseModel):
    code: str = Field(min_length=2, max_length=30, pattern=r"^[A-Z0-9\-]+$")
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    level: str = Field(pattern=LEVELS)
    instructor_id: str | None = None
    n_activities: int = Field(default=12, ge=1, le=200)
    skills: dict[str, float] = Field(default_factory=dict, description="skill_id -> peso")


class CourseUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=200)
    description: str | None = None
    level: str | None = Field(default=None, pattern=LEVELS)
    instructor_id: str | None = None
    status: str | None = Field(default=None, pattern="^(borrador|publicado|archivado)$")


class AssessmentIn(BaseModel):
    course_id: str
    title: str = Field(min_length=3, max_length=200)
    type: str = Field(pattern="^(opcion_multiple|verdadero_falso|respuesta_corta|archivo|practica)$")
    max_score: float = Field(default=100, gt=0, le=1000)
    weight: float = Field(default=0.25, gt=0, le=1)
    max_attempts: int = Field(default=2, ge=1, le=10)
    due_at: datetime


class GradeIn(BaseModel):
    student_id: str
    score: float = Field(ge=0)
    submitted_at: datetime | None = None


class EnrollIn(BaseModel):
    student_id: str
    course_id: str


class PathIn(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    objective: str = ""
    course_ids: list[str] = Field(min_length=1)


def _course_or_404(db: Session, user: Principal, course_id: str) -> Course:
    course = db.get(Course, course_id)
    if not course or course.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Curso no encontrado")
    allowed = visible_course_ids(db, user)
    if allowed is not None and course.id not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "El curso no está asignado a este docente")
    return course


def _course_out(db: Session, c: Course, student_id: str | None = None) -> dict:
    stats = db.execute(select(func.count(Enrollment.id),
                              func.sum((Enrollment.status == "completado").cast(int_type())),
                              func.avg(Enrollment.progress))
                       .where(Enrollment.course_id == c.id)).one()
    instructor = db.get(User, c.instructor_id) if c.instructor_id else None
    out = {
        "id": c.id, "code": c.code, "title": c.title, "description": c.description, "level": c.level,
        "status": c.status, "n_activities": c.n_activities, "updated_at": c.updated_at,
        "instructor": {"id": instructor.id, "full_name": instructor.full_name} if instructor else None,
        "skills": [{"skill_id": cs.skill_id, "weight": cs.weight} for cs in c.skills],
        "enrolled": int(stats[0] or 0), "completed": int(stats[1] or 0),
        "avg_progress": round(float(stats[2] or 0), 4),
    }
    if student_id:
        enr = db.scalar(select(Enrollment).where(Enrollment.course_id == c.id, Enrollment.student_id == student_id))
        out["my_enrollment"] = ({"status": enr.status, "progress": enr.progress, "completed_at": enr.completed_at}
                                if enr else None)
    return out


def int_type():
    from sqlalchemy import Integer
    return Integer


@router.get("/courses")
def list_courses(q: str | None = None, status_: str | None = Query(default=None, alias="status"),
                 level: str | None = None, mine: bool = False, limit: int = Query(50, le=200), offset: int = 0,
                 user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Course).where(Course.organization_id == user.organization_id)
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Course.id.in_(allowed))
    if user.role == STUDENT:
        if mine:
            stmt = stmt.where(Course.id.in_(select(Enrollment.course_id).where(Enrollment.student_id == user.student_id)))
        else:
            stmt = stmt.where(Course.status == "publicado")
    if q:
        stmt = stmt.where(or_(Course.title.ilike(f"%{q}%"), Course.code.ilike(f"%{q}%")))
    if status_:
        stmt = stmt.where(Course.status == status_)
    if level:
        stmt = stmt.where(Course.level == level)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Course.code).limit(limit).offset(offset))
    return {"total": total, "items": [_course_out(db, c, user.student_id) for c in rows]}


@router.post("/courses", status_code=201)
def create_course(body: CourseIn, user: Principal = Depends(require(*MANAGERS, COORDINATOR)),
                  db: Session = Depends(get_db)):
    if db.scalar(select(Course.id).where(Course.organization_id == user.organization_id, Course.code == body.code)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Ya existe un curso con ese código")
    _check_instructor(db, user, body.instructor_id)
    course = Course(organization_id=user.organization_id, code=body.code, title=body.title,
                    description=body.description, level=body.level, instructor_id=body.instructor_id,
                    n_activities=body.n_activities)
    for skill_id, weight in body.skills.items():
        skill = db.get(Skill, skill_id)
        if not skill or skill.organization_id != user.organization_id:
            raise HTTPException(422, f"Competencia inválida: {skill_id}")
        course.skills.append(CourseSkill(skill_id=skill_id, weight=weight))
    db.add(course)
    db.flush()
    audit(db, user, "crear_curso", "course", course.id, code=body.code)
    db.commit()
    return _course_out(db, course)


def _check_instructor(db: Session, user: Principal, instructor_id: str | None) -> None:
    if instructor_id:
        inst = db.get(User, instructor_id)
        if not inst or inst.organization_id != user.organization_id or inst.role != INSTRUCTOR:
            raise HTTPException(422, "Docente inválido")


@router.get("/courses/{course_id}")
def get_course(course_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    course = _course_or_404(db, user, course_id)
    if user.role == STUDENT and course.status != "publicado":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Curso no encontrado")
    out = _course_out(db, course, user.student_id)
    out["assessments"] = [{"id": a.id, "title": a.title, "type": a.type, "max_score": a.max_score, "weight": a.weight,
                           "max_attempts": a.max_attempts, "due_at": a.due_at, "status": a.status}
                          for a in db.scalars(select(Assessment).where(Assessment.course_id == course.id)
                                              .order_by(Assessment.due_at))]
    return out


@router.patch("/courses/{course_id}")
def update_course(course_id: str, body: CourseUpdate, user: Principal = Depends(require(*MANAGERS, COORDINATOR)),
                  db: Session = Depends(get_db)):
    course = _course_or_404(db, user, course_id)
    changes = body.model_dump(exclude_unset=True)
    _check_instructor(db, user, changes.get("instructor_id"))
    for k, v in changes.items():
        setattr(course, k, v)
    audit(db, user, "publicar_curso" if changes.get("status") == "publicado" else "editar_curso", "course", course.id,
          cambios=changes)
    db.commit()
    return _course_out(db, course)


@router.get("/courses/{course_id}/students")
def course_students(course_id: str, user: Principal = Depends(require(*STAFF, INSTRUCTOR)),
                    db: Session = Depends(get_db)):
    course = _course_or_404(db, user, course_id)
    rows = db.execute(select(Enrollment, Student).join(Student, Student.id == Enrollment.student_id)
                      .where(Enrollment.course_id == course.id).order_by(Student.full_name)).all()
    out = []
    for enr, st in rows:
        avg, n = academic.course_average(db, st.id, course.id)
        out.append({"student_id": st.id, "full_name": st.full_name, "external_code": st.external_code,
                    "region": st.region, "cohort": st.cohort, "status": enr.status, "progress": enr.progress,
                    "average": round(avg, 1), "attempts": n})
    return out


@router.post("/enrollments", status_code=201)
def enroll(body: EnrollIn, user: Principal = Depends(require(*MANAGERS, COORDINATOR)), db: Session = Depends(get_db)):
    course = _course_or_404(db, user, body.course_id)
    student = db.get(Student, body.student_id)
    if not student or student.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Estudiante no encontrado")
    if course.status != "publicado":
        raise HTTPException(422, "Solo se puede matricular en cursos publicados")
    if db.scalar(select(Enrollment.id).where(Enrollment.student_id == student.id, Enrollment.course_id == course.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "El estudiante ya está matriculado")
    enr = Enrollment(organization_id=user.organization_id, student_id=student.id, course_id=course.id)
    db.add(enr)
    db.flush()
    emit(db, user.organization_id, "learning.progress.updated",
         {"student_id": student.id, "course_id": course.id, "enrollment_id": enr.id, "event_type": "enrolled",
          "progress": 0}, aggregate_id=enr.id)
    audit(db, user, "matricular", "enrollment", enr.id, student_id=student.id, course_id=course.id)
    db.commit()
    return {"id": enr.id, "status": enr.status}


@router.get("/assessments")
def list_assessments(course_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    course = _course_or_404(db, user, course_id)
    items = []
    for a in db.scalars(select(Assessment).where(Assessment.course_id == course.id).order_by(Assessment.due_at)):
        item = {"id": a.id, "title": a.title, "type": a.type, "max_score": a.max_score, "due_at": a.due_at,
                "max_attempts": a.max_attempts, "weight": a.weight}
        if user.role == STUDENT:
            item["my_attempts"] = [{"score": t.score, "attempt_number": t.attempt_number, "is_late": t.is_late,
                                    "submitted_at": t.submitted_at}
                                   for t in db.scalars(select(AssessmentAttempt).where(
                                       AssessmentAttempt.assessment_id == a.id,
                                       AssessmentAttempt.student_id == user.student_id))]
        items.append(item)
    return items


@router.post("/assessments", status_code=201)
def create_assessment(body: AssessmentIn, user: Principal = Depends(require(ADMIN, SUPERADMIN, COORDINATOR, INSTRUCTOR)),
                      db: Session = Depends(get_db)):
    course = _course_or_404(db, user, body.course_id)
    a = Assessment(organization_id=user.organization_id, **body.model_dump())
    a.course_id = course.id
    db.add(a)
    db.flush()
    audit(db, user, "crear_evaluacion", "assessment", a.id, course_id=course.id)
    db.commit()
    return {"id": a.id, "title": a.title}


@router.post("/assessments/{assessment_id}/grades", status_code=201)
def grade(assessment_id: str, body: GradeIn, user: Principal = Depends(require(ADMIN, SUPERADMIN, INSTRUCTOR)),
          db: Session = Depends(get_db)):
    """El docente registra la calificación oficial; la estimación de la IA se guarda aparte."""
    a = db.get(Assessment, assessment_id)
    if not a or a.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evaluación no encontrada")
    _course_or_404(db, user, a.course_id)
    attempt = academic.register_grade(db, a, body.student_id, body.score, user.user_id, body.submitted_at)
    audit(db, user, "registrar_nota", "assessment_attempt", attempt.id, score=attempt.score, student_id=body.student_id)
    db.commit()
    return {"id": attempt.id, "score": attempt.score, "attempt_number": attempt.attempt_number,
            "is_late": attempt.is_late}


@router.get("/skills")
def list_skills(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    return [{"id": s.id, "code": s.code, "name": s.name, "parent_skill_id": s.parent_skill_id}
            for s in db.scalars(select(Skill).where(Skill.organization_id == user.organization_id).order_by(Skill.code))]


@router.get("/learning-paths")
def list_paths(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    out = []
    for p in db.scalars(select(LearningPath).where(LearningPath.organization_id == user.organization_id)):
        steps = []
        for s in p.steps:
            c = db.get(Course, s.course_id)
            step = {"position": s.position, "course_id": c.id, "code": c.code, "title": c.title, "level": c.level}
            if user.student_id:
                enr = db.scalar(select(Enrollment).where(Enrollment.course_id == c.id,
                                                         Enrollment.student_id == user.student_id))
                step["my_status"] = enr.status if enr else "pendiente"
                step["my_progress"] = enr.progress if enr else 0
            steps.append(step)
        out.append({"id": p.id, "title": p.title, "objective": p.objective, "status": p.status, "version": p.version,
                    "steps": steps})
    return out


@router.post("/learning-paths", status_code=201)
def create_path(body: PathIn, user: Principal = Depends(require(*MANAGERS, COORDINATOR)), db: Session = Depends(get_db)):
    path = LearningPath(organization_id=user.organization_id, title=body.title, objective=body.objective)
    for i, cid in enumerate(body.course_ids, start=1):
        course = _course_or_404(db, user, cid)
        if course.status != "publicado":
            raise HTTPException(422, f"El curso {course.code} no está publicado")
        path.steps.append(LearningPathStep(position=i, course_id=cid))
    db.add(path)
    db.flush()
    audit(db, user, "crear_ruta", "learning_path", path.id, cursos=body.course_ids)
    db.commit()
    return {"id": path.id, "title": path.title}
