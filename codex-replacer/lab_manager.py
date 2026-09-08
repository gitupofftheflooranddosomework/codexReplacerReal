#!/usr/bin/env python3
import fcntl
import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

LAB_ROOT = Path(os.environ.get("CODEX_LAB_ROOT", "/tank/codex-lab"))
STATE_FILE = LAB_ROOT / "state.json"
AUDIT_FILE = LAB_ROOT / "sign-in-out.jsonl"
SHEET_FILE = LAB_ROOT / "SIGN-IN-OUT.md"
LOCK_FILE = LAB_ROOT / ".lock"
WORKSPACES = LAB_ROOT / "workspaces"
ARCHIVES = LAB_ROOT / "archives"
IMAGE = os.environ.get("CODEX_LAB_IMAGE", "codex-lab-worker:bookworm")
MAX_STATIONS = max(1, min(int(os.environ.get("CODEX_LAB_MAX_STATIONS", "6")), 12))
PREWARM_STATIONS = max(0, min(int(os.environ.get("CODEX_LAB_PREWARM_STATIONS", "4")), MAX_STATIONS))


def now():
    return datetime.now(timezone.utc)


def iso(value=None):
    return (value or now()).isoformat()


def ensure_dirs():
    for path in (LAB_ROOT, WORKSPACES, ARCHIVES):
        path.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)


def load_state():
    if not STATE_FILE.exists():
        return {"version": 1, "stations": {}}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "stations": {}}


def save_state(state):
    fd, temporary = tempfile.mkstemp(prefix="state-", suffix=".json", dir=LAB_ROOT)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, STATE_FILE)
    lines = [
        "# Codex Computer Lab Sign-In / Sign-Out",
        "",
        f"Updated: {iso()}",
        "",
        "| Station | Status | Agent | Project | Signed in | Lease expires |",
        "|---:|---|---|---|---|---|",
    ]
    for station in range(1, MAX_STATIONS + 1):
        record = record_for(state, station)
        lines.append(
            f"| {station} | {record.get('status', 'free')} | {record.get('owner') or ''} | "
            f"{record.get('project') or ''} | {record.get('acquiredAt') or ''} | {record.get('expiresAt') or ''} |"
        )
    SHEET_FILE.write_text("\n".join(lines) + "\n")


def audit(event, **fields):
    with AUDIT_FILE.open("a") as handle:
        handle.write(json.dumps({"time": iso(), "event": event, **fields}, separators=(",", ":")) + "\n")


def locked_state():
    ensure_dirs()
    lock = LOCK_FILE.open("r+")
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    return lock, load_state()


def unlock(lock):
    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    lock.close()


