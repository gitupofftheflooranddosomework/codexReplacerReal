#!/usr/bin/env python3
import base64
import importlib.util
import os
import pathlib
import tempfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
TARGET = ROOT / "headless-job-runner.py"

spec = importlib.util.spec_from_file_location("runner_auth_test", TARGET)
mod = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(mod)


def main():
    token = "ghp_test_controller_only_token"
    result = mock.Mock(returncode=0, stdout=token + "\n", stderr="")
    clean = dict(os.environ)
    clean.pop("CODEX_VM_JOB_GITHUB_TOKEN_FILE", None)
    with mock.patch.dict(os.environ, clean, clear=True), mock.patch.object(mod.subprocess, "run", return_value=result) as run:
        env = mod.controller_git_env("https://github.com/dotmoosehosting/DotMoose.git")
    expected = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {expected}"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert token not in "https://github.com/dotmoosehosting/DotMoose.git"
    run.assert_called_once_with(
        ["gh", "auth", "token", "--hostname", "github.com"],
        text=True, stdout=mod.subprocess.PIPE, stderr=mod.subprocess.PIPE,
        check=False, timeout=10,
    )

    with tempfile.TemporaryDirectory() as td:
        token_path = pathlib.Path(td) / "token"
        token_path.write_text(token + "\n")
        with mock.patch.dict(os.environ, {"CODEX_VM_JOB_GITHUB_TOKEN_FILE": str(token_path)}, clear=True), \
             mock.patch.object(mod.subprocess, "run") as run:
            env = mod.controller_git_env("https://github.com/dotmoosehosting/DotMoose.git")
        assert token not in str(run.call_args_list)
        assert env["GIT_CONFIG_VALUE_0"].endswith(expected)

    try:
        mod.controller_git_env("https://secret@github.com/dotmoosehosting/DotMoose.git")
    except RuntimeError as exc:
        assert "embedded credentials" in str(exc)
    else:
        raise AssertionError("embedded GitHub credentials were accepted")

    with mock.patch.dict(os.environ, {}, clear=True):
        env = mod.controller_git_env("https://example.invalid/public/repo.git")
    assert "GIT_CONFIG_VALUE_0" not in env
    print("headless_private_repo_auth_test=ok")


if __name__ == "__main__":
    main()
