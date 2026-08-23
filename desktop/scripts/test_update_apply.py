"""
Phase 5 update-apply tests — no real installer execution.

Run: python desktop/scripts/test_update_apply.py
"""
from __future__ import annotations

import hashlib
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

from desktop.updater import updater_validate as uv  # noqa: E402
from desktop.updater import updater_apply as ua  # noqa: E402
from desktop.launcher import update_apply as la  # noqa: E402
from desktop.launcher import update_download as ud  # noqa: E402


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TempStaging:
    def __init__(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.temp = self.root / "tmp"
        self.temp.mkdir()
        self.local = self.root / "Local" / "AICA"
        self.local.mkdir(parents=True)
        (self.local / "AICA.exe").write_bytes(b"fake-aica")
        (self.local / "version.json").write_text(
            json.dumps({"version": "1.0.2"}), encoding="utf-8"
        )

    def cleanup(self):
        self._td.cleanup()

    def staging(self, version: str) -> Path:
        path = self.temp / "AICA" / "updates" / version
        path.mkdir(parents=True, exist_ok=True)
        return path


class TestHandoffValidation(unittest.TestCase):
    def setUp(self):
        self.env = TempStaging()
        self._old_temp = os.environ.get("TEMP")
        self._old_local = os.environ.get("LOCALAPPDATA")
        os.environ["TEMP"] = str(self.env.temp)
        os.environ["LOCALAPPDATA"] = str(self.env.root / "Local")

    def tearDown(self):
        if self._old_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = self._old_temp
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        self.env.cleanup()

    def _write_installer(self, version: str, payload: bytes) -> tuple[Path, str]:
        staging = self.env.staging(version)
        path = staging / f"AICA_Setup_{version}.exe"
        path.write_bytes(payload)
        return path, _sha(payload)

    def _handoff(self, version: str, sha256: str, **overrides) -> Path:
        staging = self.env.staging(version)
        kwargs = dict(
            staging_dir=staging,
            target_version=version,
            sha256=sha256,
            aica_pid=os.getpid(),
            engine_pid=None,
            install_dir=self.env.local,
            restart_exe=self.env.local / "AICA.exe",
            dry_run=True,
        )
        kwargs.update(overrides)
        return uv.write_handoff_file(**kwargs)

    def test_valid_handoff(self):
        payload = b"installer-bytes"
        _, sha = self._write_installer("1.0.3", payload)
        path = self._handoff("1.0.3", sha)
        handoff, err = uv.load_and_validate_handoff(path)
        self.assertIsNone(err)
        self.assertIsNotNone(handoff)
        self.assertEqual(handoff.target_version, "1.0.3")

    def test_missing_installer_rejected(self):
        sha = "a" * 64
        path = self._handoff("1.0.3", sha)
        handoff, err = uv.load_and_validate_handoff(path)
        self.assertIsNone(handoff)
        self.assertIn("missing", (err or "").lower())

    def test_outside_staging_rejected(self):
        payload = b"x"
        _, sha = self._write_installer("1.0.3", payload)
        outside = self.env.root / "evil" / "handoff.json"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("{}", encoding="utf-8")
        err = uv.validate_handoff_path(outside)
        self.assertIsNotNone(err)

    def test_bad_filename_rejected(self):
        staging = self.env.staging("1.0.3")
        bad = staging / "evil.exe"
        bad.write_bytes(b"x")
        path = self._handoff("1.0.3", _sha(b"x"))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["installer_filename"] = "evil.exe"
        path.write_text(json.dumps(data), encoding="utf-8")
        handoff, err = uv.load_and_validate_handoff(path)
        self.assertIsNone(handoff)
        self.assertIn("filename", (err or "").lower())

    def test_sha_mismatch_rejected(self):
        payload = b"good"
        self._write_installer("1.0.3", payload)
        path = self._handoff("1.0.3", "b" * 64)
        handoff, err = uv.load_and_validate_handoff(path)
        self.assertIsNotNone(handoff)
        rehash_err = uv.reverify_installer(handoff)
        self.assertIsNotNone(rehash_err)

    def test_installer_command_args(self):
        payload = b"cmd"
        installer, _ = self._write_installer("1.0.3", payload)
        cmd = uv.build_installer_command(installer, self.env.local)
        self.assertEqual(cmd[0], str(installer))
        self.assertIn("/VERYSILENT", cmd)
        self.assertIn("/SUPPRESSMSGBOXES", cmd)
        self.assertIn("/NORESTART", cmd)
        self.assertIn("/FORCECLOSEAPPLICATIONS", cmd)
        self.assertTrue(any(a.startswith("/DIR=") for a in cmd))

    def test_user_data_paths_untouched_by_validate(self):
        # Ensure validation helpers never delete AppData trees.
        appdata = self.env.root / "Roaming" / "AICA"
        appdata.mkdir(parents=True)
        marker = appdata / "config.env"
        marker.write_text("KEEP=1", encoding="utf-8")
        webview = self.env.local / "webview"
        webview.mkdir(parents=True)
        (webview / "profile.bin").write_bytes(b"keep")
        payload = b"ok"
        _, sha = self._write_installer("1.0.3", payload)
        path = self._handoff("1.0.3", sha)
        handoff, err = uv.load_and_validate_handoff(path)
        self.assertIsNone(err)
        uv.reverify_installer(handoff)
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), "KEEP=1")
        self.assertTrue((webview / "profile.bin").is_file())


