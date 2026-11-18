from datetime import timedelta

import numpy as np
import torch

from edumaster_ml.dataset import TABULAR_FEATURES, build_dataset, build_features
from edumaster_ml.model import EduMasterMTL, ModelConfig, describe_architecture
from edumaster_ml.synthetic import generate
from edumaster_ml.train import TrainConfig, train_model


def test_dataset_quality_and_temporal_split():
    ds = build_dataset(generate(n_students=60, seed=1))
    assert ds.x_tab.shape[1] == len(TABULAR_FEATURES)
    assert set(np.unique(ds.split)) == {"train", "val", "test"}
    assert ds.quality["aprobado"] and ds.quality["duplicados_eliminados"] == 0
    assert len(ds.checksum()) == 64


def test_features_ignore_future_events():
    data = generate(n_students=20, seed=2)
    enr = data.enrollments[0]
    events = [e for e in data.events if e["enrollment_id"] == enr["id"]]
    course = next(c for c in data.courses if c["id"] == enr["course_id"])
    cutoff = data.start + timedelta(weeks=6)
    base = build_features(events, [], [], course, set(), 5, cutoff)
    future = {**events[0], "event_id": "x", "occurred_at": cutoff + timedelta(days=1), "event_type": "activity_completed"}
    with_future = build_features(events + [future], [], [], course, set(), 5, cutoff)
    if base is not None:
        assert np.allclose(base[0], with_future[0])


def test_model_shapes_and_architecture():
    cfg = ModelConfig(n_tabular=9, n_levels=3, seq_features=4, seq_len=8, n_skills=5, tab_hidden=(64, 32))
    model = EduMasterMTL(cfg)
    out = model(torch.randn(7, 9), torch.zeros(7, dtype=torch.long), torch.randn(7, 8, 4))
    assert out["completion"].shape == (7,) and out["mastery"].shape == (7, 5) and out["ranking"].shape == (7, 5)
    arch = describe_architecture(model)
    assert arch["total_params"] == sum(p.numel() for p in model.parameters())
    dense = {l["name"]: l for l in arch["layers"]}
    assert dense["densa_1"]["params"] == (9 + 4) * 64 + 64


def test_training_is_reproducible(tmp_path):
    ds = build_dataset(generate(n_students=60, seed=3))
    cfg = TrainConfig(epochs=3, tab_hidden=(16,), gru_hidden=8, fusion_hidden=16)
    a = train_model(ds, tmp_path / "a", cfg)
    b = train_model(ds, tmp_path / "b", cfg)
    assert a.history[-1]["val_loss"] == b.history[-1]["val_loss"]
    assert (tmp_path / "a" / "model.pt").exists() and "comparacion_baseline" in a.model_card
