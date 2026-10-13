#!/usr/bin/env python3
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import server
import slackcontroller_bridge as bridge

bridge.install(server)

UUID1 = '123e4567-e89b-42d3-a456-426614174000'
UUID2 = '123e4567-e89b-42d3-a456-426614174001'

class SlackControllerBridgeTest(unittest.TestCase):
    def response(self, body, status=200):
        return {
            'station': 3,
            'leaseId': 'lease-3',
            'owner': 'RankMoose',
            'exitCode': 0,
            'stdout': json.dumps({'status': status, 'body': body}),
            'stderr': '',
            'truncated': False,
        }

    def test_tools_are_first_class_broker_tools(self):
        self.assertTrue(bridge.TOOL_NAMES.issubset(server.DIRECT_TOOLS))
        self.assertTrue(bridge.TOOL_NAMES.issubset(server.BROKER_DIRECT_TOOL_NAMES))
        listed = {item['name'] for item in server.broker_direct_tools()}
        self.assertTrue(bridge.TOOL_NAMES.issubset(listed))
        self.assertIn('SlackController chat coordination rule', server.CONVERSATION_CONTINUITY_INSTRUCTIONS)

    def test_install_is_idempotent(self):
        before = {name: id(server.DIRECT_TOOLS[name]) for name in bridge.TOOL_NAMES}
        bridge.install(server)
        after = {name: id(server.DIRECT_TOOLS[name]) for name in bridge.TOOL_NAMES}
        self.assertEqual(before, after)

    def test_identity_uses_fixed_worker_path_and_lease(self):
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'handle':'rankmoose','repo':'x','station':3})) as execute:
            result = server.DIRECT_TOOLS['slackcontroller_identity']['handler']({'leaseId':'lease-3'})
        self.assertEqual(result['structuredContent']['handle'], 'rankmoose')
        kwargs = execute.call_args.kwargs
        self.assertEqual(kwargs['lease_id'], 'lease-3')
        self.assertEqual(kwargs['env']['SC_PATH'], '/worker/v1/identity')
        self.assertEqual(kwargs['env']['SC_BASE'], 'http://10.0.0.181:8788')
        self.assertNotIn('handle', kwargs['env'])

    def test_inbox_binds_limit_only(self):
        handler = server.DIRECT_TOOLS['slackcontroller_inbox']['handler']
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'handle':'rankmoose','messages':[]})) as execute:
            handler({'leaseId':'lease-3','limit':7})
        self.assertEqual(execute.call_args.kwargs['env']['SC_PATH'], '/worker/v1/messages?limit=7')
        with self.assertRaises(ValueError): handler({'leaseId':'lease-3','limit':51})

    def test_reply_schema_cannot_supply_identity_or_routing(self):
        descriptor = server.DIRECT_TOOLS['slackcontroller_reply']['descriptor']
        self.assertEqual(set(descriptor['inputSchema']['properties']), {'leaseId','messageId','text'})
        handler = server.DIRECT_TOOLS['slackcontroller_reply']['handler']
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'ok':True,'handle':'rankmoose','messageId':UUID1})) as execute:
            handler({'leaseId':'lease-3','messageId':UUID1,'text':'done'})
        env = execute.call_args.kwargs['env']
        self.assertEqual(env['SC_PATH'], f'/worker/v1/messages/{UUID1}/reply')
        self.assertEqual(json.loads(env['SC_BODY']), {'text':'done'})
        with self.assertRaises(ValueError): handler({'leaseId':'lease-3','messageId':'bad','text':'done'})
        with self.assertRaises(ValueError): handler({'leaseId':'lease-3','messageId':UUID1,'text':'x'*4001})

    def test_ack_is_bounded_and_only_ids(self):
        descriptor = server.DIRECT_TOOLS['slackcontroller_ack']['descriptor']
        self.assertEqual(set(descriptor['inputSchema']['properties']), {'leaseId','messageIds'})
        handler = server.DIRECT_TOOLS['slackcontroller_ack']['handler']
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'acknowledgedIds':[UUID1,UUID2]})) as execute:
            handler({'leaseId':'lease-3','messageIds':[UUID1,UUID2]})
        self.assertEqual(json.loads(execute.call_args.kwargs['env']['SC_BODY']), {'ids':[UUID1,UUID2]})
        with self.assertRaises(ValueError): handler({'leaseId':'lease-3','messageIds':[]})

    def test_heartbeat_is_strict(self):
        handler = server.DIRECT_TOOLS['slackcontroller_heartbeat']['handler']
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'ok':True,'handle':'rankmoose','status':'active'})):
            result = handler({'leaseId':'lease-3','status':'active'})
        self.assertEqual(result['structuredContent']['status'], 'active')
        with self.assertRaises(ValueError): handler({'leaseId':'lease-3','status':'busy'})

    def test_activity_request_uses_lease_bound_worker_path_and_short_timeouts(self):
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'ok':True,'handle':'rankmoose','phase':'working','ttl_ms':120000})) as execute:
            self.assertTrue(bridge._activity_once(server, 'lease-3', 'working'))
        kwargs = execute.call_args.kwargs
        self.assertEqual(kwargs['lease_id'], 'lease-3')
        self.assertEqual(kwargs['timeout'], bridge.ACTIVITY_EXEC_TIMEOUT_SECONDS)
        self.assertEqual(kwargs['env']['SC_PATH'], '/worker/v1/activity')
        self.assertEqual(float(kwargs['env']['SC_TIMEOUT']), bridge.ACTIVITY_HTTP_TIMEOUT_SECONDS)
        self.assertEqual(json.loads(kwargs['env']['SC_BODY']), {'phase':'working','ttl_ms':120000})

    def test_activity_failure_is_best_effort(self):
        with patch.object(server.vm_lab_manager, 'execute', side_effect=RuntimeError('hook down')):
            self.assertFalse(bridge._activity_once(server, 'lease-3', 'working'))

    def test_activity_pulse_refreshes_long_work_and_finishes_idle(self):
        calls = []
        with patch.object(bridge, '_activity_once', side_effect=lambda _server, _lease, phase: calls.append(phase) or True), \
             patch.object(bridge, 'ACTIVITY_REFRESH_SECONDS', 0.01):
            pulse = bridge.ActivityPulse(server, 'lease-3').start()
            time.sleep(0.035)
            pulse.stop(wait=True)
        self.assertGreaterEqual(calls.count('working'), 2)
        self.assertEqual(calls[-1], 'idle')

    def test_vm_lab_exec_and_browser_calls_are_wrapped_without_changing_primary_result(self):
        fake_pulse = unittest.mock.MagicMock()
        with patch.object(bridge, 'start_activity_pulse', return_value=fake_pulse) as start, \
             patch.object(server.vm_lab_manager, 'execute', return_value={'exitCode':0,'stdout':'ok','stderr':'','truncated':False}):
            result = server.DIRECT_TOOLS['vm_lab_exec']['handler']({'leaseId':'lease-3','command':'true'})
        self.assertEqual(result['structuredContent']['exitCode'], 0)
        start.assert_called_with(server, 'lease-3', 'working')
        fake_pulse.stop.assert_called_once_with()

        fake_pulse = unittest.mock.MagicMock()
        with patch.object(bridge, 'start_activity_pulse', return_value=fake_pulse) as start, \
             patch.object(server.vm_lab_manager, 'validate_lease', return_value={'leaseId':'lease-3'}), \
             patch.object(server.KVM_BROWSER_POOL.clients[3], 'call', return_value={'content':[]}):
            result = server.KVM_BROWSER_POOL.call('vm_browser_snapshot', {'station':3,'leaseId':'lease-3'})
        self.assertEqual(result, {'content':[]})
        start.assert_called_with(server, 'lease-3', 'working')
        fake_pulse.stop.assert_called_once_with()

    def test_vm_lab_release_publishes_idle_before_release_and_never_blocks_release_on_activity_failure(self):
        events = []
        def activity(*_args):
            events.append('idle')
            return False
        with patch.object(bridge, '_activity_once', side_effect=activity), \
             patch.object(server.vm_lab_manager, 'release', side_effect=lambda **_kwargs: events.append('release') or {'status':'released'}):
            result = server.DIRECT_TOOLS['vm_lab_release']['handler']({'leaseId':'lease-3'})
        self.assertEqual(result['structuredContent']['status'], 'released')
        self.assertEqual(events, ['idle', 'release'])

    def test_expired_or_invalid_lease_failure_is_not_bypassed(self):
        handler = server.DIRECT_TOOLS['slackcontroller_identity']['handler']
        with patch.object(server.vm_lab_manager, 'execute', side_effect=KeyError('Active full-VM lab lease not found')):
            with self.assertRaises(KeyError): handler({'leaseId':'expired-lease'})

    def test_worker_api_error_is_bounded(self):
        handler = server.DIRECT_TOOLS['slackcontroller_identity']['handler']
        with patch.object(server.vm_lab_manager, 'execute', return_value=self.response({'error':'worker_identity_denied'},403)):
            with self.assertRaisesRegex(RuntimeError, r'403.*worker_identity_denied'):
                handler({'leaseId':'lease-3'})

    def test_callers_cannot_override_base_url(self):
        for name in bridge.TOOL_NAMES:
            props = server.DIRECT_TOOLS[name]['descriptor']['inputSchema']['properties']
            self.assertNotIn('url', props)
            self.assertNotIn('baseUrl', props)
            self.assertNotIn('headers', props)
            self.assertNotIn('handle', props)
            self.assertNotIn('channelId', props)
            self.assertNotIn('threadTs', props)

if __name__ == '__main__': unittest.main(verbosity=2)
