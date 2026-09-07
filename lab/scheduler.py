#!/usr/bin/env python3
import base64
import html
import hmac
import json
import hashlib
import os
import secrets
import shlex
import sqlite3
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

SCHEDULER_VERSION = "2.2.0"
HOST = os.environ.get("CODEX_LAB_SCHEDULER_HOST", "0.0.0.0")
PORT = int(os.environ.get("CODEX_LAB_SCHEDULER_PORT", "8766"))
DB_PATH = Path(os.environ.get("CODEX_LAB_SCHEDULER_DB", "/tank/vm/codex-lab/scheduler.sqlite3"))
TRUSTED_CLIENTS = {x.strip() for x in os.environ.get("CODEX_LAB_SCHEDULER_TRUSTED_CLIENTS", "127.0.0.1,192.168.122.225").split(",") if x.strip()}
MAX_STATIONS = max(1, min(int(os.environ.get("CODEX_LAB_VM_MAX_STATIONS", "6")), 8))
GUEST_KEY = os.environ.get("CODEX_LAB_GUEST_KEY", "/home/mark/.ssh/id_ed25519_codex_lab_vm")
KNOWN_HOSTS = os.environ.get("CODEX_LAB_SCHEDULER_KNOWN_HOSTS", "/home/mark/.ssh/codex_lab_scheduler_known_hosts")
WORK_ROOT = os.environ.get("CODEX_LAB_WORK_ROOT", "/workspace/codex-jobs")
SSH_CONTROL_DIR = os.environ.get("CODEX_LAB_SSH_CONTROL_DIR", "/tmp/codex-lab-ssh")
POLL_SECONDS = max(0.2, float(os.environ.get("CODEX_LAB_SCHEDULER_POLL_SECONDS", "0.5")))
MAX_TAIL = 1024 * 1024
AUTH_PATH = Path(os.environ.get("CODEX_LAB_DASHBOARD_AUTH", "/tank/vm/codex-lab/dashboard-auth.json"))
SESSION_COOKIE = "CodexLabSession"
SESSION_SECONDS = max(900, min(int(os.environ.get("CODEX_LAB_DASHBOARD_SESSION_SECONDS", str(12 * 3600))), 7 * 24 * 3600))
PASSWORD_ITERATIONS = 600_000
DB_LOCK = threading.RLock()
AUTH_LOCK = threading.RLock()
LOGIN_LOCK = threading.Lock()
LOGIN_ATTEMPTS = {}
CPU_LOCK = threading.Lock()
CPU_SAMPLES = {}
SCHEDULER_HEALTH_LOCK = threading.Lock()
SCHEDULER_HEALTH = {
    "startedEpoch": time.time(),
    "lastLoopEpoch": 0.0,
    "lastDispatchEpoch": 0.0,
    "loops": 0,
    "errors": 0,
    "lastError": None,
    "lastErrorEpoch": None,
}
STOP = threading.Event()
WAKE = threading.Event()



def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _password_record(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS)
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": PASSWORD_ITERATIONS,
        "salt": _b64url(salt),
        "digest": _b64url(digest),
    }


def _write_auth(config):
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_PATH.with_name(AUTH_PATH.name + ".tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, AUTH_PATH)


def initialize_dashboard_auth(username, password, force=False):
    username = str(username or "").strip()
    if not username or not password:
        raise ValueError("username and password are required")
    with AUTH_LOCK:
        if AUTH_PATH.exists() and not force:
            raise FileExistsError(str(AUTH_PATH))
        config = {
            "version": 1,
            "username": username,
            "password": _password_record(password),
            "sessionSecret": _b64url(secrets.token_bytes(32)),
            "sessionVersion": 1,
            "updatedAt": now_iso(),
        }
        _write_auth(config)
        return {"username": username, "path": str(AUTH_PATH)}


def load_dashboard_auth():
    with AUTH_LOCK:
        if not AUTH_PATH.exists():
            return None
        try:
            config = json.loads(AUTH_PATH.read_text())
        except Exception:
            return None
    required = {"username", "password", "sessionSecret", "sessionVersion"}
    return config if required.issubset(config) else None


def verify_dashboard_password(username, password):
    config = load_dashboard_auth()
    if config is None or not hmac.compare_digest(str(username), str(config.get("username", ""))):
        # Spend comparable CPU even for an unknown username.
        hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), b"codex-unknown-user", PASSWORD_ITERATIONS)
        return False
    record = config.get("password") or {}
    if record.get("algorithm") != "pbkdf2_sha256":
        return False
    try:
        salt = _b64url_decode(record["salt"])
        expected = _b64url_decode(record["digest"])
        iterations = int(record.get("iterations", PASSWORD_ITERATIONS))
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password).encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def change_dashboard_password(username, current_password, new_password):
    if len(new_password) < 12:
        raise ValueError("New password must be at least 12 characters.")
    if not verify_dashboard_password(username, current_password):
        raise ValueError("Current password is incorrect.")
    with AUTH_LOCK:
        config = load_dashboard_auth()
        if config is None:
            raise RuntimeError("Dashboard authentication is not configured.")
        config["password"] = _password_record(new_password)
        config["sessionSecret"] = _b64url(secrets.token_bytes(32))
        config["sessionVersion"] = int(config.get("sessionVersion", 0)) + 1
        config["updatedAt"] = now_iso()
        _write_auth(config)
        return config


