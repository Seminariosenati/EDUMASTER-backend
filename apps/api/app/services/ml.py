"""Ciclo de vida del modelo: dataset desde la base, entrenamiento asíncrono, registro, promoción e inferencia."""

from __future__ import annotations

import json
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import torch
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.events import bus, emit, envelope
from app.core.resources import training_state
from app.db.models import (Assessment, AssessmentAttempt, Course, CourseSkill, DatasetVersion, Enrollment,
                           LearningEvent, ModelRun, ModelVersion, Organization, Skill, SkillEvidence, Student)
from app.db.session import SessionLocal
from edumaster_ml.dataset import build_dataset, build_features, datasheet
from edumaster_ml.inference import Predictor
from edumaster_ml.train import TrainConfig, train_model

log = logging.getLogger("edumaster.ml")
torch.set_num_threads(2)
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="trainer")
_predictors: dict[str, Predictor] = {}
_predictor_lock = threading.Lock()

ALLOWED_PARAMS = {"epochs", "batch_size", "lr", "patience", "tab_hidden", "gru_hidden", "fusion_hidden", "dropout"}


def load_records(db: Session, organization_id: str, student_ids: list[str] | None = None) -> SimpleNamespace:
    def rows(model, *filters):
        stmt = select(model).where(model.organization_id == organization_id, *filters) \
            if hasattr(model, "organization_id") else select(model).where(*filters)
        return list(db.scalars(stmt))

    sfilter = (lambda col: [col.in_(student_ids)]) if student_ids else (lambda col: [])
    courses = rows(Course)
    course_ids = [c.id for c in courses]
    start = db.scalar(select(func.min(Enrollment.enrolled_at)).where(Enrollment.organization_id == organization_id))
    return SimpleNamespace(
        start=start,
        courses=[{"id": c.id, "level": c.level, "n_activities": c.n_activities} for c in courses],
        skills=[{"id": s.id} for s in db.scalars(select(Skill).where(Skill.organization_id == organization_id)
                                                 .order_by(Skill.code))],
        course_skills=[{"course_id": cs.course_id, "skill_id": cs.skill_id}
                       for cs in db.scalars(select(CourseSkill).where(CourseSkill.course_id.in_(course_ids)))],
        assessments=[{"id": a.id, "course_id": a.course_id} for a in rows(Assessment)],
        enrollments=[{"id": e.id, "student_id": e.student_id, "course_id": e.course_id, "enrolled_at": e.enrolled_at,
                      "completed_at": e.completed_at} for e in rows(Enrollment, *sfilter(Enrollment.student_id))],
        events=[{"event_id": e.event_id, "enrollment_id": e.enrollment_id, "event_type": e.event_type,
                 "occurred_at": e.occurred_at, "payload": e.payload or {}}
                for e in rows(LearningEvent, *sfilter(LearningEvent.student_id))],
        attempts=[{"assessment_id": a.assessment_id, "student_id": a.student_id, "score": a.score,
                   "submitted_at": a.submitted_at, "is_late": a.is_late}
                  for a in rows(AssessmentAttempt, *sfilter(AssessmentAttempt.student_id))],
        skill_evidence=[{"student_id": ev.student_id, "skill_id": ev.skill_id, "score": ev.score,
                         "observed_at": ev.observed_at} for ev in rows(SkillEvidence, *sfilter(SkillEvidence.student_id))],
    )


def _publish(org_id: str, event_type: str, run_id: str, **payload) -> None:
    bus.publish_threadsafe(envelope(event_type, org_id, {"run_id": run_id, **payload}, aggregate_id=run_id))


