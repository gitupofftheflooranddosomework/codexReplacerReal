#!/usr/bin/env python3
import fcntl
import json
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(os.environ.get("CODEX_VM_LAB_ROOT", "/tank/codex-lab-vm"))
STATE_FILE = ROOT / "state.json"
AUDIT_FILE = ROOT / "sign-in-out.jsonl"
SHEET_FILE = ROOT / "SIGN-IN-OUT.md"
LOCK_FILE = ROOT / ".lock"
MAX_STATIONS = max(1, min(int(os.environ.get("CODEX_VM_LAB_MAX_STATIONS", "4")), 8))
PREWARM = max(0, min(int(os.environ.get("CODEX_VM_LAB_PREWARM", "4")), MAX_STATIONS))
HOME_SERVER = os.environ.get("CODEX_VM_LAB_HOST", "home-server")
HOME_KEY = os.environ.get("CODEX_VM_LAB_HOME_KEY", "/home/mark/.ssh/id_ed25519_home_server")
GUEST_KEY = os.environ.get("CODEX_VM_LAB_GUEST_KEY", "/home/mark/.ssh/id_ed25519_codex_lab_vm")
KNOWN_HOSTS = os.environ.get("CODEX_VM_LAB_KNOWN_HOSTS", "/home/mark/.ssh/codex_lab_known_hosts")
REMOTE_CTL = os.environ.get("CODEX_VM_LAB_CTL", "/tank/vm/codex-lab/vm-labctl.sh")


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

def locked_state():
    ensure_dirs(); lock=LOCK_FILE.open("r+"); fcntl.flock(lock.fileno(), fcntl.LOCK_EX); return lock, load_state()
def unlock(lock): fcntl.flock(lock.fileno(), fcntl.LOCK_UN); lock.close()

def run(args, timeout=120):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)

def ssh_host(command, timeout=120):
    return run(["ssh","-i",HOME_KEY,"-o","IdentitiesOnly=yes","-o","BatchMode=yes",HOME_SERVER,command], timeout)

def ssh_guest(station, command, timeout=120):
    return run(["ssh","-i",GUEST_KEY,"-o","IdentitiesOnly=yes","-o","BatchMode=yes","-o",f"UserKnownHostsFile={KNOWN_HOSTS}","-o","StrictHostKeyChecking=accept-new","-o","ConnectTimeout=5",f"mark@{ip_for(station)}",command], timeout)

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

def expire(state):
    expired=[]; current=now()
    for r in state.setdefault("stations",{}).values():
        if r.get("status")!="leased" or not r.get("expiresAt"): continue
        try: due=datetime.fromisoformat(r["expiresAt"])
        except Exception: continue
        if due <= current:
            old=r.copy(); r.update({"status":"free","leaseId":None,"owner":None,"project":None,"acquiredAt":None,"expiresAt":None,"releasedAt":iso(current),"releaseReason":"expired"}); expired.append(old); audit("auto_sign_out",station=old["station"],leaseId=old.get("leaseId"),owner=old.get("owner"),project=old.get("project")); reset_station(old["station"])
    return expired

def acquire(owner, project=None, ttl_minutes=180):
    owner=str(owner or "").strip()
    if not owner: raise ValueError("owner is required")
    ttl=max(15,min(int(ttl_minutes),1440)); lock,state=locked_state()
    try:
        expire(state)
        for s in range(1,MAX_STATIONS+1):
            r=record_for(state,s)
            if r.get("status")=="leased": continue
            ensure_station(s); lease=str(uuid.uuid4()); t=now(); r.update({"status":"leased","leaseId":lease,"owner":owner,"project":project or None,"acquiredAt":iso(t),"expiresAt":iso(t+timedelta(minutes=ttl)),"releasedAt":None,"releaseReason":None}); save_state(state); audit("sign_in",station=s,leaseId=lease,owner=owner,project=project or None,ttlMinutes=ttl); return r.copy()
        raise RuntimeError(f"All {MAX_STATIONS} full-VM lab stations are leased")
    finally: unlock(lock)

def find_active(state, lease_id=None, station=None):
    for r in state.setdefault("stations",{}).values():
        if r.get("status")!="leased": continue
        if lease_id and r.get("leaseId")==lease_id: return r
        if station is not None and int(r.get("station",0))==int(station): return r
    raise KeyError("Active full-VM lab lease not found")

def release(lease_id=None, station=None, reason="released", recycle=True):
    lock,state=locked_state()
    try:
        r=find_active(state,lease_id,station); old=r.copy(); s=int(r["station"]); r.update({"status":"free","leaseId":None,"owner":None,"project":None,"acquiredAt":None,"expiresAt":None,"releasedAt":iso(),"releaseReason":reason}); save_state(state); audit("sign_out",station=s,leaseId=old.get("leaseId"),owner=old.get("owner"),project=old.get("project"),reason=reason,recycle=bool(recycle))
    finally: unlock(lock)
    if recycle: reset_station(s)
    return old

def execute(command, lease_id=None, station=None, cwd="/workspace", timeout=120, env=None, as_root=False, max_bytes=1048576):
    lock,state=locked_state()
    try: expire(state); r=find_active(state,lease_id,station).copy()
    finally: unlock(lock)
    env_prefix=" ".join(f"{shlex.quote(str(k))}={shlex.quote(str(v))}" for k,v in (env or {}).items())
    body=f"cd {shlex.quote(cwd)} && " + ((env_prefix+" ") if env_prefix else "") + command
    if as_root: body="sudo -n bash -lc "+shlex.quote(body)
    result=ssh_guest(r["station"],body,max(1,min(int(timeout),86400))); limit=max(1024,min(int(max_bytes),8388608)); out=result.stdout.encode(); err=result.stderr.encode()
    return {"station":r["station"],"name":r["name"],"ip":r["ip"],"leaseId":r["leaseId"],"owner":r["owner"],"project":r.get("project"),"exitCode":result.returncode,"stdout":out[:limit].decode(errors="replace"),"stderr":err[:limit].decode(errors="replace"),"truncated":len(out)>limit or len(err)>limit}

def list_stations(audit_lines=12):
    lock,state=locked_state()
    try:
        expire(state); status=host_status(); rows=[]
        for s in range(1,MAX_STATIONS+1):
            r=record_for(state,s).copy(); r.update(status.get(s,{"state":"absent","autostart":"no"})); rows.append(r)
        save_state(state)
    finally: unlock(lock)
    recent=[]
    if AUDIT_FILE.exists() and audit_lines:
        for line in AUDIT_FILE.read_text().splitlines()[-int(audit_lines):]:
            try: recent.append(json.loads(line))
            except Exception: pass
    return {"maxStations":MAX_STATIONS,"prewarmStations":PREWARM,"stations":rows,"recentSignInOut":recent,"sheet":str(SHEET_FILE)}

def collect():
    lock,state=locked_state()
    try: expired=expire(state); save_state(state)
    finally: unlock(lock)
    ensured=[]
    for s in range(1,PREWARM+1): ensured.append(ensure_station(s))
    return {"expiredLeases":[x.get("leaseId") for x in expired],"prewarmed":ensured}
