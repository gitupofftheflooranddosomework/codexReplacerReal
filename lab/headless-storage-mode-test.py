#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent

def load_allocator():
    spec = importlib.util.spec_from_file_location("headless_allocator", ROOT / "codex-ci-headless.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class HeadlessStorageModeTests(unittest.TestCase):
    def test_provision_directory_mode_ignores_restrictive_umask(self):
        allocator = load_allocator()
        with tempfile.TemporaryDirectory() as td:
            temp = pathlib.Path(td)
            allocator.ROOT = temp / "control"
            allocator.DB = allocator.ROOT / "lifecycle.sqlite3"
            allocator.STATE = allocator.ROOT / "state.json"
            allocator.LOCK = allocator.ROOT / "allocator.lock"
            allocator.PROVISION_LOCK = allocator.ROOT / "provision.lock"
            allocator.PROVISIONERS = allocator.ROOT / "provisioners"
            store = temp / "store"
            store.mkdir()
            rec = {
                "id": "a" * 32, "name": "ci-mode-proof-aaaaaaaa", "ip": "192.168.122.100",
                "mac": "52:54:00:ce:00:64", "owner": "test", "project": "test", "session_key": None,
                "storage_root": str(store), "memory_mib": 1024, "max_memory_mib": 2048,
                "vcpus": 1, "disk_gib": 40, "created_at": allocator.stamp(), "started_at": None,
                "finished_at": None, "expires_at": allocator.stamp(allocator.now()+allocator.timedelta(minutes=5)),
                "destroyed_at": None, "status": "creating", "exit_code": None, "teardown_reason": None, "error": None,
            }
            con=allocator.db()
            con.execute("""INSERT INTO instances(id,name,ip,mac,owner,project,session_key,storage_root,memory_mib,max_memory_mib,vcpus,disk_gib,created_at,started_at,finished_at,expires_at,destroyed_at,status,exit_code,teardown_reason,error) VALUES(:id,:name,:ip,:mac,:owner,:project,:session_key,:storage_root,:memory_mib,:max_memory_mib,:vcpus,:disk_gib,:created_at,:started_at,:finished_at,:expires_at,:destroyed_at,:status,:exit_code,:teardown_reason,:error)""", rec)
            con.commit(); con.close()
            seen=[]
            def fake_run(argv, timeout=60, check=True):
                if argv and argv[0] == "virt-install":
                    seen.append(stat.S_IMODE((store/rec["name"]).stat().st_mode))
                return subprocess.CompletedProcess(argv,0,"","")
            old_umask=os.umask(0o077)
            try:
                with mock.patch.object(allocator,"run",fake_run), \
                     mock.patch.object(allocator,"virsh",lambda *a,**k: subprocess.CompletedProcess(a,0,"","")), \
                     mock.patch.object(allocator,"base_virtual_bytes",return_value=40*1024**3), \
                     mock.patch.object(allocator,"write_state",lambda con: None):
                    result=allocator.provision(rec)
            finally:
                os.umask(old_umask)
            self.assertEqual(result["status"],"running")
            self.assertEqual(seen,[0o711])
            self.assertEqual(stat.S_IMODE((store/rec["name"]).stat().st_mode),0o711)

if __name__ == "__main__":
    unittest.main()
