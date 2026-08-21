#!/usr/bin/env python3

import base64
import hashlib
import json
import mimetypes
import os
import queue
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


SERVER_NAME = "codex-replacer"
SERVER_VERSION = "1.2.0"
DEFAULT_DIRECTORY = "/home/mark"
MAX_CAPTURE_BYTES = 4 * 1024 * 1024


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def absolute_path(value, default=DEFAULT_DIRECTORY):
    raw = value if isinstance(value, str) and value else default
    return os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))


def clipped_text(value, maximum=MAX_CAPTURE_BYTES):
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8", errors="replace")
    truncated = len(data) > maximum
    if truncated:
        data = data[:maximum]
    return data.decode("utf-8", errors="replace"), truncated


def tool_result(data=None, message=None, content=None, is_error=False):
    payload = {}
    if data is not None:
        payload["structuredContent"] = data
    if content is not None:
        payload["content"] = content
    elif message is not None:
        payload["content"] = [{"type": "text", "text": message}]
    elif data is not None:
        payload["content"] = [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]
    else:
        payload["content"] = []
    if is_error:
        payload["isError"] = True
    return payload


def run_program(program, args, cwd=None, timeout=120, input_text=None, env=None, maximum=MAX_CAPTURE_BYTES):
    command = [program, *[str(item) for item in args]]
    process_env = os.environ.copy()
    if isinstance(env, dict):
        process_env.update({str(key): str(value) for key, value in env.items()})
    try:
        completed = subprocess.run(
            command,
            cwd=absolute_path(cwd),
            env=process_env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, min(int(timeout), 86400)),
            check=False,
        )
        stdout, stdout_truncated = clipped_text(completed.stdout, maximum)
        stderr, stderr_truncated = clipped_text(completed.stderr, maximum)
        return {
            "command": command,
            "cwd": absolute_path(cwd),
            "exitCode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timedOut": False,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as error:
        stdout, stdout_truncated = clipped_text(error.stdout or b"", maximum)
        stderr, stderr_truncated = clipped_text(error.stderr or b"", maximum)
        return {
            "command": command,
            "cwd": absolute_path(cwd),
            "exitCode": None,
            "stdout": stdout,
            "stderr": stderr,
            "timedOut": True,
            "truncated": stdout_truncated or stderr_truncated,
        }


class ProcessSession:
    def __init__(self, command, cwd, env, interactive):
        self.id = uuid.uuid4().hex
        self.command = command
        self.cwd = absolute_path(cwd)
        self.interactive = bool(interactive)
        self.started_at = now_iso()
        self.lock = threading.Lock()
        self.events = deque(maxlen=4000)
        self.next_sequence = 1
        process_env = os.environ.copy()
        if isinstance(env, dict):
            process_env.update({str(key): str(value) for key, value in env.items()})
        launch = ["/bin/bash", "-lc", command]
        if self.interactive:
            launch = ["/usr/bin/script", "-qefc", command, "/dev/null"]
        self.process = subprocess.Popen(
            launch,
            cwd=self.cwd,
            env=process_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        self.stdout_thread = threading.Thread(target=self._read_stream, args=("stdout", self.process.stdout), daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stream, args=("stderr", self.process.stderr), daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _read_stream(self, stream_name, stream):
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            text = chunk.decode("utf-8", errors="replace")
            with self.lock:
                self.events.append({
                    "sequence": self.next_sequence,
                    "stream": stream_name,
                    "text": text,
                    "timestamp": now_iso(),
                })
                self.next_sequence += 1

    def snapshot(self, after_sequence=0):
        with self.lock:
            events = [event for event in self.events if event["sequence"] > after_sequence]
            next_sequence = self.next_sequence - 1
        exit_code = self.process.poll()
        return {
            "sessionId": self.id,
            "command": self.command,
            "cwd": self.cwd,
            "interactive": self.interactive,
            "startedAt": self.started_at,
            "running": exit_code is None,
            "exitCode": exit_code,
            "events": events,
            "nextSequence": next_sequence,
        }

    def write(self, data):
        if self.process.poll() is not None:
            raise RuntimeError("The process is no longer running.")
        self.process.stdin.write(data.encode("utf-8"))
        self.process.stdin.flush()

    def stop(self, force=False):
        if self.process.poll() is not None:
            return self.process.returncode
        sig = signal.SIGKILL if force else signal.SIGTERM
        os.killpg(self.process.pid, sig)
        try:
            return self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            return self.process.wait(timeout=5)


class ProcessManager:
    def __init__(self):
        self.sessions = {}
        self.lock = threading.Lock()

    def start(self, command, cwd=None, env=None, interactive=False):
        session = ProcessSession(command, cwd, env, interactive)
        with self.lock:
            self.sessions[session.id] = session
        return session.snapshot()

    def get(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown process session: {session_id}")
        return session

    def list(self):
        with self.lock:
            sessions = list(self.sessions.values())
        return [session.snapshot() | {"events": []} for session in sessions]

    def stop_all(self):
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            try:
                session.stop()
            except Exception:
                pass


class BrowserClient:
    def __init__(self):
        self.process = None
        self.next_id = 1
        self.tools = None
        self.lock = threading.Lock()

    def _start(self):
        self.close()
        self.process = subprocess.Popen(
            [
                "docker", "exec", "-i", "markshaw-browser-mcp",
                "node", "/opt/browser-mcp/browser-proxy.mjs", os.environ.get("CODEX_REPLACER_BROWSER_PROFILE", "codex"),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
        self._notify("notifications/initialized", {})

    def _ensure(self):
        if self.process is None or self.process.poll() is not None:
            self._start()

    def _write(self, message):
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("The visual browser MCP connection closed unexpectedly.")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "Visual browser MCP error."))
            return response.get("result", {})

    def list_tools(self):
        with self.lock:
            self._ensure()
            if self.tools is None:
                self.tools = self._request("tools/list", {}).get("tools", [])
            return self.tools

    def call(self, name, arguments):
        with self.lock:
            for attempt in range(2):
                try:
                    self._ensure()
                    return self._request("tools/call", {"name": name, "arguments": arguments})
                except Exception:
                    self.close()
                    if attempt:
                        raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
        self.tools = None


class HeadedChatGPTClient:
    """Private Playwright MCP client attached to the normal headed Chromium session.

    This client is intentionally not exposed as a general-purpose browser. It exists
    only so purpose-built ChatGPT account tools can operate in the user's persistent,
    authenticated browser without weakening the sandboxed browser MCP.
    """

    def __init__(self):
        self.process = None
        self.next_id = 1
        self.lock = threading.Lock()

    def _ensure_browser_service(self):
        completed = subprocess.run(
            ["systemctl", "--user", "start", "codex-chatgpt-browser.service"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Could not start codex-chatgpt-browser.service: "
                + (completed.stderr.strip() or completed.stdout.strip() or "unknown systemd error")
            )

        deadline = time.time() + 15
        last_error = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2) as response:
                    if response.status == 200:
                        return
            except Exception as error:
                last_error = error
            time.sleep(0.25)
        raise RuntimeError(f"Headed ChatGPT browser CDP endpoint did not become ready: {last_error}")

    def _start(self):
        self.close()
        self._ensure_browser_service()
        image = os.environ.get("CODEX_REPLACER_BROWSER_IMAGE", "markshaw-private-mcp_browser:latest")
        self.process = subprocess.Popen(
            [
                "docker", "run", "--rm", "-i", "--network", "host",
                "--entrypoint", "node", image,
                "/app/cli.js",
                "--cdp-endpoint", "http://127.0.0.1:9222",
                "--caps", "vision",
                "--image-responses", "omit",
                "--snapshot-boxes",
                "--codegen", "none",
                "--timeout-action", "10000",
                "--timeout-navigation", "90000",
                "--timeout-settle", "750",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            text=True,
            bufsize=1,
        )
        self._request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": f"{SERVER_NAME}-headed-chatgpt", "version": SERVER_VERSION},
        })
        self._notify("notifications/initialized", {})

    def _ensure(self):
        if self.process is None or self.process.poll() is not None:
            self._start()

    def _write(self, message):
        self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def _notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method, params):
        request_id = self.next_id
        self.next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        while True:
            line = self.process.stdout.readline()
            if not line:
                raise RuntimeError("The headed ChatGPT browser MCP connection closed unexpectedly.")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "Headed ChatGPT browser MCP error."))
            return response.get("result", {})

    def call(self, name, arguments=None):
        with self.lock:
            for attempt in range(2):
                try:
                    self._ensure()
                    result = self._request("tools/call", {"name": name, "arguments": arguments or {}})
                    if result.get("isError"):
                        text = "\n".join(
                            item.get("text", "")
                            for item in result.get("content", [])
                            if item.get("type") == "text"
                        ).strip()
                        raise RuntimeError(text or f"{name} failed in headed ChatGPT browser.")
                    return result
                except Exception:
                    self.close()
                    if attempt:
                        raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None


