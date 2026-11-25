import uuid
from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, TypeDecorator,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Guarda en UTC y devuelve siempre datetimes con zona horaria (SQLite los pierde)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


def pk() -> Mapped[str]:
    return mapped_column(String(36), primary_key=True, default=uid)


def org_fk() -> Mapped[str]:
    return mapped_column(ForeignKey("organizations.id"), index=True)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = pk()
    name: Mapped[str] = mapped_column(String(200))
    code: Mapped[str] = mapped_column(String(50), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="activo")
    timezone: Mapped[str] = mapped_column(String(50), default="America/Lima")
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    email: Mapped[str] = mapped_column(String(200), unique=True)
    full_name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(30), index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="activo")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Student(Base):
    __tablename__ = "students"
    __table_args__ = (UniqueConstraint("organization_id", "external_code"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), unique=True, nullable=True)
    external_code: Mapped[str] = mapped_column(String(50))
    full_name: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(80))
    cohort: Mapped[str] = mapped_column(String(30))
    enrollment_status: Mapped[str] = mapped_column(String(20), default="activo")


class Course(Base):
    __tablename__ = "courses"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    code: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="borrador")
    instructor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    n_activities: Mapped[int] = mapped_column(Integer, default=12)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    skills: Mapped[list["CourseSkill"]] = relationship(cascade="all, delete-orphan")


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("organization_id", "code"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    parent_skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"), nullable=True)


class CourseSkill(Base):
    __tablename__ = "course_skills"
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), primary_key=True)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"), primary_key=True)
    weight: Mapped[float] = mapped_column(Float, default=1.0)


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("student_id", "course_id"), Index("ix_enr_course", "course_id"))
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    status: Mapped[str] = mapped_column(String(20), default="activo")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    enrolled_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class LearningPath(Base):
    __tablename__ = "learning_paths"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    title: Mapped[str] = mapped_column(String(200))
    objective: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="borrador")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    steps: Mapped[list["LearningPathStep"]] = relationship(cascade="all, delete-orphan",
                                                           order_by="LearningPathStep.position")


class LearningPathStep(Base):
    __tablename__ = "learning_path_steps"
    id: Mapped[str] = pk()
    path_id: Mapped[str] = mapped_column(ForeignKey("learning_paths.id"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    required: Mapped[bool] = mapped_column(Boolean, default=True)


class Assessment(Base):
    __tablename__ = "assessments"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    type: Mapped[str] = mapped_column(String(30))
    max_score: Mapped[float] = mapped_column(Float, default=100)
    weight: Mapped[float] = mapped_column(Float, default=0.25)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    due_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(String(20), default="publicado")


class AssessmentAttempt(Base):
    __tablename__ = "assessment_attempts"
    __table_args__ = (UniqueConstraint("assessment_id", "student_id", "attempt_number"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    assessment_id: Mapped[str] = mapped_column(ForeignKey("assessments.id"), index=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[float] = mapped_column(Float)
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LearningEvent(Base):
    __tablename__ = "learning_events"
    __table_args__ = (Index("ix_ev_org_time", "organization_id", "occurred_at"),
                      Index("ix_ev_student_course", "student_id", "course_id"))
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"))
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"))
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    enrollment_id: Mapped[str] = mapped_column(ForeignKey("enrollments.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(50), index=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(String(20), default="web")
    schema_version: Mapped[str] = mapped_column(String(10), default="1")


class SkillEvidence(Base):
    __tablename__ = "skill_evidence"
    __table_args__ = (Index("ix_evid_student_skill", "student_id", "skill_id"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"))
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"))
    source_type: Mapped[str] = mapped_column(String(30))
    score: Mapped[float] = mapped_column(Float)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    validated_level: Mapped[str | None] = mapped_column(String(20), nullable=True)
    validated_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class DatasetVersion(Base):
    __tablename__ = "datasets"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(20))
    rows: Mapped[int] = mapped_column(Integer)
    checksum: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[str] = mapped_column(String(10))
    storage_uri: Mapped[str] = mapped_column(String(300))
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ModelRun(Base):
    __tablename__ = "model_runs"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    dataset_id: Mapped[str | None] = mapped_column(ForeignKey("datasets.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="en_cola")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    history: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ModelVersion(Base):
    __tablename__ = "model_versions"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    name: Mapped[str] = mapped_column(String(120), default="edumaster-mtl")
    version: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str] = mapped_column(ForeignKey("model_runs.id"))
    artifact_uri: Mapped[str] = mapped_column(String(300))
    stage: Mapped[str] = mapped_column(String(20), default="experimental")
    approval_status: Mapped[str] = mapped_column(String(20), default="pendiente")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    architecture: Mapped[dict] = mapped_column(JSON, default=dict)
    model_card: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    deployed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Prediction(Base):
    __tablename__ = "predictions"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    model_version_id: Mapped[str] = mapped_column(ForeignKey("model_versions.id"))
    completion_probability: Mapped[float] = mapped_column(Float)
    risk_probability: Mapped[float] = mapped_column(Float)
    mastery_level: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    course_id: Mapped[str | None] = mapped_column(ForeignKey("courses.id"), nullable=True)
    skill_id: Mapped[str | None] = mapped_column(ForeignKey("skills.id"), nullable=True)
    model_version_id: Mapped[str | None] = mapped_column(ForeignKey("model_versions.id"), nullable=True)
    type: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    rationale: Mapped[str] = mapped_column(Text)
    impact: Mapped[str] = mapped_column(String(20), default="medio")
    score: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="nueva")
    useful: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Certificate(Base):
    __tablename__ = "certificates"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), index=True)
    course_id: Mapped[str] = mapped_column(ForeignKey("courses.id"))
    folio: Mapped[str] = mapped_column(String(40), unique=True)
    verification_code: Mapped[str] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="emitido")
    final_score: Mapped[float] = mapped_column(Float)
    issued_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_org_time", "organization_id", "occurred_at"),)
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60))
    resource: Mapped[str] = mapped_column(String(60))
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    event_type: Mapped[str] = mapped_column(String(60))
    aggregate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    assessment_id: Mapped[str] = mapped_column(ForeignKey("assessments.id"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=1)
    type: Mapped[str] = mapped_column(String(30))
    prompt: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    answer: Mapped[dict] = mapped_column(JSON, default=dict)
    points: Mapped[float] = mapped_column(Float, default=1)
    explanation: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AttemptSubmission(Base):
    """Respuestas del estudiante a una evaluación autocorregida; la nota oficial queda en assessment_attempts."""

    __tablename__ = "attempt_submissions"
    id: Mapped[str] = pk()
    organization_id: Mapped[str] = org_fk()
    attempt_id: Mapped[str] = mapped_column(ForeignKey("assessment_attempts.id"), unique=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    detail: Mapped[list] = mapped_column(JSON, default=list)
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
