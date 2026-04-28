#!/usr/bin/env python3
"""Generate N Synthea patient bundles for the load test.

Wraps vanilla Synthea (github.com/synthetichealth/synthea) — clones + builds
on first run into ./synthea/, then invokes `run_synthea -p N -s SEED State City`.

The load test needs one transaction bundle per patient. Synthea's default
output at `output/fhir/*.json` is already in that shape, so the wrapper just
moves the files into `data/loadtest/fhir/` where the loader expects them.

Determinism: fixed seed 42 by default. Stage 3's "+1K incremental" reuses the
same seed with a higher `--count`; only the NEW patients are loaded (the
stage orchestrator slices the sorted file list).

Usage:
    python -m fhirbench.harness.generate --count 1000
    python -m fhirbench.harness.generate --count 100000 --seed 42
    python -m fhirbench.harness.generate --count 10 --state Massachusetts --city Boston

Disk: ~1.5 MB per patient on disk (uncompressed FHIR JSON). 100K patients is
~150 GB; script errors if the data volume has <200 GB free.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SYNTHEA_DIR = REPO_ROOT / "synthea"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "data" / "loadtest" / "fhir"
# Practitioner + hospital bundles are split off so the loader can ingest them
# first — Synthea's patient bundles conditionally reference providers/orgs by
# identifier, and those match URLs 404 until the prerequisite bundles land.
DEFAULT_PREREQ_DIR = REPO_ROOT / "data" / "loadtest" / "prerequisites"
SYNTHEA_REPO = "https://github.com/synthetichealth/synthea.git"
# Pinned so benchmark inputs are reproducible across machines and time.
# To refresh: check out a newer commit/tag, regenerate a test cohort, and
# confirm the loader still accepts the bundles before updating this pin.
SYNTHEA_COMMIT = "aa0772fb5e92e48a776c51508c00eddc0d9d27ff"  # master @ 2026-03-05 (4.0.1-SNAPSHOT)
MIN_FREE_GB_PER_1K = 2  # ~1.5 GB/1K patients + buffer


def ensure_synthea(synthea_dir: Path) -> None:
    """Clone + build Synthea at the pinned commit if not already present."""
    run_script = synthea_dir / "run_synthea"
    if run_script.exists():
        return
    if synthea_dir.exists() and not run_script.exists():
        print(f"ERROR: {synthea_dir} exists but has no run_synthea script.", file=sys.stderr)
        print("  Delete the directory and rerun, or point --synthea-dir at a built checkout.", file=sys.stderr)
        sys.exit(2)
    print(f"Cloning Synthea into {synthea_dir} (pinned to {SYNTHEA_COMMIT[:10]}) ...")
    subprocess.run(["git", "clone", SYNTHEA_REPO, str(synthea_dir)], check=True)
    subprocess.run(["git", "checkout", SYNTHEA_COMMIT], cwd=str(synthea_dir), check=True)
    print("Building (this takes ~45s) ...")
    subprocess.run(
        ["./gradlew", "build", "-x", "test"],
        cwd=str(synthea_dir),
        check=True,
    )


def precheck_disk(target_dir: Path, count: int) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    stat = shutil.disk_usage(target_dir)
    free_gb = stat.free / (1024**3)
    need_gb = max(5, (count / 1000) * MIN_FREE_GB_PER_1K)
    if free_gb < need_gb:
        print(
            f"ERROR: need ~{need_gb:.0f} GB free on {target_dir}; only {free_gb:.0f} GB available.",
            file=sys.stderr,
        )
        sys.exit(2)


def existing_patient_count(output_dir: Path) -> int:
    """Count patient bundles already sitting in data/loadtest/fhir/."""
    if not output_dir.exists():
        return 0
    return sum(1 for _ in output_dir.glob("*.json"))


def run_synthea(synthea_dir: Path, count: int, seed: int, state: str, city: str) -> None:
    cmd = [
        "./run_synthea",
        "-p", str(count),
        "-s", str(seed),
        "-cs", str(seed),
        # keep generation lean; we don't need CSV / CCDA / HTML, just FHIR JSON.
        # hospital + practitioner exports are REQUIRED — patient bundles use
        # conditional references like `Practitioner?identifier=...` that only
        # resolve if those bundles have been ingested first.
        "--exporter.fhir.export=true",
        "--exporter.ccda.export=false",
        "--exporter.csv.export=false",
        "--exporter.hospital.fhir.export=true",
        "--exporter.practitioner.fhir.export=true",
        "--exporter.html.export=false",
        # transaction-bundle shape is what the loader POSTs; default export is
        # already `transaction`, pin it to be safe.
        "--exporter.fhir.transaction_bundle=true",
        state,
        city,
    ]
    print(f"Running: {' '.join(cmd)}\n(cwd={synthea_dir})")
    subprocess.run(cmd, cwd=str(synthea_dir), check=True)


def collect_bundles(synthea_dir: Path, output_dir: Path, prereq_dir: Path) -> tuple[int, int]:
    """Split Synthea output into prerequisites and patient bundles.

    - hospitalInformation*.json / practitionerInformation*.json -> prereq_dir
      (loader must POST these first; patient bundles reference them by identifier)
    - everything else                                           -> output_dir
    Returns (prereq_count, patient_count).
    """
    src = synthea_dir / "output" / "fhir"
    if not src.exists():
        print(f"ERROR: expected Synthea output at {src}, not found.", file=sys.stderr)
        sys.exit(1)
    output_dir.mkdir(parents=True, exist_ok=True)
    prereq_dir.mkdir(parents=True, exist_ok=True)
    prereq = 0
    patients = 0
    for f in sorted(src.glob("*.json")):
        if f.name.startswith(("hospitalInformation", "practitionerInformation")):
            shutil.move(str(f), str(prereq_dir / f.name))
            prereq += 1
        else:
            shutil.move(str(f), str(output_dir / f.name))
            patients += 1
    return prereq, patients


def ensure_count(
    count: int, seed: int, state: str, city: str,
    synthea_dir: Path, output_dir: Path, prereq_dir: Path,
) -> int:
    """Ensure at least `count` patient bundles exist under output_dir.

    Idempotent: if there are already enough, return immediately. Otherwise
    runs Synthea batches until the target is met. Each batch uses a unique
    seed (seed + existing_count) so subsequent runs produce fresh patients
    rather than re-rolling the same 1..N set. Batches cap at 5K patients each
    to keep JVM heap pressure reasonable and to checkpoint progress — an
    interrupted run resumes at the batch boundary.
    """
    existing = existing_patient_count(output_dir)
    if existing >= count:
        print(f"Already have {existing} patients ≥ {count}; skipping Synthea.")
        return 0
    print(f"Have {existing} patient bundles; need {count}. Generating {count - existing} more...")
    precheck_disk(output_dir, count - existing)
    ensure_synthea(synthea_dir)
    batches_run = 0
    BATCH = 5000
    while existing < count:
        batch_count = min(BATCH, count - existing)
        batch_seed = seed + existing  # unique seed per batch = non-overlapping patients
        print(f"\n--- batch {batches_run + 1}: synthea -p {batch_count} -s {batch_seed} ---")
        run_synthea(synthea_dir, batch_count, batch_seed, state, city)
        prereq, patients = collect_bundles(synthea_dir, output_dir, prereq_dir)
        print(f"  +{patients} patient bundles, +{prereq} prereq bundles")
        existing = existing_patient_count(output_dir)
        print(f"  total now: {existing}")
        batches_run += 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, required=True, help="Target total patient count under output-dir")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--state", default="Massachusetts")
    ap.add_argument("--city", default="Boston")
    ap.add_argument("--synthea-dir", type=Path, default=DEFAULT_SYNTHEA_DIR)
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    ap.add_argument("--prereq-dir", type=Path, default=DEFAULT_PREREQ_DIR)
    args = ap.parse_args()
    return ensure_count(
        count=args.count, seed=args.seed, state=args.state, city=args.city,
        synthea_dir=args.synthea_dir, output_dir=args.output_dir, prereq_dir=args.prereq_dir,
    )


if __name__ == "__main__":
    sys.exit(main())
