"""
Walk results/conformance/<round>/ and produce the round artifact:
  results/rounds/<round>/conformance.json (conforms to schema/round-v1.schema.json)

Per (server, testscript) it folds one TestReport into the cell counters. Bucket
(MUST | SHOULD | MAY) is encoded in the TestScript file path:
  conformance/testscripts/<profile_id>/<bucket>/<test-id>.json

A test passes iff every `assert` action inside it passes. setUp / tearDown
errors mark the test as `error` (counted against total, not as `fail`).

Profiles declared in conformance/profiles/<profile_id>.yaml are merged with
the cells. Profiles without any reports (e.g. us-core-6.1) ship as
`status: "not-yet-tested"` with grey cells.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
from collections import defaultdict
from typing import Any

import yaml

from .run import ROSTER


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVERS_YAML = REPO_ROOT / "servers.yaml"
PROFILES_DIR = REPO_ROOT / "conformance" / "profiles"
TESTSCRIPTS_DIR = REPO_ROOT / "conformance" / "testscripts"


# Cell-color thresholds. Tunable; mirrored on the frontend.
GREEN_MIN = 95.0
AMBER_MIN = 70.0


def cell_color(percentage: float | None) -> str:
    if percentage is None:
        return "grey"
    if percentage >= GREEN_MIN:
        return "green"
    if percentage >= AMBER_MIN:
        return "amber"
    return "red"


def _extract_test_meta(ts: dict) -> dict:
    """Pull human-facing metadata off a TestScript for round-artifact evidence.

    Returns a dict with any of {title, description, spec} present. `spec` is
    built from the first TestScript.relatedArtifact entry — FHIR's canonical
    citation element — so the frontend can render each evidence row as a
    clickable link to the exact hl7.org anchor the test exercises.
    """
    meta: dict[str, Any] = {}
    title = ts.get("title")
    if isinstance(title, str) and title:
        meta["title"] = title
    description = ts.get("description")
    if isinstance(description, str) and description:
        meta["description"] = description
    related = ts.get("relatedArtifact")
    if isinstance(related, list) and related:
        first = related[0] or {}
        url = first.get("url")
        if isinstance(url, str) and url:
            spec: dict[str, str] = {"url": url}
            label = first.get("label")
            if isinstance(label, str) and label:
                spec["label"] = label
            meta["spec"] = spec
    return meta


def _enumerate_profile_tests(profile_id: str) -> list[tuple[str, str, pathlib.Path, dict]]:
    """List every TestScript in a profile as (test_name, bucket, path, meta).

    Used to synthesize na-outcome evidence when a server's applicability probe
    for this profile fails — we emit one evidence entry per script so the
    ServerPage can still show the list of tests, just with outcome=na.
    """
    out: list[tuple[str, str, pathlib.Path, dict]] = []
    profile_dir = TESTSCRIPTS_DIR / profile_id
    if not profile_dir.is_dir():
        return out
    for bucket_dir in sorted(profile_dir.iterdir()):
        if not bucket_dir.is_dir() or bucket_dir.name not in {"MUST", "SHOULD", "MAY"}:
            continue
        for ts_path in sorted(bucket_dir.glob("*.json")):
            try:
                ts = json.loads(ts_path.read_text())
            except Exception:
                continue
            out.append((ts.get("name") or ts_path.stem, bucket_dir.name, ts_path, _extract_test_meta(ts)))
    return out


def _expand_env(value: str) -> str:
    import os
    if not isinstance(value, str):
        return value
    while "${" in value:
        s = value.index("${"); e = value.index("}", s)
        spec = value[s + 2:e]
        if ":-" in spec:
            n, d = spec.split(":-", 1)
        else:
            n, d = spec, ""
        value = value[:s] + os.environ.get(n, d) + value[e + 1:]
    return value


def load_server_meta() -> list[dict]:
    """Restrict matrix rows to the conformance ROSTER (defined in run.py).
    Every server in the roster is locally reproducible via docker-compose;
    no paid licenses or managed services are included."""
    with SERVERS_YAML.open() as f:
        cfg = yaml.safe_load(f)
    out = []
    for raw in cfg["servers"]:
        if raw["id"] not in ROSTER:
            continue
        entry: dict[str, Any] = {
            "id": raw["id"],
            "label": raw.get("label", raw["id"]),
            "version": raw.get("version", "unknown"),
        }
        # Optional provenance fields — surface in the UI as header links so
        # a reader can trace exactly what was tested.
        for key in ("image", "image_digest", "source_url", "dockerfile_url",
                    "homepage", "license"):
            val = raw.get(key)
            if isinstance(val, str) and val:
                entry[key] = val
        out.append(entry)
    return out


def load_profile_specs() -> list[dict]:
    """Profiles defined in conformance/profiles/<id>.yaml — minimal frontmatter
    (id, label, version, spec_url, status, buckets). Per-profile testscript
    folders are looked up by id."""
    profiles = []
    if not PROFILES_DIR.is_dir():
        return profiles
    for path in sorted(PROFILES_DIR.glob("*.yaml")):
        with path.open() as f:
            profiles.append(yaml.safe_load(f))
    return profiles


def _testscript_bucket(report_path: pathlib.Path,
                       round_dir: pathlib.Path) -> tuple[str, str] | None:
    """From a per-server TestReport file, recover (profile_id, bucket).

    AEGIS writes one TestReport per source TestScript, embedding the source
    path in `TestReport.testScript.reference`. We read that reference and
    derive: conformance/testscripts/<profile_id>/<bucket>/<test-id>.json
    """
    try:
        report = json.loads(report_path.read_text())
    except Exception as e:
        print(f"[warn] {report_path}: cannot parse json ({e})", file=sys.stderr)
        return None
    ref = (report.get("testScript") or {}).get("reference", "")
    # Reference format varies by AEGIS version. Heuristic: split on "testscripts/"
    # and use the suffix.
    marker = "testscripts/"
    if marker not in ref:
        return None
    suffix = ref.split(marker, 1)[1].lstrip("/")
    parts = suffix.split("/")
    if len(parts) < 3:
        return None
    profile_id, bucket = parts[0], parts[1]
    if bucket not in {"MUST", "SHOULD", "MAY"}:
        return None
    return profile_id, bucket


def _summarize_report(report: dict) -> tuple[str, str]:
    """Return (test_outcome, first_failure_message) for one TestReport.

    Walks `setup`, `test[*]`, `teardown` actions. A test is "pass" iff every
    assert in test[*] passes; setup or teardown error escalates to "error".
    """
    first_fail = ""
    for action in (report.get("setup") or {}).get("action", []):
        op = action.get("operation") or {}
        if op.get("result") in {"fail", "error"}:
            return "error", op.get("message", "setup failed")
        a = action.get("assert") or {}
        if a.get("result") in {"fail", "error"}:
            return "error", a.get("message", "setup assert failed")
    overall = "pass"
    for test in report.get("test", []):
        for action in test.get("action", []):
            op = action.get("operation") or {}
            if op.get("result") in {"fail", "error"} and not first_fail:
                first_fail = op.get("message", "operation failed")
                overall = "fail"
            a = action.get("assert") or {}
            if a.get("result") in {"fail", "error"}:
                if not first_fail:
                    first_fail = a.get("message") or a.get("label", "assert failed")
                overall = "fail"
            elif a.get("result") == "skip" and overall == "pass":
                # Skip doesn't fail the test; record no message.
                pass
    return overall, first_fail


def build_round(round_id: str,
                methodology_version: str = "v1.0-draft") -> dict:
    round_reports_dir = REPO_ROOT / "results" / "conformance" / round_id
    if not round_reports_dir.is_dir():
        raise SystemExit(f"no reports for round {round_id} at {round_reports_dir}")

    servers = load_server_meta()
    profiles = load_profile_specs()
    if not profiles:
        raise SystemExit(f"no profiles configured under {PROFILES_DIR}")

    server_ids = [s["id"] for s in servers]
    profile_ids = [p["id"] for p in profiles]
    profile_by_id = {p["id"]: p for p in profiles}

    # cell counters: cells[(server_id, profile_id)] = {"passed": {bucket: n}, "total": {...}, "evidence": [...], "na_reason": str|None}
    cells: dict[tuple[str, str], dict[str, Any]] = {
        (s, p): {
            "passed": {"MUST": 0, "SHOULD": 0, "MAY": 0},
            "total":  {"MUST": 0, "SHOULD": 0, "MAY": 0},
            "evidence": [],
            "na_reason": None,
        }
        for s in server_ids
        for p in profile_ids
    }

    # Walk per-server TestReport JSON files.
    for server_dir in sorted(round_reports_dir.iterdir()):
        if not server_dir.is_dir():
            continue
        sid = server_dir.name
        if sid not in server_ids:
            print(f"[warn] reports for unknown server: {sid}", file=sys.stderr)
            continue

        # First pass: read applicability markers. A marker file
        # _applicability_<profile>.json means the server's probe for that
        # profile tripped N/A — no TestReports were run, so we synthesize
        # na-outcome evidence from the profile's testscript list.
        for marker_path in sorted(server_dir.glob("_applicability_*.json")):
            try:
                marker = json.loads(marker_path.read_text())
            except Exception as e:
                print(f"[warn] {marker_path}: cannot parse ({e})", file=sys.stderr)
                continue
            if marker.get("status") != "na":
                continue
            pid = marker.get("profile_id")
            if pid not in profile_by_id:
                continue
            reason = marker.get("reason", "not applicable")
            cell = cells[(sid, pid)]
            cell["na_reason"] = reason
            for test_name, bucket, ts_path, test_meta in _enumerate_profile_tests(pid):
                cell["evidence"].append({
                    "test_id": test_name,
                    "bucket": bucket,
                    "outcome": "na",
                    "details": reason,
                    "source": "aegis-testscript-engine",
                    "report_path": str(ts_path.relative_to(REPO_ROOT)),
                    **test_meta,
                })

        for report_path in sorted(server_dir.rglob("*.json")):
            if report_path.name.startswith("_"):
                continue
            classification = _testscript_bucket(report_path, round_reports_dir)
            if classification is None:
                continue
            profile_id, bucket = classification
            if profile_id not in profile_by_id:
                continue
            try:
                report = json.loads(report_path.read_text())
            except Exception:
                continue
            outcome, message = _summarize_report(report)
            cell = cells[(sid, profile_id)]
            cell["total"][bucket] += 1
            if outcome == "pass":
                cell["passed"][bucket] += 1
            # Fold TestScript metadata (title, description, spec citation) into
            # evidence so the round artifact is self-linking — each row carries
            # the hl7.org anchor for the spec section it tests.
            ts_ref = (report.get("testScript") or {}).get("reference", "")
            ts_path = REPO_ROOT / ts_ref if ts_ref else None
            test_meta: dict = {}
            if ts_path and ts_path.is_file():
                try:
                    test_meta = _extract_test_meta(json.loads(ts_path.read_text()))
                except Exception:
                    test_meta = {}
            cell["evidence"].append({
                "test_id": report.get("name", report_path.stem),
                "bucket": bucket,
                "outcome": outcome,
                "details": message[:500] if message else "",
                "source": "aegis-testscript-engine",
                "report_path": str(report_path.relative_to(REPO_ROOT)),
                **test_meta,
            })

    # Build cells array. For profiles with status=not-yet-tested, emit grey
    # cells with null percentage and empty counters.
    out_cells = []
    for sid in server_ids:
        for pid in profile_ids:
            profile_status = profile_by_id[pid].get("status", "active")
            if profile_status == "not-yet-tested":
                out_cells.append({
                    "server_id": sid,
                    "profile_id": pid,
                    "status": "grey",
                    "percentage": None,
                    "passed": {"MUST": 0, "SHOULD": 0, "MAY": 0},
                    "total":  {"MUST": 0, "SHOULD": 0, "MAY": 0},
                    "evidence": [],
                })
                continue
            c = cells[(sid, pid)]
            if c["na_reason"]:
                # Applicability probe tripped N/A. Passed/total stay zero and
                # percentage is null so the cell isn't counted in any server
                # or profile aggregate — the evidence list still shows which
                # tests would have run, so the ServerPage stays informative.
                out_cells.append({
                    "server_id": sid,
                    "profile_id": pid,
                    "status": "na",
                    "percentage": None,
                    "passed": {"MUST": 0, "SHOULD": 0, "MAY": 0},
                    "total":  {"MUST": 0, "SHOULD": 0, "MAY": 0},
                    "evidence": c["evidence"],
                    "na_reason": c["na_reason"],
                })
                continue
            tot = sum(c["total"].values())
            passed = sum(c["passed"].values())
            pct = (100.0 * passed / tot) if tot > 0 else None
            out_cells.append({
                "server_id": sid,
                "profile_id": pid,
                "status": cell_color(pct),
                "percentage": round(pct, 1) if pct is not None else None,
                "passed": c["passed"],
                "total":  c["total"],
                "evidence": c["evidence"],
            })

    return {
        "round_id": round_id,
        "kind": "conformance",
        "schema_version": "round-v1",
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "methodology_version": methodology_version,
        "servers": servers,
        "profiles": profiles,
        "cells": out_cells,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True)
    p.add_argument("--out", default=None,
                   help="output path (default: results/rounds/<round>/conformance.json)")
    p.add_argument("--methodology-version", default="v1.0-draft")
    args = p.parse_args()

    out_path = pathlib.Path(args.out) if args.out else (
        REPO_ROOT / "results" / "rounds" / args.round / "conformance.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    artifact = build_round(args.round, methodology_version=args.methodology_version)
    out_path.write_text(json.dumps(artifact, indent=2) + "\n")
    print(f"[ok] wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
