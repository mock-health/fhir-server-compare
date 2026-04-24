#!/usr/bin/env python3
"""Stage orchestrator — ingest + CRUD + Search for one server in one stage.

The three HS-matched stages:
  --stage 1  load first N patients from empty DB
  --stage 2  add patients up to --target-total (default 100000)
  --stage 3  load NEXT N patients after --target-total as an incremental test

Each stage for a server writes under:
  results/loadtest/<run_id>/stage<N>/<server>/
    ingest.jsonl             bundle timings
    ingest.resources.csv     docker stats during ingest
    crud.jsonl               CRUD op timings
    crud.resources.csv       docker stats during CRUD
    search.jsonl             search op timings
    search.resources.csv     docker stats during search
    disk.json                `docker system df -v` snapshot

The script does NOT bring up / reset the compose stack — that's the caller's
responsibility. Reason: stage 2 and 3 must PRESERVE the data from earlier
stages, so automatic reset would be wrong. A bash wrapper in the Makefile
handles the one-time stack bring-up and the optional stage-1 volume reset.

Usage:
    python -m loadtest.stage --stage 1 --server hapi --count 1000 --run-id dryrun-10p
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loadtest import loader, workload_crud, workload_search  # noqa: E402
from loadtest.resources import ResourceSampler, snapshot_disk  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results" / "loadtest"
DEFAULT_SERVERS = REPO_ROOT / "servers.yaml"
DEFAULT_QUERIES = REPO_ROOT / "queries.yaml"
DEFAULT_INPUT_DIR = REPO_ROOT / "data" / "loadtest" / "fhir"
DEFAULT_PREREQ_DIR = REPO_ROOT / "data" / "loadtest" / "prerequisites"

# containers each server OWNS (server + its sidecars). The resource sampler
# follows these so the report shows full-stack resource use per server.
SERVER_CONTAINERS: dict[str, list[str]] = {
    "hapi":    ["fhir-compare-hapi", "fhir-compare-hapi-db"],
    "aidbox":  ["fhir-compare-aidbox", "fhir-compare-aidbox-db"],
    "blaze":   ["fhir-compare-blaze"],
    "spark":   ["fhir-compare-spark", "fhir-compare-spark-db"],
    "medplum": [
        "fhir-compare-medplum-1", "fhir-compare-medplum-2",
        "fhir-compare-medplum-3", "fhir-compare-medplum-4",
        "fhir-compare-medplum-lb",
        "fhir-compare-medplum-db", "fhir-compare-medplum-redis",
    ],
    "msfhir":  ["fhir-compare-msfhir", "fhir-compare-msfhir-db"],
    "hfs":     ["fhir-compare-hfs", "fhir-compare-hfs-db", "fhir-compare-hfs-es"],
}


def stage_slice(stage: int, count: int, target_total: int) -> tuple[int, int]:
    """Return (offset, limit) for a stage.

    Stage 1: load the first `count` patients (offset=0).
    Stage 2: load patients count..target_total (fills up to the target).
    Stage 3: load `count` NEW patients at offset=target_total.
    """
    if stage == 1:
        return 0, count
    if stage == 2:
        start = count
        return start, max(0, target_total - start)
    if stage == 3:
        return target_total, count
    raise ValueError(f"unknown stage: {stage}")


def run_stage(
    stage: int,
    server_id: str,
    run_id: str,
    count: int,
    target_total: int,
    workers_ingest: int,
    workers_workload: int,
    workload_duration: float,
    input_dir: Path,
    prereq_dir: Path | None,
    results_root: Path,
    servers_file: Path,
    queries_file: Path,
    skip_workloads: bool,
) -> int:
    offset, limit = stage_slice(stage, count, target_total)
    containers = SERVER_CONTAINERS.get(server_id)
    if containers is None:
        print(f"ERROR: unknown server id '{server_id}' (not in SERVER_CONTAINERS map)", file=sys.stderr)
        return 2

    stage_dir = results_root / run_id / f"stage{stage}" / server_id
    stage_dir.mkdir(parents=True, exist_ok=True)
    ingest_log = stage_dir / "ingest.jsonl"
    ingest_res = stage_dir / "ingest.resources.csv"
    crud_log = stage_dir / "crud.jsonl"
    crud_res = stage_dir / "crud.resources.csv"
    search_log = stage_dir / "search.jsonl"
    search_res = stage_dir / "search.resources.csv"
    disk_path = stage_dir / "disk.json"

    banner = f"===== Stage {stage} on {server_id} | offset={offset} limit={limit} ====="
    print("\n" + "=" * len(banner) + "\n" + banner + "\n" + "=" * len(banner))

    t_stage = time.monotonic()

    # --- ingest -------------------------------------------------------------
    if limit > 0:
        # Prereqs (practitioner/hospital) loaded only on stage 1 — they
        # persist in the DB for stages 2 and 3. Passing None on later stages
        # makes loader.run skip the pre-phase entirely.
        prereq_for_stage = prereq_dir if stage == 1 else None
        print(f"\n[ingest] offset={offset} limit={limit} workers={workers_ingest} "
              f"prereqs={'yes' if prereq_for_stage else 'no (reuse from stage 1)'}")
        with ResourceSampler(containers, ingest_res):
            rc = loader.run(
                server_id=server_id,
                servers_path=servers_file,
                input_dir=input_dir,
                log_path=ingest_log,
                workers=workers_ingest,
                offset=offset,
                limit=limit,
                prereq_dir=prereq_for_stage,
                progress_every=100,
            )
        if rc not in (0, 1):
            return rc
    else:
        print("[ingest] limit=0, skipping (stage already at target)")

    # --- workloads ----------------------------------------------------------
    if skip_workloads:
        print("[workloads] skipped by flag")
    else:
        print(f"\n[crud] duration={workload_duration}s workers={workers_workload}")
        with ResourceSampler(containers, crud_res):
            workload_crud.run(
                server_id=server_id, servers_path=servers_file, log_path=crud_log,
                duration=workload_duration, workers=workers_workload,
                mix_spec="C:10,R:60,U:25,D:5",
            )

        print(f"\n[search] duration={workload_duration}s workers={workers_workload}")
        with ResourceSampler(containers, search_res):
            workload_search.run(
                server_id=server_id, servers_path=servers_file, queries_path=queries_file,
                log_path=search_log, duration=workload_duration, workers=workers_workload,
                exclude={"patient_export"},
            )

    # --- disk snapshot ------------------------------------------------------
    print("\n[disk] docker system df -v")
    snapshot_disk(disk_path)

    elapsed = time.monotonic() - t_stage
    print(f"\n===== Stage {stage} / {server_id} done in {elapsed/60:.1f} min =====")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", type=int, required=True, choices=[1, 2, 3])
    ap.add_argument("--server", required=True, choices=list(SERVER_CONTAINERS))
    ap.add_argument("--run-id", required=True, help="results subdirectory name")
    ap.add_argument("--count", type=int, required=True,
                    help="Stage 1: patients to load. Stage 3: incremental patients.")
    ap.add_argument("--target-total", type=int, default=100_000,
                    help="Total patient count for Stage 2 end state (default 100000)")
    ap.add_argument("--workers-ingest", type=int, default=32)
    ap.add_argument("--workers-workload", type=int, default=64)
    ap.add_argument("--workload-duration", type=float, default=900.0)
    ap.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    ap.add_argument("--prereq-dir", type=Path, default=DEFAULT_PREREQ_DIR)
    ap.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--queries-file", type=Path, default=DEFAULT_QUERIES)
    ap.add_argument("--skip-workloads", action="store_true",
                    help="Do ingest only; skip CRUD + Search phases (useful during stage 2 long ingest)")
    args = ap.parse_args()
    return run_stage(
        stage=args.stage, server_id=args.server, run_id=args.run_id,
        count=args.count, target_total=args.target_total,
        workers_ingest=args.workers_ingest, workers_workload=args.workers_workload,
        workload_duration=args.workload_duration,
        input_dir=args.input_dir, prereq_dir=args.prereq_dir,
        results_root=args.results_root,
        servers_file=args.servers_file, queries_file=args.queries_file,
        skip_workloads=args.skip_workloads,
    )


if __name__ == "__main__":
    sys.exit(main())