def docker(args, timeout=120):
    return subprocess.run(["docker", *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def container_name(station):
    return f"codex-lab-{int(station):02d}"


def workspace(station):
    return WORKSPACES / f"station-{int(station):02d}"


def container_running(name):
    result = docker(["inspect", "-f", "{{.State.Running}}", name], 20)
    return result.returncode == 0 and result.stdout.strip() == "true"


def image_ready():
    return docker(["image", "inspect", IMAGE], 20).returncode == 0


def provision(station):
    station = int(station)
    if not 1 <= station <= MAX_STATIONS:
        raise ValueError(f"station must be between 1 and {MAX_STATIONS}")
    if not image_ready():
        raise RuntimeError(f"Lab image {IMAGE} has not been built")
    name = container_name(station)
    path = workspace(station)
    path.mkdir(parents=True, exist_ok=True)
    if docker(["inspect", name], 20).returncode == 0:
        if not container_running(name):
            result = docker(["start", name], 60)
            if result.returncode:
                raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return name
    result = docker([
        "run", "-d", "--name", name, "--hostname", name,
        "--label", "codex.lab=1", "--label", f"codex.lab.station={station}",
        "--cpus", os.environ.get("CODEX_LAB_CPUS_PER_STATION", "2"),
        "--memory", os.environ.get("CODEX_LAB_MEMORY_PER_STATION", "2g"),
        "--pids-limit", os.environ.get("CODEX_LAB_PIDS_PER_STATION", "1024"),
        "--restart", "unless-stopped", "-v", f"{path}:/workspace", "-w", "/workspace", IMAGE,
    ], 120)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return name


def ensure_pool(count=None):
    target = PREWARM_STATIONS if count is None else max(0, min(int(count), MAX_STATIONS))
    return [provision(station) for station in range(1, target + 1)]


def record_for(state, station):
    key = str(int(station))
    record = state.setdefault("stations", {}).setdefault(key, {})
    record.setdefault("station", int(station))
    record.setdefault("container", container_name(station))
    record.setdefault("workspace", str(workspace(station)))
    record.setdefault("status", "free")
    return record


def recycle_station(station, lease_id=None):
    station = int(station)
    name = container_name(station)
    docker(["rm", "-f", name], 60)
    path = workspace(station)
    if path.exists() and any(path.iterdir()):
        suffix = (lease_id or "unleased")[:8]
        archived = ARCHIVES / f"station-{station:02d}-{now().strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
        os.replace(path, archived)
    path.mkdir(parents=True, exist_ok=True)
    if station <= PREWARM_STATIONS:
        provision(station)


def expire(state):
    expired = []
    current = now()
    for record in state.setdefault("stations", {}).values():
        if record.get("status") != "leased" or not record.get("expiresAt"):
            continue
        try:
            due = datetime.fromisoformat(record["expiresAt"])
        except ValueError:
            continue
        if due <= current:
            old = record.copy()
            record.update({"status": "free", "leaseId": None, "owner": None, "project": None, "acquiredAt": None, "expiresAt": None, "releasedAt": iso(current), "releaseReason": "expired"})
            expired.append(old)
            audit("auto_sign_out", station=old.get("station"), leaseId=old.get("leaseId"), owner=old.get("owner"), project=old.get("project"))
            recycle_station(old.get("station"), old.get("leaseId"))
    return expired


def acquire(owner, project=None, ttl_minutes=180):
    owner = str(owner or "").strip()
    if not owner:
        raise ValueError("owner is required")
    ttl_minutes = max(15, min(int(ttl_minutes), 1440))
    lock, state = locked_state()
    try:
        expire(state)
        for station in range(1, MAX_STATIONS + 1):
            record = record_for(state, station)
            if record.get("status") == "leased":
                continue
            provision(station)
            lease_id = str(uuid.uuid4())
            acquired = now()
            record.update({"status": "leased", "leaseId": lease_id, "owner": owner, "project": project or None, "acquiredAt": iso(acquired), "expiresAt": iso(acquired + timedelta(minutes=ttl_minutes)), "releasedAt": None, "releaseReason": None})
            save_state(state)
            audit("sign_in", station=station, leaseId=lease_id, owner=owner, project=project or None, ttlMinutes=ttl_minutes)
            return record.copy()
        raise RuntimeError(f"All {MAX_STATIONS} lab stations are leased")
    finally:
        unlock(lock)


def find_active(state, lease_id=None, station=None):
    for record in state.setdefault("stations", {}).values():
        if record.get("status") != "leased":
            continue
        if lease_id and record.get("leaseId") == lease_id:
            return record
        if station is not None and int(record.get("station", 0)) == int(station):
            return record
    raise KeyError("Active lab lease not found")


def release(lease_id=None, station=None, reason="released", recycle=True):
    lock, state = locked_state()
    try:
        record = find_active(state, lease_id, station)
        old = record.copy()
        station_number = int(record["station"])
        record.update({"status": "free", "leaseId": None, "owner": None, "project": None, "acquiredAt": None, "expiresAt": None, "releasedAt": iso(), "releaseReason": reason})
        save_state(state)
        audit("sign_out", station=station_number, leaseId=old.get("leaseId"), owner=old.get("owner"), project=old.get("project"), reason=reason, recycle=bool(recycle))
    finally:
        unlock(lock)
    if recycle:
        recycle_station(station_number, old.get("leaseId"))
    return old


def list_stations(audit_lines=12):
    lock, state = locked_state()
    try:
        expire(state)
        rows = []
        for station in range(1, MAX_STATIONS + 1):
            item = record_for(state, station).copy()
            item["running"] = container_running(item["container"])
            rows.append(item)
        save_state(state)
    finally:
        unlock(lock)
    recent = []
    if AUDIT_FILE.exists() and audit_lines:
        for line in AUDIT_FILE.read_text().splitlines()[-max(0, int(audit_lines)):]:
            try:
                recent.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {"maxStations": MAX_STATIONS, "prewarmStations": PREWARM_STATIONS, "image": IMAGE, "stations": rows, "recentSignInOut": recent}


def execute(command, lease_id=None, station=None, cwd="/workspace", timeout=120, env=None, as_root=False, max_bytes=1048576):
    lock, state = locked_state()
    try:
        expire(state)
        record = find_active(state, lease_id, station).copy()
    finally:
        unlock(lock)
    if not container_running(record["container"]):
        provision(record["station"])
    args = ["exec"]
    if as_root:
        args += ["-u", "0"]
    for key, value in (env or {}).items():
        args += ["-e", f"{key}={value}"]
    args += ["-w", cwd, record["container"], "/bin/bash", "-lc", command]
    result = docker(args, max(1, min(int(timeout), 86400)))
    limit = max(1024, min(int(max_bytes), 8388608))
    out = result.stdout.encode("utf-8", errors="replace")
    err = result.stderr.encode("utf-8", errors="replace")
    return {"station": record["station"], "leaseId": record["leaseId"], "owner": record["owner"], "project": record.get("project"), "exitCode": result.returncode, "stdout": out[:limit].decode("utf-8", errors="replace"), "stderr": err[:limit].decode("utf-8", errors="replace"), "truncated": len(out) > limit or len(err) > limit}


def collect():
    lock, state = locked_state()
    try:
        expired = expire(state)
        save_state(state)
    finally:
        unlock(lock)
    ensured = ensure_pool()
    return {"expiredLeases": [item.get("leaseId") for item in expired], "prewarmed": ensured}
