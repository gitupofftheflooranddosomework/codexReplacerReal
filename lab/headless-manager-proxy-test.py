#!/usr/bin/env python3
import importlib.util
import json
import pathlib
import subprocess
import unittest
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("scheduler_proxy_under_test", ROOT / "headless-job-scheduler.py")
scheduler=importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class HeadlessManagerProxyTests(unittest.TestCase):
    def test_proxy_is_disabled_by_default(self):
        with mock.patch.object(scheduler,"MANAGER_PROXY_ENABLED",False):
            with self.assertRaisesRegex(RuntimeError,"disabled"):
                scheduler.manager_proxy({"args":["reserve","--owner","test"]})

    def test_proxy_rejects_non_dispatch_lifecycle_actions(self):
        with mock.patch.object(scheduler,"MANAGER_PROXY_ENABLED",True):
            with self.assertRaisesRegex(ValueError,"not allowed"):
                scheduler.manager_proxy({"args":["gc"]})
            with self.assertRaisesRegex(ValueError,"not allowed"):
                scheduler.manager_proxy({"args":["state"]})

    def test_proxy_forwards_exact_argv_without_shell(self):
        completed=subprocess.CompletedProcess(
            [],0,
            json.dumps({"instance":{"id":"abc","ip":"192.0.2.7"},"reused":False}),
            ""
        )
        with mock.patch.object(scheduler,"MANAGER_PROXY_ENABLED",True), \
             mock.patch.object(scheduler.subprocess,"run",return_value=completed) as run:
            got=scheduler.manager_proxy({
                "args":["reserve","--owner","DotMoose","--project","DotMoose"]
            })
        self.assertFalse(got["reused"])
        argv=run.call_args.args[0]
        self.assertEqual(argv,[
            scheduler.MANAGER,
            "reserve","--owner","DotMoose","--project","DotMoose",
        ])
        self.assertNotIn("shell",run.call_args.kwargs)
        self.assertFalse(run.call_args.kwargs.get("check"))

    def test_proxy_validates_provision_shape(self):
        with mock.patch.object(scheduler,"MANAGER_PROXY_ENABLED",True):
            with self.assertRaisesRegex(ValueError,"exactly one"):
                scheduler.manager_proxy({"args":["provision","a","b"]})

    def test_proxy_propagates_manager_failure_without_parsing_stdout(self):
        completed=subprocess.CompletedProcess([],7,"","capacity unavailable")
        with mock.patch.object(scheduler,"MANAGER_PROXY_ENABLED",True), \
             mock.patch.object(scheduler.subprocess,"run",return_value=completed):
            with self.assertRaisesRegex(RuntimeError,"capacity unavailable"):
                scheduler.manager_proxy({"args":["reserve","--owner","x"]})


if __name__=="__main__":
    unittest.main(verbosity=2)
