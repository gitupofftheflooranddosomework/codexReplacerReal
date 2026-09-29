#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        os.environ["CODEX_VM_JOB_ROOT"] = str(root)
        os.environ["CODEX_VM_JOB_DB"] = str(root / "jobs.sqlite3")
        os.environ["CODEX_VM_JOB_WORK_ROOT"] = str(root / "jobs")
        os.environ["CODEX_VM_JOB_DRAIN_FILE"] = str(root / "drain")
        os.environ["CODEX_VM_JOB_MANAGER_PROXY"] = "1"
        sched = load(ROOT / "headless-job-scheduler.py", "headless_job_scheduler_drain_test")

        assert sched.draining() is False
        sched.DRAIN_FILE.write_text("maintenance\n")
        assert sched.draining() is True

        try:
            sched.submit_job({"owner":"test","project":"p","command":"echo no"})
        except RuntimeError as exc:
            assert "draining" in str(exc)
        else:
            raise AssertionError("drain accepted a new /api/jobs submission")

        called = []
        original = sched.subprocess.run
        sched.subprocess.run = lambda *args, **kwargs: called.append((args,kwargs))
        try:
            try:
                sched.manager_proxy({"args":["reserve","owner","project"]})
            except RuntimeError as exc:
                assert "draining" in str(exc)
            else:
                raise AssertionError("drain accepted a manager reserve")
            assert not called, "reserve reached manager while drain was active"
        finally:
            sched.subprocess.run = original

        sched.DRAIN_FILE.unlink()
        assert sched.draining() is False

        class Result:
            returncode = 0
            stdout = '{"ok":true}'
            stderr = ""

        called = []
        sched.subprocess.run = lambda *args, **kwargs: (called.append((args,kwargs)) or Result())
        try:
            result = sched.manager_proxy({"args":["provision","abc"]})
            assert result["ok"] is True
            result = sched.manager_proxy({"args":["finish","abc"]})
            assert result["ok"] is True
            assert len(called) == 2
        finally:
            sched.subprocess.run = original

        sched.manager_state = lambda: {"active":[]}
        sched.interactive_state = lambda: {"workers":[]}
        state = sched.workers_state()
        assert state["draining"] is False
        assert state["drainFile"] == str(sched.DRAIN_FILE)

    print("headless_runtime_drain_test=PASS")


if __name__ == "__main__":
    main()
