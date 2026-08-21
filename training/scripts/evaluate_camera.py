"""
Real-world camera evaluation checklist helper.

Opens the webcam with the deployed AICA detector and logs predictions.
Use AFTER training. Do not mix these evaluation captures into the training set
until after you finish scoring.

Keys:
  S — snapshot + log current detections to training/runs/aica_products/camera_eval_log.csv
  Q — quit
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from vision.yolo_inference import YOLOProductDetector  # noqa: E402

LOG = ROOT / "training" / "runs" / "aica_products" / "camera_eval_log.csv"
SNAP_DIR = ROOT / "training" / "runs" / "aica_products" / "camera_eval_frames"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--ground-truth", type=str, default="", help="Optional expected class name for this session")
    args = parser.parse_args()

    detector = YOLOProductDetector()
    if not detector.model_ready:
        print("No trained detector at vision/weights/aica_product_detector.pt")
        print("Collect photos with capture_dataset.py, annotate, then train first.")
        sys.exit(1)

    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print("Camera open failed")
        sys.exit(1)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    if not LOG.exists():
        with open(LOG, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["timestamp", "ground_truth", "pred_label", "confidence", "accepted", "latency_ms", "snapshot"]
            )

    print("Hold a product in view. Press S to log, Q to quit.")
    while True:
        t0 = time.time()
        ok, frame = cap.read()
        if not ok:
            break
        detections = detector.detect(frame)
        latency_ms = (time.time() - t0) * 1000
        summary = detector.summarize(detections)

        display = frame.copy()
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            color = (46, 204, 113) if det["accepted"] else (0, 165, 255)
            cv2.rectangle(display, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                display,
                f"{det['label']} {det['confidence']*100:.0f}%",
                (x1, max(20, y1 - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
            )
        cv2.putText(
            display,
            f"{summary['state']} | {latency_ms:.0f} ms",
            (10, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (255, 255, 255),
            2,
        )
        cv2.imshow("AICA camera eval", display)
        key = cv2.waitKey(1) & 0xFF
        if key == ord("q"):
            break
        if key == ord("s"):
            ts = int(time.time() * 1000)
            snap = SNAP_DIR / f"eval_{ts}.jpg"
            cv2.imwrite(str(snap), frame)
            preds = detections or [
                {"label": "", "confidence": 0.0, "accepted": False}
            ]
            with open(LOG, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                for det in preds:
                    w.writerow(
                        [
                            ts,
                            args.ground_truth,
                            det.get("label", ""),
                            f"{det.get('confidence', 0):.4f}",
                            int(bool(det.get("accepted"))),
                            f"{latency_ms:.1f}",
                            str(snap),
                        ]
                    )
            print(f"Logged {len(preds)} row(s) → {LOG}")

    cap.release()
    cv2.destroyAllWindows()
    print(f"Log file: {LOG}")


if __name__ == "__main__":
    main()
