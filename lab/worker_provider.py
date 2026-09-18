#!/usr/bin/env python3
"""Worker compute-provider boundary for the interactive Codex VM Lab.

The scheduler owns leases, policy, history and public APIs. Providers own only
worker identity/address and compute lifecycle observations. The legacy libvirt
provider remains the default until the Proxmox provider passes canary gates.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable

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
    raise WorkerProviderError(
        f"unsupported CODEX_LAB_WORKER_PROVIDER={selected!r}; supported providers: libvirt"
    )
