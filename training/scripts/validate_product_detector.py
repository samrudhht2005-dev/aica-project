"""
Validate / test the AICA product detector on held-out splits.

Reports mAP50, mAP50-95, precision, recall, and per-class metrics from Ultralytics.
Also writes a simple summary JSON under training/runs/aica_products/eval_summary.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ultralytics import YOLO

DEFAULT_WEIGHTS = ROOT / "vision" / "weights" / "aica_product_detector.pt"
DATA_YAML = ROOT / "dataset" / "aica_products" / "data.yaml"
OUT_JSON = ROOT / "training" / "runs" / "aica_products" / "eval_summary.json"


def evaluate(weights: Path, split: str = "val", conf: float = 0.25, imgsz: int = 640):
    if not weights.exists():
        print(f"Missing weights: {weights}")
        print("Train first: python training/scripts/train_product_detector.py")
        sys.exit(1)
    if not DATA_YAML.exists():
        print(f"Missing {DATA_YAML} — run prepare_dataset.py first.")
        sys.exit(1)

    model = YOLO(str(weights))
    metrics = model.val(data=str(DATA_YAML), split=split, conf=conf, imgsz=imgsz, plots=True)

    box = metrics.box
    summary = {
        "weights": str(weights),
        "split": split,
        "conf": conf,
        "imgsz": imgsz,
        "precision": float(box.mp) if hasattr(box, "mp") else None,
        "recall": float(box.mr) if hasattr(box, "mr") else None,
        "mAP50": float(box.map50) if hasattr(box, "map50") else None,
        "mAP50-95": float(box.map) if hasattr(box, "map") else None,
        "per_class": {},
    }

    names = model.names or {}
    if hasattr(box, "ap_class_index") and hasattr(box, "ap50"):
        for i, cls_i in enumerate(list(box.ap_class_index)):
            name = names.get(int(cls_i), str(cls_i))
            entry = {"ap50": float(box.ap50[i]) if i < len(box.ap50) else None}
            if hasattr(box, "p") and i < len(box.p):
                entry["precision"] = float(box.p[i])
            if hasattr(box, "r") and i < len(box.r):
                entry["recall"] = float(box.r[i])
            if entry.get("precision") is not None and entry.get("recall") is not None:
                p, r = entry["precision"], entry["recall"]
                entry["f1"] = (2 * p * r / (p + r)) if (p + r) else 0.0
            summary["per_class"][name] = entry

    # Overall F1 from mean P/R when available
    if summary["precision"] is not None and summary["recall"] is not None:
        p, r = summary["precision"], summary["recall"]
        summary["f1"] = (2 * p * r / (p + r)) if (p + r) else 0.0

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(json.dumps(summary, indent=2))
    print(f"\nSaved {OUT_JSON}")
    print("Confusion matrix / plots (if generated) are under training/runs/aica_products/")
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--weights", type=str, default=str(DEFAULT_WEIGHTS))
    p.add_argument("--split", choices=["val", "test"], default="test")
    p.add_argument("--conf", type=float, default=0.25)
    p.add_argument("--imgsz", type=int, default=640)
    args = p.parse_args()
    evaluate(Path(args.weights), split=args.split, conf=args.conf, imgsz=args.imgsz)


if __name__ == "__main__":
    main()
