"""Per-cell summary for a single (server, checkpoint) ramp output.

Reads crud.jsonl / search.jsonl from a cell directory and emits a compact
`cell_summary.json` next to `cell_complete.json`. Same percentile math as
`parse_report.py` (both go through `loadtest.report.workload_metrics`) so
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

    trust.reliable = (err_rate <= 0.05) AND (ok_throughput >= 1 ok/sec)

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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from loadtest.metrics import percentile  # noqa: E402
from loadtest.report import parse_jsonl, workload_metrics  # noqa: E402

# Per-quantile n_ok thresholds. q needs ~10 samples above it for ±10%.
QUANTILE_N_THRESHOLDS = {
    "p50": 30,
    "p75": 40,
    "p90": 100,
    "p95": 200,
    "p99": 1000,
}

# Headline reliability gate.
MAX_ERR_RATE_RELIABLE = 0.05
MIN_OK_THROUGHPUT_RELIABLE = 1.0  # ok responses per second

# Which workloads use the ok-only percentile stream (matches parse_report).
# CRUD expects every op to succeed; search can fast-reject unsupported
# queries, so we paint search with ok-only latency to avoid rewarding
# fast 4xx "no" responses.
USE_OK_ONLY = {"crud": False, "search": True}


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


def _workload_summary(records: list[dict], use_ok_only: bool) -> dict[str, Any] | None:
    """Build the summary dict for one workload's jsonl.

    Returns None if the jsonl was empty/missing.
    """
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

    per_verb: list[dict[str, Any]] = []
    for verb, vm in (m.get("per_verb") or {}).items():
        v_n_total = int(vm["count"])
        v_n_ok = int(vm["ok_count"])
        v_n_err = v_n_total - v_n_ok
        v_err_rate = float(vm.get("error_rate", 0.0))
        v_ops_ok_per_s = (v_n_ok / elapsed) if elapsed > 0 else 0.0
        v_lats = [
            r.get("duration_ms", 0)
            for r in records
            if r.get("verb") == verb and ((not use_ok_only) or r.get("ok"))
        ]
        v_p75 = percentile(v_lats, 75) if v_lats else 0.0
        per_verb.append({
            "verb": verb,
            "p50_ms": round(vm[f"p50{suffix}"], 2),
            "p75_ms": round(v_p75, 2),
            "p90_ms": round(vm.get(f"p90{suffix}", 0.0), 2),
            "p95_ms": round(vm[f"p95{suffix}"], 2),
            "p99_ms": round(vm[f"p99{suffix}"], 2),
            "ops_per_s":    round(vm["ops_per_s"], 2),
            "ops_ok_per_s": round(v_ops_ok_per_s, 2),
            "n":     v_n_total,
            "n_ok":  v_n_ok,
            "n_err": v_n_err,
            "error_rate": round(v_err_rate, 4),
            "trust": _trust(v_n_ok, v_err_rate, v_ops_ok_per_s),
        })
    per_verb.sort(key=lambda r: r["verb"])

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
    for wl, fname in (("crud", "crud.jsonl"), ("search", "search.jsonl")):
        path = cell_dir / fname
        if not path.is_file():
            continue
        recs = parse_jsonl(path)
        s = _workload_summary(recs, USE_OK_ONLY[wl])
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
