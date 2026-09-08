#!/usr/bin/env python3
"""Run a GitHub Actions workspace on one shared Codex KVM worker.

This client is intended for lightweight self-hosted GitHub runners on the
homeserver.  It does not make a worker project-specific.  Instead it claims a
station with the same claim.lock / scheduler.lock / exclusive.lock protocol
used by interactive Codex leases, streams a credential-free source snapshot to
that worker, runs the requested command, optionally pulls declared artifacts
back, and always releases/scrubs the worker.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

MAX_STATIONS = max(1, min(int(os.environ.get("CODEX_CI_MAX_STATIONS", "6")), 8))
WORKER_BASE = os.environ.get("CODEX_CI_WORKER_BASE", "192.168.122.229")
SSH_USER = os.environ.get("CODEX_CI_SSH_USER", "mark")
SSH_KEY = os.environ.get("CODEX_CI_SSH_KEY", "/var/lib/codex-ci/ssh/id_ed25519_codex_ci")
KNOWN_HOSTS = os.environ.get("CODEX_CI_KNOWN_HOSTS", "/var/lib/codex-ci/ssh/known_hosts")
SCHEDULER_URL = os.environ.get("CODEX_LAB_SCHEDULER_URL", "http://192.168.122.1:8766").rstrip("/")
REMOTE_ROOT = os.environ.get("CODEX_CI_REMOTE_ROOT", "/workspace/ci-dispatch")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_ip(station: int) -> str:
    # The six canonical workers are 192.168.122.230 .. .235.  Keep the base
    # configurable for future labs while retaining deterministic station IDs.
    prefix, last = WORKER_BASE.rsplit(".", 1)
    return f"{prefix}.{int(last) + int(station)}"


def ssh_base(station: int) -> list[str]:
    return [
        "ssh", "-i", SSH_KEY,
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{SSH_USER}@{worker_ip(station)}",
    ]


def ssh_run(station: int, command: str, *, input_bytes: bytes | None = None,
            timeout: int = 30, capture: bool = True) -> subprocess.CompletedProcess:
    kwargs = {
        "input": input_bytes,
        "timeout": timeout,
        "check": False,
    }
    if capture:
        kwargs.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    return subprocess.run([*ssh_base(station), command], **kwargs)


def post_usage(payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        SCHEDULER_URL + "/api/usage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status >= 300:
                raise RuntimeError(f"scheduler usage HTTP {response.status}")
    except Exception as exc:
        print(f"warning: scheduler usage record failed: {exc}", file=sys.stderr)


def lease_payload(lease_id: str, owner: str, project: str | None, ttl_minutes: int) -> dict:
    acquired = datetime.now(timezone.utc)
    return {
        "leaseId": lease_id,
        "owner": owner,
        "project": project,
        "chatLabel": None,
        "chatUrl": None,
        "source": "github-actions",
        "acquiredAt": acquired.isoformat(),
        "expiresAt": (acquired + timedelta(minutes=ttl_minutes)).isoformat(),
    }


def try_claim(station: int, payload: dict) -> bool:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    command = (
        "mkdir -p /home/mark/.local/share/codex-worker && "
        "flock -n /home/mark/.local/share/codex-worker/claim.lock sh -c "
        "'test ! -e /home/mark/.local/share/codex-worker/scheduler.lock && "
        "test ! -e /home/mark/.local/share/codex-worker/exclusive.lock && "
        "cat > /home/mark/.local/share/codex-worker/exclusive.lock'"
    )
    result = ssh_run(station, command, input_bytes=encoded, timeout=8)
    return result.returncode == 0


def acquire(owner: str, project: str | None, wait_seconds: int, ttl_minutes: int) -> tuple[int, dict]:
    deadline = time.monotonic() + max(1, wait_seconds)
    while True:
        start = int(uuid.uuid4().int % MAX_STATIONS) + 1
        for offset in range(MAX_STATIONS):
            station = ((start - 1 + offset) % MAX_STATIONS) + 1
            ready = ssh_run(
                station,
                "test -e /var/lib/codex-lab-ready -a -e /var/lib/codex-worker-workstation-v1",
                timeout=6,
            )
            if ready.returncode != 0:
                continue
            payload = lease_payload(uuid.uuid4().hex, owner, project, ttl_minutes)
            if try_claim(station, payload):
                post_usage({
                    "action": "start", "kind": "ci", "refId": payload["leaseId"],
                    "station": station, "owner": owner, "project": project,
                    "startedAt": payload["acquiredAt"], "source": "github-actions",
                })
                return station, payload
        if time.monotonic() >= deadline:
            raise TimeoutError(f"No shared Codex KVM became free within {wait_seconds}s")
        time.sleep(1)


def release(station: int, payload: dict, status: str) -> None:
    lease_id = payload["leaseId"]
    remote_dir = f"{REMOTE_ROOT}/{lease_id}"
    cleanup = f"""
set -eu
bw logout >/dev/null 2>&1 || true
rm -rf {shlex.quote(remote_dir)}
mkdir -p /home/mark/.local/share/codex-worker
flock -w 5 /home/mark/.local/share/codex-worker/claim.lock python3 - {shlex.quote(lease_id)} <<'PY'
import json, os, sys
path='/home/mark/.local/share/codex-worker/exclusive.lock'
try:
    data=json.load(open(path))
except Exception:
    data={{}}
if data.get('leaseId') == sys.argv[1]:
    try: os.unlink(path)
    except FileNotFoundError: pass
