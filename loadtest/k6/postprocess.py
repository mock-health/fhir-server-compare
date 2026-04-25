#!/usr/bin/env python3
"""Convert k6's raw NDJSON output into the OpRecord JSONL shape the rest of
the mock.health pipeline expects.

The Python harness writes one line per op into crud.jsonl / search.jsonl via
loadtest.metrics.OpLog. Downstream (cell_summary.py, parse_report.py, the
published round artifact) reads that exact shape and does all percentile +
trust-gate math. Keeping the k6 port honest means producing the same JSONL
shape and letting the existing pipeline take over from there.

k6's `--out json=<path>` emits one NDJSON line per sample. Each line looks
like:

    {"type":"Point","metric":"search_latency_ms","data":{
        "time":"2026-04-24T20:00:00.123Z",
        "value":87.3,
        "tags":{"verb":"observation_by_code","server":"hapi","status":"200",
                "method":"GET","name":"...","scenario":"search",...}
    }}

Each sample carries `status` (auto-tag from k6's HTTP layer), our `verb`
tag, and ~20 more automatic tags we don't need.

We only keep `search_latency_ms` (or `crud_latency_ms`) points — one per
FHIR request — and project them onto the Python shape:

    {"workload":"search","verb":"observation_by_code","started_at":1777…,
     "duration_ms":87,"status_code":200,"ok":true,"note":null}

Why not have k6 write the JSONL directly: k6's JS runtime has no durable
file-write API (xk6-fs is third-party + not bundled). Relying on k6's
built-in `--out json` keeps the harness dependency-free.

Usage:
  python -m loadtest.k6.postprocess \\
      --k6-json results/k6/hapi-crud.ndjson \\
      --workload crud \\
      --out results/loadtest/<run>/checkpoint_1000/hapi/crud.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


# We key off k6's built-in http_req_duration metric because it's
# auto-tagged with the HTTP status — the one field we can't reliably
# derive at .add() time from a custom Trend without plumbing it through
# every call site. The workload scripts pass `tags: { verb }` on every
# http.* call so verb rides along on this same stream.
TARGET_METRIC = "http_req_duration"

# k6 tags samples produced inside setup() with `group: "::setup"`. Those
# are the patient-id harvest + sample-pool pre-phase calls; filtering
# them out here keeps workload percentiles honest.
SETUP_GROUP = "::setup"


def _parse_time(ts: str) -> float:
    """k6 timestamp (RFC3339 with 'Z') -> unix seconds float."""
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts).timestamp()


def convert(k6_json_path: Path, workload: str, out_path: Path) -> int:
    """Stream k6 NDJSON → OpRecord JSONL. Returns count of emitted records."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with k6_json_path.open() as fin, out_path.open("w") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            try:
                point = json.loads(line)
            except json.JSONDecodeError:
                continue
            if point.get("type") != "Point":
                continue
            if point.get("metric") != TARGET_METRIC:
                continue
            data = point.get("data") or {}
            tags = data.get("tags") or {}
            # Skip setup() traffic — those are the harvest / sample-pool
            # pings, not the timed workload.
            if tags.get("group") == SETUP_GROUP:
                continue
            verb = tags.get("verb") or "?"
            # k6 auto-tags status as a string; convert to int. A missing
            # tag means the request never reached a server (connection
            # error before headers), which k6 represents with
            # `error_code` tag — we score it as a transport failure by
            # keeping status_code=0 / ok=false.
            try:
                status = int(tags.get("status") or 0)
            except ValueError:
                status = 0
            ok = 200 <= status < 300
            duration_ms = int(round(float(data.get("value") or 0)))
            started_at = _parse_time(data.get("time")) if data.get("time") else 0.0
            rec = {
                "workload": workload,
                "verb": verb,
                "started_at": started_at,
                "duration_ms": duration_ms,
                "status_code": status,
                "ok": ok,
            }
            note = tags.get("note") or tags.get("template")
            if note:
                rec["note"] = note
            fout.write(json.dumps(rec, separators=(",", ":")) + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k6-json", type=Path, required=True,
                    help="Path to the NDJSON file k6 wrote via --out json=...")
    ap.add_argument("--workload", choices=("crud", "search"), required=True)
    ap.add_argument("--out", type=Path, required=True,
                    help="Destination JSONL, e.g. .../<cell>/search.jsonl")
    args = ap.parse_args()
    if not args.k6_json.is_file():
        print(f"ERROR: {args.k6_json} not found", file=sys.stderr)
        return 2
    n = convert(args.k6_json, args.workload, args.out)
    print(f"[ok] wrote {n:,} records to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
