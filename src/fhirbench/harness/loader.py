#!/usr/bin/env python3
"""Concurrent transaction-bundle loader for the load test.

For each patient bundle in `data/loadtest/fhir/*.json`, POST the bundle to the
target server and record a JSONL result line: bundle path, wall-clock latency,
HTTP status, count of 2xx entries in the transaction response, error summary.

Concurrency is thread-based because httpx releases the GIL during network I/O.
Each worker owns its own `httpx.Client` for connection-pool locality.

Resume: on start, read the output log; bundles already present with any
recorded HTTP status are skipped. That means Stage 2 can pick up where Stage 1
left off without re-posting the first 1K patients. To force a re-run, delete
or rotate the log file.

Bundle size filter: by default, bundles above `--max-bundle-bytes` (30 MB)
are dropped before the timed ingest. Rationale: across the current roster,
30 MB is the smallest COMMON body-size cap — Spark hardcodes exactly
30,000,000 bytes, MS FHIR defaulted to 30 MB before overlay tuning, Firely
commercial caps there. Feeding bundles above that threshold just contaminates
the throughput numbers with "some servers reject, some don't" noise that has
nothing to do with per-request server speed. The filter makes the benchmark
apples-to-apples on the common workload. A skip count is logged so the
dropped tail is visible. Set `--max-bundle-bytes 0` to disable and revert
to "send everything, measure rejections."

Usage:
    python -m fhirbench.harness.loader --server hapi --workers 32 --log results/hapi.jsonl
    python -m fhirbench.harness.loader --server hapi --limit 1000 --log results/hapi-stage1.jsonl
    python -m fhirbench.harness.loader --server hapi --offset 100000 --limit 1000 --log ...
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import httpx

# allow `python src/fhirbench/harness/loader.py` as well as `python -m fhirbench.harness.loader`
from fhirbench.servers import AuthedSession, find_server, load_servers, resolve_base_url  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "loadtest" / "fhir"
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"


@dataclass
class BundleResult:
    bundle: str               # filename only
    started_at: float         # unix ts, seconds
    duration_ms: int
    status_code: int          # 0 on network error
    entries_sent: int
    entries_2xx: int
    entries_4xx: int
    entries_5xx: int
    error: str | None         # one-line summary on failure


def summarize_transaction_response(resp_json: dict | None) -> tuple[int, int, int]:
    """Count 2xx/4xx/5xx across entry[].response.status in a transaction response.

    FHIR transactions return a Bundle of type transaction-response; each entry
    has a `.response.status` like "201 Created" or "400 Bad Request". Outer
    2xx means the SERVER accepted the transaction shape, but individual
    entries can still fail on Aidbox-style batch semantics vs transaction
    semantics. Most servers treat transaction as all-or-nothing (any 4xx
    fails the whole bundle), but counting per-entry makes the loader robust
    to either mode.
    """
    if not isinstance(resp_json, dict):
        return 0, 0, 0
    entries = resp_json.get("entry") or []
    ok = fail4 = fail5 = 0
    for e in entries:
        status = (e.get("response") or {}).get("status", "")
        first = status.split()[0] if status else ""
        if first.startswith("2"):
            ok += 1
        elif first.startswith("4"):
            fail4 += 1
        elif first.startswith("5"):
            fail5 += 1
    return ok, fail4, fail5


def post_one(session: AuthedSession, base_url: str, bundle_path: Path) -> BundleResult:
    """POST a single transaction bundle. Never raises — failures end up in the result."""
    started = time.time()
    t0 = time.monotonic()
    try:
        raw = bundle_path.read_bytes()
    except Exception as exc:
        return BundleResult(
            bundle=bundle_path.name, started_at=started,
            duration_ms=int((time.monotonic() - t0) * 1000),
            status_code=0, entries_sent=0, entries_2xx=0, entries_4xx=0, entries_5xx=0,
            error=f"read_error: {exc}",
        )
    try:
        bundle = json.loads(raw)
    except Exception as exc:
        return BundleResult(
            bundle=bundle_path.name, started_at=started,
            duration_ms=int((time.monotonic() - t0) * 1000),
            status_code=0, entries_sent=0, entries_2xx=0, entries_4xx=0, entries_5xx=0,
            error=f"parse_error: {exc}",
        )
    entries_sent = len(bundle.get("entry") or [])
    try:
        resp = session.post(base_url, content=raw, timeout=1500.0)
    except httpx.RequestError as exc:
        return BundleResult(
            bundle=bundle_path.name, started_at=started,
            duration_ms=int((time.monotonic() - t0) * 1000),
            status_code=0, entries_sent=entries_sent, entries_2xx=0, entries_4xx=0, entries_5xx=0,
            error=f"network_error: {exc.__class__.__name__}: {exc}",
        )
    duration_ms = int((time.monotonic() - t0) * 1000)
    ok = fail4 = fail5 = 0
    err: str | None = None
    if 200 <= resp.status_code < 300:
        try:
            ok, fail4, fail5 = summarize_transaction_response(resp.json())
        except Exception:
            ok = entries_sent
    else:
        err = resp.text[:500].replace("\n", " ")
    return BundleResult(
        bundle=bundle_path.name, started_at=started,
        duration_ms=duration_ms, status_code=resp.status_code,
        entries_sent=entries_sent, entries_2xx=ok, entries_4xx=fail4, entries_5xx=fail5,
        error=err,
    )


def load_already_done(log_path: Path) -> set[str]:
    """Return filenames already SUCCESSFULLY ingested (for resume).

    Only 2xx records count as done. A bundle that previously got a network
    error (status_code=0) or a 4xx/5xx is intentionally retried — otherwise
    a transient blip mid-overnight silently shrinks the dataset and makes
    every downstream throughput number wrong.

    Prereq-phase records are excluded by name so a patient bundle that
    happens to share a filename with a prereq isn't mis-classified.
    """
    if not log_path.exists():
        return set()
    done: set[str] = set()
    with log_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if "bundle" not in rec or rec.get("phase") == "prereq":
                continue
            status = rec.get("status_code", 0)
            if 200 <= status < 300:
                done.add(rec["bundle"])
    return done


def load_prereqs(
    server: dict,
    prereq_dir: Path,
    log_path: Path,
) -> int:
    """POST every *.json in prereq_dir serially, before the main ingest starts.

    Synthea patient bundles use conditional references like
    `Practitioner?identifier=...` that 404 until the practitioner/hospital
    bundles have been loaded. Those two bundles are small (one per Synthea
    run) — a single-threaded load is plenty, and avoids interleaving with
    the timed ingest phase.

    Idempotent on repeat: the prereq bundles use `ifNoneExist` conditional
    creates, so re-posting is a no-op on compliant servers. Any rejection
    here is logged but not fatal.
    """
    base_url = resolve_base_url(server)
    files = sorted(prereq_dir.glob("*.json"))
    if not files:
        print(f"  [prereqs] none found in {prereq_dir} (skipping)")
        return 0
    print(f"  [prereqs] loading {len(files)} bundle(s) from {prereq_dir} before timed ingest")
    writer = _LogWriter(log_path)
    writer.write({"event": "prereq_start", "count": len(files), "started_at": time.time()})
    with httpx.Client(timeout=600.0) as client:
        session = AuthedSession(server, client)
        for f in files:
            res = post_one(session, base_url, f)
            writer.write({**asdict(res), "phase": "prereq"})
            print(f"    {f.name}: {res.status_code} in {res.duration_ms}ms "
                  f"({res.entries_2xx}/{res.entries_sent} entries ok)")
    writer.write({"event": "prereq_end", "ended_at": time.time()})
    writer.close()
    return len(files)


def select_bundles(
    input_dir: Path,
    offset: int,
    limit: int | None,
    max_bytes: int | None = None,
) -> tuple[list[Path], int]:
    """Deterministically select the bundle slice for this stage.

    Sorted filename ordering. Offset skips the first N, limit caps the rest.
    Synthea names bundles like `Firstname_Lastname_<uuid>.json`, so sort is
    effectively random-but-stable.

    If `max_bytes` is set, bundles whose file size is >= max_bytes are
    dropped BEFORE the offset/limit windowing is applied, so every slice of
    the dataset sees the same filter. Returns (selected, skipped_count).
    """
    all_bundles = sorted(p for p in input_dir.glob("*.json"))
    skipped = 0
    if max_bytes and max_bytes > 0:
        kept: list[Path] = []
        for p in all_bundles:
            try:
                if p.stat().st_size < max_bytes:
                    kept.append(p)
                else:
                    skipped += 1
            except OSError:
                kept.append(p)  # don't silently drop on stat errors
        all_bundles = kept
    sliced = all_bundles[offset:]
    if limit is not None:
        sliced = sliced[:limit]
    return sliced, skipped


class _LogWriter:
    """Thread-safe JSONL appender. One handle + one mutex."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", buffering=1)  # line-buffered
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        line = json.dumps(record, separators=(",", ":")) + "\n"
        with self._lock:
            self._fh.write(line)

    def close(self) -> None:
        self._fh.close()


