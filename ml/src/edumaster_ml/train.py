"""Entrenamiento reproducible, baseline interpretable, evaluación por tarea y artefactos versionados."""

from __future__ import annotations

import json
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import torch
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (average_precision_score, balanced_accuracy_score, brier_score_loss, f1_score,
                             precision_score, recall_score, roc_auc_score)
from torch import nn

from .dataset import MASTERY_LEVELS, Dataset, datasheet
from .model import EduMasterMTL, ModelConfig, describe_architecture


BASELINE_TOLERANCE = 0.02


@dataclass
class TrainConfig:
    epochs: int = 40
    batch_size: int = 64
    lr: float = 1e-3
    weight_decay: float = 1e-4
    patience: int = 6
    seed: int = 42
    tab_hidden: tuple[int, ...] = (64, 32)
    gru_hidden: int = 32
    fusion_hidden: int = 64
    dropout: float = 0.2
    loss_weights: dict = field(default_factory=lambda: {"completion": 1.0, "mastery": 1.0, "risk": 1.0, "ranking": 0.5})

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tab_hidden"] = list(self.tab_hidden)
        return d


@dataclass
class TrainResult:
    version_dir: Path
    metrics: dict
    baseline: dict
    history: list[dict]
    architecture: dict
    datasheet: dict
    model_card: dict


def set_seed(seed: int) -> None:
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def _tensors(part: dict, mean: np.ndarray, std: np.ndarray) -> dict[str, torch.Tensor]:
    return {
        "x_tab": torch.from_numpy((part["x_tab"] - mean) / std),
        "level": torch.from_numpy(part["level"]),
        "seq": torch.from_numpy(part["seq"]),
        "y_completion": torch.from_numpy(part["y_completion"]),
        "y_mastery": torch.from_numpy(part["y_mastery"]),
        "y_risk": torch.from_numpy(part["y_risk"]),
        "y_rank": torch.from_numpy(part["y_rank"]),
        "rank_mask": torch.from_numpy(part["rank_mask"]),
    }


def multitask_loss(out: dict, batch: dict, weights: dict) -> tuple[torch.Tensor, dict]:
    bce = nn.functional.binary_cross_entropy_with_logits
    l_completion = bce(out["completion"], batch["y_completion"])
    l_risk = bce(out["risk"], batch["y_risk"])
    has_mastery = batch["y_mastery"] >= 0
    l_mastery = (nn.functional.cross_entropy(out["mastery"][has_mastery], batch["y_mastery"][has_mastery])
                 if has_mastery.any() else out["mastery"].sum() * 0)
    masked = out["ranking"].masked_fill(~batch["rank_mask"], -1e9)
    l_rank = -(batch["y_rank"] * torch.log_softmax(masked, dim=1)).sum(dim=1).mean()
    parts = {"completion": l_completion, "mastery": l_mastery, "risk": l_risk, "ranking": l_rank}
    total = sum(weights[k] * v for k, v in parts.items())
    return total, {k: float(v.detach()) for k, v in parts.items()}


def ndcg_at_k(scores: np.ndarray, relevance: np.ndarray, mask: np.ndarray, k: int = 3) -> float:
    values = []
    for s, r, m in zip(scores, relevance, mask):
        if m.sum() < 2:
            continue
        s = np.where(m, s, -np.inf)
        order = np.argsort(-s)[:k]
        ideal = np.sort(r[m])[::-1][:k]
        discounts = 1 / np.log2(np.arange(2, k + 2))
        dcg = (r[order] * discounts[: len(order)]).sum()
        idcg = (ideal * discounts[: len(ideal)]).sum()
        values.append(dcg / idcg if idcg > 0 else 0)
    return float(np.mean(values)) if values else float("nan")


def _binary_metrics(y: np.ndarray, p: np.ndarray, threshold: float = 0.5) -> dict:
    pred = (p >= threshold).astype(int)
    two_classes = len(np.unique(y)) > 1
    return {
        "roc_auc": round(float(roc_auc_score(y, p)), 4) if two_classes else None,
        "pr_auc": round(float(average_precision_score(y, p)), 4) if two_classes else None,
        "f1": round(float(f1_score(y, pred, zero_division=0)), 4),
        "recall": round(float(recall_score(y, pred, zero_division=0)), 4),
        "precision": round(float(precision_score(y, pred, zero_division=0)), 4),
        "brier": round(float(brier_score_loss(y, p)), 4),
        "positivos": int(y.sum()),
        "n": int(len(y)),
    }


