import logging
import random
import threading
import time

import cv2
import numpy as np

from vision.product_classes import PRODUCT_CLASSES

# Opaque weigh-ticket QR prefix (must match backend.weigh_tickets.TOKEN_PREFIX).
AICA_QR_PREFIX = "AICA1."


class CameraStreamer:
    def __init__(self, detection_callback=None):
        # YOLO/Ultralytics is heavy — load lazily on first camera power-ON (not at FastAPI startup).
        self.detector = None
        self._detector_error = None
        self._detector_lock = threading.Lock()
        self.detection_callback = detection_callback

        self.running = False
        self.cap = None
        self.thread = None

        self.latest_frame = None
        self.lock = threading.Lock()
        self.is_simulated = False
        self.camera_index = 0
        self.camera_error = False
        self.camera_powered = False  # OFF by default — POS must toggle ON

        self.latest_summary = {
            "state": "camera_off",
            "message": "Camera is off. Turn it on to scan products.",
            "detections": [],
            "accepted": [],
            "updated_at": 0.0,
            "model_ready": False,
        }
        self.auto_add_enabled = False
        self.last_detection = {}
        self.cooldown = 3.0

        # OpenCV QR (no torch). Throttle + debounce so one held QR does not spam cart.
        self._qr_detector = None
        self._last_qr_scan_at = 0.0
        self._qr_scan_interval = 0.20  # seconds between QR decode attempts
        self.qr_cooldown = 4.0  # seconds before same token emits a new event_id
        self._last_qr_token = None
        self._last_qr_emit_at = 0.0
        self._qr_event_seq = 0
        self._latest_qr_event = None
        # Who should consume qr_event: "checkout" (POS) or "cancel" (Weigh verified cancel).
        self.scan_purpose = "checkout"

        self.sim_products = list(PRODUCT_CLASSES)
        self.current_sim_product = None
        self.sim_product_start = 0
        self.sim_product_duration = 3.0
        self.sim_next_product_time = 0
        self.scan_line_y = 80
        self.scan_dir = 1

    def _ensure_qr_detector(self):
        if self._qr_detector is None:
            self._qr_detector = cv2.QRCodeDetector()
        return self._qr_detector

    def _ensure_detector(self):
        """Load YOLO only when POS camera is first powered on. Returns None if unavailable."""
        if self.detector is not None:
            return self.detector
        if self._detector_error is not None and self.detector is None:
            # Previous load failed (e.g. missing torch in packaged desktop).
            return None
        with self._detector_lock:
            if self.detector is not None:
                return self.detector
            if self._detector_error is not None:
                return None
            logging.info("Lazy-loading YOLO product detector (first camera use)...")
            t0 = time.perf_counter()
            try:
                from vision.yolo_inference import YOLOProductDetector
                self.detector = YOLOProductDetector()
            except Exception as e:
                self._detector_error = str(e)
                logging.exception("YOLO product detector unavailable: %s", e)
                return None
            logging.info("YOLO product detector ready in %.2fs", time.perf_counter() - t0)
            return self.detector

    def start(self):
        if self.running:
            return
        self.running = True
        self.camera_powered = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        print("Camera Streamer started (powered OFF by default).")

    def stop(self):
        self.running = False
        self.camera_powered = False
        if self.thread:
            self.thread.join(timeout=2)
        if self.cap:
            self.cap.release()
            self.cap = None
        print("Camera Streamer stopped.")

    def get_latest_summary(self) -> dict:
        with self.lock:
            summary = dict(self.latest_summary)
            summary["detections"] = list(self.latest_summary.get("detections") or [])
            summary["accepted"] = list(self.latest_summary.get("accepted") or [])
            qr_event = dict(self._latest_qr_event) if self._latest_qr_event else None
            det = self.detector
            model_ready = bool(det and getattr(det, "model_ready", False))
            summary["model_ready"] = model_ready
            summary["preview_available"] = True
            summary["ai_detection_available"] = model_ready
            summary["ai_detection_error"] = self._detector_error
            summary["qr_detection_available"] = True
            summary["qr_event"] = qr_event
            summary["scan_purpose"] = self.scan_purpose
            summary["auto_add_enabled"] = self.auto_add_enabled
            summary["camera_powered"] = self.camera_powered
            return summary

    def set_auto_add(self, enabled: bool):
        self.auto_add_enabled = bool(enabled)

    def set_scan_purpose(self, purpose: str) -> str:
        """
        Route QR events to checkout (POS) or cancel (Weigh).
        Switching purpose clears the pending qr_event so a stale token
        is not immediately applied to the other consumer.
        """
        p = (purpose or "checkout").strip().lower()
        if p not in ("checkout", "cancel"):
            p = "checkout"
        with self.lock:
            if p != self.scan_purpose:
                self.scan_purpose = p
                self._latest_qr_event = None
                self._last_qr_token = None
                self._last_qr_emit_at = 0.0
            else:
                self.scan_purpose = p
        return self.scan_purpose

    def clear_qr_event(self):
        with self.lock:
            self._latest_qr_event = None

    def set_camera_power(self, powered: bool) -> bool:
        """Explicit ON/OFF. OFF releases the device and stops CV inference."""
        powered = bool(powered)
        if powered == self.camera_powered:
            return True

        if not powered:
            self.camera_powered = False
            if self.cap:
                try:
                    self.cap.release()
                except Exception:
                    pass
                self.cap = None
            self.current_sim_product = None
            with self.lock:
                self._latest_qr_event = None
            self._publish_summary({
                "state": "camera_off",
                "message": "Camera is off. Turn it on to scan products.",
                "detections": [],
                "accepted": [],
            })
            print("Camera powered OFF — device released.")
            return True

        # Power ON — try YOLO now (keeps FastAPI/desktop startup fast).
        # Preview + OpenCV QR still work if the detector is missing (no torch).
        detector = None if self.is_simulated else self._ensure_detector()

        self.camera_powered = True
        self.camera_error = False
        if self.is_simulated:
            self._publish_summary({
                "state": "scanning",
                "message": "Simulation active — scanning…",
                "detections": [],
                "accepted": [],
            })
            print("Camera powered ON (simulation).")
            return True

        try:
            self.cap = cv2.VideoCapture(self.camera_index)
            if not self.cap.isOpened():
                self.cap = None
                self.camera_error = True
                self.camera_powered = False
                self._publish_summary({
                    "state": "offline",
                    "message": "Could not open camera. Check the device index.",
                    "detections": [],
                    "accepted": [],
                })
                return False
            ret, frame = self.cap.read()
            if not ret or frame is None:
                self.cap.release()
                self.cap = None
                self.camera_error = True
                self.camera_powered = False
                self._publish_summary({
                    "state": "offline",
                    "message": "Camera opened but failed to read frames.",
                    "detections": [],
                    "accepted": [],
                })
                return False
            if detector is not None:
                scan_msg = "Ready — show an AICA QR ticket or a trained product."
            else:
                reason = self._detector_error or "product detector unavailable"
                scan_msg = (
                    "Camera live — QR inventory scanning ready. "
                    f"AI product detection unavailable ({reason})."
                )
            self._publish_summary({
                "state": "scanning" if detector is not None else "preview",
                "message": scan_msg,
                "detections": [],
                "accepted": [],
            })
            print(f"Camera powered ON (index {self.camera_index}).")
            return True
        except Exception as e:
            logging.error("Camera power ON failed: %s", e)
            self.cap = None
            self.camera_error = True
            self.camera_powered = False
            return False

    def set_simulation_mode(self, simulate: bool):
        if simulate:
            self.is_simulated = True
            self.camera_error = False
            if self.cap:
                self.cap.release()
                self.cap = None
            print("Switched to SIMULATION mode.")
            return True

        self.is_simulated = False
        if self.camera_powered:
            return self.set_camera_power(True)
        print("Physical camera selected (still powered OFF until toggled ON).")
        return True

    def _publish_summary(self, summary: dict):
        summary = dict(summary)
        summary["updated_at"] = time.time()
        summary["model_ready"] = bool(getattr(self.detector, "model_ready", False))
        summary["camera_powered"] = self.camera_powered
        with self.lock:
            self.latest_summary = summary

    @staticmethod
    def is_aica_qr_payload(raw: str) -> bool:
        return bool(raw) and str(raw).strip().startswith(AICA_QR_PREFIX)

    def detect_aica_qr_token(self, frame, *, force: bool = False):
        """
        Decode an AICA weigh-ticket QR from a BGR frame using OpenCV only.

        Returns (token, points) or (None, None). Non-AICA payloads are ignored.
        Throttled unless force=True (tests).
        """
        if frame is None:
            return None, None
        now = time.time()
        if not force and (now - self._last_qr_scan_at) < self._qr_scan_interval:
            return None, None
        self._last_qr_scan_at = now
        try:
            detector = self._ensure_qr_detector()
            data, points, _ = detector.detectAndDecode(frame)
        except Exception as e:
            logging.debug("QR detect failed: %s", e)
            return None, None
        if not data:
            return None, None
        token = str(data).strip()
        if not self.is_aica_qr_payload(token):
            return None, None
        return token, points

    def _emit_qr_event(self, token: str, *, force_new: bool = False) -> dict:
        """
        Debounce identical tokens. Returns the current qr_event dict.

        Within qr_cooldown for the same token, reuses the same event_id so the
        POS does not re-resolve every frame. After cooldown, a new event_id is
        emitted so a deliberate rescan (e.g. already purchased) can surface.
        """
        now = time.time()
        with self.lock:
            if (
                not force_new
                and token == self._last_qr_token
                and self._latest_qr_event is not None
                and (now - self._last_qr_emit_at) < self.qr_cooldown
            ):
                return dict(self._latest_qr_event)
            self._last_qr_token = token
            self._last_qr_emit_at = now
            self._qr_event_seq += 1
            self._latest_qr_event = {
                "token": token,
                "timestamp": now,
                "event_id": self._qr_event_seq,
            }
            return dict(self._latest_qr_event)

    def process_frame_qr_first(self, frame):
        """
        QR-first pass used by the live camera loop and tests.

        Returns (frame, qr_token_or_None, ran_yolo_bool).
        """
        now = time.time()
        throttled = (now - self._last_qr_scan_at) < self._qr_scan_interval
        if throttled:
            with self.lock:
                holding_qr = (
                    self._latest_qr_event is not None
                    and self._last_qr_token
                    and (now - self._last_qr_emit_at) < self.qr_cooldown
                )
            if holding_qr:
                # Same QR still in cooldown window — do not YOLO this frame.
                return frame, self._last_qr_token, False
            token, points = None, None
        else:
            token, points = self.detect_aica_qr_token(frame, force=True)

        if token:
            self._emit_qr_event(token)
            if points is not None:
                try:
                    pts = points.astype(int).reshape(-1, 2)
                    if len(pts) >= 4:
                        cv2.polylines(frame, [pts], True, (46, 204, 113), 2)
                        cv2.putText(
                            frame,
                            "AICA QR",
                            (int(pts[0][0]), max(20, int(pts[0][1]) - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.6,
                            (46, 204, 113),
                            2,
                            cv2.LINE_AA,
                        )
                except Exception:
                    pass
            # QR wins this cycle — skip YOLO for this frame.
            if self.detector is not None and getattr(self.detector, "model_ready", False):
                msg = "AICA QR ticket detected — resolving inventory…"
                state = "scanning"
            else:
                msg = "AICA QR ticket detected — resolving inventory… (AI product detection unavailable)"
                state = "preview"
            self._publish_summary({
                "state": state,
                "message": msg,
                "detections": [],
                "accepted": [],
            })
            return frame, token, False

        detector = self._ensure_detector()
        if detector is not None:
            detections = detector.detect(frame)
            summary = detector.summarize(detections)
            self._publish_summary(summary)
            frame = self._draw_detections(frame, detections)
            if self.auto_add_enabled and self.detection_callback:
                for det in summary.get("accepted") or []:
                    self._handle_detection(det["label"], det["confidence"])
            return frame, None, True

        reason = self._detector_error or "product detector unavailable"
        self._publish_summary({
            "state": "preview",
            "message": (
                "Camera live — QR inventory scanning ready. "
                f"AI product detection unavailable ({reason})."
            ),
            "detections": [],
            "accepted": [],
        })
        return frame, None, False

    def _idle_frame(self, message: str = "CAMERA OFF"):
        h, w = 480, 640
        frame = np.zeros((h, w, 3), dtype=np.uint8)
        frame[:] = (24, 28, 36)
        cv2.putText(frame, message, (180, 230), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (148, 163, 175), 2, cv2.LINE_AA)
        cv2.putText(
            frame,
            "Toggle Camera ON to start scanning",
            (120, 280),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (100, 116, 139),
            1,
            cv2.LINE_AA,
        )
        return frame

    def _draw_detections(self, frame, detections):
        for det in detections:
            label = det["label"]
            conf = det["confidence"]
            x1, y1, x2, y2 = det["box"]
            accepted = det.get("accepted", False)
            color = (46, 204, 113) if accepted else (241, 196, 15)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            status = "OK" if accepted else "LOW"
            label_str = f"{label} {int(conf * 100)}% [{status}]"
            (w_text, h_text), _ = cv2.getTextSize(label_str, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 2)
            cv2.rectangle(frame, (x1, y1 - 25), (x1 + w_text + 10, y1), color, -1)
            cv2.putText(frame, label_str, (x1 + 5, y1 - 7), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 2, cv2.LINE_AA)
        return frame

    def _run(self):
        # Do NOT open VideoCapture until camera_powered is True
        self._publish_summary({
            "state": "camera_off",
            "message": "Camera is off. Turn it on to scan products.",
            "detections": [],
            "accepted": [],
        })

        while self.running:
            start_time = time.time()

            if not self.camera_powered:
                frame = self._idle_frame()
                with self.lock:
                    self.latest_frame = frame.copy()
                time.sleep(0.1)
                continue

            if self.is_simulated:
                frame = self._generate_simulated_frame()
            else:
                try:
                    if self.cap is None:
                        frame = self._idle_frame("NO DEVICE")
                    else:
                        ret, frame = self.cap.read()
                        if not ret or frame is None:
                            print("Lost camera stream. Powering OFF.")
                            self.set_camera_power(False)
                            continue
                        frame, _token, _yolo = self.process_frame_qr_first(frame)
                except Exception as e:
                    logging.error("Error reading camera frame: %s", e)
                    self.set_camera_power(False)
                    continue

            with self.lock:
                self.latest_frame = frame.copy()

            elapsed = time.time() - start_time
            sleep_time = max(0.033 - elapsed, 0.005)
            time.sleep(sleep_time)

    def _generate_simulated_frame(self):
        h, w = 480, 640
        frame = np.zeros((h, w, 3), dtype=np.uint8)

        for x in range(0, w, 40):
            cv2.line(frame, (x, 0), (x, h), (24, 28, 36), 1)
        for y in range(0, h, 40):
            cv2.line(frame, (0, y), (w, y), (24, 28, 36), 1)

        now = time.time()
        cv2.rectangle(frame, (10, 10), (420, 80), (31, 41, 55), -1)
        cv2.putText(
            frame,
            "DEMO SIMULATION (not real CV)",
            (20, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (52, 152, 219),
            2,
            cv2.LINE_AA,
        )

        self.scan_line_y += self.scan_dir * 6
        if self.scan_line_y >= h - 40 or self.scan_line_y <= 100:
            self.scan_dir *= -1
        cv2.line(frame, (20, self.scan_line_y), (w - 20, self.scan_line_y), (46, 204, 113), 2)

        if self.current_sim_product is None:
            if now >= self.sim_next_product_time:
                self.current_sim_product = random.choice(self.sim_products)
                self.sim_product_start = now
                self.sim_next_product_time = now + self.sim_product_duration + random.randint(4, 7)
                self._publish_summary({
                    "state": "scanning",
                    "message": "Scanning…",
                    "detections": [],
                    "accepted": [],
                })
        else:
            if now - self.sim_product_start > self.sim_product_duration:
                self.current_sim_product = None
                self._publish_summary({
                    "state": "scanning",
                    "message": "Ready — show an AICA QR ticket or a trained product.",
                    "detections": [],
                    "accepted": [],
                })
            else:
                box_x1, box_y1 = 200, 160
                box_x2, box_y2 = 440, 340
                conf = 0.82 + (np.sin(now * 10) + 1) * 0.08
                det = {
                    "label": self.current_sim_product,
                    "canonical": self.current_sim_product,
                    "confidence": float(conf),
                    "box": [box_x1, box_y1, box_x2, box_y2],
                    "accepted": True,
                    "status": "accepted",
                }
                frame = self._draw_detections(frame, [det])
                self._publish_summary({
                    "state": "detected",
                    "message": f"Demo product: {self.current_sim_product} ({int(conf * 100)}%)",
                    "detections": [det],
                    "accepted": [det],
                })
                if self.auto_add_enabled:
                    self._handle_detection(self.current_sim_product, conf)

        return frame

    def _handle_detection(self, product_name, confidence):
        now = time.time()
        last_time = self.last_detection.get(product_name, 0.0)
        if now - last_time < self.cooldown:
            return
        self.last_detection[product_name] = now
        print(f"Vision hit: Detected '{product_name}' at {confidence:.2f} confidence.")
        if self.detection_callback:
            threading.Thread(
                target=self.detection_callback,
                args=(product_name, confidence),
                daemon=True,
            ).start()

    def get_frame_bytes(self):
        with self.lock:
            if self.latest_frame is None:
                frame = self._idle_frame("CAMERA OFF")
            else:
                frame = self.latest_frame.copy()

        ret, jpeg = cv2.imencode(".jpg", frame)
        if not ret:
            return b""
        return jpeg.tobytes()

    def set_camera_index(self, index: int):
        self.camera_index = index
        self.camera_error = False
        print(f"Camera index set to {index}.")
        if self.camera_powered and not self.is_simulated:
            self.set_camera_power(False)
            return self.set_camera_power(True)
        return True
