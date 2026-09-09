#!/usr/bin/env python3
"""Regression checks for orphan recovery, retention and admission separation."""
import importlib.util
import os
import pathlib
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parent

def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

class RecoveryTests(unittest.TestCase):
    def test_gc_retries_finished_and_lost_dispatcher_but_preserves_retained(self):
        m = load('codex-ci-headless')
        with tempfile.TemporaryDirectory() as tmp:
            m.ROOT = pathlib.Path(tmp)
            m.DB = m.ROOT/'lifecycle.sqlite3'
            m.LOCK = m.ROOT/'lock'
            m.STATE = m.ROOT/'state.json'
            c = m.db()
            rows = [('releasing','releasing'),('lost','running'),('retained','idle'),('alive','running')]
            for iid, status in rows:
                c.execute('INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (iid,'ci-'+iid,'192.0.2.1','mac','test','test',None,tmp,2048,4096,2,40,
                     m.stamp(),m.stamp(),m.stamp(),m.stamp(m.now()+timedelta(hours=2)),None,status,0,'succeeded',None))
                c.execute('INSERT INTO dispatch_owners VALUES(?,?,?)',
                    (iid,os.getpid(),m.process_identity(os.getpid()) if iid=='alive' else 'previous-boot:1'))
            c.commit(); c.close()
            calls=[]
            m.finish=lambda *args: calls.append(args)
            self.assertEqual(m.gc(), ['releasing','lost'])
            self.assertEqual(calls[0][3], 'succeeded')
            self.assertEqual(calls[1][3], 'dispatcher_lost')

    def test_reservation_identity_survives_provision_timeout(self):
        m=load('codex-ci-dispatch')
        rec={'id':'durable','name':'ci-test','ip':'192.0.2.1','reused':False}
        calls=[]
        import subprocess,sys
        def manager(*args,**kwargs):
            calls.append(args)
            if args[0]=='provision': raise subprocess.TimeoutExpired('provision',180)
            return {}
        with patch.object(sys,'argv',['dispatch','--owner','test','--command','true']), \
             patch.object(m,'validate_workspace'),patch.object(m,'acquire',return_value=rec), \
             patch.object(m,'manager',side_effect=manager):
            self.assertEqual(m.main(),124)
        self.assertEqual(calls[-1][:2],('finish','durable'))
        self.assertIn('124',calls[-1])

    def test_process_identity_rejects_missing_pid(self):
        m=load('codex-ci-headless')
        self.assertIsNone(m.process_identity(2147483647))
        self.assertIsNotNone(m.process_identity(os.getpid()))

    def test_admission_does_not_spend_unreclaimed_arc(self):
        m=load('codex-ci-headless')
        with patch.object(m,'meminfo',return_value=(192000,12000)), \
             patch.object(m,'zfs_arc_capacity',return_value={'reclaimableMiB':80000}):
            self.assertEqual(m.memory_capacity()['effectiveAvailableMiB'],12000)

    def test_worker_timeout_returns_unavailable_without_aborting_pool(self):
        import subprocess
        m=load('scheduler')
        with patch.object(m,'ssh_args',return_value=['ssh']), \
             patch.object(m,'run',side_effect=subprocess.TimeoutExpired('ssh',5)), \
             patch.object(m,'virsh_state',return_value='running'):
            result=m.worker_probe(1)
        self.assertFalse(result['ready'])
        self.assertIn('timed out',result['probeError'])

    def test_release_without_vault_cli_clears_lease_and_local_credentials(self):
        m=load('codex-ci-worker-rpc')
        with tempfile.TemporaryDirectory() as tmp:
            m.HOME=pathlib.Path(tmp)
            m.STATE_DIR=m.HOME/'state';m.STATE_DIR.mkdir()
            m.CLAIM_LOCK=m.STATE_DIR/'claim.lock';m.EXCLUSIVE=m.STATE_DIR/'exclusive.lock'
            m.WORK_ROOT=m.HOME/'work'
            iid='a'*32
            m.EXCLUSIVE.write_text('{"leaseId":"'+iid+'","source":"github-actions"}')
            data=m.HOME/'.config/Bitwarden CLI/data.json';data.parent.mkdir(parents=True);data.write_text('private')
            with patch.object(m.shutil,'which',return_value=None):
                self.assertEqual(m.action_release(['release',iid]),0)
            self.assertFalse(data.exists())
            self.assertFalse(m.EXCLUSIVE.exists())

if __name__=='__main__': unittest.main()
