#!/usr/bin/env python3

import json
import os
import subprocess
import threading
import time
import urllib.request
from pathlib import Path


HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("CODEX_REPLACER_TEST_HTTP_PORT", "18791"))
URL = f"http://127.0.0.1:{PORT}/mcp"


def request(payload, timeout=10):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status == 202:
            return None
        return json.loads(response.read())


def main():
    env = os.environ.copy()
    env["CODEX_REPLACER_HTTP_PORT"] = str(PORT)
    env["CODEX_REPLACER_MAX_WORKERS"] = "20"
    process = subprocess.Popen(
        ["python3", str(HERE / "http-server.py")],
        cwd=HERE,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/healthz", timeout=0.5
                ) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(0.05)
        else:
            raise RuntimeError("HTTP MCP service did not become ready.")

        initialized = request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "http-test", "version": "1"},
                },
            }
        )
        if (
            initialized.get("result", {}).get("serverInfo", {}).get("name")
            != "codex-replacer"
        ):
            raise RuntimeError("HTTP initialize failed.")

        jobs = 8
        barrier = threading.Barrier(jobs + 1)
        results = [None] * jobs

        def worker(index):
            barrier.wait()
            results[index] = request(
                {
                    "jsonrpc": "2.0",
                    "id": 100 + index,
                    "method": "tools/call",
                    "params": {
                        "name": "host_exec",
                        "arguments": {"command": "sleep 1; printf done"},
                    },
                },
                timeout=5,
            )

        threads = [
            threading.Thread(target=worker, args=(index,)) for index in range(jobs)
        ]
        for thread in threads:
            thread.start()
        started = time.monotonic()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        elapsed = time.monotonic() - started

        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("Concurrent HTTP request did not finish.")
        if elapsed >= 2.5:
            raise RuntimeError(
                f"HTTP concurrency regression: {jobs} one-second jobs took {elapsed:.3f}s"
            )
        for response in results:
            result = response.get("result", {}).get("structuredContent", {})
            if result.get("exitCode") != 0 or result.get("stdout") != "done":
                raise RuntimeError(f"Unexpected command result: {response}")

        # Simulate an upstream client timing out while its command is still
        # executing. The persistent HTTP service must survive and immediately
        # accept an unrelated request.
        abandoned_request = urllib.request.Request(
            URL,
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 999,
                    "method": "tools/call",
                    "params": {
                        "name": "host_exec",
                        "arguments": {"command": "sleep 2; printf abandoned"},
                    },
                }
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

        def abandon():
            try:
                urllib.request.urlopen(abandoned_request, timeout=0.2).read()
            except Exception:
                pass

        abandoned = threading.Thread(target=abandon)
        abandoned.start()
        time.sleep(0.35)
        probe = request(
            {
                "jsonrpc": "2.0",
                "id": 1000,
                "method": "tools/call",
                "params": {
                    "name": "host_exec",
                    "arguments": {"command": "printf survivor"},
                },
            },
            timeout=2,
        )
        survivor = probe.get("result", {}).get("structuredContent", {})
        if survivor.get("stdout") != "survivor":
            raise RuntimeError(
                "Abandoned HTTP connection affected an unrelated request."
            )
        abandoned.join(timeout=4)

        print(
            json.dumps(
                {
                    "ok": True,
                    "transport": "streamable-http",
                    "parallelJobs": jobs,
                    "elapsedSeconds": round(elapsed, 3),
                    "abandonedConnectionIsolation": True,
                },
                sort_keys=True,
            )
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


if __name__ == "__main__":
    main()
