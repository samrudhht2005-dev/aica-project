"""
Regression: packaged desktop camera must work without torch.

Protects against the v1.0.5 failure where vision/__init__.py eagerly imported
YOLO/Ultralytics, requiring torch and making /camera/* return HTTP 500.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _torch_available() -> bool:
    return importlib.util.find_spec("torch") is not None


class CameraWithoutTorchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _torch_available():
            raise unittest.SkipTest(
                "torch is installed in this environment; these tests assert "
                "camera behavior when torch is absent (packaged desktop)."
            )

    def setUp(self):
        # Drop cached vision/camera modules so each test starts clean.
        for name in list(sys.modules):
            if name == "vision" or name.startswith("vision.") or name == "camera" or name.startswith("camera."):
                del sys.modules[name]

    def test_vision_init_does_not_import_yolo(self):
        import vision  # noqa: F401

        self.assertNotIn("vision.yolo_inference", sys.modules)
        # product_classes must still be importable without torch
        from vision.product_classes import PRODUCT_CLASSES

        self.assertTrue(len(PRODUCT_CLASSES) >= 1)

    def test_camera_stream_imports_without_torch(self):
        from camera.camera_stream import CameraStreamer

        self.assertNotIn("vision.yolo_inference", sys.modules)
        streamer = CameraStreamer()
        self.assertIsNone(streamer.detector)
        self.assertIsNone(streamer._detector_error)

    def test_preview_idle_frame_without_torch(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        streamer.start()
        try:
            frame = streamer.get_frame_bytes()
            self.assertGreater(len(frame), 1000)
            summary = streamer.get_latest_summary()
            self.assertTrue(summary.get("preview_available"))
            self.assertFalse(summary.get("camera_powered"))
        finally:
            streamer.stop()

    def test_lazy_detector_fails_gracefully_without_torch(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        detector = streamer._ensure_detector()
        self.assertIsNone(detector)
        self.assertIsNotNone(streamer._detector_error)
        self.assertIn("torch", streamer._detector_error.lower())
        summary = streamer.get_latest_summary()
        self.assertTrue(summary.get("preview_available"))
        self.assertFalse(summary.get("ai_detection_available"))
        self.assertIn("torch", (summary.get("ai_detection_error") or "").lower())

    def test_simulation_power_on_without_torch(self):
        from camera.camera_stream import CameraStreamer

        streamer = CameraStreamer()
        streamer.start()
        try:
            streamer.set_simulation_mode(True)
            ok = streamer.set_camera_power(True)
            self.assertTrue(ok)
            self.assertTrue(streamer.camera_powered)
            self.assertGreater(len(streamer.get_frame_bytes()), 1000)
            summary = streamer.get_latest_summary()
            self.assertTrue(summary.get("preview_available"))
            # Simulation does not require YOLO; AI may still be unavailable.
            self.assertFalse(summary.get("ai_detection_available"))
        finally:
            streamer.set_camera_power(False)
            streamer.stop()


class CameraApiWithoutTorchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if _torch_available():
            raise unittest.SkipTest("torch is installed; skipping no-torch API tests")

    def test_ensure_camera_and_status_fields_without_torch(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "camera_api.db"
        os.environ["DATABASE_URL"] = "sqlite:///" + db.resolve().as_posix()
        os.environ["AICA_DESKTOP"] = "1"

        # Fresh import of routes after env is set
        for name in list(sys.modules):
            if name.startswith("backend.") or name in ("backend", "camera", "vision") or name.startswith("camera.") or name.startswith("vision."):
                # Keep backend.runtime_paths etc if already loaded; only reset camera route streamer
                pass

        import backend.routes as routes

        routes.streamer = None
        streamer = routes.ensure_camera()
        self.assertTrue(streamer.running)

        summary = streamer.get_latest_summary()
        self.assertTrue(summary.get("preview_available"))
        self.assertIn("ai_detection_available", summary)
        # ai_detection_error may be None until detector is probed
        status = {
            "preview_available": summary.get("preview_available", True),
            "ai_detection_available": summary.get("ai_detection_available", False),
            "ai_detection_error": summary.get("ai_detection_error"),
            "camera_powered": bool(getattr(streamer, "camera_powered", False)),
        }
        self.assertTrue(status["preview_available"])
        self.assertFalse(status["camera_powered"])

        # Probe detector and confirm status reflects unavailable AI
        streamer._ensure_detector()
        summary2 = streamer.get_latest_summary()
        self.assertFalse(summary2.get("ai_detection_available"))
        self.assertTrue(summary2.get("ai_detection_error"))

        streamer.stop()
        routes.streamer = None


class CameraHttpWithoutTorchTests(unittest.TestCase):
    """HTTP smoke: camera endpoints must not 500 solely because torch is missing."""

    @classmethod
    def setUpClass(cls):
        if _torch_available():
            raise unittest.SkipTest("torch is installed; skipping no-torch HTTP tests")

    def test_camera_status_and_power_not_500_without_torch(self):
        tmp = Path(tempfile.mkdtemp())
        db = tmp / "camera_http.db"
        url = "sqlite:///" + db.resolve().as_posix()
        os.environ["DATABASE_URL"] = url
        os.environ["AICA_DESKTOP"] = "1"
        os.environ["AICA_DB_BACKEND"] = "sqlite"

        # Isolated subprocess-style import via TestClient after schema init
        from database.schema_init import init_database_schema, reset_schema_init_state_for_tests
        import models.db_models  # noqa: F401

        reset_schema_init_state_for_tests()
        init_database_schema(force=True)

        import backend.routes as routes
        from models.db_models import Organization, User
        from database.db import SessionLocal
        from backend.auth import hash_password, create_session_token, SESSION_COOKIE

        routes.streamer = None

        db_sess = SessionLocal()
        try:
            org = Organization(name="Cam Org")
            db_sess.add(org)
            db_sess.flush()
            user = User(
                org_id=org.id,
                full_name="Cam User",
                email="cam@example.com",
                password_hash=hash_password("securePass9"),
            )
            db_sess.add(user)
            db_sess.commit()
            uid, oid = user.id, org.id
        finally:
            db_sess.close()

        from fastapi.testclient import TestClient
        import backend.main as mainmod

        client = TestClient(mainmod.app)
        token = create_session_token(uid, oid, remember=False)
        client.cookies.set(SESSION_COOKIE, token)

        status = client.get("/camera/status")
        self.assertNotEqual(status.status_code, 500, status.text[:500])
        self.assertEqual(status.status_code, 200, status.text[:500])
        body = status.json()
        self.assertIn("preview_available", body)
        self.assertIn("ai_detection_available", body)
        self.assertTrue(body.get("preview_available"))

        power = client.post("/camera/power", data={"enabled": "false"})
        self.assertNotEqual(power.status_code, 500, power.text[:500])
        self.assertEqual(power.status_code, 200, power.text[:500])

        # video_feed is an infinite MJPEG stream — do not consume it with TestClient
        # (hangs). Verify the streamer path used by video_feed works without torch.
        self.assertIsNotNone(routes.streamer)
        frame = routes.streamer.get_frame_bytes()
        self.assertGreater(len(frame), 1000)

        # Hitting the route must not raise/import-fail before StreamingResponse
        from backend.routes import video_feed as video_feed_route

        resp_obj = video_feed_route()
        self.assertEqual(getattr(resp_obj, "media_type", ""), "multipart/x-mixed-replace; boundary=frame")

        if routes.streamer is not None:
            routes.streamer.stop()
            routes.streamer = None


if __name__ == "__main__":
    unittest.main()
