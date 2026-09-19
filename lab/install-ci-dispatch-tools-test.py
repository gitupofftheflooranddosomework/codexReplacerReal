#!/usr/bin/env python3
from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parent
INSTALLER = ROOT / "install-ci-dispatch-tools.sh"


class FabricDispatchInstallerTests(unittest.TestCase):
    def test_installer_is_atomic_and_preserves_legacy_default(self):
        text = INSTALLER.read_text()
        self.assertIn("mktemp", text)
        self.assertIn("install -m 0700", text)
        self.assertIn("mv -f", text)
        self.assertIn("codex-ci-dispatch.py", text)
        self.assertIn("serverworkerfabric-headless-manager.py", text)
        self.assertNotIn('install_atomic "$SCRIPT_DIR/codex-ci-headless.py"', text)
        self.assertNotIn("rm -f "$TARGET_BIN/codex-ci-headless"", text)
        self.assertIn("legacy local manager remains", text)

    def test_temp_install_produces_executable_exact_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "bin"
            env = dict(os.environ, CODEX_CI_BIN=str(target))
            result = subprocess.run(
                ["bash", str(INSTALLER)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CODEX_CI_FABRIC_TOOLS_INSTALL=PASS", result.stdout)
            expected = {
                "codex-ci-dispatch": ROOT / "codex-ci-dispatch.py",
                "serverworkerfabric-headless-manager": (
                    ROOT / "serverworkerfabric-headless-manager.py"
                ),
            }
            for name, source in expected.items():
                installed = target / name
                self.assertTrue(installed.is_file())
                self.assertEqual(installed.read_bytes(), source.read_bytes())
                self.assertEqual(stat.S_IMODE(installed.stat().st_mode), 0o700)

    def test_existing_legacy_manager_is_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = pathlib.Path(tmp) / "bin"
            target.mkdir()
            legacy = target / "codex-ci-headless"
            legacy.write_text("legacy-rollback-manager\n")
            legacy.chmod(0o700)
            before = legacy.read_bytes()
            env = dict(os.environ, CODEX_CI_BIN=str(target))
            result = subprocess.run(
                ["bash", str(INSTALLER)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(legacy.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(legacy.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main(verbosity=2)
