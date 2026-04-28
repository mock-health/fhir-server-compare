#!/usr/bin/env python3
"""Ramp orchestrator — incremental ingest per server, across a checkpoint ladder.

Outer loop: servers. Inner loop: checkpoints. For each server:
  1. Reset the compose stack + volume (cold DB) — ONCE per server.
  2. Bring up JUST that server's services; wait healthy; bootstrap Medplum if so.
  3. For each checkpoint N in ascending order:
       - Ingest ONLY the delta (N - prev) of new patients onto the running DB,
         capturing per-bundle timings for that delta into ingest.jsonl under
         this checkpoint's dir.
       - Run CRUD + Search workloads against the DB in its current state.
       - Write the cell_complete.json sentinel.
  4. Stop the server (preserve volumes in case of investigation).

Why outer=server, inner=checkpoint: ingest dominates wall clock, and doing
the reverse means re-paying that cost from scratch at every checkpoint. With
a ladder of [1K, 2K, 4K, 8K, 16K, 32K], cold-per-checkpoint loads a total of
63K bundles/server; incremental loads a total of 32K (~2x faster), and the
savings grow with the max checkpoint.

Methodology implication: ingest.jsonl under checkpoint=N measures the
MARGINAL cost of ingesting (N - prev) patients onto a warm DB already
holding `prev` patients — not a cold-DB ingest of N. Search/CRUD cells are
unaffected: they measure a DB in its current state, which is exactly the
same state the old structure produced at that checkpoint.

Resume: if any (server, checkpoint) sentinel exists for a server, we
compute `prev` = the largest completed checkpoint and pick up from the next
one WITHOUT resetting the volume. That preserves the data already loaded.

Each checkpoint-server output (layout unchanged):
    results/loadtest/<run_id>/checkpoint_NNNNNNNN/<server>/
      ingest.jsonl      # per-bundle timings for THIS checkpoint's delta
      crud.jsonl        # per-op CRUD timings
      search.jsonl      # per-query search timings
      resources.csv     # 1Hz docker stats across the whole window
      disk.json         # post-run `docker system df -v`
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from fhirbench.harness import generate, loader, workload_crud, workload_search  # noqa: E402
from fhirbench.harness import k6_driver  # noqa: E402
from fhirbench.harness.host_meta import write_meta  # noqa: E402
from fhirbench.harness.resources import ResourceSampler, snapshot_disk  # noqa: E402
from fhirbench.harness.stage import SERVER_CONTAINERS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results" / "loadtest"
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"
DEFAULT_QUERIES = REPO_ROOT / "config" / "queries.yaml"
DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "loadtest" / "fhir"
DEFAULT_PREREQ_DIR = REPO_ROOT / "data" / "loadtest" / "prerequisites"
DEFAULT_SYNTHEA_DIR = REPO_ROOT / "synthea"

# Default order is fastest-first, based on empirical 1K ingest numbers:
#   Blaze:   19,272 res/s  (RocksDB embedded, Clojure — fastest)
#   HAPI:     6,902 res/s  (Postgres JPA)
#   Aidbox:   4,218 res/s  (Postgres, custom storage)
#   MS FHIR:  1,167 res/s  (SQL Server + parallel bundle processing)
#   Medplum:    690 res/s  (Postgres, 4x load-balanced replicas)
#   Spark:     ~20  res/s  (MongoDB — ingest-bottlenecked; 1K takes hours)
# Ordering so the fastest finishes first gives per-checkpoint data faster.
DEFAULT_SERVER_ORDER = ("blaze", "hapi", "aidbox", "msfhir", "medplum", "spark")

# Phased CRUD: per-verb (sample_cap, time_cap_seconds).
# R and V are fastest, bounded tighter on time — they should always hit
# cap in seconds. C/U/D get a 5-min cap so slow servers get enough tail
# samples for p99 without starving the ramp's overnight budget.
DEFAULT_CRUD_SAMPLE_CAP = 50_000
DEFAULT_CRUD_TIME_CAP_S = 300.0
DEFAULT_CRUD_R_TIME_CAP_S = 60.0
# Prewarm amortizes first-time JIT / plan-compile / cold-index-touch
# costs per U template before the timed U phase. 200 ops × 6 templates
# = 1,200 ops, typically <30s wall. Ops land in crud_prewarm.jsonl and
# never touch percentile math.
DEFAULT_CRUD_PREWARM_PER_TEMPLATE = 200


def _build_phase_caps(
    sample_cap: int, time_cap_s: float, r_time_cap_s: float,
) -> dict[str, tuple[int, float]]:
    return {
        "C": (sample_cap, time_cap_s),
        "U": (sample_cap, time_cap_s),
        "R": (sample_cap, r_time_cap_s),
        "V": (sample_cap, r_time_cap_s),
        "D": (sample_cap, time_cap_s),
    }

# Compose files, same pair the Makefile uses.
COMPOSE_FILES = ("-f", "docker-compose.yml", "-f", "docker-compose.loadtest.yml")

# Per-server volume names (matches docker-compose.yml's named volumes + the
# hapi-data volume added by the overlay).
VOLUME_MAP = {
    "hapi":    "fhir-server-compare_hapi-data",
    "aidbox":  "fhir-server-compare_aidbox-data",
    "medplum": "fhir-server-compare_medplum-data",
    "msfhir":  "fhir-server-compare_msfhir-data",
    "blaze":   "fhir-server-compare_blaze-data",
    "spark":   "fhir-server-compare_spark-data",
}

# Servers with a second named volume (e.g., a search-index sidecar) that
# reset_server also nukes. Empty by default; populate per server when adding
# split-backend servers.
EXTRA_VOLUMES: dict[str, list[str]] = {}


def _run(cmd: list[str], check: bool = True) -> int:
    print(f"  $ {' '.join(cmd)}")
    rc = subprocess.run(cmd).returncode
    if check and rc != 0:
        raise SystemExit(f"command failed ({rc}): {' '.join(cmd)}")
    return rc


def reset_server(server_id: str) -> None:
    """Stop+remove one server's containers AND nuke its named volume.

    Faster than tearing down the whole compose stack when the other three
    servers already live on preserved volumes (in this design they don't,
    but we keep the surgical scope anyway — it's a safer habit).

    For medplum we additionally tear down the base `medplum` service (the
    single-container variant defined in docker-compose.yml) — not part of the
    loadtest topology, but if left running from a prior session it competes
    on the same medplum-db/redis and pollutes resource sampling.
    """
    services = _services_for(server_id)
    extras = ["medplum"] if server_id == "medplum" else []
    _run(["docker", "compose", *COMPOSE_FILES, "rm", "-sfv", *services, *extras], check=False)
    vol = VOLUME_MAP[server_id]
    _run(["docker", "volume", "rm", "-f", vol], check=False)
    for extra in EXTRA_VOLUMES.get(server_id, []):
        _run(["docker", "volume", "rm", "-f", extra], check=False)


def _services_for(server_id: str) -> list[str]:
    # keep in sync with Makefile's *_SVCS lists. Medplum's loadtest topology
    # is 4 x medplum-server replicas + nginx LB sharing one postgres + redis
    # (see docker-compose.loadtest.yml for rationale — plain Express, no
    # cluster mode, so we match their documented horizontal-scale pattern).
    return {
        "hapi":    ["hapi", "hapi-db"],
        "aidbox":  ["aidbox", "aidbox-db"],
        "medplum": [
            "medplum-db", "medplum-redis",
            "medplum-1", "medplum-2", "medplum-3", "medplum-4",
            "medplum-lb",
        ],
        "msfhir":  ["msfhir", "msfhir-db"],
        # Blaze is single-process — RocksDB embedded, no DB sidecar.
        "blaze":   ["blaze"],
        # Spark needs MongoDB. The spark-mongo-init/ bind-mount on spark-db
        # auto-creates the searchindex.internal_id and resources.@typename
        # composite indexes on first startup of a fresh volume — without
        # them, ingest is ~70x slower (COLLSCAN per upsert).
        "spark":   ["spark", "spark-db"],
    }[server_id]


def up_server(server_id: str) -> None:
    services = _services_for(server_id)
    if server_id == "medplum":
        # Medplum seeds structure definitions on first boot of an empty DB.
        # That seed runs OUTSIDE the migration advisory lock — so if 4 replicas
        # launch at once against an empty volume, they race on concurrent
        # inserts/deletes into StructureDefinition and one or more crash with
        # "could not serialize access due to concurrent delete" before
        # restarting cleanly. The crash-then-recover completes healthy within
        # ~60s, but docker compose up -d already bailed on the dependency by
        # then. Fix: bring up medplum-1 ALONE first, wait for it to finish
        # seeding (healthcheck passes), then bring up the other three replicas
        # plus the LB — they'll see "Already seeded" and start cleanly.
        _run(["docker", "compose", *COMPOSE_FILES, "up", "-d", "--wait",
              "medplum-db", "medplum-redis", "medplum-1"])
        _run(["docker", "compose", *COMPOSE_FILES, "up", "-d",
              "medplum-2", "medplum-3", "medplum-4", "medplum-lb"])
        return
    _run(["docker", "compose", *COMPOSE_FILES, "up", "-d", *services])


def stop_server(server_id: str) -> None:
    services = _services_for(server_id)
    _run(["docker", "compose", *COMPOSE_FILES, "stop", *services], check=False)


def wait_healthy(server_id: str, timeout_s: float = 300.0) -> None:
    # Reuse the wait_healthy module (no-auth for medplum since bootstrap
    # hasn't run on a fresh volume).
    py = sys.executable
    cmd = [py, "-m", "fhirbench.harness.wait_healthy", "--server", server_id,
           "--timeout", str(timeout_s)]
    if server_id == "medplum":
        cmd.append("--no-auth")
    _run(cmd)


def bootstrap_medplum() -> None:
    py = sys.executable
    _run([py, "-m", "fhirbench.harness.bootstrap_medplum"])
    # bootstrap_medplum runs in a subprocess and rewrites .env in place, but
    # our own os.environ still holds whatever values we inherited at startup.
    # loader.run -> load_servers -> env interpolation reads os.environ, so
    # without this reload the very next OAuth token POST uses stale creds and
    # 400s. Pull the file back into our environment.
    reload_dotenv(REPO_ROOT / ".env")


def bootstrap_aidbox(run_dir: Path) -> None:
    """Create GIN(resource jsonb_path_ops) indexes on aidbox's per-resource
    tables before ingest. Aidbox 2603 ships with only primary-key indexes,
    so every FHIR search is a Seq Scan against the full resource JSONB until
    an operator creates the backing indexes. See src/fhirbench/harness/aidbox_bootstrap.py
    for the full rationale.

    Runs on an empty DB (creates are instant), and Postgres maintains the
    indexes automatically as ingest writes rows. Idempotent via an
    aidbox_indexed.json sentinel keyed off the DDL set hash."""
    py = sys.executable
    _run([py, "-m", "fhirbench.harness.aidbox_bootstrap",
          "--sentinel-dir", str(run_dir)])


def reload_dotenv(path: Path) -> None:
    """Re-read a KEY=VALUE .env file into os.environ. Bash-compatible format;
    ignores comments and blank lines. Existing os.environ keys are overwritten
    so this also picks up any values Make exported earlier."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        # Drop surrounding quotes if present (our bootstrap doesn't write any
        # but plenty of other tools do).
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def _completed_checkpoints(run_dir: Path, server_id: str, checkpoints: tuple[int, ...]) -> set[int]:
    """Return the set of checkpoints with a cell_complete.json sentinel for this server."""
    done: set[int] = set()
    for ckpt in checkpoints:
        sentinel = run_dir / f"checkpoint_{ckpt:08d}" / server_id / "cell_complete.json"
        if sentinel.exists():
            done.add(ckpt)
    return done


def run_one_server(
    server_id: str,
    checkpoints: tuple[int, ...],
    run_id: str,
    workload_duration: float,
    workers_ingest: int,
    workers_workload: int,
    input_dir: Path,
    prereq_dir: Path,
    servers_file: Path,
    queries_file: Path,
    results_root: Path,
    crud_phase_caps: dict[str, tuple[int, float]],
    crud_prewarm_per_template: int,
    workload_harness: str = "python",
) -> None:
    """Reset+boot this server ONCE, then ingest the delta at each checkpoint."""
    run_dir = results_root / run_id
    done = _completed_checkpoints(run_dir, server_id, checkpoints)
    remaining = [c for c in checkpoints if c not in done]
    if not remaining:
        print(f"[{server_id}] all checkpoints already complete, skipping server")
        return

    # Resume semantics: if any prior checkpoint finished for this server, the
    # volume was preserved and we must NOT reset it — that would wipe the
    # patients we're building on. Otherwise, start from a cold volume.
    cold_start = len(done) == 0
    prev = max(done) if done else 0

    banner = f"===== server {server_id} | checkpoints {remaining} | resume from {prev} ====="
    print("\n" + "=" * len(banner) + "\n" + banner + "\n" + "=" * len(banner))

    t_server_total = time.monotonic()
    if cold_start:
        reset_server(server_id)
    up_server(server_id)
    wait_healthy(server_id)
    if server_id == "medplum":
        bootstrap_medplum()
    elif server_id == "aidbox":
        bootstrap_aidbox(run_dir)

    try:
        for checkpoint in remaining:
            ckpt_dir = run_dir / f"checkpoint_{checkpoint:08d}"
            server_dir = ckpt_dir / server_id
            server_dir.mkdir(parents=True, exist_ok=True)

            # Ground truth: ask the server how many patients it ACTUALLY has
            # right now, and size the ingest to that. The sentinel-derived
            # `prev` can lie — if the volume was wiped or patients were
            # deleted between runs, trusting it would load too few bundles
            # (or none at all when realized >= checkpoint already).
            realized_before = _count_patients(server_id, servers_file)
            if realized_before != prev:
                print(f"  [reconcile] sentinel says prev={prev} for {server_id} but "
                      f"server reports {realized_before} patients; using actual count")
            offset = realized_before
            delta = checkpoint - offset

            # If we just did a cold reset, the volume is empty but a prior
            # (failed) run may have left ingest.jsonl / cell artifacts here.
            # The loader's idempotency cache reads ingest.jsonl to decide
            # what to skip — stale entries + empty DB would skip every
            # POST and leave the server with 0 patients. Clear the cell
            # artifacts so this run is an honest cold start.
            if cold_start and realized_before == 0:
                for stale in ("ingest.jsonl", "crud.jsonl", "search.jsonl",
                              "warmup.jsonl", "crud_prewarm.jsonl",
                              "crud_phases.json", "cell_summary.json",
                              "fairness.json", "k6_crud.ndjson",
                              "k6_search.ndjson", "resources.csv",
                              "disk.json"):
                    p = server_dir / stale
                    if p.exists():
                        p.unlink()

            cell_banner = (f"----- {server_id} | checkpoint {checkpoint} "
                           f"(have {realized_before}, +{max(0, delta)} new) -----")
            print("\n" + "-" * len(cell_banner) + "\n" + cell_banner + "\n" + "-" * len(cell_banner))

            t_cell = time.monotonic()
            ingest_log = server_dir / "ingest.jsonl"
            resource_csv = server_dir / "resources.csv"
            containers = SERVER_CONTAINERS[server_id]

            with ResourceSampler(containers, resource_csv):
                if delta <= 0:
                    print(f"  [skip-ingest] {server_id} already has {realized_before} "
                          f">= {checkpoint} patients; running workloads only")
                else:
                    # Load only the delta. Loader's select_bundles applies the
                    # max_bytes filter BEFORE offset/limit, so [offset, checkpoint)
                    # here names the same bundles the old code's offset=0,
                    # limit=checkpoint slice named at indices [offset, checkpoint).
                    rc = loader.run(
                        server_id=server_id,
                        servers_path=servers_file,
                        input_dir=input_dir,
                        log_path=ingest_log,
                        workers=workers_ingest,
                        offset=offset,
                        limit=delta,
                        prereq_dir=prereq_dir if offset == 0 else None,
                        progress_every=max(50, delta // 20),
                    )
                    if rc not in (0, 1):
                        print(f"  [abort] loader rc={rc}; stopping server")
                        return

                # Fairness pre-check: at this checkpoint the DB should hold
                # `checkpoint` patients total (cumulative), not just the delta.
                loaded_this_cell = max(0, delta)
                realized = _count_patients(server_id, servers_file)
                (server_dir / "fairness.json").write_text(json.dumps({
                    "checkpoint": checkpoint,
                    "realized_patient_count": realized,
                    "ratio": realized / checkpoint if checkpoint else 0.0,
                    "delta_loaded_this_cell": loaded_this_cell,
                    "realized_before_ingest": realized_before,
                    "prev_sentinel_checkpoint": prev,
                }, indent=2))
                print(f"  [fairness] {server_id} has {realized}/{checkpoint} patients "
                      f"({(realized/checkpoint)*100:.1f}%); this cell added +{loaded_this_cell}")

                # Warmup every checkpoint: DB state has changed (2x the rows),
                # query plans may recompile, caches need re-seeding. Python
                # harness only — k6 runs long enough (typically 15 min) that
                # the plan-recompile transient is statistically irrelevant in
                # the percentile windows.
                if workload_harness != "k6":
                    print(f"  [warmup] 30s {server_id} warmup before timed phase")
                    workload_crud.run(
                        server_id=server_id, servers_path=servers_file,
                        log_path=server_dir / "warmup.jsonl",
                        duration=30.0, workers=max(8, workers_workload // 4),
                        mix_spec="C:10,R:60,U:25,D:5",
                    )

                if workload_harness == "k6":
                    # K6 harness: run CRUD + Search back-to-back via k6, then
                    # convert the raw NDJSON into crud.jsonl / search.jsonl in
                    # the same shape the Python drivers would have produced.
                    # cell_summary.py / parse_report.py are harness-agnostic
                    # — they just read the JSONLs. No crud_phases.json because
                    # k6 runs mix-mode CRUD, not phased C→U→R→V→D.
                    k6_driver.run_workloads(
                        server_id=server_id,
                        server_dir=server_dir,
                        workload_duration=workload_duration,
                    )
                else:
                    # Python harness (default). Search runs BEFORE CRUD so it
                    # measures the post-ingest steady state, not a state
                    # mutated by 50K creates/deletes from the CRUD phase.
                    workload_search.run(
                        server_id=server_id, servers_path=servers_file, queries_path=queries_file,
                        log_path=server_dir / "search.jsonl",
                        duration=workload_duration, workers=workers_workload,
                        exclude={"patient_export"},
                    )
                    # Phased CRUD: per-verb timed phases, each capped at
                    # min(sample_cap, duration_cap). Produces enough samples
                    # per verb per cell for p90/p99 with ±5% CI on every
                    # "reliable"-flagged cell. See crud_phases.json for
                    # per-phase accounting (samples, elapsed_ms, stop_reason).
                    phase_summaries = workload_crud.run(
                        server_id=server_id, servers_path=servers_file,
                        log_path=server_dir / "crud.jsonl",
                        duration=0.0, workers=workers_workload, mix_spec="",
                        phased=True, phase_caps=crud_phase_caps,
                        prewarm_per_template=crud_prewarm_per_template,
                        prewarm_log_path=server_dir / "crud_prewarm.jsonl",
                    )
                    if isinstance(phase_summaries, list):
                        (server_dir / "crud_phases.json").write_text(
                            json.dumps(phase_summaries, indent=2)
                        )

            snapshot_disk(server_dir / "disk.json")
            (server_dir / "cell_complete.json").write_text(json.dumps({
                "checkpoint": checkpoint,
                "server": server_id,
                "completed_at": time.time(),
                "wall_minutes": (time.monotonic() - t_cell) / 60.0,
                "delta_loaded": max(0, delta),
                "realized_before_ingest": realized_before,
                "prev_sentinel_checkpoint": prev,
            }, indent=2))
            print(f"  [done] {server_id} @ {checkpoint} in {(time.monotonic() - t_cell)/60:.1f} min")
            prev = checkpoint
    finally:
        stop_server(server_id)
        print(f"[{server_id}] total server wall time: "
              f"{(time.monotonic() - t_server_total)/60:.1f} min")


def _count_patients(server_id: str, servers_file: Path) -> int:
    """Hit Patient?_summary=count to verify the realized dataset size.

    Returns 0 on any failure — the report will flag the missing fairness
    record but doesn't fail the run.
    """
    import httpx
    from fhirbench.servers import build_headers, find_server, load_servers, resolve_base_url
    try:
        servers = load_servers(servers_file)
        server = find_server(servers, server_id)
        base_url = resolve_base_url(server)
        with httpx.Client(timeout=60.0) as client:
            headers = build_headers(server, client)
            resp = client.get(f"{base_url}/Patient", params={"_summary": "count"},
                              headers=headers, timeout=60.0)
            if not (200 <= resp.status_code < 300):
                return 0
            return int(resp.json().get("total", 0))
    except Exception as exc:
        print(f"  [fairness] count probe failed for {server_id}: {exc}")
        return 0


def run_ramp(
    checkpoints: tuple[int, ...],
    servers: tuple[str, ...],
    run_id: str,
    workload_duration: float,
    workers_ingest: int,
    workers_workload: int,
    input_dir: Path,
    prereq_dir: Path,
    synthea_dir: Path,
    servers_file: Path,
    queries_file: Path,
    results_root: Path,
    seed: int,
    crud_sample_cap: int = DEFAULT_CRUD_SAMPLE_CAP,
    crud_time_cap_s: float = DEFAULT_CRUD_TIME_CAP_S,
    crud_r_time_cap_s: float = DEFAULT_CRUD_R_TIME_CAP_S,
    crud_prewarm_per_template: int = DEFAULT_CRUD_PREWARM_PER_TEMPLATE,
    workload_harness: str = "python",
) -> int:
    checkpoints = tuple(sorted(set(checkpoints)))
    print(f"\nRAMP: checkpoints={list(checkpoints)} servers={list(servers)} "
          f"run_id={run_id} harness={workload_harness}")

    # Capture host metadata once at run start. Methodology rigor — anyone
    # reproducing this needs to know the kernel, governor, THP setting, and
    # the exact image digests in play.
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    write_meta(run_dir / "meta.json", run_id=run_id, checkpoints=checkpoints, servers=servers)

    # Generate upfront for the largest checkpoint — ensure_count is idempotent,
    # so a single call sized to max(checkpoints) avoids redundant Synthea
    # batches inside the server loop.
    max_ckpt = max(checkpoints)
    banner = f"##### GENERATE ≥ {max_ckpt} patient bundles #####"
    print("\n" + "#" * len(banner) + "\n" + banner + "\n" + "#" * len(banner))
    generate.ensure_count(
        count=max_ckpt, seed=seed, state="Massachusetts", city="Boston",
        synthea_dir=synthea_dir, output_dir=input_dir, prereq_dir=prereq_dir,
    )

    crud_phase_caps = _build_phase_caps(
        crud_sample_cap, crud_time_cap_s, crud_r_time_cap_s,
    )
    print(f"CRUD phase caps: {crud_phase_caps}")
    print(f"CRUD prewarm: {crud_prewarm_per_template} ops/template")

    t_total = time.monotonic()
    for server_id in servers:
        server_banner = f"##### SERVER {server_id} #####"
        print("\n" + "#" * len(server_banner) + "\n" + server_banner + "\n" + "#" * len(server_banner))
        run_one_server(
            server_id=server_id,
            checkpoints=checkpoints,
            run_id=run_id,
            workload_duration=workload_duration,
            workers_ingest=workers_ingest,
            workers_workload=workers_workload,
            input_dir=input_dir,
            prereq_dir=prereq_dir,
            servers_file=servers_file,
            queries_file=queries_file,
            results_root=results_root,
            crud_phase_caps=crud_phase_caps,
            crud_prewarm_per_template=crud_prewarm_per_template,
            workload_harness=workload_harness,
        )

    elapsed = (time.monotonic() - t_total) / 60.0
    print(f"\n===== ramp complete in {elapsed:.1f} min =====")
    return 0


def parse_checkpoints(spec: str) -> tuple[int, ...]:
    return tuple(int(x.strip()) for x in spec.split(",") if x.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--checkpoints", required=True,
                    help="Comma-separated patient counts. Each checkpoint is run cold against every server.")
    ap.add_argument("--servers", default=",".join(DEFAULT_SERVER_ORDER),
                    help="Comma-separated server ids in the order to test (default: fastest-first).")
    ap.add_argument("--workload-duration", type=float, default=900.0)
    ap.add_argument("--workers-ingest", type=int, default=32)
    ap.add_argument("--workers-workload", type=int, default=64)
    ap.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    ap.add_argument("--prereq-dir", type=Path, default=DEFAULT_PREREQ_DIR)
    ap.add_argument("--synthea-dir", type=Path, default=DEFAULT_SYNTHEA_DIR)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--queries-file", type=Path, default=DEFAULT_QUERIES)
    ap.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--crud-sample-cap", type=int, default=DEFAULT_CRUD_SAMPLE_CAP,
                    help="Max samples per CRUD verb phase (C/U/R/D).")
    ap.add_argument("--crud-time-cap-seconds", type=float, default=DEFAULT_CRUD_TIME_CAP_S,
                    help="Max wall-clock per C/U/D phase. Slow servers will hit this instead of the sample cap.")
    ap.add_argument("--crud-r-time-cap-seconds", type=float, default=DEFAULT_CRUD_R_TIME_CAP_S,
                    help="Max wall-clock for the R (read-your-own-write) and V (vread) phases. Both are fast; usually hit sample cap first.")
    ap.add_argument("--crud-prewarm-per-template", type=int,
                    default=DEFAULT_CRUD_PREWARM_PER_TEMPLATE,
                    help="Prewarm each of the 6 U templates with this many ops "
                         "before the timed U phase (writes to crud_prewarm.jsonl, "
                         "not included in percentiles). Set to 0 to disable.")
    ap.add_argument("--workload-harness", choices=("python", "k6"), default="python",
                    help="Which harness drives the timed CRUD + Search phase. "
                         "python (default) = phased CRUD + per-verb breakouts via "
                         "the Python thread-pool drivers. k6 = grafana/k6 container, "
                         "mix-mode CRUD (no phased C/U/R/V/D), raw NDJSON converted "
                         "into the same crud.jsonl / search.jsonl shape. Warmup is "
                         "always Python regardless of this flag so thermal conditions "
                         "match across harnesses.")
    args = ap.parse_args()
    servers = tuple(s.strip() for s in args.servers.split(",") if s.strip())
    for s in servers:
        if s not in SERVER_CONTAINERS:
            print(f"ERROR: unknown server '{s}'", file=sys.stderr)
            return 2
    return run_ramp(
        checkpoints=parse_checkpoints(args.checkpoints),
        servers=servers,
        run_id=args.run_id,
        workload_duration=args.workload_duration,
        workers_ingest=args.workers_ingest,
        workers_workload=args.workers_workload,
        input_dir=args.input_dir,
        prereq_dir=args.prereq_dir,
        synthea_dir=args.synthea_dir,
        servers_file=args.servers_file,
        queries_file=args.queries_file,
        results_root=args.results_root,
        seed=args.seed,
        crud_sample_cap=args.crud_sample_cap,
        crud_time_cap_s=args.crud_time_cap_seconds,
        crud_r_time_cap_s=args.crud_r_time_cap_seconds,
        crud_prewarm_per_template=args.crud_prewarm_per_template,
        workload_harness=args.workload_harness,
    )


if __name__ == "__main__":
    sys.exit(main())
