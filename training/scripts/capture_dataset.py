"""
Webcam capture helper for building the AICA product dataset.

Does NOT require a trained model — this is for collecting photos before training.

Usage:
  python training/scripts/capture_dataset.py --camera 0

Keys:
  SPACE  — save frame into dataset/aica_products/raw/<class>/
  N      — next class
  P      — previous class
  Q      — quit

After capture, annotate with LabelImg/Roboflow (YOLO format), then:
  python training/scripts/prepare_dataset.py --split-raw
  python training/scripts/train_product_detector.py
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vision.product_classes import PRODUCT_CLASSES  # noqa: E402

RAW = ROOT / "dataset" / "aica_products" / "raw"


def class_dir(name: str) -> Path:
    d = RAW / name.replace(" ", "_").lower()
    d.mkdir(parents=True, exist_ok=True)
    return d


def main():
    parser = argparse.ArgumentParser(description="Capture real product photos for AICA training")
    parser.add_argument("--class", dest="cls", default=PRODUCT_CLASSES[0])
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--target", type=int, default=150, help="Suggested images per class")
    args = parser.parse_args()

    idx = 0
    if args.cls in PRODUCT_CLASSES:
        idx = PRODUCT_CLASSES.index(args.cls)
    else:
        print(f"Unknown class '{args.cls}'. Using {PRODUCT_CLASSES[0]}.")

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"Could not open camera index {args.camera}")
        print("Try --camera 1 or another index.")
        sys.exit(1)

    print("AICA dataset capture")
    print("  SPACE = save photo")
    print("  N     = next product class")
    print("  P     = previous class")
    print("  Q     = quit")
    print(f"Classes: {', '.join(PRODUCT_CLASSES)}")

    while True:
        cls = PRODUCT_CLASSES[idx]
        out_dir = class_dir(cls)
        count = len(list(out_dir.glob("*.jpg"))) + len(list(out_dir.glob("*.png")))

        ok, frame = cap.read()
        if not ok:
            print("Camera read failed")
            break

        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (frame.shape[1], 70), (20, 20, 20), -1)
        cv2.putText(
            overlay,
            f"{cls}  saved={count}/{args.target}  [{idx + 1}/{len(PRODUCT_CLASSES)}]",
            (12, 44),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (80, 220, 120),
            2,
            cv2.LINE_AA,
        )
        cv2.imshow("AICA dataset capture", overlay)
        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break
        if key == ord("n"):
            idx = (idx + 1) % len(PRODUCT_CLASSES)
        if key == ord("p"):
            idx = (idx - 1) % len(PRODUCT_CLASSES)
        if key == ord(" "):
            ts = int(time.time() * 1000)
            path = out_dir / f"{cls.replace(' ', '_').lower()}_{ts}.jpg"
            cv2.imwrite(str(path), frame)
            print(f"Saved {path}")

    cap.release()
    cv2.destroyAllWindows()
    print("Done. Next: annotate YOLO boxes, then prepare_dataset.py --split-raw")


if __name__ == "__main__":
    main()
