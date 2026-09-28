#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

dispatch = load("codex_ci_dispatch_under_test", "codex-ci-dispatch.py")

class RevisionContractTests(unittest.TestCase):
    def make_repo(self):
        td = tempfile.TemporaryDirectory()
        root = pathlib.Path(td.name)
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        (root / "file.txt").write_text("one\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=root, check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        return td, root, head

    def test_workspace_revision_match_passes(self):
        td, root, head = self.make_repo()
        self.addCleanup(td.cleanup)
        self.assertEqual(dispatch.verify_workspace_revision(root, head), head)

    def test_workspace_revision_mismatch_fails(self):
        td, root, _head = self.make_repo()
        self.addCleanup(td.cleanup)
        with self.assertRaisesRegex(RuntimeError, "workspace revision mismatch"):
            dispatch.verify_workspace_revision(root, "0" * 40)

    def test_empty_expected_revision_is_backward_compatible(self):
        td, root, _head = self.make_repo()
        self.addCleanup(td.cleanup)
        self.assertIsNone(dispatch.verify_workspace_revision(root, ""))

if __name__ == "__main__":
    unittest.main()
