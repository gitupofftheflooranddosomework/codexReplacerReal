#!/usr/bin/env python3

"""First-class SlackController tools for Codex Replacer chat workers.

Identity is never caller supplied. Each request is executed inside the caller's
active full-KVM lease so SlackController can authenticate the worker from the
TCP peer IP plus the homeserver lease state.
"""

import json
import os
import re

DEFAULT_BASE_URL = "http://10.0.0.181:8788"
MAX_RESPONSE_BYTES = 256 * 1024
MESSAGE_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.IGNORECASE)
TOOL_NAMES = {
    "slackcontroller_identity",
    "slackcontroller_inbox",
    "slackcontroller_reply",
    "slackcontroller_ack",
    "slackcontroller_heartbeat",
}

GUEST_REQUEST = r'''
import json
import os
import urllib.error
import urllib.request

base = os.environ["SC_BASE"].rstrip("/")
path = os.environ["SC_PATH"]
method = os.environ["SC_METHOD"]
body = os.environ.get("SC_BODY", "")
if not base.startswith("http://10.0.0.181:") and not base.startswith("https://10.0.0.181:"):
    raise SystemExit("invalid SlackController base URL")
if not path.startswith("/worker/v1/"):
    raise SystemExit("invalid SlackController worker path")
data = body.encode("utf-8") if body else None
headers = {"accept": "application/json"}
if data is not None:
    headers["content-type"] = "application/json"
request = urllib.request.Request(base + path, data=data, method=method, headers=headers)
try:
    response = urllib.request.urlopen(request, timeout=15)
except urllib.error.HTTPError as error:
    response = error
with response:
    raw = response.read(262145)
    if len(raw) > 262144:
        raise SystemExit("SlackController response too large")
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except Exception:
        parsed = {"error": "invalid_json_response"}
    print(json.dumps({"status": int(response.status), "body": parsed}, separators=(",", ":")))
'''

INSTRUCTION = (
    "SlackController chat coordination rule: SlackController is shared chat-worker infrastructure, not project application code. "
    "With your own active full-VM lease, use slackcontroller_identity, slackcontroller_inbox, slackcontroller_reply, "
    "slackcontroller_ack, and slackcontroller_heartbeat to communicate through SlackController. Never use another worker's "
    "lease, never supply or guess another logical handle, and do not modify a project repository or call that project's own "
    "Slack bots merely to use SlackController. Project/repository names are routing metadata only. "
)


