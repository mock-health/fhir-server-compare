"""
Walk results/loadtest/<run-id>/checkpoint_*/<server>/ and fold per-request
JSONL into a round artifact:
  results/rounds/<round>/benchmark.json (conforms to schema/round-v1.schema.json)

Profiles (workloads) live in benchmark/profiles/{ingest,crud,search}.yaml.
For each (server, profile) we emit one cell whose `evidence[]` is the
per-checkpoint series (1K / 4K / 16K / 64K). The `status` is derived from
the p50 (median) at the largest checkpoint the server reached;
`max_checkpoint_reached` surfaces that checkpoint so the matrix can show
"p50 @ 64K"-style labels. p95/p99 are still recorded in each evidence row
as tail evidence but are not the headline — a 2-minute run produces too
few samples on slow cells for p99 to be stable.

Re-uses `loadtest.report.{ingest_metrics, workload_metrics, parse_jsonl}` so
the percentile math is identical to the markdown report. Benchmarks with no
`cell_complete.json` sentinel are skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import sys
from typing import Any

import yaml

from fhirbench.benchmark.cell_summary import (  # noqa: E402
    USE_OK_ONLY,
    _workload_summary,
)
from fhirbench.harness.report import disk_used_bytes, parse_jsonl  # noqa: E402


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SERVERS_YAML = REPO_ROOT / "config" / "servers.yaml"
PROFILES_DIR = REPO_ROOT / "profiles" / "benchmark"
RESULTS_LOADTEST = REPO_ROOT / "results" / "loadtest"

# Local 6-server roster — excludes managed GCP (load-tests against a paid
# managed service would be unfair and expensive).
ROSTER = ("hapi", "aidbox", "medplum", "msfhir", "blaze", "spark")

# Cell-color thresholds for p50 (median) latency at max_checkpoint_reached.
# Coarse by design — sub-ms differences are noise; order-of-magnitude
# differences are the story. Matches benchmark/methodology.md bands.
# Bands shifted 10× tighter than the previous p99 thresholds, reflecting
# that p50 is typically ~10× smaller than p99 under light tail load.
GREEN_MAX_MS = 100.0
AMBER_MAX_MS = 1000.0


def cell_color(p50_ms: float | None) -> str:
    if p50_ms is None:
        return "grey"
    if p50_ms <= GREEN_MAX_MS:
        return "green"
    if p50_ms <= AMBER_MAX_MS:
        return "amber"
    return "red"


def load_server_meta() -> list[dict]:
    with SERVERS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    out: list[dict] = []
    for raw in cfg["servers"]:
        if raw["id"] not in ROSTER:
            continue
        entry: dict[str, Any] = {
            "id": raw["id"],
            "label": raw.get("label", raw["id"]),
            "version": raw.get("version", "unknown"),
        }
        for key in ("image", "image_digest", "source_url", "dockerfile_url",
                    "homepage", "license"):
            val = raw.get(key)
            if isinstance(val, str) and val:
                entry[key] = val
        out.append(entry)
    return out


def load_profile_specs() -> list[dict]:
    profiles: list[dict] = []
    if not PROFILES_DIR.is_dir():
        return profiles
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        with path.open() as f:
            profiles.append(yaml.safe_load(f))
    return profiles


def discover_checkpoints(run_dir: pathlib.Path) -> list[int]:
    out: list[int] = []
    for d in sorted(run_dir.iterdir()):
        if not d.is_dir():
            continue
        name = d.name
        if not name.startswith("checkpoint_"):
            continue
        try:
            out.append(int(name.split("_", 1)[1]))
        except ValueError:
            continue
    return out


def _evidence_for_workload(cell_dir: pathlib.Path, checkpoint: int,
                           ran_at: str, jsonl_name: str,
                           use_ok_only: bool,
                           server_id: str) -> dict | None:
    """Build one evidence row from crud.jsonl or search.jsonl.

    Delegates percentile/trust math to `cell_summary._workload_summary` so
    the round artifact and per-cell cell_summary.json can never disagree.
    The trust block decides whether the heatmap renders this cell solid or
    desaturated (see schema definitions/trust).
    """
    records = parse_jsonl(cell_dir / jsonl_name)
    summary = _workload_summary(records, use_ok_only)
    if summary is None:
        return None
    row: dict = {
        "checkpoint":   checkpoint,
        "p50_ms":       summary["p50_ms"],
        "p75_ms":       summary["p75_ms"],
        "p90_ms":       summary["p90_ms"],
        "p95_ms":       summary["p95_ms"],
        "p99_ms":       summary["p99_ms"],
        "ops_per_s":    summary["ops_per_s"],
        "ops_ok_per_s": summary["ops_ok_per_s"],
        "n_ok":         summary["n_ok"],
        "n_err":        summary["n_err"],
        "error_rate":   summary["error_rate"],
        "trust":        summary["trust"],
        "source":       "loadtest-ramp",
        "ran_at":       ran_at,
        "per_verb":     summary["per_verb"],
    }
    db_bytes = disk_used_bytes(cell_dir / "disk.json", server_id)
    if db_bytes is not None:
        row["db_size_bytes"] = db_bytes
    # Carry phase accounting into benchmark.json for CRUD cells so
    # downstream renderers can surface reliability flags per verb.
    phases_path = cell_dir / "crud_phases.json"
    if jsonl_name == "crud.jsonl" and phases_path.is_file():
        try:
            row["crud_phases"] = json.loads(phases_path.read_text())
        except Exception:
            pass
    return row


def _cell_complete_ts(cell_dir: pathlib.Path) -> str | None:
    p = cell_dir / "cell_complete.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    ts = data.get("completed_at")
    if isinstance(ts, (int, float)):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return None


def build_cells(run_dir: pathlib.Path,
                server_ids: list[str],
                profile_ids: list[str]) -> list[dict]:
    """For each (server, profile) produce a cell whose evidence[] is the
    per-checkpoint series. Cells with zero evidence rows (no checkpoint
    completed for this profile) become grey."""
    checkpoints = discover_checkpoints(run_dir)

    # Benchmark profiles are the core scaling workloads. Ingest is excluded by
    # design — it's setup tax, not a published metric: vendors reasonably argue
    # Synthea bundles should go through their bulk $import path, not transaction
    # POSTs, so per-bundle POST p99 is not a fair scaling signal.
    # USE_OK_ONLY is shared with cell_summary so the round artifact and the
    # per-cell summaries apply the same ok-only-vs-all-requests convention.
    WORKLOAD_JSONL = {"crud": "crud.jsonl", "search": "search.jsonl"}

    cells: list[dict] = []
    for sid in server_ids:
        for pid in profile_ids:
            if pid not in WORKLOAD_JSONL:
                continue
            evidence: list[dict] = []
            max_reached = 0
            for ckpt in checkpoints:
                cell_dir = run_dir / f"checkpoint_{ckpt:08d}" / sid
                if not cell_dir.is_dir():
                    continue
                ran_at = _cell_complete_ts(cell_dir)
                if ran_at is None:
                    # Skip cells without a completion sentinel — partial runs
                    # shouldn't leak into the published artifact.
                    continue
                row = _evidence_for_workload(
                    cell_dir, ckpt, ran_at,
                    WORKLOAD_JSONL[pid], USE_OK_ONLY[pid],
                    sid,
                )
                if row is None:
                    continue
                evidence.append(row)
                max_reached = max(max_reached, ckpt)

            if not evidence:
                cells.append({
                    "server_id": sid,
                    "profile_id": pid,
                    "status": "grey",
                    "percentage": None,
                    "passed": {"MUST": 0, "SHOULD": 0, "MAY": 0},
                    "total":  {"MUST": 0, "SHOULD": 0, "MAY": 0},
                    "evidence": [],
                })
                continue

            # Headline status from p50 (median) at the largest checkpoint reached.
            # trust is mirrored up from the headline evidence row so the heatmap
            # can decide whether to desaturate without descending into evidence[].
            headline = evidence[-1]  # evidence is ordered by ascending checkpoint
            cells.append({
                "server_id": sid,
                "profile_id": pid,
                "status": cell_color(headline["p50_ms"]),
                "percentage": None,
                "passed": {"MUST": 0, "SHOULD": 0, "MAY": 0},
                "total":  {"MUST": 0, "SHOULD": 0, "MAY": 0},
                "evidence": evidence,
                "max_checkpoint_reached": max_reached,
                "ran_at": headline["ran_at"],
                "trust": headline.get("trust"),
            })
    return cells


def load_hardware_meta(run_dir: pathlib.Path) -> dict:
    """Harvest the hardware section from meta.json if present.

    meta.json's shape is richer than the schema's hardware block — we
    collapse it to the schema-allowed fields (plus the schema's
    additionalProperties: true lets extras ride along untouched).
    """
    p = run_dir / "meta.json"
    if not p.is_file():
        return {}
    try:
        meta = json.loads(p.read_text())
    except Exception:
        return {}
    host = meta.get("host", {}) or {}
    cpu = meta.get("cpu", {}) or {}
    memory = meta.get("memory", {}) or {}
    docker = meta.get("docker", {}) or {}
    out: dict[str, Any] = {}
    if host.get("hostname"):
        out["host"] = host["hostname"]
    if cpu.get("model"):
        out["cpu_model"] = cpu["model"]
    try:
        out["cpu_count"] = int(cpu.get("logical_cpus") or 0) or None
    except (TypeError, ValueError):
        pass
    if out.get("cpu_count") is None:
        out.pop("cpu_count", None)
    try:
        ram_kb = int(memory.get("total_kb") or 0)
        if ram_kb:
            out["ram_bytes"] = ram_kb * 1024
    except (TypeError, ValueError):
        pass
    if host.get("kernel"):
        out["kernel"] = host["kernel"]
    if docker.get("engine"):
        out["docker_version"] = docker["engine"]
    # Additional context that readers want — governor + THP had to be tuned
    # explicitly to get reproducible p99s, so surfacing them is fair play.
    if cpu.get("governor"):
        out["cpu_governor"] = cpu["governor"]
    thp = (meta.get("kernel_tuning") or {}).get("transparent_hugepage")
    if thp:
        out["transparent_hugepage"] = thp
    return out


def build_round(round_id: str,
                run_id: str,
                methodology_version: str = "v1.0-draft") -> dict:
    run_dir = RESULTS_LOADTEST / run_id
    if not run_dir.is_dir():
        raise SystemExit(f"no loadtest run at {run_dir}")

    servers = load_server_meta()
    profiles = load_profile_specs()
    if not profiles:
        raise SystemExit(f"no profiles configured under {PROFILES_DIR}")

    server_ids = [s["id"] for s in servers]
    profile_ids = [p["id"] for p in profiles]

    cells = build_cells(run_dir, server_ids, profile_ids)
    hardware = load_hardware_meta(run_dir)

    artifact: dict[str, Any] = {
        "round_id": round_id,
        "kind": "benchmark",
        "schema_version": "round-v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "methodology_version": methodology_version,
        "servers": servers,
        "profiles": profiles,
        "cells": cells,
    }
    if hardware:
        artifact["hardware"] = hardware
    return artifact


def write_manifest(round_dir: pathlib.Path, artifacts: list[str]) -> None:
    entries: list[dict] = []
    for name in artifacts:
        p = round_dir / name
        if not p.is_file():
            continue
        size = p.stat().st_size
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        entries.append({"path": name, "size_bytes": size, "sha256": digest})
    # Merge with any existing manifest (conformance may have written one first)
    manifest_path = round_dir / "MANIFEST.json"
    existing: dict[str, Any] = {"round_id": round_dir.name, "artifacts": []}
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text())
        except Exception:
            pass
    by_path = {e["path"]: e for e in existing.get("artifacts", [])}
    for e in entries:
        by_path[e["path"]] = e
    existing["artifacts"] = sorted(by_path.values(), key=lambda e: e["path"])
    existing["round_id"] = round_dir.name
    manifest_path.write_text(json.dumps(existing, indent=2) + "\n")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True)
    p.add_argument("--run-id", required=True,
                   help="loadtest run directory name under results/loadtest/")
    p.add_argument("--out", default=None)
    p.add_argument("--methodology-version", default="v1.0-draft")
    args = p.parse_args()

    out_path = pathlib.Path(args.out) if args.out else (
        REPO_ROOT / "results" / "rounds" / args.round / "benchmark.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    artifact = build_round(args.round, args.run_id,
                           methodology_version=args.methodology_version)
    out_path.write_text(json.dumps(artifact, indent=2) + "\n")

    # methodology.md in the round dir belongs to conformance (canonical lane
    # differs per kind). Benchmark's canonical methodology lives at
    # benchmark/methodology.md and is copied to the studio by copy_to_studio.

    write_manifest(out_path.parent, ["benchmark.json", "conformance.json", "methodology.md"])
    print(f"[ok] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
