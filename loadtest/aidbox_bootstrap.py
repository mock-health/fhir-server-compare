#!/usr/bin/env python3
"""Aidbox search-index bootstrap — port of Health Samurai's own benchmark indexes.

Aidbox community edition (2603) ships with zero Postgres indexes on the
per-resource tables beyond the primary key. Every FHIR search translates to a
uniform `WHERE resource @> '<jsonb>'` predicate, so without a GIN index the
query planner has no choice but `Seq Scan on <resource>` — costing ~12s per
search on a 63K-patient corpus (~1.9M Observation rows). This is what produced
our ramp-50k aidbox search p90 of 13.5s at 1K, climbing to 56s / 94% errors
at 64K.

The index set below is a verbatim port of what Health Samurai applies in their
own benchmark — they know their engine, and treating their configuration as
the vendor-recommended baseline is the same species of fairness we already
apply for MS FHIR (`x-bundle-processing-logic: parallel`) and Spark
(`spark-mongo-init/01-create-indexes.js`). Sources:
  - github.com/HealthSamurai/fhir-server-performance-benchmark/infra/aidbox/initbundle.json
  - github.com/HealthSamurai/fhir-server-performance-benchmark/ci_search_suite.yaml

Index categories:
  1. GIN(resource jsonb_path_ops) on every resource table whose FHIR search
     we exercise. `jsonb_path_ops` is the exact opclass the `@>` operator
     uses; ~30% smaller on disk than `jsonb_ops`.
  2. Patient name trigram indexes using Aidbox's `knife_extract_text()` +
     `aidbox_text_search()` functions over a JSON path set
     (name.family | given | middle | text | prefix | suffix). Dual-index
     pattern: one with `gin_trgm_ops` for substring search, one plain GIN.
  3. Patient given-name trigram + plain indexes (same pattern, narrower path).
  4. Patient birthdate btree indexes on `knife_extract_min_timestamptz()` /
     `knife_extract_max_timestamptz()` + a compound (min, max) index for
     date-range predicates.

We POST every `CREATE INDEX` through aidbox's documented `/$sql` admin
endpoint — the same endpoint aidbox's own console uses. No back-door, no
shell exec into the container. Anyone reproducing the benchmark runs this
script identically.

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

# Superset of:
#   - queries.yaml's loadtest=include resource tables (patient, observation,
#     condition, procedure, encounter, medicationrequest) — ours
#   - Health Samurai's upstream index set (claim, encounter, explanationofbenefit,
#     location, medicationrequest, observation, organization, patient,
#     practitioner) — theirs
# Union covers every table the benchmark exercises plus every table Aidbox's
# own benchmark does.
_GIN_TABLES: tuple[str, ...] = (
    "claim",
    "condition",
    "encounter",
    "explanationofbenefit",
    "location",
    "medicationrequest",
    "observation",
    "organization",
    "patient",
    "practitioner",
    "procedure",
)


# Specialized Patient indexes from HealthSamurai/fhir-server-performance-benchmark.
# These use Aidbox's own SQL helpers (knife_extract_text / aidbox_text_search /
# knife_extract_min_timestamptz / knife_extract_max_timestamptz). Those
# functions ship with Aidbox and are not part of stock Postgres, so this DDL
# is Aidbox-specific by design — that's the point: we're mirroring the
# vendor's own benchmark configuration.
_PATIENT_NAME_PATHS = (
    '[["name","family"],["name","given"],["name","middle"],'
    '["name","text"],["name","prefix"],["name","suffix"]]'
)
_PATIENT_GIVEN_PATHS = '[["name","given"]]'
_PATIENT_BIRTHDATE_PATH = '[["birthDate"]]'


# Each entry is (index_name, ddl_sql). DDL is always idempotent
# (CREATE INDEX IF NOT EXISTS) and deterministic. If you change this list,
# the sentinel hash changes, forcing a re-bootstrap on next run.
def _build_index_list() -> tuple[tuple[str, str], ...]:
    indexes: list[tuple[str, str]] = []

    # pg_trgm is required for gin_trgm_ops; Aidbox may or may not install it by
    # default, so we do so ourselves. Safe on a fresh DB and idempotent.
    indexes.append((
        "ext_pg_trgm",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    ))

    # Category 1: GIN(resource jsonb_path_ops) per table.
    for table in _GIN_TABLES:
        name = f"{table}_resource_gin_path"
        indexes.append((
            name,
            f"CREATE INDEX IF NOT EXISTS {name} "
            f"ON {table} USING gin (resource jsonb_path_ops)",
        ))

    # Category 2: Patient name indexes (trgm + plain).
    indexes.append((
        "patient_name_param_knife_string_trgm",
        "CREATE INDEX IF NOT EXISTS patient_name_param_knife_string_trgm "
        "ON patient USING gin "
        f"((aidbox_text_search(knife_extract_text(resource, '{_PATIENT_NAME_PATHS}'))) "
        "gin_trgm_ops)",
    ))
    indexes.append((
        "patient_name_param_knife_string",
        "CREATE INDEX IF NOT EXISTS patient_name_param_knife_string "
        "ON patient USING gin "
        f"((knife_extract_text(resource, '{_PATIENT_NAME_PATHS}')))",
    ))

    # Category 3: Patient given-name indexes (trgm + plain).
    indexes.append((
        "patient_given_param_knife_string_trgm",
        "CREATE INDEX IF NOT EXISTS patient_given_param_knife_string_trgm "
        "ON patient USING gin "
        f"((aidbox_text_search(knife_extract_text(resource, '{_PATIENT_GIVEN_PATHS}'))) "
        "gin_trgm_ops)",
    ))
    indexes.append((
        "patient_given_param_knife_string",
        "CREATE INDEX IF NOT EXISTS patient_given_param_knife_string "
        "ON patient USING gin "
        f"((knife_extract_text(resource, '{_PATIENT_GIVEN_PATHS}')))",
    ))

    # Category 4: Patient birthdate indexes (min, max, compound).
    indexes.append((
        "patient_birthdate_param_knife_date_min_tstz",
        "CREATE INDEX IF NOT EXISTS patient_birthdate_param_knife_date_min_tstz "
        "ON patient USING btree "
        f"((knife_extract_min_timestamptz(resource, '{_PATIENT_BIRTHDATE_PATH}')))",
    ))
    indexes.append((
        "patient_birthdate_param_knife_date_max_tstz",
        "CREATE INDEX IF NOT EXISTS patient_birthdate_param_knife_date_max_tstz "
        "ON patient USING btree "
        f"((knife_extract_max_timestamptz(resource, '{_PATIENT_BIRTHDATE_PATH}')))",
    ))
    indexes.append((
        "patient_birthdate_param_knife_date_min_max_tstz",
        "CREATE INDEX IF NOT EXISTS patient_birthdate_param_knife_date_min_max_tstz "
        "ON patient USING btree "
        f"((knife_extract_min_timestamptz(resource, '{_PATIENT_BIRTHDATE_PATH}')), "
        f"((knife_extract_max_timestamptz(resource, '{_PATIENT_BIRTHDATE_PATH}'))))",
    ))

    return tuple(indexes)


INDEXES: tuple[tuple[str, str], ...] = _build_index_list()

# Back-compat alias: other call sites and meta emitters used to import
# INDEXED_TABLES. Keep a slim projection (just the base-table GIN entries) so
# existing `.tables_indexed` fields in sentinel files remain meaningful.
INDEXED_TABLES: tuple[str, ...] = _GIN_TABLES


def _ddl_set_hash() -> str:
    """Sentinel hash — changes if INDEXES is edited."""
    h = hashlib.sha256()
    for _name, ddl in INDEXES:
        h.update(ddl.encode())
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
    sentinel_dir: Path, wall_s: float, per_index: dict[str, float],
    probe_before_ms: float | None, probe_after_ms: float | None,
) -> None:
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    _sentinel_path(sentinel_dir).write_text(json.dumps({
        "completed_at": time.time(),
        "wall_seconds": round(wall_s, 3),
        "tables_indexed": list(INDEXED_TABLES),
        "indexes_applied": [n for n, _ in INDEXES],
        "per_index_seconds": {k: round(v, 3) for k, v in per_index.items()},
        "ddl_set_hash": _ddl_set_hash(),
        "probe_before_ms": probe_before_ms,
        "probe_after_ms": probe_after_ms,
        "aidbox_version": "2603",
        "note": (
            "Index set ported verbatim from HealthSamurai/fhir-server-"
            "performance-benchmark (initbundle.json + ci_search_suite.yaml). "
            "Covers GIN(resource jsonb_path_ops) on 11 resource tables plus "
            "Patient name/given trigram + birthdate btree indexes. Required "
            "because Aidbox 2603 ships no per-resource-table indexes beyond "
            "the primary key; every FHIR search is a Seq Scan otherwise."
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
    # `ext_pg_trgm` is a CREATE EXTENSION, not an index — it won't appear in
    # pg_indexes, so filter it out of the existence probe.
    index_names = [n for n, _ in INDEXES if not n.startswith("ext_")]
    want = ", ".join(f"'{n}'" for n in index_names)
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
    per_index: dict[str, float] = {}

    with httpx.Client(timeout=120.0) as client:
        try:
            headers = build_headers(aidbox, client)
        except Exception as exc:
            print(f"ERROR: could not build aidbox auth headers: {exc}", file=sys.stderr)
            return 2

        print(f"Aidbox index bootstrap (aidbox-root={aidbox_root})")
        print(f"  ddl set hash: {_ddl_set_hash()}")
        print(f"  indexes to apply: {len(INDEXES)} "
              f"({len(INDEXED_TABLES)} tables + Patient specialized + pg_trgm)")

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

        for name, ddl in INDEXES:
            if name.startswith("ext_"):
                # CREATE EXTENSION — always issue, idempotent via IF NOT EXISTS.
                print(f"  [{name:<50}] {ddl.split(' IF ')[0]} …", end=" ", flush=True)
                t_one = time.monotonic()
                _sql(client, sql_url, headers, ddl)
                elapsed = time.monotonic() - t_one
                per_index[name] = elapsed
                print(f"ok in {elapsed:.1f}s")
                continue
            if name in already:
                print(f"  [{name:<50}] index exists — skip")
                per_index[name] = 0.0
                continue
            print(f"  [{name:<50}] CREATE INDEX …", end=" ", flush=True)
            t_one = time.monotonic()
            _sql(client, sql_url, headers, ddl)
            elapsed = time.monotonic() - t_one
            per_index[name] = elapsed
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
          f"({len([v for v in per_index.values() if v > 0])} new, "
          f"{len([v for v in per_index.values() if v == 0])} preexisting).")

    if sentinel_dir:
        _write_sentinel(sentinel_dir, wall, per_index, probe_before, probe_after)
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
