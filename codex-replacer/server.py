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
from concurrent.futures import ThreadPoolExecutor
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

import lab_manager
import vault_manager
import vm_lab_manager
import vm_job_scheduler


SERVER_NAME = "codex-replacer"
SERVER_VERSION = "2.5.0"
DEFAULT_DIRECTORY = "/home/mark"
MAX_CAPTURE_BYTES = 1 * 1024 * 1024
HOST_EXEC_FOREGROUND_SECONDS = max(1, min(int(os.environ.get("CODEX_REPLACER_HOST_EXEC_FOREGROUND_SECONDS", "20")), 90))
HOST_EXEC_WAIT_PROMOTION_SECONDS = max(1, min(int(os.environ.get("CODEX_REPLACER_HOST_EXEC_WAIT_PROMOTION_SECONDS", "3")), 30))
HOST_EXEC_NICE = max(0, min(int(os.environ.get("CODEX_REPLACER_HOST_EXEC_NICE", "5")), 19))
BROKER_ONLY = os.environ.get("CODEX_REPLACER_BROKER_ONLY", "0").strip().lower() in {"1", "true", "yes", "on"}


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
        preview = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(preview) > 2048:
            preview = preview[:2048] + "... [full result in structuredContent]"
        payload["content"] = [{"type": "text", "text": preview}]
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
    def __init__(self, command, cwd, env, interactive, as_root=False, timeout_seconds=None, initial_stdin=None, close_stdin=False, nice=0):
        self.id = uuid.uuid4().hex
        self.command = command
        self.cwd = absolute_path(cwd)
        self.interactive = bool(interactive)
        self.as_root = bool(as_root)
        self.started_at = now_iso()
        self.lock = threading.Lock()
        self.events = deque(maxlen=4000)
        self.next_sequence = 1
        self.timed_out = False
        process_env = os.environ.copy()
        explicit_env = {str(key): str(value) for key, value in (env or {}).items()} if isinstance(env, dict) else {}
        process_env.update(explicit_env)
        launch = ["/bin/bash", "-lc", command]
        if self.interactive:
            launch = ["/usr/bin/script", "-qefc", command, "/dev/null"]
        if self.as_root:
            env_args = [f"{key}={value}" for key, value in process_env.items()]
            launch = ["sudo", "-n", "env", *env_args, *launch]
        if nice:
            launch = ["/usr/bin/nice", "-n", str(int(nice)), *launch]
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
        if initial_stdin is not None:
            try:
                self.process.stdin.write(str(initial_stdin).encode("utf-8"))
                self.process.stdin.flush()
            except BrokenPipeError:
                pass
        if close_stdin and self.process.stdin is not None and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except BrokenPipeError:
                pass
        if timeout_seconds is not None:
            threading.Thread(target=self._watchdog, args=(float(timeout_seconds),), daemon=True).start()

    def _watchdog(self, timeout_seconds):
        if timeout_seconds <= 0:
            return
        try:
            self.process.wait(timeout=timeout_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        self.timed_out = True
        try:
            self.stop()
        except Exception:
            pass

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
        exit_code = self.process.poll()
        # A child can exit a few milliseconds before the stdout/stderr reader
        # threads append their final pipe data. When reporting completion, wait
        # briefly for both readers so a caller never observes running=false while
        # missing the process's final output event.
        if exit_code is not None:
            self.stdout_thread.join(timeout=0.2)
            self.stderr_thread.join(timeout=0.2)
        with self.lock:
            events = [event for event in self.events if event["sequence"] > after_sequence]
            next_sequence = self.next_sequence - 1
        return {
            "sessionId": self.id,
            "command": self.command,
            "cwd": self.cwd,
            "interactive": self.interactive,
            "asRoot": self.as_root,
            "startedAt": self.started_at,
            "running": exit_code is None,
            "exitCode": exit_code,
            "timedOut": self.timed_out,
            "events": events,
            "nextSequence": next_sequence,
        }

    def wait(self, timeout):
        try:
            self.process.wait(timeout=max(0.01, float(timeout)))
        except subprocess.TimeoutExpired:
            return False
        self.stdout_thread.join(timeout=0.5)
        self.stderr_thread.join(timeout=0.5)
        return True

    def captured_output(self, maximum=MAX_CAPTURE_BYTES):
        with self.lock:
            events = list(self.events)
        stdout = "".join(event["text"] for event in events if event["stream"] == "stdout")
        stderr = "".join(event["text"] for event in events if event["stream"] == "stderr")
        stdout, stdout_truncated = clipped_text(stdout, maximum)
        stderr, stderr_truncated = clipped_text(stderr, maximum)
        return stdout, stderr, stdout_truncated or stderr_truncated

    def write(self, data):
        if self.process.poll() is not None:
            raise RuntimeError("The process is no longer running.")
        if self.process.stdin is None or self.process.stdin.closed:
            raise RuntimeError("The process stdin is closed.")
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

    def start(self, command, cwd=None, env=None, interactive=False, **session_options):
        session = ProcessSession(command, cwd, env, interactive, **session_options)
        with self.lock:
            self.sessions[session.id] = session
        return session.snapshot()

    def start_session(self, command, cwd=None, env=None, interactive=False, **session_options):
        session = ProcessSession(command, cwd, env, interactive, **session_options)
        with self.lock:
            self.sessions[session.id] = session
        return session

    def discard(self, session_id):
        with self.lock:
            self.sessions.pop(session_id, None)

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


class KvmWorkerBrowserClient:
    """Persistent browser MCP client attached to one visible KVM Chromium via SSH CDP tunnel."""

    def __init__(self, station):
        self.station = int(station)
        self.process = None
        self.tunnel_process = None
        self.local_port = None
        self.next_id = 1
        self.lock = threading.Lock()

    @property
    def worker_ip(self):
        return f"192.168.122.{229 + self.station}"

    @property
    def endpoint(self):
        if self.local_port is None:
            raise RuntimeError(f"KVM worker {self.station} CDP tunnel is not initialized.")
        return f"http://127.0.0.1:{self.local_port}"

    def _allocate_local_port(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
        finally:
            sock.close()

    def _stop_tunnel(self):
        if self.tunnel_process is not None and self.tunnel_process.poll() is None:
            self.tunnel_process.terminate()
            try:
                self.tunnel_process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.tunnel_process.kill()
        self.tunnel_process = None
        self.local_port = None

    def _start_tunnel(self):
        self._stop_tunnel()
        self.local_port = self._allocate_local_port()
        key = os.environ.get("CODEX_VM_LAB_GUEST_KEY", "/home/mark/.ssh/id_ed25519_codex_lab_vm")
        known_hosts = os.environ.get("CODEX_VM_LAB_KNOWN_HOSTS", "/home/mark/.ssh/codex_lab_known_hosts")
        self.tunnel_process = subprocess.Popen(
            [
                "ssh", "-N", "-i", key,
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", f"UserKnownHostsFile={known_hosts}",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:9222",
                f"mark@{self.worker_ip}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sys.stderr,
        )
        deadline = time.monotonic() + 5
        last_error = None
        while time.monotonic() < deadline:
            if self.tunnel_process.poll() is not None:
                raise RuntimeError(f"SSH CDP tunnel to KVM worker {self.station} exited early.")
            try:
                with urllib.request.urlopen(self.endpoint + "/json/version", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except Exception as error:
                last_error = error
                time.sleep(0.05)
        self._stop_tunnel()
        raise RuntimeError(f"KVM worker {self.station} Chromium CDP tunnel did not become ready: {last_error}")

    def _ensure_endpoint(self):
        if self.tunnel_process is None or self.tunnel_process.poll() is not None or self.local_port is None:
            self._start_tunnel()
            return
        try:
            with urllib.request.urlopen(self.endpoint + "/json/version", timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            self._start_tunnel()
            return
        raise RuntimeError(f"KVM worker {self.station} visible Chromium is not ready through its SSH tunnel.")

    def _start(self):
        self.close()
        self._start_tunnel()
        image = os.environ.get("CODEX_REPLACER_BROWSER_IMAGE", "markshaw-private-mcp_browser:latest")
        self.process = subprocess.Popen(
            [
                "docker", "run", "--rm", "-i", "--network", "host",
                "--entrypoint", "node", image,
                "/app/cli.js",
                "--cdp-endpoint", self.endpoint,
                "--caps", "vision",
                "--image-responses", "allow",
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
            "clientInfo": {"name": f"{SERVER_NAME}-kvm-browser-{self.station}", "version": SERVER_VERSION},
        })
        self._notify("notifications/initialized", {})

    def _ensure(self):
        if self.process is None or self.process.poll() is not None:
            self._start()
        else:
            self._ensure_endpoint()

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
                raise RuntimeError(f"KVM worker {self.station} browser MCP connection closed unexpectedly.")
            response = json.loads(line)
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise RuntimeError(response["error"].get("message", "KVM browser MCP error."))
            return response.get("result", {})

    def list_tools(self):
        with self.lock:
            for attempt in range(2):
                try:
                    self._ensure()
                    return self._request("tools/list", {}).get("tools", [])
                except Exception:
                    self.close()
                    if attempt:
                        raise

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
        self._stop_tunnel()


class KvmWorkerBrowserPool:
    def __init__(self, stations=6):
        self.clients = {station: KvmWorkerBrowserClient(station) for station in range(1, stations + 1)}
        self.descriptors = None
        self.lock = threading.Lock()

    def tool_descriptors(self):
        with self.lock:
            if self.descriptors is not None:
                return self.descriptors
            descriptors = []
            originals = None
            errors = []
            for station, client in self.clients.items():
                try:
                    originals = client.list_tools()
                    break
                except Exception as error:
                    errors.append(f"VM {station}: {error}")
            if originals is None:
                raise RuntimeError("Could not discover browser tools from any KVM worker: " + "; ".join(errors))
            for original in originals:
                name = original.get("name") or ""
                if not name.startswith("browser_"):
                    continue
                item = json.loads(json.dumps(original))
                item["name"] = "vm_" + name
                item["description"] = (
                    f"{original.get('description') or name} Operates the persistent visible Chromium on one of the six KVM workers; "
                    "set station=1..6. The same browser is viewable at browserN.home.markshaw.ca."
                )
                schema = item.setdefault("inputSchema", {"type": "object"})
                properties = schema.setdefault("properties", {})
                properties["station"] = {
                    "type": "integer", "minimum": 1, "maximum": 6,
                    "description": "KVM worker station whose visible browser should be controlled."
                }
                properties["leaseId"] = {
                    "type": "string",
                    "description": "Active exclusive vm_lab_acquire lease for this station, preventing cross-agent browser collisions."
                }
                required = list(schema.get("required") or [])
                for required_name in ("leaseId", "station"):
                    if required_name not in required:
                        required.insert(0, required_name)
                schema["required"] = required
                descriptors.append(item)
            self.descriptors = descriptors
            return descriptors

    def warm(self):
        def warm_one(item):
            station, client = item
            with client.lock:
                try:
                    client._ensure()
                    return {"station": station, "ready": True, "endpoint": client.endpoint}
                except Exception as error:
                    client.close()
                    return {"station": station, "ready": False, "error": str(error)}

        with ThreadPoolExecutor(max_workers=len(self.clients)) as pool:
            return list(pool.map(warm_one, self.clients.items()))

    def call(self, name, arguments):
        station = int(arguments.get("station", 0))
        if station not in self.clients:
            raise ValueError("station must be between 1 and 6")
        lease_id = str(arguments.get("leaseId") or "").strip()
        if not lease_id:
            raise ValueError("leaseId from vm_lab_acquire is required for KVM browser control")
        vm_lab_manager.validate_lease(lease_id, station)
        original = name[3:] if name.startswith("vm_") else name
        browser_arguments = dict(arguments)
        browser_arguments.pop("station", None)
        browser_arguments.pop("leaseId", None)
        return self.clients[station].call(original, browser_arguments)

    def close(self):
        for client in self.clients.values():
            client.close()


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
KVM_BROWSER_POOL = KvmWorkerBrowserPool(6)
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


def _wait_command_delay(command):
    # Agents often used `sleep 45; check ...` in host_exec, which needlessly held
    # one tunnel request open and could cross the control-plane response deadline.
    # If a meaningful wait appears near the start, promote the command immediately.
    prefix = command[:512]
    match = re.search(r"(?:^|[;\n])\s*sleep\s+([0-9]+(?:\.[0-9]+)?)\b", prefix)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def handle_host_exec(arguments):
    cwd = absolute_path(arguments.get("cwd"))
    command = arguments["command"]
    timeout = max(1, min(int(arguments.get("timeout", 120)), 86400))
    maximum = max(1024, min(int(arguments.get("maxOutputBytes", MAX_CAPTURE_BYTES)), 32 * 1024 * 1024))
    as_root = bool(arguments.get("asRoot", False))
    wait_delay = _wait_command_delay(command)

    session = PROCESS_MANAGER.start_session(
        command,
        cwd,
        arguments.get("env"),
        False,
        as_root=as_root,
        timeout_seconds=timeout,
        initial_stdin=arguments.get("stdin"),
        close_stdin=True,
        nice=HOST_EXEC_NICE,
    )

    foreground_seconds = min(timeout, HOST_EXEC_FOREGROUND_SECONDS)
    immediate_promotion = wait_delay >= HOST_EXEC_WAIT_PROMOTION_SECONDS
    completed = False if immediate_promotion else session.wait(foreground_seconds)
    # Preserve classic host_exec timeout semantics when the caller explicitly
    # asked for a timeout within the interactive budget. The watchdog and this
    # path can race at the boundary, so make the timeout result deterministic.
    if not immediate_promotion and not completed and timeout <= HOST_EXEC_FOREGROUND_SECONDS:
        session.timed_out = True
        try:
            session.stop()
        except Exception:
            pass
        session.wait(0.5)
        completed = True
    stdout, stderr, truncated = session.captured_output(maximum)

    if completed:
        snapshot = session.snapshot()
        PROCESS_MANAGER.discard(session.id)
        return tool_result({
            "command": command,
            "cwd": cwd,
            "asRoot": as_root,
            "exitCode": None if snapshot["timedOut"] else snapshot["exitCode"],
            "stdout": stdout,
            "stderr": stderr,
            "timedOut": snapshot["timedOut"],
            "truncated": truncated,
            "running": False,
        })

    reason = "leading_wait" if immediate_promotion else "foreground_budget_exceeded"
    return tool_result({
        "command": command,
        "cwd": cwd,
        "asRoot": as_root,
        "exitCode": None,
        "stdout": stdout,
        "stderr": stderr,
        "timedOut": False,
        "truncated": truncated,
        "running": True,
        "sessionId": session.id,
        "promotedToBackground": True,
        "promotionReason": reason,
        "foregroundBudgetSeconds": foreground_seconds,
        "requestedTimeoutSeconds": timeout,
        "nextAction": "Use process_poll with this sessionId. Do not rerun the command.",
    })


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


def handle_lab_list(arguments):
    return tool_result(lab_manager.list_stations(arguments.get("auditLines", 12)))


def handle_lab_acquire(arguments):
    return tool_result(lab_manager.acquire(
        arguments["owner"],
        arguments.get("project"),
        arguments.get("ttlMinutes", 180),
    ))


def handle_lab_release(arguments):
    return tool_result(lab_manager.release(
        lease_id=arguments.get("leaseId"),
        station=arguments.get("station"),
        reason=arguments.get("reason", "released"),
        recycle=arguments.get("recycle", True),
    ))


def handle_lab_exec(arguments):
    return tool_result(lab_manager.execute(
        arguments["command"],
        lease_id=arguments.get("leaseId"),
        station=arguments.get("station"),
        cwd=arguments.get("cwd", "/workspace"),
        timeout=arguments.get("timeout", 120),
        env=arguments.get("env"),
        as_root=arguments.get("asRoot", False),
        max_bytes=arguments.get("maxOutputBytes", 1024 * 1024),
    ))


def handle_lab_gc(_arguments):
    return tool_result(lab_manager.collect())


def handle_vm_lab_list(arguments):
    return tool_result(vm_lab_manager.list_stations(arguments.get("auditLines", 12)))


def handle_vm_lab_acquire(arguments):
    return tool_result(vm_lab_manager.acquire(
        arguments["owner"],
        arguments.get("project"),
        arguments.get("ttlMinutes", 180),
        arguments.get("chatLabel"),
        arguments.get("chatUrl"),
    ))


def handle_vm_lab_release(arguments):
    return tool_result(vm_lab_manager.release(
        lease_id=arguments.get("leaseId"),
        station=arguments.get("station"),
        reason=arguments.get("reason", "released"),
        recycle=arguments.get("recycle", False),
    ))


def handle_vm_lab_exec(arguments):
    return tool_result(vm_lab_manager.execute(
        arguments["command"],
        lease_id=arguments.get("leaseId"),
        station=arguments.get("station"),
        cwd=arguments.get("cwd", "/workspace"),
        timeout=arguments.get("timeout", 120),
        env=arguments.get("env"),
        as_root=arguments.get("asRoot", False),
        max_bytes=arguments.get("maxOutputBytes", 1024 * 1024),
    ))


def handle_vm_lab_gc(_arguments):
    return tool_result(vm_lab_manager.collect())


def handle_vm_job_submit(arguments):
    return tool_result(vm_job_scheduler.submit(arguments))


def handle_vm_job_submit_batch(arguments):
    return tool_result(vm_job_scheduler.submit_many(
        arguments["owner"],
        arguments["jobs"],
        arguments.get("project"),
        arguments.get("chatLabel"),
        arguments.get("chatUrl"),
    ))


def handle_vm_job_status(arguments):
    return tool_result(vm_job_scheduler.status(
        arguments["jobId"],
        arguments.get("maxOutputBytes", 65536),
    ))


def handle_vm_job_list(arguments):
    return tool_result(vm_job_scheduler.list_jobs(
        arguments.get("status"),
        arguments.get("limit", 50),
    ))


def handle_vm_job_cancel(arguments):
    return tool_result(vm_job_scheduler.cancel(arguments["jobId"]))


def handle_vm_worker_status(_arguments):
    return tool_result(vm_job_scheduler.workers())


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


def handle_dotmoose_vault_list(_arguments):
    items = vault_manager.VAULT.list_items()
    return tool_result({
        "project": vault_manager.VAULT.project,
        "organization": vault_manager.VAULT.organization,
        "count": len(items),
        "items": items,
    })


def handle_dotmoose_vault_get(arguments):
    secret = vault_manager.VAULT.get_item(arguments.get("item"), arguments.get("fields"))
    audit = {
        "event": "vault_secret_read",
        "project": vault_manager.VAULT.project,
        "itemHash": hashlib.sha256(secret["id"].encode()).hexdigest()[:16],
        "fields": sorted(secret["values"]),
        "at": now_iso(),
    }
    sys.stderr.write(json.dumps(audit, separators=(",", ":")) + "\n")
    return tool_result({
        "project": vault_manager.VAULT.project,
        "organization": vault_manager.VAULT.organization,
        "item": secret["name"],
        "values": secret["values"],
    })


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


def _chatgpt_select_app(snapshot, composer_ref, app):
    CHATGPT_BROWSER_CLIENT.call("browser_type", {
        "target": composer_ref,
        "element": "ChatGPT message composer",
        "text": f"@{app}",
        "slowly": True,
    })
    CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 0.75})
    picker = _chatgpt_snapshot(depth=16)
    escaped = re.escape(app)
    app_ref = _snapshot_ref(picker, [
        rf'(?:menuitem|option|button) "{escaped}"[^\n]*\[ref=([^\]]+)\]',
        rf'(?:menuitem|option|button) "[^"\n]*{escaped}[^"\n]*"[^\n]*\[ref=([^\]]+)\]',
    ])
    if not app_ref:
        raise RuntimeError(f'Could not select ChatGPT app "{app}" from the composer mention picker.')
    CHATGPT_BROWSER_CLIENT.call("browser_click", {
        "target": app_ref,
        "element": f'ChatGPT app {app}',
    })
    CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 0.5})
    selected = _chatgpt_snapshot(depth=16)
    selected_composer_ref = _chatgpt_composer_ref(selected)
    if not selected_composer_ref:
        raise RuntimeError("ChatGPT app was selected, but the message composer is no longer available.")
    return selected, selected_composer_ref


def _headed_browser_tabs():
    listing = _mcp_text(CHATGPT_BROWSER_CLIENT.call("browser_tabs", {"action": "list"}))
    tabs = []
    for line in listing.splitlines():
        match = re.match(r"-\s*(\d+):\s*(?:\(current\)\s*)?\[(.*?)\]\((https?://[^)]+)\)", line.strip())
        if match:
            tabs.append({"index": int(match.group(1)), "title": match.group(2), "url": match.group(3)})
    return tabs


def _select_chatgpt_tab(create=True):
    for tab in _headed_browser_tabs():
        try:
            parsed = urllib.parse.urlparse(tab["url"])
        except Exception:
            continue
        if parsed.hostname == "chatgpt.com":
            CHATGPT_BROWSER_CLIENT.call("browser_tabs", {"action": "select", "index": tab["index"]})
            CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 0.5})
            return _chatgpt_snapshot()
    if not create:
        return None
    CHATGPT_BROWSER_CLIENT.call("browser_tabs", {"action": "new", "url": "https://chatgpt.com/"})
    CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1.5})
    return _chatgpt_snapshot()