PROCESS_MANAGER = ProcessManager()
BROWSER_CLIENT = BrowserClient()
CHATGPT_BROWSER_CLIENT = HeadedChatGPTClient()


def handle_system_info(_arguments):
    data = {
        "hostname": socket.gethostname(),
        "user": os.environ.get("USER", "mark"),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "groups": os.getgroups(),
        "home": str(Path.home()),
        "workingDirectory": os.getcwd(),
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "time": now_iso(),
        "executables": {
            name: shutil.which(name)
            for name in ["bash", "python3", "node", "git", "gh", "docker", "rg", "curl", "ssh"]
        },
    }
    return tool_result(data)


def handle_fs_stat(arguments):
    path = absolute_path(arguments.get("path"))
    info = os.lstat(path)
    data = {
        "path": path,
        "type": "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file",
        "size": info.st_size,
        "mode": stat.filemode(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
        "modifiedAt": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        "createdAt": datetime.fromtimestamp(info.st_ctime, timezone.utc).isoformat(),
        "sha256": None,
        "linkTarget": os.readlink(path) if stat.S_ISLNK(info.st_mode) else None,
    }
    if stat.S_ISREG(info.st_mode) and arguments.get("hash", False):
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        data["sha256"] = digest.hexdigest()
    return tool_result(data)


def handle_fs_list(arguments):
    root = absolute_path(arguments.get("path"))
    recursive = bool(arguments.get("recursive", False))
    max_depth = max(0, min(int(arguments.get("maxDepth", 4)), 100))
    max_entries = max(1, min(int(arguments.get("maxEntries", 1000)), 10000))
    entries = []

    def add_entry(path):
        info = os.lstat(path)
        entries.append({
            "path": path,
            "name": os.path.basename(path),
            "type": "symlink" if stat.S_ISLNK(info.st_mode) else "directory" if stat.S_ISDIR(info.st_mode) else "file",
            "size": info.st_size,
            "mode": stat.filemode(info.st_mode),
            "modifiedAt": datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
        })

    if not os.path.isdir(root):
        add_entry(root)
    elif not recursive:
        for name in sorted(os.listdir(root)):
            add_entry(os.path.join(root, name))
            if len(entries) >= max_entries:
                break
    else:
        root_depth = root.rstrip(os.sep).count(os.sep)
        for current, directories, files in os.walk(root, followlinks=False):
            depth = current.rstrip(os.sep).count(os.sep) - root_depth
            if depth >= max_depth:
                directories[:] = []
            directories.sort()
            files.sort()
            for name in [*directories, *files]:
                add_entry(os.path.join(current, name))
                if len(entries) >= max_entries:
                    break
            if len(entries) >= max_entries:
                break
    return tool_result({"root": root, "entries": entries, "truncated": len(entries) >= max_entries})


def handle_fs_read(arguments):
    path = absolute_path(arguments.get("path"))
    offset = max(0, int(arguments.get("offset", 0)))
    length = max(1, min(int(arguments.get("length", 1024 * 1024)), 16 * 1024 * 1024))
    encoding = arguments.get("encoding", "utf8")
    with open(path, "rb") as stream:
        stream.seek(offset)
        data = stream.read(length)
        more = bool(stream.read(1))
    if encoding == "base64":
        value = base64.b64encode(data).decode("ascii")
    else:
        value = data.decode("utf-8", errors="replace")
    return tool_result({
        "path": path,
        "offset": offset,
        "bytesRead": len(data),
        "more": more,
        "encoding": encoding,
        "data": value,
    })


def handle_fs_search(arguments):
    paths = arguments.get("paths") or [arguments.get("path") or DEFAULT_DIRECTORY]
    command = ["--json", "--line-number", "--column", "--max-count", str(max(1, min(int(arguments.get("maxResults", 500)), 5000)))]
    if arguments.get("fixedStrings"):
        command.append("--fixed-strings")
    if arguments.get("ignoreCase"):
        command.append("--ignore-case")
    if arguments.get("hidden"):
        command.append("--hidden")
    for glob in arguments.get("globs") or []:
        command.extend(["--glob", str(glob)])
    command.append(str(arguments["pattern"]))
    command.extend([absolute_path(path) for path in paths])
    completed = subprocess.run(["rg", *command], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    matches = []
    for line in completed.stdout.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match":
            continue
        data = record["data"]
        for submatch in data.get("submatches", []):
            matches.append({
                "path": data["path"]["text"],
                "line": data["line_number"],
                "column": submatch["start"] + 1,
                "text": data["lines"]["text"].rstrip("\r\n"),
            })
    return tool_result({"matches": matches, "exitCode": completed.returncode, "stderr": completed.stderr})


def decode_content(arguments):
    content = arguments.get("content", "")
    if arguments.get("encoding", "utf8") == "base64":
        return base64.b64decode(content)
    return str(content).encode("utf-8")


def handle_fs_write(arguments):
    path = absolute_path(arguments.get("path"))
    data = decode_content(arguments)
    if arguments.get("createParents", True):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if arguments.get("append", False):
        with open(path, "ab") as stream:
            stream.write(data)
    else:
        directory = os.path.dirname(path) or "."
        descriptor, temporary = tempfile.mkstemp(prefix=".codex-replacer-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            if os.path.exists(path):
                os.chmod(temporary, stat.S_IMODE(os.stat(path).st_mode))
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return tool_result({"path": path, "bytesWritten": len(data), "append": bool(arguments.get("append", False))})


def handle_fs_replace(arguments):
    path = absolute_path(arguments.get("path"))
    encoding = arguments.get("encoding", "utf-8")
    with open(path, "r", encoding=encoding) as stream:
        original = stream.read()
    old = arguments["oldText"]
    count = original.count(old)
    expected = arguments.get("expectedOccurrences")
    if expected is not None and count != int(expected):
        raise RuntimeError(f"Expected {expected} occurrences but found {count}.")
    limit = int(arguments.get("count", -1))
    updated = original.replace(old, arguments.get("newText", ""), limit)
    with open(path, "w", encoding=encoding, newline="") as stream:
        stream.write(updated)
    return tool_result({"path": path, "occurrencesFound": count, "changed": updated != original})


def handle_fs_patch(arguments):
    cwd = absolute_path(arguments.get("cwd"))
    patch_text = arguments["patch"]
    descriptor, patch_path = tempfile.mkstemp(prefix="codex-replacer-", suffix=".patch")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(patch_text)
        args = ["apply"]
        if arguments.get("checkOnly", False):
            args.append("--check")
        args.append(patch_path)
        return tool_result(run_program("git", args, cwd=cwd, timeout=arguments.get("timeout", 120)))
    finally:
        os.unlink(patch_path)


def handle_fs_mkdir(arguments):
    path = absolute_path(arguments.get("path"))
    os.makedirs(path, exist_ok=bool(arguments.get("existOk", True)))
    return tool_result({"path": path, "created": True})


def handle_fs_copy(arguments):
    source = absolute_path(arguments.get("source"))
    destination = absolute_path(arguments.get("destination"))
    if os.path.isdir(source) and not os.path.islink(source):
        shutil.copytree(source, destination, dirs_exist_ok=bool(arguments.get("overwrite", False)))
    else:
        os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
        shutil.copy2(source, destination)
    return tool_result({"source": source, "destination": destination})


def handle_fs_move(arguments):
    source = absolute_path(arguments.get("source"))
    destination = absolute_path(arguments.get("destination"))
    os.makedirs(os.path.dirname(destination) or ".", exist_ok=True)
    result = shutil.move(source, destination)
    return tool_result({"source": source, "destination": absolute_path(result)})


def handle_fs_delete(arguments):
    path = absolute_path(arguments.get("path"))
    if os.path.isdir(path) and not os.path.islink(path):
        if arguments.get("recursive", False):
            shutil.rmtree(path)
        else:
            os.rmdir(path)
    else:
        os.unlink(path)
    return tool_result({"path": path, "deleted": True})


def handle_image_view(arguments):
    path = absolute_path(arguments.get("path"))
    maximum = max(1, min(int(arguments.get("maxBytes", 20 * 1024 * 1024)), 50 * 1024 * 1024))
    with open(path, "rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise RuntimeError(f"Image exceeds the {maximum}-byte limit.")
    mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return tool_result(
        {"path": path, "mimeType": mime_type, "bytes": len(data)},
        content=[{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type}],
    )


def handle_host_exec(arguments):
    cwd = absolute_path(arguments.get("cwd"))
    command = arguments["command"]
    process_env = os.environ.copy()
    process_env.update({str(key): str(value) for key, value in (arguments.get("env") or {}).items()})
    timeout = max(1, min(int(arguments.get("timeout", 120)), 86400))
    maximum = max(1024, min(int(arguments.get("maxOutputBytes", MAX_CAPTURE_BYTES)), 32 * 1024 * 1024))
    as_root = bool(arguments.get("asRoot", False))
    launch = ["/bin/bash", "-lc", command]
    if as_root:
        # Passwordless sudo is deliberately available to this private operator.
        # Use env so caller-supplied variables survive sudo's env_reset policy.
        env_args = [f"{key}={value}" for key, value in process_env.items()]
        launch = ["sudo", "-n", "env", *env_args, "/bin/bash", "-lc", command]
    try:
        completed = subprocess.run(
            launch,
            cwd=cwd,
            env=process_env,
            input=arguments.get("stdin"),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        stdout, stdout_truncated = clipped_text(completed.stdout, maximum)
        stderr, stderr_truncated = clipped_text(completed.stderr, maximum)
        data = {
            "command": command,
            "cwd": cwd,
            "asRoot": as_root,
            "exitCode": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "timedOut": False,
            "truncated": stdout_truncated or stderr_truncated,
        }
    except subprocess.TimeoutExpired as error:
        stdout, stdout_truncated = clipped_text(error.stdout or b"", maximum)
        stderr, stderr_truncated = clipped_text(error.stderr or b"", maximum)
        data = {
            "command": command,
            "cwd": cwd,
            "asRoot": as_root,
            "exitCode": None,
            "stdout": stdout,
            "stderr": stderr,
            "timedOut": True,
            "truncated": stdout_truncated or stderr_truncated,
        }
    return tool_result(data)


def handle_process_start(arguments):
    return tool_result(PROCESS_MANAGER.start(
        arguments["command"],
        arguments.get("cwd"),
        arguments.get("env"),
        arguments.get("interactive", False),
    ))


def handle_process_poll(arguments):
    session = PROCESS_MANAGER.get(arguments["sessionId"])
    return tool_result(session.snapshot(int(arguments.get("afterSequence", 0))))


def handle_process_write(arguments):
    session = PROCESS_MANAGER.get(arguments["sessionId"])
    session.write(arguments.get("data", ""))
    return tool_result({"sessionId": session.id, "written": len(arguments.get("data", ""))})


def handle_process_stop(arguments):
    session = PROCESS_MANAGER.get(arguments["sessionId"])
    exit_code = session.stop(bool(arguments.get("force", False)))
    return tool_result({"sessionId": session.id, "exitCode": exit_code})


def handle_process_list(_arguments):
    return tool_result({"sessions": PROCESS_MANAGER.list()})


def command_tool(program, arguments):
    return tool_result(run_program(
        program,
        arguments.get("args") or [],
        cwd=arguments.get("cwd"),
        timeout=arguments.get("timeout", 120),
        input_text=arguments.get("stdin"),
        env=arguments.get("env"),
        maximum=max(1024, min(int(arguments.get("maxOutputBytes", MAX_CAPTURE_BYTES)), 32 * 1024 * 1024)),
    ))


def handle_http_request(arguments):
    body = arguments.get("body")
    data = body.encode("utf-8") if isinstance(body, str) else None
    request = urllib.request.Request(
        arguments["url"],
        data=data,
        method=arguments.get("method", "GET").upper(),
        headers={str(key): str(value) for key, value in (arguments.get("headers") or {}).items()},
    )
    maximum = max(1, min(int(arguments.get("maxBytes", 2 * 1024 * 1024)), 32 * 1024 * 1024))
    timeout = max(1, min(int(arguments.get("timeout", 30)), 600))
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        response_body = response.read(maximum + 1)
        truncated = len(response_body) > maximum
        response_body = response_body[:maximum]
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset("utf-8")
        if content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
            rendered_body = response_body.decode(charset, errors="replace")
            encoding = charset
        else:
            rendered_body = base64.b64encode(response_body).decode("ascii")
            encoding = "base64"
        result = {
            "url": response.geturl(),
            "status": response.status,
            "reason": response.reason,
            "headers": dict(response.headers.items()),
            "contentType": content_type,
            "encoding": encoding,
            "body": rendered_body,
            "truncated": truncated,
        }
    return tool_result(result)


def annotations(read_only=False, destructive=False, open_world=False):
    return {
        "readOnlyHint": read_only,
        "destructiveHint": destructive,
        "openWorldHint": open_world,
    }


def object_schema(properties=None, required=None):
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def string(description=None):
    value = {"type": "string"}
    if description:
        value["description"] = description
    return value


def tool(name, title, description, schema, handler, hints):
    return name, {
        "descriptor": {
            "name": name,
            "title": title,
            "description": description,
            "inputSchema": schema,
            "annotations": hints,
        },
        "handler": handler,
    }


def _mcp_text(result):
    return "\n".join(
        item.get("text", "")
        for item in (result or {}).get("content", [])
        if item.get("type") == "text"
    )


def _snapshot_page_url(snapshot):
    match = re.search(r"- Page URL:\s*(https://chatgpt\.com/[^\s]*)", snapshot or "", re.IGNORECASE)
    return match.group(1) if match else None


def _snapshot_ref(snapshot, patterns):
    for pattern in patterns:
        match = re.search(pattern, snapshot or "", re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _chatgpt_snapshot(depth=14):
    snapshot = _mcp_text(CHATGPT_BROWSER_CLIENT.call("browser_snapshot", {"depth": depth}))
    if re.search(r"Just a moment\.\.\.|cf-chl|Cloudflare|HTTP status:\s*403", snapshot, re.IGNORECASE):
        raise RuntimeError(
            "The normal headed Chromium session is unexpectedly seeing a Cloudflare challenge. "
            "Do not use a challenge-bypass proxy; inspect the real browser session instead."
        )
    return snapshot


def _chatgpt_authentication_required(snapshot):
    return bool(
        re.search(r'- button "Log in" \[ref=', snapshot or "", re.IGNORECASE)
        or re.search(r"Get responses tailored to you", snapshot or "", re.IGNORECASE)
    )


def _chatgpt_composer_ref(snapshot):
    return _snapshot_ref(snapshot, [
        r'textbox "Chat with ChatGPT"[^\n]*\[ref=([^\]]+)\]',
        r'textbox "Message ChatGPT"[^\n]*\[ref=([^\]]+)\]',
        r'textbox "Ask anything"[^\n]*\[ref=([^\]]+)\]',
        r'textbox "Message"[^\n]*\[ref=([^\]]+)\]',
        r'textbox [^\n]*\[ref=([^\]]+)\]',
    ])


def handle_chatgpt_browser_status(_arguments):
    try:
        snapshot = _chatgpt_snapshot(depth=8)
        url = _snapshot_page_url(snapshot)
        authenticated = not _chatgpt_authentication_required(snapshot)
        return tool_result({
            "reachable": True,
            "authenticated": authenticated,
            "url": url,
            "mode": "headed-cdp",
            "cdpEndpoint": "http://127.0.0.1:9222",
        })
    except Exception as error:
        return tool_result({
            "reachable": False,
            "authenticated": False,
            "url": None,
            "mode": "headed-cdp",
            "error": str(error),
        }, message=str(error), is_error=True)


def handle_chatgpt_start_chat(arguments):
    message = str(arguments.get("message") or "").strip()
    if not message:
        return tool_result({"created": False}, message="message is required.", is_error=True)
    if len(message) > 50000:
        return tool_result({"created": False}, message="message must be 50,000 characters or fewer.", is_error=True)

    project = str(arguments.get("project") or "").strip()
    project_url = str(arguments.get("projectUrl") or "").strip()
    submit = arguments.get("submit", True) is not False

    if project_url:
        try:
            parsed = urllib.parse.urlparse(project_url)
        except Exception:
            parsed = None
        if not parsed or parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
            return tool_result({"created": False}, message="projectUrl must be an https://chatgpt.com URL.", is_error=True)

    try:
        initial_url = project_url or "https://chatgpt.com/"
        CHATGPT_BROWSER_CLIENT.call("browser_tabs", {"action": "new", "url": initial_url})
        CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1.5})
        snapshot = _chatgpt_snapshot()

        if _chatgpt_authentication_required(snapshot):
            return tool_result({
                "created": False,
                "submitted": False,
                "authenticationRequired": True,
                "project": project or None,
                "projectUrl": project_url or None,
                "chatUrl": None,
                "browserUrl": _snapshot_page_url(snapshot),
                "mode": "headed-cdp",
            }, message=(
                "The headed Chromium bridge reached ChatGPT without a Cloudflare challenge, but this dedicated "
                "browser profile is not signed in. Complete the one-time ChatGPT sign-in in the Codex Replacer "
                "desktop Chromium window, then retry chatgpt_start_chat."
            ), is_error=True)

        if project and not project_url:
            escaped = re.escape(project)
            project_ref = _snapshot_ref(snapshot, [
                rf'(?:link|button) "{escaped}" \[ref=([^\]]+)\]',
                rf'(?:link|button) "[^"]*{escaped}[^"]*" \[ref=([^\]]+)\]',
            ])
            if not project_ref:
                found = _mcp_text(CHATGPT_BROWSER_CLIENT.call("browser_find", {"text": project}))
                project_ref = _snapshot_ref(found, [
                    rf'(?:link|button) "[^"]*{escaped}[^"]*" \[ref=([^\]]+)\]',
                    r'\[ref=([^\]]+)\]',
                ])
            if not project_ref:
                raise RuntimeError(f'Could not find ChatGPT Project "{project}" in the authenticated sidebar.')
            CHATGPT_BROWSER_CLIENT.call("browser_click", {
                "target": project_ref,
                "element": f'ChatGPT Project {project}',
            })
            CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1.5})
            snapshot = _chatgpt_snapshot()

        if project or project_url:
            composer_ref = _chatgpt_composer_ref(snapshot)
            if not composer_ref:
                new_chat_ref = _snapshot_ref(snapshot, [
                    r'button "New chat" \[ref=([^\]]+)\]',
                    r'link "New chat" \[ref=([^\]]+)\]',
                    r'button "Chat" \[ref=([^\]]+)\]',
                    r'link "Chat" \[ref=([^\]]+)\]',
                    r'button "Start (?:a )?new chat[^"]*" \[ref=([^\]]+)\]',
                ])
                if not new_chat_ref:
                    raise RuntimeError("Could not find a new-chat control inside the requested ChatGPT Project.")
                CHATGPT_BROWSER_CLIENT.call("browser_click", {
                    "target": new_chat_ref,
                    "element": "new chat control in requested ChatGPT Project",
                })
                CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1.5})
                snapshot = _chatgpt_snapshot()

        composer_ref = _chatgpt_composer_ref(snapshot)
        if not composer_ref:
            raise RuntimeError("Could not locate the ChatGPT message composer in the headed browser.")

        CHATGPT_BROWSER_CLIENT.call("browser_fill_form", {
            "fields": [{
                "name": "ChatGPT message",
                "type": "textbox",
                "target": composer_ref,
                "element": "ChatGPT message composer",
                "value": message,
            }],
        })

        if submit:
            CHATGPT_BROWSER_CLIENT.call("browser_press_key", {"key": "Enter"})
            snapshot = None
            for _ in range(5):
                CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1})
                snapshot = _chatgpt_snapshot(depth=10)
                chat_url = _snapshot_page_url(snapshot)
                if chat_url and "/c/" in chat_url:
                    break
        else:
            snapshot = _chatgpt_snapshot(depth=10)

        chat_url = _snapshot_page_url(snapshot)
        created = bool(submit and chat_url and "/c/" in chat_url)
        data = {
            "created": created,
            "submitted": submit,
            "authenticationRequired": False,
            "project": project or None,
            "projectUrl": project_url or None,
            "chatUrl": chat_url if created else None,
            "browserUrl": chat_url,
            "mode": "headed-cdp",
        }
        if submit and not created:
            return tool_result(
                data,
                message="The handoff message was submitted, but a saved ChatGPT conversation URL could not be confirmed.",
                is_error=True,
            )
        if created:
            return tool_result(data, message=f"Created ChatGPT chat: {chat_url}")
        return tool_result(data, message="The handoff message is filled into the headed ChatGPT composer for review.")
    except Exception as error:
        return tool_result({
            "created": False,
            "submitted": False,
            "authenticationRequired": False,
            "project": project or None,
            "projectUrl": project_url or None,
            "chatUrl": None,
            "mode": "headed-cdp",
            "error": str(error),
        }, message=str(error), is_error=True)


