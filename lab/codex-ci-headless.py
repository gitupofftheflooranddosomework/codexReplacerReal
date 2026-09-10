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
BASE = os.environ.get("CODEX_CI_HEADLESS_BASE", "codex-ci-base-v4.qcow2")
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
    str(max(1, ((os.cpu_count() or 1) * 2) // 3)),
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
        parts = [f"rc={exc.returncode}"]
        if exc.stderr:
            parts.append(f"stderr={exc.stderr.strip()}")
        if exc.stdout:
            parts.append(f"stdout={exc.stdout.strip()}")
        return "; ".join(parts)
    return str(exc)


def read_meminfo():
    out = {}
    try:
        for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            value = rest.strip().split()[0]
            out[key] = int(value) // 1024
    except (OSError, ValueError):
        pass
    return out


def read_psi(resource):
    try:
        first = pathlib.Path(f"/proc/pressure/{resource}").read_text().splitlines()[0]
        pairs = dict(x.split("=", 1) for x in first.split()[1:] if "=" in x)
        return float(pairs.get("avg10", "0"))
    except (OSError, ValueError, IndexError):
        return 0.0


def read_load1():
    try:
        return float(pathlib.Path("/proc/loadavg").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def read_arc_bytes():
    try:
        rows = pathlib.Path("/proc/spl/kstat/zfs/arcstats").read_text().splitlines()
    except OSError:
        return 0, 0
    values = {}
    for line in rows:
        parts = line.split()
        if len(parts) >= 3 and parts[0] in {"size", "c_min"}:
            try:
                values[parts[0]] = int(parts[2])
            except ValueError:
                pass
    return values.get("size", 0), values.get("c_min", 0)


def arc_capacity_from_values(size_mib, cmin_mib):
    protected = min(size_mib, max(ARC_FLOOR_MIB, cmin_mib))
    return {"protectedMiB": protected, "reclaimableMiB": max(0, size_mib - protected)}


def arc_capacity():
    size, cmin = read_arc_bytes()
    return arc_capacity_from_values(size // (1024 * 1024), cmin // (1024 * 1024))


def scheduler_workers():
    try:
        import urllib.request
        with urllib.request.urlopen(f"{SCHEDULER}/api/workers", timeout=2) as response:
            payload = json.load(response)
        return payload.get("workers", [])
    except Exception:
        return []


def desktop_target_mib(worker, busy):
    configured = DESKTOP_ACTIVE_MIB if busy else DESKTOP_IDLE_MIB
    total = int(worker.get("memTotalMiB") or 0)
    available = int(worker.get("memAvailableMiB") or 0)
    if busy:
        configured = max(DESKTOP_ACTIVE_FLOOR_MIB, configured)
        if total > 0 and available > 0:
            used = max(0, total - available)
            configured = max(configured, used + DESKTOP_HEADROOM_MIB)
        if total > 0:
            configured = min(configured, total)
    return configured


def rebalance_desktops():
    """Balloon desktop VMs to idle/active targets without touching a leased guest's OS."""
    workers = scheduler_workers()
    changes = []
    for worker in workers:
        name = str(worker.get("name") or "")
        if not re.fullmatch(r"codex-lab-vm-0[1-6]", name):
            continue
        busy = bool(worker.get("schedulerBusy") or worker.get("lease") or worker.get("activeJob"))
        target = desktop_target_mib(worker, busy)
        try:
            info = virsh("dommemstat", name, timeout=10, check=False)
            current = None
            for line in info.stdout.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[0] == "actual":
                    current = int(parts[1]) // 1024
                    break
            if current is None or abs(current - target) < DESKTOP_BALLOON_STEP_MIB:
                continue
            virsh("setmem", name, f"{target}M", "--live", timeout=15)
            changes.append({"name": name, "fromMiB": current, "toMiB": target, "busy": busy})
        except Exception:
            continue
    return changes


def capacity_data(conn):
    mem = read_meminfo()
    arc = arc_capacity()
    active = conn.execute("SELECT memory_mib,max_memory_mib,vcpus,status FROM instances WHERE status IN (?,?,?,?)", ACTIVE).fetchall()
    active_mem = sum(int(row["max_memory_mib"]) + VM_MEMORY_OVERHEAD_MIB for row in active)
    active_vcpus = sum(int(row["vcpus"]) for row in active)
    pending_mem = pending_create_charge_mib(conn)
    host_available = int(mem.get("MemAvailable", 0))
    effective_available = host_available + arc["reclaimableMiB"]
    return {
        "active": len(active),
        "activeHeadlessVcpus": active_vcpus,
        "activeHeadlessChargeMiB": active_mem,
        "pendingCreateChargeMiB": pending_mem,
        "hostMemTotalMiB": int(mem.get("MemTotal", 0)),
        "hostMemAvailableMiB": host_available,
        "hostEffectiveAvailableMiB": effective_available,
        "hostReserveMiB": HOST_RESERVE_MIB,
        "zfsArcSizeMiB": arc["protectedMiB"] + arc["reclaimableMiB"],
        "zfsArcProtectedMiB": arc["protectedMiB"],
        "zfsArcReclaimableMiB": arc["reclaimableMiB"],
        "memoryPsiAvg10": read_psi("memory"),
        "ioPsiAvg10": read_psi("io"),
        "load1": read_load1(),
        "maxHeadlessVcpus": MAX_HEADLESS_VCPUS,
        "maxActive": MAX_ACTIVE,
        "projectMaxActive": PROJECT_MAX,
        "freeSlots": max(0, MAX_ACTIVE - len(active)),
    }


def admission_charge_mib(memory_mib=MEM_MIB):
    return max(int(memory_mib), MEM_MIB) + VM_MEMORY_OVERHEAD_MIB


def active_vcpu_charge(conn):
    return int(conn.execute(
        "SELECT COALESCE(SUM(vcpus),0) FROM instances WHERE status IN (?,?,?,?)", ACTIVE
    ).fetchone()[0] or 0)


def pending_create_charge_mib(conn):
    return int(conn.execute(
        "SELECT COUNT(*) FROM instances WHERE status='creating'"
    ).fetchone()[0] or 0) * admission_charge_mib()


def storage_backoff_seconds(failures):
    failures = max(1, int(failures))
    return min(STORE_BACKOFF_MAX_SECONDS, STORE_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)))


def storage_health_row(conn, store):
    return conn.execute("SELECT * FROM storage_health WHERE storage_root=?", (str(store),)).fetchone()


def storage_health_data(conn):
    rows = []
    current = now()
    for store in STORES:
        row = storage_health_row(conn, store)
        failures = int(row["consecutive_failures"]) if row else 0
        backoff_until = parse_stamp(row["backoff_until"]) if row else None
        retry = max(0, int((backoff_until - current).total_seconds())) if backoff_until and backoff_until > current else 0
        rows.append({
            "storageRoot": str(store),
            "basePresent": (store / BASE).exists(),
            "consecutiveFailures": failures,
            "lastFailureAt": row["last_failure_at"] if row else None,
            "backoffUntil": row["backoff_until"] if row else None,
            "retryInSeconds": retry,
            "lastError": row["last_error"] if row else None,
            "lastSuccessAt": row["last_success_at"] if row else None,
            "status": "backoff" if retry else "healthy",
        })
    return rows


def mark_store_failure(conn, store, error):
    row = storage_health_row(conn, store)
    failures = (int(row["consecutive_failures"]) if row else 0) + 1
    delay = storage_backoff_seconds(failures)
    failed_at = now()
    conn.execute("""INSERT INTO storage_health(storage_root,consecutive_failures,last_failure_at,backoff_until,last_error)
      VALUES(?,?,?,?,?) ON CONFLICT(storage_root) DO UPDATE SET consecutive_failures=excluded.consecutive_failures,
      last_failure_at=excluded.last_failure_at,backoff_until=excluded.backoff_until,last_error=excluded.last_error""",
                 (str(store), failures, stamp(failed_at), stamp(failed_at + timedelta(seconds=delay)), str(error)[:4000]))
    return delay


def mark_store_success(conn, store):
    conn.execute("""INSERT INTO storage_health(storage_root,consecutive_failures,last_failure_at,backoff_until,last_error,last_success_at)
      VALUES(?,0,NULL,NULL,NULL,?) ON CONFLICT(storage_root) DO UPDATE SET consecutive_failures=0,
      last_failure_at=NULL,backoff_until=NULL,last_error=NULL,last_success_at=excluded.last_success_at""", (str(store), stamp()))


def choose_store(conn):
    candidates = []
    current = now()
    for store in STORES:
        base = store / BASE
        if not base.exists():
            continue
        row = storage_health_row(conn, store)
        backoff_until = parse_stamp(row["backoff_until"]) if row else None
        if backoff_until and backoff_until > current:
            continue
        try:
            free = shutil.disk_usage(store).free
        except OSError:
            continue
        candidates.append((free, store))
    if not candidates:
        raise RuntimeError("all headless storage roots are in backoff or missing the base image")
    candidates.sort(reverse=True)
    return candidates[0][1]


def effective_disk_gib(base_size, requested=DISK_GIB):
    base_gib = (int(base_size) + (1024 ** 3) - 1) // (1024 ** 3)
    return max(int(requested), int(base_gib))


def current_domains():
    out = virsh("list", "--all", "--name", check=False).stdout
    return {x.strip() for x in out.splitlines() if x.strip()}


def used_ips():
    conn = db()
    try:
        return {row[0] for row in conn.execute("SELECT ip FROM instances WHERE status IN (?,?,?,?)", ACTIVE)}
    finally:
        conn.close()


def next_identity(iid, conn):
    domains = current_domains()
    existing_ips = {row[0] for row in conn.execute("SELECT ip FROM instances WHERE status IN (?,?,?,?)", ACTIVE)}
    for last in range(IP_FIRST, IP_LAST + 1):
        name = f"ci-{slug(iid)}-{iid[:8]}"
        ip = f"192.168.122.{last}"
        mac = f"52:54:00:ce:00:{last:02x}"
        if name not in domains and ip not in existing_ips:
            return name, ip, mac
    raise RuntimeError("no free headless CI identity")


def reserve(owner, project=None, session_key=None, ttl=180, memory=MEM_MIB, max_memory=MAX_MEM_MIB, vcpus=VCPUS, disk=DISK_GIB):
    conn = db()
    try:
        if session_key:
            row = conn.execute("SELECT * FROM instances WHERE project=? AND session_key=? AND status IN ('running','idle') ORDER BY created_at DESC LIMIT 1", (project, session_key)).fetchone()
            if row:
                return dict(row), True
        with locked():
            gc(conn=conn)
            cap = capacity_data(conn)
            if cap["active"] >= MAX_ACTIVE:
                raise RuntimeError("headless active VM limit reached")
            if project and conn.execute("SELECT COUNT(*) FROM instances WHERE project=? AND status IN (?,?,?,?)", (project, *ACTIVE)).fetchone()[0] >= PROJECT_MAX:
                raise RuntimeError("headless project VM limit reached")
            if cap["activeHeadlessVcpus"] + int(vcpus) > MAX_HEADLESS_VCPUS:
                raise RuntimeError("headless vCPU admission limit reached")
            projected = cap["hostEffectiveAvailableMiB"] - cap["pendingCreateChargeMiB"] - admission_charge_mib(max_memory)
            if projected < HOST_RESERVE_MIB:
                raise RuntimeError("headless host memory reserve would be violated")
            if cap["memoryPsiAvg10"] > MAX_MEMORY_PSI_AVG10:
                raise RuntimeError("headless host memory pressure too high")
            if cap["ioPsiAvg10"] > MAX_IO_PSI_AVG10:
                raise RuntimeError("headless host IO pressure too high")
            if cap["load1"] > max(4.0, (os.cpu_count() or 1) * MAX_LOAD_PER_CPU):
                raise RuntimeError("headless host load too high")
            iid = os.urandom(16).hex()
            name, ip, mac = next_identity(iid, conn)
            expires = stamp(now() + timedelta(minutes=max(5, int(ttl))))
            conn.execute("""INSERT INTO instances(id,name,ip,mac,owner,project,session_key,storage_root,memory_mib,max_memory_mib,vcpus,disk_gib,created_at,expires_at,status)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (iid, name, ip, mac, owner, project, session_key, "", int(memory), int(max_memory), int(vcpus), int(disk), stamp(), expires, "creating"))
            conn.commit()
            return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()), False
    finally:
        conn.close()


def provision(rec):
    iid = rec["id"]
    conn = db()
    try:
        with provision_marker(iid):
            with provision_locked():
                row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
                if not row or row["status"] != "creating":
                    raise RuntimeError(f"instance {iid} is no longer in creating state")
                store = choose_store(conn)
                base = store / BASE
                base_size = base.stat().st_size
                disk_gib = effective_disk_gib(base_size, int(row["disk_gib"]))
                vm_dir = store / row["name"]
                disk_path = vm_dir / f"{row['name']}.qcow2"
                vm_dir.mkdir(parents=True, exist_ok=False)
                try:
                    run(["qemu-img", "create", "-q", "-f", "qcow2", "-F", "qcow2", "-b", str(base), str(disk_path), f"{disk_gib}G"], timeout=60)
                    virsh("net-update", NETWORK, "add", "ip-dhcp-host",
                          f"<host mac='{row['mac']}' name='{row['name']}' ip='{row['ip']}'/>", "--live", "--config")
                    run(["virt-install", "--connect", URI, "--name", row["name"],
                         "--memory", f"memory={row['memory_mib']},maxmemory={row['max_memory_mib']}",
                         "--vcpus", str(row["vcpus"]), "--cpu", "host-passthrough",
                         "--disk", f"path={disk_path},format=qcow2,bus=virtio",
                         "--network", f"network={NETWORK},model=virtio,mac={row['mac']}",
                         "--os-variant", "debian11", "--graphics", "none", "--noautoconsole", "--import"], timeout=120)
                except Exception as exc:
                    virsh("destroy", row["name"], check=False)
                    virsh("undefine", row["name"], check=False)
                    virsh("net-update", NETWORK, "delete", "ip-dhcp-host",
                          f"<host mac='{row['mac']}' name='{row['name']}' ip='{row['ip']}'/>", "--live", "--config", check=False)
                    shutil.rmtree(vm_dir, ignore_errors=True)
                    delay = mark_store_failure(conn, store, process_error(exc))
                    conn.commit()
                    raise RuntimeError(f"headless provision failed on {store}; retry_after={delay}s; {process_error(exc)}") from exc
                mark_store_success(conn, store)
                conn.execute("UPDATE instances SET storage_root=?,disk_gib=?,started_at=?,status='running' WHERE id=?",
                             (str(store), disk_gib, stamp(), iid))
                conn.execute("INSERT INTO events(instance_id,at,event,detail) VALUES(?,?,?,?)",
                             (iid, stamp(), "provision_enter", str(store)))
                conn.commit()
        return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())
    except Exception as exc:
        conn.execute("UPDATE instances SET status='failed',finished_at=?,error=? WHERE id=? AND status='creating'",
                     (stamp(), str(exc)[:4000], iid))
        conn.commit()
        raise
    finally:
        conn.close()


def finish(iid, status="finished", exit_code=None, reason="job_finished", keep=0):
    conn = db()
    try:
        row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
        if not row:
            return None
        rec = dict(row)
        conn.execute("UPDATE instances SET status='releasing',finished_at=?,exit_code=?,teardown_reason=? WHERE id=?",
                     (stamp(), exit_code, reason, iid))
        conn.commit()
        if keep:
            expires = stamp(now() + timedelta(minutes=max(5, int(keep))))
            conn.execute("UPDATE instances SET status='idle',expires_at=? WHERE id=?", (expires, iid))
            conn.commit()
            return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())
        with provision_locked():
            virsh("destroy", rec["name"], check=False)
            virsh("undefine", rec["name"], check=False)
            virsh("net-update", NETWORK, "delete", "ip-dhcp-host",
                  f"<host mac='{rec['mac']}' name='{rec['name']}' ip='{rec['ip']}'/>", "--live", "--config", check=False)
            if rec["storage_root"]:
                shutil.rmtree(pathlib.Path(rec["storage_root"]) / rec["name"], ignore_errors=True)
        conn.execute("UPDATE instances SET status=?,destroyed_at=? WHERE id=?", (status, stamp(), iid))
        conn.commit()
        return dict(conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone())
    finally:
        conn.close()


def gc(conn=None):
    own = conn is None
    conn = conn or db()
    try:
        expired = []
        current = now()
        for row in conn.execute("SELECT * FROM instances WHERE status IN ('running','idle','creating')").fetchall():
            rec = dict(row)
            if rec["status"] == "creating":
                created = parse_stamp(rec["created_at"])
                if created and (current - created).total_seconds() > STALE_CREATING_SECONDS and not provision_owner_alive(rec["id"]):
                    finish(rec["id"], status="failed", reason="stale_creating")
                    expired.append(rec["id"])
                continue
            expires = parse_stamp(rec["expires_at"])
            if expires and expires <= current:
                finish(rec["id"], status="expired", reason="ttl_expired")
                expired.append(rec["id"])
        return expired
    finally:
        if own:
            conn.close()


def list_instances(limit=100):
    conn = db()
    try:
        return [dict(x) for x in conn.execute("SELECT * FROM instances ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()]
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    acquire = sub.add_parser("acquire")
    acquire.add_argument("--owner", required=True)
    acquire.add_argument("--project")
    acquire.add_argument("--session-key")
    acquire.add_argument("--ttl", type=int, default=180)
    acquire.add_argument("--memory", type=int, default=MEM_MIB)
    acquire.add_argument("--max-memory", type=int, default=MAX_MEM_MIB)
    acquire.add_argument("--vcpus", type=int, default=VCPUS)
    acquire.add_argument("--disk", type=int, default=DISK_GIB)
    release = sub.add_parser("release")
    release.add_argument("id")
    release.add_argument("--status", default="finished")
    release.add_argument("--exit-code", type=int)
    release.add_argument("--reason", default="job_finished")
    release.add_argument("--keep", type=int, default=0)
    sub.add_parser("gc")
    sub.add_parser("list")
    sub.add_parser("capacity")
    sub.add_parser("rebalance")
    args = parser.parse_args(argv)
    if args.cmd == "acquire":
        rec, reused = reserve(args.owner, args.project, args.session_key, args.ttl, args.memory, args.max_memory, args.vcpus, args.disk)
        if not reused:
            rec = provision(rec)
        print(json.dumps({**rec, "reused": reused}, separators=(",", ":")))
        return 0
    if args.cmd == "release":
        print(json.dumps(finish(args.id, args.status, args.exit_code, args.reason, args.keep), separators=(",", ":")))
        return 0
    if args.cmd == "gc":
        print(json.dumps({"released": gc()}, separators=(",", ":")))
        return 0
    if args.cmd == "list":
        print(json.dumps(list_instances(), separators=(",", ":")))
        return 0
    if args.cmd == "capacity":
        conn = db()
        try:
            payload = capacity_data(conn)
            payload["storageHealth"] = storage_health_data(conn)
        finally:
            conn.close()
        print(json.dumps(payload, separators=(",", ":")))
        return 0
    if args.cmd == "rebalance":
        print(json.dumps({"changes": rebalance_desktops()}, separators=(",", ":")))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