class TestApplyFlow(unittest.TestCase):
    def setUp(self):
        self.env = TempStaging()
        self._old_temp = os.environ.get("TEMP")
        self._old_local = os.environ.get("LOCALAPPDATA")
        os.environ["TEMP"] = str(self.env.temp)
        os.environ["LOCALAPPDATA"] = str(self.env.root / "Local")
        la._set_apply_state(status="idle", version=None, error=None, updater_started=False)

    def tearDown(self):
        if self._old_temp is None:
            os.environ.pop("TEMP", None)
        else:
            os.environ["TEMP"] = self._old_temp
        if self._old_local is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._old_local
        self.env.cleanup()
        la._set_apply_state(status="idle", version=None, error=None, updater_started=False)

    def _ready_payload(self, version="1.0.3"):
        payload = b"READY" * 20
        staging = self.env.staging(version)
        path = staging / f"AICA_Setup_{version}.exe"
        path.write_bytes(payload)
        return {
            "version": version,
            "sha256": _sha(payload),
            "size_bytes": len(payload),
            "filename": f"AICA_Setup_{version}.exe",
            "staging_dir": str(staging),
            "installer_path": str(path),
        }

    def test_parent_pid_wait(self):
        # Already-exited PID should return quickly.
        self.assertTrue(ua._wait_for_pid(99999991, timeout_s=1.0, label="aica"))

    def test_timeout_behavior(self):
        # Current process is still running — wait should time out.
        ok = ua._wait_for_pid(os.getpid(), timeout_s=0.4, label="aica")
        self.assertFalse(ok)

    @mock.patch.object(ua, "subprocess")
    def test_installer_nonzero_exit(self, mock_sp):
        payload = b"fail"
        installer = self.env.staging("1.0.3") / "AICA_Setup_1.0.3.exe"
        installer.write_bytes(payload)
        handoff = uv.Handoff(
            target_version="1.0.3",
            sha256=_sha(payload),
            installer_filename="AICA_Setup_1.0.3.exe",
            installer_path=installer,
            aica_pid=os.getpid(),
            engine_pid=None,
            install_dir=self.env.local,
            restart_exe=self.env.local / "AICA.exe",
            dry_run=False,
            handoff_path=self.env.staging("1.0.3") / "handoff.json",
        )
        mock_sp.run.return_value = mock.Mock(returncode=1)
        mock_sp.CREATE_NO_WINDOW = 0
        code = ua.run_installer(handoff)
        self.assertEqual(code, 1)

    @mock.patch.object(ua, "subprocess")
    def test_successful_installer_and_restart(self, mock_sp):
        payload = b"ok"
        installer = self.env.staging("1.0.3") / "AICA_Setup_1.0.3.exe"
        installer.write_bytes(payload)
        (self.env.local / "version.json").write_text(
            json.dumps({"version": "1.0.3"}), encoding="utf-8"
        )
        handoff = uv.Handoff(
            target_version="1.0.3",
            sha256=_sha(payload),
            installer_filename="AICA_Setup_1.0.3.exe",
            installer_path=installer,
            aica_pid=99999992,
            engine_pid=None,
            install_dir=self.env.local,
            restart_exe=self.env.local / "AICA.exe",
            dry_run=False,
            handoff_path=self.env.staging("1.0.3") / "handoff.json",
        )
        mock_sp.run.return_value = mock.Mock(returncode=0)
        mock_sp.Popen = mock.Mock()
        mock_sp.CREATE_NO_WINDOW = 0
        mock_sp.DETACHED_PROCESS = 0
        mock_sp.CREATE_NEW_PROCESS_GROUP = 0

        self.assertEqual(ua.run_installer(handoff), 0)
        self.assertIsNone(ua.post_install_validate(handoff))
        self.assertIsNone(ua.restart_aica(handoff))
        mock_sp.Popen.assert_called_once()

    def test_post_install_missing_exe(self):
        (self.env.local / "AICA.exe").unlink()
        handoff = uv.Handoff(
            target_version="1.0.3",
            sha256="a" * 64,
            installer_filename="AICA_Setup_1.0.3.exe",
            installer_path=self.env.staging("1.0.3") / "AICA_Setup_1.0.3.exe",
            aica_pid=1,
            engine_pid=None,
            install_dir=self.env.local,
            restart_exe=self.env.local / "AICA.exe",
            dry_run=False,
            handoff_path=self.env.staging("1.0.3") / "handoff.json",
        )
        self.assertEqual(ua.post_install_validate(handoff), "missing_aica_exe")

    def test_wrong_installed_version(self):
        (self.env.local / "version.json").write_text(
            json.dumps({"version": "1.0.2"}), encoding="utf-8"
        )
        handoff = uv.Handoff(
            target_version="1.0.3",
            sha256="a" * 64,
            installer_filename="AICA_Setup_1.0.3.exe",
            installer_path=self.env.staging("1.0.3") / "AICA_Setup_1.0.3.exe",
            aica_pid=1,
            engine_pid=None,
            install_dir=self.env.local,
            restart_exe=self.env.local / "AICA.exe",
            dry_run=False,
            handoff_path=self.env.staging("1.0.3") / "handoff.json",
        )
        self.assertEqual(ua.post_install_validate(handoff), "version_mismatch")

    def test_restart_only_after_validation(self):
        with mock.patch.object(ua, "restart_aica") as restart:
            handoff_path = self.env.staging("1.0.3") / "handoff.json"
            # Missing installer → fail before restart
            handoff_path.write_text("{}", encoding="utf-8")
            code = ua.apply_from_handoff(handoff_path, wait_timeout_s=0.1)
            self.assertNotEqual(code, 0)
            restart.assert_not_called()

    @mock.patch.object(la, "resolve_updater_command", return_value=None)
    @mock.patch.object(la, "_ready_installer_info")
    def test_updater_launch_failure_keeps_aica(self, ready, _cmd):
        ready.return_value = (self._ready_payload(), None)
        shutdown = mock.Mock()
        la.register_graceful_shutdown(shutdown)
        result = la.apply_staged_update()
        self.assertFalse(result["ok"])
        self.assertFalse(result["updater_started"])
        shutdown.assert_not_called()

    @mock.patch.object(la, "_schedule_shutdown")
    @mock.patch.object(la, "subprocess")
    @mock.patch.object(la, "resolve_updater_command")
    @mock.patch.object(la, "_ready_installer_info")
    def test_valid_updater_handoff(self, ready, resolve_cmd, mock_sp, sched):
        ready.return_value = (self._ready_payload(), None)
        resolve_cmd.return_value = [sys.executable, "-m", "desktop.updater.main"]
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.pid = 4242
        mock_sp.Popen.return_value = proc
        mock_sp.DETACHED_PROCESS = 0
        mock_sp.CREATE_NEW_PROCESS_GROUP = 0
        mock_sp.CREATE_NO_WINDOW = 0
        mock_sp.DEVNULL = object()

        result = la.apply_staged_update(dry_run=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["updater_started"])
        self.assertNotIn("path", json.dumps(result).lower())
        mock_sp.Popen.assert_called_once()
        # dry_run should not schedule shutdown
        sched.assert_not_called()

    @mock.patch.object(la, "resolve_updater_command")
    @mock.patch.object(la, "_ready_installer_info")
    def test_duplicate_apply_prevention(self, ready, resolve_cmd):
        ready.return_value = (self._ready_payload(), None)
        resolve_cmd.return_value = [sys.executable]
        la._set_apply_state(status="applying", version="1.0.3", error=None, updater_started=True)
        result = la.apply_staged_update()
        self.assertFalse(result["ok"])
        self.assertTrue(result.get("already_in_progress"))

    def test_apply_status_json_safe(self):
        la._set_apply_state(status="applying", version="1.0.3", error=None, updater_started=True)
        payload = la.get_apply_status_dict()
        text = json.dumps(payload)
        self.assertNotIn("\\\\", text)  # no windows path fragments expected
        self.assertNotIn("TEMP", text)
        self.assertIn("status", payload)

    def test_dry_run_apply_from_handoff(self):
        payload = b"dry"
        installer = self.env.staging("1.0.3") / "AICA_Setup_1.0.3.exe"
        installer.write_bytes(payload)
        path = uv.write_handoff_file(
            staging_dir=self.env.staging("1.0.3"),
            target_version="1.0.3",
            sha256=_sha(payload),
            aica_pid=99999993,
            engine_pid=None,
            install_dir=self.env.local,
            restart_exe=self.env.local / "AICA.exe",
            dry_run=True,
        )
        code = ua.apply_from_handoff(path, wait_timeout_s=0.2, installer_timeout_s=1)
        self.assertEqual(code, 0)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
