#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
LAB = ROOT / "lab"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def main():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        os.environ["CODEX_VM_JOB_ROOT"] = str(root / "scheduler")
        os.environ["CODEX_VM_JOB_DB"] = str(root / "scheduler" / "jobs.sqlite3")
        os.environ["CODEX_VM_JOB_WORK_ROOT"] = str(root / "scheduler" / "jobs")
        os.environ["CODEX_VM_JOB_HOST"] = "127.0.0.1"
        sched = load(LAB / "headless-job-scheduler.py", "headless_job_scheduler_tested")
        assert sched.MAX_LAUNCHERS == 48
        job = sched.submit_job({"owner":"test","project":"p","command":"echo ok","timeout":123})
        assert job["status"] == "queued"
        assert job["execution"] == "ephemeral-headless-kvm"
        assert job["timeout"] == 123
        assert not job.get("worker")

    runner = load(LAB / "headless-job-runner.py", "headless_job_runner_tested")
    assert runner.normalize_cwd("/workspace") == "."
    assert runner.normalize_cwd("/workspace/sub/dir") == "sub/dir"
    try:
        runner.normalize_cwd("/etc")
    except ValueError:
        pass
    else:
        raise AssertionError("absolute cwd escaped /workspace")

    headless = (LAB / "codex-ci-headless.py").read_text()
    assert 'CODEX_CI_HEADLESS_MAX_ACTIVE", "48"' in headless
    assert 'CODEX_CI_HEADLESS_IP_START", "100"' in headless
    assert 'CODEX_CI_HEADLESS_IP_END", "199"' in headless
    assert 'net-dhcp-leases' in headless
    assert 'MAX_IO_PSI_AVG10' in headless and 'MAX_MEMORY_PSI_AVG10' in headless

    client = (ROOT / "codex-replacer" / "vm_job_scheduler.py").read_text()
    assert 'http://192.168.122.1:8767' in client
    assert 'ThreadPoolExecutor(max_workers=min(48, len(jobs)))' in client

    server = (ROOT / "codex-replacer" / "server.py").read_text()
    assert 'SERVER_VERSION = "2.5.0"' in server
    assert 'elastic headless KVM pool' in server
    assert 'all six shared workers immediately' not in server

    readme = (ROOT / "README.md").read_text()
    assert 'elastic disposable headless-KVM pool' in readme
    assert 'up to all six' not in readme
    print("headless_job_scheduler_test=ok")


if __name__ == "__main__":
    main()