def handle_prepare_chat_handoff(arguments):
    objective = str(arguments.get("objective") or "").strip()
    current_state = str(arguments.get("currentState") or "").strip()
    if not objective:
        return tool_result({"prepared": False}, message="objective is required.", is_error=True)
    if not current_state:
        return tool_result({"prepared": False}, message="currentState is required.", is_error=True)

    def clean_list(name):
        value = arguments.get(name) or []
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    sections = [
        ("Objective", [objective]),
        ("Current state", [current_state]),
        ("Completed", clean_list("completed")),
        ("Pending", clean_list("pending")),
        ("Important context", clean_list("importantContext")),
        ("Exact references", clean_list("exactReferences")),
        ("Blockers / unresolved", clean_list("blockers")),
        ("Constraints / non-negotiables", clean_list("constraints")),
        ("Next actions", clean_list("nextActions")),
    ]

    lines = ["NEW CHAT HANDOFF", ""]
    for title, items in sections:
        if not items:
            continue
        lines.append(f"## {title}")
        if title in {"Objective", "Current state"}:
            lines.extend(items)
        else:
            lines.extend(f"- {item}" for item in items)
        lines.append("")
    lines.append("Continue from this state without redoing completed work. Verify live state before any irreversible action.")
    handoff = "\n".join(lines).strip()
    return tool_result({"prepared": True, "handoff": handoff}, message=handoff)