def _base_url():
    return os.environ.get("SLACKCONTROLLER_WORKER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _message_id(value):
    message_id = str(value or "").strip()
    if not MESSAGE_ID.fullmatch(message_id):
        raise ValueError("messageId must be a SlackController UUID")
    return message_id


def _request(server, lease_id, method, path, payload=None):
    lease_id = str(lease_id or "").strip()
    if not lease_id:
        raise ValueError("leaseId from vm_lab_acquire is required")
    method = str(method).upper()
    if method not in {"GET", "POST"}:
        raise ValueError("unsupported SlackController worker method")
    if not path.startswith("/worker/v1/"):
        raise ValueError("unsupported SlackController worker path")
    body = "" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result = server.vm_lab_manager.execute(
        "python3 - <<'PY'\n" + GUEST_REQUEST + "\nPY",
        lease_id=lease_id,
        cwd="/workspace",
        timeout=20,
        env={
            "SC_BASE": _base_url(),
            "SC_METHOD": method,
            "SC_PATH": path,
            "SC_BODY": body,
        },
        max_bytes=MAX_RESPONSE_BYTES,
    )
    if result.get("exitCode") != 0:
        raise RuntimeError("SlackController request failed inside the leased worker")
    try:
        envelope = json.loads(result.get("stdout") or "")
    except Exception as error:
        raise RuntimeError("SlackController worker returned an invalid response") from error
    status = int(envelope.get("status", 0))
    response_body = envelope.get("body")
    if not isinstance(response_body, dict):
        raise RuntimeError("SlackController worker returned an invalid JSON body")
    if status < 200 or status >= 300:
        code = str(response_body.get("error") or "request_failed")[:120]
        raise RuntimeError(f"SlackController worker API error ({status}): {code}")
    return response_body


def install(server):
    if TOOL_NAMES.issubset(server.DIRECT_TOOLS):
        return

    def identity(arguments):
        return server.tool_result(_request(server, arguments.get("leaseId"), "GET", "/worker/v1/identity"))

    def inbox(arguments):
        limit = int(arguments.get("limit", 20))
        if limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        return server.tool_result(_request(server, arguments.get("leaseId"), "GET", f"/worker/v1/messages?limit={limit}"))

    def reply(arguments):
        message_id = _message_id(arguments.get("messageId"))
        text = str(arguments.get("text") or "")
        if not text.strip() or len(text) > 4000:
            raise ValueError("text must contain 1-4000 characters")
        return server.tool_result(_request(
            server,
            arguments.get("leaseId"),
            "POST",
            f"/worker/v1/messages/{message_id}/reply",
            {"text": text.strip()},
        ))

    def acknowledge(arguments):
        ids = arguments.get("messageIds")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 50:
            raise ValueError("messageIds must contain between 1 and 50 IDs")
        normalized = []
        for value in ids:
            message_id = _message_id(value)
            if message_id not in normalized:
                normalized.append(message_id)
        return server.tool_result(_request(
            server,
            arguments.get("leaseId"),
            "POST",
            "/worker/v1/messages/ack",
            {"ids": normalized},
        ))

    def heartbeat(arguments):
        status = str(arguments.get("status") or "").strip().lower()
        if status not in {"active", "idle", "offline"}:
            raise ValueError("status must be active, idle, or offline")
        return server.tool_result(_request(
            server,
            arguments.get("leaseId"),
            "POST",
            "/worker/v1/heartbeat",
            {"status": status},
        ))

    lease = server.string("Active full-VM lease ID returned by vm_lab_acquire for this same chat worker.")
    tools = [
        server.tool(
            "slackcontroller_identity",
            "Identify SlackController worker",
            "Resolve this chat worker's SlackController identity from its own active KVM lease. This never accepts a bot handle and does not use any project-specific Slack bot.",
            server.object_schema({"leaseId": lease}, ["leaseId"]),
            identity,
            server.annotations(True, False, True),
        ),
        server.tool(
            "slackcontroller_inbox",
            "Read SlackController inbox",
            "Read this leased chat worker's own SlackController inbox non-destructively. Use the lease belonging to this chat; no logical handle can be supplied.",
            server.object_schema({
                "leaseId": lease,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            }, ["leaseId"]),
            inbox,
            server.annotations(True, False, True),
        ),
        server.tool(
            "slackcontroller_reply",
            "Reply through SlackController",
            "Reply to one message already routed to this leased chat worker. Slack channel/thread routing is bound by SlackController and cannot be supplied by the caller.",
            server.object_schema({
                "leaseId": lease,
                "messageId": {"type": "string", "format": "uuid"},
                "text": {"type": "string", "minLength": 1, "maxLength": 4000},
            }, ["leaseId", "messageId", "text"]),
            reply,
            server.annotations(False, False, True),
        ),
        server.tool(
            "slackcontroller_ack",
            "Acknowledge SlackController messages",
            "Acknowledge one or more messages from this leased chat worker's own SlackController inbox. Other workers' routed copies are preserved.",
            server.object_schema({
                "leaseId": lease,
                "messageIds": {
                    "type": "array", "minItems": 1, "maxItems": 50, "uniqueItems": True,
                    "items": {"type": "string", "format": "uuid"},
                },
            }, ["leaseId", "messageIds"]),
            acknowledge,
            server.annotations(False, True, True),
        ),
        server.tool(
            "slackcontroller_heartbeat",
            "Update SlackController heartbeat",
            "Update only this leased chat worker's SlackController liveness. Use active only while actually present/processing, idle while connected but not processing, and offline when deliberately unavailable.",
            server.object_schema({
                "leaseId": lease,
                "status": {"type": "string", "enum": ["active", "idle", "offline"]},
            }, ["leaseId", "status"]),
            heartbeat,
            server.annotations(False, False, True),
        ),
    ]
    server.DIRECT_TOOLS.update(dict(tools))
    server.BROKER_DIRECT_TOOL_NAMES.update(TOOL_NAMES)
    if INSTRUCTION not in server.CONVERSATION_CONTINUITY_INSTRUCTIONS:
        server.CONVERSATION_CONTINUITY_INSTRUCTIONS += INSTRUCTION
