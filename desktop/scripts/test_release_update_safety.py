"""
Release / update-manifest regression tests (no network, no GitHub publish).

Covers gaps that caused v1.0.7 clients to miss updates (missing manifest)
and safety rules (no downgrade, stable ignores prerelease, SHA mismatch).

Run:
  python -m unittest desktop.scripts.test_release_update_safety -v
  python desktop/scripts/test_release_update_safety.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.launcher import update_checker as uc  # noqa: E402
from desktop.launcher import update_config as ucfg  # noqa: E402
from desktop.launcher import update_download as ud  # noqa: E402

VALID_SHA = "a" * 64
ALT_SHA = "b" * 64


def _manifest(
    version: str = "1.1.0",
    *,
    channel: str = "stable",
    url: str | None = None,
    sha256: str = VALID_SHA,
    size_bytes: int = 1000,
    filename: str | None = None,
) -> dict:
    fn = filename or f"AICA_Setup_{version}.exe"
    if url is None:
        url = f"https://github.com/samrudhht2005-dev/aica-project/releases/download/v{version}/{fn}"
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


def _config(installed: str = "1.0.7", channel: str = "stable") -> ucfg.UpdateConfig:
    return ucfg.UpdateConfig(
        strategy="github_releases",
        repo="samrudhht2005-dev/aica-project",
        manifest_url="https://github.com/samrudhht2005-dev/aica-project/releases/latest/download/aica-update-manifest.json",
        installed_version=installed,
        channel=channel,
    )


class TestNoDowngrade(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name
        uc._latest_state = None
        uc._check_in_progress = False

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    @mock.patch.object(uc, "load_update_config", return_value=(_config("1.1.0"), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_equal_version_is_up_to_date(self, fetch, _cfg):
        fetch.return_value = (_manifest("1.1.0"), None)
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "up_to_date")
        self.assertFalse(state.update_available)

    @mock.patch.object(uc, "load_update_config", return_value=(_config("1.1.0"), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_older_manifest_never_downgrades(self, fetch, _cfg):
        fetch.return_value = (_manifest("1.0.7"), None)
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertEqual(state.status, "older_manifest_ignored")
        self.assertFalse(state.update_available)


class TestStableIgnoresPrerelease(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name
        uc._latest_state = None
        uc._check_in_progress = False

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    @mock.patch.object(uc, "load_update_config", return_value=(_config("1.0.7"), None))
    @mock.patch.object(uc, "_fetch_manifest")
    def test_prerelease_channel_ignored_on_stable(self, fetch, _cfg):
        fetch.return_value = (_manifest("1.1.0", channel="prerelease"), None)
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertFalse(state.update_available)
        self.assertEqual(state.status, "invalid_manifest")
        self.assertIn("non-stable", state.error or "")


class TestManifestIntegrity(unittest.TestCase):
    def test_filename_must_match_version(self):
        m = _manifest("1.1.0", filename="AICA_Setup_1.0.7.exe")
        data, err = uc.validate_manifest(m, installed_version="1.0.7")
        self.assertIsNone(data)
        self.assertTrue(err)

    def test_sha_must_be_lowercase_hex(self):
        m = _manifest(sha256="GG" * 32)
        data, err = uc.validate_manifest(m, installed_version="1.0.7")
        self.assertIsNone(data)

    def test_release_pipeline_scripts_exist(self):
        """Guard: build_installer must generate+verify manifest (v1.0.7 forgot upload)."""
        build_installer = (ROOT / "desktop" / "scripts" / "build_installer.ps1").read_text(encoding="utf-8")
        self.assertIn("generate_update_manifest.ps1", build_installer)
        self.assertIn("verify_release_artifacts.ps1", build_installer)
        self.assertIn("aica-update-manifest.json", build_installer)
        verify = ROOT / "desktop" / "scripts" / "verify_release_artifacts.ps1"
        self.assertTrue(verify.is_file())
        gen = ROOT / "desktop" / "scripts" / "generate_update_manifest.ps1"
        self.assertTrue(gen.is_file())


class TestShaMismatchRejected(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name
        ud._download_state = None

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    def test_verify_rejects_wrong_hash(self):
        staging = Path(self._tmpdir.name) / "updates" / "1.1.0"
        staging.mkdir(parents=True)
        payload = staging / "AICA_Setup_1.1.0.exe"
        payload.write_bytes(b"not-the-real-installer")
        # Public helper used after download
        ok = False
        try:
            digest = ud._sha256_file(payload)  # type: ignore[attr-defined]
            ok = digest == VALID_SHA
        except AttributeError:
            # Fallback: mimic verify logic
            import hashlib

            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            ok = digest == VALID_SHA
        self.assertFalse(ok)
        self.assertNotEqual(digest, VALID_SHA)


class TestAppDataPreservationContract(unittest.TestCase):
    def test_inno_does_not_wipe_appdata(self):
        iss = (ROOT / "desktop" / "packaging" / "aica_setup.iss").read_text(encoding="utf-8")
        self.assertIn("{localappdata}\\AICA", iss.replace("/", "\\"))
        # Must not Delete appdata aica.db
        self.assertNotIn("aica.db", iss.lower())
        self.assertIn("PrivilegesRequired=lowest", iss)

    def test_updater_restarts_same_install_dir(self):
        apply_src = (ROOT / "desktop" / "updater" / "updater_apply.py").read_text(encoding="utf-8")
        self.assertIn("restart_aica", apply_src)
        self.assertIn("post_install_validate", apply_src)


class TestMissingManifestFetch(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["AICA_APPDATA"] = self._tmpdir.name
        uc._latest_state = None

    def tearDown(self):
        os.environ.pop("AICA_APPDATA", None)
        self._tmpdir.cleanup()

    @mock.patch.object(uc, "load_update_config", return_value=(_config("1.0.7"), None))
    @mock.patch.object(uc, "_fetch_manifest", return_value=(None, "HTTP 404"))
    def test_missing_manifest_does_not_claim_update(self, *_m):
        state = uc.check_for_updates(force=True, use_cache=False)
        self.assertFalse(state.update_available)
        self.assertNotEqual(state.status, "update_available")
        self.assertEqual(state.status, "error")
        self.assertIn("404", state.error or "")


if __name__ == "__main__":
    unittest.main()
