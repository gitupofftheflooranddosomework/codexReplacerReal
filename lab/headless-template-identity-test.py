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
    arc = mod.arc_capacity_from_values(92 * 1024, 6 * 1024)
    assert arc["protectedMiB"] == 16 * 1024, arc
    assert arc["reclaimableMiB"] == 76 * 1024, arc
    assert mod.admission_charge_mib(2048) == 2560
    conn = mod.db()
    future = mod.stamp(mod.now() + timedelta(hours=1))
    old = mod.stamp(mod.now() - timedelta(minutes=10))
    recent = mod.stamp(mod.now() - timedelta(seconds=30))
    def row(iid, created):
        n = 240 + len(iid)
        return (iid, f"ci-test-{iid}", f"192.168.122.{n}", f"52:54:00:ce:00:{n:02x}", "test", "test", None, str(td), 2048, 4096, 2, 40, created, None, None, future, None, "creating", None, None, None)
    conn.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row("old", old))
    conn.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row("newer", recent))
    conn.commit()
    assert mod.pending_create_charge_mib(conn) == 5120
    conn.close()
    calls = []
    def fake_finish(iid, status="finished", exit_code=None, reason="job_finished", keep=0):
        calls.append((iid, status, reason)); return {"id": iid}
    mod.finish = fake_finish
    # A live creator/waiter marker prevents stale-create GC from reaping a
    # healthy process that is queued behind the libvirt mutation lock.
    mod.PROVISIONERS.mkdir(parents=True, exist_ok=True)
    marker = mod.PROVISIONERS / "old.json"
    marker.write_text(__import__("json").dumps({"pid": os.getpid(), "started_at": mod.stamp()}) + "\n")
    assert mod.provision_owner_alive("old") is True
    assert mod.gc() == [], calls
    assert calls == [], calls
    marker.unlink()
    assert mod.gc() == ["old"], calls
    assert calls == [("old", "failed", "stale_creating")], calls
    err = __import__("subprocess").CalledProcessError(1, ["virt-install", "--import"], output="OUT", stderr="REAL_LIBVIRT_ERROR")
    formatted = mod.process_error(err)
    assert "rc=1" in formatted and "REAL_LIBVIRT_ERROR" in formatted and "OUT" in formatted, formatted
    assert 'def provision_locked' in (ROOT / "codex-ci-headless.py").read_text()
    assert 'provision_wait' in (ROOT / "codex-ci-headless.py").read_text()
    assert 'provision_enter' in (ROOT / "codex-ci-headless.py").read_text()

    # Existing dispatchers invoke the one-shot `acquire` command. Preserve that
    # public contract while the internal reserve/provision split is available
    # for focused orchestration and testing.
    import contextlib, io, json
    mod.gc = lambda: []
    mod.reserve = lambda *args, **kwargs: ({
        "id": "compat", "name": "ci-compat", "ip": "192.168.122.199",
        "mac": "52:54:00:ce:00:c7", "status": "creating"
    }, False)
    mod.provision = lambda rec: {**rec, "status": "running"}
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert mod.main(["acquire", "--owner", "compat-test", "--project", "serverworkerbot"]) == 0
    payload = json.loads(output.getvalue())
    assert payload["id"] == "compat" and payload["status"] == "running" and payload["reused"] is False, payload

print("headless_template_identity_test=ok")
