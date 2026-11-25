from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import (ADMIN, COORDINATOR, INSTRUCTOR, STUDENT, SUPERADMIN, Principal, current_user,
                               require, visible_course_ids)
from app.db.models import Assessment, AssessmentAttempt, AttemptSubmission, Course, Enrollment, Question
from app.db.session import get_db
from app.services import academic
from app.services.question_bank import MC, SHORT, TF, grade

router = APIRouter(prefix="/evaluations", tags=["evaluaciones"])
EDITORS = (ADMIN, SUPERADMIN, COORDINATOR, INSTRUCTOR)


class QuestionIn(BaseModel):
    type: str = Field(pattern=f"^({MC}|{TF}|{SHORT})$")
    prompt: str = Field(min_length=5, max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=8)
    correct: int | None = None
    accepted: list[str] = Field(default_factory=list, max_length=10)
    explanation: str = Field(default="", max_length=1000)
    points: float = Field(default=1, gt=0, le=100)

    @model_validator(mode="after")
    def check(self):
        if self.type == MC:
            opts = [o.strip() for o in self.options if o.strip()]
            if len(opts) < 2:
                raise ValueError("La opción múltiple necesita al menos 2 opciones")
            if self.correct is None or not 0 <= self.correct < len(opts):
                raise ValueError("Indica cuál opción es la correcta")
            self.options = opts
        elif self.type == TF:
            if self.correct not in (0, 1):
                raise ValueError("Indica si la respuesta correcta es Verdadero (0) o Falso (1)")
            self.options = ["Verdadero", "Falso"]
        else:
            self.accepted = [a.strip() for a in self.accepted if a.strip()]
            if not self.accepted:
                raise ValueError("Agrega al menos una respuesta aceptada")
            self.options = []
        return self


class SubmitIn(BaseModel):
    answers: dict[str, str | int] = Field(default_factory=dict)


def _assessment_or_404(db: Session, user: Principal, assessment_id: str) -> tuple[Assessment, Course]:
    a = db.get(Assessment, assessment_id)
    if not a or a.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evaluación no encontrada")
    course = db.get(Course, a.course_id)
    if user.role == STUDENT:
        enrolled = db.scalar(select(Enrollment.id).where(Enrollment.student_id == user.student_id,
                                                         Enrollment.course_id == course.id))
        if not enrolled:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "No estás matriculado en este curso")
    else:
        allowed = visible_course_ids(db, user)
        if allowed is not None and course.id not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "El curso no está asignado a este docente")
    return a, course


def _questions(db: Session, assessment_id: str) -> list[Question]:
    return list(db.scalars(select(Question).where(Question.assessment_id == assessment_id).order_by(Question.position)))


def _q_out(q: Question, with_answer: bool) -> dict:
    out = {"id": q.id, "position": q.position, "type": q.type, "prompt": q.prompt, "options": q.options,
           "points": q.points}
    if with_answer:
        out["answer"] = q.answer
        out["explanation"] = q.explanation
    return out


@router.get("")
def list_evaluations(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Assessment, Course).join(Course, Course.id == Assessment.course_id).where(
        Assessment.organization_id == user.organization_id)
    if user.role == STUDENT:
        stmt = stmt.where(Course.id.in_(select(Enrollment.course_id).where(Enrollment.student_id == user.student_id)))
    else:
        allowed = visible_course_ids(db, user)
        if allowed is not None:
            stmt = stmt.where(Course.id.in_(allowed))
    rows = db.execute(stmt.order_by(Assessment.due_at)).all()
    ids = [a.id for a, _ in rows]
    qcount = dict(db.execute(select(Question.assessment_id, func.count(Question.id))
                             .where(Question.assessment_id.in_(ids)).group_by(Question.assessment_id)).all()) if ids else {}
    out = []
    for a, c in rows:
        item = {"id": a.id, "title": a.title, "type": a.type, "due_at": a.due_at, "max_attempts": a.max_attempts,
                "course": {"id": c.id, "code": c.code, "title": c.title}, "questions": qcount.get(a.id, 0)}
        if user.role == STUDENT:
            attempts = list(db.scalars(select(AssessmentAttempt).where(
                AssessmentAttempt.assessment_id == a.id, AssessmentAttempt.student_id == user.student_id)
                .order_by(AssessmentAttempt.attempt_number)))
            item["my_attempts"] = len(attempts)
            item["best_score"] = max((t.score for t in attempts), default=None)
        else:
            stats = db.execute(select(func.count(AssessmentAttempt.id), func.avg(AssessmentAttempt.score))
                               .where(AssessmentAttempt.assessment_id == a.id)).one()
            item["attempts"] = int(stats[0] or 0)
            item["avg_score"] = round(float(stats[1]), 1) if stats[1] is not None else None
        out.append(item)
    return out


