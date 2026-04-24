"""Shared workload timing helpers: op record, percentile math, JSONL logger.

Both workload_crud and workload_search emit per-op records through this
module so the report stage has a uniform shape to aggregate.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class OpRecord:
    workload: str        # 'crud' | 'search'
    verb: str            # 'R' | 'C' | 'U' | 'D' | query name
    started_at: float
    duration_ms: int
    status_code: int     # 0 on network error
    ok: bool             # 2xx
    note: str | None = None


class OpLog:
    """Thread-safe JSONL op-record writer + in-memory running counters.

    Opens in truncate mode: workloads (CRUD, Search) are stateless and a
    re-run with the same log path should overwrite, not append. Otherwise
    the report's elapsed_s = max(started_at) - min(started_at) spans both
    runs and produces a misleading ops/s number.
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("w", buffering=1)
        self._lock = threading.Lock()
        self.total = 0
        self.ok = 0
        self.errors = 0
        self.started_at = time.monotonic()

    def record(self, rec: OpRecord) -> None:
        line = json.dumps(asdict(rec), separators=(",", ":")) + "\n"
        with self._lock:
            self._fh.write(line)
            self.total += 1
            if rec.ok:
                self.ok += 1
            else:
                self.errors += 1

    def summary(self) -> dict:
        elapsed = time.monotonic() - self.started_at
        return {
            "elapsed_s": elapsed,
            "total": self.total,
            "ok": self.ok,
            "errors": self.errors,
            "ops_per_s": self.total / elapsed if elapsed > 0 else 0,
        }

    def close(self) -> None:
        self._fh.close()


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile. q in [0, 100]. Empty -> 0."""
    if not values:
        return 0.0
    s = sorted(values)
    if q <= 0:
        return s[0]
    if q >= 100:
        return s[-1]
    k = (len(s) - 1) * (q / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac
