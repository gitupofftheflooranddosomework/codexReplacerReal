#!/usr/bin/env python3
"""Restricted SSH forced-command endpoint for homeserver CI dispatchers."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone

STATE_DIR = pathlib.Path("/home/mark/.local/share/codex-worker")
CLAIM_LOCK = STATE_DIR / "claim.lock"
EXCLUSIVE = STATE_DIR / "exclusive.lock"
SCHEDULER = STATE_DIR / "scheduler.lock"
WORK_ROOT = pathlib.Path("/workspace/ci-dispatch")


def die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(code)


def decode(value: str) -> str:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except Exception:
        die("invalid encoded value")


def safe_lease(value: str) -> str:
    value = str(value or "")
    if not value or any(ch not in "0123456789abcdef" for ch in value) or len(value) > 64:
        die("invalid lease id")
    return value


def lease_dir(lease_id: str) -> pathlib.Path:
    return WORK_ROOT / safe_lease(lease_id)


def load_lease() -> dict:
    try:
        return json.loads(EXCLUSIVE.read_text())
    except Exception:
        return {}


def require_lease(lease_id: str) -> dict:
    lease = load_lease()
    if lease.get("leaseId") != lease_id or lease.get("source") != "github-actions":
        die("lease is not owned by this CI dispatcher", 3)
    return lease


def lock_claim():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CLAIM_LOCK.touch(exist_ok=True)
    handle = CLAIM_LOCK.open("r+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def unlock(handle) -> None:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def action_probe() -> int:
    ready = pathlib.Path("/var/lib/codex-lab-ready").exists()
    workstation = pathlib.Path("/var/lib/codex-worker-workstation-v1").exists()
    print(json.dumps({"ready": ready and workstation, "hostname": os.uname().nodename}, separators=(",", ":")))
    return 0 if ready and workstation else 4


def action_claim(args: list[str]) -> int:
    if len(args) != 2:
        die("claim requires one encoded JSON payload")
    try:
        payload = json.loads(decode(args[1]))
    except Exception:
        die("invalid claim payload")
    lease_id = safe_lease(payload.get("leaseId"))
    if payload.get("source") != "github-actions" or not str(payload.get("owner") or "").strip():
        die("invalid CI lease payload")
    handle = lock_claim()
    try:
        if SCHEDULER.exists() or EXCLUSIVE.exists():
            return 10
        payload["leaseId"] = lease_id
        EXCLUSIVE.write_text(json.dumps(payload, separators=(",", ":")))
        os.chmod(EXCLUSIVE, 0o600)
    finally:
        unlock(handle)
    print(json.dumps(payload, separators=(",", ":")))
    return 0


def action_import(args: list[str]) -> int:
    if len(args) != 2:
        die("import requires lease id")
    lease_id = safe_lease(args[1])
    require_lease(lease_id)
    root = lease_dir(lease_id)
    repo = root / "repo"
    shutil.rmtree(root, ignore_errors=True)
    repo.mkdir(parents=True, exist_ok=True)
    tar = subprocess.run(["tar", "-xf", "-", "-C", str(repo)], stdin=sys.stdin.buffer, check=False)
    if tar.returncode:
        return tar.returncode
    commands = [
        ["git", "init", "-q"],
        ["git", "add", "-A"],
        ["git", "-c", "user.name=Codex-CI", "-c", "user.email=codex-ci@localhost", "commit", "-qm", "snapshot", "--no-gpg-sign"],
    ]
    for command in commands:
        completed = subprocess.run(command, cwd=repo, check=False)
        if completed.returncode:
            return completed.returncode
    print(str(repo))
    return 0


def parse_env(encoded: str) -> dict[str, str]:
    if not encoded:
        return {}
    try:
        data = json.loads(decode(encoded))
    except Exception:
        die("invalid environment payload")
    if not isinstance(data, dict):
        die("environment must be an object")
    return {str(k): str(v) for k, v in data.items()}


def action_exec(args: list[str]) -> int:
    if len(args) != 4:
        die("exec requires lease id, encoded command, encoded env")
    lease_id = safe_lease(args[1])
    require_lease(lease_id)
    command = decode(args[2])
    env = os.environ.copy()
    env.update(parse_env(args[3]))
    repo = lease_dir(lease_id) / "repo"
    if not repo.is_dir():
        die("lease workspace is missing", 5)
    return subprocess.run(["bash", "-lc", command], cwd=repo, env=env, check=False).returncode


def safe_relative(value: str) -> str:
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        die("invalid artifact path")
    return str(path)


def action_artifact(args: list[str]) -> int:
    if len(args) < 3:
        die("artifact requires lease id and one or more encoded paths")
    lease_id = safe_lease(args[1])
    require_lease(lease_id)
    paths = [safe_relative(decode(item)) for item in args[2:]]
    repo = lease_dir(lease_id) / "repo"
    return subprocess.run(["tar", "-cf", "-", "--", *paths], cwd=repo, stdout=sys.stdout.buffer, check=False).returncode


def action_release(args: list[str]) -> int:
    if len(args) != 2:
        die("release requires lease id")
    lease_id = safe_lease(args[1])
    root = lease_dir(lease_id)
    handle = lock_claim()
    try:
        lease = load_lease()
        if lease.get("leaseId") != lease_id or lease.get("source") != "github-actions":
            return 0
        subprocess.run(["bw", "logout"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        shutil.rmtree(root, ignore_errors=True)
        try:
            EXCLUSIVE.unlink()
        except FileNotFoundError:
            pass
    finally:
        unlock(handle)
    return 0


def main() -> int:
    raw = os.environ.get("SSH_ORIGINAL_COMMAND", "").strip()
    if not raw:
        die("restricted Codex CI key: command required")
    try:
        args = shlex.split(raw)
    except ValueError:
        die("invalid command quoting")
    if not args or args[0] != "codex-ci":
        die("restricted Codex CI key")
    action = args[1] if len(args) > 1 else ""
    dispatch = {
        "probe": action_probe,
        "claim": action_claim,
        "import": action_import,
        "exec": action_exec,
        "artifact": action_artifact,
        "release": action_release,
    }
    handler = dispatch.get(action)
    if handler is None:
        die("unsupported Codex CI action")
    if action == "probe":
        return handler()
    return handler([action, *args[2:]])


if __name__ == "__main__":
    raise SystemExit(main())
