#!/usr/bin/env python3
"""Aidbox search-index bootstrap — CREATE INDEX on each resource table.

Aidbox community edition (2603) ships with zero Postgres indexes on the
per-resource tables beyond the primary key. Every FHIR search translates to a
uniform `WHERE resource @> '<jsonb>'` predicate, so without a GIN index the
query planner has no choice but `Seq Scan on <resource>` — costing ~12s per
search on a 63K-patient corpus (~1.9M Observation rows). This is what produced
our ramp-50k aidbox search p90 of 13.5s at 1K, climbing to 56s / 94% errors
at 64K.

The fix is one `CREATE INDEX ... USING gin (resource jsonb_path_ops)` per
resource table whose search queries are benchmarked. `jsonb_path_ops` is the
exact opclass the `@>` operator uses, and it's about 30% smaller on disk
than the default `jsonb_ops` because it only supports `@>`.

We POST `CREATE INDEX` through aidbox's documented `/$sql` admin endpoint —
the same endpoint aidbox's own console uses. No back-door, no shell exec into
the container. Anyone reproducing the benchmark runs this script identically.

This script is infrastructure-as-code: the SQL it executes is the operator
configuration an aidbox deployment would need to ship out-of-the-box to match
the other benchmarked FHIR servers (HAPI/medplum/msfhir/blaze) on search
latency. The fact that aidbox 2603 does not ship these indexes is itself a
published finding of the benchmark — see benchmark/methodology.md.

Methodology:
  - Runs AFTER `docker compose up aidbox aidbox-db` + wait_healthy.
  - Runs BEFORE bundle ingest AND before warmup. The CREATE INDEX on an
    empty table is instant; Postgres will maintain the index automatically as
    ingest writes rows, which is the same steady-state behavior hapi/medplum
    get for free from their ORMs.
  - Idempotent via `CREATE INDEX IF NOT EXISTS` + a sentinel hash.

Usage:
    python -m loadtest.aidbox_bootstrap
    python -m loadtest.aidbox_bootstrap --sentinel-dir results/loadtest/<run_id>
    python -m loadtest.aidbox_bootstrap --probe   # Phase-1 latency diff
    python -m loadtest.aidbox_bootstrap --force   # re-run despite sentinel
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _fhir_servers import build_headers, find_server, load_servers, resolve_base_url  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVERS = REPO_ROOT / "servers.yaml"
DEFAULT_BASE_URL = "http://localhost:8888/fhir"

# One GIN(resource jsonb_path_ops) index per resource table whose search queries
# are exercised by the benchmark workload. Drawn from queries.yaml's
# loadtest=include queries: Patient, Observation, Condition, Procedure,
# Encounter, MedicationRequest.
#
# Aidbox stores each FHIR resource in its own lowercase-named table, with the
# full JSON body in the `resource` column. A GIN jsonb_path_ops index on that
# column accelerates the `@>` predicate aidbox's query generator emits for
# every search parameter on the base resource.
INDEXED_TABLES: tuple[str, ...] = (
    "condition",
    "encounter",
    "medicationrequest",
    "observation",
    "patient",
    "procedure",
)


def _index_name(table: str) -> str:
    return f"{table}_resource_gin_path"


def _index_ddl(table: str) -> str:
    return (
        f"CREATE INDEX IF NOT EXISTS {_index_name(table)} "
        f"ON {table} USING gin (resource jsonb_path_ops)"
    )


def _ddl_set_hash() -> str:
    """Sentinel hash — changes if INDEXED_TABLES is edited."""
    h = hashlib.sha256()
    for t in INDEXED_TABLES:
        h.update(_index_ddl(t).encode())
    return h.hexdigest()[:16]


def _sentinel_path(sentinel_dir: Path) -> Path:
    return sentinel_dir / "aidbox_indexed.json"


def _sentinel_up_to_date(sentinel_dir: Path) -> bool:
    p = _sentinel_path(sentinel_dir)
    if not p.exists():
        return False
    try:
        body = json.loads(p.read_text())
    except Exception:
        return False
    return body.get("ddl_set_hash") == _ddl_set_hash()


def _write_sentinel(
    sentinel_dir: Path, wall_s: float, per_table: dict[str, float],
    probe_before_ms: float | None, probe_after_ms: float | None,
) -> None:
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    _sentinel_path(sentinel_dir).write_text(json.dumps({
        "completed_at": time.time(),
        "wall_seconds": round(wall_s, 3),
        "tables_indexed": list(INDEXED_TABLES),
        "per_table_seconds": {k: round(v, 3) for k, v in per_table.items()},
        "ddl_set_hash": _ddl_set_hash(),
        "probe_before_ms": probe_before_ms,
        "probe_after_ms": probe_after_ms,
        "aidbox_version": "2603",
        "note": (
            "Each index is CREATE INDEX ... USING gin (resource jsonb_path_ops). "
            "Required because aidbox 2603 ships no per-resource-table indexes "
            "beyond the primary key; every FHIR search is a Seq Scan otherwise."
        ),
    }, indent=2))


def _sql(client: httpx.Client, sql_url: str, headers: dict, statement: str) -> None:
    """Execute a single SQL statement through aidbox's /$sql admin endpoint.

    aidbox /$sql accepts either a string (single-statement) or an array
    ["SQL WITH PLACEHOLDERS", arg1, arg2, ...]. We only use the string form
    since CREATE INDEX has no bound parameters.

    Raises SystemExit on any non-2xx response.
    """
    resp = client.post(sql_url, json=[statement], headers=headers, timeout=1800.0)
    if not (200 <= resp.status_code < 300):
        raise SystemExit(
            f"aidbox /$sql {resp.status_code} on `{statement[:120]}…`: "
            f"{resp.text[:400]}"
        )


def _probe(
    client: httpx.Client, base_url: str, headers: dict, iterations: int = 10,
) -> float | None:
    """Median latency (ms) of GET /Observation?code=http://loinc.org|8480-6.

    Returns None if the corpus has no observations yet (bootstrap run on a
    fresh volume before ingest). Used by --probe / Phase-1 verification.
    """
    url = f"{base_url.rstrip('/')}/Observation"
    params = {"code": "http://loinc.org|8480-6", "_count": "1", "_total": "none"}
    lats: list[float] = []
    for _ in range(iterations):
        t0 = time.monotonic()
        try:
            resp = client.get(url, params=params, headers=headers, timeout=60.0)
            if resp.status_code != 200:
                return None
        except httpx.RequestError:
            return None
        lats.append((time.monotonic() - t0) * 1000)
    lats.sort()
    return lats[len(lats) // 2]


def _existing_indexes(client: httpx.Client, sql_url: str, headers: dict) -> set[str]:
    want = ", ".join(f"'{_index_name(t)}'" for t in INDEXED_TABLES)
    resp = client.post(
        sql_url, headers=headers, timeout=60.0,
        json=[f"SELECT indexname FROM pg_indexes WHERE schemaname='public' "
              f"AND indexname IN ({want})"],
    )
    if resp.status_code != 200:
        return set()
    rows = resp.json()
    if not isinstance(rows, list):
        return set()
    return {r["indexname"] for r in rows if isinstance(r, dict) and "indexname" in r}


def run_bootstrap(
    base_url: str, servers_file: Path, sentinel_dir: Path | None,
    force: bool, probe: bool,
) -> int:
    if sentinel_dir and not force and _sentinel_up_to_date(sentinel_dir):
        print(f"Aidbox index sentinel up to date at {_sentinel_path(sentinel_dir)} — skipping.")
        return 0

    servers = load_servers(servers_file)
    aidbox = find_server(servers, "aidbox")
    fhir_base = resolve_base_url(aidbox) or base_url
    aidbox_root = fhir_base.rsplit("/fhir", 1)[0]
    sql_url = f"{aidbox_root}/$sql"

    t0 = time.monotonic()
    probe_before: float | None = None
    probe_after: float | None = None
    per_table: dict[str, float] = {}

    with httpx.Client(timeout=120.0) as client:
        try:
            headers = build_headers(aidbox, client)
        except Exception as exc:
            print(f"ERROR: could not build aidbox auth headers: {exc}", file=sys.stderr)
            return 2

        print(f"Aidbox index bootstrap (aidbox-root={aidbox_root})")
        print(f"  ddl set hash: {_ddl_set_hash()}")
        print(f"  target tables: {', '.join(INDEXED_TABLES)}")

        already = _existing_indexes(client, sql_url, headers)
        if already:
            print(f"  already indexed: {sorted(already)}")

        if probe:
            print("\n  probing pre-index Observation?code (10 iters)…")
            probe_before = _probe(client, fhir_base, headers)
            if probe_before is None:
                print("  probe skipped — empty/unreachable corpus")
            else:
                print(f"  pre-index probe median: {probe_before:.1f} ms")

        for table in INDEXED_TABLES:
            idx = _index_name(table)
            if idx in already:
                print(f"  [{table:<20}] index exists — skip")
                per_table[table] = 0.0
                continue
            print(f"  [{table:<20}] CREATE INDEX {idx} …", end=" ", flush=True)
            t_tbl = time.monotonic()
            _sql(client, sql_url, headers, _index_ddl(table))
            elapsed = time.monotonic() - t_tbl
            per_table[table] = elapsed
            print(f"ok in {elapsed:.1f}s")

        if probe:
            print("\n  probing post-index Observation?code (10 iters)…")
            probe_after = _probe(client, fhir_base, headers)
            if probe_after is None:
                print("  probe skipped — empty/unreachable corpus")
            else:
                print(f"  post-index probe median: {probe_after:.1f} ms")
                if probe_before:
                    speedup = probe_before / max(probe_after, 0.01)
                    print(f"  speedup: {speedup:.0f}x  ({probe_before:.0f} ms → {probe_after:.1f} ms)")

    wall = time.monotonic() - t0
    print(f"\nAidbox bootstrap complete in {wall:.1f}s "
          f"({len([v for v in per_table.values() if v > 0])} new index(es), "
          f"{len([v for v in per_table.values() if v == 0])} preexisting).")

    if sentinel_dir:
        _write_sentinel(sentinel_dir, wall, per_table, probe_before, probe_after)
        print(f"Sentinel: {_sentinel_path(sentinel_dir)}")

    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help="Fallback base URL if servers.yaml can't be resolved.")
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--sentinel-dir", type=Path, default=None,
                    help="Directory to write aidbox_indexed.json sentinel.")
    ap.add_argument("--force", action="store_true",
                    help="Run bootstrap even if a matching sentinel already exists.")
    ap.add_argument("--probe", action="store_true",
                    help="Time Observation?code before and after index creation "
                    "(10 iters each). No-op when the corpus is empty.")
    args = ap.parse_args()
    return run_bootstrap(
        base_url=args.base_url,
        servers_file=args.servers_file,
        sentinel_dir=args.sentinel_dir,
        force=args.force,
        probe=args.probe,
    )


if __name__ == "__main__":
    sys.exit(main())
