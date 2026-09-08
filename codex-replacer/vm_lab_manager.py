#!/usr/bin/env python3
import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CODEX_VM_LAB_ROOT", "/tank/codex-lab-vm"))
STATE_FILE = ROOT / "state.json"
AUDIT_FILE = ROOT / "sign-in-out.jsonl"
SHEET_FILE = ROOT / "SIGN-IN-OUT.md"
LOCK_FILE = ROOT / ".lock"
MAX_STATIONS = max(1, min(int(os.environ.get("CODEX_VM_LAB_MAX_STATIONS", "6")), 8))
PREWARM = max(0, min(int(os.environ.get("CODEX_VM_LAB_PREWARM", "6")), MAX_STATIONS))
HOME_SERVER = os.environ.get("CODEX_VM_LAB_HOST", "home-server")
HOME_KEY = os.environ.get("CODEX_VM_LAB_HOME_KEY", "/home/mark/.ssh/id_ed25519_home_server")
GUEST_KEY = os.environ.get("CODEX_VM_LAB_GUEST_KEY", "/home/mark/.ssh/id_ed25519_codex_lab_vm")
KNOWN_HOSTS = os.environ.get("CODEX_VM_LAB_KNOWN_HOSTS", "/home/mark/.ssh/codex_lab_known_hosts")
REMOTE_CTL = os.environ.get("CODEX_VM_LAB_CTL", "/tank/vm/codex-lab/vm-labctl.sh")
SCHEDULER_URL = os.environ.get("CODEX_LAB_SCHEDULER_URL", "http://192.168.122.1:8766").rstrip("/")
VAULT_URL = os.environ.get("CODEX_VAULT_URL", "https://vault.markshaw.ca").rstrip("/")


def now(): return datetime.now(timezone.utc)
def iso(v=None): return (v or now()).isoformat()
def ip_for(station): return f"192.168.122.{229 + int(station)}"
def name_for(station): return f"codex-lab-vm-{int(station):02d}"

def ensure_dirs():
    ROOT.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.touch(exist_ok=True)
    Path(KNOWN_HOSTS).parent.mkdir(parents=True, exist_ok=True)
    Path(KNOWN_HOSTS).touch(exist_ok=True)

def load_state():
    if not STATE_FILE.exists(): return {"version": 1, "stations": {}}
    try: return json.loads(STATE_FILE.read_text())
    except Exception: return {"version": 1, "stations": {}}

def record_for(state, station):
    s = int(station); key = str(s)
    r = state.setdefault("stations", {}).setdefault(key, {})
    r.setdefault("station", s); r.setdefault("name", name_for(s)); r.setdefault("ip", ip_for(s)); r.setdefault("status", "free")
    return r

def save_state(state):
    fd, tmp = tempfile.mkstemp(prefix="state-", suffix=".json", dir=ROOT)
    with os.fdopen(fd, "w") as h:
        json.dump(state, h, indent=2, sort_keys=True); h.write("\n")
    os.replace(tmp, STATE_FILE)
    lines=["# Codex Full-VM Lab Sign-In / Sign-Out","",f"Updated: {iso()}","","| Station | VM | IP | Status | Agent | Project | Signed in | Lease expires |","|---:|---|---|---|---|---|---|---|"]
    for s in range(1, MAX_STATIONS+1):
        r=record_for(state,s); lines.append(f"| {s} | {r['name']} | {r['ip']} | {r.get('status','free')} | {r.get('owner') or ''} | {r.get('project') or ''} | {r.get('acquiredAt') or ''} | {r.get('expiresAt') or ''} |")
    SHEET_FILE.write_text("\n".join(lines)+"\n")

def audit(event, **fields):
    ensure_dirs()
    with AUDIT_FILE.open("a") as h: h.write(json.dumps({"time":iso(),"event":event,**fields},separators=(",",":"))+"\n")

