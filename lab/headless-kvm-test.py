#!/usr/bin/env python3
import contextlib
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest

ROOT=pathlib.Path(__file__).resolve().parent

class HeadlessTests(unittest.TestCase):
    def test_dispatcher_has_no_desktop_pool_addressing(self):
        text=(ROOT/'codex-ci-dispatch.py').read_text()
        self.assertNotIn('192.168.122.230',text)
        self.assertNotIn('MAX_STATIONS',text)
        self.assertIn('mode',text)
        self.assertIn('headless',text)
        self.assertIn('codex-ci cancel',text)

    def test_worker_rpc_cancels_process_groups(self):
        text=(ROOT/'codex-ci-worker-rpc.py').read_text()
        self.assertIn('start_new_session=True',text)
        self.assertIn('os.killpg',text)
        self.assertIn('SIGHUP',text)
        self.assertIn('"cancel":action_cancel',text)

    def test_manager_never_names_desktop_domains(self):
        text=(ROOT/'codex-ci-headless.py').read_text()
        self.assertIn('ci-{slug(',text)
        self.assertNotIn('codex-lab-vm-%',text)
        self.assertIn('IP_FIRST',text)
        self.assertIn('MAX_ACTIVE',text)

    def test_dispatcher_does_not_mask_failed_command_with_artifact_pull(self):
        text=(ROOT/'codex-ci-dispatch.py').read_text()
        command=text.index('rc=run_command(')
        failed=text.index('if rc != 0:', command)
        artifacts=text.index('pull_artifacts(', failed)
        self.assertLess(failed, artifacts)
        self.assertIn('status="failed"', text[failed:artifacts])
        self.assertIn('return rc', text[failed:artifacts])

    def test_dispatcher_records_timeout_and_interrupt_exit_codes(self):
        text=(ROOT/'codex-ci-dispatch.py').read_text()
        timeout=text.index('except subprocess.TimeoutExpired:', text.index('def main():'))
        interrupt=text.index('except Interrupted:', timeout)
        finalizer=text.index('finally:', interrupt)
        self.assertIn('rc=124', text[timeout:interrupt])
        self.assertIn('return rc', text[timeout:interrupt])
        self.assertIn('rc=130', text[interrupt:finalizer])
        self.assertIn('return rc', text[interrupt:finalizer])
        self.assertIn('"--exit-code",str(rc)', text[finalizer:])

    def test_destroy_timeout_continues_cleanup_and_unknown_state_preserves_disk(self):
        spec=importlib.util.spec_from_file_location('headless_cleanup_test', ROOT/'codex-ci-headless.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Result:
            def __init__(self, returncode=0, stdout='', stderr=''):
                self.returncode=returncode
                self.stdout=stdout
                self.stderr=stderr

        @contextlib.contextmanager
        def unlocked():
            yield

        module.provision_locked=unlocked
        calls=[]
        state={'destroy': 0, 'dominfo': 0}

        def converging_virsh(*args, **_kwargs):
            calls.append(args)
            if args[0] == 'destroy':
                state['destroy'] += 1
                if state['destroy'] == 1:
                    raise subprocess.TimeoutExpired(['virsh', 'destroy'], 12)
                return Result()
            if args[0] == 'undefine':
                return Result()
            if args[0] == 'dominfo':
                state['dominfo'] += 1
                if state['dominfo'] == 1:
                    return Result()
                return Result(1, stderr='error: Domain not found')
            raise AssertionError(args)

        module.virsh=converging_virsh
        module.del_dhcp=lambda rec: calls.append(('del_dhcp', rec['name']))
        with tempfile.TemporaryDirectory() as tempdir:
            root=pathlib.Path(tempdir)/'ci-test'
            root.mkdir()
            (root/'disk.qcow2').write_text('x')
            warnings=module.destroy_resources({'name':'ci-test','storage_root':tempdir})
            self.assertFalse(root.exists())
            self.assertTrue(any('timed out' in warning for warning in warnings))
            self.assertIn(('undefine','ci-test'), calls)
            self.assertIn(('del_dhcp','ci-test'), calls)

        def unknown_virsh(*args, **_kwargs):
            if args[0] == 'dominfo':
                raise subprocess.TimeoutExpired(['virsh', 'dominfo'], 5)
            return Result()

        module.virsh=unknown_virsh
        module.del_dhcp=lambda _rec: None
        with tempfile.TemporaryDirectory() as tempdir:
            root=pathlib.Path(tempdir)/'ci-unknown'
            root.mkdir()
            (root/'disk.qcow2').write_text('x')
            with self.assertRaisesRegex(RuntimeError, 'unable to prove domain absent'):
                module.destroy_resources({'name':'ci-unknown','storage_root':tempdir})
            self.assertTrue(root.exists())

    def test_units_are_bounded_cleanup_and_balloon_loops(self):
        self.assertIn('OnUnitActiveSec=1min',(ROOT/'codex-ci-headless-gc.timer').read_text())
        self.assertIn('OnUnitActiveSec=15s',(ROOT/'codex-desktop-balloon.timer').read_text())
        balloon=(ROOT/'codex-desktop-balloon.service').read_text()
        self.assertIn('CODEX_LAB_SCHEDULER_URL=http://192.168.122.1:8766',balloon)
        self.assertIn('rebalance-desktops',balloon)

    def test_build_marks_base_headless(self):
        text=(ROOT/'build-ci-headless-base.sh').read_text()
        self.assertIn('headless-v1',text)
        self.assertIn('codex-worker-desktop.service',text)
        self.assertIn('qemu-img convert',text)

if __name__=='__main__': unittest.main()
