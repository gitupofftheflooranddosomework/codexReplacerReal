#!/usr/bin/env python3
import io
import pathlib
import subprocess
import sys
import unittest
import urllib.error
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


class Response:
    def __init__(self, payload, status=200):
        import json
        self.body=json.dumps(payload).encode()
        self.status=status
    def __enter__(self):
        return self
    def __exit__(self,*_a):
        return False
    def read(self):
        return self.body


class FabricProviderTests(unittest.TestCase):
    def worker(self, **overrides):
        payload={
            "logical_id":"station-03",
            "state":"running",
            "provider_id":58203,
            "node":"pve-a",
            "address":"10.77.20.33",
            "metadata":{"name":"swf-station-03","type":"qemu","tags":["serverworkerfabric"]},
        }
        payload.update(overrides)
        return payload

    def test_opt_in_provider_reads_name_address_and_state(self):
        calls=[]
        def open_url(request, timeout):
            calls.append((request,timeout))
            return Response(self.worker())
        provider=worker_provider.ServerWorkerFabricProvider(
            "http://fabric.internal:8788",
            urlopen=open_url,
        )
        self.assertEqual(provider.worker_name(3),"swf-station-03")
        self.assertEqual(provider.worker_ip(3),"10.77.20.33")
        self.assertEqual(provider.state(3),"running")
        self.assertTrue(all(call[0].full_url.endswith("/v1/workers/station-03") for call in calls))
        self.assertTrue(all(call[1]==5.0 for call in calls))

    def test_loader_keeps_libvirt_default_but_supports_swf_opt_in(self):
        with patch.dict(
            "os.environ",
            {
                "CODEX_LAB_WORKER_PROVIDER":"serverworkerfabric",
                "CODEX_LAB_WORKER_API_URL":"https://fabric.example.invalid",
                "CODEX_LAB_WORKER_LOGICAL_PREFIX":"workstation-",
                "CODEX_LAB_WORKER_API_TIMEOUT":"3.5",
            },
            clear=True,
        ):
            provider=worker_provider.load_worker_provider(
                urlopen=lambda *_a,**_k: Response({
                    "logical_id":"workstation-01",
                    "state":"running",
                    "provider_id":1,
                    "node":"pve-a",
                    "address":"10.0.0.1",
                    "metadata":{},
                })
            )
        self.assertEqual(provider.name,"serverworkerfabric")
        self.assertEqual(provider.logical_id(1),"workstation-01")
        self.assertEqual(provider.timeout,3.5)

    def test_missing_url_fails_closed(self):
        with patch.dict(
            "os.environ",
            {"CODEX_LAB_WORKER_PROVIDER":"serverworkerfabric"},
            clear=True,
        ):
            with self.assertRaisesRegex(worker_provider.WorkerProviderError,"WORKER_API_URL"):
                worker_provider.load_worker_provider()

    def test_http_404_maps_state_to_absent(self):
        def open_url(request, timeout):
            raise urllib.error.HTTPError(request.full_url,404,"not found",{},io.BytesIO())
        provider=worker_provider.ServerWorkerFabricProvider(
            "http://fabric.internal:8788",urlopen=open_url
        )
        self.assertEqual(provider.state(1),"absent")

    def test_transport_failure_maps_state_to_unknown(self):
        def open_url(*_a,**_k):
            raise urllib.error.URLError("offline")
        provider=worker_provider.ServerWorkerFabricProvider(
            "http://fabric.internal:8788",urlopen=open_url
        )
        self.assertEqual(provider.state(1),"unknown")

    def test_missing_address_does_not_recreate_legacy_ip(self):
        provider=worker_provider.ServerWorkerFabricProvider(
            "http://fabric.internal:8788",
            urlopen=lambda *_a,**_k: Response(self.worker(address=None)),
        )
        with self.assertRaisesRegex(worker_provider.WorkerProviderError,"no provider-resolved address"):
            provider.worker_ip(3)

    def test_identity_mismatch_fails_closed(self):
        provider=worker_provider.ServerWorkerFabricProvider(
            "http://fabric.internal:8788",
            urlopen=lambda *_a,**_k: Response(self.worker(logical_id="station-99")),
        )
        with self.assertRaisesRegex(worker_provider.WorkerProviderError,"identity mismatch"):
            provider.worker_name(3)

    def test_invalid_station_is_rejected_before_network(self):
        provider=worker_provider.ServerWorkerFabricProvider(
            "http://fabric.internal:8788",
            urlopen=lambda *_a,**_k: self.fail("network should not be called"),
        )
        with self.assertRaisesRegex(worker_provider.WorkerProviderError,"station"):
            provider.worker_ip(0)


if __name__=="__main__":
    unittest.main()
