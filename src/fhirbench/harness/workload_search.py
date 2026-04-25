#!/usr/bin/env python3
"""Search workload driver — fires queries.yaml requests concurrently under load.

Reuses the 11-query suite that drives the behavior matrix. Each worker picks
a query uniformly at random per request and fires it until the duration
elapses. Timings + status codes land in the OpLog JSONL for per-query p50/p95/
p99 computation in the report stage.

Why reuse queries.yaml: the behavior matrix's mix is already curated to
exercise the most divergent bits of FHIR (silent-ignore, _total=accurate,
combo-code, operations, bulk). Firing that same mix at scale is what surfaces
where each server's index design holds up vs. falls over at 100K volume.

Not all queries make equal sense at 100K (e.g. Patient_export kicks off a
background job and always returns 202 fast — doesn't measure the server
under load). Use --exclude to drop those.

Usage:
    python -m fhirbench.harness.workload_search \\
        --server hapi --log results/hapi-search.jsonl \\
        --duration 900 --workers 64 --exclude patient_export
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from fhirbench.servers import AuthedSession, find_server, load_servers, resolve_base_url  # noqa: E402
from fhirbench.harness.metrics import OpLog, OpRecord  # noqa: E402
from fhirbench.harness.sample_pool import SamplePool  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"
DEFAULT_QUERIES = REPO_ROOT / "config" / "queries.yaml"


def load_queries(path: Path, exclude: set[str]) -> list[dict]:
    """Return the queries that participate in the load-test workload.

    Filtering is two-layered:
      1. Honor `loadtest: skip:<reason>` in the query definition. The behavior
         matrix (compare.py) tests every query in the file — that's how we
         document "what each server supports." The load workload is a strict
         subset: only queries every server can answer cleanly, so the
         headline ops/s and p99 numbers measure SPEED of supported queries
         rather than re-document known support gaps under load.
      2. Honor the runtime --exclude set (caller can drop queries by name
         even if the YAML doesn't tag them).
    """
    import yaml  # type: ignore
    data = yaml.safe_load(path.read_text())
    items = data.get("queries") if isinstance(data, dict) else []
    out: list[dict] = []
    for q in items:
        name = q.get("name")
        if name in exclude:
            continue
        loadtest_marker = (q.get("loadtest") or "").strip()
        if loadtest_marker.startswith("skip"):
            continue
        out.append(q)
    return out


def execute_query(
    session: AuthedSession, base_url: str, query: dict,
) -> tuple[int, int, bool]:
    method = (query.get("method") or "GET").upper()
    url = f"{base_url}/{(query.get('path') or '').lstrip('/')}"
    params = query.get("params") or {}
    body = query.get("body")
    extra_headers = query.get("headers") or None
    t0 = time.monotonic()
    try:
        if method == "POST":
            resp = session.post(url, params=params, json=body, headers=extra_headers, timeout=60.0)
        else:
            resp = session.get(url, params=params, headers=extra_headers, timeout=60.0)
        ms = int((time.monotonic() - t0) * 1000)
        return ms, resp.status_code, 200 <= resp.status_code < 300
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False


def run(
    server_id: str, servers_path: Path, queries_path: Path, log_path: Path,
    duration: float, workers: int, exclude: set[str],
) -> int:
    servers = load_servers(servers_path)
    server = find_server(servers, server_id)
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{server_id}' has no base_url configured", file=sys.stderr)
        return 2
    queries = load_queries(queries_path, exclude)
    if not queries:
        print("ERROR: no queries to run (all excluded?)", file=sys.stderr)
        return 2

    # Harvest sampling pools once (before timed workload) if any queries
    # declare `sample:` placeholders. Queries whose pools come back empty
    # are dropped with a one-line log so a missing resource type (e.g., no
    # Practitioners loaded) doesn't fail the whole workload.
    pool = SamplePool()
    if any(q.get("sample") for q in queries):
        with httpx.Client(timeout=60.0) as harvest_client:
            harvest_session = AuthedSession(server, harvest_client)
            pool.load(harvest_session, base_url)
        survivors: list[dict] = []
        for q in queries:
            missing = pool.missing_for(q)
            if missing:
                print(f"[sample_pool] dropping '{q.get('name')}' — empty pools: {missing}")
            else:
                survivors.append(q)
        queries = survivors
        if not queries:
            print("ERROR: every query was dropped (no data harvested).", file=sys.stderr)
            return 2

    names = [q.get("name") for q in queries]
    print(f"Search workload on {server_id}: duration={duration}s workers={workers} queries={names}")

    log = OpLog(log_path)
    stop_at = time.monotonic() + duration

    def worker(wid: int) -> None:
        rng = random.Random(wid * 9973 + int(time.time()))
        with httpx.Client(
            timeout=60.0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:
            s = AuthedSession(server, client)
            while time.monotonic() < stop_at:
                q = rng.choice(queries)
                verb = q.get("name", "?")
                if q.get("sample"):
                    q = pool.expand(q, rng)
                started = time.time()
                ms, code, ok = execute_query(s, base_url, q)
                log.record(OpRecord("search", verb, started, ms, code, ok))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, i) for i in range(workers)]
        t_start = time.monotonic()
        while time.monotonic() < stop_at:
            time.sleep(5)
            s = log.summary()
            elapsed = time.monotonic() - t_start
            print(f"  t={elapsed:.0f}s ops={s['total']} ok={s['ok']} err={s['errors']} rate={s['ops_per_s']:.0f}/s")
        for f in futs:
            f.result()

    summary = log.summary()
    log.close()
    print(f"Done. {summary['total']} queries in {summary['elapsed_s']:.1f}s "
          f"({summary['ops_per_s']:.1f}/s), {summary['errors']} errors.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", required=True)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--queries-file", type=Path, default=DEFAULT_QUERIES)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--duration", type=float, default=900.0)
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--exclude", default="patient_export",
                    help="Comma-separated query names to skip (default: patient_export)")
    args = ap.parse_args()
    excl = {s.strip() for s in args.exclude.split(",") if s.strip()}
    return run(
        server_id=args.server, servers_path=args.servers_file, queries_path=args.queries_file,
        log_path=args.log, duration=args.duration, workers=args.workers, exclude=excl,
    )


if __name__ == "__main__":
    sys.exit(main())
