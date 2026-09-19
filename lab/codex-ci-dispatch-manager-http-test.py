#!/usr/bin/env python3
import importlib.util
import io
import json
import pathlib
import subprocess
import unittest
import urllib.error
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location("dispatch_under_test", ROOT / "codex-ci-dispatch.py")
dispatch=importlib.util.module_from_spec(spec)
spec.loader.exec_module(dispatch)


class Response:
    def __init__(self,payload,status=200):
        self.body=json.dumps(payload).encode()
        self.status=status
    def __enter__(self):
        return self
    def __exit__(self,*_a):
        return False
    def read(self):
        return self.body


class DispatchManagerHTTPTests(unittest.TestCase):
    def test_http_mode_forwards_exact_argv_without_local_manager(self):
        calls=[]
        def open_url(request,timeout):
            calls.append((request,timeout))
            return Response({"instance":{"id":"abc","ip":"192.0.2.5"},"reused":False})
        with mock.patch.object(dispatch,"MANAGER_URL","http://scheduler.internal:8767/api/manager"), \
             mock.patch.object(dispatch.urllib.request,"urlopen",side_effect=open_url), \
             mock.patch.object(dispatch.subprocess,"run",side_effect=AssertionError("local manager must not run")):
            got=dispatch.manager("reserve","--owner","DotMoose","--project","DotMoose",timeout=45)
        self.assertFalse(got["reused"])
        request,timeout=calls[0]
        self.assertEqual(request.full_url,"http://scheduler.internal:8767/api/manager")
        self.assertEqual(request.method,"POST")
        self.assertEqual(timeout,45)
        self.assertEqual(json.loads(request.data),{
            "args":["reserve","--owner","DotMoose","--project","DotMoose"]
        })

    def test_main_uses_provision_resolved_address_before_worker_probe(self):
        import sys
        rec={"id":"headless-abcd","name":"swf-headless-abcd","ip":"","reused":False}
        manager_calls=[]
        probed=[]
        def manager(*args,**_kwargs):
            manager_calls.append(args)
            if args[0]=="provision":
                return {
                    "id":"headless-abcd",
                    "name":"swf-headless-abcd",
                    "ip":"10.77.20.44",
                }
            if args[0]=="finish":
                return {"status":"destroyed"}
            raise AssertionError(args)
        def ready(record,*_args,**_kwargs):
            probed.append(dict(record))
            return {"ready":True,"mode":"headless"}
        completed=subprocess.CompletedProcess([],0,b'{"ok":true}',b"")
        with mock.patch.object(
            sys,"argv",
            ["dispatch","--owner","test","--command","true"],
        ), mock.patch.object(dispatch,"validate_workspace"),              mock.patch.object(dispatch,"acquire",return_value=rec),              mock.patch.object(dispatch,"manager",side_effect=manager),              mock.patch.object(dispatch,"wait_ready",side_effect=ready),              mock.patch.object(dispatch,"ssh_capture",return_value=completed),              mock.patch.object(dispatch,"stream_snapshot"),              mock.patch.object(dispatch,"push_inputs"),              mock.patch.object(dispatch,"run_command",return_value=0),              mock.patch.object(dispatch,"pull_artifacts"):
            self.assertEqual(dispatch.main(),0)
        self.assertEqual(probed[0]["ip"],"10.77.20.44")
        self.assertEqual(manager_calls[0],("provision","headless-abcd"))

    def test_main_rejects_provision_identity_mismatch_before_ssh(self):
        import sys
        rec={"id":"headless-abcd","name":"swf-headless-abcd","ip":"","reused":False}
        with mock.patch.object(
            sys,"argv",
            ["dispatch","--owner","test","--command","true"],
        ), mock.patch.object(dispatch,"validate_workspace"),              mock.patch.object(dispatch,"acquire",return_value=rec),              mock.patch.object(
                 dispatch,"manager",
                 return_value={
                     "id":"headless-other",
                     "name":"swf-headless-other",
                     "ip":"10.77.20.45",
                 },
             ),              mock.patch.object(
                 dispatch,"wait_ready",
                 side_effect=AssertionError("worker probe must not run"),
             ),              mock.patch.object(
                 dispatch,"ssh_capture",
                 side_effect=AssertionError("SSH must not run"),
             ):
            with self.assertRaisesRegex(RuntimeError,"identity mismatch"):
                dispatch.main()

    def test_http_error_surfaces_scheduler_message(self):
        error=urllib.error.HTTPError(
            "http://scheduler/api/manager",400,"bad",{},
            io.BytesIO(b'{"error":"manager proxy action is not allowed: gc"}')
        )
        with mock.patch.object(dispatch,"MANAGER_URL","http://scheduler/api/manager"), \
             mock.patch.object(dispatch.urllib.request,"urlopen",side_effect=error):
            with self.assertRaisesRegex(RuntimeError,"not allowed: gc"):
                dispatch.manager("gc")

    def test_local_manager_remains_default_rollback_path(self):
        result=subprocess.CompletedProcess(
            ["manager","reserve"],0,
            json.dumps({"instance":{"id":"local"},"reused":True}),
            ""
        )
        with mock.patch.object(dispatch,"MANAGER_URL",""), \
             mock.patch.object(dispatch.subprocess,"run",return_value=result) as run:
            got=dispatch.manager("reserve","--owner","local",timeout=9)
        self.assertTrue(got["reused"])
        args=run.call_args.args[0]
        self.assertEqual(args[0],dispatch.MANAGER)
        self.assertEqual(args[1:],["reserve","--owner","local"])


if __name__=="__main__":
    unittest.main(verbosity=2)
