#!/usr/bin/env python3
"""Dispatch a clean GitHub Actions checkout to one shared Codex KVM worker.

The SSH key used by this client is deliberately restricted on every worker to
codex-ci-worker-rpc.py.  The runner cannot bypass the shared VM claim locks or
access another lease's workspace.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

MAX_STATIONS = max(1, min(int(os.environ.get("CODEX_CI_MAX_STATIONS", "6")), 8))
WORKER_BASE = os.environ.get("CODEX_CI_WORKER_BASE", "192.168.122.229")
SSH_USER = os.environ.get("CODEX_CI_SSH_USER", "mark")
SSH_KEY = os.environ.get("CODEX_CI_SSH_KEY", "/var/lib/codex-ci/ssh/id_ed25519_codex_ci")
KNOWN_HOSTS = os.environ.get("CODEX_CI_KNOWN_HOSTS", "/var/lib/codex-ci/ssh/known_hosts")
SCHEDULER_URL = os.environ.get("CODEX_LAB_SCHEDULER_URL", "http://192.168.122.1:8766").rstrip("/")


def encode(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def worker_ip(station: int) -> str:
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


def ssh_capture(station: int, original_command: str, *, input_bytes: bytes | None = None,
                timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*ssh_base(station), original_command], input=input_bytes,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False,
    )


def post_usage(payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        SCHEDULER_URL + "/api/usage", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2):
            return
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


def acquire(owner: str, project: str | None, wait_seconds: int, ttl_minutes: int) -> tuple[int, dict]:
    deadline = time.monotonic() + max(1, wait_seconds)
    while True:
        start = int(uuid.uuid4().int % MAX_STATIONS) + 1
        for offset in range(MAX_STATIONS):
            station = ((start - 1 + offset) % MAX_STATIONS) + 1
            probe = ssh_capture(station, "codex-ci probe", timeout=6)
            if probe.returncode != 0:
                continue
            payload = lease_payload(uuid.uuid4().hex, owner, project, ttl_minutes)
            claim = ssh_capture(
                station,
                "codex-ci claim " + encode(json.dumps(payload, separators=(",", ":"))),
                timeout=8,
            )
            if claim.returncode != 0:
                continue
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
    result = ssh_capture(station, f"codex-ci release {lease_id}", timeout=20)
    if result.returncode != 0:
        print(
            f"warning: worker release failed on VM {station}: "
            f"{result.stderr.decode(errors='replace').strip()}", file=sys.stderr,
        )
    post_usage({
        "action": "end", "kind": "ci", "refId": lease_id,
        "station": station, "owner": payload["owner"], "project": payload.get("project"),
        "startedAt": payload["acquiredAt"], "finishedAt": now_iso(),
        "status": status, "source": "github-actions",
    })


def check_clean_git(workspace: pathlib.Path) -> None:
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=workspace,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("workspace is not a Git checkout")
    if result.stdout.strip():
        raise RuntimeError("workspace is dirty; dispatch only committed Actions checkouts")


def stream_snapshot(station: int, lease_id: str, workspace: pathlib.Path) -> None:
    archive = subprocess.Popen(
        ["git", "archive", "--format=tar", "HEAD"], cwd=workspace,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert archive.stdout is not None
    remote = subprocess.Popen(
        [*ssh_base(station), f"codex-ci import {lease_id}"],
        stdin=archive.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    archive.stdout.close()
    _, remote_stderr = remote.communicate(timeout=120)
    archive_stderr = archive.stderr.read() if archive.stderr is not None else b""
    archive_rc = archive.wait(timeout=10)
    if archive_rc != 0:
        raise RuntimeError(f"git archive failed: {archive_stderr.decode(errors='replace').strip()}")
    if remote.returncode != 0:
        raise RuntimeError(f"worker snapshot import failed: {remote_stderr.decode(errors='replace').strip()}")


def safe_artifact(value: str) -> str:
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"artifact path must be repository-relative: {value}")
    return str(path)


def pull_artifacts(station: int, lease_id: str, artifacts: list[str], destination: pathlib.Path) -> None:
    if not artifacts:
        return
    clean = [safe_artifact(item) for item in artifacts]
    destination.mkdir(parents=True, exist_ok=True)
    remote_cmd = "codex-ci artifact " + lease_id + " " + " ".join(encode(item) for item in clean)
    remote = subprocess.Popen([*ssh_base(station), remote_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert remote.stdout is not None
    extract = subprocess.run(["tar", "-xf", "-", "-C", str(destination)], stdin=remote.stdout, check=False)
    remote.stdout.close()
    stderr = remote.stderr.read() if remote.stderr is not None else b""
    rc = remote.wait(timeout=60)
    if rc != 0 or extract.returncode != 0:
        raise RuntimeError(f"artifact transfer failed: {stderr.decode(errors='replace').strip()}")


def run_command(station: int, lease_id: str, command: str, timeout: int, env: dict[str, str]) -> int:
    encoded_env = encode(json.dumps(env, separators=(",", ":")))
    original = f"codex-ci exec {lease_id} {encode(command)} {encoded_env}"
    return subprocess.run([*ssh_base(station), original], timeout=timeout, check=False).returncode


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
    parser.add_argument("--owner", required=True)
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
        stream_snapshot(station, payload["leaseId"], workspace)
        rc = run_command(station, payload["leaseId"], args.command, max(1, args.timeout), env)
        if args.artifact:
            destination = pathlib.Path(args.artifact_dest or workspace).resolve()
            pull_artifacts(station, payload["leaseId"], args.artifact, destination)
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
