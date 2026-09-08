#!/usr/bin/env python3
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE_URL = os.environ.get("CODEX_LAB_SCHEDULER_URL", "http://192.168.122.1:8766").rstrip("/")
def request(method, path, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"scheduler HTTP {exc.code}: {detail[:1000]}") from exc


def submit(arguments):
    payload = {
        "owner": arguments["owner"],
        "project": arguments.get("project"),
        "chatLabel": arguments.get("chatLabel"),
        "chatUrl": arguments.get("chatUrl"),
        "command": arguments["command"],
        "cwd": arguments.get("cwd", "/workspace"),
        "env": arguments.get("env") or {},
        "timeout": arguments.get("timeout", 3600),
        "jobClass": arguments.get("jobClass", "cpu"),
        "repoUrl": arguments.get("repoUrl"),
        "revision": arguments.get("revision"),
    }
    return request("POST", "/api/jobs", payload, timeout=15)


def submit_many(owner, jobs, project=None, chat_label=None, chat_url=None):
    if not isinstance(jobs, list) or not jobs:
        raise ValueError("jobs must be a non-empty array")
    if len(jobs) > 48:
        raise ValueError("at most 48 jobs can be submitted in one batch")

    def one(item):
        if not isinstance(item, dict) or not str(item.get("command") or "").strip():
            raise ValueError("each batch job requires command")
        payload = dict(item)
        payload["owner"] = owner
        if project and not payload.get("project"):
            payload["project"] = project
        if chat_label and not payload.get("chatLabel"):
            payload["chatLabel"] = chat_label
        if chat_url and not payload.get("chatUrl"):
            payload["chatUrl"] = chat_url
        return submit(payload)

    with ThreadPoolExecutor(max_workers=min(12, len(jobs))) as pool:
        submitted = list(pool.map(one, jobs))
    return {"count": len(submitted), "jobs": submitted}


def status(job_id, max_bytes=65536):
    return request("GET", f"/api/jobs/{urllib.parse.quote(job_id)}?maxBytes={int(max_bytes)}", timeout=10)


def list_jobs(status=None, limit=50):
    q = {"limit": int(limit)}
    if status: q["status"] = status
    return request("GET", "/api/jobs?" + urllib.parse.urlencode(q), timeout=10)


def workers():
    return request("GET", "/api/workers", timeout=10)


def cancel(job_id):
    return request("POST", f"/api/jobs/{urllib.parse.quote(job_id)}/cancel", {}, timeout=10)
