#!/usr/bin/env python3
"""Run one queued MCP heavy job through the disposable headless KVM dispatcher."""
from __future__ import annotations

import base64
import json
import os
import pathlib
import urllib.parse
import shlex
import signal
import subprocess
import sys
import threading
import time

import rollout_admission

DISPATCH = os.environ.get("CODEX_CI_DISPATCH", "/home/mark/.local/bin/codex-ci-dispatch")
WAIT_SECONDS = max(60, int(os.environ.get("CODEX_VM_JOB_WAIT_SECONDS", "86400")))
GIT_TOKEN_FILE = os.environ.get("CODEX_GIT_TOKEN_FILE", "").strip()


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
        "heavy": (4096, 8192, 4),
    }
    return table.get(str(job_class or "cpu"), table["cpu"])


def git_environment(job_dir: pathlib.Path, repo_url: str = "") -> dict[str, str]:
    """Build a non-interactive Git environment without putting secrets in argv."""
    parsed = urllib.parse.urlsplit(str(repo_url or "").strip())
    if parsed.hostname == "github.com" and (
        parsed.username is not None or parsed.password is not None
    ):
        raise RuntimeError("GitHub repoUrl must not contain embedded credentials")
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        # Ignore any ambient credential helper inherited by the service account.
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
    }
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GITHUB_PAT"):
        env.pop(key, None)
    if not GIT_TOKEN_FILE:
        token_file = os.environ.get("CODEX_VM_JOB_GITHUB_TOKEN_FILE", "").strip()
        token = ""
        if token_file:
            try:
                token = pathlib.Path(token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise RuntimeError("controller GitHub token file is not readable") from exc
        if not token and parsed.hostname == "github.com":
            try:
                cp = subprocess.run(
                    ["gh", "auth", "token", "--hostname", "github.com"],
                    text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    check=False, timeout=10,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                cp = None
            if cp is not None and cp.returncode == 0:
                token = cp.stdout.strip()
        if not token:
            return env
        if any(ord(ch) < 33 for ch in token):
            raise RuntimeError("controller GitHub token is malformed")
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env["GIT_CONFIG_COUNT"] = "2"
        env["GIT_CONFIG_KEY_1"] = "http.https://github.com/.extraheader"
        env["GIT_CONFIG_VALUE_1"] = f"AUTHORIZATION: basic {basic}"
        return env
    token_file = pathlib.Path(GIT_TOKEN_FILE)
    if not token_file.is_absolute() or not token_file.is_file():
        raise RuntimeError("CODEX_GIT_TOKEN_FILE must name a readable absolute file")
    helper = job_dir / ".git-askpass"
    helper.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *Username*) printf '%s\\n' x-access-token ;;\n"
        "  *Password*) cat \"$CODEX_GIT_TOKEN_FILE\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    helper.chmod(0o700)
    env["GIT_ASKPASS"] = str(helper)
    return env


def prepare_workspace(payload: dict, job_dir: pathlib.Path) -> pathlib.Path:
    workspace = job_dir / "workspace"
    repo = str(payload.get("repoUrl") or "").strip()
    revision = str(payload.get("revision") or "").strip()
    if workspace.exists():
        subprocess.run(["rm", "-rf", str(workspace)], check=True)
    if repo:
        git_env = git_environment(job_dir, repo)
        cp = subprocess.run(
            ["git", "clone", "--filter=blob:none", repo, str(workspace)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=git_env,
        )
        if cp.returncode:
            raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "git clone failed")
        if revision:
            cp = subprocess.run(
                ["git", "fetch", "--depth=1", "origin", revision], cwd=workspace,
                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=git_env,
            )
            if cp.returncode:
                raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "git fetch failed")
            subprocess.run(
                ["git", "checkout", "--detach", "FETCH_HEAD"], cwd=workspace,
                check=True, env=git_env,
            )
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
    rollout_admission.authorize_effect(
        str(payload["id"]),
        payload.get("admissionGeneration"),
        str(payload.get("project") or ""),
        str(payload.get("jobClass") or "cpu"),
    )
    cwd = normalize_cwd(payload.get("cwd") or "/workspace")
    command = str(payload["command"])
    if cwd != ".":
        command = f"cd {shlex.quote(cwd)} && {command}"
    memory_mib, max_memory_mib, vcpus = resources(str(payload.get("jobClass") or "cpu"))
    # Explicit requests override the class defaults but remain positive and are still
    # subject to aggregate admission control in codex-ci-headless.
    if payload.get("memoryMiB") is not None:
        memory_mib = max(768, int(payload["memoryMiB"]))
    if payload.get("maxMemoryMiB") is not None:
        max_memory_mib = max(memory_mib, int(payload["maxMemoryMiB"]))
    if payload.get("vcpus") is not None:
        vcpus = max(1, int(payload["vcpus"]))
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