def _close_stale_google_auth_tabs():
    stale = []
    for tab in _headed_browser_tabs():
        try:
            host = urllib.parse.urlparse(tab["url"]).hostname
        except Exception:
            host = None
        if host == "accounts.google.com":
            stale.append(tab["index"])
    for index in sorted(stale, reverse=True):
        CHATGPT_BROWSER_CLIENT.call("browser_tabs", {"action": "close", "index": index})


def _auth_flow(snapshot):
    text = snapshot or ""
    if re.search(r"Get responses tailored to you|button \"Log in\"", text, re.IGNORECASE):
        return "chatgpt_login"
    if re.search(r"Use your passkey|passkey", text, re.IGNORECASE):
        return "passkey"
    if re.search(r"Check your phone|Google sent a notification", text, re.IGNORECASE):
        return "phone_prompt"
    if re.search(r"Google Authenticator", text, re.IGNORECASE):
        return "authenticator"
    if re.search(r"text message with a 6-digit verification code|2-Step Verification phone", text, re.IGNORECASE):
        return "sms"
    if re.search(r"backup code", text, re.IGNORECASE):
        return "backup_code"
    if re.search(r"Enter your password", text, re.IGNORECASE):
        return "password"
    if re.search(r"Sign in with Google|Email or phone", text, re.IGNORECASE):
        return "google_login"
    return "unknown"


