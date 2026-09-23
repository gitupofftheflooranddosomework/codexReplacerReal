#!/usr/bin/env python3
"""External candidate probe for isolated G0C scheduler validation."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import urllib.parse
import urllib.request
import uuid


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=2) as response:
        value = json.load(response)
    return value if isinstance(value, dict) else {}


def post_json(url: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        value = json.load(response)
    return value if isinstance(value, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduler-url", required=True)
    parser.add_argument("--admission-url", required=True)
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--log-file", required=True)
    parser.add_argument("--max-load-per-cpu", type=float, default=2.0)
    parser.add_argument("--min-free-bytes", type=int, default=256 * 1024 * 1024)
    args = parser.parse_args()
    result = {name: False for name in (
        "process_ok", "capability_ok", "dependencies_ok", "resources_ok", "logs_ok", "isolation_ok"
    )}
    try:
        scheduler = args.scheduler_url.rstrip("/")
        health = get_json(scheduler + "/health")
        result["process_ok"] = health.get("ok") is True
        operation_id = "probe-" + uuid.uuid4().hex
        first = post_json(scheduler + "/api/canary", {"operationId": operation_id})
        second = post_json(scheduler + "/api/canary", {"operationId": operation_id})
        result["capability_ok"] = (
            first.get("ok") is True
            and first.get("created") is True
            and second.get("ok") is True
            and second.get("created") is False
            and first.get("createdAt") == second.get("createdAt")
        )
        admission = get_json(args.admission_url.rstrip("/") + "/healthz")
        result["dependencies_ok"] = admission.get("ok") is True
        state = pathlib.Path(args.state_path)
        usage = shutil.disk_usage(state.parent)
        cpu_count = max(1, os.cpu_count() or 1)
        load = os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0
        result["resources_ok"] = (
            state.exists()
            and usage.free >= args.min_free_bytes
            and load / cpu_count <= args.max_load_per_cpu
        )
        log_path = pathlib.Path(args.log_file)
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-65536:] if log_path.exists() else ""
        result["logs_ok"] = not any(
            marker in tail.lower() for marker in ("traceback", "database is locked", "disk i/o error", "corrupt")
        )
        result["isolation_ok"] = (
            urllib.parse.urlsplit(args.scheduler_url).hostname in {"127.0.0.1", "localhost", "::1"}
            and urllib.parse.urlsplit(args.admission_url).hostname in {"127.0.0.1", "localhost", "::1"}
        )
    except Exception:
        pass
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