def start_training(db: Session, organization_id: str, user_id: str | None, params: dict,
                   auto_promote: bool = False) -> ModelRun:
    if training_state["active_run"]:
        raise RuntimeError("Ya hay un entrenamiento en curso")
    clean = {k: v for k, v in params.items() if k in ALLOWED_PARAMS and v is not None}
    run = ModelRun(organization_id=organization_id, params=clean, requested_by=user_id, status="en_cola")
    db.add(run)
    emit(db, organization_id, "model.training.queued", {"run_id": run.id, "params": clean}, aggregate_id=run.id)
    db.commit()
    with training_state["lock"]:
        training_state["active_run"] = run.id
    _executor.submit(_run_training, run.id, auto_promote)
    return run


def _set_run(run_id: str, **fields) -> None:
    with SessionLocal() as db:
        run = db.get(ModelRun, run_id)
        for k, v in fields.items():
            setattr(run, k, v)
        db.commit()


def _run_training(run_id: str, auto_promote: bool) -> None:
    settings = get_settings()
    with SessionLocal() as db:
        run = db.get(ModelRun, run_id)
        org_id, params = run.organization_id, dict(run.params)
        is_demo = db.get(Organization, org_id).is_demo
    history: list[dict] = []
    try:
        _set_run(run_id, status="cargando_datos", started_at=datetime.now(timezone.utc))
        _publish(org_id, "model.training.started", run_id, status="cargando_datos")
        with SessionLocal() as db:
            ds = build_dataset(load_records(db, org_id))
            version = (db.scalar(select(func.max(DatasetVersion.version))
                                 .where(DatasetVersion.organization_id == org_id)) or 0) + 1
            ds_dir = settings.model_dir / "datasets"
            ds_dir.mkdir(parents=True, exist_ok=True)
            source = "demo" if is_demo else "institucional"
            sheet = datasheet(ds, source)
            uri = ds_dir / f"{org_id}-v{version}.json"
            uri.write_text(json.dumps(sheet, ensure_ascii=False, indent=2, default=str))
            dsv = DatasetVersion(organization_id=org_id, name="edumaster-learning-snapshots", version=version,
                                 source=source, rows=len(ds), checksum=sheet["checksum_sha256"],
                                 schema_version=ds.meta["feature_schema_version"], storage_uri=str(uri),
                                 quality=ds.quality)
            db.add(dsv)
            db.commit()
            dataset_id = dsv.id
        _set_run(run_id, status="entrenando", dataset_id=dataset_id)

        def on_epoch(entry: dict) -> None:
            history.append(entry)
            _publish(org_id, "model.training.progress", run_id, status="entrenando", **entry)
            if entry["epoch"] % 5 == 0:
                _set_run(run_id, history=list(history))

        def on_status(status: str, detail: dict) -> None:
            _set_run(run_id, status=status)
            _publish(org_id, "model.training.progress", run_id, status=status, detail=detail)

        cfg_kwargs = dict(params)
        if "tab_hidden" in cfg_kwargs:
            cfg_kwargs["tab_hidden"] = tuple(int(x) for x in cfg_kwargs["tab_hidden"])
        out_dir = settings.model_dir / org_id / run_id
        result = train_model(ds, out_dir, TrainConfig(**cfg_kwargs), source=source,
                             on_epoch=on_epoch, on_status=on_status)

        with SessionLocal() as db:
            run = db.get(ModelRun, run_id)
            run.status, run.finished_at = "completado", datetime.now(timezone.utc)
            run.metrics = {"test": result.metrics, "baseline": result.baseline}
            run.history = result.history
            number = (db.scalar(select(func.max(ModelVersion.version))
                                .where(ModelVersion.organization_id == org_id)) or 0) + 1
            mv = ModelVersion(organization_id=org_id, version=number, run_id=run_id, artifact_uri=str(out_dir),
                              metrics=run.metrics, architecture=result.architecture, model_card=result.model_card)
            db.add(mv)
            db.flush()
            emit(db, org_id, "model.training.completed",
                 {"run_id": run_id, "model_version_id": mv.id, "version": number,
                  "supera_baseline": result.model_card["supera_baseline"]}, aggregate_id=run_id)
            db.commit()
            has_production = db.scalar(select(ModelVersion.id).where(
                ModelVersion.organization_id == org_id, ModelVersion.stage == "produccion"))
            if auto_promote and not has_production:
                promote(db, mv, None, note="promoción inicial automática del entorno DEMO")
                from app.services import recommender  # import diferido: recommender depende de este módulo
                recommender.generate(db, org_id, list(db.scalars(select(Student.id).where(
                    Student.organization_id == org_id))))
    except Exception as exc:  # noqa: BLE001
        log.exception("Entrenamiento fallido")
        _set_run(run_id, status="error", error=str(exc)[:2000], finished_at=datetime.now(timezone.utc),
                 history=history)
        _publish(org_id, "model.training.failed", run_id, status="error", error=str(exc)[:300],
                 trace=traceback.format_exc(limit=2)[-500:])
    finally:
        with training_state["lock"]:
            training_state["active_run"] = None


