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
PROVISION_LOCK = pathlib.Path(os.environ.get("CODEX_CI_HEADLESS_PROVISION_LOCK", str(ROOT / "provision.lock")))
PROVISIONERS = pathlib.Path(os.environ.get("CODEX_CI_HEADLESS_PROVISIONERS", str(ROOT / "provisioners")))
BASE = os.environ.get("CODEX_CI_HEADLESS_BASE", "codex-ci-base-v3.qcow2")
STORES = [pathlib.Path(x.strip()) for x in os.environ.get(
    "CODEX_CI_HEADLESS_STORAGE_ROOTS",
    "/tank/vm/codex-ci-headless,/tank2/vm/codex-ci-headless,/tank3/vm/codex-ci-headless",
).split(",") if x.strip()]
IP_FIRST = int(os.environ.get("CODEX_CI_HEADLESS_IP_START", "100"))
IP_LAST = int(os.environ.get("CODEX_CI_HEADLESS_IP_END", "199"))
MAX_ACTIVE = max(1, int(os.environ.get("CODEX_CI_HEADLESS_MAX_ACTIVE", "96")))
PROJECT_MAX = max(1, int(os.environ.get("CODEX_CI_HEADLESS_PROJECT_MAX_ACTIVE", str(MAX_ACTIVE))))
MEM_MIB = max(768, int(os.environ.get("CODEX_CI_HEADLESS_MEMORY_MIB", "2048")))
MAX_MEM_MIB = max(MEM_MIB, int(os.environ.get("CODEX_CI_HEADLESS_MAX_MEMORY_MIB", "4096")))
VCPUS = max(1, int(os.environ.get("CODEX_CI_HEADLESS_VCPUS", "2")))
MAX_HEADLESS_VCPUS = max(1, int(os.environ.get(
    "CODEX_CI_HEADLESS_MAX_VCPUS",
    str(max(1, ((os.cpu_count() or 1) * 3) // 4)),
)))
DISK_GIB = max(10, int(os.environ.get("CODEX_CI_HEADLESS_DISK_GIB", "40")))
HOST_RESERVE_MIB = max(4096, int(os.environ.get("CODEX_CI_HOST_RESERVE_MIB", "12288")))
ARC_FLOOR_MIB = max(4096, int(os.environ.get("CODEX_CI_ARC_FLOOR_MIB", "16384")))
VM_MEMORY_OVERHEAD_MIB = max(256, int(os.environ.get("CODEX_CI_VM_MEMORY_OVERHEAD_MIB", "512")))
MAX_LOAD_PER_CPU = max(.5, float(os.environ.get("CODEX_CI_MAX_LOAD_PER_CPU", "1.25")))
MAX_IO_PSI_AVG10 = max(1.0, float(os.environ.get("CODEX_CI_MAX_IO_PSI_AVG10", "70.0")))
MAX_MEMORY_PSI_AVG10 = max(1.0, float(os.environ.get("CODEX_CI_MAX_MEMORY_PSI_AVG10", "25.0")))
DESKTOP_IDLE_MIB = max(2048, int(os.environ.get("CODEX_DESKTOP_IDLE_MIB", "4096")))
DESKTOP_ACTIVE_FLOOR_MIB = max(DESKTOP_IDLE_MIB, int(os.environ.get("CODEX_DESKTOP_ACTIVE_FLOOR_MIB", "6144")))
DESKTOP_ACTIVE_MIB = max(DESKTOP_ACTIVE_FLOOR_MIB, int(os.environ.get("CODEX_DESKTOP_ACTIVE_MIB", "8192")))
DESKTOP_HEADROOM_MIB = max(512, int(os.environ.get("CODEX_DESKTOP_HEADROOM_MIB", "2048")))
DESKTOP_BALLOON_STEP_MIB = max(256, int(os.environ.get("CODEX_DESKTOP_BALLOON_STEP_MIB", "512")))
STALE_CREATING_SECONDS = max(120, int(os.environ.get("CODEX_CI_STALE_CREATING_SECONDS", "300")))
STORE_BACKOFF_BASE_SECONDS = max(5, int(os.environ.get("CODEX_CI_STORE_BACKOFF_BASE_SECONDS", "30")))
STORE_BACKOFF_MAX_SECONDS = max(STORE_BACKOFF_BASE_SECONDS, int(os.environ.get("CODEX_CI_STORE_BACKOFF_MAX_SECONDS", "900")))
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
    conn.execute("""CREATE TABLE IF NOT EXISTS dispatch_owners(
      instance_id TEXT PRIMARY KEY,pid INTEGER NOT NULL,identity TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS storage_health(
      storage_root TEXT PRIMARY KEY,consecutive_failures INTEGER NOT NULL DEFAULT 0,
      last_failure_at TEXT,backoff_until TEXT,last_error TEXT,last_success_at TEXT)""")
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


@contextlib.contextmanager
def provision_locked():
    """Serialize only libvirt network/domain mutations, never guest execution."""
    ROOT.mkdir(parents=True, exist_ok=True)
    PROVISION_LOCK.touch(mode=0o600, exist_ok=True)
    with PROVISION_LOCK.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def provision_marker(iid):
    """Mark a creator process alive while it waits for/holds the libvirt lock."""
    PROVISIONERS.mkdir(parents=True, exist_ok=True)
    marker = PROVISIONERS / f"{iid}.json"
    tmp = marker.with_suffix(".tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "started_at": stamp()}, separators=(",", ":")) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(marker)
    try:
        yield
    finally:
        try:
            marker.unlink()
        except FileNotFoundError:
            pass


def provision_owner_alive(iid):
    marker = PROVISIONERS / f"{iid}.json"
    try:
        payload = json.loads(marker.read_text())
        pid = int(payload.get("pid") or 0)
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError, ProcessLookupError, PermissionError):
        return False


def process_error(exc):
    if isinstance(exc, subprocess.CalledProcessError):
        parts = [f"command failed rc={exc.returncode}: {exc.cmd!r}"]
        if exc.stdout:
            parts.append("stdout=" + str(exc.stdout)[-2000:])
        if exc.stderr:
            parts.append("stderr=" + str(exc.stderr)[-4000:])
        return "\n".join(parts)
    if isinstance(exc, subprocess.TimeoutExpired):
        parts = [f"command timed out after {exc.timeout}s: {exc.cmd!r}"]
        if exc.stdout:
            parts.append("stdout=" + str(exc.stdout)[-2000:])
        if exc.stderr:
            parts.append("stderr=" + str(exc.stderr)[-4000:])
        return "\n".join(parts)
    return str(exc)


def record_instance_event(iid, kind, detail=None):
    with locked():
        conn = db()
        try:
            event(conn, iid, kind, detail)
            conn.commit(); write_state(conn)
        finally:
            conn.close()


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
    # ARC shrink is asynchronous and can stall under swap/I/O pressure. Admit
    # only against RAM the kernel already reports available, not theoretical
    # future reclaim (which previously overcommitted a heavily swapping host).
    effective = available
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


def active_vcpu_charge(conn):
    return sum(max(1, int(row["vcpus"])) for row in active_rows(conn))


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
    io_psi = psi_avg10("io")
    if io_psi is not None and io_psi > MAX_IO_PSI_AVG10:
        return f"host I/O pressure high-water guard is active ({io_psi:.2f}>{MAX_IO_PSI_AVG10:.2f})"
    memory_psi = psi_avg10("memory")
    if memory_psi is not None and memory_psi > MAX_MEMORY_PSI_AVG10:
        return f"host memory pressure high-water guard is active ({memory_psi:.2f}>{MAX_MEMORY_PSI_AVG10:.2f})"
    return None


def storage_backoff_seconds(failures):
    failures = max(1, int(failures or 1))
    return min(STORE_BACKOFF_MAX_SECONDS, STORE_BACKOFF_BASE_SECONDS * (2 ** min(20, failures - 1)))


def storage_health_row(conn, root):
    return conn.execute("SELECT * FROM storage_health WHERE storage_root=?", (str(root),)).fetchone()


def storage_retry_seconds(row, current=None):
    if not row:
        return 0
    until = parse_stamp(row["backoff_until"])
    current = current or now()
    if not until or until <= current:
        return 0
    return max(1, int((until - current).total_seconds() + .999))


def mark_store_failure(conn, root, detail):
    root = str(root)
    row = storage_health_row(conn, root)
    failures = int(row["consecutive_failures"] if row else 0) + 1
    delay = storage_backoff_seconds(failures)
    current = now()
    until = current + timedelta(seconds=delay)
    conn.execute("""INSERT INTO storage_health(storage_root,consecutive_failures,last_failure_at,backoff_until,last_error,last_success_at)
                    VALUES(?,?,?,?,?,NULL)
                    ON CONFLICT(storage_root) DO UPDATE SET
                      consecutive_failures=excluded.consecutive_failures,
                      last_failure_at=excluded.last_failure_at,
                      backoff_until=excluded.backoff_until,
                      last_error=excluded.last_error""",
                 (root, failures, current.isoformat(), until.isoformat(), str(detail)[:2000]))
    return delay


def mark_store_success(conn, root):
    root = str(root)
    conn.execute("""INSERT INTO storage_health(storage_root,consecutive_failures,last_failure_at,backoff_until,last_error,last_success_at)
                    VALUES(?,0,NULL,NULL,NULL,?)
                    ON CONFLICT(storage_root) DO UPDATE SET
                      consecutive_failures=0,backoff_until=NULL,last_error=NULL,last_success_at=excluded.last_success_at""",
                 (root, stamp()))


def storage_health_data(conn):
    current = now()
    rows = {row["storage_root"]: row for row in conn.execute("SELECT * FROM storage_health")}
    result = []
    for root in STORES:
        row = rows.get(str(root))
        retry = storage_retry_seconds(row, current)
        base = root / BASE
        result.append({
            "storageRoot": str(root),
            "basePresent": base.is_file(),
            "status": "backoff" if retry else ("healthy" if base.is_file() else "missing_base"),
            "consecutiveFailures": int(row["consecutive_failures"] if row else 0),
            "retryInSeconds": retry,
            "backoffUntil": row["backoff_until"] if row else None,
            "lastFailureAt": row["last_failure_at"] if row else None,
            "lastSuccessAt": row["last_success_at"] if row else None,
            "lastError": row["last_error"] if row else None,
        })
    return result


def store_retry_seconds(root):
    with locked():
        conn = db()
        try:
            return storage_retry_seconds(storage_health_row(conn, root))
        finally:
            conn.close()


def choose_store(conn):
    active = active_rows(conn)
    counts = {str(x): 0 for x in STORES}
    for row in active:
        if row["storage_root"] in counts: counts[row["storage_root"]] += 1
    candidates, blocked = [], []
    current = now()
    for root in STORES:
        try:
            root.mkdir(parents=True, exist_ok=True)
            base = root / BASE
            if not base.is_file():
                continue
            usage = shutil.disk_usage(root)
        except OSError:
            continue
        health = storage_health_row(conn, root)
        retry = storage_retry_seconds(health, current)
        if retry:
            blocked.append((retry, str(root)))
            continue
        candidates.append((counts[str(root)], -(usage.free), str(root), root))
    if not candidates:
        if blocked:
            retry, root = min(blocked)
            raise RuntimeError(f"all headless storage roots are in backoff; earliest retry in {retry}s ({root})")
        raise RuntimeError("no healthy headless storage root with base image is available")
    candidates.sort()
    return candidates[0][-1]


def current_dhcp_leases():
    leases = virsh("net-dhcp-leases", NETWORK, timeout=15, check=False)
    text = (leases.stdout or "") + "\n" + (leases.stderr or "")
    found = set()
    for match in re.finditer(r"\b192\.168\.122\.(\d{1,3})\b", text):
        found.add(int(match.group(1)))
    return found


def choose_slot(conn):
    used_ips = {int(r["ip"].rsplit(".",1)[1]) for r in active_rows(conn)}
    used_ips |= current_dhcp_leases()
    for last in range(IP_FIRST, IP_LAST + 1):
        if last in used_ips: continue
        mac = f"52:54:00:ce:{last // 256:02x}:{last % 256:02x}"
        return f"192.168.122.{last}", mac
    raise RuntimeError("no headless IP slots are available")


def state_data(conn=None, history_limit=40):
    own = conn is None
    conn = conn or db()
    try:
        marks = ",".join("?" for _ in ACTIVE)
        current = [dict(x) for x in conn.execute(f"SELECT * FROM instances WHERE status IN ({marks}) ORDER BY created_at DESC", ACTIVE)]
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
                "maxHeadlessVcpus": MAX_HEADLESS_VCPUS,
                "activeHeadlessVcpus": active_vcpu_charge(conn),
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
                "storageHealth": storage_health_data(conn),
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


def process_identity(pid):
    """Distinguish a live dispatcher from PID reuse and a previous host boot."""
    try:
        stat = pathlib.Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()
        if stat[0] == "Z":
            return None
        return pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ':' + stat[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def record_dispatch_owner(conn, iid):
    pid = int(os.environ.get('CODEX_CI_DISPATCH_PID', '0'))
    if pid > 0:
        identity = process_identity(pid)
        if identity is None:
            raise RuntimeError('dispatcher exited before instance reservation')
        conn.execute('INSERT OR REPLACE INTO dispatch_owners VALUES(?,?,?)', (iid, pid, identity))
    else:
        conn.execute('DELETE FROM dispatch_owners WHERE instance_id=?', (iid,))


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
                    record_dispatch_owner(conn, row['id'])
                    conn.commit(); write_state(conn)
                    return dict(conn.execute("SELECT * FROM instances WHERE id=?", (row["id"],)).fetchone()), True
            rows = active_rows(conn)
            if len(rows) >= MAX_ACTIVE:
                raise RuntimeError(f"headless KVM capacity reached ({len(rows)}/{MAX_ACTIVE})")
            if sum(1 for r in rows if r["project"] == project) >= PROJECT_MAX:
                raise RuntimeError(f"project headless KVM capacity reached ({PROJECT_MAX})")
            active_vcpus = sum(max(1, int(row["vcpus"])) for row in rows)
            if active_vcpus + vcpus > MAX_HEADLESS_VCPUS:
                raise RuntimeError(
                    f"headless vCPU capacity reached ({active_vcpus}/{MAX_HEADLESS_VCPUS}); "
                    f"requested {vcpus}"
                )
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
            record_dispatch_owner(conn, iid)
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


def domain_presence(name):
    """Return True/False when libvirt can prove presence/absence, else None."""
    try:
        cp = virsh("dominfo", name, timeout=5, check=False)
    except subprocess.TimeoutExpired:
        return None
    if cp.returncode == 0:
        return True
    detail = ((cp.stderr or "") + "\n" + (cp.stdout or "")).lower()
    if "domain not found" in detail or "failed to get domain" in detail:
        return False
    return None


def destroy_resources(rec):
    # A stuck libvirt call must not skip later cleanup. We attempt each mutation
    # independently, then prove the domain is absent before removing its disk.
    # Transient errors are returned for lifecycle history; incomplete cleanup is
    # raised so the instance remains `releasing` and GC can safely retry it.
    warnings = []
    fatal = []

    def attempt(label, func):
        try:
            return func()
        except Exception as exc:
            warnings.append(f"{label}: {process_error(exc)}")
            return None

    with provision_locked():
        attempt("destroy", lambda: virsh("destroy", rec["name"], timeout=12, check=False))
        if domain_presence(rec["name"]) is True:
            attempt("destroy_retry", lambda: virsh("destroy", rec["name"], timeout=12, check=False))
        attempt("undefine", lambda: virsh("undefine", rec["name"], timeout=12, check=False))
        try:
            del_dhcp(rec)
        except Exception as exc:
            fatal.append(f"dhcp_cleanup: {process_error(exc)}")

    presence = domain_presence(rec["name"])
    root = pathlib.Path(rec["storage_root"]) / rec["name"]
    if presence is False:
        try:
            shutil.rmtree(root, ignore_errors=False)
        except FileNotFoundError:
            pass
        except Exception as exc:
            fatal.append(f"storage_cleanup: {process_error(exc)}")
        if root.exists():
            fatal.append(f"storage_cleanup: path still exists: {root}")
    elif presence is True:
        fatal.append(f"domain_cleanup: domain still exists: {rec['name']}")
    else:
        fatal.append(f"domain_cleanup: unable to prove domain absent: {rec['name']}")

    if fatal:
        raise RuntimeError("headless cleanup incomplete: " + "; ".join(warnings + fatal))
    return warnings


def provision(rec):
    store = pathlib.Path(rec["storage_root"])
    base = store / BASE
    root = store / rec["name"]
    disk = root / f"{rec['name']}.qcow2"
    retry = store_retry_seconds(store)
    if retry:
        detail = f"storage root {store} is in backoff for {retry}s after provisioning failure"
        with locked():
            conn = db()
            try:
                conn.execute("UPDATE instances SET status='failed',finished_at=?,destroyed_at=?,error=?,teardown_reason='storage_backoff' WHERE id=?",
                             (stamp(), stamp(), detail, rec["id"]))
                event(conn, rec["id"], "storage_backoff", detail)
                conn.commit(); write_state(conn)
            finally:
                conn.close()
        raise RuntimeError(detail)
    root.mkdir(parents=True, exist_ok=False)
    # Caller umasks differ between the scheduler service and GitHub runner
    # services. Libvirt creates the qcow2 as libvirt-qemu, so QEMU must be
    # able to search this mark-owned directory even when the caller uses 077.
    # 0711 grants traversal without allowing other users to list its contents.
    os.chmod(root, 0o711)
    try:
        with provision_marker(rec["id"]):
            effective_gib = effective_disk_gib(base_virtual_bytes(base), rec["disk_gib"])
            record_instance_event(rec["id"], "provision_wait", f"pid={os.getpid()}")
            # Let libvirt create the sparse backed overlay inside the serialized
            # storage/domain mutation section. Pre-creating it as the scheduler
            # user leaves a mark-owned qcow2 that qemu (uid 64055) cannot open on
            # tank2/tank3; virt-install creates the same overlay as libvirt-qemu.
            with provision_locked():
                record_instance_event(rec["id"], "provision_enter", f"pid={os.getpid()}")
                add_dhcp(rec)
                try:
                    run(["virt-install", "--connect", URI, "--name", rec["name"],
                         "--memory", f"memory={rec['memory_mib']},maxmemory={rec['max_memory_mib']}",
                         "--vcpus", str(rec["vcpus"]), "--cpu", "host-passthrough",
                         "--disk", (f"path={disk},size={effective_gib},format=qcow2,"
                                    f"backing_store={base},backing_format=qcow2,bus=virtio,sparse=yes"),
                         "--network", f"network={NETWORK},model=virtio,mac={rec['mac']}",
                         "--os-variant", "debian11", "--graphics", "none", "--noautoconsole", "--import"], timeout=90)
                except Exception:
                    # Undo the reservation before another creator enters the lock.
                    del_dhcp(rec)
                    raise
            with locked():
                conn = db()
                try:
                    mark_store_success(conn, store)
                    conn.execute("UPDATE instances SET status='running',started_at=?,disk_gib=?,error=NULL WHERE id=?",
                                 (stamp(), effective_gib, rec["id"]))
                    event(conn, rec["id"], "started", f"domain={rec['name']}")
                    event(conn, rec["id"], "storage_healthy", str(store))
                    conn.commit(); write_state(conn)
                    return dict(conn.execute("SELECT * FROM instances WHERE id=?", (rec["id"],)).fetchone())
                finally: conn.close()
    except Exception as exc:
        detail = process_error(exc)
        destroy_resources(rec)
        with locked():
            conn = db()
            try:
                delay = mark_store_failure(conn, store, detail)
                conn.execute("UPDATE instances SET status='failed',finished_at=?,destroyed_at=?,error=?,teardown_reason='provision_failed' WHERE id=?",
                             (stamp(), stamp(), detail[:6000], rec["id"]))
                event(conn, rec["id"], "provision_failed", detail[:6000])
                event(conn, rec["id"], "storage_backoff", f"root={store} delay={delay}s")
                conn.commit(); write_state(conn)
            finally: conn.close()
        raise RuntimeError(detail) from exc


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
    try:
        warnings = destroy_resources(rec)
    except Exception as exc:
        detail = process_error(exc)[:6000]
        with locked():
            conn = db()
            try:
                conn.execute("UPDATE instances SET status='releasing',error=? WHERE id=?", (detail, iid))
                event(conn, iid, "cleanup_incomplete", detail)
                conn.commit(); write_state(conn)
            finally:
                conn.close()
        raise
    with locked():
        conn = db()
        try:
            conn.execute("UPDATE instances SET status='destroyed',destroyed_at=?,error=NULL WHERE id=?", (stamp(), iid))
            for warning in warnings:
                event(conn, iid, "cleanup_warning", warning[:2000])
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
    try:
        rows = [dict(x) for x in active_rows(conn)]
        owners = {r['instance_id']: dict(r) for r in conn.execute('SELECT * FROM dispatch_owners')}
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
            and not provision_owner_alive(rec["id"])
        )
        expired = bool(expiry and expiry <= current)
        owner = owners.get(rec['id'])
        # A retained idle session deliberately outlives its dispatcher. A running
        # job does not: process identity is authoritative even after SIGKILL.
        abandoned = bool(owner and rec['status'] == 'running'
                         and process_identity(owner['pid']) != owner['identity'])
        retry_cleanup = rec['status'] == 'releasing'
        if not (expired or stale_creating or retry_cleanup or abandoned):
            continue
        reason = (rec['teardown_reason'] or 'cleanup_retry') if retry_cleanup else (
            'dispatcher_lost' if abandoned else 'stale_creating' if stale_creating else 'ttl_expired')
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
            errors.append({"name": name, "error": "domain_missing"}); continue
        setmem = virsh("setmem", name, f"{target}MiB", "--live", timeout=10, check=False)
        if setmem.returncode:
            errors.append({"name": name, "error": (setmem.stderr or setmem.stdout or "setmem failed").strip()})
        else:
            changed.append({"name": name, "busy": busy, "targetMiB": target})
    return {"changed": changed, "errors": errors}


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    def add_acquire_args(p):
        p.add_argument("--owner", required=True); p.add_argument("--project", default="")
        p.add_argument("--ttl-seconds", type=int, default=3600); p.add_argument("--session-key")
        p.add_argument("--memory-mib", type=int, default=MEM_MIB); p.add_argument("--max-memory-mib", type=int, default=MAX_MEM_MIB)
        p.add_argument("--vcpus", type=int, default=VCPUS); p.add_argument("--disk-gib", type=int, default=DISK_GIB)
    p = sub.add_parser("acquire"); add_acquire_args(p)
    p = sub.add_parser("reserve"); add_acquire_args(p)
    p = sub.add_parser("provision"); p.add_argument("id")
    p = sub.add_parser("finish"); p.add_argument("id"); p.add_argument("--status", default="finished"); p.add_argument("--exit-code", type=int); p.add_argument("--reason", default="job_finished"); p.add_argument("--keep-seconds", type=int, default=0)
    p = sub.add_parser("renew"); p.add_argument("id"); p.add_argument("--ttl-seconds", type=int, required=True)
    p = sub.add_parser("state"); p.add_argument("--history-limit", type=int, default=40)
    sub.add_parser("gc"); sub.add_parser("rebalance-desktops")
    args = parser.parse_args(argv)
    if args.action in ("acquire", "reserve"):
        # The minute timer owns GC. Running teardown on every admission request
        # serializes callers behind unrelated guests and loses the RPC deadline.
        rec, reused = reserve(args.owner, args.project, args.ttl_seconds, args.session_key,
                              args.memory_mib, args.max_memory_mib, args.vcpus, args.disk_gib)
        if args.action == "reserve":
            print(json.dumps({"instance": rec, "reused": reused}, separators=(",", ":"))); return 0
        if reused:
            state = virsh("domstate", rec["name"], timeout=5, check=False)
            if state.returncode:
                raise RuntimeError(f"retained VM {rec['name']} no longer exists")
            if "running" not in (state.stdout or "").lower():
                virsh("start", rec["name"], timeout=30)
        else:
            rec = provision(rec)
        print(json.dumps({**rec, "reused": reused}, separators=(",", ":"))); return 0
    if args.action == "provision":
        conn = db()
        try: row = conn.execute("SELECT * FROM instances WHERE id=?", (args.id,)).fetchone()
        finally: conn.close()
        if not row: raise RuntimeError(f"unknown headless instance {args.id}")
        print(json.dumps(provision(dict(row)), separators=(",", ":"))); return 0
    if args.action == "finish": print(json.dumps(finish(args.id,args.status,args.exit_code,args.reason,args.keep_seconds),separators=(",", ":"))); return 0
    if args.action == "renew": print(json.dumps(renew(args.id,args.ttl_seconds),separators=(",", ":"))); return 0
    if args.action == "state": print(json.dumps(state_data(history_limit=args.history_limit),separators=(",", ":"))); return 0
    if args.action == "gc": print(json.dumps({"destroyed": gc()},separators=(",", ":"))); return 0
    if args.action == "rebalance-desktops": print(json.dumps(rebalance_desktops(),separators=(",", ":"))); return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
