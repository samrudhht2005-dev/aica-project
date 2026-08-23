"""
Phase 2 update checker tests — no GitHub network required.

Run: python desktop/scripts/test_update_check.py
  or: pytest desktop/scripts/test_update_check.py -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.launcher import update_checker as uc  # noqa: E402
from desktop.launcher import update_config as ucfg  # noqa: E402

VALID_SHA = "a" * 64


def _manifest(
    version: str = "1.0.3",
    *,
    channel: str = "stable",
    url: str = "https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
    sha256: str = VALID_SHA,
    size_bytes: int = 1000,
    filename: str | None = None,
) -> dict:
    fn = filename or f"AICA_Setup_{version}.exe"
    return {
        "version": version,
        "published_at": "2026-09-01T12:00:00+00:00",
        "channel": channel,
        "minimum_supported_version": None,
        "mandatory": False,
        "release_notes": "Test release",
        "installer": {
            "filename": fn,
            "url": url,
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
    }


def _config(installed: str = "1.0.2") -> ucfg.UpdateConfig:
    return ucfg.UpdateConfig(
        strategy="github_releases",
        repo="samrudhht2005-dev/aica-project",
        manifest_url="https://github.com/samrudhht2005-dev/aica-project/releases/latest/download/aica-update-manifest.json",
        installed_version=installed,
        channel="stable",
    )


class TestSemVerComparison(unittest.TestCase):
    def test_same_version(self):
        self.assertEqual(uc.compare_versions("1.0.2", "1.0.2"), 0)

    def test_newer_patch(self):
        self.assertEqual(uc.compare_versions("1.0.3", "1.0.2"), 1)
        self.assertEqual(uc.compare_versions("1.0.2", "1.0.3"), -1)

    def test_newer_minor(self):
        self.assertEqual(uc.compare_versions("1.1.0", "1.0.99"), 1)

    def test_newer_major(self):
        self.assertEqual(uc.compare_versions("2.0.0", "1.99.99"), 1)

    def test_multi_digit_patch(self):
        self.assertEqual(uc.compare_versions("1.0.10", "1.0.9"), 1)
        self.assertEqual(uc.compare_versions("10.0.0", "9.99.99"), 1)

    def test_invalid_version(self):
        self.assertIsNone(uc.compare_versions("bad", "1.0.0"))
        self.assertIsNone(uc.compare_versions("1.0", "1.0.0"))


class TestManifestValidation(unittest.TestCase):
    def test_valid_manifest(self):
        data, err = uc.validate_manifest(_manifest(), installed_version="1.0.2")
        self.assertIsNone(err)
        self.assertEqual(data["version"], "1.0.3")

    def test_malformed_root(self):
        data, err = uc.validate_manifest([], installed_version="1.0.2")
        self.assertIsNone(data)
        self.assertIn("object", err or "")

    def test_missing_release_notes(self):
        m = _manifest()
        m["release_notes"] = ""
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)

    def test_invalid_version(self):
        m = _manifest(version="not-a-version")
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)

    def test_http_url_rejected(self):
        m = _manifest(url="http://github.com/evil/AICA_Setup_1.0.3.exe")
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)
        self.assertIn("HTTPS", err or "")

    def test_disallowed_host(self):
        m = _manifest(url="https://evil.example.com/AICA_Setup_1.0.3.exe")
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)
        self.assertIn("allowlist", err or "")

    def test_invalid_sha256(self):
        m = _manifest(sha256="ABC" * 10)
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)

    def test_invalid_filename(self):
        m = _manifest(filename="wrong.exe")
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)

    def test_invalid_size(self):
        m = _manifest(size_bytes=0)
        data, err = uc.validate_manifest(m, installed_version="1.0.2")
        self.assertIsNone(data)


class TestUpdateDecisions(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name
        uc._latest_state = None
        uc._check_in_progress = False

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    @mock.patch.object(uc, "load_update_config", return_value=(_config(), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_newer_version_available(self, fetch, _cfg):
        fetch.return_value = (_manifest("1.0.3"), None)
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "update_available")
        self.assertTrue(state.update_available)
        self.assertEqual(state.available_version, "1.0.3")

    @mock.patch.object(uc, "load_update_config", return_value=(_config(), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_same_version_up_to_date(self, fetch, _cfg):
        fetch.return_value = (_manifest("1.0.2"), None)
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "up_to_date")
        self.assertFalse(state.update_available)

    @mock.patch.object(uc, "load_update_config", return_value=(_config(), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_older_manifest_ignored(self, fetch, _cfg):
        fetch.return_value = (_manifest("1.0.1"), None)
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "older_manifest_ignored")
        self.assertFalse(state.update_available)


class TestCache(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    def test_valid_recent_cache(self):
        payload = {
            "last_check_at": "2099-01-01T12:00:00+00:00",
            "installed_version": "1.0.2",
            "available_version": "1.0.3",
            "update_available": True,
            "status": "update_available",
            "mandatory": False,
            "release_notes": "cached",
            "manifest_summary": {"filename": "AICA_Setup_1.0.3.exe"},
            "error": None,
        }
        uc.write_update_cache(payload)
        cache = uc.read_update_cache()
        self.assertIsNotNone(cache)
        self.assertTrue(uc._cache_is_fresh(cache, "1.0.2"))

    def test_expired_cache(self):
        payload = {
            "last_check_at": "2020-01-01T12:00:00+00:00",
            "installed_version": "1.0.2",
            "status": "up_to_date",
        }
        uc.write_update_cache(payload)
        cache = uc.read_update_cache()
        self.assertFalse(uc._cache_is_fresh(cache, "1.0.2"))

    def test_corrupt_cache(self):
        path = uc._cache_path()
        path.write_text("{not json", encoding="utf-8")
        self.assertIsNone(uc.read_update_cache())

    def test_missing_cache(self):
        path = uc._cache_path()
        if path.exists():
            path.unlink()
        self.assertIsNone(uc.read_update_cache())

    @mock.patch.object(uc, "load_update_config", return_value=(_config(), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_check_uses_recent_cache(self, fetch, _cfg):
        payload = {
            "last_check_at": "2099-06-01T12:00:00+00:00",
            "installed_version": "1.0.2",
            "available_version": "1.0.3",
            "update_available": True,
            "status": "update_available",
            "mandatory": False,
            "release_notes": "from cache",
            "manifest_summary": None,
            "error": None,
        }
        uc.write_update_cache(payload)
        state = uc.check_for_updates(force=False, use_cache=True)
        self.assertTrue(state.from_cache)
        fetch.assert_not_called()


class TestNetworkBehavior(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    @mock.patch.object(uc, "load_update_config", return_value=(_config(), None))
    @mock.patch.object(uc, "_fetch_manifest", return_value=(None, "timeout"))
    def test_timeout(self, *_m):
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "timeout")

    @mock.patch.object(uc, "load_update_config", return_value=(_config(), None))
    @mock.patch.object(uc, "_fetch_manifest", return_value=(None, "no_network"))
    def test_no_network(self, *_m):
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "no_network")

    @mock.patch.object(uc, "load_update_config", return_value=(None, "bad config"))
    def test_invalid_config(self, *_m):
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "invalid_config")


class TestUpdateConfig(unittest.TestCase):
    def test_https_manifest_required(self):
        err = ucfg._validate_https_url("http://github.com/x", field="test")
        self.assertIn("HTTPS", err or "")

    def test_disallowed_manifest_host(self):
        err = ucfg._validate_https_url("https://evil.com/x", field="test")
        self.assertIn("allowlist", err or "")


class TestBackgroundScheduling(unittest.TestCase):
    def test_schedule_does_not_block(self):
        with mock.patch.object(uc, "_background_check") as bg:
            def slow(*a, **k):
                time.sleep(0.5)

            bg.side_effect = slow
            t0 = time.perf_counter()
            uc.schedule_background_update_check(delay_s=0.0, force=True)
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 0.2)
            time.sleep(0.8)


class TestUpdateStatusApi(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name
        uc._latest_state = None
        uc._check_in_progress = False

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    def test_get_update_status_pending(self):
        d = uc.get_update_status_dict()
        self.assertIn(d["status"], ("pending", "checking"))
        self.assertFalse(d["update_available"])

    def test_get_update_status_serializable(self):
        uc._latest_state = uc.UpdateState(
            status="update_available",
            installed_version="1.0.2",
            update_available=True,
            available_version="1.0.3",
            release_notes="Notes line",
            published_at="2026-09-01T12:00:00+00:00",
            installer={"filename": "AICA_Setup_1.0.3.exe", "url": "https://github.com/x", "sha256": "a" * 64, "size_bytes": 1},
        )
        d = uc.get_update_status_dict()
        json.dumps(d)
        self.assertTrue(d["update_available"])
        self.assertEqual(d["available_version"], "1.0.3")
        self.assertNotIn("url", d)
        self.assertEqual(d["installer_filename"], "AICA_Setup_1.0.3.exe")

    def test_force_refresh_returns_false_when_busy(self):
        uc._check_in_progress = True
        self.assertFalse(uc.force_refresh_update_check())

    @mock.patch.object(uc, "schedule_background_update_check")
    def test_force_refresh_schedules_when_idle(self, sched):
        uc._check_in_progress = False
        self.assertTrue(uc.force_refresh_update_check())
        sched.assert_called_once()


class TestBridgeSerialization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # voice_bridge imports voice stack; allow unit tests without full voice deps.
        for mod in ("numpy", "sounddevice", "webrtcvad"):
            if mod not in sys.modules:
                sys.modules[mod] = mock.MagicMock()

    def test_bridge_get_update_status(self):
        from desktop.launcher.voice_bridge import DesktopVoiceBridge

        uc._latest_state = uc.UpdateState(
            status="up_to_date",
            installed_version="1.0.2",
            update_available=False,
        )
        bridge = DesktopVoiceBridge()
        payload = bridge.get_update_status()
        self.assertEqual(payload["status"], "up_to_date")
        json.dumps(payload)

    @mock.patch.object(uc, "force_refresh_update_check", return_value=True)
    def test_bridge_check_for_updates_now(self, _refresh):
        from desktop.launcher.voice_bridge import DesktopVoiceBridge

        bridge = DesktopVoiceBridge()
        result = bridge.check_for_updates_now()
        self.assertTrue(result["ok"])
        self.assertTrue(result["checking"])


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