def _mastery_metrics(y: np.ndarray, pred: np.ndarray) -> dict:
    has = y >= 0
    if not has.any():
        return {"macro_f1": None, "balanced_accuracy": None, "mae_ordinal": None, "n": 0}
    y, pred = y[has], pred[has]
    return {
        "macro_f1": round(float(f1_score(y, pred, average="macro", zero_division=0)), 4),
        "balanced_accuracy": round(float(balanced_accuracy_score(y, pred)), 4),
        "mae_ordinal": round(float(np.abs(y - pred).mean()), 4),
        "n": int(len(y)),
    }


@torch.no_grad()
def evaluate(model: EduMasterMTL, t: dict) -> dict:
    model.eval()
    out = model(t["x_tab"], t["level"], t["seq"])
    p_completion = torch.sigmoid(out["completion"]).numpy()
    p_risk = torch.sigmoid(out["risk"]).numpy()
    mastery = out["mastery"].argmax(dim=1).numpy()
    return {
        "finalizacion": _binary_metrics(t["y_completion"].numpy(), p_completion),
        "dominio": _mastery_metrics(t["y_mastery"].numpy(), mastery),
        "riesgo": _binary_metrics(t["y_risk"].numpy(), p_risk),
        "recomendacion": {"ndcg@3": round(ndcg_at_k(out["ranking"].numpy(), t["y_rank"].numpy(),
                                                    t["rank_mask"].numpy()), 4)},
    }


def train_baseline(train: dict, test: dict, mean: np.ndarray, std: np.ndarray) -> dict:
    """Regresión logística sobre variables tabulares: referencia interpretable que la red debe superar."""
    def design(part):
        level = np.eye(3, dtype=np.float32)[part["level"]]
        return np.hstack([(part["x_tab"] - mean) / std, level])

    xtr, xte = design(train), design(test)
    result = {}
    for key, name in (("y_completion", "finalizacion"), ("y_risk", "riesgo")):
        if len(np.unique(train[key])) < 2:
            continue
        clf = LogisticRegression(max_iter=500).fit(xtr, train[key])
        result[name] = _binary_metrics(test[key], clf.predict_proba(xte)[:, 1])
    has = train["y_mastery"] >= 0
    if len(np.unique(train["y_mastery"][has])) > 1:
        clf = LogisticRegression(max_iter=1000).fit(xtr[has], train["y_mastery"][has])
        result["dominio"] = _mastery_metrics(test["y_mastery"], clf.predict(xte))
    return result


