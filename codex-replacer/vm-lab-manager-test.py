#!/usr/bin/env python3
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name('vm_lab_manager.py')
spec = importlib.util.spec_from_file_location('vm_lab_manager_under_test', MODULE_PATH)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def lease(station=3, lease_id='wanted'):
    return {
        'station': station, 'name': m.name_for(station), 'ip': m.ip_for(station),
        'status': 'leased', 'leaseId': lease_id, 'owner': 'test', 'project': 'test',
        'expiresAt': None,
    }


class DummyLock:
    def close(self):
        pass


class StateMirrorTests(unittest.TestCase):
    def test_mirror_state_to_home_sends_exact_json_over_ssh(self):
        state = {'version': 1, 'stations': {'4': lease(4, 'lease-4')}}
        completed = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(m.subprocess, 'run', return_value=completed) as run:
            m.mirror_state_to_home(state)
        kwargs = run.call_args.kwargs
        self.assertEqual(json.loads(kwargs['input']), state)
        self.assertTrue(kwargs['text'])
        self.assertIn(m.HOME_SERVER, run.call_args.args[0])
        self.assertIn(m.HOME_STATE_MIRROR, run.call_args.args[0][-1])
        self.assertIn('chmod 600', run.call_args.args[0][-1])

    def test_mirror_state_to_home_failure_raises(self):
        failed = subprocess.CompletedProcess([], 23, '', 'mirror denied')
        with mock.patch.object(m.subprocess, 'run', return_value=failed):
            with self.assertRaisesRegex(RuntimeError, 'mirror denied'):
                m.mirror_state_to_home({'version': 1, 'stations': {}})

    def test_save_state_updates_local_state_then_requires_mirror(self):
        state = {'version': 1, 'stations': {'4': lease(4, 'lease-4')}}
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(m, 'ROOT', Path(td)), \
             mock.patch.object(m, 'STATE_FILE', Path(td) / 'state.json'), \
             mock.patch.object(m, 'SHEET_FILE', Path(td) / 'SIGN-IN-OUT.md'), \
             mock.patch.object(m, 'mirror_state_to_home') as mirror:
            m.save_state(state)
            saved = json.loads((Path(td) / 'state.json').read_text())
            self.assertEqual(saved['stations']['4']['leaseId'], 'lease-4')
            self.assertEqual(saved['stations']['4']['owner'], 'test')
            mirror.assert_called_once_with(state)


