#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
source = (ROOT / "codex-ci-headless-gc.timer").read_text()

required = (
    "[Timer]",
    "OnActiveSec=1min",
    "OnBootSec=1min",
    "OnUnitActiveSec=1min",
    "AccuracySec=10s",
    "Persistent=true",
    "WantedBy=timers.target",
)
for marker in required:
    assert marker in source, marker

# The timer-relative bootstrap is the regression guard: restarting this unit
# long after boot must still have a future first deadline. OnUnitActiveSec keeps
# the one-minute cadence after the GC service has fired.
assert source.index("OnActiveSec=1min") < source.index("OnUnitActiveSec=1min")
assert source.count("OnActiveSec=") == 1
assert source.count("OnUnitActiveSec=") == 1

print("headless_gc_timer_test=ok")
