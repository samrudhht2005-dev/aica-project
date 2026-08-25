"""Regression: app_release_info must read version.json with or without UTF-8 BOM."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class AppReleaseInfoBomTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.version_path = self.root / "desktop" / "config" / "version.json"
        self.version_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmpdir.cleanup()

    def _write_version(self, *, with_bom: bool, version: str = "1.0.7", build: str = "test-build") -> None:
        payload = {
            "name": "AICA",
            "version": version,
            "channel": "stable",
            "build": build,
            "window_title": "AICA",
            "update": {
                "strategy": "github_releases",
                "repo": "samrudhht2005-dev/aica-project",
                "manifest_url": "https://github.com/samrudhht2005-dev/aica-project/releases/latest/download/aica-update-manifest.json",
            },
        }
        body = json.dumps(payload, indent=4).encode("utf-8")
        if with_bom:
            body = b"\xef\xbb\xbf" + body
        self.version_path.write_bytes(body)

    def _info_with_env(self, **env):
        from backend import runtime_paths as rp

        # Isolate from developer AppData / repo version.json via project_root mock.
        with mock.patch.dict(os.environ, env, clear=False):
            with mock.patch.object(rp, "project_root", return_value=self.root):
                with mock.patch.object(rp, "is_frozen", return_value=False):
                    return rp.app_release_info()

    def test_version_json_without_bom(self):
        self._write_version(with_bom=False, version="1.0.7")
        info = self._info_with_env(AICA_VERSION="1.0.2", AICA_BUILD="env-build")
        self.assertEqual(info["version"], "1.0.7")
        self.assertEqual(info["build"], "test-build")
        self.assertEqual(info["channel"], "stable")

    def test_version_json_with_utf8_bom(self):
        self._write_version(with_bom=True, version="1.0.7")
        # Simulate AppData/config.env override that previously won when BOM broke parsing.
        info = self._info_with_env(AICA_VERSION="1.0.2", AICA_BUILD="stale")
        self.assertEqual(info["version"], "1.0.7")
        self.assertEqual(info["build"], "test-build")

    def test_missing_version_json_falls_back_to_env(self):
        # No version.json written
        info = self._info_with_env(AICA_VERSION="9.9.9", AICA_BUILD="only-env")
        self.assertEqual(info["version"], "9.9.9")
        self.assertEqual(info["build"], "only-env")

    def test_invalid_json_falls_back_to_env(self):
        self.version_path.write_bytes(b"{not-json")
        info = self._info_with_env(AICA_VERSION="1.0.2", AICA_BUILD="fallback")
        self.assertEqual(info["version"], "1.0.2")
        self.assertEqual(info["build"], "fallback")


if __name__ == "__main__":
    unittest.main()
