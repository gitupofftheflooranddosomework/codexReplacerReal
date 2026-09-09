#!/usr/bin/env python3

"""Loopback-only Streamable HTTP transport for Codex Replacer.

The tunnel client used to spawn server.py over stdio. A single expired MCP
connection could therefore close the shared stdin/stdout child and interrupt
unrelated chats. This process is deliberately independent of the tunnel client:
the tunnel may expire or reconnect individual HTTP requests without taking the
MCP server, its process sessions, or other concurrent requests down with it.
"""

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import server
import slackcontroller_bridge

slackcontroller_bridge.install(server)


HOST = os.environ.get("CODEX_REPLACER_HTTP_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_REPLACER_HTTP_PORT", "8791"))
PATH = os.environ.get("CODEX_REPLACER_HTTP_PATH", "/mcp")
MAX_WORKERS = max(2, min(int(os.environ.get("CODEX_REPLACER_MAX_WORKERS", "20")), 20))
REQUEST_SLOTS = threading.BoundedSemaphore(MAX_WORKERS)
MAX_BODY_BYTES = max(
    1024,
    min(
        int(os.environ.get("CODEX_REPLACER_HTTP_MAX_BODY_BYTES", str(8 * 1024 * 1024))),
        32 * 1024 * 1024,
    ),
)


def origin_is_loopback(value):
    if not value:
        return True
    try:
        parsed = urlsplit(value)
    except Exception:
        return False
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CodexReplacerHTTP/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write(
            json.dumps(
                {
                    "time": server.now_iso(),
                    "event": "mcp_http_access",
                    "client": self.client_address[0],
                    "message": fmt % args,
                },
                separators=(",", ":"),
            )
            + "\n"
        )
        sys.stderr.flush()

    def send_bytes(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        if body:
            self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # The upstream MCP connection may legitimately time out while a
                # long tool is still finishing. That client disconnect must not
                # affect this persistent server or any other request.
                pass

    def send_json(self, status, payload):
        self.send_bytes(
            status,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )

    def path_ok(self):
        return self.path.split("?", 1)[0] == PATH

    def reject_bad_origin(self):
        origin = self.headers.get("Origin")
        if origin_is_loopback(origin):
            return False
        self.send_json(403, {"error": "Codex Replacer MCP accepts only loopback origins."})
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_json(
                200,
                {
                    "ok": True,
                    "server": server.SERVER_NAME,
                    "version": server.SERVER_VERSION,
                    "transport": "streamable-http",
                },
            )
            return
        if path == PATH:
            # No unsolicited server notifications are emitted, so an SSE GET
            # stream is unnecessary. Streamable HTTP explicitly allows this.
            self.send_bytes(405)
            return
        self.send_bytes(404)

    def do_DELETE(self):
        if not self.path_ok():
            self.send_bytes(404)
            return
        if self.reject_bad_origin():
            return
        # Stateless service: there is no server-side MCP session to delete.
        self.send_bytes(204)

    def do_POST(self):
        if not self.path_ok():
            self.send_bytes(404)
            return
        if self.reject_bad_origin():
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json(400, {"error": "Invalid Content-Length."})
            return
        if length <= 0:
            self.send_json(400, {"error": "Request body is required."})
            return
        if length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "MCP request body is too large."})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except Exception as error:
            self.send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {error}"},
                },
            )
            return

        # Batching is accepted defensively even though current MCP revisions do
        # not require it. Each HTTP connection still stays isolated.
        if isinstance(payload, list):
            if not payload:
                self.send_json(
                    400,
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Invalid empty batch."},
                    },
                )
                return
            with REQUEST_SLOTS:
                responses = [
                    server.process_request(item)
                    for item in payload
                    if isinstance(item, dict)
                ]
            responses = [item for item in responses if item is not None]
            if not responses:
                self.send_bytes(202)
                return
            self.send_json(200, responses)
            return

        if not isinstance(payload, dict):
            self.send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "Invalid JSON-RPC request."},
                },
            )
            return

        with REQUEST_SLOTS:
            response = server.process_request(payload)
        if response is None:
            self.send_bytes(202)
            return
        self.send_json(200, response)


class HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


def cleanup():
    server.PROCESS_MANAGER.stop_all()
    server.BROWSER_CLIENT.close()
    server.KVM_BROWSER_POOL.close()
    server.CHATGPT_BROWSER_CLIENT.close()


def warm_dependencies():
    if not server.BROKER_ONLY:
        started = time.monotonic()
        try:
            tools = server.BROWSER_CLIENT.list_tools()
            event = {
                "time": server.now_iso(),
                "event": "mcp_dependency_warmed",
                "dependency": "visual_browser",
                "toolCount": len(tools),
                "elapsedMs": round((time.monotonic() - started) * 1000, 2),
            }
        except Exception as error:
            event = {
                "time": server.now_iso(),
                "event": "mcp_dependency_warm_failed",
                "dependency": "visual_browser",
                "error": str(error),
                "elapsedMs": round((time.monotonic() - started) * 1000, 2),
            }
        sys.stderr.write(json.dumps(event, separators=(",", ":")) + "\n")
        sys.stderr.flush()

    kvm_started = time.monotonic()
    try:
        workers = server.KVM_BROWSER_POOL.warm()
        ready = sum(1 for worker in workers if worker.get("ready"))
        kvm_event = {
            "time": server.now_iso(),
            "event": "mcp_dependency_warmed" if ready == len(workers) else "mcp_dependency_warm_partial",
            "dependency": "kvm_worker_browsers",
            "ready": ready,
            "workers": len(workers),
            "elapsedMs": round((time.monotonic() - kvm_started) * 1000, 2),
        }
        failures = [worker for worker in workers if not worker.get("ready")]
        if failures:
            kvm_event["failures"] = failures
    except Exception as error:
        kvm_event = {
            "time": server.now_iso(),
            "event": "mcp_dependency_warm_failed",
            "dependency": "kvm_worker_browsers",
            "error": str(error),
            "elapsedMs": round((time.monotonic() - kvm_started) * 1000, 2),
        }
    sys.stderr.write(json.dumps(kvm_event, separators=(",", ":")) + "\n")
    sys.stderr.flush()


def main():
    if HOST not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit(
            "Refusing to expose the privileged Codex Replacer MCP service off loopback."
        )

    httpd = HTTPServer((HOST, PORT), Handler)
    stopped = threading.Event()

    def stop(_signal_number, _frame):
        if stopped.is_set():
            return
        stopped.set()
        # BaseServer.shutdown() must be called from a different thread than
        # serve_forever().
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    sys.stderr.write(
        json.dumps(
            {
                "time": server.now_iso(),
                "event": "mcp_http_started",
                "url": f"http://{HOST}:{PORT}{PATH}",
                "workers": MAX_WORKERS,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stderr.flush()
    threading.Thread(target=warm_dependencies, daemon=True, name="dependency-warmup").start()
    try:
        httpd.serve_forever(poll_interval=0.2)
    finally:
        httpd.server_close()
        cleanup()


if __name__ == "__main__":
    main()
