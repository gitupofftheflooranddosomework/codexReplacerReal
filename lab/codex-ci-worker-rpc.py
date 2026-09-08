#!/usr/bin/env python3
"""Restricted forced-command endpoint for Codex CI workers.

Exec jobs run in their own process group. SSH disconnect, explicit cancel,
release, SIGTERM, or SIGHUP reaps the complete remote job tree.
"""
from __future__ import annotations

import base64
import fcntl
import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import time

HOME = pathlib.Path.home()
STATE_DIR = HOME / ".local/share/codex-worker"
CI_STATE = HOME / ".local/share/codex-ci"
CLAIM_LOCK = STATE_DIR / "claim.lock"
EXCLUSIVE = STATE_DIR / "exclusive.lock"
SCHEDULER = STATE_DIR / "scheduler.lock"
WORK_ROOT = pathlib.Path("/workspace/ci-dispatch")


def die(message, code=2):
    print(message, file=sys.stderr); raise SystemExit(code)


def decode(value):
    try: return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except Exception: die("invalid encoded value")


def safe_lease(value):
    value = str(value or "")
    if not value or len(value) > 64 or any(c not in "0123456789abcdef" for c in value): die("invalid lease id")
    return value


def lease_dir(iid): return WORK_ROOT / safe_lease(iid)
def exec_state(iid): return lease_dir(iid) / ".exec.json"


def load_lease():
    try: return json.loads(EXCLUSIVE.read_text())
    except Exception: return {}


def require_lease(iid):
    lease = load_lease()
    if lease.get("leaseId") != iid or lease.get("source") != "github-actions": die("lease is not owned by this CI dispatcher", 3)
    return lease


def lock_claim():
    STATE_DIR.mkdir(parents=True, exist_ok=True); CLAIM_LOCK.touch(exist_ok=True)
    h = CLAIM_LOCK.open("r+"); fcntl.flock(h.fileno(), fcntl.LOCK_EX); return h


def unlock(h): fcntl.flock(h.fileno(), fcntl.LOCK_UN); h.close()


def kill_exec(iid, grace=3):
    path = exec_state(iid)
    try: state = json.loads(path.read_text())
    except Exception: return False
    pgid = int(state.get("pgid") or 0)
    if pgid <= 1: return False
    try: os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError: pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        try: os.killpg(pgid, 0)
        except ProcessLookupError: break
        time.sleep(.1)
    else:
        try: os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError: pass
    try: path.unlink()
    except FileNotFoundError: pass
    return True


def action_probe():
    headless = (CI_STATE / "headless-v1").exists()
    workstation = pathlib.Path("/var/lib/codex-worker-workstation-v1").exists()
    ready = headless or (pathlib.Path("/var/lib/codex-lab-ready").exists() and workstation)
    mode = "headless" if headless else "interactive"
    print(json.dumps({"ready":ready,"mode":mode,"hostname":os.uname().nodename}, separators=(",", ":")))
    return 0 if ready else 4


def action_claim(args):
    if len(args) != 2: die("claim requires one encoded JSON payload")
    try: payload = json.loads(decode(args[1]))
    except Exception: die("invalid claim payload")
    iid = safe_lease(payload.get("leaseId"))
    if payload.get("source") != "github-actions" or not str(payload.get("owner") or "").strip(): die("invalid CI lease payload")
    h = lock_claim()
    try:
        if SCHEDULER.exists() or EXCLUSIVE.exists(): return 10
        payload["leaseId"] = iid; EXCLUSIVE.write_text(json.dumps(payload,separators=(",", ":"))); os.chmod(EXCLUSIVE,0o600)
    finally: unlock(h)
    print(json.dumps(payload,separators=(",", ":"))); return 0


def action_import(args):
    if len(args) != 2: die("import requires lease id")
    iid = safe_lease(args[1]); require_lease(iid); root = lease_dir(iid); repo = root / "repo"
    shutil.rmtree(root, ignore_errors=True); repo.mkdir(parents=True, exist_ok=True)
    rc = subprocess.run(["tar","-xf","-","-C",str(repo)], stdin=sys.stdin.buffer, check=False).returncode
    if rc: return rc
    # A command-only scheduler job legitimately archives an empty Git tree.
    # Preserve that snapshot as a real commit instead of rejecting it before exec.
    for cmd in (["git","init","-q"],["git","add","-A"],["git","-c","user.name=Codex-CI","-c","user.email=codex-ci@localhost","commit","--allow-empty","-qm","snapshot","--no-gpg-sign"]):
        rc = subprocess.run(cmd,cwd=repo,check=False).returncode
        if rc: return rc
    print(repo); return 0


