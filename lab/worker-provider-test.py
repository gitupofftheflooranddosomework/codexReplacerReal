#!/usr/bin/env python3
import pathlib
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))

import worker_provider


class Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode=returncode
        self.stdout=stdout
        self.stderr=stderr


class WorkerProviderTests(unittest.TestCase):
    def test_default_provider_is_libvirt(self):
        with patch.dict("os.environ", {}, clear=True):
            provider=worker_provider.load_worker_provider()
        self.assertEqual(provider.name,"libvirt")

    def test_legacy_identity_and_addresses_are_preserved(self):
        provider=worker_provider.LibvirtWorkerProvider(runner=lambda *a,**k: Result())
        self.assertEqual(provider.worker_name(1),"codex-lab-vm-01")
        self.assertEqual(provider.worker_name(9),"codex-lab-vm-09")
        self.assertEqual(provider.worker_ip(1),"192.168.122.230")
        self.assertEqual(provider.worker_ip(9),"192.168.122.238")

    def test_state_uses_expected_virsh_domain(self):
        calls=[]
        def run(args,**kwargs):
            calls.append((args,kwargs))
            return Result(0,"running\n","")
        provider=worker_provider.LibvirtWorkerProvider(runner=run)
        self.assertEqual(provider.state(4),"running")
        self.assertEqual(
            calls[0][0],
            ["virsh","-c","qemu:///system","domstate","codex-lab-vm-04"],
        )
        self.assertEqual(calls[0][1]["timeout"],5)

    def test_missing_domain_maps_to_absent(self):
        provider=worker_provider.LibvirtWorkerProvider(
            runner=lambda *a,**k: Result(1,"","not found")
        )
        self.assertEqual(provider.state(2),"absent")

    def test_timeout_maps_to_unknown(self):
        def run(*_a,**_k):
            raise subprocess.TimeoutExpired(["virsh"],5)
        provider=worker_provider.LibvirtWorkerProvider(runner=run)
        self.assertEqual(provider.state(2),"unknown")

    def test_provider_selection_is_fail_closed(self):
        with self.assertRaisesRegex(worker_provider.WorkerProviderError,"unsupported"):
            worker_provider.load_worker_provider("proxmox")

    def test_environment_overrides_legacy_libvirt_parameters(self):
        with patch.dict(
            "os.environ",
            {
                "CODEX_LAB_WORKER_PROVIDER":"libvirt",
                "CODEX_LAB_LIBVIRT_URI":"qemu+unix:///system",
                "CODEX_LAB_LIBVIRT_NETWORK_BASE":"10.77.9",
                "CODEX_LAB_LIBVIRT_FIRST_WORKER_OCTET":"40",
            },
            clear=True,
        ):
            provider=worker_provider.load_worker_provider(runner=lambda *a,**k: Result())
        self.assertEqual(provider.uri,"qemu+unix:///system")
        self.assertEqual(provider.worker_ip(3),"10.77.9.42")


if __name__=="__main__":
    unittest.main()