def promote(db: Session, mv: ModelVersion, user_id: str | None, note: str = "") -> ModelVersion:
    for other in db.scalars(select(ModelVersion).where(ModelVersion.organization_id == mv.organization_id,
                                                       ModelVersion.stage == "produccion")):
        other.stage = "archivado"
    mv.stage, mv.approval_status, mv.approved_by = "produccion", "aprobado", user_id
    mv.deployed_at = datetime.now(timezone.utc)
    emit(db, mv.organization_id, "model.deployed", {"model_version_id": mv.id, "version": mv.version, "note": note},
         aggregate_id=mv.id)
    db.commit()
    return mv


def production_version(db: Session, organization_id: str) -> ModelVersion | None:
    return db.scalar(select(ModelVersion).where(ModelVersion.organization_id == organization_id,
                                                ModelVersion.stage == "produccion"))


def get_predictor(mv: ModelVersion) -> Predictor:
    with _predictor_lock:
        if mv.id not in _predictors:
            _predictors[mv.id] = Predictor(mv.artifact_uri)
        return _predictors[mv.id]


def current_features(db: Session, organization_id: str, student_ids: list[str]) -> list[dict]:
    """Variables actuales (corte = ahora) para las matrículas activas de los estudiantes indicados."""
    rec = load_records(db, organization_id, student_ids)
    now = datetime.now(timezone.utc)
    courses = {c["id"]: c for c in rec.courses}
    cskills: dict[str, set] = {}
    for cs in rec.course_skills:
        cskills.setdefault(cs["course_id"], set()).add(cs["skill_id"])
    acourse = {a["id"]: a["course_id"] for a in rec.assessments}
    events: dict[str, list] = {}
    for e in rec.events:
        events.setdefault(e["enrollment_id"], []).append(e)
    out = []
    for enr in rec.enrollments:
        if enr["completed_at"]:
            continue
        course = courses[enr["course_id"]]
        attempts = [a for a in rec.attempts if a["student_id"] == enr["student_id"]
                    and acourse.get(a["assessment_id"]) == course["id"]]
        evidence = [ev for ev in rec.skill_evidence if ev["student_id"] == enr["student_id"]]
        feats = build_features(events.get(enr["id"], []), attempts, evidence, course,
                               cskills.get(course["id"], set()), len(rec.skills), now)
        if feats is None:
            continue
        tab, level, seq = feats
        out.append({"student_id": enr["student_id"], "course_id": course["id"], "x_tab": tab, "level": level,
                    "seq": seq, "course_skills": cskills.get(course["id"], set())})
    return out


def predict_rows(predictor: Predictor, rows: list[dict], with_activations: bool = False) -> list[dict]:
    if not rows:
        return []
    preds = predictor.predict(np.stack([r["x_tab"] for r in rows]), np.asarray([r["level"] for r in rows]),
                              np.stack([r["seq"] for r in rows]), with_activations=with_activations)
    return [{**{k: r[k] for k in ("student_id", "course_id", "course_skills")}, **p} for r, p in zip(rows, preds)]