def _click_snapshot_control(snapshot, patterns, element):
    ref = _snapshot_ref(snapshot, patterns)
    if not ref:
        return False
    CHATGPT_BROWSER_CLIENT.call("browser_click", {"target": ref, "element": element})
    CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1.0})
    return True


def _headed_snapshot_until(patterns, timeout=8.0, depth=14):
    deadline = time.time() + timeout
    latest = ""
    while time.time() < deadline:
        latest = _mcp_text(CHATGPT_BROWSER_CLIENT.call("browser_snapshot", {"depth": depth}))
        if any(re.search(pattern, latest, re.IGNORECASE) for pattern in patterns):
            return latest
        CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 0.5})
    return latest


def handle_chatgpt_auth_begin(arguments):
    method = str(arguments.get("method") or "passkey").strip().lower()
    if method not in {"passkey", "phone_prompt"}:
        return tool_result(
            {"started": False, "method": method},
            message="method must be passkey or phone_prompt. Passwords, OTPs, backup codes, and MFA secrets are intentionally not accepted by this tool.",
            is_error=True,
        )
    account_email = str(arguments.get("accountEmail") or "").strip()
    try:
        _close_stale_google_auth_tabs()
        snapshot = _select_chatgpt_tab(create=True)
        if not _chatgpt_authentication_required(snapshot):
            return tool_result({
                "started": False,
                "authenticated": True,
                "method": method,
                "actionRequired": None,
                "url": _snapshot_page_url(snapshot),
            }, message="The headed ChatGPT browser is already authenticated.")

        if not re.search(r"Log in or sign up", snapshot, re.IGNORECASE):
            _click_snapshot_control(snapshot, [
                r'button "Log in" \[ref=([^\]]+)\]',
            ], "ChatGPT Log in")
            snapshot = _chatgpt_snapshot()

        if re.search(r"Continue with Google", snapshot, re.IGNORECASE):
            _click_snapshot_control(snapshot, [
                r'(?:link|button) "Continue with Google" \[ref=([^\]]+)\]',
            ], "Continue with Google")
            snapshot = _headed_snapshot_until([
                r"Email or phone",
                r"Enter your password",
                r"Use your passkey",
                r"Check your phone",
            ], timeout=10.0)

        if account_email and re.search(r"Email or phone", snapshot, re.IGNORECASE):
            email_ref = _snapshot_ref(snapshot, [r'textbox "Email or phone"[^\n]*\[ref=([^\]]+)\]'])
            if email_ref:
                CHATGPT_BROWSER_CLIENT.call("browser_type", {
                    "target": email_ref,
                    "element": "Google account email",
                    "text": account_email,
                    "submit": True,
                })
                snapshot = _headed_snapshot_until([
                    r"Enter your password",
                    r"Use your passkey",
                    r"Do you have your phone",
                    r"Google Authenticator",
                ], timeout=10.0)

        if re.search(r"Enter your password", snapshot, re.IGNORECASE):
            _click_snapshot_control(snapshot, [
                r'(?:button|link) "Try another way" \[ref=([^\]]+)\]',
            ], "Try another way")
            snapshot = _headed_snapshot_until([
                r"Use your passkey",
                r"Do you have your phone",
                r"Google Authenticator",
                r"backup code",
            ], timeout=10.0)

        if (
            method == "phone_prompt"
            and re.search(r"Choose how you want to sign in", snapshot, re.IGNORECASE)
            and not re.search(r"Do you have your phone\?|Check your phone", snapshot, re.IGNORECASE)
        ):
            _click_snapshot_control(snapshot, [
                r'(?:button|link) "Try another way" \[ref=([^\]]+)\]',
            ], "Try another way for phone prompt")
            snapshot = _headed_snapshot_until([
                r"Do you have your phone",
                r"Check your phone",
                r"Google Authenticator",
                r"backup code",
            ], timeout=10.0)

        if method == "passkey" and re.search(r"Use your passkey", snapshot, re.IGNORECASE):
            _click_snapshot_control(snapshot, [
                r'(?:button|link) "Use your passkey" \[ref=([^\]]+)\]',
            ], "Use your passkey")
            snapshot = _headed_snapshot_until([
                r"passkey",
                r"QR",
                r"Check your phone",
                r"Choose where to save",
            ], timeout=6.0)
        elif method == "phone_prompt" and re.search(r"Do you have your phone\?|Check your phone", snapshot, re.IGNORECASE):
            yes_ref = _snapshot_ref(snapshot, [r'button "Yes" \[ref=([^\]]+)\]'])
            if yes_ref:
                CHATGPT_BROWSER_CLIENT.call("browser_click", {"target": yes_ref, "element": "Send Google phone prompt"})
                CHATGPT_BROWSER_CLIENT.call("browser_wait_for", {"time": 1.5})
                snapshot = _mcp_text(CHATGPT_BROWSER_CLIENT.call("browser_snapshot", {"depth": 14}))

        flow = _auth_flow(snapshot)
        action = {
            "passkey": "Approve the passkey request on a trusted device or scan the cross-device QR code if Google presents one.",
            "phone_prompt": "Approve the Google sign-in notification on a trusted phone.",
            "google_login": "Google account identification is still required.",
            "password": "Choose Try another way; this MCP never accepts Google passwords.",
            "authenticator": "Use a different approved sign-in method; this MCP never accepts authenticator codes.",
            "sms": "Use a different approved sign-in method; this MCP never accepts SMS verification codes.",
            "backup_code": "Use a different approved sign-in method; this MCP never accepts backup codes.",
            "unknown": "Complete the visible Google approval step in the headed browser.",
        }.get(flow, "Complete the visible Google approval step.")
        return tool_result({
            "started": True,
            "authenticated": False,
            "method": method,
            "flow": flow,
            "actionRequired": action,
            "persistentProfile": str(Path.home() / ".local/share/codex-replacer/chatgpt-browser"),
        }, message=f"Authentication flow started. Current step: {flow}. {action}")
    except Exception as error:
        return tool_result({
            "started": False,
            "authenticated": False,
            "method": method,
            "error": str(error),
        }, message=str(error), is_error=True)


