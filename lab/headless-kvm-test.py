#!/usr/bin/env python3
import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT=pathlib.Path(__file__).resolve().parent

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

class HeadlessTests(unittest.TestCase):
    def test_dispatcher_has_no_desktop_pool_addressing(self):
        text=(ROOT/'codex-ci-dispatch.py').read_text()
        self.assertNotIn('192.168.122.230',text); self.assertNotIn('MAX_STATIONS',text); self.assertIn('mode')
        self.assertIn('headless',text); self.assertIn('codex-ci cancel',text)

    def test_worker_rpc_cancels_process_groups(self):
        text=(ROOT/'codex-ci-worker-rpc.py').read_text()
        self.assertIn('start_new_session=True',text); self.assertIn('os.killpg',text); self.assertIn('SIGHUP',text); self.assertIn('"cancel":action_cancel',text)

    def test_manager_never_names_desktop_domains(self):
        text=(ROOT/'codex-ci-headless.py').read_text()
        self.assertIn('ci-{slug(',text); self.assertNotIn('codex-lab-vm-%',text)
        self.assertIn('IP_FIRST',text); self.assertIn('MAX_ACTIVE',text)

    def test_units_are_bounded_cleanup_and_balloon_loops(self):
        self.assertIn('OnUnitActiveSec=1min',(ROOT/'codex-ci-headless-gc.timer').read_text())
        self.assertIn('OnUnitActiveSec=15s',(ROOT/'codex-desktop-balloon.timer').read_text())

    def test_build_marks_base_headless(self):
        text=(ROOT/'build-ci-headless-base.sh').read_text()
        self.assertIn('headless-v1',text); self.assertIn('codex-worker-desktop.service',text); self.assertIn('qemu-img convert',text)

if __name__=='__main__': unittest.main()
