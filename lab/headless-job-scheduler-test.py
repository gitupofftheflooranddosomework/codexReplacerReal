#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import tempfile
import threading
import time

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
        assert sched.MAX_LAUNCHERS == 96
        job = sched.submit_job({"owner":"test","project":"p","command":"echo ok","timeout":123})
        assert job["status"] == "queued"
        assert job["execution"] == "ephemeral-headless-kvm"
        assert job["timeout"] == 123
        assert not job.get("worker")
        canceled = sched.cancel_job(job["id"])
        assert canceled["status"] == "canceled", canceled
        assert not canceled.get("worker"), canceled

        # API reconciliation and the background scheduler may call
        # scheduler_iteration concurrently. One queued row must still receive
        # exactly one launcher; launcher_pid=0 is the atomic pre-Popen claim.
        race = sched.submit_job({"owner":"race","project":"p","command":"echo race","timeout":123})
        launches = []
        errors = []
        barrier = threading.Barrier(8)

        def fake_launch(row):
            launches.append(row["id"])
            time.sleep(0.05)
            with sched.DB_LOCK:
                conn = sched.db()
                current = conn.execute("SELECT status,launcher_pid FROM jobs WHERE id=?", (row["id"],)).fetchone()
                assert current["status"] == "queued" and current["launcher_pid"] == 0, dict(current)
                conn.execute("UPDATE jobs SET launcher_pid=? WHERE id=?", (os.getpid(), row["id"]))
                conn.commit(); conn.close()

        sched.launch_row = fake_launch

        def race_iteration():
            try:
                barrier.wait()
                sched.scheduler_iteration()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=race_iteration) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads), 'scheduler race threads did not finish'
        assert not errors, errors
        assert launches == [race["id"]], launches
        conn = sched.db()
        claimed = conn.execute("SELECT launcher_pid FROM jobs WHERE id=?", (race["id"],)).fetchone()[0]
        conn.close()
        assert claimed == os.getpid(), claimed

    runner = load(LAB / "headless-job-runner.py", "headless_job_runner_tested")
    assert runner.resources("io") == (1024, 2048, 1)
    assert runner.resources("cpu") == (1536, 3072, 2)
    assert runner.resources("build") == (3072, 5120, 2)
    assert runner.normalize_cwd("/workspace") == "."
    assert runner.normalize_cwd("/workspace/sub/dir") == "sub/dir"
    try:
        runner.normalize_cwd("/etc")
    except ValueError:
        pass
    else:
        raise AssertionError("absolute cwd escaped /workspace")

    runner_source = (LAB / "headless-job-runner.py").read_text()
    assert '--depth=1' not in runner_source
    assert '--filter=blob:none' not in runner_source
    dispatcher_source = (LAB / "codex-ci-dispatch.py").read_text()
    assert 'git","bundle","create","-","HEAD' in dispatcher_source
    assert 'codex-ci import-git' in dispatcher_source
    worker_source = (LAB / "codex-ci-worker-rpc.py").read_text()
    assert '"import-git":action_import_git' in worker_source

    headless = (LAB / "codex-ci-headless.py").read_text()
    assert 'CODEX_CI_HEADLESS_MAX_ACTIVE", "96"' in headless
    assert 'CODEX_CI_HEADLESS_IP_START", "100"' in headless
    assert 'CODEX_CI_HEADLESS_IP_END", "199"' in headless
    assert 'net-dhcp-leases' in headless
    assert 'MAX_IO_PSI_AVG10' in headless and 'MAX_MEMORY_PSI_AVG10' in headless

    client_path = ROOT / "codex-replacer" / "vm_job_scheduler.py"
    client = client_path.read_text()
    assert 'CODEX_VM_JOB_SCHEDULER_URL' in client
    assert 'os.environ.get("CODEX_LAB_SCHEDULER_URL"' not in client
    assert 'http://192.168.122.1:8767' in client
    assert 'ThreadPoolExecutor(max_workers=min(48, len(jobs)))' in client

    previous_lab = os.environ.get("CODEX_LAB_SCHEDULER_URL")
    previous_job = os.environ.pop("CODEX_VM_JOB_SCHEDULER_URL", None)
    os.environ["CODEX_LAB_SCHEDULER_URL"] = "http://192.168.122.1:8766"
    routed = load(client_path, "vm_job_scheduler_interactive_env_collision")
    assert routed.BASE_URL == "http://192.168.122.1:8767", routed.BASE_URL
    os.environ["CODEX_VM_JOB_SCHEDULER_URL"] = "http://127.0.0.1:9876/"
    overridden = load(client_path, "vm_job_scheduler_explicit_override")
    assert overridden.BASE_URL == "http://127.0.0.1:9876", overridden.BASE_URL
    if previous_lab is None:
        os.environ.pop("CODEX_LAB_SCHEDULER_URL", None)
    else:
        os.environ["CODEX_LAB_SCHEDULER_URL"] = previous_lab
    if previous_job is None:
        os.environ.pop("CODEX_VM_JOB_SCHEDULER_URL", None)
    else:
        os.environ["CODEX_VM_JOB_SCHEDULER_URL"] = previous_job

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
