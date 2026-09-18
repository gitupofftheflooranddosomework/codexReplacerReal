#!/usr/bin/env python3
"""Worker compute-provider boundary for the interactive Codex VM Lab.

The scheduler owns leases, policy, history and public APIs. Providers own only
worker identity/address and compute lifecycle observations. The legacy libvirt
provider remains the default until the Proxmox provider passes canary gates.
"""
from __future__ import annotations

import os
import subprocess
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]


class WorkerProviderError(RuntimeError):
    pass


class WorkerProvider:
    name = "base"

    def worker_name(self, station: int) -> str:
        raise NotImplementedError

    def worker_ip(self, station: int) -> str:
        raise NotImplementedError

    def state(self, station: int) -> str:
        raise NotImplementedError


@dataclass
class LibvirtWorkerProvider(WorkerProvider):
    """Behavior-compatible adapter for the current nested-libvirt pool."""

    uri: str = "qemu:///system"
    network_base: str = "192.168.122"
    first_worker_octet: int = 230
    command_timeout: int = 5
    runner: Runner = subprocess.run

    name = "libvirt"

    def worker_name(self, station: int) -> str:
        return f"codex-lab-vm-{int(station):02d}"

    def worker_ip(self, station: int) -> str:
        station = int(station)
        if station < 1:
            raise WorkerProviderError("station must be >= 1")
        return f"{self.network_base}.{self.first_worker_octet + station - 1}"

    def state(self, station: int) -> str:
        args = [
            "virsh",
            "-c",
            self.uri,
            "domstate",
            self.worker_name(station),
        ]
        try:
            result = self.runner(
                args,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.command_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "unknown"
        return result.stdout.strip() if result.returncode == 0 else "absent"


HttpTransport = Callable[[str, int, str], dict[str, Any]]


def default_http_transport(url: str, timeout: int, bearer_token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer_token}",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except Exception as exc:
        raise WorkerProviderError(f"worker provider request failed: {url}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerProviderError(f"worker provider returned invalid JSON: {url}") from exc
    if not isinstance(value, dict):
        raise WorkerProviderError(f"worker provider returned non-object response: {url}")
    return value


@dataclass
class HttpWorkerProvider(WorkerProvider):
    base_url: str = ""
    bearer_token: str = ""
    timeout: int = 5
    transport: HttpTransport = default_http_transport

    name = "http"

    def __post_init__(self) -> None:
        self.base_url = str(self.base_url).rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise WorkerProviderError("CODEX_LAB_WORKER_PROVIDER_URL must be http(s)")
        if len(str(self.bearer_token)) < 24:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_PROVIDER_TOKEN must be at least 24 characters"
            )
        if self.timeout < 1 or self.timeout > 30:
            raise WorkerProviderError("HTTP worker provider timeout must be 1..30 seconds")

    def logical_id(self, station: int) -> str:
        station = int(station)
        if station < 1:
            raise WorkerProviderError("station must be >= 1")
        return f"station-{station}"

    def record(self, station: int) -> dict[str, Any]:
        logical_id = self.logical_id(station)
        value = self.transport(
            f"{self.base_url}/v1/workers/{logical_id}",
            self.timeout,
            self.bearer_token,
        )
        if str(value.get("logicalId") or "") != logical_id:
            raise WorkerProviderError(
                f"worker provider identity mismatch for {logical_id}"
            )
        state = str(value.get("state") or "").strip()
        address = str(value.get("address") or "").strip()
        name = str(value.get("name") or logical_id).strip()
        if not state:
            raise WorkerProviderError(f"worker provider record missing state for {logical_id}")
        if not address:
            raise WorkerProviderError(f"worker provider record missing address for {logical_id}")
        return {**value, "state": state, "address": address, "name": name}

    def worker_name(self, station: int) -> str:
        return self.record(station)["name"]

    def worker_ip(self, station: int) -> str:
        return self.record(station)["address"]

    def state(self, station: int) -> str:
        try:
            return self.record(station)["state"]
        except WorkerProviderError:
            return "unknown"


def load_worker_provider(name: str | None = None, *, runner: Runner | None = None) -> WorkerProvider:
    selected = str(name or os.environ.get("CODEX_LAB_WORKER_PROVIDER", "libvirt")).strip().lower()
    if selected == "libvirt":
        kwargs = {
            "uri": os.environ.get("CODEX_LAB_LIBVIRT_URI", "qemu:///system"),
            "network_base": os.environ.get("CODEX_LAB_LIBVIRT_NETWORK_BASE", "192.168.122"),
            "first_worker_octet": int(os.environ.get("CODEX_LAB_LIBVIRT_FIRST_WORKER_OCTET", "230")),
        }
        if runner is not None:
            kwargs["runner"] = runner
        return LibvirtWorkerProvider(**kwargs)
    if selected == "http":
        base_url = os.environ.get("CODEX_LAB_WORKER_PROVIDER_URL", "").strip()
        bearer_token = os.environ.get("CODEX_LAB_WORKER_PROVIDER_TOKEN", "").strip()
        if not base_url:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_PROVIDER_URL is required for http provider"
            )
        if not bearer_token:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_PROVIDER_TOKEN is required for http provider"
            )
        return HttpWorkerProvider(
            base_url=base_url,
            bearer_token=bearer_token,
            timeout=int(os.environ.get("CODEX_LAB_WORKER_PROVIDER_TIMEOUT", "5")),
        )
    raise WorkerProviderError(
        f"unsupported CODEX_LAB_WORKER_PROVIDER={selected!r}; supported providers: libvirt, http"
    )