def notify_usage(action, record, status=None, finished_at=None):
    if not record or not record.get("leaseId") or not record.get("station"):
        return False
    payload = {
        "action": str(action),
        "kind": "lease",
        "refId": str(record.get("leaseId")),
        "station": int(record.get("station")),
        "owner": record.get("owner"),
        "project": record.get("project"),
        "chatLabel": record.get("chatLabel"),
        "chatUrl": record.get("chatUrl"),
        "startedAt": record.get("acquiredAt"),
        "finishedAt": finished_at,
        "status": status,
        "source": "vm-lab-controller",
    }
    data = json.dumps(payload, separators=(",", ":")).encode()
    request = urllib.request.Request(
        SCHEDULER_URL + "/api/usage",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            return 200 <= response.status < 300
    except Exception:
        # The local sign-in/out audit remains the source-of-truth fallback if
        # the homeserver scheduler is temporarily unavailable.
        return False


def locked_state():
    ensure_dirs(); lock=LOCK_FILE.open("r+"); fcntl.flock(lock.fileno(), fcntl.LOCK_EX); return lock, load_state()
def unlock(lock): fcntl.flock(lock.fileno(), fcntl.LOCK_UN); lock.close()

def run(args, timeout=120):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)

def ssh_host(command, timeout=120):
    return run(["ssh","-i",HOME_KEY,"-o","IdentitiesOnly=yes","-o","BatchMode=yes",HOME_SERVER,command], timeout)

def ssh_guest(station, command, timeout=120, input_text=None):
    return subprocess.run(["ssh","-i",GUEST_KEY,"-o","IdentitiesOnly=yes","-o","BatchMode=yes","-o",f"UserKnownHostsFile={KNOWN_HOSTS}","-o","StrictHostKeyChecking=accept-new","-o","ConnectTimeout=5",f"mark@{ip_for(station)}",command], input=input_text, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)

def public_key():
    p=Path(GUEST_KEY+".pub")
    if not p.exists(): raise RuntimeError("VM lab guest public key missing")
    return p.read_text().strip()

def ctl(*parts, timeout=180):
    cmd=" ".join([shlex.quote(REMOTE_CTL), *[shlex.quote(str(p)) for p in parts]])
    r=ssh_host(cmd, timeout)
    if r.returncode: raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout.strip()

def ensure_station(station):
    s=int(station)
    if not 1 <= s <= MAX_STATIONS: raise ValueError(f"station must be 1..{MAX_STATIONS}")
    ctl("ensure", s, public_key(), timeout=180)
    run(["ssh-keygen","-f",KNOWN_HOSTS,"-R",ip_for(s)],20)
    for _ in range(25):
        r=ssh_guest(s,"test -e /var/lib/codex-lab-ready && printf READY",10)
        if r.returncode==0 and "READY" in r.stdout: return {"station":s,"name":name_for(s),"ip":ip_for(s),"ready":True}
        time.sleep(1)
    raise RuntimeError(f"VM lab station {s} did not become SSH-ready")

def reset_station(station):
    s=int(station); ctl("reset",s,public_key(),timeout=180); run(["ssh-keygen","-f",KNOWN_HOSTS,"-R",ip_for(s)],20)
    if s <= PREWARM:
        for _ in range(25):
            r=ssh_guest(s,"test -e /var/lib/codex-lab-ready && printf READY",10)
            if r.returncode==0 and "READY" in r.stdout: return
            time.sleep(1)

def host_status():
    out=ctl("status",timeout=30).splitlines(); rows={}
    for line in out[1:]:
        parts=line.split("\t")
        if len(parts)>=5: rows[int(parts[0])]={"name":parts[1],"ip":parts[2],"state":parts[3],"autostart":parts[4]}
    return rows

class RemoteLeaseProbeUnavailable(RuntimeError):
    pass


