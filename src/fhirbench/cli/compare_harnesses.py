#!/usr/bin/env python3
"""Shadow-run validator: diff a Python-harness round against a k6-harness round.

Drives step 5 of the k6-port plan:

    Run the Python harness and the k6 harness on the same Docker stack,
    same Synthea seed, same checkpoint ladder. Two round artifacts drop
    into results/rounds/<python-id>/benchmark.json and
    results/rounds/<k6-id>/benchmark.json. This script diffs them
    cell-by-cell. Green iff:
      - headline p50 within ±10% per cell
      - error rate within ±0.5 percentage points
      - ok-throughput within ±10%

    Any red cell blocks cutover. The intended workflow is:
      1. Run both harnesses.
      2. `python -m fhirbench.cli.compare_harnesses --python <id-a> --k6 <id-b>`.
      3. Eyeball the report. If any red, investigate before flipping the
         default in Makefile.

Usage:
  python -m fhirbench.cli.compare_harnesses \\
      --python results/rounds/2026-q2-r100/benchmark.json \\
      --k6     results/rounds/2026-q2-r101/benchmark.json
  # Non-zero exit if any cell is red.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Headline parity tolerances. Tuned loose enough that same-stack identical
# workloads on two different runners produce stable-comparable numbers;
# tight enough that a 20% regression would be caught.
P50_REL_TOL = 0.10          # ±10%
ERR_RATE_ABS_TOL = 0.005    # ±0.5 percentage points
OK_TPS_REL_TOL = 0.10       # ±10%


def _load_round(p: Path) -> dict:
    if not p.is_file():
        raise SystemExit(f"not found: {p}")
    return json.loads(p.read_text())


def _index_cells(r: dict) -> dict[tuple[str, str], dict]:
    """Return {(server_id, profile_id): cell} so both rounds align."""
    out: dict[tuple[str, str], dict] = {}
    for cell in r.get("cells") or []:
        sid = cell.get("server_id")
        pid = cell.get("profile_id")
        if sid and pid:
            out[(sid, pid)] = cell
    return out


def _headline_p50(cell: dict) -> float | None:
    """Pick the headline p50 from the last evidence row (max checkpoint)."""
    ev = cell.get("evidence") or []
    if not ev:
        return None
    last = ev[-1]
    return last.get("p50_ms")


def _err_rate(cell: dict) -> float | None:
    ev = cell.get("evidence") or []
    if not ev:
        return None
    return ev[-1].get("error_rate")


def _ok_tps(cell: dict) -> float | None:
    ev = cell.get("evidence") or []
    if not ev:
        return None
    last = ev[-1]
    return last.get("ops_ok_per_s") or last.get("ops_per_s")


def _within_rel(a: float, b: float, tol: float) -> bool:
    if a is None or b is None:
        return False
    # Handle near-zero: if both tiny, consider same.
    if max(abs(a), abs(b)) < 1e-6:
        return True
    return abs(a - b) / max(abs(a), abs(b)) <= tol


def _diff_cell(a: dict, b: dict) -> tuple[str, list[str]]:
    """Return (status, reasons). status in {'green', 'red', 'amber', 'grey'}."""
    reasons: list[str] = []
    ap50, bp50 = _headline_p50(a), _headline_p50(b)
    aerr, berr = _err_rate(a), _err_rate(b)
    atps, btps = _ok_tps(a), _ok_tps(b)

    if ap50 is None or bp50 is None:
        return "grey", ["missing headline p50 on one side"]

    if not _within_rel(ap50, bp50, P50_REL_TOL):
        reasons.append(f"p50 {ap50:.1f} vs {bp50:.1f} ms "
                       f"({((bp50 - ap50) / max(ap50, 1e-6)) * 100:+.1f}%)")
    if aerr is not None and berr is not None:
        if abs(aerr - berr) > ERR_RATE_ABS_TOL:
            reasons.append(f"err {aerr * 100:.2f}% vs {berr * 100:.2f}%")
    if atps is not None and btps is not None:
        if not _within_rel(atps, btps, OK_TPS_REL_TOL):
            reasons.append(f"ok-tps {atps:.2f} vs {btps:.2f}/s "
                           f"({((btps - atps) / max(atps, 1e-6)) * 100:+.1f}%)")

    if not reasons:
        return "green", []
    if len(reasons) == 1:
        return "amber", reasons
    return "red", reasons


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--python", type=Path, required=True,
                    help="Round artifact produced by the Python harness")
    ap.add_argument("--k6", type=Path, required=True,
                    help="Round artifact produced by the k6 harness")
    ap.add_argument("--fail-on", default="red",
                    choices=("red", "amber", "any"),
                    help="Non-zero exit if any cell hits this status or worse")
    args = ap.parse_args()

    py_round = _load_round(args.python)
    k6_round = _load_round(args.k6)

    py_cells = _index_cells(py_round)
    k6_cells = _index_cells(k6_round)
    all_keys = sorted(set(py_cells) | set(k6_cells))

    counts = {"green": 0, "amber": 0, "red": 0, "grey": 0}
    rows: list[tuple[str, str, str, list[str]]] = []
    for (sid, pid) in all_keys:
        a = py_cells.get((sid, pid))
        b = k6_cells.get((sid, pid))
        if a is None or b is None:
            status = "grey"
            reasons = ["missing on python" if a is None else "missing on k6"]
        else:
            status, reasons = _diff_cell(a, b)
        counts[status] += 1
        rows.append((sid, pid, status, reasons))

    # Report.
    print(f"Python round: {py_round.get('round_id')}  "
          f"({args.python})")
    print(f"k6     round: {k6_round.get('round_id')}  "
          f"({args.k6})")
    print()
    width = max(len(r[0]) for r in rows) if rows else 8
    for sid, pid, status, reasons in rows:
        tag = {
            "green": "✓",
            "amber": "~",
            "red":   "✗",
            "grey":  "?",
        }[status]
        print(f"  [{tag}] {sid.ljust(width)}  {pid:<10}  {status:<5}  "
              f"{'; '.join(reasons) if reasons else 'within tolerance'}")
    print()
    print(
        f"Totals: green={counts['green']}  amber={counts['amber']}  "
        f"red={counts['red']}  grey={counts['grey']}"
    )
    print()
    print(f"Tolerances: p50 ±{P50_REL_TOL * 100:.0f}%, "
          f"err ±{ERR_RATE_ABS_TOL * 100:.1f}pp, "
          f"ok-tps ±{OK_TPS_REL_TOL * 100:.0f}%")

    threshold = {
        "red":   counts["red"] > 0,
        "amber": counts["red"] + counts["amber"] > 0,
        "any":   counts["red"] + counts["amber"] + counts["grey"] > 0,
    }[args.fail_on]
    return 1 if threshold else 0


if __name__ == "__main__":
    sys.exit(main())
