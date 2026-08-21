import json
import logging
import os
import time
from pathlib import Path

from ultralytics import YOLO

from vision.product_classes import PRODUCT_CLASSES, canonicalize_product_name

try:
    from backend.runtime_paths import project_root
    PROJECT_ROOT = project_root()
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = Path(__file__).resolve().parent / "detector_config.json"

# Kept for backwards-compatible imports (Gemini product options, etc.)
CLASSES = list(PRODUCT_CLASSES)


def load_detector_config() -> dict:
    defaults = {
        "model_path": "vision/weights/aica_product_detector.pt",
        "fallback_legacy_path": "vision/best.pt",
        "use_legacy_synthetic_model": False,
        "imgsz": 640,
        "confidence_threshold": 0.55,
        "iou_threshold": 0.45,
        "max_detections": 10,
        "require_user_confirm": True,
    }
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            defaults.update({k: v for k, v in data.items() if not k.startswith("notes")})
        except Exception as e:
            logging.error("Failed to read detector_config.json: %s", e)
    return defaults


class YOLOProductDetector:
    """
    Honest product detector for AICA POS.

    - Loads only the curated custom weights when present.
    - Does NOT remap COCO classes to grocery names (that produced fake Maggi/Coke/etc.).
    - Returns accepted detections only when confidence >= configured threshold.
    - Below-threshold hits are reported as uncertain, never forced into a class.
    """

    def __init__(self):
        self.config = load_detector_config()
        self.model = None
        self.model_path = None
        self.model_ready = False
        self.classes = list(PRODUCT_CLASSES)
        self._load_model()

    def _resolve_path(self, relative: str) -> Path:
        p = Path(relative)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def _load_model(self):
        primary = self._resolve_path(self.config["model_path"])
        legacy = self._resolve_path(self.config.get("fallback_legacy_path", "vision/best.pt"))

        path_to_load = None
        if primary.exists():
            path_to_load = primary
        elif self.config.get("use_legacy_synthetic_model") and legacy.exists():
            logging.warning(
                "Using legacy synthetic weights at %s — unsuitable for real products. "
                "Collect real images and train vision/weights/aica_product_detector.pt.",
                legacy,
            )
            path_to_load = legacy
        else:
            logging.warning(
                "No trained AICA product detector found at %s. "
                "Camera will report 'not confidently recognized' until you train one.",
                primary,
            )
            self.model = None
            self.model_ready = False
            return

        try:
            print(f"Loading AICA product detector from {path_to_load}...")
            self.model = YOLO(str(path_to_load))
            self.model_path = str(path_to_load)
            self.model_ready = True
            print(f"Detector ready. Classes in weights: {self.model.names}")
        except Exception as e:
            logging.error("Failed to load product detector: %s", e)
            self.model = None
            self.model_ready = False

    def reload(self):
        self.config = load_detector_config()
        self._load_model()

    def detect(self, frame, confidence_threshold: float | None = None) -> list[dict]:
        """
        Run detection and return list of dicts:
          label, confidence, box [x1,y1,x2,y2], accepted (bool), status
        """
        threshold = (
            float(confidence_threshold)
            if confidence_threshold is not None
            else float(self.config.get("confidence_threshold", 0.55))
        )
        if not self.model_ready or self.model is None:
            return []

        detections = []
        try:
            results = self.model.predict(
                source=frame,
                conf=min(0.15, threshold),  # keep low-conf for uncertain reporting
                iou=float(self.config.get("iou_threshold", 0.45)),
                imgsz=int(self.config.get("imgsz", 640)),
                max_det=int(self.config.get("max_detections", 10)),
                verbose=False,
            )
            if not results or results[0].boxes is None:
                return []

            for box in results[0].boxes:
                conf = float(box.conf[0])
                class_id = int(box.cls[0])
                raw_name = (
                    self.model.names.get(class_id, "")
                    if isinstance(self.model.names, dict)
                    else str(self.model.names[class_id])
                )
                label = canonicalize_product_name(raw_name)
                if label is None:
                    # Unknown class id / name outside curated set — treat as uncertain
                    xyxy = box.xyxy[0].tolist()
                    x1, y1, x2, y2 = map(int, xyxy)
                    detections.append({
                        "label": raw_name or "Unknown",
                        "canonical": None,
                        "confidence": conf,
                        "box": [x1, y1, x2, y2],
                        "accepted": False,
                        "status": "uncertain",
                    })
                    continue

                xyxy = box.xyxy[0].tolist()
                x1, y1, x2, y2 = map(int, xyxy)
                accepted = conf >= threshold
                detections.append({
                    "label": label,
                    "canonical": label,
                    "confidence": conf,
                    "box": [x1, y1, x2, y2],
                    "accepted": accepted,
                    "status": "accepted" if accepted else "uncertain",
                })
        except Exception as e:
            logging.error("Error during product detection: %s", e)

        return detections

    def summarize(self, detections: list[dict]) -> dict:
        """Build a UI-friendly status summary from detections."""
        if not self.model_ready:
            return {
                "state": "no_model",
                "message": "Product detector not trained yet. Collect images and train.",
                "detections": [],
                "accepted": [],
            }
        if not detections:
            return {
                "state": "scanning",
                "message": "Ready to scan — place a trained product in view.",
                "detections": [],
                "accepted": [],
            }
        accepted = [d for d in detections if d.get("accepted")]
        if accepted:
            names = ", ".join(f"{d['label']} ({d['confidence']*100:.0f}%)" for d in accepted)
            return {
                "state": "detected",
                "message": f"Product detected: {names}",
                "detections": detections,
                "accepted": accepted,
            }
        best = max(detections, key=lambda d: d.get("confidence", 0.0))
        return {
            "state": "uncertain",
            "message": "Product not confidently recognized. Move closer or adjust angle.",
            "detections": detections,
            "accepted": [],
            "best_guess": {
                "label": best.get("label"),
                "confidence": best.get("confidence"),
            },
        }
