#!/usr/bin/env python3
import importlib.util
import io
import json
import subprocess
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





class Response:
    def __init__(self, payload, status=200):
        self.body=json.dumps(payload).encode()
        self.status=status
    def __enter__(self):
        return self
    def __exit__(self,*_a):
        return False
    def read(self):
        return self.body


class ProviderEndpointTests(unittest.TestCase):
    def test_libvirt_keeps_legacy_address_formula(self):
        with mock.patch.object(m, 'WORKER_PROVIDER', 'libvirt'):
            self.assertEqual(m.worker_ip_for(3), '192.168.122.232')

    def test_external_provider_uses_scheduler_resolved_address(self):
        calls=[]
        def open_url(request, timeout):
            calls.append((request, timeout))
            return Response({'station':3,'name':'swf-station-03','ip':'10.77.20.33','state':'running','provider':'serverworkerfabric'})
        with mock.patch.object(m, 'WORKER_PROVIDER', 'serverworkerfabric'), \
             mock.patch.object(m.urllib.request, 'urlopen', side_effect=open_url):
            self.assertEqual(m.worker_ip_for(3), '10.77.20.33')
        self.assertEqual(calls[0][0].full_url, m.SCHEDULER_URL + '/api/workers/3')
        self.assertEqual(calls[0][1], 3)

    def test_external_provider_identity_mismatch_fails_closed(self):
        with mock.patch.object(m, 'WORKER_PROVIDER', 'serverworkerfabric'), \
             mock.patch.object(m.urllib.request, 'urlopen', return_value=Response({'station':4,'ip':'10.0.0.4'})):
            with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                m.worker_ip_for(3)

    def test_external_provider_missing_address_fails_closed(self):
        with mock.patch.object(m, 'WORKER_PROVIDER', 'serverworkerfabric'), \
             mock.patch.object(m.urllib.request, 'urlopen', return_value=Response({'station':3,'ip':None})):
            with self.assertRaisesRegex(RuntimeError, 'no provider-resolved address'):
                m.worker_ip_for(3)

    def test_ssh_guest_uses_provider_resolver(self):
        completed=subprocess.CompletedProcess([],0,'ok','')
        with mock.patch.object(m, 'worker_ip_for', return_value='10.77.20.33'), \
             mock.patch.object(m.subprocess, 'run', return_value=completed) as run:
            got=m.ssh_guest(3,'true',5)
        self.assertEqual(got.returncode,0)
        args=run.call_args.args[0]
        self.assertIn('mark@10.77.20.33',args)


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
