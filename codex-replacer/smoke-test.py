#!/usr/bin/env python3

import json
import os
import subprocess
import sys
import time
import uuid


SERVER = "/home/mark/docker/markshaw-private-mcp/codex-replacer/server.py"


class Client:
    def __init__(self):
        self.next_id = 1
        process_env = os.environ.copy()
        process_env["CODEX_REPLACER_BROWSER_PROFILE"] = f"smoke-{uuid.uuid4().hex[:12]}"
        self.process = subprocess.Popen(
            ["python3", SERVER],
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )

    def request(self, method, params=None):
        request_id = self.next_id
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError(f"Server stopped while waiting for {method}.")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(response["error"])
            return response["result"]

    def notify(self, method, params=None):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.process.stdin.flush()

    def call(self, name, arguments=None):
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise RuntimeError(result)
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=10)


def structured(result):
    return result.get("structuredContent", {})


def main():
    client = Client()
    test_root = f"/tmp/codex-replacer-smoke-{uuid.uuid4().hex}"
    checks = {}
    try:
        initialized = client.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "codex-replacer-smoke-test", "version": "1.0.0"},
        })
        if "continuity guard" not in initialized.get("instructions", "").lower():
            raise RuntimeError("Conversation continuity instructions are missing.")
        client.notify("notifications/initialized")

        tools = client.request("tools/list").get("tools", [])
        name_list = [item["name"] for item in tools]
        names = set(name_list)
        required = {
            "fs_read", "fs_write", "fs_search", "host_exec", "process_start",
            "git", "github", "docker", "browser_navigate", "browser_take_screenshot",
            "prepare_chat_handoff", "chatgpt_start_chat", "chatgpt_browser_status", "chatgpt_auth_begin",
        }
        missing = sorted(required - names)
        if missing:
            raise RuntimeError(f"Missing tools: {missing}")
        if name_list.count("chatgpt_start_chat") != 1:
            raise RuntimeError("chatgpt_start_chat must be exposed exactly once.")
        if name_list.count("chatgpt_auth_begin") != 1:
            raise RuntimeError("chatgpt_auth_begin must be exposed exactly once.")
        auth_tool = next(item for item in tools if item["name"] == "chatgpt_auth_begin")
        auth_methods = auth_tool.get("inputSchema", {}).get("properties", {}).get("method", {}).get("enum", [])
        if auth_methods != ["passkey", "phone_prompt"]:
            raise RuntimeError(f"chatgpt_auth_begin exposed unexpected methods: {auth_methods}")
        invalid_chat_start = client.request("tools/call", {
            "name": "chatgpt_start_chat",
            "arguments": {"message": "   "},
        })
        if invalid_chat_start.get("isError") is not True or "message is required" not in json.dumps(invalid_chat_start):
            raise RuntimeError("chatgpt_start_chat did not reject empty input locally.")
        checks["tools"] = len(tools)

        handoff = structured(client.call("prepare_chat_handoff", {
            "objective": "Continue the active task",
            "currentState": "Work is partially complete",
            "completed": ["First step finished"],
            "pending": ["Second step remains"],
            "exactReferences": ["repo: example/repo"],
            "nextActions": ["Continue without redoing completed work"],
        }))
        handoff_text = handoff.get("handoff", "")
        if "NEW CHAT HANDOFF" not in handoff_text or "First step finished" not in handoff_text or "Second step remains" not in handoff_text:
            raise RuntimeError("Chat handoff formatting failed.")
        checks["conversationContinuity"] = True

        client.call("fs_mkdir", {"path": test_root})
        file_path = f"{test_root}/hello.txt"
        client.call("fs_write", {"path": file_path, "content": "hello codex replacer\n"})
        read = structured(client.call("fs_read", {"path": file_path}))
        if read.get("data") != "hello codex replacer\n":
            raise RuntimeError("File read did not match the write.")
        client.call("fs_replace", {
            "path": file_path,
            "oldText": "hello",
            "newText": "working",
            "expectedOccurrences": 1,
        })
        found = structured(client.call("fs_search", {
            "pattern": "working codex replacer",
            "paths": [test_root],
            "fixedStrings": True,
        }))
        if not found.get("matches"):
            raise RuntimeError("File search did not find the replacement.")
        checks["filesystem"] = True

        command = structured(client.call("host_exec", {
            "command": "printf 'host-command-ok'",
            "cwd": test_root,
        }))
        if command.get("exitCode") != 0 or command.get("stdout") != "host-command-ok":
            raise RuntimeError("Host command execution failed.")
        checks["command"] = True

        started = structured(client.call("process_start", {
            "command": "printf 'start\\n'; sleep 0.2; printf 'finish\\n'",
            "cwd": test_root,
        }))
        session_id = started["sessionId"]
        snapshot = started
        deadline = time.time() + 10
        while snapshot.get("running") and time.time() < deadline:
            time.sleep(0.1)
            snapshot = structured(client.call("process_poll", {"sessionId": session_id}))
        output = "".join(event["text"] for event in snapshot.get("events", []))
        if snapshot.get("running") or "finish" not in output:
            raise RuntimeError("Background process did not complete correctly.")
        checks["processes"] = True

        git = structured(client.call("git", {"args": ["init"], "cwd": test_root}))
        if git.get("exitCode") != 0:
            raise RuntimeError("Git command failed.")
        checks["git"] = True

        docker = structured(client.call("docker", {"args": ["version", "--format", "{{.Server.Version}}"]}))
        if docker.get("exitCode") != 0 or not docker.get("stdout", "").strip():
            raise RuntimeError("Docker command failed.")
        checks["docker"] = docker["stdout"].strip()

        client.call("browser_navigate", {"url": "https://example.com/"})
        screenshot = client.call("browser_take_screenshot", {"type": "png", "scale": "css"})
        if not any(item.get("type") == "image" for item in screenshot.get("content", [])):
            raise RuntimeError("Visual browser did not return an image.")
        checks["visualBrowser"] = True

        print(json.dumps({"ok": True, "checks": checks}, sort_keys=True))
    finally:
        try:
            client.call("fs_delete", {"path": test_root, "recursive": True})
        except Exception:
            pass
        client.close()


if __name__ == "__main__":
    main()
