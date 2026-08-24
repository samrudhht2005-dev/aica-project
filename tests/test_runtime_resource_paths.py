"""
Regression: packaged desktop must resolve frontend/templates and frontend/static
from PyInstaller _internal, not from an inherited launcher AICA_ROOT.

Simulates production layout:
  %LOCALAPPDATA%\\AICA\\
    AICA.Engine.exe
    _internal\\frontend\\templates\\login.html
    _internal\\frontend\\static\\
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _make_production_layout(tmp: Path) -> tuple[Path, Path, Path]:
    """Return (install_dir, internal_dir, fake_exe_path)."""
    install = tmp / "AICA"
    internal = install / "_internal"
    templates = internal / "frontend" / "templates"
    static = internal / "frontend" / "static"
    templates.mkdir(parents=True)
    static.mkdir(parents=True)

    for name in ("login.html", "executive_dashboard.html"):
        src = ROOT / "frontend" / "templates" / name
        if src.is_file():
            shutil.copy2(src, templates / name)
        else:
            (templates / name).write_text(f"<!-- test stub {name} -->", encoding="utf-8")

    (static / "placeholder.txt").write_text("static", encoding="utf-8")

    fake_exe = install / "AICA.Engine.exe"
    fake_exe.write_bytes(b"")
    return install, internal, fake_exe


class RuntimeResourcePathTests(unittest.TestCase):
    def test_dev_mode_uses_repo_frontend(self):
        import backend.runtime_paths as rp

        self.assertFalse(rp.is_frozen())
        root = rp.project_root()
        self.assertTrue((root / "frontend" / "templates" / "login.html").is_file())
        self.assertTrue((root / "frontend" / "static").is_dir())
        self.assertEqual(rp.templates_dir(), root / "frontend" / "templates")
        self.assertEqual(rp.static_dir(), root / "frontend" / "static")

    def test_packaged_layout_ignores_invalid_aica_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            install, internal, fake_exe = _make_production_layout(Path(tmp))
            os.environ["AICA_ROOT"] = str(install)

            import backend.runtime_paths as rp

            rp = importlib.reload(rp)
            with unittest.mock.patch.object(sys, "frozen", True, create=True), unittest.mock.patch.object(
                sys, "executable", str(fake_exe)
            ), unittest.mock.patch.object(sys, "_MEIPASS", str(internal), create=True):
                root = rp.project_root()
                self.assertEqual(root.resolve(), internal.resolve())
                self.assertTrue((rp.templates_dir() / "login.html").is_file())
                self.assertTrue((rp.templates_dir() / "executive_dashboard.html").is_file())
                self.assertTrue(rp.static_dir().is_dir())

            if "AICA_ROOT" in os.environ and os.environ["AICA_ROOT"] == str(install):
                del os.environ["AICA_ROOT"]

    def test_packaged_layout_via_internal_only(self):
        """When _MEIPASS is unset, exe_dir/_internal must still resolve."""
        with tempfile.TemporaryDirectory() as tmp:
            install, internal, fake_exe = _make_production_layout(Path(tmp))
            os.environ.pop("AICA_ROOT", None)

            import backend.runtime_paths as rp

            rp = importlib.reload(rp)
            meipass_backup = getattr(sys, "_MEIPASS", None)
            if hasattr(sys, "_MEIPASS"):
                delattr(sys, "_MEIPASS")
            try:
                with unittest.mock.patch.object(sys, "frozen", True, create=True), unittest.mock.patch.object(
                    sys, "executable", str(fake_exe)
                ):
                    root = rp.project_root()
                    self.assertEqual(root.resolve(), internal.resolve())
            finally:
                if meipass_backup is not None:
                    sys._MEIPASS = meipass_backup


class PackagedLoginSmokeTests(unittest.TestCase):
    def test_health_and_login_not_500_with_packaged_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            install, internal, fake_exe = _make_production_layout(Path(tmp))
            db_path = Path(tmp) / "smoke.db"
            url = "sqlite:///" + db_path.resolve().as_posix()
            script = textwrap.dedent(
                f"""
                import os, sys
                from pathlib import Path

                os.environ["DATABASE_URL"] = {url!r}
                os.environ["AICA_DESKTOP"] = "1"
                os.environ["AICA_DB_BACKEND"] = "sqlite"
                # Wrong inherited launcher root (no frontend/ here)
                os.environ["AICA_ROOT"] = {str(install)!r}
                sys.path.insert(0, {str(ROOT)!r})
                sys.frozen = True
                sys.executable = {str(fake_exe)!r}
                sys._MEIPASS = {str(internal)!r}

                from backend.runtime_paths import project_root, templates_dir, static_dir
                root = project_root()
                assert root.resolve() == Path({str(internal)!r}).resolve(), root
                assert (templates_dir() / "login.html").is_file(), templates_dir()
                assert (templates_dir() / "executive_dashboard.html").is_file(), templates_dir()
                assert static_dir().is_dir(), static_dir()

                from fastapi.testclient import TestClient
                import backend.main as mainmod
                client = TestClient(mainmod.app)
                health = client.get("/health")
                assert health.status_code == 200, health.text
                login = client.get("/login")
                assert login.status_code == 200, login.text[:500]
                print("SMOKE_OK root=", root)
                """
            )
            proc = subprocess.run(
                [sys.executable, "-c", script],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, "DATABASE_URL": url, "AICA_DESKTOP": "1"},
            )
            if proc.returncode != 0:
                self.fail(
                    f"subprocess failed ({proc.returncode})\n"
                    f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
                )
            self.assertIn("SMOKE_OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
