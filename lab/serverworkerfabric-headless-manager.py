#!/usr/bin/env python3
"""codex-ci-dispatch-compatible headless manager backed by ServerWorkerFabric.

This adapter intentionally implements only disposable headless reserve/provision/finish.
Retained sessions are refused until their semantics are explicitly represented by the
Fabric worker API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL=os.environ.get("CODEX_CI_SWF_API_URL","").strip().rstrip("/")
TOKEN=os.environ.get("CODEX_CI_SWF_API_TOKEN","").strip()
TOKEN_FILE=os.environ.get("CODEX_CI_SWF_API_TOKEN_FILE","").strip()
CA_FILE=os.environ.get("CODEX_CI_SWF_API_CA_FILE","").strip()
PROFILE=os.environ.get("CODEX_CI_SWF_HEADLESS_PROFILE","headless").strip() or "headless"
HTTP_TIMEOUT=max(1.0,float(os.environ.get("CODEX_CI_SWF_API_TIMEOUT","10")))
READY_TIMEOUT=max(5.0,float(os.environ.get("CODEX_CI_SWF_READY_TIMEOUT","180")))
POLL_SECONDS=max(0.1,float(os.environ.get("CODEX_CI_SWF_READY_POLL_SECONDS","2")))


class FabricManagerError(RuntimeError):
    pass


def api_token():
    if TOKEN:
        return TOKEN
    if TOKEN_FILE:
        try:
            value=pathlib.Path(TOKEN_FILE).read_text().strip()
        except OSError as exc:
            raise FabricManagerError("ServerWorkerFabric API token file is not readable") from exc
        if value:
            return value
    raise FabricManagerError(
        "ServerWorkerFabric API token is required via "
        "CODEX_CI_SWF_API_TOKEN or CODEX_CI_SWF_API_TOKEN_FILE"
    )


def base_url():
    parsed=urllib.parse.urlsplit(BASE_URL)
    if parsed.scheme not in {"http","https"} or not parsed.netloc:
        raise FabricManagerError("CODEX_CI_SWF_API_URL must be an absolute http(s) URL")
    return BASE_URL


def _urlopen(request):
    kwargs={"timeout":HTTP_TIMEOUT}
    if CA_FILE:
        try:
            kwargs["context"]=ssl.create_default_context(cafile=CA_FILE)
        except (OSError,ssl.SSLError) as exc:
            raise FabricManagerError(
                "ServerWorkerFabric CA file could not be loaded"
            ) from exc
    return urllib.request.urlopen(request,**kwargs)


def _request(method,path,payload=None,*,allow_404=False):
    url=base_url()+path
    headers={
        "Accept":"application/json",
        "Authorization":f"Bearer {api_token()}",
        "User-Agent":"CodexCI/ServerWorkerFabricHeadlessManager",
    }
    data=None
    if payload is not None:
        data=json.dumps(payload,separators=(",",":")).encode()
        headers["Content-Type"]="application/json"
    request=urllib.request.Request(url,data=data,method=method,headers=headers)
    try:
        with _urlopen(request) as response:
            raw=response.read()
            status=int(getattr(response,"status",200))
    except urllib.error.HTTPError as exc:
        if allow_404 and int(exc.code)==404:
            return 404,{"error":"worker not found"}
        raw=exc.read()
        try:
            body=json.loads(raw.decode()) if raw else {}
        except Exception:
            body={}
        detail=str(body.get("error") or f"HTTP {int(exc.code)}")
        raise FabricManagerError(f"ServerWorkerFabric request failed: {detail}") from exc
    except (urllib.error.URLError,TimeoutError,socket.timeout,OSError) as exc:
        raise FabricManagerError("ServerWorkerFabric request failed") from exc
    try:
        body=json.loads(raw.decode()) if raw else {}
    except (UnicodeDecodeError,json.JSONDecodeError) as exc:
        raise FabricManagerError("ServerWorkerFabric returned invalid JSON") from exc
    if not isinstance(body,dict):
        raise FabricManagerError("ServerWorkerFabric response must be an object")
    if status < 200 or status >= 300:
        if allow_404 and status==404:
            return status,body
        raise FabricManagerError(
            f"ServerWorkerFabric returned HTTP {status}: {body.get('error') or 'request failed'}"
        )
    return status,body


def _correlation(owner,project):
    raw=f"{owner}\0{project}".encode()
    return "ci-"+hashlib.sha256(raw).hexdigest()[:16]


def _instance(payload,*,owner=None,project=None):
    logical=str(payload.get("instance_id") or "").strip()
    worker=payload.get("worker")
    if not logical or not isinstance(worker,dict):
        raise FabricManagerError("ServerWorkerFabric create response is missing worker identity")
    if str(worker.get("logical_id") or "") != logical:
        raise FabricManagerError("ServerWorkerFabric create response identity mismatch")
    metadata=worker.get("metadata")
    name=""
    if isinstance(metadata,dict):
        name=str(metadata.get("name") or "").strip()
    return {
        "id":logical,
        "name":name or logical,
        "ip":str(worker.get("address") or "").strip(),
        "owner":owner,
        "project":project or None,
        "status":str(worker.get("state") or "creating"),
        "expires_at_epoch":payload.get("expires_at_epoch"),
    }


def reserve(owner,project,ttl_seconds,session_key=None,**_resources):
    if session_key:
        raise FabricManagerError(
            "ServerWorkerFabric manager does not support retained session reuse"
        )
    ttl=int(ttl_seconds)
    if ttl < 60 or ttl > 7*24*3600:
        raise FabricManagerError("ttl_seconds must be between 60 and 604800")
    _,payload=_request(
        "POST",
        "/v1/headless",
        {
            "profile":PROFILE,
            "correlation_id":_correlation(owner,project),
            "ttl_seconds":ttl,
        },
    )
    return {"instance":_instance(payload,owner=owner,project=project),"reused":False}


def provision(instance_id):
    logical=str(instance_id or "").strip()
    if not logical:
        raise FabricManagerError("instance id is required")
    quoted=urllib.parse.quote(logical,safe="")
    deadline=time.monotonic()+READY_TIMEOUT
    last="worker not ready"
    while True:
        url=base_url()+f"/v1/workers/{quoted}/ready"
        request=urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept":"application/json",
                "Authorization":f"Bearer {api_token()}",
                "User-Agent":"CodexCI/ServerWorkerFabricHeadlessManager",
            },
        )
        try:
            with _urlopen(request) as response:
                raw=response.read()
                status=int(getattr(response,"status",200))
        except urllib.error.HTTPError as exc:
            raw=exc.read()
            try:
                body=json.loads(raw.decode()) if raw else {}
            except Exception:
                body={}
            if int(exc.code)==202:
                status=202
            else:
                detail=str(body.get("error") or f"HTTP {int(exc.code)}")
                raise FabricManagerError(
                    f"ServerWorkerFabric readiness failed: {detail}"
                ) from exc
        except (urllib.error.URLError,TimeoutError,socket.timeout,OSError) as exc:
            raise FabricManagerError("ServerWorkerFabric readiness request failed") from exc
        try:
            body=json.loads(raw.decode()) if raw else {}
        except (UnicodeDecodeError,json.JSONDecodeError) as exc:
            raise FabricManagerError("ServerWorkerFabric readiness returned invalid JSON") from exc
        if not isinstance(body,dict):
            raise FabricManagerError("ServerWorkerFabric readiness response must be an object")
        if status==200 and body.get("ready") is True:
            worker=body.get("worker")
            if not isinstance(worker,dict):
                raise FabricManagerError("ServerWorkerFabric readiness response is missing worker")
            if str(worker.get("logical_id") or "") != logical:
                raise FabricManagerError("ServerWorkerFabric readiness identity mismatch")
            address=str(worker.get("address") or "").strip()
            if not address:
                raise FabricManagerError("ServerWorkerFabric ready worker has no address")
            metadata=worker.get("metadata")
            name=""
            if isinstance(metadata,dict):
                name=str(metadata.get("name") or "").strip()
            return {
                "id":logical,
                "name":name or logical,
                "ip":address,
                "status":str(worker.get("state") or "running"),
            }
        if status != 202:
            raise FabricManagerError(
                f"ServerWorkerFabric readiness returned unexpected HTTP {status}"
            )
        last=str(body.get("reason") or "worker not ready")
        if time.monotonic() >= deadline:
            raise FabricManagerError(
                f"ServerWorkerFabric worker {logical!r} did not become ready: {last}"
            )
        time.sleep(POLL_SECONDS)


def state(history_limit=40):
    """Return read-only Fabric worker inventory for scheduler telemetry."""
    del history_limit
    _,payload=_request("GET","/v1/workers")
    workers=payload.get("workers")
    if not isinstance(workers,list):
        raise FabricManagerError("ServerWorkerFabric worker inventory is missing workers")
    visible=[worker for worker in workers if isinstance(worker,dict)]
    headless=[
        worker for worker in visible
        if str(worker.get("logical_id") or "").startswith("headless-")
        or str((worker.get("metadata") or {}).get("profile") or "") == PROFILE
    ]
    return {
        "provider":"serverworkerfabric",
        "workers":visible,
        "capacity":{
            "visibleWorkers":len(visible),
            "headlessWorkers":len(headless),
        },
    }


def finish(instance_id,status="finished",exit_code=None,reason="job_finished",keep_seconds=0):
    del status,exit_code,reason
    logical=str(instance_id or "").strip()
    if not logical:
        raise FabricManagerError("instance id is required")
    if int(keep_seconds or 0) > 0:
        raise FabricManagerError(
            "ServerWorkerFabric manager does not support retained keep_seconds"
        )
    quoted=urllib.parse.quote(logical,safe="")
    code,payload=_request("DELETE",f"/v1/headless/{quoted}",allow_404=True)
    if code==404:
        return {
            "id":logical,
            "status":"destroyed",
            "deleted":False,
            "already_absent":True,
        }
    return {
        "id":logical,
        "status":"destroyed",
        "deleted":bool(payload.get("deleted",True)),
    }


def parser():
    p=argparse.ArgumentParser(prog="serverworkerfabric-headless-manager")
    sub=p.add_subparsers(dest="action",required=True)
    r=sub.add_parser("reserve")
    r.add_argument("--owner",required=True)
    r.add_argument("--project",default="")
    r.add_argument("--ttl-seconds",type=int,default=3600)
    r.add_argument("--session-key")
    r.add_argument("--memory-mib",type=int,default=2048)
    r.add_argument("--max-memory-mib",type=int,default=4096)
    r.add_argument("--vcpus",type=int,default=2)
    r.add_argument("--disk-gib",type=int,default=40)
    st=sub.add_parser("state")
    st.add_argument("--history-limit",type=int,default=40)
    pr=sub.add_parser("provision")
    pr.add_argument("id")
    f=sub.add_parser("finish")
    f.add_argument("id")
    f.add_argument("--status",default="finished")
    f.add_argument("--exit-code",type=int)
    f.add_argument("--reason",default="job_finished")
    f.add_argument("--keep-seconds",type=int,default=0)
    return p


def main(argv=None):
    a=parser().parse_args(argv)
    try:
        if a.action=="reserve":
            result=reserve(
                a.owner,a.project,a.ttl_seconds,a.session_key,
                memory_mib=a.memory_mib,max_memory_mib=a.max_memory_mib,
                vcpus=a.vcpus,disk_gib=a.disk_gib,
            )
        elif a.action=="state":
            result=state(a.history_limit)
        elif a.action=="provision":
            result=provision(a.id)
        elif a.action=="finish":
            result=finish(
                a.id,status=a.status,exit_code=a.exit_code,
                reason=a.reason,keep_seconds=a.keep_seconds,
            )
        else:
            raise FabricManagerError(f"unsupported action {a.action!r}")
    except FabricManagerError as exc:
        print(str(exc),file=sys.stderr)
        return 1
    print(json.dumps(result,separators=(",",":")))
    return 0


if __name__=="__main__":
    raise SystemExit(main())
