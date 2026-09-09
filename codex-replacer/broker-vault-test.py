#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path


SERVER = Path(__file__).with_name("server.py")


def request(process, method, params=None, request_id=1):
    process.stdin.write(json.dumps({
        "jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {},
    }) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())["result"]


def main():
    environment = os.environ.copy()
    environment["CODEX_REPLACER_BROKER_ONLY"] = "1"
    environment.pop("CODEX_VAULT_ORGANIZATION_ID", None)
    process = subprocess.Popen(
        [sys.executable, str(SERVER)],
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    try:
        tools = request(process, "tools/list").get("tools", [])
        names = {item["name"] for item in tools}
        assert {"dotmoose_vault_list", "dotmoose_vault_get"} <= names
        assert "host_exec" not in names
        unavailable = request(process, "tools/call", {
            "name": "dotmoose_vault_list", "arguments": {},
        }, request_id=2)
        assert unavailable.get("isError") is True
        rendered = json.dumps(unavailable)
        assert "not configured" in rendered
        assert "session" not in unavailable.get("structuredContent", {})
        print(json.dumps({"ok": True, "brokerVaultTools": True}))
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
