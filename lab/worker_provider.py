#!/usr/bin/env python3
"""Worker compute-provider boundary for the interactive Codex VM Lab.

The scheduler owns leases, policy, history and public APIs. Providers own only
worker identity/address and compute lifecycle observations. The legacy libvirt
provider remains the default until the external ServerWorkerFabric provider
passes canary gates.
"""
from __future__ import annotations

import json
import os
import pathlib
import socket
import ssl
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]
Urlopen = Callable[..., object]


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


@dataclass
class ServerWorkerFabricProvider(WorkerProvider):
    """Read-only adapter for the external ServerWorkerFabric worker API."""

    base_url: str
    api_token: str
    logical_prefix: str = "station-"
    timeout: float = 5.0
    ca_file: str | None = None
    urlopen: Urlopen = urllib.request.urlopen

    name = "serverworkerfabric"

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_API_URL must be an absolute http(s) URL"
            )
        if not self.api_token:
            raise WorkerProviderError(
                "ServerWorkerFabric worker API bearer token is required"
            )
        if not self.logical_prefix:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_LOGICAL_PREFIX must not be empty"
            )

    def logical_id(self, station: int) -> str:
        station = int(station)
        if station < 1:
            raise WorkerProviderError("station must be >= 1")
        return f"{self.logical_prefix}{station:02d}"

    def _worker(self, station: int) -> dict:
        logical_id = self.logical_id(station)
        url = (
            self.base_url
            + "/v1/workers/"
            + urllib.parse.quote(logical_id, safe="")
        )
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_token}",
                "User-Agent": "CodexReplacer/ServerWorkerFabricProvider",
            },
            method="GET",
        )
        try:
            open_kwargs = {"timeout": self.timeout}
            if self.ca_file:
                try:
                    open_kwargs["context"] = ssl.create_default_context(
                        cafile=self.ca_file
                    )
                except (OSError, ssl.SSLError) as exc:
                    raise WorkerProviderError(
                        "ServerWorkerFabric CA file could not be loaded"
                    ) from exc
            with self.urlopen(request, **open_kwargs) as response:
                raw = response.read()
                status = int(getattr(response, "status", 200))
        except urllib.error.HTTPError as exc:
            if int(exc.code) == 404:
                raise WorkerProviderError(f"worker {logical_id!r} is absent") from exc
            raise WorkerProviderError(
                f"ServerWorkerFabric returned HTTP {int(exc.code)}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            raise WorkerProviderError("ServerWorkerFabric request failed") from exc

        if status == 404:
            raise WorkerProviderError(f"worker {logical_id!r} is absent")
        if status < 200 or status >= 300:
            raise WorkerProviderError(
                f"ServerWorkerFabric returned HTTP {status}"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerProviderError(
                "ServerWorkerFabric returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise WorkerProviderError(
                "ServerWorkerFabric worker response must be an object"
            )
        if str(payload.get("logical_id") or "") != logical_id:
            raise WorkerProviderError(
                "ServerWorkerFabric logical worker identity mismatch"
            )
        return payload

    def worker_name(self, station: int) -> str:
        worker = self._worker(station)
        metadata = worker.get("metadata")
        if isinstance(metadata, dict):
            name = str(metadata.get("name") or "").strip()
            if name:
                return name
        return str(worker["logical_id"])

    def worker_ip(self, station: int) -> str:
        worker = self._worker(station)
        address = str(worker.get("address") or "").strip()
        if not address:
            raise WorkerProviderError(
                f"worker {worker.get('logical_id')!r} has no provider-resolved address"
            )
        return address

    def state(self, station: int) -> str:
        try:
            worker = self._worker(station)
        except WorkerProviderError as exc:
            if " is absent" in str(exc):
                return "absent"
            return "unknown"
        return str(worker.get("state") or "unknown").strip().lower() or "unknown"


def _worker_api_token_from_env() -> str:
    direct = str(os.environ.get("CODEX_LAB_WORKER_API_TOKEN", "")).strip()
    if direct:
        return direct
    token_file = str(os.environ.get("CODEX_LAB_WORKER_API_TOKEN_FILE", "")).strip()
    if token_file:
        try:
            value = pathlib.Path(token_file).read_text().strip()
        except OSError as exc:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_API_TOKEN_FILE is not readable"
            ) from exc
        if value:
            return value
    raise WorkerProviderError(
        "CODEX_LAB_WORKER_API_TOKEN or CODEX_LAB_WORKER_API_TOKEN_FILE "
        "is required for serverworkerfabric"
    )


def load_worker_provider(
    name: str | None = None,
    *,
    runner: Runner | None = None,
    urlopen: Urlopen | None = None,
) -> WorkerProvider:
    selected = str(
        name or os.environ.get("CODEX_LAB_WORKER_PROVIDER", "libvirt")
    ).strip().lower()
    if selected == "libvirt":
        kwargs = {
            "uri": os.environ.get("CODEX_LAB_LIBVIRT_URI", "qemu:///system"),
            "network_base": os.environ.get(
                "CODEX_LAB_LIBVIRT_NETWORK_BASE", "192.168.122"
            ),
            "first_worker_octet": int(
                os.environ.get(
                    "CODEX_LAB_LIBVIRT_FIRST_WORKER_OCTET", "230"
                )
            ),
        }
        if runner is not None:
            kwargs["runner"] = runner
        return LibvirtWorkerProvider(**kwargs)
    if selected in {"serverworkerfabric", "swf"}:
        base_url = str(
            os.environ.get("CODEX_LAB_WORKER_API_URL", "")
        ).strip()
        if not base_url:
            raise WorkerProviderError(
                "CODEX_LAB_WORKER_API_URL is required for serverworkerfabric"
            )
        kwargs = {
            "base_url": base_url,
            "api_token": _worker_api_token_from_env(),
            "logical_prefix": os.environ.get(
                "CODEX_LAB_WORKER_LOGICAL_PREFIX", "station-"
            ),
            "timeout": float(
                os.environ.get("CODEX_LAB_WORKER_API_TIMEOUT", "5")
            ),
            "ca_file": (
                str(os.environ.get("CODEX_LAB_WORKER_API_CA_FILE", "")).strip()
                or None
            ),
        }
        if urlopen is not None:
            kwargs["urlopen"] = urlopen
        return ServerWorkerFabricProvider(**kwargs)
    raise WorkerProviderError(
        "unsupported CODEX_LAB_WORKER_PROVIDER="
        f"{selected!r}; supported providers: libvirt, serverworkerfabric"
    )