def run(
    server_id: str,
    servers_path: Path,
    input_dir: Path,
    log_path: Path,
    workers: int,
    offset: int,
    limit: int | None,
    prereq_dir: Path | None = None,
    progress_every: int = 50,
    max_bundle_bytes: int | None = 30_000_000,
) -> int:
    servers = load_servers(servers_path)
    server = find_server(servers, server_id)
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{server_id}' has no base_url configured", file=sys.stderr)
        return 2

    if prereq_dir is not None and prereq_dir.exists():
        load_prereqs(server, prereq_dir, log_path)

    bundles, skipped_big = select_bundles(input_dir, offset, limit, max_bundle_bytes)
    if not bundles:
        print(f"ERROR: no bundles found in {input_dir} at offset={offset} limit={limit}", file=sys.stderr)
        return 2

    done = load_already_done(log_path)
    todo = [b for b in bundles if b.name not in done]
    filter_note = (
        f", skipped {skipped_big} >= {max_bundle_bytes}B"
        if max_bundle_bytes and skipped_big
        else ""
    )
    print(
        f"Loader {server_id}: {len(bundles)} selected ({offset=}, {limit=}{filter_note}), "
        f"{len(done)} already logged, {len(todo)} to POST, workers={workers}"
    )
    if not todo:
        print("Nothing to do (all selected bundles already logged).")
        return 0

    writer = _LogWriter(log_path)
    writer.write({
        "event": "run_start",
        "server": server_id,
        "base_url": base_url,
        "started_at": time.time(),
        "workers": workers,
        "offset": offset,
        "limit": limit,
        "selected": len(bundles),
        "resume_skipped": len(done),
        "todo": len(todo),
        "max_bundle_bytes": max_bundle_bytes,
        "skipped_oversize": skipped_big,
    })

    # One session per thread, so connection pools don't thrash and each
    # worker owns its own token cache for refresh-on-401.
    session_pool: dict[int, AuthedSession] = {}
    client_pool: dict[int, httpx.Client] = {}

    def get_session() -> AuthedSession:
        tid = threading.get_ident()
        if tid not in session_pool:
            c = httpx.Client(timeout=1500.0, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
            client_pool[tid] = c
            session_pool[tid] = AuthedSession(server, c)
        return session_pool[tid]

    t_run = time.monotonic()
    completed = 0
    entries_ok_total = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = []
        for bundle in todo:
            def job(bp: Path = bundle) -> BundleResult:
                return post_one(get_session(), base_url, bp)
            futures.append(ex.submit(job))

        for fut in as_completed(futures):
            res = fut.result()
            writer.write(asdict(res))
            completed += 1
            entries_ok_total += res.entries_2xx
            if not (200 <= res.status_code < 300):
                errors += 1
            if completed % progress_every == 0 or completed == len(todo):
                elapsed = time.monotonic() - t_run
                rate = completed / elapsed if elapsed > 0 else 0
                eta = (len(todo) - completed) / rate if rate > 0 else 0
                print(
                    f"  [{completed}/{len(todo)}] {rate:.1f} bundles/s, "
                    f"{entries_ok_total} resources accepted, {errors} errors, "
                    f"ETA {eta/60:.1f} min"
                )

    for c in client_pool.values():
        c.close()

    elapsed = time.monotonic() - t_run
    bundles_per_s = completed / elapsed if elapsed > 0 else 0
    resources_per_s = entries_ok_total / elapsed if elapsed > 0 else 0
    writer.write({
        "event": "run_end",
        "server": server_id,
        "ended_at": time.time(),
        "elapsed_s": elapsed,
        "bundles_completed": completed,
        "errors": errors,
        "entries_2xx_total": entries_ok_total,
        "bundles_per_s": bundles_per_s,
        "resources_per_s": resources_per_s,
    })
    writer.close()
    print(
        f"Done. {completed} bundles in {elapsed:.1f}s "
        f"({bundles_per_s:.1f} bundles/s, {resources_per_s:.0f} resources/s), "
        f"{errors} HTTP errors."
    )
    return 0 if errors == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", required=True)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    ap.add_argument("--log", type=Path, required=True, help="JSONL output log path")
    ap.add_argument("--workers", type=int, default=32)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--prereq-dir", type=Path, default=None,
                    help="Optional: directory of prerequisite bundles to POST first (hospitalInformation, practitionerInformation)")
    ap.add_argument("--progress-every", type=int, default=50)
    ap.add_argument("--max-bundle-bytes", type=int, default=30_000_000,
                    help="Drop bundles at or above this file size before ingest "
                    "(default: 30_000_000 = Spark's hardcoded cap, the smallest "
                    "common ceiling in the roster). Pass 0 to disable.")
    args = ap.parse_args()
    max_bytes = args.max_bundle_bytes if args.max_bundle_bytes > 0 else None
    return run(
        server_id=args.server,
        servers_path=args.servers_file,
        input_dir=args.input_dir,
        log_path=args.log,
        workers=args.workers,
        offset=args.offset,
        limit=args.limit,
        prereq_dir=args.prereq_dir,
        progress_every=args.progress_every,
        max_bundle_bytes=max_bytes,
    )


if __name__ == "__main__":
    sys.exit(main())
