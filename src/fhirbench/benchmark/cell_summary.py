"""Per-cell summary for a single (server, checkpoint) ramp output.

Reads crud.jsonl / search.jsonl from a cell directory and emits a compact
`cell_summary.json` next to `cell_complete.json`. Same percentile math as
`parse_report.py` (both go through `fhirbench.harness.report.workload_metrics`) so
the round artifact and the per-cell file never disagree.

The summary is the "three-number honest publication" shape — p50, err-rate,
ok-throughput — plus a trust block whose `reliable` flag drives heatmap
desaturation.

Trust thresholds (derived from the rule of thumb: quantile q needs
~1/(1-q) samples above it to be stable, so ~10× that for a ±10% read):

    p50_trustworthy  n_ok >=   30
    p75_trustworthy  n_ok >=   40
    p90_trustworthy  n_ok >=  100
    p95_trustworthy  n_ok >=  200
    p99_trustworthy  n_ok >= 1000

And the headline gate for cell desaturation:

    trust.reliable = (err_rate <= 0.20) AND (ok_throughput >= 1 ok/sec)

A cell below that gate still reports its p50 (medians survive small n) but
the heatmap renders it dim — reader sees "this number is technically
correct but the server wasn't really serving the workload."

The 60s per-worker request timeout is applied uniformly across servers, so
timeout-driven errors are a legitimate performance signal — see
`benchmark/methodology.md` and the memory note on
"Worker timeouts = fair measurement".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from fhirbench.harness.metrics import percentile  # noqa: E402
from fhirbench.harness.report import parse_jsonl, workload_metrics  # noqa: E402

# Per-quantile n_ok thresholds. q needs ~10 samples above it for ±10%.
QUANTILE_N_THRESHOLDS = {
    "p50": 30,
    "p75": 40,
    "p90": 100,
    "p95": 200,
    "p99": 1000,
}

# Headline reliability gate.
MAX_ERR_RATE_RELIABLE = 0.20
MIN_OK_THROUGHPUT_RELIABLE = 1.0  # ok responses per second

# Which workloads use the ok-only percentile stream (matches parse_report).
# CRUD expects every op to succeed; search can fast-reject unsupported
# queries, so we paint search with ok-only latency to avoid rewarding
# fast 4xx "no" responses. Ingest also expects every transaction Bundle
# POST to succeed (200) — mismatched payloads count as errors, not as
# fast successes.
USE_OK_ONLY = {"crud": False, "search": True, "ingest": False}


def _trust(n_ok: int, err_rate: float, ops_ok_per_s: float) -> dict[str, Any]:
    t: dict[str, Any] = {
        "p50_trustworthy": n_ok >= QUANTILE_N_THRESHOLDS["p50"],
        "p75_trustworthy": n_ok >= QUANTILE_N_THRESHOLDS["p75"],
        "p90_trustworthy": n_ok >= QUANTILE_N_THRESHOLDS["p90"],
        "p95_trustworthy": n_ok >= QUANTILE_N_THRESHOLDS["p95"],
        "p99_trustworthy": n_ok >= QUANTILE_N_THRESHOLDS["p99"],
    }
    reliable = (
        err_rate <= MAX_ERR_RATE_RELIABLE
        and ops_ok_per_s >= MIN_OK_THROUGHPUT_RELIABLE
    )
    t["reliable"] = reliable
    if not reliable:
        reasons: list[str] = []
        if err_rate > MAX_ERR_RATE_RELIABLE:
            reasons.append(f"err_rate={err_rate * 100:.1f}%")
        if ops_ok_per_s < MIN_OK_THROUGHPUT_RELIABLE:
            reasons.append(f"{ops_ok_per_s:.2f} ok/s (n_ok={n_ok})")
        t["reason"] = "; ".join(reasons)
    return t


def _normalize_ingest_records(records: list[dict]) -> list[dict]:
    """Map loader.py's per-bundle rows into the canonical shape.

    Loader writes:
      {bundle, started_at, duration_ms, status_code, entries_sent,
       entries_2xx/4xx/5xx, error, phase}
    plus a leading sentinel like {"event":"prereq_start", count, started_at}.

    The percentile + grouping pipeline expects `ok` (bool) and `verb` (str).
    We synthesize `ok` from a 2xx status_code and tag verb="T" (transaction)
    so per-verb grouping yields a single "T" entry per ingest cell. Records
    without `duration_ms` (sentinels) are dropped.
    """
    out: list[dict] = []
    for r in records:
        if "duration_ms" not in r:
            continue
        sc = r.get("status_code") or 0
        out.append({**r, "ok": 200 <= sc < 300, "verb": r.get("verb", "T")})
    return out


def _workload_summary(records: list[dict], use_ok_only: bool,
                      workload_id: str = "") -> dict[str, Any] | None:
    """Build the summary dict for one workload's jsonl.

    Returns None if the jsonl was empty/missing.

    Per-verb grouping (added 2026-04-30): records are grouped by the
    composite key (verb, resource_type, complexity). When records lack
    resource_type/complexity (legacy pre-2026-04-30 NDJSON), the
    composite reduces to (verb, None, None) and the published per_verb
    items are identical to the verb-only output — strict backward
    compatibility. See plans/marat-from-health-samurai-wondrous-tome.md.

    workload_id="ingest" triggers the loader-shape adapter (records carry
    status_code instead of ok, no verb, with a leading event sentinel).
    """
    if workload_id == "ingest":
        records = _normalize_ingest_records(records)
    if not records:
        return None
    m = workload_metrics(records)
    if not m or not m.get("total"):
        return None
    suffix = "_ms_ok" if use_ok_only else "_ms"
    n_total = int(m["total"])
    n_ok = int(m["ok_count"])
    n_err = n_total - n_ok
    elapsed = float(m["elapsed_s"]) if m.get("elapsed_s") else 0.0
    ops_ok_per_s = (n_ok / elapsed) if elapsed > 0 else 0.0
    err_rate = float(m.get("error_rate", 0.0))

    # p75 isn't in workload_metrics yet — compute it directly off the same
    # records using the same ok-only-vs-all convention as the rest.
    lats = [
        r.get("duration_ms", 0)
        for r in records
        if (not use_ok_only) or r.get("ok")
    ]
    p75 = percentile(lats, 75) if lats else 0.0

    trust = _trust(n_ok, err_rate, ops_ok_per_s)

    # Group by composite key. defaultdict(list) lets us accumulate
    # without checking key existence; the key shape — a 3-tuple of
    # strings or None — is hashable so this is O(N) over records.
    from collections import defaultdict
    groups: dict[tuple[str, str | None, str | None], list[dict]] = defaultdict(list)
    for r in records:
        key = (
            r.get("verb", "?"),
            r.get("resource_type"),
            r.get("complexity"),
        )
        groups[key].append(r)

    per_verb: list[dict[str, Any]] = []
    for (verb, resource_type, complexity), group_recs in groups.items():
        v_n_total = len(group_recs)
        v_n_ok = sum(1 for r in group_recs if r.get("ok"))
        v_n_err = v_n_total - v_n_ok
        v_err_rate = (v_n_err / v_n_total) if v_n_total else 0.0
        v_ops_per_s = (v_n_total / elapsed) if elapsed > 0 else 0.0
        v_ops_ok_per_s = (v_n_ok / elapsed) if elapsed > 0 else 0.0
        v_lats = [
            r.get("duration_ms", 0)
            for r in group_recs
            if (not use_ok_only) or r.get("ok")
        ]
        item: dict[str, Any] = {
            "verb": verb,
            "p50_ms": round(percentile(v_lats, 50), 2),
            "p75_ms": round(percentile(v_lats, 75), 2),
            "p90_ms": round(percentile(v_lats, 90), 2),
            "p95_ms": round(percentile(v_lats, 95), 2),
            "p99_ms": round(percentile(v_lats, 99), 2),
            "ops_per_s":    round(v_ops_per_s, 2),
            "ops_ok_per_s": round(v_ops_ok_per_s, 2),
            "n":     v_n_total,
            "n_ok":  v_n_ok,
            "n_err": v_n_err,
            "error_rate": round(v_err_rate, 4),
            "trust": _trust(v_n_ok, v_err_rate, v_ops_ok_per_s),
        }
        # Only emit the new dimensions when present. Schema marks both
        # as optional so legacy consumers reading current artifacts
        # don't trip on the additions, and current consumers reading
        # legacy artifacts don't see ghost None fields.
        if resource_type is not None:
            item["resource_type"] = resource_type
        if complexity is not None:
            item["complexity"] = complexity
        per_verb.append(item)
    # Stable, deterministic sort by all three dimensions so successive
    # runs on identical data produce byte-identical cell_summary.json
    # files (eases diff review in PRs).
    per_verb.sort(key=lambda r: (
        r["verb"],
        r.get("resource_type") or "",
        r.get("complexity") or "",
    ))

    return {
        "n":      n_total,
        "n_ok":   n_ok,
        "n_err":  n_err,
        "error_rate":   round(err_rate, 4),
        "elapsed_s":    round(elapsed, 3),
        "ops_per_s":    round(m["ops_per_s"], 2),
        "ops_ok_per_s": round(ops_ok_per_s, 2),
        "p50_ms": round(m[f"p50{suffix}"], 2),
        "p75_ms": round(p75, 2),
        "p90_ms": round(m.get(f"p90{suffix}", 0.0), 2),
        "p95_ms": round(m[f"p95{suffix}"], 2),
        "p99_ms": round(m[f"p99{suffix}"], 2),
        "trust": trust,
        "per_verb": per_verb,
    }


def summarize_cell(cell_dir: pathlib.Path) -> dict[str, Any] | None:
    """Build the cell-level summary dict. Returns None if neither jsonl exists."""
    summaries: dict[str, dict[str, Any]] = {}
    for wl, fname in (("crud", "crud.jsonl"),
                      ("search", "search.jsonl"),
                      ("ingest", "ingest.jsonl")):
        path = cell_dir / fname
        if not path.is_file():
            continue
        recs = parse_jsonl(path)
        s = _workload_summary(recs, USE_OK_ONLY[wl], workload_id=wl)
        if s is not None:
            summaries[wl] = s
    if not summaries:
        return None

    sentinel = cell_dir / "cell_complete.json"
    sentinel_data: dict[str, Any] = {}
    if sentinel.is_file():
        try:
            sentinel_data = json.loads(sentinel.read_text())
        except Exception:
            pass

    return {
        "server":     sentinel_data.get("server") or cell_dir.name,
        "checkpoint": sentinel_data.get("checkpoint"),
        "completed_at": sentinel_data.get("completed_at"),
        "workloads":  summaries,
    }


def _walk_run(run_dir: pathlib.Path) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for ckpt_dir in sorted(run_dir.glob("checkpoint_*")):
        if not ckpt_dir.is_dir():
            continue
        for server_dir in sorted(ckpt_dir.iterdir()):
            if server_dir.is_dir():
                out.append(server_dir)
    return out


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--run-dir", type=pathlib.Path, required=True,
                   help="results/loadtest/<run-id> directory")
    p.add_argument("--only-complete", action="store_true",
                   help="Skip cells that lack cell_complete.json (in-progress cells)")
    args = p.parse_args()

    if not args.run_dir.is_dir():
        raise SystemExit(f"not a directory: {args.run_dir}")

    wrote = 0
    skipped = 0
    for cell_dir in _walk_run(args.run_dir):
        if args.only_complete and not (cell_dir / "cell_complete.json").is_file():
            skipped += 1
            continue
        summary = summarize_cell(cell_dir)
        if summary is None:
            skipped += 1
            continue
        out_path = cell_dir / "cell_summary.json"
        out_path.write_text(json.dumps(summary, indent=2) + "\n")
        wrote += 1
    print(f"[ok] wrote {wrote} cell_summary.json; skipped {skipped}", file=sys.stderr)


if __name__ == "__main__":
    main()
