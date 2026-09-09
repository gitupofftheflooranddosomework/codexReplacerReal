#!/usr/bin/env python3
"""Run one queued MCP heavy job through the disposable headless KVM dispatcher."""
from __future__ import annotations

import json
import os
import pathlib
import shlex
import signal
import subprocess
import sys
import threading
import time

DISPATCH = os.environ.get("CODEX_CI_DISPATCH", "/home/mark/.local/bin/codex-ci-dispatch")
WAIT_SECONDS = max(60, int(os.environ.get("CODEX_VM_JOB_WAIT_SECONDS", "86400")))


def atomic_json(path: pathlib.Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    tmp.replace(path)


def atomic_text(path: pathlib.Path, value: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(value)
    tmp.replace(path)


def normalize_cwd(value: str) -> str:
    raw = str(value or "/workspace").strip()
    if raw in ("", ".", "/workspace"):
        return "."
    if raw.startswith("/workspace/"):
        raw = raw[len("/workspace/"):]
    p = pathlib.PurePosixPath(raw)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"cwd must be inside /workspace: {value}")
    return str(p) or "."


def resources(job_class: str) -> tuple[int, int, int]:
    # Current memory is the resident/boot target; max memory is balloon headroom.
    # Keep small independent jobs cheap so aggregate concurrency can rise.
    table = {
        "io": (1024, 2048, 1),
        "cpu": (1536, 3072, 2),
        "test": (2048, 4096, 2),
        "build": (3072, 5120, 2),
        "browser": (2048, 4096, 2),
    }
    return table.get(str(job_class or "cpu"), table["cpu"])


def prepare_workspace(payload: dict, job_dir: pathlib.Path) -> pathlib.Path:
    workspace = job_dir / "workspace"
    repo = str(payload.get("repoUrl") or "").strip()
    revision = str(payload.get("revision") or "").strip()
    if workspace.exists():
        subprocess.run(["rm", "-rf", str(workspace)], check=True)
    if repo:
        cp = subprocess.run(
            ["git", "clone", "--no-tags", repo, str(workspace)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if cp.returncode:
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "git clone failed")
        if revision:
            cp = subprocess.run(
                ["git", "fetch", "--no-tags", "origin", revision], cwd=workspace,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            )
            if cp.returncode:
                raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "git fetch failed")
            subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=workspace, check=True)
    else:
        workspace.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.name", "CodexHeadlessScheduler"], cwd=workspace, check=True)
        subprocess.run(["git", "config", "user.email", "codex-headless@localhost"], cwd=workspace, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "Empty headless job workspace"], cwd=workspace, check=True)
    return workspace


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: headless-job-runner.py PAYLOAD.json", file=sys.stderr)
        return 2
    payload_path = pathlib.Path(sys.argv[1]).resolve()
    payload = json.loads(payload_path.read_text())
    job_dir = payload_path.parent
    meta_path = job_dir / "meta.json"
    rc_path = job_dir / "rc"
    workspace = prepare_workspace(payload, job_dir)
    cwd = normalize_cwd(payload.get("cwd") or "/workspace")
    command = str(payload["command"])
    if cwd != ".":
        command = f"cd {shlex.quote(cwd)} && {command}"
    memory_mib, max_memory_mib, vcpus = resources(str(payload.get("jobClass") or "cpu"))
    argv = [
        DISPATCH,
        "--owner", str(payload["owner"]),
        "--project", str(payload.get("project") or ""),
        "--workspace", str(workspace),
        "--command", command,
        "--timeout", str(int(payload.get("timeout") or 3600)),
        "--wait-seconds", str(WAIT_SECONDS),
        "--memory-mib", str(memory_mib),
        "--max-memory-mib", str(max_memory_mib),
        "--vcpus", str(vcpus),
    ]
    for key, value in (payload.get("env") or {}).items():
        argv += ["--env", f"{key}={value}"]

    child: subprocess.Popen[str] | None = None
    interrupted = False

    def on_signal(sig, _frame):
        nonlocal interrupted
        interrupted = True
        if child and child.poll() is None:
            try:
                child.send_signal(sig)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    child = subprocess.Popen(
        argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )

    def copy_stderr():
        assert child is not None and child.stderr is not None
        for line in child.stderr:
            sys.stderr.write(line)
            sys.stderr.flush()

    thread = threading.Thread(target=copy_stderr, daemon=True)
    thread.start()
    meta: dict[str, object] = {"launcherPid": os.getpid(), "dispatchPid": child.pid, "createdAt": time.time()}
    assert child.stdout is not None
    for line in child.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        text = line.strip()
        for prefix, key in (
            ("codex_ci_instance=", "instanceId"),
            ("codex_ci_vm=", "worker"),
            ("codex_ci_worker=", "workerIp"),
            ("codex_ci_reused=", "reused"),
        ):
            if text.startswith(prefix):
                value = text[len(prefix):]
                meta[key] = (value.lower() == "true") if key == "reused" else value
                atomic_json(meta_path, meta)
                break
    rc = child.wait()
    thread.join(timeout=2)
    if interrupted and rc == 0:
        rc = 130
    atomic_text(rc_path, str(int(rc)) + "\n")
    return int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
