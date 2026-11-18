from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.resources import snapshot, training_state
from app.core.security import (ADMIN, INSTRUCTOR, ML_ENGINEER, STAFF, STUDENT, SUPERADMIN, Principal, current_user,
                               require, visible_course_ids)
from app.db.models import DatasetVersion, ModelRun, ModelVersion, Skill
from app.db.session import get_db
from app.services import ml
from edumaster_ml.model import EduMasterMTL, ModelConfig, describe_architecture

router = APIRouter(prefix="/ml", tags=["ia"])
ML_MANAGERS = (ML_ENGINEER, ADMIN, SUPERADMIN)


class TrainIn(BaseModel):
    epochs: int = Field(default=40, ge=1, le=300)
    batch_size: int = Field(default=64, ge=8, le=1024)
    lr: float = Field(default=1e-3, gt=0, le=0.1)
    patience: int = Field(default=6, ge=1, le=50)
    tab_hidden: list[int] = Field(default=[64, 32], min_length=1, max_length=5)
    gru_hidden: int = Field(default=32, ge=4, le=512)
    fusion_hidden: int = Field(default=64, ge=4, le=1024)
    dropout: float = Field(default=0.2, ge=0, le=0.8)


class PreviewIn(BaseModel):
    tab_hidden: list[int] = Field(default=[64, 32], min_length=1, max_length=5)
    gru_hidden: int = Field(default=32, ge=4, le=512)
    fusion_hidden: int = Field(default=64, ge=4, le=1024)
    batch_size: int = Field(default=64, ge=1, le=4096)


class PromoteIn(BaseModel):
    justification: str = Field(min_length=5, max_length=500)
    force: bool = False


class PredictIn(BaseModel):
    student_id: str
    course_id: str | None = None


def _version_out(mv: ModelVersion, full: bool = False) -> dict:
    out = {"id": mv.id, "name": mv.name, "version": mv.version, "stage": mv.stage,
           "approval_status": mv.approval_status, "created_at": mv.created_at, "deployed_at": mv.deployed_at,
           "run_id": mv.run_id, "metrics": mv.metrics, "supera_baseline": mv.model_card.get("supera_baseline"),
           "total_params": mv.architecture.get("total_params"), "config": mv.architecture.get("config")}
    if full:
        out["architecture"] = mv.architecture
        out["model_card"] = mv.model_card
    return out


def _run_out(r: ModelRun) -> dict:
    return {"id": r.id, "status": r.status, "params": r.params, "metrics": r.metrics, "history": r.history,
            "error": r.error, "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at,
            "dataset_id": r.dataset_id}


def _get_version(db: Session, user: Principal, version_id: str) -> ModelVersion:
    if version_id == "active":
        mv = ml.production_version(db, user.organization_id)
    else:
        mv = db.get(ModelVersion, version_id)
    if not mv or mv.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Modelo no encontrado")
    return mv


@router.get("/models")
def list_models(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(ModelVersion).where(ModelVersion.organization_id == user.organization_id)
                      .order_by(ModelVersion.version.desc()))
    return [_version_out(mv) for mv in rows]


@router.get("/models/{version_id}")
def get_model(version_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    return _version_out(_get_version(db, user, version_id), full=True)


@router.get("/models/{version_id}/architecture")
def architecture(version_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    mv = _get_version(db, user, version_id)
    return {**mv.architecture, "model_version_id": mv.id, "version": mv.version, "stage": mv.stage,
            "training_active": training_state["active_run"] is not None, "active_run_id": training_state["active_run"]}


@router.post("/architecture/preview")
def preview_architecture(body: PreviewIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    """Calcula parámetros y memoria de una configuración sin entrenarla, para comparar dónde añadir neuronas."""
    n_skills = len(list(db.scalars(select(Skill.id).where(Skill.organization_id == user.organization_id)))) or 5
    cfg = ModelConfig(n_tabular=9, n_levels=3, seq_features=4, seq_len=8, n_skills=n_skills,
                      tab_hidden=tuple(body.tab_hidden), gru_hidden=body.gru_hidden, fusion_hidden=body.fusion_hidden)
    return describe_architecture(EduMasterMTL(cfg), body.batch_size)


@router.post("/models/{version_id}/promote")
def promote(version_id: str, body: PromoteIn, user: Principal = Depends(require(*ML_MANAGERS)),
            db: Session = Depends(get_db)):
    mv = _get_version(db, user, version_id)
    if not mv.model_card.get("supera_baseline") and not body.force:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "El modelo no supera al baseline; use force=true con una justificación aprobada")
    ml.promote(db, mv, user.user_id, body.justification)
    audit(db, user, "promover_modelo", "model_version", mv.id, version=mv.version, justificacion=body.justification,
          forzado=body.force)
    db.commit()
    return _version_out(mv)


@router.get("/training-runs")
def list_runs(limit: int = Query(20, le=100), user: Principal = Depends(require(*ML_MANAGERS, *STAFF)),
              db: Session = Depends(get_db)):
    rows = db.scalars(select(ModelRun).where(ModelRun.organization_id == user.organization_id)
                      .order_by(ModelRun.created_at.desc()).limit(limit))
    return [_run_out(r) for r in rows]


@router.post("/training-runs", status_code=202)
def create_run(body: TrainIn, user: Principal = Depends(require(*ML_MANAGERS)), db: Session = Depends(get_db)):
    try:
        run = ml.start_training(db, user.organization_id, user.user_id, body.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
    audit(db, user, "solicitar_entrenamiento", "model_run", run.id, params=body.model_dump())
    db.commit()
    return _run_out(run)


@router.get("/training-runs/{run_id}")
def get_run(run_id: str, user: Principal = Depends(require(*ML_MANAGERS, *STAFF)), db: Session = Depends(get_db)):
    run = db.get(ModelRun, run_id)
    if not run or run.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Entrenamiento no encontrado")
    return _run_out(run)


@router.get("/datasets")
def datasets(user: Principal = Depends(require(*ML_MANAGERS, *STAFF)), db: Session = Depends(get_db)):
    rows = db.scalars(select(DatasetVersion).where(DatasetVersion.organization_id == user.organization_id)
                      .order_by(DatasetVersion.version.desc()))
    return [{"id": d.id, "name": d.name, "version": d.version, "source": d.source, "rows": d.rows,
             "checksum": d.checksum, "schema_version": d.schema_version, "quality": d.quality,
             "created_at": d.created_at} for d in rows]


@router.post("/predictions")
def predict(body: PredictIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == STUDENT and body.student_id != user.student_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo puede consultar sus propias estimaciones")
    if user.role not in (STUDENT, INSTRUCTOR, *STAFF, *ML_MANAGERS):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Rol sin permiso")
    mv = ml.production_version(db, user.organization_id)
    if not mv:
        raise HTTPException(status.HTTP_409_CONFLICT, "No hay un modelo publicado todavía")
    rows = ml.current_features(db, user.organization_id, [body.student_id])
    allowed = visible_course_ids(db, user)
    rows = [r for r in rows if (not body.course_id or r["course_id"] == body.course_id)
            and (allowed is None or r["course_id"] in allowed)]
    preds = ml.predict_rows(ml.get_predictor(mv), rows, with_activations=True)
    for p in preds:
        p.pop("course_skills", None)
    return {"model_version_id": mv.id, "model_name": mv.name, "model_version": mv.version,
            "disclaimer": "Estimación del modelo; no es una calificación oficial ni un diagnóstico.",
            "predictions": preds}


@router.get("/system")
def system(user: Principal = Depends(current_user)):
    return snapshot()
