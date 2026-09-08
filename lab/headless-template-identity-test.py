#!/usr/bin/env python3
import importlib.util
import os
import pathlib
import tempfile
from datetime import timedelta

ROOT = pathlib.Path(__file__).resolve().parent
build = (ROOT / "build-ci-headless-base.sh").read_text()
convert_at = build.index('qemu-img convert -p -O qcow2 "$OVERLAY" "$FLAT"')
sanitize_at = build.index("truncate -s 0 /etc/machine-id")
replicate_at = build.index("IFS=',' read -ra roots")
assert convert_at < sanitize_at < replicate_at
assert 'codex-ci-base-v2.qcow2' in build
for needle in ("/var/lib/dbus/machine-id", "/var/lib/dhcp/*", "/var/lib/NetworkManager/*lease*", "/var/lib/systemd/network/*"):
    assert needle in build, needle

with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    os.environ["CODEX_CI_HEADLESS_ROOT"] = str(td)
    os.environ["CODEX_CI_HEADLESS_DB"] = str(td / "lifecycle.sqlite3")
    os.environ["CODEX_CI_HEADLESS_STATE"] = str(td / "state.json")
    os.environ["CODEX_CI_HEADLESS_LOCK"] = str(td / "allocator.lock")
    os.environ["CODEX_CI_STALE_CREATING_SECONDS"] = "300"
    spec = importlib.util.spec_from_file_location("headless", ROOT / "codex-ci-headless.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.BASE == "codex-ci-base-v2.qcow2"
    gib = 1024 ** 3
    assert mod.effective_disk_gib(100 * gib, 40) == 100
    assert mod.effective_disk_gib(100 * gib, 120) == 120
    assert mod.effective_disk_gib(100 * gib + 1, 100) == 101
    assert mod.desktop_target_mib({"memTotalMiB": 8192, "memAvailableMiB": 6500}, True) == 6144
    assert mod.desktop_target_mib({"memTotalMiB": 8192, "memAvailableMiB": 1000}, True) == 8192
    assert mod.desktop_target_mib({"memTotalMiB": 8192, "memAvailableMiB": 6500}, False) == 4096
    assert mod.desktop_target_mib({}, True) == 8192
    conn = mod.db()
    future = mod.stamp(mod.now() + timedelta(hours=1))
    old = mod.stamp(mod.now() - timedelta(minutes=10))
    recent = mod.stamp(mod.now() - timedelta(seconds=30))
    def row(iid, created):
        n = 240 + len(iid)
        return (iid, f"ci-test-{iid}", f"192.168.122.{n}", f"52:54:00:ce:00:{n:02x}", "test", "test", None, str(td), 2048, 4096, 2, 40, created, None, None, future, None, "creating", None, None, None)
    conn.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row("old", old))
    conn.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row("newer", recent))
    conn.commit(); conn.close()
    calls = []
    def fake_finish(iid, status="finished", exit_code=None, reason="job_finished", keep=0):
        calls.append((iid, status, reason)); return {"id": iid}
    mod.finish = fake_finish
    assert mod.gc() == ["old"], calls
    assert calls == [("old", "failed", "stale_creating")], calls

print("headless_template_identity_test=ok")