def parse_env(encoded):
    if not encoded: return {}
    try: data=json.loads(decode(encoded))
    except Exception: die("invalid environment payload")
    if not isinstance(data,dict): die("environment must be an object")
    return {str(k):str(v) for k,v in data.items()}


def action_overlay(args):
    if len(args) != 2: die("overlay requires lease id")
    iid=safe_lease(args[1]); require_lease(iid); repo=lease_dir(iid)/"repo"
    if not repo.is_dir(): die("lease workspace is missing",5)
    return subprocess.run(["tar","-xf","-","-C",str(repo)],stdin=sys.stdin.buffer,check=False).returncode


def action_exec(args):
    if len(args) != 4: die("exec requires lease id, encoded command, encoded env")
    iid=safe_lease(args[1]); require_lease(iid); repo=lease_dir(iid)/"repo"
    if not repo.is_dir(): die("lease workspace is missing",5)
    env=os.environ.copy(); env.update(parse_env(args[3])); path=exec_state(iid)
    proc=subprocess.Popen(["bash","-lc",decode(args[2])],cwd=repo,env=env,start_new_session=True)
    path.write_text(json.dumps({"pid":proc.pid,"pgid":proc.pid,"startedAt":time.time()},separators=(",", ":"))); os.chmod(path,0o600)
    old={}
    def stop(signum, _frame):
        kill_exec(iid); raise SystemExit(128+signum)
    for sig in (signal.SIGHUP,signal.SIGTERM,signal.SIGINT):
        old[sig]=signal.signal(sig,stop)
    try: return proc.wait()
    finally:
        for sig,handler in old.items(): signal.signal(sig,handler)
        try: path.unlink()
        except FileNotFoundError: pass


def action_cancel(args):
    if len(args) != 2: die("cancel requires lease id")
    iid=safe_lease(args[1]); require_lease(iid); killed=kill_exec(iid)
    print(json.dumps({"leaseId":iid,"cancelled":killed},separators=(",", ":"))); return 0


def safe_relative(value):
    p=pathlib.PurePosixPath(value)
    if p.is_absolute() or not p.parts or ".." in p.parts: die("invalid artifact path")
    return str(p)


def action_artifact(args):
    if len(args)<3: die("artifact requires lease id and one or more encoded paths")
    iid=safe_lease(args[1]); require_lease(iid); repo=lease_dir(iid)/"repo"
    paths=[safe_relative(decode(x)) for x in args[2:]]
    return subprocess.run(["tar","-cf","-","--",*paths],cwd=repo,stdout=sys.stdout.buffer,check=False).returncode


def action_release(args):
    if len(args)!=2: die("release requires lease id")
    iid=safe_lease(args[1]); root=lease_dir(iid); h=lock_claim()
    try:
        lease=load_lease()
        if lease.get("leaseId")!=iid or lease.get("source")!="github-actions": return 0
        kill_exec(iid)
        subprocess.run(["bw","logout"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False)
        shutil.rmtree(root,ignore_errors=True)
        try: EXCLUSIVE.unlink()
        except FileNotFoundError: pass
    finally: unlock(h)
    return 0


def main():
    raw=os.environ.get("SSH_ORIGINAL_COMMAND","").strip()
    if not raw: die("restricted Codex CI key: command required")
    try: args=shlex.split(raw)
    except ValueError: die("invalid command quoting")
    if not args or args[0]!="codex-ci": die("restricted Codex CI key")
    action=args[1] if len(args)>1 else ""
    handlers={"probe":action_probe,"claim":action_claim,"import":action_import,"overlay":action_overlay,
              "exec":action_exec,"cancel":action_cancel,"artifact":action_artifact,"release":action_release}
    fn=handlers.get(action)
    if fn is None: die("unsupported Codex CI action")
    return fn() if action=="probe" else fn([action,*args[2:]])


if __name__=="__main__": raise SystemExit(main())
