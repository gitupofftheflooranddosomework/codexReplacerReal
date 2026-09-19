#!/usr/bin/env python3
import importlib.util
import pathlib
import sys
import unittest
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
spec=importlib.util.spec_from_file_location("scheduler_under_test", ROOT / "scheduler.py")
scheduler=importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class FakeProvider:
    name="serverworkerfabric"
    def worker_name(self, station):
        return f"swf-station-{int(station):02d}"
    def worker_ip(self, station):
        return f"10.77.20.{30+int(station)}"
    def state(self, station):
        return "running"


class SchedulerWorkerEndpointTests(unittest.TestCase):
    def test_endpoint_uses_active_provider(self):
        with mock.patch.object(scheduler, "WORKER_PROVIDER", FakeProvider()):
            got=scheduler.worker_endpoint(3)
        self.assertEqual(got, {
            "station":3,
            "name":"swf-station-03",
            "ip":"10.77.20.33",
            "state":"running",
            "provider":"serverworkerfabric",
        })

    def test_endpoint_rejects_invalid_station(self):
        with self.assertRaisesRegex(ValueError, "station must be"):
            scheduler.worker_endpoint(0)


if __name__=="__main__":
    unittest.main(verbosity=2)
