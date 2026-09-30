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
runner = load("headless_job_runner_under_test", "headless-job-runner.py")

class RevisionContractTests(unittest.TestCase):
    def make_source_repo(self):
        td = tempfile.TemporaryDirectory()
        root = pathlib.Path(td.name).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        (root / "file.txt").write_text("one\n")
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "one"], cwd=root, check=True)
        subprocess.run(["git", "tag", "v1"], cwd=root, check=True)
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        return td, root, head

    def test_generic_branch_and_tag_revision_resolve_to_checked_out_sha(self):
        source_td, source, expected = self.make_source_repo()
        self.addCleanup(source_td.cleanup)
        for revision in ("main", "v1", expected):
            with self.subTest(revision=revision):
                job_td = tempfile.TemporaryDirectory()
                self.addCleanup(job_td.cleanup)
                job_dir = pathlib.Path(job_td.name).resolve()
                payload = {"repoUrl": source.as_uri(), "revision": revision}
                workspace = runner.prepare_workspace(payload, job_dir)
                self.assertEqual(runner.resolved_workspace_revision(payload, workspace), expected)

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

    def test_restricted_key_rpc_is_sent_directly(self):
        self.assertEqual(dispatch.rpc("codex-ci probe"), "codex-ci probe")
        self.assertEqual(
            dispatch.rpc("codex-ci claim abc123"),
            "codex-ci claim abc123",
        )
        self.assertNotIn("SSH_ORIGINAL_COMMAND", dispatch.rpc("codex-ci probe"))

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
