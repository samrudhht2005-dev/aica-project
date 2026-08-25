"""OpenCV QR detection in the POS camera pipeline (no torch / YOLO)."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _qr_frame_from_token(token: str, canvas_size=(640, 480)):
    from backend.weigh_label import qr_png_bytes

    png = qr_png_bytes(token, box_size=8, border=2)
    arr = np.frombuffer(png, dtype=np.uint8)
    qr_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    assert qr_img is not None
    h, w = canvas_size[1], canvas_size[0]
    frame = np.full((h, w, 3), 255, dtype=np.uint8)
    qh, qw = qr_img.shape[:2]
    scale = min((w - 80) / qw, (h - 80) / qh, 1.0)
    nw, nh = int(qw * scale), int(qh * scale)
    qr_resized = cv2.resize(qr_img, (nw, nh), interpolation=cv2.INTER_NEAREST)
    x0 = (w - nw) // 2
    y0 = (h - nh) // 2
    frame[y0 : y0 + nh, x0 : x0 + nw] = qr_resized
    return frame


class CameraQrDetectionTests(unittest.TestCase):
    def test_opencv_decodes_aica_token_exactly(self):
        from backend.weigh_tickets import generate_public_token
        from camera.camera_stream import CameraStreamer

        token = generate_public_token()
        frame = _qr_frame_from_token(token)
        streamer = CameraStreamer()
        decoded, _pts = streamer.detect_aica_qr_token(frame, force=True)
        self.assertEqual(decoded, token)

    def test_non_aica_qr_ignored(self):
        from camera.camera_stream import CameraStreamer
        from backend.weigh_label import qr_png_bytes

        png = qr_png_bytes("NOT-AICA-PAYLOAD-12345", box_size=8, border=2)
        arr = np.frombuffer(png, dtype=np.uint8)
        qr_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        h, w = 480, 640
        frame = np.full((h, w, 3), 255, dtype=np.uint8)
        qh, qw = qr_img.shape[:2]
        frame[40 : 40 + qh, 40 : 40 + qw] = qr_img

        streamer = CameraStreamer()
        decoded, _ = streamer.detect_aica_qr_token(frame, force=True)
        self.assertIsNone(decoded)
        self.assertFalse(CameraStreamer.is_aica_qr_payload("https://example.com/x"))

    def test_qr_path_without_torch_or_yolo_import(self):
        # Drop vision yolo if previously imported by other tests in same process.
        for name in list(sys.modules):
            if name == "vision.yolo_inference" or name.startswith("vision.yolo_inference"):
                del sys.modules[name]

        from backend.weigh_tickets import generate_public_token
        from camera.camera_stream import CameraStreamer

        token = generate_public_token()
        frame = _qr_frame_from_token(token)
        streamer = CameraStreamer()
        # Force detector failure path without importing torch successfully
        streamer._detector_error = "No module named 'torch'"
        out, got, ran_yolo = streamer.process_frame_qr_first(frame.copy())
        self.assertEqual(got, token)
        self.assertFalse(ran_yolo)
        self.assertNotIn("vision.yolo_inference", sys.modules)
        summary = streamer.get_latest_summary()
        self.assertTrue(summary.get("qr_detection_available"))
        self.assertFalse(summary.get("ai_detection_available"))
        self.assertIsNotNone(summary.get("qr_event"))
        self.assertEqual(summary["qr_event"]["token"], token)

    def test_duplicate_suppression_within_cooldown(self):
        from backend.weigh_tickets import generate_public_token
        from camera.camera_stream import CameraStreamer

        token = generate_public_token()
        frame = _qr_frame_from_token(token)
        streamer = CameraStreamer()
        streamer.qr_cooldown = 4.0
        streamer._detector_error = "No module named 'torch'"

        _, t1, _ = streamer.process_frame_qr_first(frame.copy())
        e1 = streamer.get_latest_summary()["qr_event"]
        self.assertEqual(t1, token)
        event_id_1 = e1["event_id"]

        # Immediate second pass — same event_id (no spam)
        streamer._last_qr_scan_at = 0  # allow detect after throttle
        _, t2, _ = streamer.process_frame_qr_first(frame.copy())
        e2 = streamer.get_latest_summary()["qr_event"]
        self.assertEqual(t2, token)
        self.assertEqual(e2["event_id"], event_id_1)

        # After cooldown — new event_id (allows deliberate rescan / already_purchased)
        streamer._last_qr_emit_at = time.time() - (streamer.qr_cooldown + 0.5)
        streamer._last_qr_scan_at = 0
        _, t3, _ = streamer.process_frame_qr_first(frame.copy())
        e3 = streamer.get_latest_summary()["qr_event"]
        self.assertEqual(t3, token)
        self.assertNotEqual(e3["event_id"], event_id_1)

    def test_summary_exposes_qr_flags_when_camera_off(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        summary = streamer.get_latest_summary()
        self.assertTrue(summary.get("preview_available"))
        self.assertTrue(summary.get("qr_detection_available"))
        self.assertIsNone(summary.get("qr_event"))
        self.assertEqual(summary.get("scan_purpose"), "checkout")

    def test_scan_purpose_clears_stale_qr_event(self):
        from backend.weigh_tickets import generate_public_token
        from camera.camera_stream import CameraStreamer

        token = generate_public_token()
        frame = _qr_frame_from_token(token)
        streamer = CameraStreamer()
        streamer._detector_error = "No module named 'torch'"
        streamer.process_frame_qr_first(frame.copy())
        self.assertIsNotNone(streamer.get_latest_summary()["qr_event"])
        self.assertEqual(streamer.set_scan_purpose("cancel"), "cancel")
        summary = streamer.get_latest_summary()
        self.assertEqual(summary["scan_purpose"], "cancel")
        self.assertIsNone(summary["qr_event"])
        self.assertEqual(streamer.set_scan_purpose("checkout"), "checkout")


if __name__ == "__main__":
    unittest.main()
