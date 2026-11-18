"""Inferencia con explicación resumida (gradiente × entrada sobre variables tabulares)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch

from .dataset import FEATURE_SCHEMA_VERSION, MASTERY_LEVELS, TABULAR_FEATURES
from .model import EduMasterMTL, ModelConfig

FEATURE_LABELS = {
    "active_days_14d": "días activos en las últimas 2 semanas",
    "sessions_14d": "sesiones en las últimas 2 semanas",
    "completion_ratio": "avance de actividades",
    "assessment_mean": "promedio de evaluaciones",
    "assessment_trend": "tendencia de notas",
    "late_submissions_30d": "entregas tardías recientes",
    "content_engagement": "horas de contenido en 30 días",
    "skill_evidence_score": "evidencia de competencias",
    "days_since_last_activity": "días desde la última actividad",
}


class Predictor:
    def __init__(self, version_dir: Path):
        self.version_dir = Path(version_dir)
        self.metadata = json.loads((self.version_dir / "metadata.json").read_text())
        self.cfg = ModelConfig.from_dict(self.metadata["model_config"])
        self.model = EduMasterMTL(self.cfg)
        self.model.load_state_dict(torch.load(self.version_dir / "model.pt", weights_only=True))
        self.model.eval()
        self.mean = np.asarray(self.metadata["scaler"]["mean"], dtype=np.float32)
        self.std = np.asarray(self.metadata["scaler"]["std"], dtype=np.float32)
        self.skills: list[str] = self.metadata["skills"]

    def predict(self, x_tab: np.ndarray, level: np.ndarray, seq: np.ndarray, explain: bool = True,
                with_activations: bool = False) -> list[dict]:
        xt = torch.from_numpy(((x_tab - self.mean) / self.std).astype(np.float32)).requires_grad_(explain)
        lv = torch.from_numpy(level.astype(np.int64))
        sq = torch.from_numpy(seq.astype(np.float32))
        out, acts = self.model(xt, lv, sq, return_activations=True)
        p_completion = torch.sigmoid(out["completion"])
        p_risk = torch.sigmoid(out["risk"])
        contrib_c = contrib_r = None
        if explain:
            grad_c = torch.autograd.grad(p_completion.sum(), xt, retain_graph=True)[0]
            grad_r = torch.autograd.grad(p_risk.sum(), xt, retain_graph=True)[0]
            contrib_c = (grad_c * xt).detach().numpy()
            contrib_r = (grad_r * xt).detach().numpy()
        p_completion, p_risk = p_completion.detach(), p_risk.detach()
        mastery = torch.softmax(out["mastery"], dim=1).detach().numpy()
        ranking = out["ranking"].detach().numpy()
        results = []
        for i in range(len(x_tab)):
            item = {
                "completion_probability": round(float(p_completion[i]), 4),
                "risk_probability": round(float(p_risk[i]), 4),
                "mastery_level": MASTERY_LEVELS[int(mastery[i].argmax())],
                "mastery_distribution": {lvl: round(float(p), 4) for lvl, p in zip(MASTERY_LEVELS, mastery[i])},
                "skill_scores": {sid: round(float(s), 4) for sid, s in zip(self.skills, ranking[i])},
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
            }
            if explain:
                item["explanation"] = {"finalizacion": _top_factors(contrib_c[i]), "riesgo": _top_factors(contrib_r[i])}
            if with_activations:
                item["activations"] = {k: [round(float(v), 4) for v in a[i].detach().numpy()] for k, a in acts.items()}
            results.append(item)
        return results


def _top_factors(contrib: np.ndarray, k: int = 3) -> list[dict]:
    order = np.argsort(-np.abs(contrib))[:k]
    return [{"variable": TABULAR_FEATURES[j], "descripcion": FEATURE_LABELS[TABULAR_FEATURES[j]],
             "efecto": "aumenta" if contrib[j] > 0 else "disminuye", "peso": round(float(contrib[j]), 4)}
            for j in order]