def make_session(config=None):
    config = config or load_dashboard_auth()
    if config is None:
        raise RuntimeError("Dashboard authentication is not configured.")
    now = int(time.time())
    payload = {
        "u": config["username"],
        "iat": now,
        "exp": now + SESSION_SECONDS,
        "v": int(config["sessionVersion"]),
        "csrf": _b64url(secrets.token_bytes(18)),
        "n": _b64url(secrets.token_bytes(12)),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    secret = _b64url_decode(config["sessionSecret"])
    sig = hmac.new(secret, raw, hashlib.sha256).digest()
    return _b64url(raw) + "." + _b64url(sig), payload


def verify_session(token):
    config = load_dashboard_auth()
    if config is None or not token or "." not in token:
        return None
    try:
        encoded, signature = token.split(".", 1)
        raw = _b64url_decode(encoded)
        provided = _b64url_decode(signature)
        secret = _b64url_decode(config["sessionSecret"])
        expected = hmac.new(secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(provided, expected):
            return None
        payload = json.loads(raw)
        if payload.get("u") != config["username"]:
            return None
        if int(payload.get("v", -1)) != int(config["sessionVersion"]):
            return None
        now = int(time.time())
        if int(payload.get("exp", 0)) <= now or int(payload.get("iat", now + 1)) > now + 60:
            return None
        return payload
    except Exception:
        return None


def login_rate_state(client_ip):
    now = time.monotonic()
    with LOGIN_LOCK:
        state = LOGIN_ATTEMPTS.get(client_ip, {"failures": [], "blockedUntil": 0.0})
        state["failures"] = [stamp for stamp in state["failures"] if now - stamp < 300]
        LOGIN_ATTEMPTS[client_ip] = state
        return max(0, int(state.get("blockedUntil", 0) - now))


def record_login_failure(client_ip):
    now = time.monotonic()
    with LOGIN_LOCK:
        state = LOGIN_ATTEMPTS.setdefault(client_ip, {"failures": [], "blockedUntil": 0.0})
        state["failures"] = [stamp for stamp in state["failures"] if now - stamp < 300]
        state["failures"].append(now)
        if len(state["failures"]) >= 5:
            penalty = min(300, 30 * (2 ** min(3, len(state["failures"]) - 5)))
            state["blockedUntil"] = max(state.get("blockedUntil", 0.0), now + penalty)


def clear_login_failures(client_ip):
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(client_ip, None)


def safe_next(value):
    value = str(value or "/")
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    return value[:2048]


def page_shell(title, body, extra_head=""):
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><meta name="robots" content="noindex,nofollow"><style>
:root{{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#07090d;color:#eef4fb}}*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;background:radial-gradient(circle at 15% 10%,#17345e55,transparent 35%),radial-gradient(circle at 85% 90%,#183f3350,transparent 40%),#07090d}}a{{color:#9bc9ff}}button,input{{font:inherit}}.auth-wrap{{min-height:100vh;display:grid;place-items:center;padding:28px}}.auth-card{{width:min(430px,100%);background:#10151dcc;border:1px solid #2a3442;border-radius:20px;box-shadow:0 24px 80px #0008;backdrop-filter:blur(18px);padding:30px}}.brand{{display:flex;gap:13px;align-items:center;margin-bottom:26px}}.mark{{width:45px;height:45px;border-radius:13px;display:grid;place-items:center;background:linear-gradient(145deg,#1d75d9,#36c782);font-weight:850;color:white;box-shadow:0 8px 28px #1875d744}}h1{{font-size:23px;margin:0 0 6px}}.sub{{margin:0;color:#9aa8b8;font-size:14px;line-height:1.5}}label{{display:block;font-size:12px;font-weight:700;color:#b9c4d0;margin:16px 0 7px}}input{{width:100%;padding:12px 13px;border:1px solid #344153;border-radius:10px;background:#090d13;color:#f7fbff;outline:none}}input:focus{{border-color:#5ba7ff;box-shadow:0 0 0 3px #2d84e526}}button{{width:100%;margin-top:19px;padding:12px 14px;border:0;border-radius:10px;background:linear-gradient(135deg,#287fdc,#31b97d);color:white;font-weight:800;cursor:pointer}}button:hover{{filter:brightness(1.08)}}.error,.success{{margin:15px 0 0;padding:10px 12px;border-radius:9px;font-size:13px}}.error{{background:#5b1c263d;border:1px solid #8f3544;color:#ffc4cc}}.success{{background:#18533543;border:1px solid #2f8057;color:#bff2d3}}.fine{{margin-top:18px;color:#7f8c9b;font-size:12px;line-height:1.5}}.links{{display:flex;justify-content:space-between;gap:12px;margin-top:18px;font-size:13px}}@media(max-width:520px){{.auth-card{{padding:24px 20px;border-radius:16px}}}}
</style>{extra_head}</head><body>{body}</body></html>"""


def login_html(next_path="/", error=None, username="mark", retry_after=0):
    message = ""
    if retry_after:
        message = f'<div class="error">Too many failed attempts. Try again in about {int(retry_after)} seconds.</div>'
    elif error:
        message = f'<div class="error">{html.escape(error)}</div>'
    body = f"""<main class="auth-wrap"><section class="auth-card"><div class="brand"><div class="mark">KM</div><div><h1>Codex KVM Lab</h1><p class="sub">Sign in to view and control the six persistent worker computers.</p></div></div><form method="post" action="/login" autocomplete="on"><input type="hidden" name="next" value="{html.escape(safe_next(next_path), quote=True)}"><label for="username">Username</label><input id="username" name="username" autocomplete="username" value="{html.escape(username, quote=True)}" required autofocus><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required>{message}<button type="submit">Sign in</button></form><p class="fine">Private Mark Shaw home infrastructure · authenticated sessions expire automatically.</p></section></main>"""
    return page_shell("Sign in · Codex KVM Lab", body)


def change_password_html(session, error=None, success=None):
    notice = f'<div class="error">{html.escape(error)}</div>' if error else (f'<div class="success">{html.escape(success)}</div>' if success else "")
    body = f"""<main class="auth-wrap"><section class="auth-card"><div class="brand"><div class="mark">KM</div><div><h1>Change password</h1><p class="sub">Update the login for the Codex KVM Lab.</p></div></div><form method="post" action="/change-password"><input type="hidden" name="csrf" value="{html.escape(session['csrf'], quote=True)}"><label for="current">Current password</label><input id="current" name="current_password" type="password" autocomplete="current-password" required><label for="new">New password</label><input id="new" name="new_password" type="password" autocomplete="new-password" minlength="12" required><label for="confirm">Confirm new password</label><input id="confirm" name="confirm_password" type="password" autocomplete="new-password" minlength="12" required>{notice}<button type="submit">Save new password</button></form><div class="links"><a href="/">Back to dashboard</a><a href="/logout">Sign out</a></div></section></main>"""
    return page_shell("Change password · Codex KVM Lab", body)

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def worker_ip(station):
    return f"192.168.122.{229 + int(station)}"


def worker_name(station):
    return f"codex-lab-vm-{int(station):02d}"


def browser_url(station):
    station = int(station)
    return f"https://browser.home.markshaw.ca/vm{station}/vnc.html?autoconnect=1&resize=scale&path=vm{station}/websockify"


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          owner TEXT NOT NULL,
          project TEXT,
          chat_label TEXT,
          chat_url TEXT,
          job_class TEXT NOT NULL,
          command TEXT NOT NULL,
          cwd TEXT NOT NULL,
          env_json TEXT NOT NULL,
          timeout_seconds INTEGER NOT NULL,
          repo_url TEXT,
          revision TEXT,
          status TEXT NOT NULL,
          requested_station INTEGER,
          station INTEGER,
          unit_name TEXT,
          exit_code INTEGER,
          error TEXT
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "chat_label" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN chat_label TEXT")
    if "chat_url" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN chat_url TEXT")
    if "requested_station" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN requested_station INTEGER")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs(finished_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_station_started ON jobs(station, started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_requested_status ON jobs(requested_station, status, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS worker_usage (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          ref_id TEXT NOT NULL,
          station INTEGER NOT NULL,
          started_at TEXT NOT NULL,
          finished_at TEXT,
          owner TEXT,
          project TEXT,
          chat_label TEXT,
          chat_url TEXT,
          status TEXT NOT NULL,
          source TEXT NOT NULL DEFAULT 'scheduler',
          UNIQUE(kind, ref_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_worker_usage_station_started ON worker_usage(station, started_at DESC)")
    # Backfill prior scheduled jobs once so history exists immediately after the migration.
    conn.execute(
        """
        INSERT OR IGNORE INTO worker_usage(kind,ref_id,station,started_at,finished_at,owner,project,chat_label,chat_url,status,source)
        SELECT 'job',id,station,COALESCE(started_at,created_at),finished_at,owner,project,chat_label,chat_url,status,'scheduler-backfill'
        FROM jobs WHERE station IS NOT NULL AND COALESCE(started_at,created_at) IS NOT NULL
        """
    )
    conn.commit()
    return conn


def run(args, timeout=30, input_text=None):
    return subprocess.run(args, input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def ssh_args(station):
    Path(SSH_CONTROL_DIR).mkdir(parents=True, exist_ok=True)
    Path(KNOWN_HOSTS).touch(exist_ok=True)
    return [
        "ssh", "-i", GUEST_KEY,
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=3",
        "-o", "ControlMaster=auto",
        "-o", "ControlPersist=600",
        "-o", f"ControlPath={SSH_CONTROL_DIR}/%C",
        f"mark@{worker_ip(station)}",
    ]


def ssh(station, command, timeout=30, input_text=None):
    return run([*ssh_args(station), command], timeout=timeout, input_text=input_text)


def virsh_state(station):
    r = run(["virsh", "-c", "qemu:///system", "domstate", worker_name(station)], timeout=5)
    return r.stdout.strip() if r.returncode == 0 else "absent"


def job_row(row):
    if row is None:
        return None
    out = dict(row)
    try:
        out["env"] = json.loads(out.pop("env_json", "{}"))
    except Exception:
        out["env"] = {}
    out["chatLabel"] = out.pop("chat_label", None)
    out["chatUrl"] = out.pop("chat_url", None)
    out["requestedStation"] = out.pop("requested_station", None)
    station = out.get("station")
    if station:
        out["worker"] = worker_name(station)
        out["workerIp"] = worker_ip(station)
        out["browserUrl"] = browser_url(station)
    return out


def public_job(row):
    job = job_row(row) if not isinstance(row, dict) or "env_json" in row else dict(row)
    if not job:
        return None
    keep = (
        "id", "created_at", "updated_at", "started_at", "finished_at", "owner", "project",
        "chatLabel", "chatUrl", "job_class", "status", "station", "worker", "workerIp",
        "browserUrl", "requestedStation", "exit_code", "error",
    )
    return {key: job.get(key) for key in keep if key in job}


def usage_row(row):
    if row is None:
        return None
    out = dict(row)
    out["chatLabel"] = out.pop("chat_label", None)
    out["chatUrl"] = out.pop("chat_url", None)
    return out


def record_usage_start(conn, kind, ref_id, station, owner=None, project=None, chat_label=None, chat_url=None, started_at=None, source="scheduler"):
    station = int(station)
    if not 1 <= station <= MAX_STATIONS:
        raise ValueError(f"station must be 1..{MAX_STATIONS}")
    stamp = started_at or now_iso()
    conn.execute(
        """
        INSERT INTO worker_usage(kind,ref_id,station,started_at,finished_at,owner,project,chat_label,chat_url,status,source)
        VALUES(?,?,?,?,?,?,?,?,?,'running',?)
        ON CONFLICT(kind,ref_id) DO UPDATE SET
          station=excluded.station,
          started_at=COALESCE(worker_usage.started_at,excluded.started_at),
          finished_at=NULL,
          owner=COALESCE(excluded.owner,worker_usage.owner),
          project=COALESCE(excluded.project,worker_usage.project),
          chat_label=COALESCE(excluded.chat_label,worker_usage.chat_label),
          chat_url=COALESCE(excluded.chat_url,worker_usage.chat_url),
          status='running',
          source=excluded.source
        """,
        (kind, str(ref_id), station, stamp, None, owner, project, chat_label, chat_url, source),
    )


def record_usage_end(conn, kind, ref_id, status, finished_at=None):
    stamp = finished_at or now_iso()
    conn.execute(
        "UPDATE worker_usage SET status=?, finished_at=COALESCE(finished_at,?) WHERE kind=? AND ref_id=?",
        (str(status), stamp, str(kind), str(ref_id)),
    )


def worker_usage_history(conn, station, limit=8):
    rows = conn.execute(
        "SELECT * FROM worker_usage WHERE station=? ORDER BY started_at DESC, id DESC LIMIT ?",
        (int(station), max(1, min(int(limit), 100))),
    ).fetchall()
    return [usage_row(row) for row in rows]


def record_external_usage(payload):
    action = str(payload.get("action") or "").strip()
    kind = str(payload.get("kind") or "lease").strip() or "lease"
    ref_id = str(payload.get("refId") or "").strip()
    station = int(payload.get("station") or 0)
    if not ref_id or not 1 <= station <= MAX_STATIONS:
        raise ValueError("refId and valid station are required")
    with DB_LOCK:
        conn = db()
        if action == "start":
            record_usage_start(
                conn, kind, ref_id, station,
                owner=str(payload.get("owner") or "").strip() or None,
                project=str(payload.get("project") or "").strip() or None,
                chat_label=str(payload.get("chatLabel") or "").strip() or None,
                chat_url=str(payload.get("chatUrl") or "").strip() or None,
                started_at=str(payload.get("startedAt") or "").strip() or None,
                source=str(payload.get("source") or "controller"),
            )
        elif action == "end":
            # If the start event was missed, create a minimal row before ending it.
            existing = conn.execute("SELECT 1 FROM worker_usage WHERE kind=? AND ref_id=?", (kind, ref_id)).fetchone()
            if not existing:
                record_usage_start(
                    conn, kind, ref_id, station,
                    owner=str(payload.get("owner") or "").strip() or None,
                    project=str(payload.get("project") or "").strip() or None,
                    chat_label=str(payload.get("chatLabel") or "").strip() or None,
                    chat_url=str(payload.get("chatUrl") or "").strip() or None,
                    started_at=str(payload.get("startedAt") or payload.get("finishedAt") or "").strip() or None,
                    source=str(payload.get("source") or "controller"),
                )
            record_usage_end(conn, kind, ref_id, str(payload.get("status") or "released"), str(payload.get("finishedAt") or "").strip() or None)
        else:
            conn.close()
            raise ValueError("action must be start or end")
        conn.commit()
        row = conn.execute("SELECT * FROM worker_usage WHERE kind=? AND ref_id=?", (kind, ref_id)).fetchone()
        conn.close()
    return usage_row(row)


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except Exception:
        return None


def percentile(values, p):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * float(p)
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    fraction = position - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def scheduler_metrics(conn, now_epoch=None):
    now_epoch = float(now_epoch or time.time())
    rows = [dict(row) for row in conn.execute(
        "SELECT created_at,started_at,finished_at,status,station FROM jobs WHERE created_at >= ? ORDER BY created_at",
        (datetime.fromtimestamp(now_epoch - 86400, timezone.utc).isoformat(),),
    )]
    windows = {}
    for label, seconds in (("5m", 300), ("1h", 3600), ("24h", 86400)):
        cutoff = now_epoch - seconds
        created = [row for row in rows if (parse_time(row.get("created_at")) or 0) >= cutoff]
        finished = [row for row in rows if row.get("finished_at") and (parse_time(row.get("finished_at")) or 0) >= cutoff]
        succeeded = sum(row.get("status") == "succeeded" for row in finished)
        failed = sum(row.get("status") in ("failed", "timed_out", "canceled") for row in finished)
        queue_ms = []
        runtime_ms = []
        busy_seconds = 0.0
        for row in created:
            created_ts = parse_time(row.get("created_at"))
            started_ts = parse_time(row.get("started_at"))
            finished_ts = parse_time(row.get("finished_at"))
            if created_ts is not None and started_ts is not None:
                queue_ms.append(max(0.0, (started_ts - created_ts) * 1000.0))
            if started_ts is not None:
                end_ts = finished_ts if finished_ts is not None else now_epoch
                runtime_ms.append(max(0.0, (end_ts - started_ts) * 1000.0))
                busy_seconds += max(0.0, min(now_epoch, end_ts) - max(cutoff, started_ts))
        windows[label] = {
            "submitted": len(created),
            "completed": len(finished),
            "succeeded": succeeded,
            "failed": failed,
            "successRatePercent": round(100.0 * succeeded / len(finished), 1) if finished else None,
            "jobsPerMinute": round(len(finished) / max(1.0, seconds / 60.0), 3),
            "avgQueueMs": round(sum(queue_ms) / len(queue_ms), 1) if queue_ms else None,
            "p95QueueMs": round(percentile(queue_ms, 0.95), 1) if queue_ms else None,
            "avgRuntimeMs": round(sum(runtime_ms) / len(runtime_ms), 1) if runtime_ms else None,
            "p95RuntimeMs": round(percentile(runtime_ms, 0.95), 1) if runtime_ms else None,
            "workerUtilizationPercent": round(100.0 * busy_seconds / (seconds * MAX_STATIONS), 1),
        }
    history = []
    start_minute = int((now_epoch - 29 * 60) // 60) * 60
    for index in range(30):
        bucket_start = start_minute + index * 60
        bucket_end = bucket_start + 60
        completed_rows = [row for row in rows if row.get("finished_at") and bucket_start <= (parse_time(row.get("finished_at")) or 0) < bucket_end]
        queue_values = []
        for row in rows:
            created_ts = parse_time(row.get("created_at"))
            started_ts = parse_time(row.get("started_at"))
            if created_ts is not None and started_ts is not None and bucket_start <= started_ts < bucket_end:
                queue_values.append(max(0.0, (started_ts - created_ts) * 1000.0))
        history.append({
            "time": datetime.fromtimestamp(bucket_start, timezone.utc).isoformat(),
            "completed": len(completed_rows),
            "failed": sum(row.get("status") in ("failed", "timed_out", "canceled") for row in completed_rows),
            "avgQueueMs": round(sum(queue_values) / len(queue_values), 1) if queue_values else 0.0,
        })
    queued = [row for row in rows if row.get("status") == "queued"]
    oldest_queued_ms = None
    if queued:
        oldest = min(parse_time(row.get("created_at")) or now_epoch for row in queued)
        oldest_queued_ms = round(max(0.0, (now_epoch - oldest) * 1000.0), 1)
    with SCHEDULER_HEALTH_LOCK:
        health = dict(SCHEDULER_HEALTH)
    loop_age_ms = round(max(0.0, (now_epoch - float(health.get("lastLoopEpoch") or now_epoch)) * 1000.0), 1)
    return {
        "loopHealthy": bool(health.get("lastLoopEpoch")) and loop_age_ms < max(5000.0, POLL_SECONDS * 6000.0),
        "loopAgeMs": loop_age_ms,
        "uptimeSeconds": round(max(0.0, now_epoch - float(health.get("startedEpoch") or now_epoch)), 1),
        "loops": int(health.get("loops") or 0),
        "errors": int(health.get("errors") or 0),
        "lastError": health.get("lastError"),
        "lastErrorAt": datetime.fromtimestamp(health["lastErrorEpoch"], timezone.utc).isoformat() if health.get("lastErrorEpoch") else None,
        "oldestQueuedMs": oldest_queued_ms,
        "windows": windows,
        "history": history,
    }


def active_counts(conn):
    return {int(row["station"]): int(row["n"]) for row in conn.execute(
        "SELECT station, COUNT(*) AS n FROM jobs WHERE status='running' AND station IS NOT NULL GROUP BY station"
    )}


def available_stations(conn):
    counts = active_counts(conn)
    candidates = [station for station in range(1, MAX_STATIONS + 1) if counts.get(station, 0) == 0]

    def available(station):
        if virsh_state(station) != "running":
            return None
        r = ssh(
            station,
            "test -e /var/lib/codex-worker-workstation-v1 && test ! -e /home/mark/.local/share/codex-worker/exclusive.lock && test ! -e /home/mark/.local/share/codex-worker/scheduler.lock",
            timeout=4,
        )
        return station if r.returncode == 0 else None

    with ThreadPoolExecutor(max_workers=max(1, len(candidates))) as pool:
        ready = [station for station in pool.map(available, candidates) if station is not None]
    last_used = {}
    for row in conn.execute("SELECT station, MAX(COALESCE(finished_at, started_at, created_at)) AS last_used FROM jobs WHERE station IS NOT NULL GROUP BY station"):
        last_used[int(row["station"])] = row["last_used"] or ""
    return sorted(ready, key=lambda station: (last_used.get(station, ""), station))


def remote_job_dir(job_id):
    return f"/home/mark/.local/share/codex-worker/jobs/{job_id}"


def build_job_script(job):
    job_id = job["id"]
    jdir = remote_job_dir(job_id)
    cwd = job["cwd"] or "/workspace"
    timeout_seconds = max(1, min(int(job["timeout_seconds"]), 86400))
    env = json.loads(job["env_json"] or "{}")
    command_b64 = base64.b64encode(job["command"].encode()).decode()
    lines = [
        "#!/bin/bash",
        "set +e",
        f"JOBDIR={shlex.quote(jdir)}",
        "mkdir -p \"$JOBDIR\"",
        "exec >\"$JOBDIR/stdout.log\" 2>\"$JOBDIR/stderr.log\"",
        "date -u +%FT%TZ >\"$JOBDIR/started\"",
    ]
    repo_url = job["repo_url"]
    revision = job["revision"]
    if repo_url:
        repo_dir = f"{WORK_ROOT}/{job_id}/repo"
        mirror_key = hashlib.sha256(repo_url.encode()).hexdigest()[:24]
        mirror_dir = f"/home/mark/.cache/codex-worker/git-mirrors/{mirror_key}.git"
        lines += [
            f"REPO={shlex.quote(repo_dir)}",
            f"MIRROR={shlex.quote(mirror_dir)}",
            'mkdir -p "$(dirname "$REPO")" "$(dirname "$MIRROR")"',
            f'if [ ! -d "$MIRROR" ]; then git clone --mirror {shlex.quote(repo_url)} "$MIRROR"; else git --git-dir="$MIRROR" remote update --prune; fi',
            "mirror_rc=$?",
            'if [ "$mirror_rc" -ne 0 ]; then echo "$mirror_rc" >"$JOBDIR/rc"; exit "$mirror_rc"; fi',
            f'git clone --reference-if-able "$MIRROR" --no-checkout {shlex.quote(repo_url)} "$REPO"',
            "clone_rc=$?",
            'if [ "$clone_rc" -ne 0 ]; then echo "$clone_rc" >"$JOBDIR/rc"; exit "$clone_rc"; fi',
            'cd "$REPO"',
        ]
        if revision:
            lines += [
                f"git fetch --depth=1 origin {shlex.quote(revision)}",
                "fetch_rc=$?",
                'if [ "$fetch_rc" -ne 0 ]; then echo "$fetch_rc" >"$JOBDIR/rc"; exit "$fetch_rc"; fi',
                "git checkout --detach FETCH_HEAD",
            ]
        if cwd not in ("", "/workspace", "."):
            suffix = cwd.lstrip("/")
            lines.append(f'cd "$REPO"/{shlex.quote(suffix)}')
    else:
        lines.append(f"cd {shlex.quote(cwd)}")
    for key, value in env.items():
        if not str(key).replace("_", "").isalnum() or str(key)[0].isdigit():
            continue
        lines.append(f"export {key}={shlex.quote(str(value))}")
    lines += [
        f"COMMAND_B64={shlex.quote(command_b64)}",
        "COMMAND=$(printf '%s' \"$COMMAND_B64\" | base64 -d)",
        f"timeout --signal=TERM --kill-after=10 {timeout_seconds}s /bin/bash -lc \"$COMMAND\"",
        "rc=$?",
        "printf '%s\\n' \"$rc\" >\"$JOBDIR/rc\"",
        "date -u +%FT%TZ >\"$JOBDIR/finished\"",
        "exit \"$rc\"",
    ]
    return "\n".join(lines) + "\n"


def start_job(conn, job, station):
    job_id = job["id"]
    unit = f"codex-job-{job_id[:16]}"
    script = build_job_script(job)
    script_b64 = base64.b64encode(script.encode()).decode()
    jdir = remote_job_dir(job_id)
    remote = f"""
set -eu
mkdir -p {shlex.quote(jdir)} /home/mark/.local/share/codex-worker
CLAIM=/home/mark/.local/share/codex-worker/claim.lock
if ! flock -n "$CLAIM" sh -c 'test ! -e /home/mark/.local/share/codex-worker/exclusive.lock && printf "%s\n" {job_id} > /home/mark/.local/share/codex-worker/scheduler.lock'; then
  exit 75
fi
printf '%s' {shlex.quote(script_b64)} | base64 -d > {shlex.quote(jdir + '/run.sh')}
chmod 700 {shlex.quote(jdir + '/run.sh')}
if ! sudo systemd-run --quiet --unit={shlex.quote(unit)} --uid=mark --gid=mark --property=Nice=5 --property=CPUWeight=80 --collect /bin/bash {shlex.quote(jdir + '/run.sh')}; then
  rm -f /home/mark/.local/share/codex-worker/scheduler.lock
  exit 1
fi
"""
    r = ssh(station, "bash -s", timeout=15, input_text=remote)
    stamp = now_iso()
    if r.returncode != 0:
        conn.execute("UPDATE jobs SET status='queued', updated_at=?, error=? WHERE id=?", (stamp, (r.stderr or r.stdout).strip()[:2000], job_id))
        conn.commit()
        return False
    conn.commit()
    current = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    if current is None or current["status"] != "queued":
        ssh(station, f"sudo systemctl stop {shlex.quote(unit)} 2>/dev/null || true; rm -f /home/mark/.local/share/codex-worker/scheduler.lock", timeout=8)
        return False
    conn.execute(
        "UPDATE jobs SET status='running', station=?, unit_name=?, started_at=?, updated_at=?, error=NULL WHERE id=?",
        (station, unit, stamp, stamp, job_id),
    )
    record_usage_start(
        conn, "job", job_id, station,
        owner=job.get("owner"), project=job.get("project"),
        chat_label=job.get("chat_label"), chat_url=job.get("chat_url"),
        started_at=stamp, source="scheduler",
    )
    conn.commit()
    return True


def reconcile_running(conn):
    rows = [dict(row) for row in conn.execute("SELECT * FROM jobs WHERE status='running' ORDER BY started_at")]
    if not rows:
        return

    def probe(job):
        station = int(job["station"])
        jdir = remote_job_dir(job["id"])
        command = (
            f"if [ -f {shlex.quote(jdir + '/rc')} ]; then "
            f"cat {shlex.quote(jdir + '/rc')}; rm -f /home/mark/.local/share/codex-worker/scheduler.lock; "
            "else printf RUNNING; fi"
        )
        r = ssh(station, command, timeout=5)
        return job, r

    with ThreadPoolExecutor(max_workers=min(MAX_STATIONS, len(rows))) as pool:
        results = list(pool.map(probe, rows))

    stamp = now_iso()
    for job, r in results:
        if r.returncode != 0:
            continue
        value = r.stdout.strip()
        if value in ("RUNNING", ""):
            continue
        try:
            rc = int(value.splitlines()[-1])
        except ValueError:
            continue
        status = "succeeded" if rc == 0 else ("timed_out" if rc == 124 else "failed")
        conn.execute(
            "UPDATE jobs SET status=?, exit_code=?, finished_at=?, updated_at=? WHERE id=?",
            (status, rc, stamp, stamp, job["id"]),
        )
        record_usage_end(conn, "job", job["id"], status, stamp)
    conn.commit()


def plan_assignments(queued, stations):
    remaining = list(stations)
    assignments = []
    # A pinned job reserves its requested worker before automatic jobs are
    # placed. Other workers remain available, so pinning one station never
    # serializes or blocks the rest of the fleet.
    for job in queued:
        requested = job.get("requested_station")
        if requested is None:
            continue
        requested = int(requested)
        if requested not in remaining:
            continue
        assignments.append((job, requested))
        remaining.remove(requested)
        if not remaining:
            return assignments
    for job in queued:
        if not remaining:
            break
        if job.get("requested_station") is not None:
            continue
        station = remaining.pop(0)
        assignments.append((job, station))
    return assignments


def dispatch(conn):
    stations = available_stations(conn)
    if not stations:
        return
    queued = [dict(row) for row in conn.execute(
        "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at"
    )]
    assignments = plan_assignments(queued, stations)
    if not assignments:
        return

    def launch(assignment):
        job, station = assignment
        thread_conn = db()
        try:
            return start_job(thread_conn, job, station)
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=len(assignments)) as pool:
        list(pool.map(launch, assignments))


def scheduler_loop():
    conn = db()
    while not STOP.is_set():
        with SCHEDULER_HEALTH_LOCK:
            SCHEDULER_HEALTH["lastLoopEpoch"] = time.time()
            SCHEDULER_HEALTH["loops"] += 1
        try:
            reconcile_running(conn)
            dispatch(conn)
            with SCHEDULER_HEALTH_LOCK:
                SCHEDULER_HEALTH["lastDispatchEpoch"] = time.time()
        except Exception as exc:
            with SCHEDULER_HEALTH_LOCK:
                SCHEDULER_HEALTH["errors"] += 1
                SCHEDULER_HEALTH["lastError"] = str(exc)[:1000]
                SCHEDULER_HEALTH["lastErrorEpoch"] = time.time()
            print(json.dumps({"event": "scheduler_error", "time": now_iso(), "error": str(exc)}), flush=True)
        WAKE.wait(POLL_SECONDS)
        WAKE.clear()
    conn.close()


def worker_probe(station, active_job=None):
    state = virsh_state(station)
    out = {
        "station": station,
        "name": worker_name(station),
        "ip": worker_ip(station),
        "state": state,
        "browserUrl": browser_url(station),
        "activeJob": active_job,
    }
    if state != "running":
        out.update({"ready": False, "browserReady": False, "workstationReady": False, "dockerReady": False})
        return out
    command = r'''printf 'HOST '; hostname
printf 'LOAD '; cut -d' ' -f1-3 /proc/loadavg
printf 'MEM '; awk '/MemTotal:/{t=$2}/MemAvailable:/{a=$2}END{print t,a}' /proc/meminfo
printf 'CPU '; awk '/^cpu /{for(i=2;i<=11;i++)printf "%s%s",$i,(i==11?ORS:OFS);exit}' /proc/stat
printf 'DISK '; df -Pk / | awk 'NR==2{print $2,$4}'
printf 'UPTIME '; awk '{print int($1)}' /proc/uptime
printf 'VCPUS '; nproc
printf 'BROWSER '; (ss -ltn | grep -q ':6080 ' && ss -ltn | grep -q ':9222 ') && echo 1 || echo 0
printf 'DOCKER '; systemctl is-active --quiet docker && echo 1 || echo 0
printf 'WORKSTATION '; test -e /var/lib/codex-worker-workstation-v1 && echo 1 || echo 0
printf 'EXCLUSIVE '; test -e /home/mark/.local/share/codex-worker/exclusive.lock && echo 1 || echo 0
printf 'LEASE64 '; if [ -s /home/mark/.local/share/codex-worker/exclusive.lock ]; then base64 -w0 /home/mark/.local/share/codex-worker/exclusive.lock; fi; echo
printf 'SCHEDULER '; test -e /home/mark/.local/share/codex-worker/scheduler.lock && echo 1 || echo 0'''
    r = ssh(station, command, timeout=5)
    if r.returncode != 0:
        out.update({"ready": False, "browserReady": False, "workstationReady": False, "dockerReady": False, "probeError": (r.stderr or r.stdout).strip()[:500]})
        return out
    values = {}
    for line in r.stdout.splitlines():
        key, _, val = line.partition(" ")
        values[key] = val.strip()
    load = values.get("LOAD", "").split()
    mem = values.get("MEM", "").split()
    disk = values.get("DISK", "").split()
    cpu_values = [int(x) for x in values.get("CPU", "").split() if x.isdigit()]
    cpu_percent = None
    if len(cpu_values) >= 5:
        total = sum(cpu_values)
        idle = cpu_values[3] + cpu_values[4]
        with CPU_LOCK:
            previous = CPU_SAMPLES.get(station)
            CPU_SAMPLES[station] = (total, idle)
        if previous and total > previous[0]:
            total_delta = total - previous[0]
            idle_delta = max(0, idle - previous[1])
            cpu_percent = round(max(0.0, min(100.0, 100.0 * (1.0 - idle_delta / total_delta))), 1)
    mem_total_kib = int(mem[0]) if len(mem) > 0 else 0
    mem_available_kib = int(mem[1]) if len(mem) > 1 else 0
    disk_total_kib = int(disk[0]) if len(disk) > 0 else 0
    disk_available_kib = int(disk[1]) if len(disk) > 1 else 0
    vcpus = int(values.get("VCPUS", "0") or 0)
    if cpu_percent is None and load and vcpus:
        cpu_percent = round(max(0.0, min(100.0, float(load[0]) / vcpus * 100.0)), 1)
    mem_used_percent = round(100.0 * (1.0 - mem_available_kib / mem_total_kib), 1) if mem_total_kib else None
    disk_used_percent = round(100.0 * (1.0 - disk_available_kib / disk_total_kib), 1) if disk_total_kib else None
    workstation_ready = values.get("WORKSTATION") == "1"
    docker_ready = values.get("DOCKER") == "1"
    browser_ready = values.get("BROWSER") == "1"
    lease = None
    if values.get("LEASE64"):
        try:
            lease = json.loads(base64.b64decode(values["LEASE64"]).decode("utf-8"))
        except Exception:
            lease = {"invalid": True}
    out.update({
        "ready": True,
        "hostname": values.get("HOST") or worker_name(station),
        "browserReady": browser_ready,
        "workstationReady": workstation_ready,
        "dockerReady": docker_ready,
        "exclusive": values.get("EXCLUSIVE") == "1",
        "lease": lease,
        "schedulerBusy": values.get("SCHEDULER") == "1",
        "load1": float(load[0]) if load else None,
        "load5": float(load[1]) if len(load) > 1 else None,
        "cpuPercent": cpu_percent,
        "vcpus": vcpus,
        "memTotalMiB": round(mem_total_kib / 1024, 1),
        "memAvailableMiB": round(mem_available_kib / 1024, 1),
        "memUsedPercent": mem_used_percent,
        "diskTotalGiB": round(disk_total_kib / 1024 / 1024, 1),
        "diskAvailableGiB": round(disk_available_kib / 1024 / 1024, 1),
        "diskUsedPercent": disk_used_percent,
        "uptimeSeconds": int(values.get("UPTIME", "0") or 0),
    })
    return out


def state_payload():
    with DB_LOCK:
        conn = db()
        active = {int(row["station"]): public_job(row) for row in conn.execute("SELECT * FROM jobs WHERE status='running' AND station IS NOT NULL")}
        recent = [public_job(row) for row in conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 20")]
        queued = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0]
        metrics = scheduler_metrics(conn)
        usage = {station: worker_usage_history(conn, station, 8) for station in range(1, MAX_STATIONS + 1)}
        pinned_queued = {station: [] for station in range(1, MAX_STATIONS + 1)}
        for row in conn.execute("SELECT * FROM jobs WHERE status='queued' AND requested_station IS NOT NULL ORDER BY created_at"):
            station = int(row["requested_station"])
            if station in pinned_queued:
                pinned_queued[station].append(public_job(row))
        conn.close()
    with ThreadPoolExecutor(max_workers=MAX_STATIONS) as pool:
        workers = list(pool.map(lambda station: worker_probe(station, active.get(station)), range(1, MAX_STATIONS + 1)))
    for worker in workers:
        station = int(worker["station"])
        worker["history"] = usage.get(station, [])
        worker["queuedPinned"] = pinned_queued.get(station, [])
    ready = sum(bool(worker.get("ready")) for worker in workers)
    workstation_ready = sum(bool(worker.get("workstationReady")) for worker in workers)
    browser_ready = sum(bool(worker.get("browserReady")) for worker in workers)
    busy = sum(bool(worker.get("activeJob") or worker.get("exclusive") or worker.get("schedulerBusy")) for worker in workers)
    free = sum(bool(worker.get("ready") and worker.get("workstationReady") and not (worker.get("activeJob") or worker.get("exclusive") or worker.get("schedulerBusy"))) for worker in workers)
    return {
        "time": now_iso(),
        "maxStations": MAX_STATIONS,
        "queuedJobs": queued,
        "workers": workers,
        "recentJobs": recent,
        "metrics": metrics,
        "scheduler": {
            "mode": "automatic-six-way",
            "healthy": ready == MAX_STATIONS and workstation_ready == MAX_STATIONS and metrics.get("loopHealthy", False),
            "capacity": MAX_STATIONS,
            "ready": ready,
            "workstationsReady": workstation_ready,
            "browsersReady": browser_ready,
            "busy": busy,
            "free": free,
            "queued": queued,
            "pollSeconds": POLL_SECONDS,
        },
    }


def submit_job(payload):
    owner = str(payload.get("owner") or "").strip()
    command = str(payload.get("command") or "").strip()
    if not owner or not command:
        raise ValueError("owner and command are required")
    requested_station = payload.get("requestedStation")
    if requested_station not in (None, ""):
        requested_station = int(requested_station)
        if not 1 <= requested_station <= MAX_STATIONS:
            raise ValueError(f"requestedStation must be 1..{MAX_STATIONS}")
    else:
        requested_station = None
    job_id = uuid.uuid4().hex
    stamp = now_iso()
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    record = (
        job_id, stamp, stamp, owner, str(payload.get("project") or "") or None,
        str(payload.get("chatLabel") or "").strip() or None,
        str(payload.get("chatUrl") or "").strip() or None,
        str(payload.get("jobClass") or "cpu"), command, str(payload.get("cwd") or "/workspace"),
        json.dumps(env, separators=(",", ":")), max(1, min(int(payload.get("timeout", 3600)), 86400)),
        str(payload.get("repoUrl") or "") or None, str(payload.get("revision") or "") or None, "queued", requested_station,
    )
    with DB_LOCK:
        conn = db()
        conn.execute(
            "INSERT INTO jobs(id,created_at,updated_at,owner,project,chat_label,chat_url,job_class,command,cwd,env_json,timeout_seconds,repo_url,revision,status,requested_station) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            record,
        )
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
    WAKE.set()
    return job_row(row)


def get_job(job_id, include_tail=True, max_bytes=65536):
    with DB_LOCK:
        conn = db()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        conn.close()
    result = job_row(row)
    if result is None:
        return None
    if include_tail and result.get("station"):
        limit = max(1024, min(int(max_bytes), MAX_TAIL))
        jdir = remote_job_dir(job_id)
        cmd = f"tail -c {limit} {shlex.quote(jdir + '/stdout.log')} 2>/dev/null || true; printf '\\n---STDERR---\\n'; tail -c {limit} {shlex.quote(jdir + '/stderr.log')} 2>/dev/null || true"
        r = ssh(result["station"], cmd, timeout=5)
        stdout, _, stderr = r.stdout.partition("\n---STDERR---\n")
        result["stdoutTail"] = stdout
        result["stderrTail"] = stderr
    return result


def list_jobs(status=None, limit=50):
    limit = max(1, min(int(limit), 200))
    with DB_LOCK:
        conn = db()
        if status:
            rows = conn.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
    return [job_row(row) for row in rows]


def cancel_job(job_id):
    with DB_LOCK:
        conn = db()
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            conn.close()
            return None
        if row["status"] == "running" and row["station"] and row["unit_name"]:
            station = int(row["station"])
            ssh(station, f"sudo systemctl stop {shlex.quote(row['unit_name'])} 2>/dev/null || true; rm -f /home/mark/.local/share/codex-worker/scheduler.lock", timeout=8)
        stamp = now_iso()
        conn.execute("UPDATE jobs SET status='canceled', finished_at=?, updated_at=? WHERE id=?", (stamp, stamp, job_id))
        if row["station"]:
            record_usage_end(conn, "job", job_id, "canceled", stamp)
        conn.commit()
        out = job_row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        conn.close()
        WAKE.set()
        return out


def dashboard_history(station, limit=50):
    station = int(station)
    if not 1 <= station <= MAX_STATIONS:
        raise ValueError(f"station must be 1..{MAX_STATIONS}")
    with DB_LOCK:
        conn = db()
        rows = worker_usage_history(conn, station, limit)
        conn.close()
    return {"station": station, "history": rows}


def dashboard_job(job_id, max_bytes=32768):
    job = get_job(str(job_id), include_tail=True, max_bytes=max_bytes)
    if not job:
        return None
    public = public_job(job)
    public["stdoutTail"] = job.get("stdoutTail", "")
    public["stderrTail"] = job.get("stderrTail", "")
    return public


def submit_worker_job(station, payload):
    station = int(station)
    if not 1 <= station <= MAX_STATIONS:
        raise ValueError(f"station must be 1..{MAX_STATIONS}")
    body = dict(payload or {})
    body["requestedStation"] = station
    return public_job(submit_job(body))


def worker_control(station, action):
    station = int(station)
    if not 1 <= station <= MAX_STATIONS:
        raise ValueError(f"station must be 1..{MAX_STATIONS}")
    if action == "cancel-job":
        with DB_LOCK:
            conn = db()
            row = conn.execute("SELECT id FROM jobs WHERE station=? AND status='running' ORDER BY started_at DESC LIMIT 1", (station,)).fetchone()
            conn.close()
        if not row:
            return {"ok": True, "station": station, "action": action, "message": "No running scheduled job."}
        job = cancel_job(row["id"])
        return {"ok": True, "station": station, "action": action, "job": public_job(job) if job else None}
    if action == "release-lease":
        before = ssh(station, "cat /home/mark/.local/share/codex-worker/exclusive.lock 2>/dev/null || true", timeout=5)
        lease = None
        if before.stdout.strip():
            try:
                lease = json.loads(before.stdout)
            except Exception:
                lease = {"raw": before.stdout.strip()[:500]}
        result = ssh(
            station,
            "mkdir -p /home/mark/.local/share/codex-worker; flock -n /home/mark/.local/share/codex-worker/claim.lock sh -c 'rm -f /home/mark/.local/share/codex-worker/exclusive.lock'",
            timeout=6,
        )
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Could not release interactive lease.")
        WAKE.set()
        return {"ok": True, "station": station, "action": action, "lease": lease}
    if action == "launch-terminal":
        command = (
            "setsid -f env DISPLAY=:99 xterm -fa Monospace -fs 11 -geometry 120x38+24+52 "
            f"-title {shlex.quote('Codex Worker ' + str(station) + ' Terminal')} >/dev/null 2>&1"
        )
        result = ssh(station, command, timeout=6)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Could not launch terminal.")
        return {"ok": True, "station": station, "action": action, "browserUrl": browser_url(station)}
    if action == "restart-browser":
        result = ssh(station, "sudo systemctl restart codex-worker-desktop.service", timeout=12)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip() or "Could not restart browser desktop.")
        return {"ok": True, "station": station, "action": action, "browserUrl": browser_url(station)}
    raise ValueError(f"unknown worker action: {action}")


def dashboard_html(session):
    cards = "".join(f"""
      <section class="worker" id="worker-{i}">
        <header class="worker-head">
          <span class="dot"></span><strong>VM {i}</strong><span class="worker-state">checking</span>
          <span class="worker-owner">Idle</span><span class="spacer"></span>
          <a class="screen-link" target="_blank" href="/vm{i}/vnc.html?autoconnect=1&resize=scale&path=vm{i}/websockify">full screen</a>
        </header>
        <iframe src="/vm{i}/vnc.html?autoconnect=1&resize=scale&path=vm{i}/websockify" loading="eager" title="Codex worker {i}"></iframe>
        <div class="cockpit">
          <div class="identity-row">
            <div class="identity-main"><span>Current bot / chat</span><strong class="identity-owner">Idle</strong><small class="identity-project">No bot assigned</small></div>
            <a class="identity-chat" target="_blank" rel="noreferrer" hidden>open chat</a>
            <div class="activity">No active job</div>
          </div>
          <div class="status-row">
            <div class="meter"><span>CPU</span><b class="cpu">—</b></div>
            <div class="meter"><span>RAM</span><b class="ram">—</b></div>
            <div class="meter"><span>Disk</span><b class="disk">—</b></div>
            <div class="meter"><span>Uptime</span><b class="uptime">—</b></div>
            <div class="services"><span class="svc workstation">Linux</span><span class="svc docker">Docker</span><span class="svc browser">Browser</span></div>
          </div>
          <div class="worker-actions">
            <a class="mini" target="_blank" href="/vm{i}/vnc.html?autoconnect=1&resize=scale&path=vm{i}/websockify">Desktop</a>
            <button class="mini terminal" data-station="{i}" data-action="launch-terminal">Terminal</button>
            <button class="mini primary run-job" data-station="{i}" data-view="submit">Run job on VM {i}</button>
            <button class="mini history" data-station="{i}" data-view="history">History</button>
            <button class="mini logs" data-station="{i}" data-view="logs" hidden>Job logs</button>
            <button class="mini restart" data-station="{i}" data-action="restart-browser">Restart browser</button>
            <button class="mini danger cancel" data-station="{i}" data-action="cancel-job" hidden>Cancel job</button>
            <button class="mini danger release" data-station="{i}" data-action="release-lease" hidden>Release lease</button>
          </div>
          <div class="history-inline"><span class="history-label">Recent use</span><div class="history-items"><span class="history-empty">No history yet</span></div></div>
        </div>
      </section>""" for i in range(1, MAX_STATIONS + 1))
    csrf_json = json.dumps(str(session.get("csrf") or ""))
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex KVM Lab</title><style>
:root{{color-scheme:dark;background:#0b0d10;color:#e7edf5;font-family:Inter,system-ui,sans-serif}}*{{box-sizing:border-box}}body{{margin:0;background:#0b0d10}}button,input,textarea,select{{font:inherit}}nav{{position:sticky;top:0;z-index:9;background:#11161def;padding:10px 14px;backdrop-filter:blur(10px);display:flex;gap:14px;align-items:center;border-bottom:1px solid #27303a}}nav h1{{font-size:16px;margin:0}}#summary{{font-size:12px;color:#aab6c3}}#dispatch{{font-size:11px;padding:4px 8px;border:1px solid #31503d;border-radius:999px;background:#153221;color:#9ef0bd}}#dispatch.bad{{border-color:#713744;background:#391b23;color:#ffc0c9}}nav a{{color:#9dc9ff;text-decoration:none}}nav .spacer,.worker-head .spacer{{margin-left:auto}}.section{{padding:12px 10px 2px}}.section-title{{display:flex;align-items:center;gap:10px;margin:0 2px 9px}}.section-title h2{{font-size:13px;margin:0}}.section-title span{{font-size:11px;color:#8190a0}}.metrics{{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin-bottom:8px}}.metric{{background:#11161d;border:1px solid #27303a;border-radius:10px;padding:10px;min-width:0}}.metric span{{display:block;color:#7e8c9c;font-size:9px;text-transform:uppercase;letter-spacing:.06em}}.metric strong{{display:block;font-size:18px;margin-top:3px}}.metric small{{display:block;color:#8290a0;font-size:9px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.metric.good strong{{color:#80e0a8}}.metric.warn strong{{color:#ffd078}}.metric.bad strong{{color:#ff8995}}.graph-wrap{{background:#11161d;border:1px solid #27303a;border-radius:10px;padding:10px}}.graph-head{{display:flex;gap:16px;align-items:center;font-size:10px;color:#8391a1;margin-bottom:4px}}.legend{{display:inline-flex;gap:4px;align-items:center}}.legend i{{width:8px;height:8px;border-radius:2px;background:#3895e8}}.legend.queue i{{background:#45d08b}}.legend.fail i{{background:#e65e70}}#throughput-svg{{display:block;width:100%;height:120px;overflow:visible}}main{{padding:10px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}}.worker{{background:#11161d;border:1px solid #27303a;border-radius:12px;overflow:hidden;min-width:0;box-shadow:0 8px 30px #0002}}.worker-head{{min-height:38px;display:flex;align-items:center;gap:7px;padding:0 10px;font-size:11px;border-bottom:1px solid #202832}}.worker-head strong{{font-size:13px}}.worker-state{{color:#8593a3}}.worker-owner{{color:#b5c5d6;max-width:130px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.screen-link{{color:#9dc9ff;text-decoration:none}}.dot{{width:8px;height:8px;border-radius:50%;background:#7d8793;flex:0 0 auto}}.ready .dot{{background:#36d37e;box-shadow:0 0 8px #36d37e88}}.busy .dot{{background:#ffc857;box-shadow:0 0 8px #ffc85788}}.down .dot{{background:#ff5d6c}}.installing .dot{{background:#5ba7ff;box-shadow:0 0 8px #5ba7ff88}}iframe{{display:block;width:100%;aspect-ratio:16/10;border:0;background:#050607}}.cockpit{{border-top:1px solid #202832}}.identity-row{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 10px;padding:9px 10px;background:#0e1319}}.identity-main span{{display:block;color:#728090;font-size:8px;text-transform:uppercase;letter-spacing:.06em}}.identity-main strong{{display:block;font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.identity-main small{{display:block;color:#8795a5;font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.identity-chat{{grid-column:2;grid-row:1/2;align-self:center;color:#8fc5ff;font-size:9px;text-decoration:none}}.activity{{grid-column:1/3;color:#9aa8b7;font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.status-row{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr)) auto;gap:5px;padding:7px 10px;align-items:stretch}}.meter{{background:#0b0f14;border-radius:6px;padding:5px}}.meter span{{display:block;color:#728090;font-size:8px;text-transform:uppercase;letter-spacing:.05em}}.meter b{{font-size:10px}}.services{{display:flex;gap:3px;align-items:center;justify-content:flex-end;flex-wrap:wrap}}.svc{{font-size:8px;border:1px solid #3a4654;border-radius:999px;padding:2px 5px;color:#7e8b99}}.svc.ok{{border-color:#2c6a49;color:#8edcae;background:#173122}}.worker-actions{{display:flex;gap:5px;flex-wrap:wrap;padding:6px 10px 8px}}.mini{{appearance:none;border:1px solid #344354;background:#151c25;color:#b9d6f5;border-radius:6px;padding:5px 7px;font-size:9px;text-decoration:none;cursor:pointer}}.mini:hover{{background:#1c2734}}.mini:disabled{{opacity:.4;cursor:not-allowed}}.mini.primary{{border-color:#2c6a49;background:#153221;color:#a9efc3}}.mini.danger{{border-color:#65343e;color:#ffb5bf;background:#2a171b}}.history-inline{{border-top:1px solid #202832;padding:7px 10px;display:grid;grid-template-columns:auto 1fr;gap:8px;align-items:start;min-height:34px}}.history-label{{font-size:8px;text-transform:uppercase;letter-spacing:.06em;color:#728090;padding-top:2px}}.history-items{{display:flex;gap:5px;min-width:0;overflow:hidden}}.history-chip{{border:1px solid #2c3743;background:#0c1117;border-radius:6px;padding:3px 5px;min-width:0;max-width:32%;font-size:8px;color:#a9b7c5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.history-chip a{{color:#8fc5ff;text-decoration:none}}.history-empty{{font-size:8px;color:#65717e;padding-top:2px}}#toast{{position:fixed;right:16px;bottom:16px;z-index:40;max-width:360px;background:#16202b;border:1px solid #3a4a5c;border-radius:9px;padding:10px 12px;font-size:11px;box-shadow:0 12px 40px #0008;display:none}}#toast.bad{{border-color:#7d3947;background:#32191e;color:#ffc5cc}}.modal{{position:fixed;inset:0;z-index:30;background:#000a;display:none;place-items:center;padding:20px}}.modal.open{{display:grid}}.modal-card{{width:min(920px,100%);max-height:90vh;background:#0e141b;border:1px solid #344252;border-radius:12px;box-shadow:0 24px 80px #000c;overflow:hidden;display:flex;flex-direction:column}}.modal-card.narrow{{width:min(700px,100%)}}.modal-head{{display:flex;gap:10px;align-items:center;padding:10px 12px;border-bottom:1px solid #28323e;font-size:11px}}.modal-head strong{{font-size:12px}}.modal-head .spacer{{flex:1}}.modal pre{{margin:0;padding:12px;overflow:auto;min-height:240px;white-space:pre-wrap;word-break:break-word;font:11px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;color:#cdd8e4}}.form-grid{{padding:12px;display:grid;grid-template-columns:1fr 1fr;gap:10px;overflow:auto}}.field{{display:flex;flex-direction:column;gap:4px}}.field.wide{{grid-column:1/3}}.field label{{font-size:9px;color:#8997a7}}.field input,.field textarea,.field select{{width:100%;background:#090d12;border:1px solid #344150;color:#dce5ef;border-radius:7px;padding:7px;font-size:10px}}.field textarea{{min-height:150px;resize:vertical;font-family:ui-monospace,SFMono-Regular,Consolas,monospace}}.form-note{{font-size:9px;color:#8391a0;grid-column:1/3}}.form-actions{{grid-column:1/3;display:flex;justify-content:flex-end;gap:8px}}.history-modal-list{{padding:8px 12px 14px;overflow:auto}}.history-row{{display:grid;grid-template-columns:110px 80px minmax(0,1fr) auto;gap:8px;align-items:center;border-bottom:1px solid #202832;padding:7px 0;font-size:9px}}.history-row .when{{color:#758291}}.history-row .kind{{text-transform:uppercase;color:#8ba1b7}}.history-row strong{{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.history-row small{{display:block;color:#7f8d9b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.history-row a{{color:#8fc5ff;text-decoration:none}}@media(max-width:1400px){{.metrics{{grid-template-columns:repeat(3,1fr)}}main{{grid-template-columns:repeat(2,minmax(0,1fr))}}}}@media(max-width:850px){{main{{grid-template-columns:1fr}}.status-row{{grid-template-columns:repeat(4,1fr)}}.services{{grid-column:1/5;justify-content:flex-start}}}}@media(max-width:700px){{.metrics{{grid-template-columns:repeat(2,1fr)}}.form-grid{{grid-template-columns:1fr}}.field.wide,.form-note,.form-actions{{grid-column:1}}}}@media(max-width:460px){{.metrics{{grid-template-columns:1fr}}.history-row{{grid-template-columns:75px 60px minmax(0,1fr)}}.history-row>a{{grid-column:3}}}}
</style></head><body><nav><h1>Codex KVM Lab</h1><span id="dispatch">AUTO · 6-way scheduler</span><span id="summary">loading…</span><span class="spacer"></span><a href="/controller/vnc.html?autoconnect=1&resize=scale&path=controller/websockify" target="_blank">controller</a><a href="/change-password">password</a><a href="/logout">sign out</a></nav>
<section class="section"><div class="section-title"><h2>Scheduler health & throughput</h2><span id="timestamp">updating…</span></div><div class="metrics">
<div class="metric" id="m-health"><span>Scheduler</span><strong>—</strong><small>loop health</small></div><div class="metric" id="m-throughput"><span>Throughput</span><strong>—</strong><small>jobs/min · 5m</small></div><div class="metric" id="m-queue"><span>Queue p95</span><strong>—</strong><small>start delay · 5m</small></div><div class="metric" id="m-success"><span>Success</span><strong>—</strong><small>completed · 1h</small></div><div class="metric" id="m-util"><span>Worker utilization</span><strong>—</strong><small>six VMs · 5m</small></div><div class="metric" id="m-runtime"><span>Runtime p95</span><strong>—</strong><small>job duration · 1h</small></div>
</div><div class="graph-wrap"><div class="graph-head"><span>Last 30 minutes</span><span class="legend"><i></i>completed/min</span><span class="legend fail"><i></i>failed</span><span class="legend queue"><i></i>avg queue ms</span></div><svg id="throughput-svg" viewBox="0 0 600 120" preserveAspectRatio="none" aria-label="Scheduler throughput history"></svg></div></section>
<section class="section"><div class="section-title"><h2>Six KVM workstations</h2><span>status, ownership, controls and history are attached to each live screen</span></div></section><main>{cards}</main>
<div id="job-modal" class="modal"><section class="modal-card"><div class="modal-head"><strong id="job-modal-title">Job logs</strong><span id="job-modal-meta"></span><span class="spacer"></span><button class="mini" id="job-refresh">Refresh</button><button class="mini" id="job-close">Close</button></div><pre id="job-output">Loading…</pre></section></div>
<div id="history-modal" class="modal"><section class="modal-card"><div class="modal-head"><strong id="history-title">VM history</strong><span class="spacer"></span><button class="mini" id="history-close">Close</button></div><div id="history-list" class="history-modal-list">Loading…</div></section></div>
<div id="submit-modal" class="modal"><section class="modal-card narrow"><div class="modal-head"><strong id="submit-title">Run job</strong><span class="spacer"></span><button class="mini" id="submit-close">Close</button></div><form id="submit-form" class="form-grid"><input type="hidden" id="submit-station"><div class="field"><label>Bot / owner</label><input id="submit-owner" required value="Mark Dashboard"></div><div class="field"><label>Project</label><input id="submit-project" placeholder="optional project"></div><div class="field"><label>Chat label</label><input id="submit-chat-label" placeholder="optional chat title"></div><div class="field"><label>Chat URL</label><input id="submit-chat-url" type="url" placeholder="https://chatgpt.com/..."></div><div class="field"><label>Job class</label><select id="submit-class"><option>test</option><option>build</option><option selected>cpu</option><option>io</option><option>browser</option></select></div><div class="field"><label>Timeout seconds</label><input id="submit-timeout" type="number" min="1" max="86400" value="3600"></div><div class="field wide"><label>Working directory</label><input id="submit-cwd" value="/workspace"></div><div class="field wide"><label>Command</label><textarea id="submit-command" required placeholder="npm test"></textarea></div><div class="form-note" id="submit-note">This job is pinned to the selected VM. If that VM is busy, it waits for that VM; normal agent jobs continue using automatic six-way scheduling.</div><div class="form-actions"><button type="button" class="mini" id="submit-cancel">Cancel</button><button type="submit" class="mini primary">Queue on this VM</button></div></form></section></div>
<div id="toast"></div>
<script>
const csrf={csrf_json};const NS='http://www.w3.org/2000/svg';let lastState=null,logJobId=null;
const pct=v=>v==null?'—':`${{Math.round(v)}}%`;const gib=v=>v==null?'—':`${{Number(v).toFixed(0)}}G`;const ms=v=>v==null?'—':v<1000?`${{Math.round(v)}}ms`:`${{(v/1000).toFixed(v<10000?1:0)}}s`;function uptime(s){{if(!s)return'—';const d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60);return d?`${{d}}d ${{h}}h`:h?`${{h}}h ${{m}}m`:`${{m}}m`;}}function elapsed(iso){{if(!iso)return'';return uptime(Math.max(0,(Date.now()-Date.parse(iso))/1000))}}function when(iso){{if(!iso)return'—';const sec=Math.max(0,(Date.now()-Date.parse(iso))/1000);return sec<86400?`${{uptime(sec)}} ago`:new Date(iso).toLocaleDateString()}}function validChat(url){{return typeof url==='string'&&(url.startsWith('https://chatgpt.com/')||url.startsWith('https://chat.openai.com/'))}}
function toast(message,bad=false){{const el=document.getElementById('toast');el.textContent=message;el.classList.toggle('bad',bad);el.style.display='block';clearTimeout(el._t);el._t=setTimeout(()=>el.style.display='none',4000)}}
async function action(station,name){{const confirmText=name==='cancel-job'?`Cancel the running job on VM ${{station}}?`:name==='release-lease'?`Release the interactive bot/browser lease on VM ${{station}}?`:name==='restart-browser'?`Restart the visible browser desktop on VM ${{station}}?`:null;if(confirmText&&!confirm(confirmText))return;try{{const r=await fetch(`/control/worker/${{station}}/${{name}}`,{{method:'POST',headers:{{'X-CSRF-Token':csrf,'Content-Type':'application/json'}},body:'{{}}'}}),d=await r.json();if(!r.ok)throw new Error(d.error||`HTTP ${{r.status}}`);toast(`VM ${{station}}: ${{name.replaceAll('-',' ')}} complete`);if(name==='launch-terminal')window.open(`/vm${{station}}/vnc.html?autoconnect=1&resize=scale&path=vm${{station}}/websockify`,'_blank');setTimeout(refresh,150)}}catch(e){{toast(`VM ${{station}}: ${{e.message}}`,true)}}}}
async function showLogs(station){{const w=(lastState?.workers||[]).find(x=>x.station===station),id=w?.activeJob?.id;if(!id)return toast(`VM ${{station}} has no active scheduled job`,true);logJobId=id;document.getElementById('job-modal').classList.add('open');document.getElementById('job-modal-title').textContent=`VM ${{station}} · ${{w.activeJob.owner||'job'}}`;await refreshLogs()}}async function refreshLogs(){{if(!logJobId)return;const out=document.getElementById('job-output');try{{const r=await fetch(`/dashboard/job/${{encodeURIComponent(logJobId)}}`,{{cache:'no-store'}}),d=await r.json();if(!r.ok)throw new Error(d.error||`HTTP ${{r.status}}`);document.getElementById('job-modal-meta').textContent=`${{d.status||''}} · ${{d.project||d.job_class||''}}`;out.textContent=(d.stdoutTail||'(no stdout yet)')+(d.stderrTail?`\n\n--- STDERR ---\n${{d.stderrTail}}`:'')}}catch(e){{out.textContent=`Could not load logs: ${{e.message}}`}}}}function closeLogs(){{logJobId=null;document.getElementById('job-modal').classList.remove('open')}}
function historyRow(item){{const row=document.createElement('div');row.className='history-row';const t=document.createElement('span');t.className='when';t.textContent=when(item.started_at);const k=document.createElement('span');k.className='kind';k.textContent=`${{item.kind}} · ${{item.status}}`;const who=document.createElement('div');const strong=document.createElement('strong');strong.textContent=item.owner||'unknown';const small=document.createElement('small');small.textContent=item.chatLabel||item.project||'no project/chat label';who.append(strong,small);row.append(t,k,who);if(validChat(item.chatUrl)){{const a=document.createElement('a');a.href=item.chatUrl;a.target='_blank';a.rel='noreferrer';a.textContent='open chat';row.appendChild(a)}}return row}}
async function showHistory(station){{const modal=document.getElementById('history-modal'),list=document.getElementById('history-list');document.getElementById('history-title').textContent=`VM ${{station}} · chat / bot history`;list.textContent='Loading…';modal.classList.add('open');try{{const r=await fetch(`/dashboard/history/${{station}}`,{{cache:'no-store'}}),d=await r.json();if(!r.ok)throw new Error(d.error||`HTTP ${{r.status}}`);list.replaceChildren();if(!d.history?.length)list.textContent='No recorded use yet.';else d.history.forEach(item=>list.appendChild(historyRow(item)))}}catch(e){{list.textContent=`Could not load history: ${{e.message}}`}}}}function closeHistory(){{document.getElementById('history-modal').classList.remove('open')}}
function openSubmit(station){{document.getElementById('submit-station').value=station;document.getElementById('submit-title').textContent=`Run job on VM ${{station}}`;document.getElementById('submit-note').textContent=`Pinned to VM ${{station}}. If it is busy, this job waits for VM ${{station}} while automatic jobs continue using the other free workers.`;document.getElementById('submit-modal').classList.add('open');document.getElementById('submit-command').focus()}}function closeSubmit(){{document.getElementById('submit-modal').classList.remove('open')}}
async function submitPinned(e){{e.preventDefault();const station=Number(document.getElementById('submit-station').value),owner=document.getElementById('submit-owner').value.trim(),command=document.getElementById('submit-command').value;if(!owner||!command.trim())return toast('Owner and command are required',true);const payload={{owner,project:document.getElementById('submit-project').value.trim()||null,chatLabel:document.getElementById('submit-chat-label').value.trim()||null,chatUrl:document.getElementById('submit-chat-url').value.trim()||null,jobClass:document.getElementById('submit-class').value,cwd:document.getElementById('submit-cwd').value.trim()||'/workspace',timeout:Number(document.getElementById('submit-timeout').value)||3600,command}};try{{const r=await fetch(`/control/worker/${{station}}/submit-job`,{{method:'POST',headers:{{'X-CSRF-Token':csrf,'Content-Type':'application/json'}},body:JSON.stringify(payload)}}),d=await r.json();if(!r.ok)throw new Error(d.error||`HTTP ${{r.status}}`);localStorage.setItem('codexDashboardOwner',owner);toast(`Queued ${{d.id?.slice(0,8)||'job'}} for VM ${{station}}`);document.getElementById('submit-command').value='';closeSubmit();setTimeout(refresh,100)}}catch(err){{toast(`Could not queue VM ${{station}} job: ${{err.message}}`,true)}}}}
function metric(id,value,detail,kind=''){{const el=document.getElementById(id);el.classList.remove('good','warn','bad');if(kind)el.classList.add(kind);el.querySelector('strong').textContent=value;el.querySelector('small').textContent=detail}}function renderSvg(history){{const svg=document.getElementById('throughput-svg');svg.replaceChildren();if(!history?.length)return;const w=600,pad=8,base=105,maxJobs=Math.max(1,...history.map(x=>x.completed||0)),maxQueue=Math.max(1,...history.map(x=>x.avgQueueMs||0)),bw=(w-pad*2)/history.length;history.forEach((x,i)=>{{const bh=(x.completed||0)/maxJobs*72,rect=document.createElementNS(NS,'rect');rect.setAttribute('x',pad+i*bw+1);rect.setAttribute('y',base-bh);rect.setAttribute('width',Math.max(1,bw-2));rect.setAttribute('height',bh);rect.setAttribute('fill','#3895e8');svg.appendChild(rect);if(x.failed){{const fail=document.createElementNS(NS,'rect'),fh=Math.max(4,Math.min(bh,(x.failed/maxJobs)*72));fail.setAttribute('x',pad+i*bw+1);fail.setAttribute('y',base-fh);fail.setAttribute('width',Math.max(1,bw-2));fail.setAttribute('height',fh);fail.setAttribute('fill','#e65e70');svg.appendChild(fail)}}}});const line=document.createElementNS(NS,'polyline');line.setAttribute('points',history.map((x,i)=>`${{pad+i*bw+bw/2}},${{base-(x.avgQueueMs||0)/maxQueue*72}}`).join(' '));line.setAttribute('fill','none');line.setAttribute('stroke','#45d08b');line.setAttribute('stroke-width','2');line.setAttribute('vector-effect','non-scaling-stroke');svg.appendChild(line)}}function updateMetrics(s){{const m=s.metrics||{{}},w5=(m.windows||{{}})['5m']||{{}},w1=(m.windows||{{}})['1h']||{{}},health=m.loopHealthy&&s.scheduler?.healthy;metric('m-health',health?'Healthy':'Degraded',`${{ms(m.loopAgeMs)}} loop age · ${{m.errors||0}} errors`,health?'good':'bad');metric('m-throughput',`${{Number(w5.jobsPerMinute||0).toFixed(2)}}`,`${{w5.completed||0}} completed / 5m`,w5.jobsPerMinute>0?'good':'');metric('m-queue',ms(w5.p95QueueMs),`avg ${{ms(w5.avgQueueMs)}} · oldest ${{ms(m.oldestQueuedMs)}}`,(w5.p95QueueMs||0)>3000?'warn':'good');metric('m-success',w1.successRatePercent==null?'—':`${{w1.successRatePercent}}%`,`${{w1.succeeded||0}} ok · ${{w1.failed||0}} failed`,w1.failed?'warn':'good');metric('m-util',`${{w5.workerUtilizationPercent??0}}%`,`${{s.scheduler?.busy??0}} busy · ${{s.scheduler?.free??0}} free`);metric('m-runtime',ms(w1.p95RuntimeMs),`avg ${{ms(w1.avgRuntimeMs)}} · 1h`);renderSvg(m.history)}}
function renderInlineHistory(el,history){{el.replaceChildren();if(!history?.length){{const empty=document.createElement('span');empty.className='history-empty';empty.textContent='No history yet';el.appendChild(empty);return}}history.slice(0,3).forEach(item=>{{const chip=document.createElement('span');chip.className='history-chip';chip.title=`${{item.kind}} · ${{item.status}} · ${{item.project||''}}`;if(validChat(item.chatUrl)){{const a=document.createElement('a');a.href=item.chatUrl;a.target='_blank';a.rel='noreferrer';a.textContent=item.owner||item.chatLabel||'chat';chip.appendChild(a)}}else chip.textContent=item.owner||item.chatLabel||item.project||item.kind;el.appendChild(chip)}})}}
function updateWorker(w){{const busy=!!(w.activeJob||w.exclusive||w.schedulerBusy),installing=w.ready&&!w.workstationReady,cls=!w.ready?'down':installing?'installing':busy?'busy':'ready',identity=w.activeJob||w.lease||null,owner=identity?.owner||(w.schedulerBusy?'Scheduled job':'Idle'),project=identity?.chatLabel||identity?.project||(identity?'No project/chat label':'No bot assigned'),chatUrl=identity?.chatUrl,el=document.getElementById(`worker-${{w.station}}`);el.classList.remove('ready','busy','down','installing');el.classList.add(cls);el.querySelector('.worker-state').textContent=!w.ready?w.state:installing?'installing':busy?'busy':`ready`;el.querySelector('.worker-owner').textContent=owner;el.querySelector('.identity-owner').textContent=owner;el.querySelector('.identity-project').textContent=project;const chat=el.querySelector('.identity-chat');if(validChat(chatUrl)){{chat.href=chatUrl;chat.textContent='open chat';chat.hidden=false}}else{{chat.hidden=true;chat.removeAttribute('href')}}const age=w.activeJob?elapsed(w.activeJob.started_at):w.lease?elapsed(w.lease.acquiredAt):'';el.querySelector('.activity').textContent=w.activeJob?`${{w.activeJob.job_class||'job'}} · ${{w.activeJob.status}} · ${{age}}${{w.activeJob.requestedStation?` · pinned VM ${{w.activeJob.requestedStation}}`:''}}`:w.lease?`Interactive/browser lease · ${{age}}`:w.schedulerBusy?'Scheduled job':w.queuedPinned?.length?`${{w.queuedPinned.length}} pinned job${{w.queuedPinned.length===1?'':'s'}} waiting · next: ${{w.queuedPinned[0].owner||'unknown'}}`:'No active job';el.querySelector('.cpu').textContent=pct(w.cpuPercent);el.querySelector('.ram').textContent=pct(w.memUsedPercent);el.querySelector('.disk').textContent=pct(w.diskUsedPercent);el.querySelector('.uptime').textContent=uptime(w.uptimeSeconds);el.querySelector('.workstation').classList.toggle('ok',!!w.workstationReady);el.querySelector('.docker').classList.toggle('ok',!!w.dockerReady);el.querySelector('.browser').classList.toggle('ok',!!w.browserReady);el.querySelector('.cancel').hidden=!w.activeJob;el.querySelector('.logs').hidden=!w.activeJob;el.querySelector('.release').hidden=!w.lease;el.querySelector('.terminal').disabled=!w.ready;el.querySelector('.restart').disabled=!w.browserReady;renderInlineHistory(el.querySelector('.history-items'),w.history)}}
async function refresh(){{try{{const s=await fetch('/state.json',{{cache:'no-store'}}).then(r=>{{if(!r.ok)throw new Error(`HTTP ${{r.status}}`);return r.json()}});lastState=s;const sched=s.scheduler||{{}};document.getElementById('summary').textContent=`${{sched.ready??0}}/6 ready · ${{sched.busy??0}} busy · ${{s.queuedJobs}} queued`;const badge=document.getElementById('dispatch');badge.textContent=`AUTO · ${{sched.capacity||6}}-way · ${{sched.free??0}} free`;badge.classList.toggle('bad',!sched.healthy);document.getElementById('timestamp').textContent=`updated ${{new Date(s.time).toLocaleTimeString()}}`;updateMetrics(s);for(const w of s.workers)updateWorker(w)}}catch(e){{document.getElementById('summary').textContent='status unavailable';toast(`Status refresh failed: ${{e.message}}`,true)}}}}
document.addEventListener('click',e=>{{const a=e.target.closest('button[data-action]');if(a)action(Number(a.dataset.station),a.dataset.action);const v=e.target.closest('button[data-view]');if(v?.dataset.view==='logs')showLogs(Number(v.dataset.station));if(v?.dataset.view==='history')showHistory(Number(v.dataset.station));if(v?.dataset.view==='submit')openSubmit(Number(v.dataset.station))}});document.getElementById('job-refresh').addEventListener('click',refreshLogs);document.getElementById('job-close').addEventListener('click',closeLogs);document.getElementById('history-close').addEventListener('click',closeHistory);document.getElementById('submit-close').addEventListener('click',closeSubmit);document.getElementById('submit-cancel').addEventListener('click',closeSubmit);document.getElementById('submit-form').addEventListener('submit',submitPinned);for(const id of ['job-modal','history-modal','submit-modal'])document.getElementById(id).addEventListener('click',e=>{{if(e.target.id===id)document.getElementById(id).classList.remove('open')}});const savedOwner=localStorage.getItem('codexDashboardOwner');if(savedOwner)document.getElementById('submit-owner').value=savedOwner;refresh();setInterval(refresh,2000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = f"CodexLabScheduler/{SCHEDULER_VERSION}"

    def log_message(self, fmt, *args):
        print(json.dumps({"time": now_iso(), "event": "http", "client": self.client_ip(), "message": fmt % args}), flush=True)

    def client_ip(self):
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded or self.client_address[0]

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "SAMEORIGIN")

    def send_json(self, payload, code=200):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body, code=200):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'")
        self.security_headers()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location, code=303):
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.security_headers()
        self.end_headers()

    def authorized(self):
        return self.client_address[0] in TRUSTED_CLIENTS

    def read_body(self, maximum=2 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > maximum:
            raise ValueError("request too large")
        return self.rfile.read(length)

    def read_json(self):
        return json.loads(self.read_body() or b"{}")

    def read_form(self):
        return {key: values[-1] for key, values in parse_qs(self.read_body(64 * 1024).decode("utf-8", "replace"), keep_blank_values=True).items()}

    def session(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            morsel = cookie.get(SESSION_COOKIE)
            return verify_session(morsel.value if morsel else None)
        except Exception:
            return None

    def set_session(self, token):
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_SECONDS}; Secure; HttpOnly; SameSite=Strict")

    def clear_session(self):
        self.send_header("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict")

    def login_redirect(self):
        forwarded = self.headers.get("X-Forwarded-Uri") or self.path
        target = safe_next(forwarded)
        self.redirect("/login?next=" + quote(target, safe=""), 302)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self.send_json({"ok": True, "service": "codex-lab-scheduler", "version": SCHEDULER_VERSION, "maxStations": MAX_STATIONS, "authConfigured": load_dashboard_auth() is not None})
        if u.path == "/auth/check":
            if self.session():
                self.send_response(204); self.send_header("Cache-Control", "no-store"); self.security_headers(); self.end_headers(); return
            return self.login_redirect()
        if u.path == "/login":
            query = parse_qs(u.query)
            target = safe_next((query.get("next") or ["/"])[0])
            if self.session():
                return self.redirect(target, 302)
            config = load_dashboard_auth()
            username = config.get("username", "mark") if config else "mark"
            return self.send_html(login_html(target, username=username))
        if u.path == "/logout":
            self.send_response(303); self.send_header("Location", "/login"); self.send_header("Cache-Control", "no-store"); self.clear_session(); self.security_headers(); self.end_headers(); return
        if u.path == "/change-password":
            session = self.session()
            if not session:
                return self.login_redirect()
            changed = (parse_qs(u.query).get("changed") or [""])[0] == "1"
            return self.send_html(change_password_html(session, success="Password updated. Other signed-in sessions were invalidated." if changed else None))
        if u.path.startswith("/dashboard/job/"):
            if not self.session():
                return self.send_json({"error": "unauthorized"}, 401)
            job_id = u.path.rsplit("/", 1)[-1]
            job = dashboard_job(job_id)
            return self.send_json(job or {"error": "not found"}, 200 if job else 404)
        if u.path.startswith("/dashboard/history/"):
            if not self.session():
                return self.send_json({"error": "unauthorized"}, 401)
            try:
                return self.send_json(dashboard_history(int(u.path.rsplit("/", 1)[-1]), 50))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
        if u.path == "/state.json":
            if not self.session():
                return self.send_json({"error": "unauthorized"}, 401)
            return self.send_json(state_payload())
        if u.path == "/":
            session = self.session()
            if not session:
                return self.login_redirect()
            return self.send_html(dashboard_html(session))
        if not u.path.startswith("/api/") or not self.authorized():
            return self.send_json({"error": "unauthorized"}, 401)
        if u.path == "/api/workers":
            return self.send_json(state_payload())
        if u.path == "/api/jobs":
            q = parse_qs(u.query)
            return self.send_json({"jobs": list_jobs((q.get("status") or [None])[0], (q.get("limit") or [50])[0])})
        if u.path.startswith("/api/history/"):
            station = int(u.path.rsplit("/", 1)[-1])
            with DB_LOCK:
                conn = db(); rows = worker_usage_history(conn, station, 100); conn.close()
            return self.send_json({"station": station, "history": rows})
        if u.path.startswith("/api/jobs/"):
            job_id = u.path.split("/")[3]
            q = parse_qs(u.query)
            job = get_job(job_id, True, (q.get("maxBytes") or [65536])[0])
            return self.send_json(job or {"error": "not found"}, 200 if job else 404)
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/login":
            try:
                form = self.read_form()
            except Exception as exc:
                return self.send_html(login_html("/", str(exc)), 400)
            target = safe_next(form.get("next", "/"))
            blocked = login_rate_state(self.client_ip())
            config = load_dashboard_auth()
            username = str(form.get("username") or "")
            if blocked:
                return self.send_html(login_html(target, username=username or (config or {}).get("username", "mark"), retry_after=blocked), 429)
            if config is None:
                return self.send_html(login_html(target, "Dashboard authentication is not configured.", username=username or "mark"), 503)
            if not verify_dashboard_password(username, str(form.get("password") or "")):
                record_login_failure(self.client_ip())
                return self.send_html(login_html(target, "Incorrect username or password.", username=username or config["username"]), 401)
            clear_login_failures(self.client_ip())
            token, _ = make_session(config)
            self.send_response(303); self.send_header("Location", target); self.send_header("Cache-Control", "no-store"); self.set_session(token); self.security_headers(); self.end_headers(); return
        if u.path == "/change-password":
            session = self.session()
            if not session:
                return self.login_redirect()
            try:
                form = self.read_form()
            except Exception as exc:
                return self.send_html(change_password_html(session, error=str(exc)), 400)
            if not hmac.compare_digest(str(form.get("csrf") or ""), str(session.get("csrf") or "")):
                return self.send_html(change_password_html(session, error="Your session security token is invalid. Please reload and try again."), 403)
            new_password = str(form.get("new_password") or "")
            if new_password != str(form.get("confirm_password") or ""):
                return self.send_html(change_password_html(session, error="The new passwords do not match."), 400)
            try:
                config = change_dashboard_password(session["u"], str(form.get("current_password") or ""), new_password)
            except Exception as exc:
                return self.send_html(change_password_html(session, error=str(exc)), 400)
            token, new_session = make_session(config)
            self.send_response(303); self.send_header("Location", "/change-password?changed=1"); self.send_header("Cache-Control", "no-store"); self.set_session(token); self.security_headers(); self.end_headers(); return
        if u.path.startswith("/control/worker/"):
            session = self.session()
            if not session:
                return self.send_json({"error": "unauthorized"}, 401)
            csrf = self.headers.get("X-CSRF-Token", "")
            if not hmac.compare_digest(str(csrf), str(session.get("csrf") or "")):
                return self.send_json({"error": "invalid csrf token"}, 403)
            parts = [part for part in u.path.split("/") if part]
            if len(parts) != 4:
                return self.send_json({"error": "invalid worker action path"}, 404)
            try:
                station = int(parts[2]); action = parts[3]
                if action == "submit-job":
                    payload = self.read_json()
                    result = submit_worker_job(station, payload)
                    return self.send_json(result, 202)
                result = worker_control(station, action)
                return self.send_json(result, 200)
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            except Exception as exc:
                return self.send_json({"error": str(exc)}, 500)
        if not u.path.startswith("/api/") or not self.authorized():
            return self.send_json({"error": "unauthorized"}, 401)
        try:
            payload = self.read_json()
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 400)
        try:
            if u.path == "/api/jobs":
                return self.send_json(submit_job(payload), 202)
            if u.path == "/api/usage":
                return self.send_json(record_external_usage(payload), 202)
            if u.path.startswith("/api/jobs/") and u.path.endswith("/cancel"):
                job_id = u.path.split("/")[3]
                job = cancel_job(job_id)
                return self.send_json(job or {"error": "not found"}, 200 if job else 404)
        except Exception as exc:
            return self.send_json({"error": str(exc)}, 500)
        return self.send_json({"error": "not found"}, 404)


def main():
    db().close()
    thread = threading.Thread(target=scheduler_loop, daemon=True, name="scheduler-loop"); thread.start()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler); httpd.daemon_threads = True
    print(json.dumps({"event":"scheduler_started","time":now_iso(),"host":HOST,"port":PORT,"stations":MAX_STATIONS}), flush=True)
    try: httpd.serve_forever(poll_interval=0.2)
    finally:
        STOP.set(); httpd.server_close(); thread.join(timeout=3)

if __name__ == "__main__": main()