PY
"""
    result = ssh_run(station, cleanup, timeout=20)
    if result.returncode != 0:
        err = (result.stderr or b"").decode(errors="replace").strip()
        print(f"warning: worker release cleanup failed on VM {station}: {err}", file=sys.stderr)
    post_usage({
        "action": "end", "kind": "ci", "refId": lease_id,
        "station": station, "owner": payload["owner"], "project": payload.get("project"),
        "startedAt": payload["acquiredAt"], "finishedAt": now_iso(),
        "status": status, "source": "github-actions",
    })


def check_clean_git(workspace: pathlib.Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("workspace is not a Git checkout")
    if result.stdout.strip():
        raise RuntimeError("workspace is dirty; refusing to dispatch a snapshot that differs from the checked-out revision")


def stream_snapshot(station: int, payload: dict, workspace: pathlib.Path) -> str:
    lease_id = payload["leaseId"]
    remote_dir = f"{REMOTE_ROOT}/{lease_id}"
    remote_repo = f"{remote_dir}/repo"
    init_cmd = (
        f"rm -rf {shlex.quote(remote_dir)} && mkdir -p {shlex.quote(remote_repo)} && "
        f"tar -xf - -C {shlex.quote(remote_repo)} && cd {shlex.quote(remote_repo)} && "
        "git init -q && git add -A && "
        "git -c user.name=Codex-CI -c user.email=codex-ci@localhost commit -qm snapshot --no-gpg-sign"
    )
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"], cwd=workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    remote = subprocess.Popen([*ssh_base(station), init_cmd], stdin=archive.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    archive.stdout.close()
    remote_stdout, remote_stderr = remote.communicate(timeout=120)
    archive_stderr = archive.stderr.read() if archive.stderr is not None else b""
    archive_rc = archive.wait(timeout=10)
    if archive_rc != 0:
        raise RuntimeError(f"git archive failed: {archive_stderr.decode(errors='replace').strip()}")
    if remote.returncode != 0:
        raise RuntimeError(f"worker snapshot import failed: {remote_stderr.decode(errors='replace').strip()}")
    return remote_repo


def validate_artifact_path(value: str) -> str:
    p = pathlib.PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise ValueError(f"artifact path must be repository-relative: {value}")
    return str(p)


def pull_artifacts(station: int, remote_repo: str, artifacts: list[str], destination: pathlib.Path) -> None:
    if not artifacts:
        return
    clean = [validate_artifact_path(item) for item in artifacts]
    destination.mkdir(parents=True, exist_ok=True)
    command = "cd " + shlex.quote(remote_repo) + " && tar -cf - -- " + " ".join(shlex.quote(x) for x in clean)
    remote = subprocess.Popen([*ssh_base(station), command], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert remote.stdout is not None
    extract = subprocess.run(["tar", "-xf", "-", "-C", str(destination)], stdin=remote.stdout, check=False)
    remote.stdout.close()
    stderr = remote.stderr.read() if remote.stderr is not None else b""
    rc = remote.wait(timeout=30)
    if rc != 0 or extract.returncode != 0:
        raise RuntimeError(f"artifact transfer failed: {stderr.decode(errors='replace').strip()}")


def run_command(station: int, remote_repo: str, command: str, timeout: int, env: dict[str, str]) -> int:
    exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
    shell = f"cd {shlex.quote(remote_repo)} && "
    if exports:
        shell += f"export {exports}; "
    shell += f"exec bash -lc {shlex.quote(command)}"
    completed = subprocess.run([*ssh_base(station), shell], timeout=timeout, check=False)
    return int(completed.returncode)


def parse_env(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"--env requires KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key or not key.replace("_", "a").isalnum() or not (key[0].isalpha() or key[0] == "_"):
            raise ValueError(f"invalid environment name: {key}")
        out[key] = value
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Dispatch one CI command to the shared Codex KVM pool")
    parser.add_argument("--owner", required=True, help="stable runner/bot owner shown in lab history")
    parser.add_argument("--project", default=None)
    parser.add_argument("--workspace", default=os.environ.get("GITHUB_WORKSPACE", "."))
    parser.add_argument("--command", required=True)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--wait-seconds", type=int, default=600)
    parser.add_argument("--ttl-minutes", type=int, default=180)
    parser.add_argument("--artifact", action="append", default=[])
    parser.add_argument("--artifact-dest", default=None)
    parser.add_argument("--env", action="append", default=[])
    args = parser.parse_args()

    workspace = pathlib.Path(args.workspace).resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace does not exist: {workspace}")
    check_clean_git(workspace)
    env = parse_env(args.env)
    station = 0
    payload = None
    status = "failed"
    try:
        station, payload = acquire(args.owner, args.project, args.wait_seconds, args.ttl_minutes)
        print(f"codex_ci_station={station}")
        print(f"codex_ci_worker={worker_ip(station)}")
        remote_repo = stream_snapshot(station, payload, workspace)
        rc = run_command(station, remote_repo, args.command, max(1, args.timeout), env)
        if args.artifact:
            destination = pathlib.Path(args.artifact_dest or workspace).resolve()
            pull_artifacts(station, remote_repo, args.artifact, destination)
        status = "succeeded" if rc == 0 else "failed"
        return rc
    except subprocess.TimeoutExpired:
        status = "timed_out"
        print("Codex CI worker command timed out", file=sys.stderr)
        return 124
    finally:
        if station and payload:
            release(station, payload, status)


if __name__ == "__main__":
    raise SystemExit(main())
