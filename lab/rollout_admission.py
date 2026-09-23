"""Fail-closed client for the local ServerWorkerFabric rollout boundary."""
from __future__ import annotations

import json
import os
import pathlib
import urllib.error
import urllib.parse
import urllib.request


class AdmissionError(RuntimeError):
    pass


URL = os.environ.get("CODEX_VM_JOB_ADMISSION_URL", "").strip().rstrip("/")
TOKEN_FILE = os.environ.get("CODEX_VM_JOB_ADMISSION_TOKEN_FILE", "").strip()


def enabled() -> bool:
    return bool(URL or TOKEN_FILE)


def _configuration() -> tuple[str, str]:
    if not URL or not TOKEN_FILE:
        raise AdmissionError("candidate admission URL and token file must be configured together")
    parsed = urllib.parse.urlsplit(URL)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AdmissionError("candidate admission URL must use local HTTP loopback")
    try:
        token = pathlib.Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise AdmissionError("candidate admission token file is not readable") from exc
    if not token:
        raise AdmissionError("candidate admission token file is empty")
    return URL, token


def request(action: str, payload: dict) -> dict:
    if not enabled():
        return {"disabled": True}
    base, token = _configuration()
    req = urllib.request.Request(
        f"{base}/v1/{action}",
        data=json.dumps(payload, separators=(",", ":")).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=2) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read())
        except Exception:
            body = {}
        finally:
            exc.close()
        raise AdmissionError(str(body.get("error") or f"admission HTTP {exc.code}")) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise AdmissionError("candidate admission boundary is unavailable") from exc
    if not isinstance(result, dict):
        raise AdmissionError("candidate admission response is invalid")
    return result


def admit(task_id: str, project_id: str, operation_class: str) -> int | None:
    if not enabled():
        return None
    result = request(
        "admit",
        {"task_id": task_id, "project_id": project_id, "operation_class": operation_class},
    )
    return int(result["generation"])


def authorize_effect(task_id: str, generation: int | None, project_id: str, operation_class: str) -> None:
    if not enabled():
        return
    if generation is None:
        raise AdmissionError("candidate task is missing its admission generation")
    request(
        "authorize-effect",
        {
            "task_id": task_id,
            "generation": generation,
            "project_id": project_id,
            "operation_class": operation_class,
        },
    )


def finish(task_id: str) -> None:
    if enabled():
        request("finish", {"task_id": task_id})
