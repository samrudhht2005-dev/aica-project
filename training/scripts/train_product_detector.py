"""
Train AICA curated product detector with transfer learning.

Default backbone: YOLO26n (Ultralytics). Falls back to YOLO11n, then YOLOv8n
if a weight file cannot be downloaded.

Does not overwrite yolov8n.pt or the legacy synthetic vision/best.pt.
Writes production weights to vision/weights/aica_product_detector.pt.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ultralytics import YOLO

from training.scripts.prepare_dataset import count_split, write_data_yaml  # noqa: E402

WEIGHTS_DIR = ROOT / "vision" / "weights"
RUNS_DIR = ROOT / "training" / "runs" / "aica_products"
DATA_YAML = ROOT / "dataset" / "aica_products" / "data.yaml"

CANDIDATE_PRETRAINS = ("yolo26n.pt", "yolo11n.pt", "yolov8n.pt")


def pick_pretrained(preferred: str | None = None) -> str:
    order = []
    if preferred:
        order.append(preferred)
    for name in CANDIDATE_PRETRAINS:
        if name not in order:
            order.append(name)
    last_err = None
    for name in order:
        try:
            print(f"Trying pretrained weights: {name}")
            YOLO(name)  # downloads if needed
            return name
        except Exception as e:
            last_err = e
            print(f"  failed: {e}")
    raise RuntimeError(f"Could not load any pretrained YOLO weights. Last error: {last_err}")


def train(
    epochs: int = 80,
    imgsz: int = 640,
    batch: int = 8,
    model_name: str | None = None,
    patience: int = 20,
    device: str = "",
):
    write_data_yaml(DATA_YAML)
    stats = count_split()
    if stats["train"]["images"] < 40:
        print(
            "ERROR: Not enough training images "
            f"(found {stats['train']['images']}). "
            "Collect ~100–200 real annotated images per class before training. "
            "See docs/CV_PRODUCT_RECOGNITION.md"
        )
        sys.exit(1)
    if stats["val"]["images"] < 8:
        print("ERROR: Need a non-empty validation split before training.")
        sys.exit(1)

    pretrained = pick_pretrained(model_name)
    model = YOLO(pretrained)
    results = model.train(
        data=str(DATA_YAML),
        epochs=epochs,
        imgsz=imgsz,
        batch=batch,
        workers=0,
        patience=patience,
        device=device or None,
        project=str(ROOT / "training" / "runs"),
        name="aica_products",
        exist_ok=True,
        pretrained=True,
        # Realistic POS augmentations (no vertical flip)
        degrees=12.0,
        translate=0.1,
        scale=0.5,
        shear=2.0,
        perspective=0.0005,
        fliplr=0.5,
        flipud=0.0,
        hsv_h=0.015,
        hsv_s=0.6,
        hsv_v=0.4,
        mosaic=0.8,
    )

    best = RUNS_DIR / "weights" / "best.pt"
    WEIGHTS_DIR.mkdir(parents=True, exist_ok=True)
    dest = WEIGHTS_DIR / "aica_product_detector.pt"
    if best.exists():
        shutil.copy2(best, dest)
        print(f"Deployed weights → {dest}")
    else:
        print("WARNING: best.pt not found after training.")
    print(results)
    return dest


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--epochs", type=int, default=80)
    p.add_argument("--imgsz", type=int, default=640)
    p.add_argument("--batch", type=int, default=8)
    p.add_argument("--model", type=str, default="yolo26n.pt", help="Pretrained start weights")
    p.add_argument("--patience", type=int, default=20)
    p.add_argument("--device", type=str, default="")
    args = p.parse_args()
    train(
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        model_name=args.model,
        patience=args.patience,
        device=args.device,
    )


if __name__ == "__main__":
    main()
