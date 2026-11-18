"""Entrenamiento por línea de comandos con el dataset DEMO: python -m edumaster_ml.cli --out artifacts/demo"""

import argparse
from pathlib import Path

from .dataset import build_dataset, to_json
from .synthetic import generate
from .train import TrainConfig, train_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Entrena EduMaster MTL con datos DEMO")
    parser.add_argument("--out", type=Path, default=Path("artifacts/demo"))
    parser.add_argument("--students", type=int, default=240)
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--tab-hidden", type=int, nargs="+", default=[64, 32])
    parser.add_argument("--gru-hidden", type=int, default=32)
    parser.add_argument("--fusion-hidden", type=int, default=64)
    args = parser.parse_args()

    ds = build_dataset(generate(n_students=args.students))
    print(to_json(ds.quality))
    cfg = TrainConfig(epochs=args.epochs, tab_hidden=tuple(args.tab_hidden), gru_hidden=args.gru_hidden,
                      fusion_hidden=args.fusion_hidden)
    result = train_model(ds, args.out, cfg,
                         on_epoch=lambda e: print(f"época {e['epoch']:>3}  train={e['train_loss']:.4f}  val={e['val_loss']:.4f}"))
    print(to_json({"metricas": result.metrics, "baseline": result.baseline, "model_card": result.model_card}))


if __name__ == "__main__":
    main()