def train_model(ds: Dataset, out_dir: Path, cfg: TrainConfig | None = None, source: str = "demo",
                on_epoch: Callable[[dict], None] | None = None,
                on_status: Callable[[str, dict], None] | None = None) -> TrainResult:
    cfg = cfg or TrainConfig()
    set_seed(cfg.seed)
    status = on_status or (lambda *_: None)
    started = time.time()

    train, val, test = ds.subset("train"), ds.subset("val"), ds.subset("test")
    mean = train["x_tab"].mean(axis=0)
    std = train["x_tab"].std(axis=0) + 1e-6
    t_train, t_val, t_test = (_tensors(p, mean, std) for p in (train, val, test))

    model_cfg = ModelConfig(
        n_tabular=ds.x_tab.shape[1], n_levels=len(ds.meta["levels"]), seq_features=ds.seq.shape[2],
        seq_len=ds.seq.shape[1], n_skills=len(ds.skills), n_mastery=len(MASTERY_LEVELS),
        tab_hidden=tuple(cfg.tab_hidden), gru_hidden=cfg.gru_hidden, fusion_hidden=cfg.fusion_hidden,
        dropout=cfg.dropout,
    )
    model = EduMasterMTL(model_cfg)
    architecture = describe_architecture(model, cfg.batch_size)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(opt, factor=0.5, patience=2)
    generator = torch.Generator().manual_seed(cfg.seed)
    n = len(t_train["y_completion"])

    status("entrenando", {"filas_train": n, "filas_val": len(t_val["y_completion"]),
                          "filas_test": len(t_test["y_completion"]), "parametros": architecture["total_params"]})
    history, best_val, best_state, bad_epochs = [], float("inf"), None, 0
    for epoch in range(1, cfg.epochs + 1):
        model.train()
        perm = torch.randperm(n, generator=generator)
        epoch_loss, batches = 0.0, 0
        for i in range(0, n, cfg.batch_size):
            idx = perm[i:i + cfg.batch_size]
            batch = {k: v[idx] for k, v in t_train.items()}
            out = model(batch["x_tab"], batch["level"], batch["seq"])
            loss, _ = multitask_loss(out, batch, cfg.loss_weights)
            opt.zero_grad()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            epoch_loss += float(loss.detach())
            batches += 1
        model.eval()
        with torch.no_grad():
            val_out = model(t_val["x_tab"], t_val["level"], t_val["seq"])
            val_loss, val_parts = multitask_loss(val_out, t_val, cfg.loss_weights)
        val_loss = float(val_loss)
        scheduler.step(val_loss)
        entry = {"epoch": epoch, "train_loss": round(epoch_loss / max(batches, 1), 5), "val_loss": round(val_loss, 5),
                 "val_parts": {k: round(v, 5) for k, v in val_parts.items()}, "lr": opt.param_groups[0]["lr"]}
        history.append(entry)
        if on_epoch:
            on_epoch({**entry, "epochs": cfg.epochs})
        if val_loss < best_val - 1e-4:
            best_val, bad_epochs = val_loss, 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            bad_epochs += 1
            if bad_epochs >= cfg.patience:
                break

    if best_state:
        model.load_state_dict(best_state)
    status("validando", {"mejor_val_loss": round(best_val, 5), "epocas": len(history)})
    metrics = evaluate(model, t_test)
    baseline = train_baseline(train, test, mean, std)
    sheet = datasheet(ds, source)

    out_dir.mkdir(parents=True, exist_ok=True)
    torch.save(model.state_dict(), out_dir / "model.pt")
    card = model_card(metrics, baseline, sheet, cfg, time.time() - started)
    metadata = {
        "model_config": model_cfg.to_dict(),
        "train_config": cfg.to_dict(),
        "scaler": {"mean": mean.tolist(), "std": std.tolist()},
        "skills": ds.skills,
        "dataset_meta": ds.meta,
        "metrics": metrics,
        "baseline": baseline,
        "history": history,
        "architecture": architecture,
        "datasheet": sheet,
        "model_card": card,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2, default=str))
    return TrainResult(out_dir, metrics, baseline, history, architecture, sheet, card)


def model_card(metrics: dict, baseline: dict, sheet: dict, cfg: TrainConfig, seconds: float) -> dict:
    comparison = {}
    for task in ("finalizacion", "riesgo"):
        red, base = metrics[task].get("pr_auc"), baseline.get(task, {}).get("pr_auc")
        if red is not None and base is not None:
            comparison[task] = {"red_pr_auc": red, "baseline_pr_auc": base, "mejora": round(red - base, 4)}
    # Iguala = no empeora más de la tolerancia en ninguna tarea; supera = además mejora en al menos una.
    beats = bool(comparison) and all(c["mejora"] >= -BASELINE_TOLERANCE for c in comparison.values()) \
        and any(c["mejora"] > 0 for c in comparison.values())
    return {
        "modelo": "EduMaster Multi-Task Learning Network",
        "uso_previsto": "Apoyo a docentes y coordinadores: priorizar acompañamiento y recomendar refuerzos.",
        "usos_no_permitidos": [
            "Sancionar, excluir o calificar oficialmente a un estudiante",
            "Decisiones automáticas sin revisión humana",
        ],
        "datos": {"fuente": sheet["fuente"], "es_demo": sheet["es_demo"], "filas": sheet["filas"],
                  "checksum": sheet["checksum_sha256"]},
        "comparacion_baseline": comparison,
        "supera_baseline": beats,
        "recomendacion_publicacion": (
            "Apta para revisión: mejora o iguala al baseline." if beats
            else "No publicar: no supera al baseline interpretable."),
        "limitaciones": [
            "Entrenado con datos sintéticos DEMO." if sheet["es_demo"] else "Validar por institución y cohorte.",
            "Una probabilidad es una estimación, no una certeza ni un diagnóstico.",
        ],
        "duracion_segundos": round(seconds, 2),
        "semilla": cfg.seed,
    }
