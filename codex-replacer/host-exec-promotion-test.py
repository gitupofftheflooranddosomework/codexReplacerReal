#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SERVER = str(Path(__file__).with_name("server.py"))


class Client:
    def __init__(self):
        env = os.environ.copy()
        env["CODEX_REPLACER_HOST_EXEC_FOREGROUND_SECONDS"] = "1"
        env["CODEX_REPLACER_HOST_EXEC_WAIT_PROMOTION_SECONDS"] = "1"
        env["CODEX_REPLACER_HOST_EXEC_NICE"] = "0"
        self.p = subprocess.Popen(
            ["python3", SERVER],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self.i = 1

    def request(self, method, params=None):
        rid = self.i
        self.i += 1
        self.p.stdin.write(json.dumps({"jsonrpc":"2.0","id":rid,"method":method,"params":params or {}})+"\n")
        self.p.stdin.flush()
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("server closed")
            d = json.loads(line)
            if d.get("id") == rid:
                return d["result"]

    def call(self, name, args=None):
        return self.request("tools/call", {"name": name, "arguments": args or {}})["structuredContent"]

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            self.p.wait(timeout=5)


def poll_until_done(c, sid, timeout=5):
    deadline = time.monotonic() + timeout
    seq = 0
    text = []
    last = None
    while time.monotonic() < deadline:
        last = c.call("process_poll", {"sessionId": sid, "afterSequence": seq})
        text.extend(event["text"] for event in last.get("events", []))
        seq = last.get("nextSequence", seq)
        if not last.get("running"):
            return last, "".join(text)
        time.sleep(0.05)
    raise RuntimeError("promoted process did not finish")


def main():
    c = Client()
    try:
        t = time.monotonic()
        wait = c.call("host_exec", {"command": "sleep 1.2; printf wait-done", "timeout": 5})
        elapsed_wait = time.monotonic() - t
        if not wait.get("running") or not wait.get("promotedToBackground") or wait.get("promotionReason") != "leading_wait":
            raise RuntimeError(f"leading wait not promoted: {wait}")
        if elapsed_wait > 0.8:
            raise RuntimeError(f"leading wait promotion was slow: {elapsed_wait:.3f}s")
        final_wait, text_wait = poll_until_done(c, wait["sessionId"])
        if final_wait.get("exitCode") != 0 or "wait-done" not in text_wait:
            raise RuntimeError("promoted leading wait did not complete")

        t = time.monotonic()
        slow = c.call("host_exec", {"command": "python3 -c 'import time; time.sleep(2); print(\"slow-done\")'", "timeout": 5})
        elapsed_slow = time.monotonic() - t
        if not slow.get("running") or slow.get("promotionReason") != "foreground_budget_exceeded":
            raise RuntimeError(f"slow command not promoted after budget: {slow}")
        if elapsed_slow > 1.6:
            raise RuntimeError(f"foreground budget return was slow: {elapsed_slow:.3f}s")
        final_slow, text_slow = poll_until_done(c, slow["sessionId"])
        if final_slow.get("exitCode") != 0 or "slow-done" not in text_slow:
            raise RuntimeError("promoted slow command did not complete")

        quick = c.call("host_exec", {"command": "printf quick"})
        if quick.get("running") or quick.get("stdout") != "quick" or quick.get("exitCode") != 0:
            raise RuntimeError(f"quick command behavior regressed: {quick}")

        stdin_result = c.call("host_exec", {"command": "cat", "stdin": "stdin-ok"})
        if stdin_result.get("stdout") != "stdin-ok" or stdin_result.get("exitCode") != 0:
            raise RuntimeError(f"stdin behavior regressed: {stdin_result}")

        timed = c.call("host_exec", {"command": "python3 -c 'import time; time.sleep(2)'", "timeout": 1})
        if timed.get("running") or not timed.get("timedOut") or timed.get("exitCode") is not None:
            raise RuntimeError(f"short timeout behavior regressed: {timed}")

        print(json.dumps({
            "ok": True,
            "leadingWaitReturnSeconds": round(elapsed_wait, 3),
            "foregroundBudgetReturnSeconds": round(elapsed_slow, 3),
            "quickCommandCompatible": True,
            "stdinCompatible": True,
            "shortTimeoutCompatible": True,
        }, sort_keys=True))
    finally:
        c.close()


if __name__ == "__main__":
    main()
