#!/usr/bin/env python3
import importlib.util
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="codex-dashboard-metrics-") as tmp:
        db_path = Path(tmp) / "jobs.sqlite3"
        # Simulate a pre-attribution database so db() must migrate it in place.
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE jobs (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              started_at TEXT, finished_at TEXT, owner TEXT NOT NULL, project TEXT,
              job_class TEXT NOT NULL, command TEXT NOT NULL, cwd TEXT NOT NULL,
              env_json TEXT NOT NULL, timeout_seconds INTEGER NOT NULL, repo_url TEXT,
              revision TEXT, status TEXT NOT NULL, station INTEGER, unit_name TEXT,
              exit_code INTEGER, error TEXT
            )
        """)
        now = datetime.now(timezone.utc)
        created = now - timedelta(seconds=4)
        started = created + timedelta(milliseconds=250)
        finished = started + timedelta(seconds=2)
        conn.execute(
            "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy", created.isoformat(), finished.isoformat(), started.isoformat(), finished.isoformat(),
                "TestBot", "dashboard-test", "test", "printf secret-command", "/workspace",
                '{"SECRET":"do-not-expose"}', 60, None, None, "succeeded", 1, None, 0, None,
            ),
        )
        conn.commit(); conn.close()
        os.environ["CODEX_LAB_SCHEDULER_DB"] = str(db_path)
        os.environ["CODEX_LAB_DASHBOARD_AUTH"] = str(Path(tmp) / "auth.json")
        spec = importlib.util.spec_from_file_location("sched_metrics", Path(__file__).with_name("scheduler.py"))
        sched = importlib.util.module_from_spec(spec); spec.loader.exec_module(sched)
        conn = sched.db()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert {"chat_label", "chat_url", "requested_station"}.issubset(columns)
        usage_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "worker_usage" in usage_tables
        backfill = conn.execute("SELECT * FROM worker_usage WHERE kind='job' AND ref_id='legacy'").fetchone()
        assert backfill is not None and backfill["station"] == 1 and backfill["owner"] == "TestBot"
        row = conn.execute("SELECT * FROM jobs WHERE id='legacy'").fetchone()
        public = sched.public_job(row)
        assert public["owner"] == "TestBot"
        assert "command" not in public and "env" not in public and "env_json" not in public
        metrics = sched.scheduler_metrics(conn, now.timestamp())
        conn.close()
        assert len(metrics["history"]) == 30
        assert metrics["windows"]["5m"]["completed"] == 1
        assert metrics["windows"]["5m"]["succeeded"] == 1
        assert metrics["windows"]["5m"]["avgQueueMs"] == 250.0
        assert metrics["windows"]["5m"]["avgRuntimeMs"] == 2000.0
        html = sched.dashboard_html({"csrf": "test-csrf"})
        assert "Scheduler health &amp; throughput" not in html  # literal HTML, not escaped
        assert "Scheduler health & throughput" in html
        assert 'id="throughput-svg"' in html
        assert "Cancel job" in html and "Restart browser" in html and "Release lease" in html and "Job logs" in html
        assert "Live six-VM status & controls" not in html
        assert "Six KVM workstations" in html and "Run job on VM 1" in html and "Recent use" in html
        assert 'id="submit-modal"' in html and 'id="history-modal"' in html
        assert "const csrf=\"test-csrf\"" in html
        print('{"ok":true,"migration":true,"historyBackfill":true,"metrics":true,"publicStateRedacted":true,"svg":true,"integratedControls":true}')


if __name__ == "__main__":
    main()
