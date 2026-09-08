#!/usr/bin/env python3
import argparse
import json
import time
import urllib.request


def request(base, method, path, payload=None, timeout=10):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://192.168.122.1:8766")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--sleep", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()

    ids = []
    started = time.monotonic()
    for i in range(args.jobs):
        job = request(args.base, "POST", "/api/jobs", {
            "owner": "Scheduler-Smoke",
            "project": "scheduler-smoke-test",
            "jobClass": "test",
            "command": f"sleep {args.sleep}; printf 'smoke-{i + 1}-%s' \"$(hostname)\"",
            "timeout": int(args.timeout),
        })
        ids.append(job["id"])

    deadline = time.monotonic() + args.timeout
    states = []
    while time.monotonic() < deadline:
        states = [request(args.base, "GET", f"/api/jobs/{job_id}?maxBytes=4096") for job_id in ids]
        if all(state["status"] not in {"queued", "running"} for state in states):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("scheduler smoke test timed out")

    elapsed = time.monotonic() - started
    failures = [state for state in states if state["status"] != "succeeded"]
    if failures:
        raise RuntimeError(f"jobs failed: {failures}")
    print(json.dumps({
        "ok": True,
        "jobs": args.jobs,
        "elapsedSeconds": round(elapsed, 3),
        "stations": [state.get("station") for state in states],
        "uniqueStations": sorted({state.get("station") for state in states}),
        "outputs": [state.get("stdoutTail", "").strip() for state in states],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
