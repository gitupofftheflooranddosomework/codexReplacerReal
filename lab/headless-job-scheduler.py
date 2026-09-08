#!/usr/bin/env python3
"""Elastic MCP heavy-job queue backed by disposable headless KVMs.

This service deliberately does not schedule automatic work on codex-lab-vm-01..06.
It launches lightweight local dispatch processes aggressively; the headless KVM
allocator is the admission controller and queues launchers at CPU/RAM/I/O high water.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = os.environ.get("CODEX_VM_JOB_HOST", "192.168.122.1")
PORT = int(os.environ.get("CODEX_VM_JOB_PORT", "8767"))
ROOT = pathlib.Path(os.environ.get("CODEX_VM_JOB_ROOT", "/tank/vm/codex-ci-headless/job-scheduler"))
DB = pathlib.Path(os.environ.get("CODEX_VM_JOB_DB", str(ROOT / "jobs.sqlite3")))
JOB_ROOT = pathlib.Path(os.environ.get("CODEX_VM_JOB_WORK_ROOT", str(ROOT / "jobs")))
RUNNER = os.environ.get("CODEX_VM_JOB_RUNNER", "/home/mark/.local/bin/codex-headless-job-runner")
MANAGER = os.environ.get("CODEX_CI_HEADLESS_MANAGER", "/home/mark/.local/bin/codex-ci-headless")
INTERACTIVE_URL = os.environ.get("CODEX_LAB_INTERACTIVE_SCHEDULER_URL", "http://192.168.122.1:8766").rstrip("/")
MAX_LAUNCHERS = max(1, int(os.environ.get("CODEX_VM_JOB_MAX_LAUNCHERS", "96")))
MAX_TAIL = 1024 * 1024
POLL_SECONDS = max(0.2, float(os.environ.get("CODEX_VM_JOB_POLL_SECONDS", "0.5")))
TERMINAL = {"succeeded", "failed", "timed_out", "canceled"}
STOP = threading.Event()
WAKE = threading.Event()
DB_LOCK = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    ROOT.mkdir(parents=True, exist_ok=True)
    JOB_ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          started_at TEXT,finished_at TEXT,owner TEXT NOT NULL,project TEXT,
          chat_label TEXT,chat_url TEXT,job_class TEXT NOT NULL,command TEXT NOT NULL,
          cwd TEXT NOT NULL,env_json TEXT NOT NULL,timeout_seconds INTEGER NOT NULL,
          repo_url TEXT,revision TEXT,status TEXT NOT NULL,launcher_pid INTEGER,
          instance_id TEXT,worker TEXT,worker_ip TEXT,exit_code INTEGER,error TEXT,
          cancel_requested INTEGER NOT NULL DEFAULT 0
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS headless_jobs_status_created ON jobs(status,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS headless_jobs_finished ON jobs(finished_at)")
    conn.commit()
    return conn


def job_dir(job_id: str) -> pathlib.Path:
    return JOB_ROOT / str(job_id)


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def load_json(path: pathlib.Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def public_job(row) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    try:
        out["env"] = json.loads(out.pop("env_json", "{}") or "{}")
    except Exception:
        out["env"] = {}
    out["chatLabel"] = out.pop("chat_label", None)
    out["chatUrl"] = out.pop("chat_url", None)
    out["jobClass"] = out.pop("job_class", None)
    out["timeout"] = out.pop("timeout_seconds", None)
    out["repoUrl"] = out.pop("repo_url", None)
    out["workerIp"] = out.pop("worker_ip", None)
    out["instanceId"] = out.pop("instance_id", None)
    out["exitCode"] = out.pop("exit_code", None)
    out["cancelRequested"] = bool(out.pop("cancel_requested", 0))
    out.pop("launcher_pid", None)
    out["execution"] = "ephemeral-headless-kvm"
    return out


def write_payload(row: sqlite3.Row) -> pathlib.Path:
    d = job_dir(row["id"])
    d.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": row["id"], "owner": row["owner"], "project": row["project"],
        "chatLabel": row["chat_label"], "chatUrl": row["chat_url"],
        "jobClass": row["job_class"], "command": row["command"], "cwd": row["cwd"],
        "env": json.loads(row["env_json"] or "{}"), "timeout": row["timeout_seconds"],
        "repoUrl": row["repo_url"], "revision": row["revision"],
    }
    path = d / "payload.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def launch_row(row: sqlite3.Row) -> None:
    payload = write_payload(row)
    d = payload.parent
    stdout = open(d / "stdout.log", "ab", buffering=0)
    stderr = open(d / "stderr.log", "ab", buffering=0)
    try:
        proc = subprocess.Popen(
            [RUNNER, str(payload)], stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
            start_new_session=True, close_fds=True,
        )
    finally:
        stdout.close(); stderr.close()
    stamp = now_iso()
    with DB_LOCK:
        conn = db()
        current = conn.execute("SELECT status,launcher_pid FROM jobs WHERE id=?", (row["id"],)).fetchone()
        # scheduler_iteration atomically claims a queued row with launcher_pid=0
        # before releasing DB_LOCK. Only that claim may be replaced by a real PID.
        if current and current["status"] == "queued" and current["launcher_pid"] == 0:
            conn.execute("UPDATE jobs SET launcher_pid=?,updated_at=?,error=NULL WHERE id=?", (proc.pid, stamp, row["id"]))
            conn.commit()
        else:
            try: os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError: pass
        conn.close()


def reconcile_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    d = job_dir(row["id"])
    meta = load_json(d / "meta.json")
    stamp = now_iso()
    if meta.get("instanceId") and row["status"] == "queued":
        conn.execute(
            "UPDATE jobs SET status='running',started_at=COALESCE(started_at,?),updated_at=?,instance_id=?,worker=?,worker_ip=? WHERE id=?",
            (stamp, stamp, str(meta.get("instanceId")), str(meta.get("worker") or "") or None,
             str(meta.get("workerIp") or "") or None, row["id"]),
        )
    rc_path = d / "rc"
    if rc_path.is_file():
        try: rc = int(rc_path.read_text().strip().splitlines()[-1])
        except Exception: rc = 1
        canceled = bool(row["cancel_requested"])
        status = "canceled" if canceled or rc == 130 else ("succeeded" if rc == 0 else ("timed_out" if rc == 124 else "failed"))
        conn.execute(
            "UPDATE jobs SET status=?,exit_code=?,finished_at=COALESCE(finished_at,?),updated_at=?,launcher_pid=NULL WHERE id=?",
            (status, rc, stamp, stamp, row["id"]),
        )
        return
    if row["launcher_pid"] and not pid_alive(int(row["launcher_pid"])):
        conn.execute(
            "UPDATE jobs SET status='failed',finished_at=COALESCE(finished_at,?),updated_at=?,launcher_pid=NULL,error=? WHERE id=?",
            (stamp, stamp, "headless launcher exited without a result", row["id"]),
        )


def scheduler_iteration() -> None:
    with DB_LOCK:
        conn = db()
        active = conn.execute("SELECT * FROM jobs WHERE status IN ('queued','running') AND launcher_pid IS NOT NULL ORDER BY created_at").fetchall()
        for row in active:
            reconcile_one(conn, row)
        conn.commit()
        launcher_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running') AND launcher_pid IS NOT NULL").fetchone()[0]
        room = max(0, MAX_LAUNCHERS - int(launcher_count))
        pending = conn.execute(
            "SELECT * FROM jobs WHERE status='queued' AND launcher_pid IS NULL ORDER BY created_at LIMIT ?", (room,)
        ).fetchall() if room else []
        claimed = []
        claim_stamp = now_iso()
        for row in pending:
            # Claim under DB_LOCK before Popen. API GETs and the background loop can
            # call scheduler_iteration concurrently; without this claim they can
            # both spawn a runner for the same job and race on one workspace.
            changed = conn.execute(
                "UPDATE jobs SET launcher_pid=0,updated_at=? WHERE id=? AND status='queued' AND launcher_pid IS NULL",
                (claim_stamp, row["id"]),
            ).rowcount
            if changed == 1:
                claimed.append(row)
        conn.commit()
        conn.close()
    for row in claimed:
        try:
            launch_row(row)
        except Exception as exc:
            with DB_LOCK:
                conn = db(); stamp = now_iso()
                conn.execute("UPDATE jobs SET status='failed',finished_at=?,updated_at=?,launcher_pid=NULL,error=? WHERE id=?",
                             (stamp, stamp, str(exc)[:2000], row["id"]))
                conn.commit(); conn.close()


def scheduler_loop() -> None:
    while not STOP.is_set():
        try:
            scheduler_iteration()
        except Exception as exc:
            print(json.dumps({"event":"headless_scheduler_error","time":now_iso(),"error":str(exc)[:2000]}), flush=True)
        WAKE.wait(POLL_SECONDS)
        WAKE.clear()


def submit_job(payload: dict) -> dict:
    owner = str(payload.get("owner") or "").strip()
    command = str(payload.get("command") or "").strip()
    if not owner or not command:
        raise ValueError("owner and command are required")
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    job_id = uuid.uuid4().hex
    stamp = now_iso()
    record = (
        job_id, stamp, stamp, owner, str(payload.get("project") or "").strip() or None,
        str(payload.get("chatLabel") or "").strip() or None, str(payload.get("chatUrl") or "").strip() or None,
        str(payload.get("jobClass") or "cpu"), command, str(payload.get("cwd") or "/workspace"),
        json.dumps(env, separators=(",", ":")), max(1, min(int(payload.get("timeout", 3600)), 86400)),
        str(payload.get("repoUrl") or "").strip() or None, str(payload.get("revision") or "").strip() or None,
        "queued",
    )
    with DB_LOCK:
        conn = db()
        conn.execute(
            "INSERT INTO jobs(id,created_at,updated_at,owner,project,chat_label,chat_url,job_class,command,cwd,env_json,timeout_seconds,repo_url,revision,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            record,
        )
        conn.commit(); row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(); conn.close()
    WAKE.set()
    return public_job(row)


def tail(path: pathlib.Path, limit: int) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END); size = handle.tell(); handle.seek(max(0, size-limit)); return handle.read(limit).decode(errors="replace")
    except OSError:
        return ""


def get_job(job_id: str, max_bytes: int = 65536) -> dict | None:
    scheduler_iteration()
    with DB_LOCK:
        conn = db(); row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(); conn.close()
    result = public_job(row)
    if result is None:
        return None
    limit = max(1024, min(int(max_bytes), MAX_TAIL))
    d = job_dir(job_id)
    result["stdoutTail"] = tail(d / "stdout.log", limit)
    result["stderrTail"] = tail(d / "stderr.log", limit)
    return result


def list_jobs(status: str | None = None, limit: int = 50) -> list[dict]:
    scheduler_iteration()
    limit = max(1, min(int(limit), 200))
    with DB_LOCK:
        conn = db()
        if status:
            rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
    return [public_job(row) for row in rows]


def manager_state() -> dict:
    cp = subprocess.run([MANAGER, "state", "--history-limit", "40"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False)
    if cp.returncode:
        return {"error": cp.stderr.strip() or cp.stdout.strip() or f"manager rc={cp.returncode}"}
    try: return json.loads(cp.stdout)
    except json.JSONDecodeError: return {"error": "invalid headless manager state"}


def interactive_state() -> dict:
    try:
        req = urllib.request.Request(INTERACTIVE_URL + "/api/workers", headers={"Accept":"application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    except Exception as exc:
        return {"error": str(exc)}


def workers_state() -> dict:
    scheduler_iteration()
    with DB_LOCK:
        conn = db()
        queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
        conn.close()
    headless = manager_state()
    interactive = interactive_state()
    return {
        "mode": "elastic-headless",
        "batchMax": 48,
        "maxLaunchers": MAX_LAUNCHERS,
        "queuedJobs": queued,
        "runningJobs": running,
        "ephemeralHeadless": headless,
        "interactiveDesktops": interactive,
        "workers": interactive.get("workers", []) if isinstance(interactive, dict) else [],
    }


def cancel_job(job_id: str) -> dict | None:
    with DB_LOCK:
        conn = db(); row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            conn.close(); return None
        if row["status"] in TERMINAL:
            out = public_job(row); conn.close(); return out
        stamp = now_iso()
        pid = int(row["launcher_pid"] or 0)
        if not pid:
            # Make a never-started queued job terminal under the same DB lock.
            # launch_row rechecks status after spawning and kills its process group
            # if a selection raced this cancellation.
            conn.execute(
                "UPDATE jobs SET status='canceled',cancel_requested=1,finished_at=?,updated_at=? WHERE id=?",
                (stamp, stamp, job_id),
            )
            conn.commit(); row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone(); conn.close()
            WAKE.set()
            return public_job(row)
        conn.execute("UPDATE jobs SET cancel_requested=1,updated_at=? WHERE id=?", (stamp, job_id)); conn.commit(); conn.close()
    if pid_alive(pid):
        try: os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError: pass
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if (job_dir(job_id) / "rc").is_file() or not pid_alive(pid):
                break
            time.sleep(0.25)
    scheduler_iteration()
    current = get_job(job_id, 4096)
    if current and current.get("status") not in TERMINAL:
        instance = current.get("instanceId")
        if instance:
            subprocess.run([MANAGER, "finish", str(instance), "--status", "canceled", "--reason", "scheduler_cancel"],
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
        if pid_alive(pid):
            try: os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError: pass
        with DB_LOCK:
            conn = db(); stamp = now_iso()
            conn.execute("UPDATE jobs SET status='canceled',finished_at=?,updated_at=?,launcher_pid=NULL WHERE id=?", (stamp, stamp, job_id))
            conn.commit(); conn.close()
        current = get_job(job_id, 4096)
    WAKE.set()
    return current


class Handler(BaseHTTPRequestHandler):
    server_version = "CodexHeadlessJobScheduler/1.0"
    def log_message(self, fmt, *args):
        print(json.dumps({"time":now_iso(),"event":"http","client":self.client_address[0],"message":fmt % args}), flush=True)
    def send_json(self, payload, status=200):
        data = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4 * 1024 * 1024: raise ValueError("request too large")
        return json.loads(self.rfile.read(n) or b"{}")
    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        try:
            if u.path == "/health":
                return self.send_json({"ok":True,"service":"codex-headless-job-scheduler","time":now_iso(),"maxLaunchers":MAX_LAUNCHERS})
            if u.path == "/api/workers":
                return self.send_json(workers_state())
            if u.path == "/api/jobs":
                return self.send_json({"jobs":list_jobs((q.get("status") or [None])[0], (q.get("limit") or [50])[0])})
            if u.path.startswith("/api/jobs/"):
                job_id = u.path.rsplit("/", 1)[-1]
                row = get_job(job_id, (q.get("maxBytes") or [65536])[0])
                return self.send_json(row if row is not None else {"error":"job not found"}, 200 if row is not None else 404)
            return self.send_json({"error":"not found"}, 404)
        except (ValueError, RuntimeError) as exc:
            return self.send_json({"error":str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error":str(exc)}, 500)
    def do_POST(self):
        u = urlparse(self.path)
        try:
            payload = self.read_json()
            if u.path == "/api/jobs":
                return self.send_json(submit_job(payload), 201)
            if u.path.startswith("/api/jobs/") and u.path.endswith("/cancel"):
                job_id = u.path.split("/")[-2]
                row = cancel_job(job_id)
                return self.send_json(row if row is not None else {"error":"job not found"}, 200 if row is not None else 404)
            return self.send_json({"error":"not found"}, 404)
        except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
            return self.send_json({"error":str(exc)}, 400)
        except Exception as exc:
            return self.send_json({"error":str(exc)}, 500)


def stop(_sig=None, _frame=None):
    STOP.set(); WAKE.set()


def main() -> int:
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, stop)
    db().close()
    thread = threading.Thread(target=scheduler_loop, daemon=True); thread.start()
    server = ThreadingHTTPServer((HOST, PORT), Handler); server.timeout = 1
    print(json.dumps({"event":"headless_job_scheduler_started","time":now_iso(),"host":HOST,"port":PORT,"maxLaunchers":MAX_LAUNCHERS}), flush=True)
    try:
        while not STOP.is_set(): server.handle_request()
    finally:
        server.server_close(); STOP.set(); WAKE.set(); thread.join(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
