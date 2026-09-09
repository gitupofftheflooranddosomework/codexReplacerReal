#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

from vault_manager import VaultError, VaultManager


ORG = "3e3b3667-0137-43d6-b297-bb7abc6fb202"


def main():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        session = root / "session"
        session.write_text("session-key\n", encoding="utf-8")
        session.chmod(0o600)
        environment = {
            "CODEX_VAULT_PROJECT": "DotMoose",
            "CODEX_VAULT_ORGANIZATION": "DotMoose",
            "CODEX_VAULT_ORGANIZATION_ID": ORG,
            "CODEX_VAULT_BW": "bw",
            "CODEX_VAULT_BW_CONFIG_DIR": str(root / "config"),
            "CODEX_VAULT_SESSION_FILE": str(session),
            "CODEX_VAULT_SYNC_SECONDS": "60",
        }
        items = [
            {"id": "login-1", "organizationId": ORG, "type": 1, "name": "Cloudflare", "login": {"username": "bot@example.com", "password": "secret", "totp": "seed"}},
            {"id": "note-1", "organizationId": ORG, "type": 2, "name": "Private note", "secureNote": {}},
            {"id": "other-1", "organizationId": "outside", "type": 1, "name": "Outside", "login": {"username": "x", "password": "y"}},
        ]

        def fake_run(command, **kwargs):
            assert kwargs["env"]["BW_SESSION"] == "session-key"
            assert kwargs["env"]["BITWARDENCLI_APPDATA_DIR"] == str(root / "config")
            assert "session-key" not in command
            arguments = command[1:]
            output = ""
            if arguments == ["sync"]:
                output = "{}\n"
            elif arguments[:2] == ["list", "items"]:
                output = json.dumps(items)
            elif arguments == ["get", "item", "login-1"]:
                output = json.dumps(items[0])
            elif arguments == ["get", "totp", "login-1"]:
                output = "123456\n"
            else:
                return subprocess.CompletedProcess(command, 2, "", "unexpected arguments")
            return subprocess.CompletedProcess(command, 0, output, "")

        with mock.patch("vault_manager.shutil.which", return_value="/usr/bin/bw"), mock.patch(
            "vault_manager.subprocess.run", side_effect=fake_run,
        ):
            vault = VaultManager(environment, clock=lambda: 100.0)
            listing = vault.list_logins()
            assert listing == [{"name": "Cloudflare", "username": True, "password": True, "totp": True}]
            secret = vault.get_login("Cloudflare", ["username", "password", "totp"])
            assert secret["values"] == {
                "username": "bot@example.com", "password": "secret", "totp": "123456",
            }
            try:
                vault.get_login("Outside", ["password"])
            except VaultError as error:
                assert "No DotMoose login" in str(error)
            else:
                raise AssertionError("outside-organization item was returned")
            if os.name != "nt":
                session.chmod(0o644)
                try:
                    VaultManager(environment).list_logins()
                except VaultError as error:
                    assert "permissions" in str(error)
                else:
                    raise AssertionError("insecure session permissions were accepted")
    print(json.dumps({"ok": True, "scope": "DotMoose login items only"}))


if __name__ == "__main__":
    main()