@router.get("/{assessment_id}")
def get_evaluation(assessment_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    a, course = _assessment_or_404(db, user, assessment_id)
    is_student = user.role == STUDENT
    out = {"id": a.id, "title": a.title, "type": a.type, "due_at": a.due_at, "max_score": a.max_score,
           "max_attempts": a.max_attempts, "course": {"id": course.id, "code": course.code, "title": course.title},
           "questions": [_q_out(q, not is_student) for q in _questions(db, a.id)],
           "can_edit": user.role in EDITORS}
    if is_student:
        attempts = list(db.scalars(select(AssessmentAttempt).where(
            AssessmentAttempt.assessment_id == a.id, AssessmentAttempt.student_id == user.student_id)
            .order_by(AssessmentAttempt.attempt_number)))
        out["my_attempts"] = [{"attempt_number": t.attempt_number, "score": t.score, "is_late": t.is_late,
                               "submitted_at": t.submitted_at} for t in attempts]
        out["remaining_attempts"] = max(a.max_attempts - len(attempts), 0)
    return out


@router.post("/{assessment_id}/questions", status_code=201)
def add_question(assessment_id: str, body: QuestionIn, user: Principal = Depends(require(*EDITORS)),
                 db: Session = Depends(get_db)):
    a, _ = _assessment_or_404(db, user, assessment_id)
    position = (db.scalar(select(func.max(Question.position)).where(Question.assessment_id == a.id)) or 0) + 1
    answer = {"accepted": body.accepted} if body.type == SHORT else {"correct": body.correct}
    q = Question(organization_id=user.organization_id, assessment_id=a.id, position=position, type=body.type,
                 prompt=body.prompt.strip(), options=body.options, answer=answer, points=body.points,
                 explanation=body.explanation.strip(), created_by=user.user_id)
    db.add(q)
    db.flush()
    audit(db, user, "crear_pregunta", "question", q.id, assessment_id=a.id)
    db.commit()
    return _q_out(q, True)


@router.delete("/questions/{question_id}", status_code=204)
def delete_question(question_id: str, user: Principal = Depends(require(*EDITORS)), db: Session = Depends(get_db)):
    q = db.get(Question, question_id)
    if not q or q.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pregunta no encontrada")
    _assessment_or_404(db, user, q.assessment_id)
    audit(db, user, "eliminar_pregunta", "question", q.id, assessment_id=q.assessment_id)
    db.delete(q)
    db.commit()


@router.post("/{assessment_id}/submit", status_code=201)
def submit(assessment_id: str, body: SubmitIn, user: Principal = Depends(require(STUDENT)),
           db: Session = Depends(get_db)):
    """El estudiante responde; la corrección automática genera la nota oficial del intento."""
    a, _ = _assessment_or_404(db, user, assessment_id)
    questions = _questions(db, a.id)
    if not questions:
        raise HTTPException(422, "La evaluación aún no tiene preguntas")
    missing = [q.position for q in questions if str(body.answers.get(q.id, "")).strip() == ""]
    if missing:
        raise HTTPException(422, f"Faltan respuestas en las preguntas {', '.join(map(str, missing))}")
    pct, detail = grade(questions, body.answers)
    attempt = academic.register_grade(db, a, user.student_id, round(pct / 100 * a.max_score, 2), None)
    db.add(AttemptSubmission(organization_id=user.organization_id, attempt_id=attempt.id,
                             answers={k: v for k, v in body.answers.items()}, detail=detail))
    audit(db, user, "responder_evaluacion", "assessment_attempt", attempt.id, score=attempt.score)
    db.commit()
    used = db.scalar(select(func.count(AssessmentAttempt.id)).where(
        AssessmentAttempt.assessment_id == a.id, AssessmentAttempt.student_id == user.student_id)) or 0
    return {"attempt_id": attempt.id, "attempt_number": attempt.attempt_number, "score": attempt.score,
            "is_late": attempt.is_late, "correct": sum(d["correct"] for d in detail), "total": len(detail),
            "detail": detail, "remaining_attempts": max(a.max_attempts - used, 0)}
