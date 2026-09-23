#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import tempfile


LAB = pathlib.Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def main():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        os.environ["CODEX_VM_JOB_ROOT"] = str(root)
        os.environ["CODEX_VM_JOB_DB"] = str(root / "jobs.sqlite3")
        os.environ["CODEX_VM_JOB_WORK_ROOT"] = str(root / "jobs")
        os.environ["CODEX_VM_JOB_SYNTHETIC_CANARY"] = "1"
        scheduler = load(LAB / "headless-job-scheduler.py", "synthetic_canary_scheduler")
        first = scheduler.synthetic_canary("probe-001")
        second = scheduler.synthetic_canary("probe-001")
        assert first["ok"] and first["created"]
        assert second["ok"] and not second["created"]
        assert first["createdAt"] == second["createdAt"]
        conn = scheduler.db()
        count = conn.execute("SELECT COUNT(*) FROM synthetic_canary_receipts").fetchone()[0]
        jobs = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        conn.close()
        assert count == 1
        assert jobs == 0
        try:
            scheduler.synthetic_canary("../../escape")
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe operation id was accepted")
        os.environ.pop("CODEX_VM_JOB_SYNTHETIC_CANARY", None)
    print("headless_synthetic_canary_test=ok")


if __name__ == "__main__":
    main()
