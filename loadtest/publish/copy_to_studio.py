"""
Copy a finished round artifact tree into the fhir-studio frontend's content
directory so the next `npm run build` includes it.

Layout for --kind conformance (default, back-compat):
  fhir-server-compare/results/rounds/<round_id>/conformance.json    [in]
  fhir-server-compare/results/rounds/<round_id>/methodology.md      [in, optional]
  fhir-server-compare/results/rounds/<round_id>/MANIFEST.json       [in, optional]
                                  ↓
  fhir-studio/frontend/src/content/conformance/<round_id>/{conformance.json,methodology.md,MANIFEST.json}
  fhir-studio/frontend/src/content/conformance/methodology.md   (canonical, cross-round)

Layout for --kind benchmark:
  fhir-server-compare/results/rounds/<round_id>/benchmark.json     [in]
  fhir-server-compare/results/rounds/<round_id>/MANIFEST.json      [in, optional]
                                  ↓
  fhir-studio/frontend/src/content/performance/<round_id>/{benchmark.json,MANIFEST.json}
  fhir-studio/frontend/src/content/performance/methodology.md   (canonical, cross-round — sourced from benchmark/methodology.md)

Atomic: writes to a sibling temp dir then `os.rename`s it over the destination,
per the eng-review note in leaderboard-plan.md (partial-copy hazard).

Validates the artifact against schema/round-v1.schema.json before the copy.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tempfile

import jsonschema


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schema" / "round-v1.schema.json"
DEFAULT_STUDIO = REPO_ROOT.parent / "fhir-studio"

# Per-kind configuration: which artifact file is the round's payload, which
# files to copy into the per-round studio directory, which studio content
# subdir to target, and where the canonical (cross-round) methodology lives.
KIND_CONFIG = {
    "conformance": {
        "artifact": "conformance.json",
        "copy_files": ("conformance.json", "methodology.md", "MANIFEST.json"),
        "studio_subdir": "conformance",
        "canonical_methodology_src": REPO_ROOT / "conformance" / "methodology.md",
    },
    "benchmark": {
        "artifact": "benchmark.json",
        "copy_files": ("benchmark.json", "MANIFEST.json"),
        "studio_subdir": "performance",
        "canonical_methodology_src": REPO_ROOT / "benchmark" / "methodology.md",
    },
}


def _validate(round_dir: pathlib.Path, artifact_name: str) -> None:
    cf = round_dir / artifact_name
    if not cf.is_file():
        raise SystemExit(f"missing {cf} — run the parse_report step for this kind first")
    schema = json.loads(SCHEMA_PATH.read_text())
    data = json.loads(cf.read_text())
    jsonschema.validate(data, schema)


def _copy_atomic(src_round: pathlib.Path, dest_round: pathlib.Path,
                 files: tuple[str, ...]) -> None:
    """Copy the named files into dest_round atomically.

    Strategy: write to a sibling temp dir, then rename over the destination.
    If a previous round dir exists, replace it atomically (rename swap).
    """
    dest_round.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{dest_round.name}.tmp-", dir=str(dest_round.parent)
    ) as tmp_str:
        tmp_dir = pathlib.Path(tmp_str)
        for name in files:
            src = src_round / name
            if src.is_file():
                shutil.copy2(src, tmp_dir / name)
        if dest_round.exists():
            shutil.rmtree(dest_round)
        os.replace(tmp_dir, dest_round)


def _copy_canonical_methodology(src: pathlib.Path,
                                studio_content: pathlib.Path) -> None:
    if not src.is_file():
        print(f"[skip] no canonical methodology at {src}", file=sys.stderr)
        return
    studio_content.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, studio_content / "methodology.md")
    print(f"[ok]   wrote {studio_content / 'methodology.md'}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True)
    p.add_argument("--kind", choices=sorted(KIND_CONFIG), default="conformance",
                   help="Which lane to publish (default: conformance)")
    p.add_argument("--studio-dir", default=str(DEFAULT_STUDIO),
                   help="path to fhir-studio repo root")
    args = p.parse_args()

    cfg = KIND_CONFIG[args.kind]
    round_id = args.round
    src_round = REPO_ROOT / "results" / "rounds" / round_id
    if not src_round.is_dir():
        raise SystemExit(f"no round dir: {src_round}")

    studio = pathlib.Path(args.studio_dir).resolve()
    if not studio.is_dir():
        raise SystemExit(f"--studio-dir not found: {studio}")

    studio_content = studio / "frontend" / "src" / "content" / cfg["studio_subdir"]
    dest_round = studio_content / round_id

    print(f"[validate] {src_round / cfg['artifact']}", file=sys.stderr)
    _validate(src_round, cfg["artifact"])

    print(f"[copy]     {src_round} -> {dest_round}", file=sys.stderr)
    _copy_atomic(src_round, dest_round, cfg["copy_files"])
    print(f"[ok]       wrote {dest_round}", file=sys.stderr)

    _copy_canonical_methodology(cfg["canonical_methodology_src"], studio_content)


if __name__ == "__main__":
    main()
