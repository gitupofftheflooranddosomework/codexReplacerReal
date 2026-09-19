#!/usr/bin/env python3
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location("server_under_test", ROOT / "server.py")
server=importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class BrowserProviderEndpointTests(unittest.TestCase):
    def test_browser_worker_ip_uses_vm_lab_provider_resolver(self):
        client=server.KvmWorkerBrowserClient(3)
        with mock.patch.object(server.vm_lab_manager, "worker_ip_for", return_value="10.77.20.33") as resolve:
            self.assertEqual(client.worker_ip, "10.77.20.33")
        resolve.assert_called_once_with(3)


if __name__=="__main__":
    unittest.main(verbosity=2)