CONVERSATION_CONTINUITY_INSTRUCTIONS = (
    "Conversation continuity guard: proactively protect long-running work from ChatGPT conversation limits. "
    "There is no exact remaining-context counter exposed to MCP, so use strong warning signs instead: a very long multi-turn thread, "
    "large/repeated tool outputs, a context compaction or prior-conversation summary, difficulty retaining early decisions, or any platform warning about conversation length. "
    "Before context exhaustion, use prepare_chat_handoff and present its complete handoff to the user. The handoff must preserve the objective, exact current state, completed work, pending work, blockers, constraints, "
    "and concrete references such as repositories, branches, PRs, run IDs, paths, URLs, commands, services, and test results. Do not wait until the platform refuses another message. "
    "If the user explicitly asks to create the next chat and chatgpt_start_chat is available, seed that new chat with the generated handoff; otherwise output the handoff for the user to use."
)


COMMON_COMMAND_PROPERTIES = {
    "args": {"type": "array", "items": {"type": "string"}, "default": []},
    "cwd": string("Working directory. Relative paths resolve from /home/mark."),
    "timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 120},
    "stdin": string(),
    "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
    "maxOutputBytes": {"type": "integer", "minimum": 1024, "maximum": 33554432},
}


DIRECT_TOOLS = dict([
    tool("prepare_chat_handoff", "Prepare chat handoff", "Use this proactively when the current conversation is becoming long or context-risky. It formats a complete continuation summary for a new chat before context is exhausted.", object_schema({"objective": string(), "currentState": string(), "completed": {"type": "array", "items": {"type": "string"}}, "pending": {"type": "array", "items": {"type": "string"}}, "importantContext": {"type": "array", "items": {"type": "string"}}, "exactReferences": {"type": "array", "items": {"type": "string"}}, "blockers": {"type": "array", "items": {"type": "string"}}, "constraints": {"type": "array", "items": {"type": "string"}}, "nextActions": {"type": "array", "items": {"type": "string"}}}, ["objective", "currentState"]), handle_prepare_chat_handoff, annotations(True, False, False)),
    tool("chatgpt_browser_status", "Inspect ChatGPT browser", "Check whether the dedicated normal headed Chromium session for ChatGPT is reachable and authenticated. This does not expose general control of that browser.", object_schema(), handle_chatgpt_browser_status, annotations(True, False, True)),
    tool("chatgpt_start_chat", "Start ChatGPT chat", "Create a new ChatGPT conversation through the user's persistent headed Chromium session, optionally inside an existing ChatGPT Project, seed it with a message, submit it, and return the resulting conversation URL. Use this only when the user explicitly asks to start, hand off, or continue work in another ChatGPT chat.", object_schema({"message": string("First message to place in the new chat."), "project": string("Optional exact ChatGPT Project name."), "projectUrl": string("Optional exact https://chatgpt.com project URL; prefer when known."), "submit": {"type": "boolean", "default": True}}, ["message"]), handle_chatgpt_start_chat, annotations(False, False, True)),
    tool("system_info", "Inspect VM", "Use this when you need the dedicated Codex Replacer VM identity, Mark's guest execution context, or installed development tools.", object_schema(), handle_system_info, annotations(True, False, False)),
    tool("fs_stat", "Inspect path", "Use this when you need metadata, ownership, permissions, timestamps, link target, or an optional SHA-256 hash for any host path.", object_schema({"path": string(), "hash": {"type": "boolean", "default": False}}, ["path"]), handle_fs_stat, annotations(True, False, False)),
    tool("fs_list", "List files", "Use this when you need to list any host directory, optionally recursively.", object_schema({"path": string(), "recursive": {"type": "boolean", "default": False}, "maxDepth": {"type": "integer", "minimum": 0, "maximum": 100}, "maxEntries": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["path"]), handle_fs_list, annotations(True, False, False)),
    tool("fs_read", "Read file", "Use this when you need text or base64 bytes from any host file.", object_schema({"path": string(), "offset": {"type": "integer", "minimum": 0}, "length": {"type": "integer", "minimum": 1, "maximum": 16777216}, "encoding": {"type": "string", "enum": ["utf8", "base64"], "default": "utf8"}}, ["path"]), handle_fs_read, annotations(True, False, False)),
    tool("fs_search", "Search files", "Use this when you need fast regular-expression or literal text search across any host files using ripgrep.", object_schema({"pattern": string(), "paths": {"type": "array", "items": {"type": "string"}}, "globs": {"type": "array", "items": {"type": "string"}}, "fixedStrings": {"type": "boolean"}, "ignoreCase": {"type": "boolean"}, "hidden": {"type": "boolean"}, "maxResults": {"type": "integer", "minimum": 1, "maximum": 5000}}, ["pattern"]), handle_fs_search, annotations(True, False, False)),
    tool("fs_write", "Write file", "Use this when you need to create, overwrite, or append text or base64 bytes to any host file Mark can access.", object_schema({"path": string(), "content": string(), "encoding": {"type": "string", "enum": ["utf8", "base64"], "default": "utf8"}, "append": {"type": "boolean", "default": False}, "createParents": {"type": "boolean", "default": True}}, ["path", "content"]), handle_fs_write, annotations(False, True, False)),
    tool("fs_replace", "Replace file text", "Use this when you need an exact, reviewable text replacement in any host file.", object_schema({"path": string(), "oldText": string(), "newText": string(), "count": {"type": "integer"}, "expectedOccurrences": {"type": "integer", "minimum": 0}, "encoding": string()}, ["path", "oldText", "newText"]), handle_fs_replace, annotations(False, True, False)),
    tool("fs_patch", "Apply patch", "Use this when you need to check or apply a standard unified git patch in any directory.", object_schema({"cwd": string(), "patch": string(), "checkOnly": {"type": "boolean", "default": False}, "timeout": {"type": "integer", "minimum": 1, "maximum": 86400}}, ["cwd", "patch"]), handle_fs_patch, annotations(False, True, False)),
    tool("fs_mkdir", "Create directory", "Use this when you need to create any host directory Mark can access.", object_schema({"path": string(), "existOk": {"type": "boolean", "default": True}}, ["path"]), handle_fs_mkdir, annotations(False, False, False)),
    tool("fs_copy", "Copy path", "Use this when you need to copy a file or directory anywhere Mark can access.", object_schema({"source": string(), "destination": string(), "overwrite": {"type": "boolean", "default": False}}, ["source", "destination"]), handle_fs_copy, annotations(False, True, False)),
    tool("fs_move", "Move path", "Use this when you need to move or rename a file or directory anywhere Mark can access.", object_schema({"source": string(), "destination": string()}, ["source", "destination"]), handle_fs_move, annotations(False, True, False)),
    tool("fs_delete", "Delete path", "Use this when you need to permanently delete a file or directory Mark can access.", object_schema({"path": string(), "recursive": {"type": "boolean", "default": False}}, ["path"]), handle_fs_delete, annotations(False, True, False)),
    tool("image_view", "View image", "Use this when you need to visually inspect an image file from any host path.", object_schema({"path": string(), "maxBytes": {"type": "integer", "minimum": 1, "maximum": 52428800}}, ["path"]), handle_image_view, annotations(True, False, False)),
    tool("host_exec", "Execute VM command", "Use this for unrestricted shell access inside the dedicated Codex Replacer VM. Runs as Mark by default; set asRoot=true for passwordless root execution when privileged filesystem, networking, package, service, mount, device, firewall, or system operations are needed.", object_schema({"command": string(), "cwd": string(), "timeout": {"type": "integer", "minimum": 1, "maximum": 86400}, "stdin": string(), "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}, "asRoot": {"type": "boolean", "default": False}, "maxOutputBytes": {"type": "integer", "minimum": 1024, "maximum": 33554432}}, ["command"]), handle_host_exec, annotations(False, True, True)),
    tool("process_start", "Start host process", "Use this when you need to start a long-running or interactive command as Mark and continue it across later tool calls.", object_schema({"command": string(), "cwd": string(), "interactive": {"type": "boolean", "default": False}, "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}}, ["command"]), handle_process_start, annotations(False, True, True)),
    tool("process_poll", "Read process output", "Use this when you need new output or completion state from a previously started process.", object_schema({"sessionId": string(), "afterSequence": {"type": "integer", "minimum": 0, "default": 0}}, ["sessionId"]), handle_process_poll, annotations(True, False, False)),
    tool("process_write", "Write process input", "Use this when you need to send text or terminal input to a running process.", object_schema({"sessionId": string(), "data": string()}, ["sessionId", "data"]), handle_process_write, annotations(False, True, True)),
    tool("process_stop", "Stop process", "Use this when you need to terminate a process started by Codex Replacer.", object_schema({"sessionId": string(), "force": {"type": "boolean", "default": False}}, ["sessionId"]), handle_process_stop, annotations(False, True, False)),
    tool("process_list", "List processes", "Use this when you need to inspect all commands started by Codex Replacer.", object_schema(), handle_process_list, annotations(True, False, False)),
    tool("git", "Run git", "Use this when you need unrestricted git operations as Mark in any repository.", object_schema(COMMON_COMMAND_PROPERTIES), lambda arguments: command_tool("git", arguments), annotations(False, True, True)),
    tool("github", "Run GitHub CLI", "Use this when you need unrestricted GitHub operations as Mark through the authenticated gh CLI.", object_schema(COMMON_COMMAND_PROPERTIES), lambda arguments: command_tool("gh", arguments), annotations(False, True, True)),
    tool("docker", "Run Docker", "Use this when you need unrestricted Docker or Docker Compose operations inside the dedicated Codex Replacer VM.", object_schema(COMMON_COMMAND_PROPERTIES), lambda arguments: command_tool("docker", arguments), annotations(False, True, True)),
    tool("http_request", "Make HTTP request", "Use this when you need an HTTP or HTTPS request to any internet or private-network endpoint reachable from the dedicated Codex Replacer VM.", object_schema({"url": string(), "method": {"type": "string", "default": "GET"}, "headers": {"type": "object", "additionalProperties": {"type": "string"}}, "body": string(), "timeout": {"type": "integer", "minimum": 1, "maximum": 600}, "maxBytes": {"type": "integer", "minimum": 1, "maximum": 33554432}}, ["url"]), handle_http_request, annotations(True, False, True)),
])


def send_message(message):
    sys.stdout.write(json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle_request(message):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": CONVERSATION_CONTINUITY_INSTRUCTIONS,
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    if method == "tools/list":
        tools = [entry["descriptor"] for entry in DIRECT_TOOLS.values()]
        try:
            tools.extend(
                item for item in BROWSER_CLIENT.list_tools()
                if item.get("name") not in DIRECT_TOOLS
            )
        except Exception as error:
            sys.stderr.write(f"Visual browser tools are temporarily unavailable: {error}\n")
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if name in DIRECT_TOOLS:
                result = DIRECT_TOOLS[name]["handler"](arguments)
            else:
                browser_names = {item["name"] for item in BROWSER_CLIENT.list_tools()}
                if name not in browser_names:
                    raise KeyError(f"Unknown tool: {name}")
                result = BROWSER_CLIENT.call(name, arguments)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as error:
            sys.stderr.write(f"Tool {name} failed: {type(error).__name__}: {error}\n")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": tool_result(
                    {"tool": name, "errorType": type(error).__name__, "error": str(error)},
                    message=f"{type(error).__name__}: {error}",
                    is_error=True,
                ),
            }
    if request_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def shutdown(_signal_number=None, _frame=None):
    PROCESS_MANAGER.stop_all()
    BROWSER_CLIENT.close()
    CHATGPT_BROWSER_CLIENT.close()
    raise SystemExit(0)


def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle_request(message)
            if response is not None:
                send_message(response)
        except Exception as error:
            send_message({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": f"Internal error: {error}"},
            })
    shutdown()


if __name__ == "__main__":
    main()
