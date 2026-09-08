#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import subprocess
import sys
import tarfile
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("codex_ci_worker_rpc", ROOT / "codex-ci-worker-rpc.py")
assert SPEC and SPEC.loader
rpc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rpc)


class FakeStdin:
    def __init__(self, stream):
        self.buffer = stream


def archive(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def git(repo: pathlib.Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def run_import(state: pathlib.Path, iid: str, files: dict[str, bytes]) -> pathlib.Path:
    rpc.STATE_DIR = state / "state"
    rpc.CLAIM_LOCK = rpc.STATE_DIR / "claim.lock"
    rpc.EXCLUSIVE = rpc.STATE_DIR / "exclusive.lock"
    rpc.SCHEDULER = rpc.STATE_DIR / "scheduler.lock"
    rpc.CI_STATE = state / "ci-state"
    rpc.WORK_ROOT = state / "work"
    rpc.STATE_DIR.mkdir(parents=True, exist_ok=True)
    rpc.EXCLUSIVE.write_text(json.dumps({"leaseId": iid, "source": "github-actions", "owner": "test"}))
    original_stdin = sys.stdin
    try:
        with tempfile.TemporaryFile() as stream:
            stream.write(archive(files))
            stream.seek(0)
            sys.stdin = FakeStdin(stream)
            assert rpc.action_import(["import", iid]) == 0
    finally:
        sys.stdin = original_stdin
    return rpc.WORK_ROOT / iid / "repo"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        state = pathlib.Path(td)
        empty = run_import(state, "a" * 32, {})
        assert git(empty, "rev-parse", "--verify", "HEAD")
        assert git(empty, "ls-tree", "-r", "--name-only", "HEAD") == ""
        assert git(empty, "status", "--porcelain") == ""

        nonempty = run_import(state, "b" * 32, {"proof.txt": b"headless import works\n"})
        assert git(nonempty, "rev-parse", "--verify", "HEAD")
        assert git(nonempty, "show", "HEAD:proof.txt") == "headless import works"
        assert git(nonempty, "status", "--porcelain") == ""
    print("codex_ci_worker_rpc_test=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