class LeaseValidationTests(unittest.TestCase):
    def test_acquire_continues_past_unavailable_station(self):
        state={'version':1,'stations':{}}
        def ready(station):
            if station==1: raise RuntimeError('station 1 SSH not ready')
        with mock.patch.object(m,'locked_state',return_value=(DummyLock(),state)), \
             mock.patch.object(m,'unlock'),mock.patch.object(m,'expire'), \
             mock.patch.object(m,'reconcile_lost_leases'),mock.patch.object(m,'ensure_station',side_effect=ready), \
             mock.patch.object(m,'reset_worker_vault',return_value=True), \
             mock.patch.object(m,'ssh_guest',return_value=subprocess.CompletedProcess([],0,'','')), \
             mock.patch.object(m,'save_state'),mock.patch.object(m,'audit'),mock.patch.object(m,'notify_usage'):
            got=m.acquire('test')
        self.assertEqual(got['station'],2)
        self.assertEqual(state['stations']['1']['status'],'free')

    def test_validate_does_not_reconcile_unrelated_workers(self):
        state = {'version': 1, 'stations': {'2': lease(2, 'other'), '3': lease(3, 'wanted')}}
        with mock.patch.object(m, 'locked_state', return_value=(DummyLock(), state)), \
             mock.patch.object(m, 'unlock'), \
             mock.patch.object(m, 'expire'), \
             mock.patch.object(m, 'reconcile_lost_leases', side_effect=AssertionError('must not run')), \
             mock.patch.object(m, '_remote_exclusive_lease', return_value={'leaseId': 'wanted'}) as probe:
            got = m.validate_lease('wanted')
        self.assertEqual(got['station'], 3)
        probe.assert_called_once_with(3)

    def test_reconcile_transport_timeout_keeps_lease(self):
        state = {'version': 1, 'stations': {'2': lease(2, 'other')}}
        with mock.patch.object(m, '_remote_exclusive_lease', side_effect=m.RemoteLeaseProbeUnavailable('timeout')), \
             mock.patch.object(m, 'save_state') as save, \
             mock.patch.object(m, 'audit'), \
             mock.patch.object(m, 'notify_usage'):
            recovered = m.reconcile_lost_leases(state)
        self.assertEqual(recovered, [])
        self.assertEqual(state['stations']['2']['status'], 'leased')
        save.assert_not_called()

    def test_target_missing_lock_fails_closed_and_marks_lost(self):
        state = {'version': 1, 'stations': {'3': lease(3, 'wanted')}}
        with mock.patch.object(m, 'locked_state', return_value=(DummyLock(), state)), \
             mock.patch.object(m, 'unlock'), \
             mock.patch.object(m, 'expire'), \
             mock.patch.object(m, '_remote_exclusive_lease', return_value=None), \
             mock.patch.object(m, 'save_state'), \
             mock.patch.object(m, 'audit'), \
             mock.patch.object(m, 'notify_usage'):
            with self.assertRaises(KeyError):
                m.validate_lease('wanted')
        self.assertEqual(state['stations']['3']['status'], 'free')

    def test_target_transport_failure_fails_closed_without_freeing(self):
        state = {'version': 1, 'stations': {'3': lease(3, 'wanted')}}
        with mock.patch.object(m, 'locked_state', return_value=(DummyLock(), state)), \
             mock.patch.object(m, 'unlock'), \
             mock.patch.object(m, 'expire'), \
             mock.patch.object(m, '_remote_exclusive_lease', side_effect=m.RemoteLeaseProbeUnavailable('timeout')):
            with self.assertRaises(m.RemoteLeaseProbeUnavailable):
                m.validate_lease('wanted')
        self.assertEqual(state['stations']['3']['status'], 'leased')

    def test_probe_distinguishes_timeout_from_missing_lock(self):
        timeout = subprocess.TimeoutExpired(cmd=['ssh'], timeout=5)
        with mock.patch.object(m, 'ssh_guest', side_effect=timeout):
            with self.assertRaises(m.RemoteLeaseProbeUnavailable):
                m._remote_exclusive_lease(1)
        missing = subprocess.CompletedProcess([], 1, '', '')
        with mock.patch.object(m, 'ssh_guest', return_value=missing):
            self.assertIsNone(m._remote_exclusive_lease(1))

    def test_release_returns_post_release_free_record(self):
        state = {'version': 1, 'stations': {'3': lease(3, 'wanted')}}
        cleared = subprocess.CompletedProcess([], 0, '', '')
        with mock.patch.object(m, 'locked_state', return_value=(DummyLock(), state)), \
             mock.patch.object(m, 'unlock'), \
             mock.patch.object(m, 'reset_worker_vault', return_value=True), \
             mock.patch.object(m, 'ssh_guest', return_value=cleared), \
             mock.patch.object(m, 'save_state') as save, \
             mock.patch.object(m, 'audit'), \
             mock.patch.object(m, 'notify_usage'):
            got = m.release(lease_id='wanted', reason='finished', recycle=False)
        self.assertEqual(got['status'], 'free')
        self.assertIsNone(got['leaseId'])
        self.assertIsNone(got['owner'])
        self.assertIsNone(got['project'])
        self.assertTrue(got['releasedAt'])
        self.assertEqual(got['releaseReason'], 'finished')
        self.assertEqual(state['stations']['3']['status'], 'free')
        save.assert_called_once_with(state)


if __name__ == '__main__':
    unittest.main(verbosity=2)
