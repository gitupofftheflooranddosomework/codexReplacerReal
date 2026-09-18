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

    def test_http_provider_resolves_identity_address_and_state(self):
        calls=[]
        def transport(url,timeout,token):
            calls.append((url,timeout,token))
            return {
                "logicalId":"station-3",
                "name":"swf-workstation-03",
                "address":"10.77.20.43",
                "state":"running",
                "provider":"proxmox",
                "providerId":58213,
            }
        provider=worker_provider.HttpWorkerProvider(
            base_url="http://vm-provider.internal/",
            bearer_token="x"*32,
            timeout=4,
            transport=transport,
        )
        self.assertEqual(provider.worker_name(3),"swf-workstation-03")
        self.assertEqual(provider.worker_ip(3),"10.77.20.43")
        self.assertEqual(provider.state(3),"running")
        self.assertEqual(calls[0],("http://vm-provider.internal/v1/workers/station-3",4,"x"*32))

    def test_http_provider_rejects_identity_mismatch(self):
        provider=worker_provider.HttpWorkerProvider(
            base_url="http://provider",
            bearer_token="x"*32,
            transport=lambda *_: {
                "logicalId":"station-99",
                "address":"10.0.0.9",
                "state":"running",
            },
        )
        with self.assertRaisesRegex(worker_provider.WorkerProviderError,"identity mismatch"):
            provider.worker_ip(1)

    def test_http_provider_rejects_missing_address(self):
        provider=worker_provider.HttpWorkerProvider(
            base_url="http://provider",
            bearer_token="x"*32,
            transport=lambda *_: {
                "logicalId":"station-1",
                "state":"running",
            },
        )
        with self.assertRaisesRegex(worker_provider.WorkerProviderError,"missing address"):
            provider.worker_ip(1)

    def test_http_provider_state_degrades_to_unknown_on_failure(self):
        def fail(*_):
            raise worker_provider.WorkerProviderError("down")
        provider=worker_provider.HttpWorkerProvider(
            base_url="http://provider",
            bearer_token="x"*32,
            transport=fail,
        )
        self.assertEqual(provider.state(1),"unknown")

    def test_http_provider_selection_requires_url(self):
        with patch.dict("os.environ",{"CODEX_LAB_WORKER_PROVIDER":"http"},clear=True):
            with self.assertRaisesRegex(worker_provider.WorkerProviderError,"PROVIDER_URL"):
                worker_provider.load_worker_provider()

    def test_http_provider_selection_from_environment(self):
        with patch.dict(
            "os.environ",
            {
                "CODEX_LAB_WORKER_PROVIDER":"http",
                "CODEX_LAB_WORKER_PROVIDER_URL":"http://fabric-provider.internal",
                "CODEX_LAB_WORKER_PROVIDER_TOKEN":"z"*32,
                "CODEX_LAB_WORKER_PROVIDER_TIMEOUT":"7",
            },
            clear=True,
        ):
            provider=worker_provider.load_worker_provider()
        self.assertEqual(provider.name,"http")
        self.assertEqual(provider.base_url,"http://fabric-provider.internal")
        self.assertEqual(provider.timeout,7)
        self.assertEqual(provider.bearer_token,"z"*32)

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
