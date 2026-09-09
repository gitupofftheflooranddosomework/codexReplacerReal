#!/usr/bin/env python3

import importlib.util
import os
import pathlib
import sys
import tempfile


HERE = pathlib.Path(__file__).resolve().parent
SIBLING = (HERE / "vm_job_scheduler.py").resolve()
EXPECTED_URL = "http://192.168.122.1:8767"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        fake = pathlib.Path(tmp) / "vm_job_scheduler.py"
        fake.write_text("BASE_URL = 'legacy://desktop-scheduler'\n")

        sys.path.insert(0, tmp)
        sys.path.insert(1, str(HERE))
        sys.modules.pop("vm_job_scheduler", None)
        sys.modules.pop("server", None)
        os.environ["CODEX_VM_JOB_SCHEDULER_URL"] = EXPECTED_URL

        spec = importlib.util.spec_from_file_location(
            "codex_http_server_import_test",
            HERE / "http-server.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load http-server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        loaded = pathlib.Path(module.server.vm_job_scheduler.__file__).resolve()
        assert loaded == SIBLING, (loaded, SIBLING)
        assert module.server.vm_job_scheduler.BASE_URL == EXPECTED_URL

    print("http_scheduler_import_test=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
