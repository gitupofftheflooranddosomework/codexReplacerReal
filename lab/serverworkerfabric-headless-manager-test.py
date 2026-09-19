#!/usr/bin/env python3
import importlib.util
import io
import json
import pathlib
import unittest
import urllib.error
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location(
    "swf_headless_manager_under_test",
    ROOT / "serverworkerfabric-headless-manager.py",
)
m=importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


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


class ServerWorkerFabricHeadlessManagerTests(unittest.TestCase):
    def settings(self):
        return (
            mock.patch.object(m,"BASE_URL","https://fabric.internal"),
            mock.patch.object(m,"TOKEN","secret"),
            mock.patch.object(m,"TOKEN_FILE",""),
        )

    def test_reserve_creates_disposable_headless_with_bounded_ttl(self):
        calls=[]
        def open_url(request,timeout):
            calls.append((request,timeout))
            return Response({
                "instance_id":"headless-abcd",
                "expires_at_epoch":1600,
                "worker":{
                    "logical_id":"headless-abcd",
                    "state":"running",
                    "address":None,
                    "metadata":{"name":"swf-headless-abcd"},
                },
            },201)
        p1,p2,p3=self.settings()
        with p1,p2,p3,mock.patch.object(m.urllib.request,"urlopen",side_effect=open_url):
            got=m.reserve("DotMoose-site-build","DotMoose",600)
        self.assertFalse(got["reused"])
        self.assertEqual(got["instance"]["id"],"headless-abcd")
        self.assertEqual(got["instance"]["name"],"swf-headless-abcd")
        self.assertEqual(got["instance"]["ip"],"")
        request,timeout=calls[0]
        self.assertEqual(request.full_url,"https://fabric.internal/v1/headless")
        self.assertEqual(request.method,"POST")
        self.assertEqual(timeout,m.HTTP_TIMEOUT)
        self.assertEqual(request.get_header("Authorization"),"Bearer secret")
        payload=json.loads(request.data)
        self.assertEqual(payload["profile"],m.PROFILE)
        self.assertEqual(payload["ttl_seconds"],600)
        self.assertRegex(payload["correlation_id"],r"^ci-[0-9a-f]{16}$")

    def test_reserve_refuses_retained_session_before_api_call(self):
        p1,p2,p3=self.settings()
        with p1,p2,p3,mock.patch.object(
            m.urllib.request,"urlopen",side_effect=AssertionError("API must not run")
        ):
            with self.assertRaisesRegex(m.FabricManagerError,"retained session"):
                m.reserve("owner","project",600,session_key="reuse-me")

    def test_reserve_rejects_out_of_bounds_ttl_before_api_call(self):
        p1,p2,p3=self.settings()
        with p1,p2,p3,mock.patch.object(
            m.urllib.request,"urlopen",side_effect=AssertionError("API must not run")
        ):
            for ttl in (59,604801):
                with self.subTest(ttl=ttl):
                    with self.assertRaisesRegex(m.FabricManagerError,"ttl_seconds"):
                        m.reserve("owner","project",ttl)

    def test_provision_polls_until_guest_agent_resolves_ip(self):
        responses=[
            Response({"ready":False,"reason":"worker-not-ready"},202),
            Response({
                "ready":True,
                "worker":{
                    "logical_id":"headless-abcd",
                    "state":"running",
                    "address":"10.77.20.44",
                    "metadata":{"name":"swf-headless-abcd"},
                },
            },200),
        ]
        calls=[]
        def open_url(request,timeout):
            calls.append((request,timeout))
            return responses.pop(0)
        p1,p2,p3=self.settings()
        with p1,p2,p3,              mock.patch.object(m,"READY_TIMEOUT",10),              mock.patch.object(m,"POLL_SECONDS",0.001),              mock.patch.object(m.urllib.request,"urlopen",side_effect=open_url),              mock.patch.object(m.time,"sleep"):
            got=m.provision("headless-abcd")
        self.assertEqual(got["id"],"headless-abcd")
        self.assertEqual(got["ip"],"10.77.20.44")
        self.assertEqual(got["name"],"swf-headless-abcd")
        self.assertEqual(len(calls),2)
        self.assertTrue(all(
            c[0].full_url.endswith("/v1/workers/headless-abcd/ready")
            for c in calls
        ))

    def test_provision_identity_mismatch_fails_closed(self):
        p1,p2,p3=self.settings()
        with p1,p2,p3,mock.patch.object(
            m.urllib.request,"urlopen",
            return_value=Response({
                "ready":True,
                "worker":{
                    "logical_id":"headless-other",
                    "state":"running",
                    "address":"10.77.20.44",
                    "metadata":{},
                },
            },200),
        ):
            with self.assertRaisesRegex(m.FabricManagerError,"identity mismatch"):
                m.provision("headless-abcd")

    def test_finish_refuses_retention_without_deleting(self):
        p1,p2,p3=self.settings()
        with p1,p2,p3,mock.patch.object(
            m.urllib.request,"urlopen",side_effect=AssertionError("DELETE must not run")
        ):
            with self.assertRaisesRegex(m.FabricManagerError,"keep_seconds"):
                m.finish("headless-abcd",keep_seconds=60)

    def test_finish_delete_is_idempotent_when_gc_already_removed_worker(self):
        error=urllib.error.HTTPError(
            "https://fabric.internal/v1/headless/headless-abcd",
            404,"not found",{},
            io.BytesIO(b'{"error":"worker not found"}'),
        )
        p1,p2,p3=self.settings()
        with p1,p2,p3,mock.patch.object(
            m.urllib.request,"urlopen",side_effect=error
        ):
            got=m.finish("headless-abcd")
        self.assertEqual(got["status"],"destroyed")
        self.assertTrue(got["already_absent"])
        self.assertFalse(got["deleted"])

    def test_missing_token_fails_closed_before_network(self):
        with mock.patch.object(m,"BASE_URL","https://fabric.internal"),              mock.patch.object(m,"TOKEN",""),              mock.patch.object(m,"TOKEN_FILE",""),              mock.patch.object(
                 m.urllib.request,"urlopen",
                 side_effect=AssertionError("network must not run"),
             ):
            with self.assertRaisesRegex(m.FabricManagerError,"API token"):
                m.reserve("owner","project",600)


if __name__=="__main__":
    unittest.main(verbosity=2)
