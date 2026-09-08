#!/usr/bin/env python3
"""Elastic project-scoped headless KVM allocator for CI/build/test overflow.

The persistent codex-lab-vm-01..06 desktop pool is deliberately excluded.
Instances are disposable by default, optionally retained behind a session key
for a bounded TTL, and always recorded in durable lifecycle history.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

URI = os.environ.get("CODEX_CI_LIBVIRT_URI", "qemu:///system")
NETWORK = os.environ.get("CODEX_CI_NETWORK", "default")
ROOT = pathlib.Path(os.environ.get("CODEX_CI_HEADLESS_ROOT", "/tank/vm/codex-ci-headless"))
DB = pathlib.Path(os.environ.get("CODEX_CI_HEADLESS_DB", str(ROOT / "lifecycle.sqlite3")))
STATE = pathlib.Path(os.environ.get("CODEX_CI_HEADLESS_STATE", str(ROOT / "state.json")))
LOCK = pathlib.Path(os.environ.get("CODEX_CI_HEADLESS_LOCK", str(ROOT / "allocator.lock")))
BASE = os.environ.get("CODEX_CI_HEADLESS_BASE", "codex-ci-base-v2.qcow2")
STORES = [pathlib.Path(x.strip()) for x in os.environ.get(
    "CODEX_CI_HEADLESS_STORAGE_ROOTS",
    "/tank/vm/codex-ci-headless,/tank2/vm/codex-ci-headless,/tank3/vm/codex-ci-headless",
).split(",") if x.strip()]
IP_FIRST = int(os.environ.get("CODEX_CI_HEADLESS_IP_START", "100"))
IP_LAST = int(os.environ.get("CODEX_CI_HEADLESS_IP_END", "199"))
MAX_ACTIVE = max(1, int(os.environ.get("CODEX_CI_HEADLESS_MAX_ACTIVE", "48")))
PROJECT_MAX = max(1, int(os.environ.get("CODEX_CI_HEADLESS_PROJECT_MAX_ACTIVE", str(MAX_ACTIVE))))
MEM_MIB = max(768, int(os.environ.get("CODEX_CI_HEADLESS_MEMORY_MIB", "2048")))
MAX_MEM_MIB = max(MEM_MIB, int(os.environ.get("CODEX_CI_HEADLESS_MAX_MEMORY_MIB", "4096")))
VCPUS = max(1, int(os.environ.get("CODEX_CI_HEADLESS_VCPUS", "2")))
DISK_GIB = max(10, int(os.environ.get("CODEX_CI_HEADLESS_DISK_GIB", "40")))
HOST_RESERVE_MIB = max(4096, int(os.environ.get("CODEX_CI_HOST_RESERVE_MIB", "12288")))
ARC_FLOOR_MIB = max(4096, int(os.environ.get("CODEX_CI_ARC_FLOOR_MIB", "16384")))
VM_MEMORY_OVERHEAD_MIB = max(256, int(os.environ.get("CODEX_CI_VM_MEMORY_OVERHEAD_MIB", "512")))
MAX_LOAD_PER_CPU = max(.5, float(os.environ.get("CODEX_CI_MAX_LOAD_PER_CPU", "2.0")))
MAX_IO_PSI_AVG10 = max(1.0, float(os.environ.get("CODEX_CI_MAX_IO_PSI_AVG10", "70.0")))
MAX_MEMORY_PSI_AVG10 = max(1.0, float(os.environ.get("CODEX_CI_MAX_MEMORY_PSI_AVG10", "25.0")))
DESKTOP_IDLE_MIB = max(2048, int(os.environ.get("CODEX_DESKTOP_IDLE_MIB", "4096")))
DESKTOP_ACTIVE_FLOOR_MIB = max(DESKTOP_IDLE_MIB, int(os.environ.get("CODEX_DESKTOP_ACTIVE_FLOOR_MIB", "6144")))
DESKTOP_ACTIVE_MIB = max(DESKTOP_ACTIVE_FLOOR_MIB, int(os.environ.get("CODEX_DESKTOP_ACTIVE_MIB", "8192")))
DESKTOP_HEADROOM_MIB = max(512, int(os.environ.get("CODEX_DESKTOP_HEADROOM_MIB", "2048")))
DESKTOP_BALLOON_STEP_MIB = max(256, int(os.environ.get("CODEX_DESKTOP_BALLOON_STEP_MIB", "512")))
STALE_CREATING_SECONDS = max(120, int(os.environ.get("CODEX_CI_STALE_CREATING_SECONDS", "300")))
SCHEDULER = os.environ.get("CODEX_LAB_SCHEDULER_URL", "http://127.0.0.1:8766").rstrip("/")
ACTIVE = ("creating", "running", "idle", "releasing")


def now():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or now()).isoformat()


def parse_stamp(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def slug(value, limit=18):
    out = re.sub(r"[^a-z0-9]+", "-", str(value or "ci").lower()).strip("-")
    return (out or "ci")[:limit]


def run(argv, timeout=60, check=True):
    return subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, check=check)


def virsh(*args, timeout=30, check=True):
    return run(["virsh", "--connect", URI, *map(str, args)], timeout=timeout, check=check)


def db():
    ROOT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS instances(
      id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,ip TEXT NOT NULL,mac TEXT NOT NULL,
      owner TEXT NOT NULL,project TEXT,session_key TEXT,storage_root TEXT NOT NULL,
      memory_mib INTEGER NOT NULL,max_memory_mib INTEGER NOT NULL,vcpus INTEGER NOT NULL,
      disk_gib INTEGER NOT NULL,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,
      expires_at TEXT NOT NULL,destroyed_at TEXT,status TEXT NOT NULL,exit_code INTEGER,
      teardown_reason TEXT,error TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS events(
      id INTEGER PRIMARY KEY AUTOINCREMENT,instance_id TEXT NOT NULL,at TEXT NOT NULL,
      event TEXT NOT NULL,detail TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS ci_status ON instances(status,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ci_project ON instances(project,status,created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ci_session ON instances(project,session_key,status)")
    conn.commit()
    return conn


@contextlib.contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    LOCK.touch(mode=0o600, exist_ok=True)
    with LOCK.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def active_rows(conn):
    marks = ",".join("?" for _ in ACTIVE)
    return conn.execute(f"SELECT * FROM instances WHERE status IN ({marks}) ORDER BY created_at", ACTIVE).fetchall()


def event(conn, iid, kind, detail=None):
    conn.execute("INSERT INTO events(instance_id,at,event,detail) VALUES(?,?,?,?)",
                 (iid, stamp(), kind, detail))


def meminfo():
    values = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if ":" in line:
                key, rest = line.split(":", 1)
                values[key] = int(rest.strip().split()[0]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def arc_capacity_from_values(size_mib, c_min_mib):
    size = max(0, int(size_mib or 0))
    c_min = max(0, int(c_min_mib or 0))
    protected = max(ARC_FLOOR_MIB, c_min)
    return {
        "sizeMiB": size,
        "cMinMiB": c_min,
        "protectedMiB": protected,
        "reclaimableMiB": max(0, size - protected),
    }


def zfs_arc_capacity():
    values = {}
    try:
        for line in pathlib.Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[0] in ("size", "c_min"):
                values[parts[0]] = int(parts[2]) // (1024 * 1024)
    except (OSError, ValueError, IndexError):
        pass
    return arc_capacity_from_values(values.get("size", 0), values.get("c_min", 0))


def memory_capacity():
    total, available = meminfo()
    arc = zfs_arc_capacity()
    effective = available + arc["reclaimableMiB"]
    if total:
        effective = min(total, effective)
    return {
        "totalMiB": total,
        "availableMiB": available,
        "effectiveAvailableMiB": max(0, effective),
        "arc": arc,
    }


def admission_charge_mib(memory_mib):
    return max(0, int(memory_mib)) + VM_MEMORY_OVERHEAD_MIB


def pending_create_charge_mib(conn):
    rows = conn.execute("SELECT memory_mib FROM instances WHERE status='creating'").fetchall()
    return sum(admission_charge_mib(row["memory_mib"]) for row in rows)


def load_ok():
    try:
        return os.getloadavg()[0] <= max(1, os.cpu_count() or 1) * MAX_LOAD_PER_CPU
    except OSError:
        return True


def psi_avg10(resource, level="some"):
    try:
        for line in pathlib.Path(f"/proc/pressure/{resource}").read_text().splitlines():
            parts = line.split()
            if parts and parts[0] == level:
                for item in parts[1:]:
                    if item.startswith("avg10="):
                        return float(item.split("=", 1)[1])
    except (OSError, ValueError):
        pass
    return None


def pressure_reason():
    if not load_ok():
        return "host CPU/load high-water guard is active"
    io = psi_avg10("io")
    if io is not None and io >= MAX_IO_PSI_AVG10:
        return f"host I/O pressure high-water guard is active ({io:.1f}% avg10)"
    memory = psi_avg10("memory")
    if memory is not None and memory >= MAX_MEMORY_PSI_AVG10:
        return f"host memory pressure high-water guard is active ({memory:.1f}% avg10)"
    return None


def reservations():
    xml = virsh("net-dumpxml", NETWORK, timeout=15, check=False).stdout or ""
    used = set(re.findall(r"\bip=['\"]([^'\"]+)['\"]", xml))
    leases = virsh("net-dhcp-leases", NETWORK, timeout=15, check=False).stdout or ""
    used.update(re.findall(r"\b(192\.168\.122\.\d+)(?:/\d+)?\b", leases))
    return used


def choose_slot(conn):
    used = {row["ip"] for row in active_rows(conn)} | reservations()
    for last in range(IP_FIRST, IP_LAST + 1):
        ip = f"192.168.122.{last}"
        if ip not in used:
            return ip, f"52:54:00:ce:00:{last:02x}"
    raise RuntimeError("no free headless KVM network slot")


def choose_store(conn):
    counts = {str(root): 0 for root in STORES}
    for row in active_rows(conn):
        if row["storage_root"] in counts:
            counts[row["storage_root"]] += 1
    choices = []
    for root in STORES:
        if not (root / BASE).is_file():
            continue
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = 0
        choices.append((counts[str(root)], -free, str(root), root))
    if not choices:
        raise RuntimeError(f"headless base image {BASE} is missing from all storage roots")
    return sorted(choices)[0][3]


def state_data(conn=None, history_limit=100):
    own = conn is None
    conn = conn or db()
    try:
        marks = ",".join("?" for _ in ACTIVE)
        current = [dict(x) for x in conn.execute(
            f"SELECT * FROM instances WHERE status IN ({marks}) ORDER BY created_at DESC", ACTIVE)]
        history = [dict(x) for x in conn.execute(
            f"SELECT * FROM instances WHERE status NOT IN ({marks}) "
            "ORDER BY COALESCE(destroyed_at,finished_at,created_at) DESC LIMIT ?",
            (*ACTIVE, int(history_limit)))]
        memory = memory_capacity()
        arc = memory["arc"]
        pending_charge = pending_create_charge_mib(conn)
        return {"time": stamp(), "capacity": {"maxActive": MAX_ACTIVE,
                "projectMaxActive": PROJECT_MAX, "active": len(current),
                "freeSlots": max(0, MAX_ACTIVE - len(current)),
                "hostMemTotalMiB": memory["totalMiB"],
                "hostMemAvailableMiB": memory["availableMiB"],
                "hostEffectiveAvailableMiB": memory["effectiveAvailableMiB"],
                "zfsArcSizeMiB": arc["sizeMiB"],
                "zfsArcCMinMiB": arc["cMinMiB"],
                "zfsArcProtectedMiB": arc["protectedMiB"],
                "zfsArcReclaimableMiB": arc["reclaimableMiB"],
                "pendingCreateChargeMiB": pending_charge,
                "headlessAdmissionOverheadMiB": VM_MEMORY_OVERHEAD_MIB,
                "headlessDefaultAdmissionChargeMiB": admission_charge_mib(MEM_MIB),
                "hostReserveMiB": HOST_RESERVE_MIB,
                "load1": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
                "loadPerCpuLimit": MAX_LOAD_PER_CPU,
                "ioPsiAvg10": psi_avg10("io"),
                "memoryPsiAvg10": psi_avg10("memory"),
                "pressureReason": pressure_reason()},
                "active": current, "history": history}
    finally:
        if own:
            conn.close()


def write_state(conn=None):
    ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state_data(conn), indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o644)
    tmp.replace(STATE)


def reserve(owner, project, ttl, session_key, memory, max_memory, vcpus, disk):
    current = now()
    with locked():
        conn = db()
        try:
            if session_key:
                row = conn.execute("SELECT * FROM instances WHERE project IS ? AND session_key=? "
                                   "AND status='idle' ORDER BY created_at DESC LIMIT 1",
                                   (project, session_key)).fetchone()
                if row and (parse_stamp(row["expires_at"]) or current) > current:
                    expiry = current + timedelta(seconds=max(60, ttl))
                    conn.execute("UPDATE instances SET status='running',expires_at=?,error=NULL WHERE id=?",
                                 (expiry.isoformat(), row["id"]))
                    event(conn, row["id"], "reused", f"ttl={ttl}")
                    conn.commit(); write_state(conn)
                    return dict(conn.execute("SELECT * FROM instances WHERE id=?", (row["id"],)).fetchone()), True
            rows = active_rows(conn)
            if len(rows) >= MAX_ACTIVE:
                raise RuntimeError(f"headless KVM capacity reached ({len(rows)}/{MAX_ACTIVE})")
            if sum(1 for r in rows if r["project"] == project) >= PROJECT_MAX:
                raise RuntimeError(f"project headless KVM capacity reached ({PROJECT_MAX})")
            memory_state = memory_capacity()
            pending_charge = pending_create_charge_mib(conn)
            new_charge = admission_charge_mib(memory)
            effective_after = memory_state["effectiveAvailableMiB"] - pending_charge - new_charge
            if memory_state["effectiveAvailableMiB"] and effective_after < HOST_RESERVE_MIB:
                arc = memory_state["arc"]
                raise RuntimeError(
                    "host RAM guard: "
                    f"raw={memory_state['availableMiB']} MiB effective={memory_state['effectiveAvailableMiB']} MiB "
                    f"arc_reclaimable={arc['reclaimableMiB']} MiB pending={pending_charge} MiB "
                    f"new_charge={new_charge} MiB reserve={HOST_RESERVE_MIB} MiB"
                )
            pressure = pressure_reason()
            if pressure:
                raise RuntimeError(pressure)
            store = choose_store(conn)
            ip, mac = choose_slot(conn)
            iid = __import__("uuid").uuid4().hex
            name = f"ci-{slug(project or owner)}-{iid[:8]}"
            expiry = current + timedelta(seconds=max(60, ttl))
            conn.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (iid,name,ip,mac,owner,project,session_key,str(store),memory,max_memory,vcpus,disk,
                 current.isoformat(),None,None,expiry.isoformat(),None,"creating",None,None,None))
            event(conn, iid, "reserved", f"ip={ip} store={store}")
            conn.commit(); write_state(conn)
            return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()), False
        finally:
            conn.close()


def effective_disk_gib(base_bytes, requested_gib):
    gib = 1024 ** 3
    base_gib = (int(base_bytes) + gib - 1) // gib
    return max(int(requested_gib), base_gib)


def base_virtual_bytes(base):
    info = run(["qemu-img", "info", "--output=json", str(base)], timeout=30)
    try:
        size = int(json.loads(info.stdout or "{}")["virtual-size"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"unable to determine base virtual size for {base}: {exc}") from exc
    if size <= 0:
        raise RuntimeError(f"invalid base virtual size for {base}: {size}")
    return size


def add_dhcp(rec):
    xml = f"<host mac='{rec['mac']}' name='{rec['name']}' ip='{rec['ip']}'/>"
    virsh("net-update", NETWORK, "add", "ip-dhcp-host", xml, "--live", "--config")


def del_dhcp(rec):
    xml = f"<host mac='{rec['mac']}' name='{rec['name']}' ip='{rec['ip']}'/>"
    virsh("net-update", NETWORK, "delete", "ip-dhcp-host", xml, "--live", "--config", check=False)


def destroy_resources(rec):
    virsh("destroy", rec["name"], timeout=20, check=False)
    virsh("undefine", rec["name"], timeout=20, check=False)
    del_dhcp(rec)
    shutil.rmtree(pathlib.Path(rec["storage_root"]) / rec["name"], ignore_errors=True)


def provision(rec):
    store = pathlib.Path(rec["storage_root"])
    base = store / BASE
    root = store / rec["name"]
    disk = root / f"{rec['name']}.qcow2"
    root.mkdir(parents=True, exist_ok=False)
    try:
        effective_gib = effective_disk_gib(base_virtual_bytes(base), rec["disk_gib"])
        run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disk), f"{effective_gib}G"])
        add_dhcp(rec)
        run(["virt-install", "--connect", URI, "--name", rec["name"],
             "--memory", f"memory={rec['memory_mib']},maxmemory={rec['max_memory_mib']}",
             "--vcpus", str(rec["vcpus"]), "--cpu", "host-passthrough",
             "--disk", f"path={disk},format=qcow2,bus=virtio",
             "--network", f"network={NETWORK},model=virtio,mac={rec['mac']}",
             "--os-variant", "debian11", "--graphics", "none", "--noautoconsole", "--import"], timeout=90)
        with locked():
            conn = db()
            try:
                conn.execute("UPDATE instances SET status='running',started_at=?,disk_gib=?,error=NULL WHERE id=?",
                             (stamp(), effective_gib, rec["id"]))
                event(conn, rec["id"], "started", f"domain={rec['name']}")
                conn.commit(); write_state(conn)
                return dict(conn.execute("SELECT * FROM instances WHERE id=?", (rec["id"],)).fetchone())
            finally: conn.close()
    except Exception as exc:
        destroy_resources(rec)
        with locked():
            conn = db()
            try:
                conn.execute("UPDATE instances SET status='failed',finished_at=?,destroyed_at=?,error=?,teardown_reason='provision_failed' WHERE id=?",
                             (stamp(), stamp(), str(exc)[:2000], rec["id"]))
                event(conn, rec["id"], "provision_failed", str(exc)[:2000])
                conn.commit(); write_state(conn)
            finally: conn.close()
        raise


def finish(iid, status="finished", exit_code=None, reason="job_finished", keep=0):
    with locked():
        conn = db()
        try:
            row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            if not row: raise RuntimeError(f"unknown headless instance {iid}")
            rec = dict(row)
            if rec["status"] in ("destroyed", "failed"): return rec
            if keep > 0:
                expiry = now() + timedelta(seconds=max(60, keep))
                conn.execute("UPDATE instances SET status='idle',finished_at=?,expires_at=?,exit_code=?,teardown_reason=? WHERE id=?",
                             (stamp(), expiry.isoformat(), exit_code, reason, iid))
                event(conn, iid, "retained", expiry.isoformat()); conn.commit(); write_state(conn)
                return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())
            conn.execute("UPDATE instances SET status='releasing',finished_at=?,exit_code=?,teardown_reason=? WHERE id=?",
                         (stamp(), exit_code, reason, iid))
            event(conn, iid, "releasing", f"status={status} reason={reason}"); conn.commit(); write_state(conn)
        finally: conn.close()
    destroy_resources(rec)
    with locked():
        conn = db()
        try:
            conn.execute("UPDATE instances SET status='destroyed',destroyed_at=?,error=NULL WHERE id=?", (stamp(), iid))
            event(conn, iid, "destroyed", reason); conn.commit(); write_state(conn)
            return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())
        finally: conn.close()


def renew(iid, ttl):
    expiry = now() + timedelta(seconds=max(60, ttl))
    with locked():
        conn = db()
        try:
            row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
            if not row or row["status"] not in ACTIVE: raise RuntimeError(f"headless instance {iid} is not active")
            conn.execute("UPDATE instances SET expires_at=? WHERE id=?", (expiry.isoformat(), iid))
            event(conn, iid, "renewed", expiry.isoformat()); conn.commit(); write_state(conn)
            return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())
        finally: conn.close()


def gc():
    conn = db()
    try: rows = [dict(x) for x in active_rows(conn)]
    finally: conn.close()
    reaped = []
    current = now()
    for rec in rows:
        expiry = parse_stamp(rec["expires_at"])
        created = parse_stamp(rec["created_at"])
        stale_creating = (
            rec["status"] == "creating"
            and created is not None
            and (current - created).total_seconds() >= STALE_CREATING_SECONDS
        )
        expired = bool(expiry and expiry <= current)
        if not (expired or stale_creating):
            continue
        reason = "stale_creating" if stale_creating else "ttl_expired"
        status = "failed" if stale_creating else "expired"
        try:
            finish(rec["id"], status, rec["exit_code"], reason, 0)
            reaped.append(rec["id"])
        except Exception as exc:
            with locked():
                conn = db()
                try:
                    conn.execute("UPDATE instances SET error=? WHERE id=?", (str(exc)[:2000], rec["id"]))
                    event(conn, rec["id"], "gc_error", str(exc)[:2000]); conn.commit(); write_state(conn)
                finally: conn.close()
    return reaped


def scheduler_workers():
    import urllib.request
    req = urllib.request.Request(SCHEDULER + "/api/workers", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as response:
        return list((json.load(response) or {}).get("workers") or [])


def desktop_target_mib(worker, busy):
    if not busy:
        return DESKTOP_IDLE_MIB
    try:
        total = float(worker["memTotalMiB"])
        available = float(worker["memAvailableMiB"])
        if total <= 0 or available < 0 or available > total:
            raise ValueError("invalid guest memory metrics")
        used = max(0.0, total - available)
    except (KeyError, TypeError, ValueError):
        # Fail safe: never shrink an active desktop when live guest metrics are missing.
        return DESKTOP_ACTIVE_MIB
    wanted = int(used + DESKTOP_HEADROOM_MIB + DESKTOP_BALLOON_STEP_MIB - 1)
    wanted = (wanted // DESKTOP_BALLOON_STEP_MIB) * DESKTOP_BALLOON_STEP_MIB
    return min(DESKTOP_ACTIVE_MIB, max(DESKTOP_ACTIVE_FLOOR_MIB, wanted))


def rebalance_desktops():
    changed, errors = [], []
    for worker in scheduler_workers():
        try: station = int(worker.get("station"))
        except Exception: continue
        if not 1 <= station <= 6: continue
        name = f"codex-lab-vm-{station:02d}"
        busy = bool(worker.get("activeJob") or worker.get("exclusive") or worker.get("schedulerBusy") or worker.get("lease"))
        target = desktop_target_mib(worker, busy)
        if virsh("dominfo", name, timeout=5, check=False).returncode:
            errors.append(f"{name}: unavailable"); continue
        live = virsh("setmem", name, f"{target}MiB", "--live", timeout=8, check=False)
        conf = virsh("setmem", name, f"{target}MiB", "--config", timeout=8, check=False)
        if live.returncode == 0 and conf.returncode == 0:
            changed.append({"name": name, "busy": busy, "targetMiB": target})
        else:
            errors.append(f"{name}: {(live.stderr or conf.stderr or live.stdout or conf.stdout).strip()}")
    return {"changed": changed, "errors": errors}


def acquire_cmd(a):
    gc()
    rec, reused = reserve(a.owner, a.project or None, a.ttl_seconds, a.session_key or None,
                          a.memory_mib, max(a.memory_mib, a.max_memory_mib), a.vcpus, a.disk_gib)
    if reused:
        state = virsh("domstate", rec["name"], timeout=5, check=False)
        if state.returncode: raise RuntimeError(f"retained VM {rec['name']} no longer exists")
        if "running" not in (state.stdout or "").lower(): virsh("start", rec["name"], timeout=30)
    else:
        rec = provision(rec)
    print(json.dumps({**rec, "reused": reused}, separators=(",", ":"))); return 0


def parser():
    p = argparse.ArgumentParser(description="Elastic project-scoped headless KVM allocator")
    s = p.add_subparsers(dest="cmd", required=True)
    q = s.add_parser("acquire"); q.add_argument("--owner", required=True); q.add_argument("--project", default="")
    q.add_argument("--ttl-seconds", type=int, default=7200); q.add_argument("--session-key", default="")
    q.add_argument("--memory-mib", type=int, default=MEM_MIB); q.add_argument("--max-memory-mib", type=int, default=MAX_MEM_MIB)
    q.add_argument("--vcpus", type=int, default=VCPUS); q.add_argument("--disk-gib", type=int, default=DISK_GIB); q.set_defaults(fn=acquire_cmd)
    q = s.add_parser("finish"); q.add_argument("instance_id"); q.add_argument("--status", default="finished")
    q.add_argument("--exit-code", type=int); q.add_argument("--reason", default="job_finished"); q.add_argument("--keep-seconds", type=int, default=0)
    q.set_defaults(fn=lambda a: (print(json.dumps(finish(a.instance_id,a.status,a.exit_code,a.reason,a.keep_seconds), separators=(",", ":"))) or 0))
    q = s.add_parser("renew"); q.add_argument("instance_id"); q.add_argument("--ttl-seconds", type=int, required=True)
    q.set_defaults(fn=lambda a: (print(json.dumps(renew(a.instance_id,a.ttl_seconds), separators=(",", ":"))) or 0))
    q = s.add_parser("gc"); q.set_defaults(fn=lambda a: (print(json.dumps({"destroyed":gc()}, separators=(",", ":"))) or 0))
    q = s.add_parser("state"); q.add_argument("--history-limit", type=int, default=100)
    q.set_defaults(fn=lambda a: (print(json.dumps(state_data(history_limit=a.history_limit), separators=(",", ":"))) or 0))
    q = s.add_parser("rebalance-desktops"); q.set_defaults(fn=lambda a: (print(json.dumps(rebalance_desktops(), separators=(",", ":"))) or 0))
    return p


def main():
    a = parser().parse_args()
    try: return int(a.fn(a) or 0)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr); return 75


if __name__ == "__main__":
    raise SystemExit(main())
