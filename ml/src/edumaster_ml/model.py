"""EduMaster Multi-Task Learning Network: rama densa tabular + rama temporal GRU + fusión + 4 cabezas."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import torch
from torch import nn

BYTES_PER_FLOAT = 4


@dataclass
class ModelConfig:
    n_tabular: int
    n_levels: int
    seq_features: int
    seq_len: int
    n_skills: int
    n_mastery: int = 5
    level_embedding: int = 4
    tab_hidden: tuple[int, ...] = (64, 32)
    gru_hidden: int = 32
    fusion_hidden: int = 64
    dropout: float = 0.2

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tab_hidden"] = list(self.tab_hidden)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ModelConfig":
        d = dict(d)
        d["tab_hidden"] = tuple(d["tab_hidden"])
        return cls(**d)


class EduMasterMTL(nn.Module):
    def __init__(self, cfg: ModelConfig):
        super().__init__()
        self.cfg = cfg
        self.level_embedding = nn.Embedding(cfg.n_levels, cfg.level_embedding)
        layers, width = [], cfg.n_tabular + cfg.level_embedding
        for i, hidden in enumerate(cfg.tab_hidden):
            layers += [nn.Linear(width, hidden), nn.ReLU(), nn.Dropout(cfg.dropout)]
            width = hidden
        self.tabular = nn.Sequential(*layers)
        self.temporal = nn.GRU(cfg.seq_features, cfg.gru_hidden, batch_first=True)
        self.fusion = nn.Sequential(
            nn.Linear(width + cfg.gru_hidden, cfg.fusion_hidden),
            nn.LayerNorm(cfg.fusion_hidden),
            nn.ReLU(),
            nn.Dropout(cfg.dropout),
        )
        self.head_completion = nn.Linear(cfg.fusion_hidden, 1)
        self.head_mastery = nn.Linear(cfg.fusion_hidden, cfg.n_mastery)
        self.head_risk = nn.Linear(cfg.fusion_hidden, 1)
        self.head_ranking = nn.Linear(cfg.fusion_hidden, cfg.n_skills)

    def forward(self, x_tab: torch.Tensor, level: torch.Tensor, seq: torch.Tensor, return_activations: bool = False):
        acts = {}
        tab_in = torch.cat([x_tab, self.level_embedding(level)], dim=1)
        acts["entrada"] = tab_in
        h = tab_in
        dense_idx = 0
        for layer in self.tabular:
            h = layer(h)
            if isinstance(layer, nn.ReLU):
                dense_idx += 1
                acts[f"densa_{dense_idx}"] = h
        _, h_seq = self.temporal(seq)
        h_seq = h_seq[-1]
        acts["gru"] = h_seq
        fused = self.fusion(torch.cat([h, h_seq], dim=1))
        acts["fusion"] = fused
        out = {
            "completion": self.head_completion(fused).squeeze(-1),
            "mastery": self.head_mastery(fused),
            "risk": self.head_risk(fused).squeeze(-1),
            "ranking": self.head_ranking(fused),
        }
        if return_activations:
            return out, acts
        return out


def _params(module: nn.Module) -> int:
    return sum(p.numel() for p in module.parameters())


def describe_architecture(model: EduMasterMTL, batch_size: int = 64) -> dict:
    """Capas, neuronas, parámetros y memoria estimada (pesos + activaciones del lote) de cada bloque."""
    cfg = model.cfg
    layers = []

    def add(name, kind, group, n_in, n_out, module, extra_activation=0, note=""):
        params = _params(module) if module is not None else 0
        param_bytes = params * BYTES_PER_FLOAT
        # Adam guarda 2 tensores extra por parámetro y el gradiente otro más.
        training_bytes = param_bytes * 4
        activation_bytes = batch_size * (n_out + extra_activation) * BYTES_PER_FLOAT
        layers.append({
            "name": name, "type": kind, "group": group, "inputs": n_in, "neurons": n_out,
            "params": params, "param_bytes": param_bytes, "training_bytes": training_bytes,
            "activation_bytes": activation_bytes, "total_bytes": training_bytes + activation_bytes, "note": note,
        })

    add("entrada", "Input + Embedding", "entrada", cfg.n_tabular + 1, cfg.n_tabular + cfg.level_embedding,
        model.level_embedding, note="variables tabulares normalizadas + nivel del curso")
    width = cfg.n_tabular + cfg.level_embedding
    linears = [m for m in model.tabular if isinstance(m, nn.Linear)]
    for i, lin in enumerate(linears, start=1):
        add(f"densa_{i}", "Dense + ReLU + Dropout", "oculta", width, lin.out_features, lin)
        width = lin.out_features
    add("gru", "GRU", "temporal", cfg.seq_features, cfg.gru_hidden, model.temporal,
        extra_activation=cfg.seq_len * cfg.gru_hidden * 3,
        note=f"secuencia de {cfg.seq_len} semanas; guarda 3 compuertas por paso")
    add("fusion", "Dense + LayerNorm + ReLU", "fusion", width + cfg.gru_hidden, cfg.fusion_hidden, model.fusion)
    add("cabeza_finalizacion", "Sigmoid", "salida", cfg.fusion_hidden, 1, model.head_completion,
        note="probabilidad de completar el curso en 28 días")
    add("cabeza_dominio", "Softmax", "salida", cfg.fusion_hidden, cfg.n_mastery, model.head_mastery,
        note="nivel de dominio estimado (5 niveles)")
    add("cabeza_riesgo", "Sigmoid", "salida", cfg.fusion_hidden, 1, model.head_risk,
        note="señal de desconexión para apoyo humano")
    add("cabeza_ranking", "Ranking (ListNet)", "salida", cfg.fusion_hidden, cfg.n_skills, model.head_ranking,
        note="orden de competencias a reforzar")

    dense = [l for l in layers if l["group"] in ("oculta", "fusion")]
    heaviest = max(dense, key=lambda l: l["params"])
    total = sum(l["total_bytes"] for l in layers)
    for l in layers:
        l["memory_share"] = round(l["total_bytes"] / total, 4) if total else 0
    return {
        "name": "EduMaster Multi-Task Learning Network",
        "config": cfg.to_dict(),
        "batch_size": batch_size,
        "layers": layers,
        "total_params": _params(model),
        "total_bytes": total,
        "heaviest_dense_layer": heaviest["name"],
        "explanation": (
            f"La capa '{heaviest['name']}' es la densa con más parámetros ({heaviest['params']:,}). "
            "En una capa Dense los pesos crecen como entradas × neuronas, así que duplicar neuronas en la capa "
            "con más entradas multiplica la memoria; conviene ampliar primero donde la validación mejora."
        ),
    }
