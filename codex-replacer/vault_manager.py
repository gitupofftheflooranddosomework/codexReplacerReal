#!/usr/bin/env python3
"""Read-only, organization-scoped access to the DotMoose Bitwarden vault."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path


class VaultError(RuntimeError):
    """A safe, user-facing vault failure."""


class VaultManager:
    LOGIN_TYPE = 1
    SECURE_NOTE_TYPE = 2
    TYPE_NAMES = {LOGIN_TYPE: "login", SECURE_NOTE_TYPE: "secure_note"}
    ALLOWED_FIELDS = frozenset({"username", "password", "totp", "notes"})

    def __init__(self, environ=None, clock=None):
        values = os.environ if environ is None else environ
        self.project = values.get("CODEX_VAULT_PROJECT", "DotMoose").strip()
        self.organization = values.get("CODEX_VAULT_ORGANIZATION", "DotMoose").strip()
        self.organization_id = values.get("CODEX_VAULT_ORGANIZATION_ID", "").strip()
        self.executable = values.get("CODEX_VAULT_BW", "bw").strip()
        self.config_dir = Path(values.get(
            "CODEX_VAULT_BW_CONFIG_DIR",
            "~/.config/codex-replacer/bitwarden-dotmoose",
        )).expanduser()
        self.session_file = Path(values.get(
            "CODEX_VAULT_SESSION_FILE",
            "~/.config/codex-replacer/dotmoose-vault.session",
        )).expanduser()
        self.sync_seconds = max(0, int(values.get("CODEX_VAULT_SYNC_SECONDS", "60")))
        self._clock = time.monotonic if clock is None else clock
        self._last_sync = 0.0
        self._lock = threading.RLock()

    def _require_configuration(self):
        if not self.organization_id:
            raise VaultError("DotMoose vault access is not configured on the broker.")
        if not shutil.which(self.executable):
            raise VaultError("The Bitwarden CLI is not installed on the broker.")
        if not self.session_file.is_file():
            raise VaultError(
                "The DotMoose vault is not enrolled on the broker. "
                "Mark must complete the one-time Bitwarden enrollment."
            )
        if os.name != "nt":
            mode = stat.S_IMODE(self.session_file.stat().st_mode)
            if mode & 0o077:
                raise VaultError("The DotMoose vault session file permissions are too broad; expected mode 0600.")

    def _session(self):
        self._require_configuration()
        try:
            value = self.session_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise VaultError("The DotMoose vault session could not be read.") from error
        if not value:
            raise VaultError("The DotMoose vault session is empty; enrollment must be completed again.")
        return value

    def _run(self, arguments, timeout=30):
        environment = os.environ.copy()
        environment["BITWARDENCLI_APPDATA_DIR"] = str(self.config_dir)
        environment["BW_SESSION"] = self._session()
        environment["BW_NOINTERACTION"] = "true"
        try:
            completed = subprocess.run(
                [self.executable, *arguments],
                env=environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise VaultError("The DotMoose vault request timed out.") from error
        if completed.returncode:
            detail = (completed.stderr or completed.stdout).strip().splitlines()
            hint = detail[-1] if detail else "Bitwarden returned an error."
            if any(word in hint.lower() for word in ("locked", "session", "logged in", "unauthenticated")):
                raise VaultError("The DotMoose vault session has expired; enrollment must be completed again.")
            raise VaultError("The DotMoose vault request failed. Check broker enrollment and Vaultwarden availability.")
        return completed.stdout

    def _sync_if_due(self):
        now = self._clock()
        if self.sync_seconds and self._last_sync and now - self._last_sync < self.sync_seconds:
            return
        self._run(["sync"], timeout=45)
        self._last_sync = now

    def _vault_items(self):
        self._sync_if_due()
        try:
            items = json.loads(self._run([
                "list", "items", "--organizationid", self.organization_id,
            ]))
        except json.JSONDecodeError as error:
            raise VaultError("Bitwarden returned an invalid vault response.") from error
        if not isinstance(items, list):
            raise VaultError("Bitwarden returned an unexpected vault response.")
        return [
            item for item in items
            if item.get("type") in self.TYPE_NAMES
            and str(item.get("organizationId", "")).lower() == self.organization_id.lower()
            and not item.get("deletedDate")
        ]

    def list_items(self):
        with self._lock:
            result = []
            for item in self._vault_items():
                login = item.get("login") or {}
                result.append({
                    "name": str(item.get("name") or ""),
                    "type": self.TYPE_NAMES[item.get("type")],
                    "username": bool(login.get("username")),
                    "password": bool(login.get("password")),
                    "totp": bool(login.get("totp")),
                    "notes": bool(item.get("notes")),
                })
            return sorted(result, key=lambda item: item["name"].casefold())

    def get_item(self, name, fields):
        requested_name = str(name or "").strip()
        if not requested_name:
            raise VaultError("An exact DotMoose vault item name is required.")
        requested_fields = list(fields or ["username", "password"])
        if not requested_fields or len(requested_fields) != len(set(requested_fields)):
            raise VaultError("Request one or more unique vault fields.")
        invalid = sorted(set(requested_fields) - self.ALLOWED_FIELDS)
        if invalid:
            raise VaultError(f"Unsupported DotMoose vault fields: {', '.join(invalid)}")

        with self._lock:
            matches = [item for item in self._vault_items() if item.get("name") == requested_name]
            if not matches:
                raise VaultError(f"No DotMoose vault item exactly matches {requested_name!r}.")
            if len(matches) != 1:
                raise VaultError(f"More than one DotMoose vault item is named {requested_name!r}.")
            item_id = str(matches[0].get("id") or "")
            try:
                item = json.loads(self._run(["get", "item", item_id]))
            except json.JSONDecodeError as error:
                raise VaultError("Bitwarden returned an invalid item response.") from error
            if (
                item.get("type") not in self.TYPE_NAMES
                or str(item.get("organizationId", "")).lower() != self.organization_id.lower()
            ):
                raise VaultError("Bitwarden returned an item outside the DotMoose vault scope.")

            login = item.get("login") or {}
            values = {}
            for field in requested_fields:
                if field == "notes":
                    value = item.get("notes")
                    if value in (None, ""):
                        raise VaultError(f"DotMoose vault item {requested_name!r} has no notes.")
                    values[field] = str(value)
                elif field == "totp":
                    if not login.get("totp"):
                        raise VaultError(f"DotMoose vault item {requested_name!r} has no TOTP seed.")
                    values[field] = self._run(["get", "totp", item_id]).strip()
                else:
                    value = login.get(field)
                    if value in (None, ""):
                        raise VaultError(f"DotMoose vault item {requested_name!r} has no {field}.")
                    values[field] = str(value)
            return {
                "id": item_id,
                "name": requested_name,
                "type": self.TYPE_NAMES[item.get("type")],
                "values": values,
            }


VAULT = VaultManager()
