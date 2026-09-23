#!/usr/bin/env python3
import importlib.util
import json
import os
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


LAB = pathlib.Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        assert self.headers.get("Authorization") == "Bearer local-secret"
        self.calls.append((self.path, payload))
        response = {"generation": 7} if self.path == "/v1/admit" else {"ok": True}
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    with tempfile.TemporaryDirectory() as td:
        token = pathlib.Path(td, "admission.token")
        token.write_text("local-secret\n")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            os.environ["CODEX_VM_JOB_ADMISSION_URL"] = f"http://127.0.0.1:{server.server_port}"
            os.environ["CODEX_VM_JOB_ADMISSION_TOKEN_FILE"] = str(token)
            admission = load(LAB / "rollout_admission.py", "rollout_admission_tested")
            generation = admission.admit("task-1", "project-a", "test")
            assert generation == 7
            admission.authorize_effect("task-1", generation, "project-a", "test")
            admission.finish("task-1")
            assert [call[0] for call in Handler.calls] == [
                "/v1/admit",
                "/v1/authorize-effect",
                "/v1/finish",
            ]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            os.environ.pop("CODEX_VM_JOB_ADMISSION_URL", None)
            os.environ.pop("CODEX_VM_JOB_ADMISSION_TOKEN_FILE", None)

    admission.URL = "https://example.invalid"
    admission.TOKEN_FILE = "missing"
    try:
        admission.admit("task-2", "project-a", "test")
    except admission.AdmissionError as exc:
        assert "loopback" in str(exc)
    else:
        raise AssertionError("non-loopback admission endpoint was accepted")

    admission.URL = "http://127.0.0.1:8791"
    admission.TOKEN_FILE = ""
    try:
        admission.admit("task-3", "project-a", "test")
    except admission.AdmissionError as exc:
        assert "configured together" in str(exc)
    else:
        raise AssertionError("partial admission configuration was accepted")

    scheduler = (LAB / "headless-job-scheduler.py").read_text()
    runner = (LAB / "headless-job-runner.py").read_text()
    launch = scheduler[scheduler.index("def launch_row"):scheduler.index("def reconcile_one")]
    run = runner[runner.index("def main"):]
    assert launch.index("rollout_admission.authorize_effect") < launch.index("subprocess.Popen")
    assert run.index("rollout_admission.authorize_effect") < run.index("child = subprocess.Popen")
    assert "admission_generation INTEGER" in scheduler
    print("headless_rollout_admission_test=ok")


if __name__ == "__main__":
    main()
