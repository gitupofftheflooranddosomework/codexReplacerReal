#!/usr/bin/env python3
"""Dispatch CI work to an elastic project-scoped headless KVM.

No automatic path in this client targets codex-lab-vm-01..06. Those persistent
VMs remain available for explicitly interactive/browser/manual work.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid

MANAGER=os.environ.get("CODEX_CI_HEADLESS_MANAGER","/home/mark/.local/bin/codex-ci-headless")
SSH_USER=os.environ.get("CODEX_CI_SSH_USER","mark")
SSH_KEY=os.environ.get("CODEX_CI_SSH_KEY","/home/mark/.local/share/codex-ci/ssh/id_ed25519_codex_ci")
KNOWN_HOSTS=os.environ.get("CODEX_CI_KNOWN_HOSTS","/home/mark/.local/share/codex-ci/ssh/known_hosts")


def enc(value): return base64.urlsafe_b64encode(value.encode()).rstrip(b"=").decode()


def manager(*args,timeout=120):
    environ = dict(os.environ, CODEX_CI_DISPATCH_PID=str(os.getpid()))
    cp=subprocess.run([MANAGER,*map(str,args)],text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False,env=environ)
    if cp.returncode: raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or f"headless manager rc={cp.returncode}")
    try: return json.loads(cp.stdout)
    except json.JSONDecodeError as exc: raise RuntimeError(f"invalid manager response: {cp.stdout[:500]}") from exc


def ssh_base(rec):
    return ["ssh","-i",SSH_KEY,"-o","IdentitiesOnly=yes","-o","BatchMode=yes",
            "-o",f"UserKnownHostsFile={KNOWN_HOSTS}","-o","StrictHostKeyChecking=accept-new",
            "-o",f"HostKeyAlias=codex-ci-{rec['id']}","-o","ConnectTimeout=5",f"{SSH_USER}@{rec['ip']}"]


def ssh_capture(rec,cmd,input_bytes=None,timeout=30):
    return subprocess.run([*ssh_base(rec),cmd],input=input_bytes,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False)


def wait_ready(rec,seconds=180):
    deadline=time.monotonic()+seconds; last=""
    while time.monotonic()<deadline:
        try: cp=ssh_capture(rec,"codex-ci probe",timeout=8)
        except subprocess.TimeoutExpired: cp=None
        if cp and cp.returncode==0:
            try: payload=json.loads(cp.stdout.decode())
            except Exception: payload={}
            if payload.get("ready") and payload.get("mode")=="headless": return payload
            last=f"unexpected probe {payload!r}"
        elif cp: last=(cp.stderr or cp.stdout).decode(errors="replace").strip()
        time.sleep(2)
    raise RuntimeError(f"headless worker did not become ready: {last}")


def safe_path(value,allow_git=False):
    p=pathlib.PurePosixPath(value)
    if p.is_absolute() or not p.parts or ".." in p.parts or (not allow_git and p.parts[0]==".git"):
        raise ValueError(f"unsafe repository-relative path: {value}")
    return str(p)


def dirty_paths(workspace):
    out=set()
    for cmd in (["git","diff","--no-renames","--name-only","-z"],["git","diff","--cached","--no-renames","--name-only","-z"],["git","ls-files","--others","--exclude-standard","-z"]):
        cp=subprocess.run(cmd,cwd=workspace,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
        if cp.returncode: raise RuntimeError(cp.stderr.decode(errors="replace").strip() or "git workspace inspection failed")
        out|={x.decode(errors="surrogateescape") for x in cp.stdout.split(b"\0") if x}
    return out


def validate_workspace(workspace,inputs):
    cp=subprocess.run(["git","rev-parse","--is-inside-work-tree"],cwd=workspace,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=False)
    if cp.returncode or cp.stdout.strip()!="true": raise RuntimeError("workspace is not a Git checkout")
    roots=[pathlib.PurePosixPath(safe_path(x)) for x in inputs]
    def allowed(x):
        p=pathlib.PurePosixPath(x); return any(p==r or r in p.parents for r in roots)
    unexpected=sorted(x for x in dirty_paths(workspace) if not allowed(x))
    if unexpected: raise RuntimeError("workspace contains undeclared changes: "+", ".join(unexpected[:20]))


def stream_snapshot(rec,iid,workspace):
    archive=subprocess.Popen(["git","archive","--format=tar","HEAD"],cwd=workspace,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    remote=subprocess.Popen([*ssh_base(rec),f"codex-ci import {iid}"],stdin=archive.stdout,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    archive.stdout.close(); _,rerr=remote.communicate(timeout=180); aerr=archive.stderr.read(); arc=archive.wait(timeout=10)
    if arc: raise RuntimeError("git archive failed: "+aerr.decode(errors="replace").strip())
    if remote.returncode: raise RuntimeError("worker import failed: "+rerr.decode(errors="replace").strip())


def push_inputs(rec,iid,workspace,inputs):
    for raw in inputs:
        item=safe_path(raw)
        if not (workspace/item).exists(): raise FileNotFoundError(f"input path does not exist: {item}")
        tar=subprocess.Popen(["tar","-cf","-","--",item],cwd=workspace,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        remote=subprocess.Popen([*ssh_base(rec),f"codex-ci overlay {iid}"],stdin=tar.stdout,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        tar.stdout.close(); _,rerr=remote.communicate(timeout=300); terr=tar.stderr.read(); trc=tar.wait(timeout=10)
        if trc: raise RuntimeError("input archive failed: "+terr.decode(errors="replace").strip())
        if remote.returncode: raise RuntimeError("worker overlay failed: "+rerr.decode(errors="replace").strip())


def cancel(rec,iid):
    try: ssh_capture(rec,f"codex-ci cancel {iid}",timeout=15)
    except Exception: pass


def run_command(rec,iid,command,timeout,env):
    original=f"codex-ci exec {iid} {enc(command)} {enc(json.dumps(env,separators=(',',':')))}"
    proc=subprocess.Popen([*ssh_base(rec),original])
    try: return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()
        cancel(rec,iid); raise


def pull_artifacts(rec,iid,artifacts,destination):
    if not artifacts: return
    clean=[safe_path(x,allow_git=True) for x in artifacts]; destination.mkdir(parents=True,exist_ok=True)
    cmd="codex-ci artifact "+iid+" "+" ".join(enc(x) for x in clean)
    remote=subprocess.Popen([*ssh_base(rec),cmd],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    extract=subprocess.run(["tar","-xf","-","-C",str(destination)],stdin=remote.stdout,check=False)
    remote.stdout.close(); err=remote.stderr.read(); rc=remote.wait(timeout=300)
    if rc or extract.returncode: raise RuntimeError("artifact transfer failed: "+err.decode(errors="replace").strip())


def parse_env(values):
    out={}
    for item in values:
        if "=" not in item: raise ValueError(f"--env requires KEY=VALUE: {item}")
        k,v=item.split("=",1)
        if not k or not k.replace("_","a").isalnum() or not (k[0].isalpha() or k[0]=="_"): raise ValueError(f"invalid environment name: {k}")
        out[k]=v
    return out


def acquire(a):
    deadline=time.monotonic()+max(1,a.wait_seconds); last=""
    ttl=max(a.timeout+600,a.ttl_minutes*60,int(a.persist_hours*3600) if a.persist_hours else 0)
    while True:
        argv=["reserve","--owner",a.owner,"--project",a.project or "","--ttl-seconds",str(ttl),
              "--memory-mib",str(a.memory_mib),"--max-memory-mib",str(a.max_memory_mib),
              "--vcpus",str(a.vcpus),"--disk-gib",str(a.disk_gib)]
        if a.session_key: argv += ["--session-key",a.session_key]
        try:
            reserved=manager(*argv,timeout=45)
            return dict(reserved['instance'], reused=reserved['reused'])
        except RuntimeError as exc:
            last=str(exc)
            if time.monotonic()>=deadline: raise TimeoutError(f"no safe headless KVM capacity within {a.wait_seconds}s: {last}")
            time.sleep(2)


def main():
    p=argparse.ArgumentParser(description="Dispatch one CI command to an elastic project-scoped headless KVM")
    p.add_argument("--owner",required=True); p.add_argument("--project",default="")
    p.add_argument("--workspace",default=os.environ.get("GITHUB_WORKSPACE",".")); p.add_argument("--command",required=True)
    p.add_argument("--timeout",type=int,default=3600); p.add_argument("--wait-seconds",type=int,default=600); p.add_argument("--ttl-minutes",type=int,default=180)
    p.add_argument("--persist-hours",type=float,default=0); p.add_argument("--session-key",default="")
    p.add_argument("--memory-mib",type=int,default=int(os.environ.get("CODEX_CI_HEADLESS_MEMORY_MIB","2048")))
    p.add_argument("--max-memory-mib",type=int,default=int(os.environ.get("CODEX_CI_HEADLESS_MAX_MEMORY_MIB","4096")))
    p.add_argument("--vcpus",type=int,default=int(os.environ.get("CODEX_CI_HEADLESS_VCPUS","2"))); p.add_argument("--disk-gib",type=int,default=40)
    p.add_argument("--input",action="append",default=[]); p.add_argument("--artifact",action="append",default=[]); p.add_argument("--artifact-dest",default=None); p.add_argument("--env",action="append",default=[])
    a=p.parse_args()
    if a.persist_hours and not a.session_key: p.error("--persist-hours requires --session-key")
    workspace=pathlib.Path(a.workspace).resolve(); validate_workspace(workspace,a.input); env=parse_env(a.env)
    rec=None; iid=None; claimed=False; rc=1; status="failed"; old={}
    class Interrupted(Exception): pass
    def interrupted(_sig,_frame): raise Interrupted()
    try:
        for sig in (signal.SIGTERM,signal.SIGINT): old[sig]=signal.signal(sig,interrupted)
        rec=acquire(a); print(f"codex_ci_instance={rec['id']}"); print(f"codex_ci_vm={rec['name']}"); print(f"codex_ci_worker={rec['ip']}"); print(f"codex_ci_reused={str(bool(rec.get('reused'))).lower()}")
        # Keep the durable identity before waiting for provisioning, so timeout
        # and signal finalizers can always release the reserved VM.
        sys.stdout.flush()
        if not rec.get('reused'):
            manager('provision',rec['id'],timeout=max(180,a.wait_seconds))
        wait_ready(rec); iid=uuid.uuid4().hex
        payload={"leaseId":iid,"owner":a.owner,"project":a.project or None,"source":"github-actions"}
        cp=ssh_capture(rec,"codex-ci claim "+enc(json.dumps(payload,separators=(",", ":"))),timeout=15)
        if cp.returncode: raise RuntimeError("headless worker claim failed: "+(cp.stderr or cp.stdout).decode(errors="replace").strip())
        claimed=True
        stream_snapshot(rec,iid,workspace); push_inputs(rec,iid,workspace,a.input)
        rc=run_command(rec,iid,a.command,max(1,a.timeout),env)
        if rc != 0:
            status="failed"
            return rc
        pull_artifacts(rec,iid,a.artifact,pathlib.Path(a.artifact_dest or workspace).resolve())
        status="succeeded"
        return 0
    except subprocess.TimeoutExpired:
        status="timed_out"; rc=124
        print("Codex CI worker command timed out",file=sys.stderr); return rc
    except Interrupted:
        status="cancelled"; rc=130
        print("Codex CI dispatch interrupted",file=sys.stderr); return rc
    finally:
        for sig,handler in old.items(): signal.signal(sig,handler)
        if rec and iid and claimed:
            if status in ("timed_out","cancelled"): cancel(rec,iid)
            try: ssh_capture(rec,f"codex-ci release {iid}",timeout=20)
            except Exception: pass
        if rec:
            keep=max(0,int(a.persist_hours*3600)) if claimed else 0
            try: manager("finish",rec["id"],"--status",status,"--exit-code",str(rc),"--reason",status,"--keep-seconds",str(keep),timeout=90)
            except Exception as exc: print(f"warning: headless lifecycle cleanup failed: {exc}",file=sys.stderr)


if __name__=="__main__": raise SystemExit(main())
