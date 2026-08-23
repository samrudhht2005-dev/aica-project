"""
Phase 4 update download tests — no large GitHub downloads.

Run: python desktop/scripts/test_update_download.py
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from desktop.launcher import update_download as ud  # noqa: E402
from desktop.launcher import update_checker as uc  # noqa: E402

VALID_SHA_PLACEHOLDER = "a" * 64


def _reset_download_module() -> None:
    ud._download_thread = None
    ud._download_state = None


def _installer_meta(*, url: str, version: str = "1.0.3", size: int, sha256: str) -> dict:
    return {
        "filename": f"AICA_Setup_{version}.exe",
        "url": url,
        "sha256": sha256,
        "size_bytes": size,
    }


def _update_state(version: str = "1.0.3", installer: dict | None = None) -> uc.UpdateState:
    if installer is None:
        installer = _installer_meta(
            url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
            version=version,
            size=128,
            sha256=hashlib.sha256(b"x" * 128).hexdigest(),
        )
    return uc.UpdateState(
        status="update_available",
        installed_version="1.0.2",
        update_available=True,
        available_version=version,
        release_notes="Test",
        installer=installer,
    )


class TestValidation(unittest.TestCase):
    def test_reject_http_url(self):
        err = ud._validate_installer_metadata(
            _installer_meta(url="http://github.com/x/y.exe", version="1.0.3", size=1, sha256=VALID_SHA_PLACEHOLDER),
            "1.0.3",
        )
        self.assertIn("HTTPS", err or "")

    def test_reject_invalid_host(self):
        err = ud._validate_installer_metadata(
            _installer_meta(url="https://evil.example/x.exe", version="1.0.3", size=1, sha256=VALID_SHA_PLACEHOLDER),
            "1.0.3",
        )
        self.assertIn("allowlist", err or "")

    def test_reject_bad_sha(self):
        err = ud._validate_installer_metadata(
            _installer_meta(url="https://github.com/x/y.exe", version="1.0.3", size=1, sha256="bad"),
            "1.0.3",
        )
        self.assertIsNotNone(err)


class TestShaAndSize(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._td = Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_sha256_streaming(self):
        data = b"hello-aica" * 10000
        path = self._td / "blob.bin"
        path.write_bytes(data)
        expected = hashlib.sha256(data).hexdigest()
        self.assertEqual(ud._sha256_file(path), expected)

    def test_file_matches(self):
        data = b"abc123"
        path = self._td / "f.bin"
        path.write_bytes(data)
        sha = hashlib.sha256(data).hexdigest()
        self.assertTrue(ud._file_matches(path, size_bytes=len(data), sha256=sha))
        self.assertFalse(ud._file_matches(path, size_bytes=len(data) + 1, sha256=sha))


class TestDownloadFlow(unittest.TestCase):
    def setUp(self):
        _reset_download_module()
        self._tmpdir = tempfile.TemporaryDirectory()
        self._td = Path(self._tmpdir.name)
        os.environ["TEMP"] = self._tmpdir.name

    def tearDown(self):
        _reset_download_module()
        os.environ.pop("TEMP", None)
        self._tmpdir.cleanup()

    def _serve(self, payload: bytes) -> str:
        sha = hashlib.sha256(payload).hexdigest()
        state_holder = {"payload": payload}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data = state_holder["payload"]
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_a):
                return

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{port}/AICA_Setup_1.0.3.exe"
        return url, sha, len(payload), server

    @mock.patch.object(ud, "load_update_config")
    @mock.patch.object(uc, "get_update_state")
    def test_valid_download_success(self, mock_state, mock_cfg):
        payload = b"P" * 256
        url, sha, size, server = self._serve(payload)
        mock_cfg.return_value = (mock.Mock(installed_version="1.0.2"), None)
        mock_state.return_value = _update_state(
            installer=_installer_meta(
                url=url.replace("http://", "https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/"),
                size=size,
                sha256=sha,
            )
        )

        with mock.patch.object(ud, "_download_to_part") as dl_part:
            staging = ud.staging_dir("1.0.3")
            part = staging / "AICA_Setup_1.0.3.exe.part"
            final = staging / "AICA_Setup_1.0.3.exe"
            part.write_bytes(payload)

            def _real_download(u, p, **kw):
                p.write_bytes(payload)
                return None

            dl_part.side_effect = _real_download

            with mock.patch.object(ud, "_authoritative_installer") as auth:
                auth.return_value = (
                    "1.0.3",
                    _installer_meta(
                        url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                        size=size,
                        sha256=sha,
                    ),
                    "",
                )
                ud._download_worker()

        server.shutdown()
        state = ud.get_update_download_status_dict()
        self.assertEqual(state["status"], "ready")
        self.assertTrue(state["sha_verified"])
        self.assertTrue(final.is_file())

    @mock.patch.object(ud, "_authoritative_installer")
    def test_sha_mismatch(self, auth):
        payload = b"data"
        sha = hashlib.sha256(payload).hexdigest()
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=len(payload),
                sha256="b" * 64,
            ),
            "",
        )
        staging = ud.staging_dir("1.0.3")
        part = staging / "AICA_Setup_1.0.3.exe.part"
        part.write_bytes(payload)

        with mock.patch.object(ud, "_download_to_part", return_value=None):
            ud._download_worker()

        state = ud.get_update_download_status_dict()
        self.assertEqual(state["status"], "error")
        self.assertFalse((staging / "AICA_Setup_1.0.3.exe").exists())

    @mock.patch.object(ud, "_authoritative_installer")
    def test_size_mismatch(self, auth):
        payload = b"12345"
        sha = hashlib.sha256(payload).hexdigest()
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=len(payload) + 10,
                sha256=sha,
            ),
            "",
        )
        staging = ud.staging_dir("1.0.3")
        part = staging / "AICA_Setup_1.0.3.exe.part"
        part.write_bytes(payload)

        with mock.patch.object(ud, "_download_to_part", return_value=None):
            ud._download_worker()

        self.assertEqual(ud.get_update_download_status_dict()["status"], "error")

    @mock.patch.object(ud, "_authoritative_installer")
    def test_reuse_verified_installer(self, auth):
        payload = b"reuse-me"
        sha = hashlib.sha256(payload).hexdigest()
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=len(payload),
                sha256=sha,
            ),
            "",
        )
        final = ud.staging_dir("1.0.3") / "AICA_Setup_1.0.3.exe"
        final.write_bytes(payload)

        with mock.patch.object(ud, "_download_to_part") as dl:
            ud._download_worker()
            dl.assert_not_called()

        self.assertEqual(ud.get_update_download_status_dict()["status"], "ready")

    @mock.patch.object(ud, "_authoritative_installer")
    def test_corrupt_existing_redownload(self, auth):
        payload = b"good"
        sha = hashlib.sha256(payload).hexdigest()
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=len(payload),
                sha256=sha,
            ),
            "",
        )
        final = ud.staging_dir("1.0.3") / "AICA_Setup_1.0.3.exe"
        final.write_bytes(b"bad")

        def _write_part(url, part_path, **kw):
            part_path.write_bytes(payload)
            return None

        with mock.patch.object(ud, "_download_to_part", side_effect=_write_part):
            ud._download_worker()

        self.assertEqual(ud.get_update_download_status_dict()["status"], "ready")
        self.assertEqual(final.read_bytes(), payload)

    @mock.patch.object(ud, "_authoritative_installer")
    def test_http_and_timeout_failure(self, auth):
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=10,
                sha256=VALID_SHA_PLACEHOLDER,
            ),
            "",
        )
        with mock.patch.object(ud, "_download_to_part", return_value="HTTP 404"):
            ud._download_worker()
        state = ud.get_update_download_status_dict()
        self.assertEqual(state["status"], "error")
        self.assertIn("Download failed", state.get("error") or "")

        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=10,
                sha256=VALID_SHA_PLACEHOLDER,
            ),
            "",
        )
        with mock.patch.object(ud, "_download_to_part", return_value="timeout"):
            ud._download_worker()
        self.assertEqual(ud.get_update_download_status_dict()["status"], "error")

    @mock.patch.object(ud, "_authoritative_installer")
    def test_insufficient_disk(self, auth):
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=10_000_000_000,
                sha256=VALID_SHA_PLACEHOLDER,
            ),
            "",
        )
        with mock.patch.object(ud, "_disk_space_error", return_value="insufficient_storage"):
            ud._download_worker()
        state = ud.get_update_download_status_dict()
        self.assertEqual(state["status"], "error")
        self.assertIn("disk", (state.get("error") or "").lower())

    @mock.patch.object(ud, "_authoritative_installer")
    def test_duplicate_download_prevention(self, auth):
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=4,
                sha256=VALID_SHA_PLACEHOLDER,
            ),
            "",
        )
        hold = threading.Event()
        release = threading.Event()

        def worker():
            hold.set()
            release.wait(timeout=2)

        ud._set_download_state(ud.DownloadState(status="downloading", version="1.0.3"))
        ud._download_thread = threading.Thread(target=worker, daemon=True)
        ud._download_thread.start()
        try:
            self.assertTrue(hold.wait(timeout=2))
            result = ud.start_update_download()
            self.assertFalse(result.get("ok", True))
            self.assertTrue(result.get("already_in_progress"))
        finally:
            release.set()
            ud._download_thread.join(timeout=2)
            ud._download_thread = None

    @mock.patch.object(ud, "_authoritative_installer")
    def test_start_returns_immediately(self, auth):
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=4,
                sha256=VALID_SHA_PLACEHOLDER,
            ),
            "",
        )
        started = threading.Event()

        def slow_worker():
            started.wait(timeout=2)
            ud._set_download_state(ud.DownloadState(status="idle"))

        with mock.patch.object(ud, "_download_worker", side_effect=slow_worker):
            t0 = time.perf_counter()
            result = ud.start_update_download()
            elapsed = time.perf_counter() - t0
            self.assertTrue(result.get("ok"))
            self.assertLess(elapsed, 0.3)
        started.set()

    def test_status_json_safe(self):
        ud._set_download_state(
            ud.DownloadState(
                status="downloading",
                version="1.0.3",
                bytes_downloaded=50,
                total_bytes=100,
                progress_percent=50.0,
            )
        )
        payload = ud.get_update_download_status_dict()
        json.dumps(payload)
        self.assertNotIn("path", json.dumps(payload).lower())

    @mock.patch.object(ud, "_authoritative_installer")
    def test_part_file_removed_on_failure(self, auth):
        auth.return_value = (
            "1.0.3",
            _installer_meta(
                url="https://github.com/samrudhht2005-dev/aica-project/releases/download/v1.0.3/AICA_Setup_1.0.3.exe",
                size=5,
                sha256=VALID_SHA_PLACEHOLDER,
            ),
            "",
        )
        staging = ud.staging_dir("1.0.3")
        part = staging / "AICA_Setup_1.0.3.exe.part"
        part.write_bytes(b"12345")
        with mock.patch.object(ud, "_download_to_part", return_value="connection_failed"):
            ud._download_worker()
        self.assertFalse(part.exists())


class TestChunkedDownload(unittest.TestCase):
    @mock.patch("urllib.request.urlopen")
    def test_reads_in_chunks_not_memory(self, mock_open):
        chunks = [b"a" * 1024, b"b" * 512, b""]

        class Resp:
            headers = {"Content-Length": "1536"}

            def read(self, n):
                return chunks.pop(0)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        mock_open.return_value = Resp()
        part = Path(tempfile.mkdtemp()) / "x.part"
        err = ud._download_to_part(
            "https://github.com/x/y.exe",
            part,
            total_bytes=1536,
            version="1.0.3",
            user_agent="test",
        )
        self.assertIsNone(err)
        self.assertEqual(part.stat().st_size, 1536)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
