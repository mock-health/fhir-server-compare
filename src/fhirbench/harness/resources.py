#!/usr/bin/env python3
"""Docker-stats sampler: write a 1Hz per-container CSV while a workload runs.

The Docker stats JSON output is human-formatted strings ("1.683GiB / 186.3GiB",
"0.13%", etc). This module parses them into numbers and appends rows to CSV,
so reporting can chart CPU + RSS timeseries and compute peak/area metrics.

Used as a context manager around each workload so start/stop is paired:

    from fhirbench.harness.resources import ResourceSampler
    with ResourceSampler(containers, out_path):
        run_workload()

Stats sampling itself is effectively free (docker CLI subprocess once per
second), so the overhead is well under 1% CPU on the host.

Also exposes `snapshot_disk(out_path)` which runs `docker system df -v` once
to capture final volume sizes.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

SIZE_RE = re.compile(r"^\s*([0-9.]+)\s*([a-zA-Z]+)?\s*$")
UNIT_MULT = {
    "B": 1, "KB": 1_000, "MB": 1_000_000, "GB": 1_000_000_000, "TB": 1_000_000_000_000,
    "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4,
    "K": 1_000, "M": 1_000_000, "G": 1_000_000_000, "T": 1_000_000_000_000,
}


def parse_size(s: str) -> float:
    """'1.683GiB' -> 1806931128.32. 'nan' -> 0. Empty -> 0."""
    if not s:
        return 0.0
    m = SIZE_RE.match(s)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = (m.group(2) or "B").upper()
    return val * UNIT_MULT.get(unit, 1)


def parse_pct(s: str) -> float:
    """'0.13%' -> 0.13. Empty -> 0."""
    if not s:
        return 0.0
    return float(s.rstrip("%"))


def parse_pair(s: str) -> tuple[float, float]:
    """'1.683GiB / 186.3GiB' -> (used_bytes, total_bytes). '3.85MB / 9.55MB' -> (rx, tx)."""
    if not s or "/" not in s:
        return 0.0, 0.0
    a, b = s.split("/", 1)
    return parse_size(a.strip()), parse_size(b.strip())


def existing_containers(requested: list[str]) -> list[str]:
    """Filter `requested` down to the ones currently known to docker.

    docker stats errors out entirely if ANY name is missing, so callers
    shouldn't blindly pass the intended list — a missing sidecar turns
    sampling into a silent no-op.
    """
    if not requested:
        return []
    try:
        proc = subprocess.run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            capture_output=True, text=True, check=False, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return []
    live = set(proc.stdout.split())
    return [c for c in requested if c in live]


def sample_once(containers: list[str]) -> list[dict]:
    """One docker-stats read for the named containers. Returns list of rows."""
    if not containers:
        return []
    try:
        proc = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "json", *containers],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return []
    out: list[dict] = []
    ts = time.time()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except Exception:
            continue
        mem_used, mem_limit = parse_pair(raw.get("MemUsage", ""))
        net_rx, net_tx = parse_pair(raw.get("NetIO", ""))
        blk_r, blk_w = parse_pair(raw.get("BlockIO", ""))
        out.append({
            "ts": ts,
            "container": raw.get("Name") or raw.get("Container", ""),
            "cpu_pct": parse_pct(raw.get("CPUPerc", "")),
            "mem_bytes": mem_used,
            "mem_pct": parse_pct(raw.get("MemPerc", "")),
            "net_rx_bytes": net_rx,
            "net_tx_bytes": net_tx,
            "block_read_bytes": blk_r,
            "block_write_bytes": blk_w,
            "pids": int(raw.get("PIDs") or 0),
        })
    return out


CSV_FIELDS = [
    "ts", "container", "cpu_pct", "mem_bytes", "mem_pct",
    "net_rx_bytes", "net_tx_bytes", "block_read_bytes", "block_write_bytes", "pids",
]


class ResourceSampler:
    """Samples docker stats at 1Hz into a CSV while the context is open.

    Missing / stopped containers show up as absent rows — no error. That way
    a sampler running across a server restart keeps working on the survivors.
    """

    def __init__(self, containers: Iterable[str], out_path: Path, interval_s: float = 1.0):
        requested = list(containers)
        self.containers = existing_containers(requested)
        missing = set(requested) - set(self.containers)
        if missing:
            print(f"  [resources] containers not found, skipping: {sorted(missing)}")
        self.out_path = Path(out_path)
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "ResourceSampler":
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        # Open append so a rerun doesn't clobber prior samples; caller controls rotation.
        new_file = not self.out_path.exists()
        self._fh = self.out_path.open("a", newline="", buffering=1)
        self._writer = csv.DictWriter(self._fh, fieldnames=CSV_FIELDS)
        if new_file:
            self._writer.writeheader()
        self._thread = threading.Thread(target=self._loop, name="resource-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._fh.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            start = time.monotonic()
            for row in sample_once(self.containers):
                self._writer.writerow(row)
            # sleep the remainder of the interval, not a fixed 1s — so a slow
            # docker-stats call doesn't drift the sampling cadence.
            elapsed = time.monotonic() - start
            if self._stop.wait(max(0.05, self.interval_s - elapsed)):
                return


def snapshot_disk(out_path: Path) -> None:
    """One-shot `docker system df -v` dump (JSON if docker supports it, else text)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["docker", "system", "df", "-v", "--format", "json"],
            capture_output=True, text=True, check=False, timeout=30,
        )
        content = proc.stdout or proc.stderr
    except Exception as exc:
        content = f"error: {exc}"
    out_path.write_text(content)


def main() -> int:
    """CLI harness: sample for N seconds, useful for smoke-testing.

    Example:
        python -m fhirbench.harness.resources --containers fhir-compare-hapi --duration 5 --out /tmp/r.csv
    """
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--containers", nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--duration", type=float, default=5.0)
    args = ap.parse_args()
    with ResourceSampler(args.containers, args.out):
        time.sleep(args.duration)
    print(f"Wrote samples for {args.duration}s to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
