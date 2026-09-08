#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path

server = Path(__file__).with_name("server.py")
process = subprocess.Popen(
    ["python3", str(server)],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL,
    text=True,
    bufsize=1,
)


def send(message):
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
json.loads(process.stdout.readline())
start = time.monotonic()
ids = list(range(10, 14))
for request_id in ids:
    send({
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": "host_exec", "arguments": {"command": "sleep 1; printf done"}},
    })
seen = set()
while seen != set(ids):
    response = json.loads(process.stdout.readline())
    if response.get("id") in ids:
        seen.add(response["id"])
elapsed = time.monotonic() - start
process.stdin.close()
process.wait(timeout=10)
if elapsed >= 2.0:
    raise SystemExit(f"Concurrency regression: four 1s jobs took {elapsed:.3f}s")
print(json.dumps({"ok": True, "jobs": len(ids), "elapsedSeconds": round(elapsed, 3)}))