def handle_chatgpt_browser_status(_arguments):
    try:
        snapshot = _select_chatgpt_tab(create=True)
        url = _snapshot_page_url(snapshot)
        authenticated = not _chatgpt_authentication_required(snapshot)
        profile = Path.home() / ".local/share/codex-replacer/chatgpt-browser"
        return tool_result({
            "reachable": True,
            "authenticated": authenticated,
            "authFlow": None if authenticated else _auth_flow(snapshot),
            "url": url,
            "mode": "headed-cdp",
            "cdpEndpoint": "http://127.0.0.1:9222",
            "persistentProfile": str(profile),
            "profileExists": profile.is_dir(),
        })
    except Exception as error:
        return tool_result({
            "reachable": False,
            "authenticated": False,
            "authFlow": None,
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
    app = str(arguments.get("app") or "").strip()
    if len(app) > 120 or any(character in app for character in "\r\n\t"):
        return tool_result({"created": False}, message="app must be a single-line name of 120 characters or fewer.", is_error=True)
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

        if app:
            snapshot, composer_ref = _chatgpt_select_app(snapshot, composer_ref, app)
            CHATGPT_BROWSER_CLIENT.call("browser_type", {
                "target": composer_ref,
                "element": "ChatGPT message composer",
                "text": message,
            })
        else:
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
            "app": app or None,
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
    "If the user explicitly asks to create the next chat and chatgpt_start_chat is available, seed that new chat with the generated handoff; otherwise output the handoff for the user to use. "
    "Execution boundary: the user console/control VM is reserved exclusively for Mark and is never a bot work machine. This MCP endpoint is a broker. For interactive repository, shell, or browser work first use vm_lab_acquire with your stable bot name, then vm_lab_exec or vm_browser_* with that lease and always vm_lab_release when finished. For transferable CPU-heavy or long-running builds, test suites, compilers, recursive validators/scans, Docker builds, package builds, PDF/link validation, and similar work, use vm_job_submit. When two or more independent heavy tasks are available, prefer vm_job_submit_batch so one MCP call can feed the elastic headless KVM pool immediately. The homeserver scheduler creates disposable workers aggressively until CPU, RAM, or I/O high-water guards engage, then queues the remainder; the six persistent desktop VMs are never the automatic heavy-job concurrency limit. Poll with vm_job_status and never resubmit a running job just to check it. Controller-local host/fs/git/docker/browser tools are intentionally unavailable to bots. "
    "The six KVM workers are persistent computers with visible Chromium desktops. Use vm_worker_status for worker/browser health and direct the human to https://browser.home.markshaw.ca for the six-screen dashboard when useful. For interactive browser work, first call vm_lab_acquire with your stable agent name, then use the returned station and leaseId with vm_browser_* tools; always release the lease when finished. This permits six independent visible browser agents at once without cross-agent clicking/type collisions. vm_lab_exec can use the same lease for shell work. The lightweight Docker lab is legacy/on-demand and should not be preferred over the KVM scheduler. "
    "For repository scheduler jobs, prefer repoUrl plus revision for a committed/pushed revision so the worker can prepare its own isolated checkout. Use the main github tool for GitHub API/PR operations if gh authentication is not present inside a worker. Do not run an expensive build on the main VM merely because host_exec is convenient. The exception is work that genuinely depends on unsynced main-VM state and cannot safely be transferred first. "
    "Parallelism rule: issue independent read-only checks concurrently or combine them into one short shell/API call when safe instead of paying serial tool round trips. Never poll by sleeping in a foreground tool call; continue other useful work and poll later. "
    "Browser hygiene rule: reuse the current relevant tab/profile instead of opening duplicate tabs, and close pages/tabs when their task is complete. Do not leave dozens of finished directory, form, search, or test pages open indefinitely because Chromium renderer accumulation consumes memory and process slots shared by other chats. "
    "Attribution rule: always use your stable bot/agent name as owner for vm_job_* and vm_lab_acquire so the dashboard can show who is using each VM. If the client or current task gives you a real ChatGPT conversation label or https://chatgpt.com conversation URL, pass it as chatLabel/chatUrl; never invent a chat ID or URL when one is not actually available. "
    "Use a stable agent name, never use a station leased by another agent, and always call the matching lab_release or vm_lab_release when finished. "
    "Transport resilience rule: never use foreground sleep commands to wait for a future check. Use process_start/process_poll, continue other useful work, and poll the session later. host_exec automatically promotes leading waits of a few seconds and commands that exceed its short foreground budget into persistent process sessions; when it returns running=true, use process_poll with the returned sessionId and never rerun that command. "
    "If any write or mutating tool call ends with an uncertain transport error, do not blindly retry it because it may already have executed; inspect the target state first, then retry only if still needed."
    " DotMoose credential rule: for work on the DotMoose project, use dotmoose_vault_list to discover available login names and dotmoose_vault_get to retrieve only the fields needed for the current operation. Never ask for, store, or echo the Vaultwarden master password or Bitwarden session key. Do not use the DotMoose vault for unrelated projects."
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
    tool("dotmoose_vault_list", "List DotMoose credentials", "List item names, item types, and available credential fields from the project-scoped DotMoose Vaultwarden organization. This never returns secret values. Use only for DotMoose project work.", object_schema(), handle_dotmoose_vault_list, annotations(True, False, False)),
    tool("dotmoose_vault_get", "Get DotMoose credential", "Retrieve selected fields from one exact item in the project-scoped DotMoose Vaultwarden organization. Request only fields required for the current DotMoose task and avoid repeating returned secrets in chat or logs.", object_schema({"item": string("Exact item name returned by dotmoose_vault_list."), "fields": {"type": "array", "minItems": 1, "uniqueItems": True, "items": {"type": "string", "enum": ["username", "password", "totp", "notes"]}, "default": ["username", "password"]}}, ["item"]), handle_dotmoose_vault_get, annotations(True, False, True)),
    tool("chatgpt_browser_status", "Inspect ChatGPT browser", "Check whether the dedicated normal headed Chromium session for ChatGPT is reachable and authenticated. This does not expose general control of that browser.", object_schema(), handle_chatgpt_browser_status, annotations(True, False, True)),
    tool("chatgpt_auth_begin", "Begin ChatGPT authentication", "Start or resume the normal Google authentication flow for the persistent headed ChatGPT browser using only user-approved passkey or phone-prompt methods. This tool intentionally cannot accept passwords, one-time codes, backup codes, passkey secrets, or MFA secrets.", object_schema({"method": {"type": "string", "enum": ["passkey", "phone_prompt"], "default": "passkey"}, "accountEmail": string("Optional Google account email used only to fill the account identifier field.")}), handle_chatgpt_auth_begin, annotations(False, False, True)),
    tool("chatgpt_start_chat", "Start ChatGPT chat", "Create a new ChatGPT conversation through the user's persistent headed Chromium session, optionally select an installed ChatGPT app, seed it with a message, submit it, and return the resulting conversation URL. Use this only when the user explicitly asks to start, hand off, or continue work in another ChatGPT chat.", object_schema({"message": string("First message to place in the new chat."), "project": string("Optional exact ChatGPT Project name."), "projectUrl": string("Optional exact https://chatgpt.com project URL; prefer when known."), "app": string("Optional exact installed ChatGPT app name to select through the composer mention picker before sending."), "submit": {"type": "boolean", "default": True}}, ["message"]), handle_chatgpt_start_chat, annotations(False, False, True)),
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
    tool("host_exec", "Execute VM command", "Use this for unrestricted shell access inside the dedicated Codex Replacer control VM. Runs as Mark by default; set asRoot=true for passwordless root execution when privileged filesystem, networking, package, service, mount, device, firewall, or system operations are needed. Keep this tool for short interactive/orchestration work; offload transferable CPU-heavy builds, broad test suites, compilers, recursive validators/scans, Docker builds, and similar work to vm_job_submit so the six-KVM scheduler can run them concurrently. Do not use sleep to wait in the foreground. Leading waits and commands that exceed the short foreground budget are automatically promoted to a persistent process session; if running=true is returned, continue with process_poll using sessionId and do not rerun the command.", object_schema({"command": string(), "cwd": string(), "timeout": {"type": "integer", "minimum": 1, "maximum": 86400}, "stdin": string(), "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}, "asRoot": {"type": "boolean", "default": False}, "maxOutputBytes": {"type": "integer", "minimum": 1024, "maximum": 33554432}}, ["command"]), handle_host_exec, annotations(False, True, True)),
    tool("process_start", "Start host process", "Use this when you need to start a long-running or interactive command as Mark and continue it across later tool calls.", object_schema({"command": string(), "cwd": string(), "interactive": {"type": "boolean", "default": False}, "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}}, ["command"]), handle_process_start, annotations(False, True, True)),
    tool("process_poll", "Read process output", "Use this when you need new output or completion state from a previously started process.", object_schema({"sessionId": string(), "afterSequence": {"type": "integer", "minimum": 0, "default": 0}}, ["sessionId"]), handle_process_poll, annotations(True, False, False)),
    tool("process_write", "Write process input", "Use this when you need to send text or terminal input to a running process.", object_schema({"sessionId": string(), "data": string()}, ["sessionId", "data"]), handle_process_write, annotations(False, True, True)),
    tool("process_stop", "Stop process", "Use this when you need to terminate a process started by Codex Replacer.", object_schema({"sessionId": string(), "force": {"type": "boolean", "default": False}}, ["sessionId"]), handle_process_stop, annotations(False, True, False)),
    tool("process_list", "List processes", "Use this when you need to inspect all commands started by Codex Replacer.", object_schema(), handle_process_list, annotations(True, False, False)),
    tool("lab_list", "List computer lab", "Show the Codex computer-lab stations, active leases, running state, and recent sign-in/sign-out activity.", object_schema({"auditLines": {"type": "integer", "minimum": 0, "maximum": 100, "default": 12}}), handle_lab_list, annotations(True, False, False)),
    tool("lab_acquire", "Sign into lab computer", "Lease an isolated Codex lab workstation for an agent/project. Use a short stable agent name so other agents can see who owns the station.", object_schema({"owner": string("Agent name signing out the workstation, for example BuildMoose-Sol."), "project": string("Optional project or repository being worked on."), "ttlMinutes": {"type": "integer", "minimum": 15, "maximum": 1440, "default": 180}}, ["owner"]), handle_lab_acquire, annotations(False, False, False)),
    tool("lab_release", "Sign out of lab computer", "Release a leased lab workstation and record the sign-out. By default the container is recycled and its prior workspace is archived rather than deleted.", object_schema({"leaseId": string(), "station": {"type": "integer", "minimum": 1, "maximum": 12}, "reason": string(), "recycle": {"type": "boolean", "default": True}}), handle_lab_release, annotations(False, True, False)),
    tool("lab_exec", "Run command in lab computer", "Run a shell command inside a currently leased isolated lab workstation. Identify it by leaseId or station.", object_schema({"command": string(), "leaseId": string(), "station": {"type": "integer", "minimum": 1, "maximum": 12}, "cwd": string("Directory inside the lab computer; defaults to /workspace."), "timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 120}, "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}, "asRoot": {"type": "boolean", "default": False}, "maxOutputBytes": {"type": "integer", "minimum": 1024, "maximum": 8388608, "default": 1048576}}, ["command"]), handle_lab_exec, annotations(False, True, True)),
    tool("lab_gc", "Maintain computer lab", "Release expired lightweight lab leases and ensure the configured prewarmed container workstation pool is ready.", object_schema(), handle_lab_gc, annotations(False, True, False)),
    tool("vm_lab_list", "List full-VM lab", "Show full KVM lab computers, active leases, VM state, and recent sign-in/sign-out activity.", object_schema({"auditLines": {"type": "integer", "minimum": 0, "maximum": 100, "default": 12}}), handle_vm_lab_list, annotations(True, False, False)),
    tool("vm_lab_acquire", "Sign into full VM", "Lease a clean full KVM workstation for an agent/project. Use this by default for CPU-heavy builds, broad test suites, compilers, recursive validators/scans, Docker-in-VM, risky dependency work, or other expensive work that can be run from a transferable repository revision. This keeps the main Codex VM responsive for all chats.", object_schema({"owner": string("Stable bot/agent name signing into the workstation."), "project": string("Optional project or repository being worked on."), "chatLabel": string("Optional real ChatGPT chat title/label when known; do not invent one."), "chatUrl": string("Optional real https://chatgpt.com conversation URL when known; do not invent one."), "ttlMinutes": {"type": "integer", "minimum": 15, "maximum": 1440, "default": 180}}, ["owner"]), handle_vm_lab_acquire, annotations(False, False, False)),
    tool("vm_lab_release", "Sign out of full VM", "Release an exclusive full KVM workstation lease. The computer stays running and preserves its browser/profile by default for speed; set recycle=true only when a full reimage is actually required.", object_schema({"leaseId": string(), "station": {"type": "integer", "minimum": 1, "maximum": 8}, "reason": string(), "recycle": {"type": "boolean", "default": False}}), handle_vm_lab_release, annotations(False, True, False)),
    tool("vm_lab_exec", "Run command in full VM", "Run a shell command inside a currently leased full KVM lab workstation. Identify it by leaseId or station.", object_schema({"command": string(), "leaseId": string(), "station": {"type": "integer", "minimum": 1, "maximum": 8}, "cwd": string("Directory inside the full VM; defaults to /workspace."), "timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 120}, "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}, "asRoot": {"type": "boolean", "default": False}, "maxOutputBytes": {"type": "integer", "minimum": 1024, "maximum": 8388608, "default": 1048576}}, ["command"]), handle_vm_lab_exec, annotations(False, True, True)),
    tool("vm_lab_gc", "Maintain full-VM lab", "Release expired full-VM leases and ensure the configured prewarmed KVM workstation pool is ready.", object_schema(), handle_vm_lab_gc, annotations(False, True, False)),
    tool("vm_job_submit", "Schedule KVM job", "Submit a long or CPU-heavy command to the central six-KVM scheduler. It immediately chooses a free persistent worker or queues the job, returns a jobId, and does not hold the MCP request open for the job duration. Prefer repoUrl+revision when the work can run from a committed revision.", object_schema({"owner": string("Stable bot/agent name."), "project": string(), "chatLabel": string("Optional real ChatGPT chat title/label when known."), "chatUrl": string("Optional real https://chatgpt.com conversation URL when known."), "command": string(), "cwd": string("Worker cwd; defaults to /workspace. With repoUrl this can be a path inside the cloned repository."), "timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600}, "jobClass": {"type": "string", "enum": ["cpu", "io", "browser", "test", "build"], "default": "cpu"}, "repoUrl": string(), "revision": string(), "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}}, ["owner", "command"]), handle_vm_job_submit, annotations(False, True, True)),
    tool("vm_job_submit_batch", "Schedule KVM job batch", "Submit 2-48 independent heavy jobs in one MCP call. The elastic scheduler starts as many disposable headless KVM workers as host CPU, RAM, and I/O pressure safely permit and queues the remainder. The six persistent desktop VMs are not an automatic-job limit and are not used for this work. Prefer this over serial vm_job_submit calls when work can be parallelized.", object_schema({"owner": string("Stable bot/agent name applied to every job."), "project": string("Default project for jobs that do not override it."), "chatLabel": string("Default real ChatGPT chat title/label when known."), "chatUrl": string("Default real https://chatgpt.com conversation URL when known."), "jobs": {"type": "array", "minItems": 1, "maxItems": 48, "items": {"type": "object", "properties": {"command": string(), "project": string(), "chatLabel": string(), "chatUrl": string(), "cwd": string(), "timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": 3600}, "jobClass": {"type": "string", "enum": ["cpu", "io", "browser", "test", "build"], "default": "cpu"}, "repoUrl": string(), "revision": string(), "env": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}}}, "required": ["command"], "additionalProperties": False}}}, ["owner", "jobs"]), handle_vm_job_submit_batch, annotations(False, True, True)),
    tool("vm_job_status", "Read KVM job", "Read scheduler state plus recent stdout/stderr for a previously submitted KVM job. Poll this instead of resubmitting the command.", object_schema({"jobId": string(), "maxOutputBytes": {"type": "integer", "minimum": 1024, "maximum": 1048576, "default": 65536}}, ["jobId"]), handle_vm_job_status, annotations(True, False, False)),
    tool("vm_job_list", "List KVM jobs", "List recent scheduled KVM jobs across all six workers.", object_schema({"status": {"type": "string", "enum": ["queued", "running", "succeeded", "failed", "timed_out", "canceled"]}, "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50}}), handle_vm_job_list, annotations(True, False, False)),
    tool("vm_job_cancel", "Cancel KVM job", "Cancel a queued or running KVM scheduler job by jobId.", object_schema({"jobId": string()}, ["jobId"]), handle_vm_job_cancel, annotations(False, True, False)),
    tool("vm_worker_status", "Inspect KVM workers", "Show both elastic headless heavy-job capacity and the six persistent interactive desktop/browser workers, including pressure/capacity state and dashboard information.", object_schema(), handle_vm_worker_status, annotations(True, False, False)),
    tool("git", "Run git", "Use this when you need unrestricted git operations as Mark in any repository.", object_schema(COMMON_COMMAND_PROPERTIES), lambda arguments: command_tool("git", arguments), annotations(False, True, True)),
    tool("github", "Run GitHub CLI", "Use this when you need unrestricted GitHub operations as Mark through the authenticated gh CLI.", object_schema(COMMON_COMMAND_PROPERTIES), lambda arguments: command_tool("gh", arguments), annotations(False, True, True)),
    tool("docker", "Run Docker", "Use this when you need unrestricted Docker or Docker Compose operations inside the dedicated Codex Replacer VM.", object_schema(COMMON_COMMAND_PROPERTIES), lambda arguments: command_tool("docker", arguments), annotations(False, True, True)),
    tool("http_request", "Make HTTP request", "Use this when you need an HTTP or HTTPS request to any internet or private-network endpoint reachable from the dedicated Codex Replacer VM.", object_schema({"url": string(), "method": {"type": "string", "default": "GET"}, "headers": {"type": "object", "additionalProperties": {"type": "string"}}, "body": string(), "timeout": {"type": "integer", "minimum": 1, "maximum": 600}, "maxBytes": {"type": "integer", "minimum": 1, "maximum": 33554432}}, ["url"]), handle_http_request, annotations(True, False, True)),
])


class ControllerReservedError(RuntimeError):
    pass


BROKER_DIRECT_TOOL_NAMES = {
    "prepare_chat_handoff",
    "dotmoose_vault_list",
    "dotmoose_vault_get",
    "chatgpt_browser_status",
    "chatgpt_auth_begin",
    "chatgpt_start_chat",
    "vm_lab_list",
    "vm_lab_acquire",
    "vm_lab_release",
    "vm_lab_exec",
    "vm_lab_gc",
    "vm_job_submit",
    "vm_job_submit_batch",
    "vm_job_status",
    "vm_job_list",
    "vm_job_cancel",
    "vm_worker_status",
}


def controller_reserved_message(name=None):
    prefix = f"Tool {name!r} is controller-local and was not executed. " if name else "Controller-local execution was not performed. "
    return (
        prefix
        + "The user console/control VM is reserved for Mark. Use vm_lab_acquire + vm_lab_exec for "
          "interactive repository/shell work, vm_job_submit or vm_job_submit_batch for transferable "
          "build/test work, and vm_browser_* with an active lease for visible browser work."
    )


def broker_direct_tools():
    return [
        entry["descriptor"]
        for name, entry in DIRECT_TOOLS.items()
        if name in BROKER_DIRECT_TOOL_NAMES
    ]


SEND_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
REQUEST_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(2, min(int(os.environ.get("CODEX_REPLACER_MAX_WORKERS", "20")), 20)),
    thread_name_prefix="mcp-request",
)

def send_message(message):
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
    with SEND_LOCK:
        sys.stdout.write(payload)
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
        tools = broker_direct_tools() if BROKER_ONLY else [entry["descriptor"] for entry in DIRECT_TOOLS.values()]
        try:
            if not BROKER_ONLY:
                tools.extend(
                    item for item in BROWSER_CLIENT.list_tools()
                    if item.get("name") not in DIRECT_TOOLS
                )
            tools.extend(KVM_BROWSER_POOL.tool_descriptors())
        except Exception as error:
            sys.stderr.write(f"Visual browser tools are temporarily unavailable: {error}\n")
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": tools}}
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            if BROKER_ONLY and name not in BROKER_DIRECT_TOOL_NAMES and not (name and name.startswith("vm_browser_")):
                raise ControllerReservedError(controller_reserved_message(name))
            if name in DIRECT_TOOLS:
                result = DIRECT_TOOLS[name]["handler"](arguments)
            elif name and name.startswith("vm_browser_"):
                vm_browser_names = {item["name"] for item in KVM_BROWSER_POOL.tool_descriptors()}
                if name not in vm_browser_names:
                    raise KeyError(f"Unknown KVM browser tool: {name}")
                result = KVM_BROWSER_POOL.call(name, arguments)
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


def process_request(message):
    started = time.monotonic()
    method = message.get("method")
    tool_name = (message.get("params") or {}).get("name") if method == "tools/call" else None
    status = "ok"
    try:
        return handle_request(message)
    except Exception as error:
        status = "error"
        return {
            "jsonrpc": "2.0",
            "id": message.get("id"),
            "error": {"code": -32603, "message": f"Internal error: {error}"},
        }
    finally:
        event = {
            "time": now_iso(),
            "event": "mcp_request_completed",
            "requestId": message.get("id"),
            "method": method,
            "tool": tool_name,
            "status": status,
            "elapsedMs": round((time.monotonic() - started) * 1000, 2),
        }
        with LOG_LOCK:
            sys.stderr.write(json.dumps(event, separators=(",", ":")) + "\n")
            sys.stderr.flush()


def _process_message(message):
    response = process_request(message)
    if response is not None:
        send_message(response)

def main():
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except Exception as error:
            send_message({
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {error}"},
            })
            continue
        REQUEST_EXECUTOR.submit(_process_message, message)
    REQUEST_EXECUTOR.shutdown(wait=True)
    shutdown()


if __name__ == "__main__":
    main()