def _remote_exclusive_lease(station):
    try:
        result = ssh_guest(
            station,
            "cat /home/mark/.local/share/codex-worker/exclusive.lock 2>/dev/null",
            5,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteLeaseProbeUnavailable(
            f"worker {int(station)} lease probe timed out"
        ) from exc
    if result.returncode == 255:
        raise RemoteLeaseProbeUnavailable(
            (result.stderr or result.stdout).strip()
            or f"worker {int(station)} lease probe transport failed"
        )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def reset_worker_vault(station):
    """Remove reusable Bitwarden state before a shared VM changes hands."""
    scrubber = f"""
import json
import os
import pathlib
import tempfile

path = pathlib.Path('/home/mark/.config/Bitwarden CLI/data.json')
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
state_version = 79
try:
    existing = json.loads(path.read_text())
    if isinstance(existing.get('stateVersion'), int):
        state_version = existing['stateVersion']
except (FileNotFoundError, json.JSONDecodeError, OSError):
    pass
clean = {{
    'global_environment_environment': {{
        'region': 'Self-hosted',
        'urls': {{
            'api': None,
            'base': {VAULT_URL!r},
            'events': None,
            'icons': None,
            'identity': None,
            'keyConnector': None,
            'notifications': None,
            'send': None,
            'webVault': None,
        }},
    }},
    'stateVersion': state_version,
}}
handle, temporary = tempfile.mkstemp(prefix='.data.json.', dir=path.parent)
try:
    os.fchmod(handle, 0o600)
    with os.fdopen(handle, 'w') as stream:
        json.dump(clean, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
"""
    command = (
        "if command -v bw >/dev/null 2>&1; then "
        "timeout 3s bw logout >/dev/null 2>&1 || true; "
        f"python3 -c {shlex.quote(scrubber)}; "
        "fi"
    )
    try:
        result = ssh_guest(int(station), command, 8)
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _mark_free(record, reason, released_at=None):
    old = record.copy()
    record.update({
        "status": "free", "leaseId": None, "owner": None, "project": None,
        "chatLabel": None, "chatUrl": None,
        "acquiredAt": None, "expiresAt": None,
        "releasedAt": iso(released_at or now()), "releaseReason": reason,
    })
    return old


def reconcile_lost_leases(state):
    recovered = []
    for record in state.setdefault("stations", {}).values():
        if record.get("status") != "leased":
            continue
        try:
            remote = _remote_exclusive_lease(record["station"])
        except RemoteLeaseProbeUnavailable:
            # A transport failure is not evidence that the remote ownership
            # lock disappeared. Leave the lease intact and retry later.
            continue
        if remote and remote.get("leaseId") == record.get("leaseId"):
            continue
        old = _mark_free(record, "remote-lock-lost")
        recovered.append(old)
        audit(
            "auto_sign_out",
            station=old.get("station"), leaseId=old.get("leaseId"),
            owner=old.get("owner"), project=old.get("project"), reason="remote-lock-lost",
        )
        notify_usage("end", old, status="lost", finished_at=old.get("releasedAt"))
    if recovered:
        save_state(state)
    return recovered


def expire(state):
    expired = []
    current = now()
    for record in state.setdefault("stations", {}).values():
        if record.get("status") != "leased" or not record.get("expiresAt"):
            continue
        try:
            due = datetime.fromisoformat(record["expiresAt"])
        except Exception:
            continue
        if due > current:
            continue
        result = ssh_guest(
            record["station"],
            "rm -f /home/mark/.local/share/codex-worker/exclusive.lock",
            8,
        )
        if result.returncode != 0:
            continue
        old = _mark_free(record, "expired", current)
        expired.append(old)
        audit(
            "auto_sign_out", station=old.get("station"), leaseId=old.get("leaseId"),
            owner=old.get("owner"), project=old.get("project"), reason="expired",
        )
        notify_usage("end", old, status="expired", finished_at=old.get("releasedAt"))
    if expired:
        save_state(state)
    return expired


def acquire(owner, project=None, ttl_minutes=180, chat_label=None, chat_url=None):
    owner=str(owner or "").strip()
    if not owner: raise ValueError("owner is required")
    ttl=max(15,min(int(ttl_minutes),1440)); lock,state=locked_state()
    try:
        expire(state)
        reconcile_lost_leases(state)
        for s in range(1,MAX_STATIONS+1):
            r=record_for(state,s)
            if r.get("status")=="leased": continue
            ensure_station(s)
            if not reset_worker_vault(s):
                continue
            repair = ssh_guest(
                s,
                "mkdir -p /home/mark/.local/share/codex-worker && flock -n /home/mark/.local/share/codex-worker/claim.lock sh -c 'test ! -e /home/mark/.local/share/codex-worker/scheduler.lock && rm -f /home/mark/.local/share/codex-worker/exclusive.lock'",
                5,
            )
            if repair.returncode != 0:
                continue
            lease = str(uuid.uuid4())
            t = now()
            expires = t + timedelta(minutes=ttl)
            lock_payload = json.dumps({
                "leaseId": lease, "owner": owner, "project": project or None,
                "chatLabel": str(chat_label or "").strip() or None,
                "chatUrl": str(chat_url or "").strip() or None,
                "acquiredAt": iso(t), "expiresAt": iso(expires),
            }, separators=(",", ":"))
            lock_command = (
                "mkdir -p /home/mark/.local/share/codex-worker && "
                "flock -n /home/mark/.local/share/codex-worker/claim.lock sh -c 'test ! -e /home/mark/.local/share/codex-worker/scheduler.lock && test ! -e /home/mark/.local/share/codex-worker/exclusive.lock && cat > /home/mark/.local/share/codex-worker/exclusive.lock'"
            )
            lock_result = ssh_guest(s, lock_command, 8, input_text=lock_payload)
            if lock_result.returncode != 0:
                continue
            r.update({
                "status": "leased", "leaseId": lease, "owner": owner,
                "project": project or None,
                "chatLabel": str(chat_label or "").strip() or None,
                "chatUrl": str(chat_url or "").strip() or None,
                "acquiredAt": iso(t),
                "expiresAt": iso(expires), "releasedAt": None, "releaseReason": None,
            })
            save_state(state)
            audit("sign_in", station=s, leaseId=lease, owner=owner, project=project or None,
                  chatLabel=str(chat_label or "").strip() or None,
                  chatUrl=str(chat_url or "").strip() or None, ttlMinutes=ttl)
            notify_usage("start", r)
            return r.copy()
        raise RuntimeError(f"All {MAX_STATIONS} full-VM lab stations are leased")
    finally: unlock(lock)

def find_active(state, lease_id=None, station=None):
    for r in state.setdefault("stations",{}).values():
        if r.get("status")!="leased": continue
        if lease_id and r.get("leaseId")==lease_id: return r
        if station is not None and int(r.get("station",0))==int(station): return r
    raise KeyError("Active full-VM lab lease not found")

def validate_lease(lease_id, station=None):
    lock, state = locked_state()
    try:
        expire(state)
        # Validate only the requested lease here. Global reconciliation can
        # involve multiple SSH probes and belongs in acquire/list/gc paths;
        # making every exec/browser call depend on unrelated workers causes
        # latency amplification and cross-worker failures.
        record = find_active(state, lease_id=lease_id)
        if station is not None and int(record.get("station", 0)) != int(station):
            raise KeyError(f"Lease {lease_id} belongs to station {record.get('station')}, not station {station}")
        remote = _remote_exclusive_lease(record["station"])
        if not remote or remote.get("leaseId") != lease_id:
            old = _mark_free(record, "remote-lock-lost")
            save_state(state)
            audit(
                "auto_sign_out", station=old.get("station"), leaseId=old.get("leaseId"),
                owner=old.get("owner"), project=old.get("project"), reason="remote-lock-lost",
            )
            notify_usage("end", old, status="lost", finished_at=old.get("releasedAt"))
            raise KeyError(f"Lease {lease_id} no longer owns worker {old.get('station')}")
        return record.copy()
    finally:
        unlock(lock)


def release(lease_id=None, station=None, reason="released", recycle=False):
    lock, state = locked_state()
    try:
        record = find_active(state, lease_id, station)
        old = record.copy()
        station_number = int(record["station"])
        if not reset_worker_vault(station_number):
            raise RuntimeError(
                f"Could not scrub vault state on worker {station_number}; "
                "the lease remains active"
            )
        clear = ssh_guest(
            station_number,
            "rm -f /home/mark/.local/share/codex-worker/exclusive.lock",
            8,
        )
        if clear.returncode != 0:
            raise RuntimeError(
                f"Could not clear exclusive lock on worker {station_number}: "
                f"{(clear.stderr or clear.stdout).strip()}"
            )
        _mark_free(record, reason)
        save_state(state)
        audit(
            "sign_out", station=station_number, leaseId=old.get("leaseId"),
            owner=old.get("owner"), project=old.get("project"), reason=reason,
            recycle=bool(recycle),
        )
        notify_usage("end", old, status=str(reason or "released"), finished_at=record.get("releasedAt"))
    finally:
        unlock(lock)
    if recycle:
        reset_station(station_number)
    return old


def execute(command, lease_id=None, station=None, cwd="/workspace", timeout=120, env=None, as_root=False, max_bytes=1048576):
    record = validate_lease(lease_id, station)
    env_prefix = " ".join(
        f"{shlex.quote(str(k))}={shlex.quote(str(v))}" for k, v in (env or {}).items()
    )
    body = f"cd {shlex.quote(cwd)} && " + ((env_prefix + " ") if env_prefix else "") + command
    if as_root:
        body = "sudo -n bash -lc " + shlex.quote(body)
    result = ssh_guest(record["station"], body, max(1, min(int(timeout), 86400)))
    limit = max(1024, min(int(max_bytes), 8388608))
    out = result.stdout.encode()
    err = result.stderr.encode()
    return {
        "station": record["station"], "name": record["name"], "ip": record["ip"],
        "leaseId": record["leaseId"], "owner": record["owner"],
        "project": record.get("project"), "chatLabel": record.get("chatLabel"),
        "chatUrl": record.get("chatUrl"), "exitCode": result.returncode,
        "stdout": out[:limit].decode(errors="replace"),
        "stderr": err[:limit].decode(errors="replace"),
        "truncated": len(out) > limit or len(err) > limit,
    }


def list_stations(audit_lines=12):
    lock,state=locked_state()
    try:
        expire(state); reconcile_lost_leases(state); status=host_status(); rows=[]
        for s in range(1,MAX_STATIONS+1):
            r=record_for(state,s).copy(); r.update(status.get(s,{"state":"absent","autostart":"no"})); rows.append(r)
        save_state(state)
    finally: unlock(lock)
    recent=[]
    if AUDIT_FILE.exists() and audit_lines:
        for line in AUDIT_FILE.read_text().splitlines()[-int(audit_lines):]:
            try: recent.append(json.loads(line))
            except Exception: pass
    return {"maxStations":MAX_STATIONS,"prewarmStations":PREWARM,"stations":[dict(x, browserUrl=f"https://browser{x['station']}.home.markshaw.ca/") for x in rows],"recentSignInOut":recent,"sheet":str(SHEET_FILE),"dashboardUrl":"https://browser.home.markshaw.ca/"}

def collect():
    lock,state=locked_state()
    try: expired=expire(state); recovered=reconcile_lost_leases(state); save_state(state)
    finally: unlock(lock)
    ensured=[]
    for s in range(1,PREWARM+1): ensured.append(ensure_station(s))
    return {"expiredLeases":[x.get("leaseId") for x in expired],"recoveredLeases":[x.get("leaseId") for x in recovered],"prewarmed":ensured}
