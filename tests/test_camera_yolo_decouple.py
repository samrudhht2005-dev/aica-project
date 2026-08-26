"""Capture path must not wait on slow YOLO inference."""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class CameraYoloDecoupleTests(unittest.TestCase):
    def test_live_frame_returns_before_slow_yolo(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        slow = threading.Event()
        ran = {"n": 0}

        class SlowDetector:
            model_ready = True

            def detect(self, frame):
                ran["n"] += 1
                slow.wait(timeout=2.0)
                return []

            def summarize(self, detections):
                return {
                    "state": "scanning",
                    "message": "ok",
                    "detections": [],
                    "accepted": [],
                }

        streamer.detector = SlowDetector()
        streamer._session_gen = 1
        streamer.camera_powered = True
        streamer._start_ai_worker()

        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        t0 = time.perf_counter()
        out, token = streamer._process_live_frame(frame)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self.assertIsNone(token)
        self.assertIsNotNone(out)
        # Must not block on SlowDetector.detect (~would be seconds if sync).
        self.assertLess(elapsed_ms, 200.0)

        # Worker should pick up the offered frame.
        deadline = time.time() + 1.0
        while ran["n"] < 1 and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(ran["n"], 1)

        # Let worker finish one inference without hanging the test forever.
        slow.set()
        time.sleep(0.15)
        streamer._session_gen += 1
        streamer.camera_powered = False
        streamer._stop_ai_worker()

    def test_ai_worker_drops_stale_frames(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        streamer.detector = MagicMock(model_ready=True)
        streamer.detector.detect.return_value = []
        streamer.detector.summarize.return_value = {
            "state": "scanning",
            "message": "ok",
            "detections": [],
            "accepted": [],
        }
        streamer._session_gen = 1
        streamer.camera_powered = True
        # Do not start worker yet — fill slot then overwrite.
        streamer._ai_thread = threading.Thread(target=lambda: None, daemon=True)
        streamer._ai_thread.start()
        streamer._ai_thread.join(timeout=1)

        # Fake an "alive" worker thread for offer gate.
        class Alive:
            def is_alive(self):
                return True

        streamer._ai_thread = Alive()

        f1 = np.zeros((10, 10, 3), dtype=np.uint8)
        f2 = np.ones((10, 10, 3), dtype=np.uint8)
        streamer._offer_ai_frame(f1)
        streamer._offer_ai_frame(f2)
        with streamer.lock:
            self.assertIs(streamer._ai_frame, f2)
            self.assertGreaterEqual(streamer._perf["ai_frames_dropped"], 1)

    def test_power_off_clears_overlay_and_session(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        streamer.camera_powered = True
        streamer.is_simulated = True
        with streamer.lock:
            streamer._overlay_detections = [{"label": "x", "confidence": 0.9, "box": [0, 0, 1, 1]}]
        streamer.set_camera_power(False)
        self.assertFalse(streamer.camera_powered)
        with streamer.lock:
            self.assertEqual(streamer._overlay_detections, [])
        summary = streamer.get_latest_summary()
        self.assertEqual(summary["state"], "camera_off")
        self.assertEqual(summary["detections"], [])


if __name__ == "__main__":
    unittest.main()
