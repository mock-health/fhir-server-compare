#!/usr/bin/env python3
"""Aggregate a finished run's JSONL/CSV artifacts into a publishable report.

Walks results/loadtest/<run-id>/stage{1,2,3}/<server>/ and produces:
  - summary.md (the headline matrix)
  - per-server.md (deep-dive tables with p50/p95/p99 per verb/query)
  - resources.png (per-server CPU + memory timeseries during stage 2 ingest),
    if matplotlib is available; skipped with a note otherwise.

Runs with only the stdlib + whatever's in requirements.txt. matplotlib is
optional — the report is still useful without charts.

Usage:
    python -m loadtest.report --run-id dryrun-10p
    python -m loadtest.report --run-id full-100k --results-root results/loadtest
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loadtest.metrics import percentile  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results" / "loadtest"
SERVERS_ORDER = ["hapi", "aidbox", "medplum", "msfhir", "blaze", "spark", "hfs"]
SERVER_LABELS = {
    "hapi": "HAPI", "aidbox": "Aidbox", "medplum": "Medplum",
    "msfhir": "MS FHIR", "blaze": "Blaze", "spark": "Spark", "hfs": "HFS",
}


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def _load_phase_summary(sdir: Path) -> list[dict] | None:
    """Read crud_phases.json emitted by phased CRUD runs.

    Returns None for legacy runs (mixed-workload CRUD) so the report falls
    back to per-verb percentiles without the phase accounting column.
    """
    p = sdir / "crud_phases.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def parse_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def ingest_metrics(records: list[dict]) -> dict:
    """Compute ingest throughput + error rate from loader.jsonl records.

    Prereq bundles (hospitalInformation/practitionerInformation) carry
    phase=prereq and are excluded from the timed throughput number. They
    count toward correctness but not performance.
    """
    bundles = [r for r in records if "bundle" in r and r.get("phase") != "prereq"]
    run_start = next((r for r in records if r.get("event") == "run_start"), {})
    run_end = next((r for r in records if r.get("event") == "run_end"), {})
    if not bundles:
        return {"bundles": 0}

    total = len(bundles)
    errors = sum(1 for b in bundles if not (200 <= b.get("status_code", 0) < 300))
    resources_ok = sum(b.get("entries_2xx", 0) for b in bundles)
    latencies = [b.get("duration_ms", 0) for b in bundles if b.get("duration_ms")]

    elapsed_s = run_end.get("elapsed_s") or 0
    if not elapsed_s and bundles:
        # derive from min(started_at) + max(started_at + duration)
        starts = [b.get("started_at", 0) for b in bundles]
        ends = [b.get("started_at", 0) + b.get("duration_ms", 0) / 1000 for b in bundles]
        if starts and ends:
            elapsed_s = max(ends) - min(starts)
    bundles_per_s = total / elapsed_s if elapsed_s > 0 else 0
    resources_per_s = resources_ok / elapsed_s if elapsed_s > 0 else 0

    return {
        "bundles": total,
        "errors": errors,
        "error_rate": errors / total if total else 0,
        "resources_accepted": resources_ok,
        "elapsed_s": elapsed_s,
        "bundles_per_s": bundles_per_s,
        "resources_per_s": resources_per_s,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p90_ms": percentile(latencies, 90),
        "latency_p95_ms": percentile(latencies, 95),
        "latency_p99_ms": percentile(latencies, 99),
    }


def workload_metrics(records: list[dict]) -> dict:
    """Group op records by verb, compute ops/s + p50/p95/p99 each.

    Latency split: every metric is computed twice — once across ALL records
    (`p99_ms`) and once across only the 2xx records (`p99_ms_ok`). This
    matters most for search workloads, where some queries are not supported
    by some servers and return 4xx/404 fast. Reporting the ok-only number as
    the headline stops fast-rejecting vendors from looking artificially
    "fast" on the search workload.

    Active-time elapsed: when a log contains multiple runs concatenated (an
    earlier OpLog bug used append mode), idle gaps between runs inflate
    `elapsed_s` and drop the apparent ops/s. We subtract any gap between
    consecutive op timestamps that exceeds 10 seconds so the number reflects
    actual concurrent-work time.
    """
    if not records:
        return {"total": 0}
    by_verb: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_verb[r.get("verb", "?")].append(r)

    # Pair (started_at, duration_ms) so `end` reflects the actual end of the
    # last-finished op. Original form used records[0]'s duration, which made
    # elapsed depend on whichever record happened to be first in the file.
    pairs = sorted((r.get("started_at", 0), r.get("duration_ms", 0)) for r in records)
    times = [p[0] for p in pairs]
    start = times[0]
    end = max(s + d / 1000.0 for s, d in pairs)
    # Subtract idle gaps > 10s (signature of concatenated runs)
    idle = sum(
        (times[i + 1] - times[i]) - 10.0
        for i in range(len(times) - 1)
        if (times[i + 1] - times[i]) > 10.0
    )
    elapsed = max(1e-6, (end - start) - idle)
    total = len(records)
    errors = sum(1 for r in records if not r.get("ok"))

    def _stats(rs: list[dict]) -> dict:
        ok_recs = [r for r in rs if r.get("ok")]
        all_lats = [r.get("duration_ms", 0) for r in rs]
        ok_lats  = [r.get("duration_ms", 0) for r in ok_recs]
        # Common short-codes encountered: 0=network/timeout, 4xx=client reject,
        # 5xx=server error. Top-3 lets the report flag *why* a query erred
        # without dumping every status_code into the table.
        code_hist: dict[int, int] = defaultdict(int)
        for r in rs:
            if not r.get("ok"):
                code_hist[int(r.get("status_code", 0))] += 1
        top_err_codes = sorted(code_hist.items(), key=lambda kv: -kv[1])[:3]
        # Reliability buckets from binomial-rank CI math at 95% confidence:
        # p99 needs n>=4,000 for ±5%, n>=1,000 for ±10%. Cells below 1K
        # are flagged so the report can show them but mark them unstable.
        n = len(rs)
        if n >= 4_000:
            reliability = "reliable"
        elif n >= 1_000:
            reliability = "noisy"
        else:
            reliability = "unreliable"
        return {
            "count": n,
            "ok_count": len(ok_recs),
            "errors": n - len(ok_recs),
            "error_rate": (n - len(ok_recs)) / n if rs else 0.0,
            "ops_per_s": n / elapsed,
            "p50_ms": percentile(all_lats, 50),
            "p90_ms": percentile(all_lats, 90),
            "p95_ms": percentile(all_lats, 95),
            "p99_ms": percentile(all_lats, 99),
            "p50_ms_ok": percentile(ok_lats, 50),
            "p90_ms_ok": percentile(ok_lats, 90),
            "p95_ms_ok": percentile(ok_lats, 95),
            "p99_ms_ok": percentile(ok_lats, 99),
            "top_err_codes": top_err_codes,
            "reliability": reliability,
        }

    per_verb: dict[str, dict] = {v: _stats(rs) for v, rs in by_verb.items()}
    overall = _stats(records)

    return {
        "total": total,
        "errors": errors,
        "ok_count": overall["ok_count"],
        "error_rate": errors / total if total else 0,
        "elapsed_s": elapsed,
        "ops_per_s": total / elapsed,
        "p50_ms": overall["p50_ms"],
        "p90_ms": overall["p90_ms"],
        "p95_ms": overall["p95_ms"],
        "p99_ms": overall["p99_ms"],
        "p50_ms_ok": overall["p50_ms_ok"],
        "p90_ms_ok": overall["p90_ms_ok"],
        "p95_ms_ok": overall["p95_ms_ok"],
        "p99_ms_ok": overall["p99_ms_ok"],
        "reliability": overall["reliability"],
        "per_verb": per_verb,
    }


def resource_peak(csv_path: Path) -> dict:
    """Peak CPU% and peak RSS bytes across all containers in a resource CSV."""
    if not csv_path.exists():
        return {}
    peak_cpu = 0.0
    peak_mem = 0
    samples = 0
    per_container: dict[str, dict] = defaultdict(lambda: {"peak_cpu": 0.0, "peak_mem": 0})
    with csv_path.open() as f:
        for row in csv.DictReader(f):
            samples += 1
            c = row.get("container", "")
            cpu = float(row.get("cpu_pct", 0) or 0)
            mem = float(row.get("mem_bytes", 0) or 0)
            peak_cpu = max(peak_cpu, cpu)
            peak_mem = max(peak_mem, int(mem))
            per_container[c]["peak_cpu"] = max(per_container[c]["peak_cpu"], cpu)
            per_container[c]["peak_mem"] = max(per_container[c]["peak_mem"], int(mem))
    return {
        "samples": samples,
        "peak_cpu_pct": peak_cpu,
        "peak_mem_bytes": peak_mem,
        "peak_mem_gib": peak_mem / 1024**3,
        "per_container": dict(per_container),
    }


def disk_used(disk_json_path: Path, server_id: str) -> float:
    """Sum bytes of the Docker volumes owned by this server. Returns GB."""
    if not disk_json_path.exists():
        return 0.0
    try:
        data = json.loads(disk_json_path.read_text())
    except Exception:
        # `docker system df -v --format json` isn't parseable on older docker; skip
        return 0.0
    # Newer docker: {"Volumes":[{"Name":"...","Size":"1.2GB",...}], ...}
    vols = data.get("Volumes") or []
    # Fallback: data may be a list of objects
    if isinstance(data, list):
        vols = [d for d in data if d.get("Type") == "Volume"]
    prefix = f"fhir-server-compare_{server_id}"
    total = 0.0
    for v in vols:
        name = v.get("Name", "")
        if not name.startswith(prefix) and server_id not in name:
            continue
        size_str = str(v.get("Size", "0B"))
        try:
            from loadtest.resources import parse_size
            total += parse_size(size_str)
        except Exception:
            pass
    return total / 1024**3


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def fmt_num(v, digits=1) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        if v == 0:
            return "0"
        if isinstance(v, int) or v.is_integer():
            return f"{int(v):,}"
        return f"{v:,.{digits}f}"
    return str(v)


def render_headline(run_dir: Path) -> str:
    """One-row-per-server matrix with the headline numbers."""
    lines: list[str] = []
    lines.append("# Load Test Results\n")
    lines.append(f"Run: `{run_dir.name}`\n\n")

    header = [
        "Server",
        "S1 bundles/s", "S1 resources/s",
        "S2 bundles/s", "S2 resources/s",
        "S3 bundles/s", "S3 resources/s",
        "Δ(S3/S1)",
        "S3 CRUD p99 ms", "S3 Search ok-p99 ms", "S3 Search err%",
        "Peak CPU%", "Peak RSS GiB",
    ]
    lines.append("## Headline matrix\n")
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    for sid in SERVERS_ORDER:
        row = [SERVER_LABELS[sid]]
        s_m: dict[int, dict] = {}
        crud_p99 = None
        search_p99_ok = None
        search_err_pct = None
        peak_cpu = 0.0
        peak_mem = 0.0
        # Headline takes its workload numbers from the LATEST stage that has
        # any workload data. For the legacy 3-stage path that's stage 3; for
        # `loadtest-1k` (stage1-only) it's stage 1.
        for st in (1, 2, 3):
            sdir = run_dir / f"stage{st}" / sid
            if not sdir.exists():
                s_m[st] = {}
                continue
            s_m[st] = ingest_metrics(parse_jsonl(sdir / "ingest.jsonl"))
            r = resource_peak(sdir / "ingest.resources.csv")
            peak_cpu = max(peak_cpu, r.get("peak_cpu_pct", 0.0))
            peak_mem = max(peak_mem, r.get("peak_mem_gib", 0.0))
            c = workload_metrics([
                rec for rec in parse_jsonl(sdir / "crud.jsonl") if "verb" in rec
            ])
            if c.get("total"):
                crud_p99 = c["p99_ms"]
            s = workload_metrics([
                rec for rec in parse_jsonl(sdir / "search.jsonl") if "verb" in rec
            ])
            if s.get("total"):
                # Use ok-only p99 for the headline so a server that fast-rejects
                # half the workload doesn't get a flattering p99.
                search_p99_ok = s.get("p99_ms_ok") or s.get("p99_ms")
                search_err_pct = s.get("error_rate", 0.0) * 100.0

        # Degradation: stage3 bundles_per_s vs stage1 bundles_per_s
        s1r = s_m[1].get("bundles_per_s") or 0
        s3r = s_m[3].get("bundles_per_s") or 0
        delta = (s3r / s1r) if s1r > 0 else None

        row += [
            fmt_num(s_m[1].get("bundles_per_s")),
            fmt_num(s_m[1].get("resources_per_s")),
            fmt_num(s_m[2].get("bundles_per_s")),
            fmt_num(s_m[2].get("resources_per_s")),
            fmt_num(s_m[3].get("bundles_per_s")),
            fmt_num(s_m[3].get("resources_per_s")),
            fmt_num(delta, digits=2) if delta is not None else "—",
            fmt_num(crud_p99),
            fmt_num(search_p99_ok),
            f"{search_err_pct:.1f}%" if search_err_pct is not None else "—",
            fmt_num(peak_cpu),
            fmt_num(peak_mem, digits=1),
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.append("\n*S1 = Stage 1 (empty → 1K). S2 = Stage 2 (ingest to target). S3 = Stage 3 (+N on top of target).*\n")
    lines.append("*Δ(S3/S1) = ingest throughput retention after scale. 1.00 = no degradation.*\n")
    lines.append("*Search ok-p99 excludes failed queries — fast 4xx rejections of unsupported operations don't get to look like \"fast search.\" Pair with Search err% to see why a server's number is what it is.*\n")
    return "\n".join(lines)


def render_per_server(run_dir: Path) -> str:
    """Deep-dive sections per server, per stage, per workload."""
    lines: list[str] = ["\n\n---\n\n## Per-server deep dive\n"]
    for sid in SERVERS_ORDER:
        if not any((run_dir / f"stage{st}" / sid).exists() for st in (1, 2, 3)):
            continue
        lines.append(f"\n### {SERVER_LABELS[sid]}\n")
        for st in (1, 2, 3):
            sdir = run_dir / f"stage{st}" / sid
            if not sdir.exists():
                continue
            lines.append(f"\n**Stage {st}**\n")
            im = ingest_metrics(parse_jsonl(sdir / "ingest.jsonl"))
            if im.get("bundles"):
                lines.append(
                    f"- Ingest: {fmt_num(im['bundles'])} bundles in {fmt_num(im['elapsed_s'])}s "
                    f"→ {fmt_num(im['bundles_per_s'])} bundles/s, "
                    f"{fmt_num(im['resources_per_s'])} resources/s accepted, "
                    f"{fmt_num(im['errors'])} errors "
                    f"(latency p50/p90/p99 ms: {fmt_num(im['latency_p50_ms'])} / "
                    f"{fmt_num(im.get('latency_p90_ms'))} / {fmt_num(im['latency_p99_ms'])})"
                )

            crud = workload_metrics([r for r in parse_jsonl(sdir / "crud.jsonl") if "verb" in r])
            crud_phases = _load_phase_summary(sdir)
            if crud.get("total"):
                lines.append(
                    f"- CRUD: {fmt_num(crud['total'])} ops @ {fmt_num(crud['ops_per_s'])}/s, "
                    f"p50/p90/p99 = {fmt_num(crud['p50_ms'])}/{fmt_num(crud.get('p90_ms'))}/{fmt_num(crud['p99_ms'])} ms, "
                    f"{fmt_num(crud['errors'])} errors"
                )
                if crud_phases:
                    lines.append(
                        "  - Phased mode: " + ", ".join(
                            f"{ph['verb']}={ph['samples']}/{ph['planned_cap']} "
                            f"({ph['elapsed_ms']/1000:.0f}s, {ph['stop_reason']})"
                            for ph in crud_phases
                        )
                    )
                if "per_verb" in crud:
                    lines.append("")
                    lines.append(
                        "  | Verb | Count | ops/s | p50 ms | p90 ms | p99 ms | reliability | errors |"
                    )
                    lines.append("  |---|---|---|---|---|---|---|---|")
                    for v in ("R", "V", "C", "U", "D"):
                        pv = crud["per_verb"].get(v)
                        if pv:
                            lines.append(
                                f"  | {v} | {fmt_num(pv['count'])} | {fmt_num(pv['ops_per_s'])} | "
                                f"{fmt_num(pv['p50_ms'])} | {fmt_num(pv.get('p90_ms'))} | "
                                f"{fmt_num(pv['p99_ms'])} | "
                                f"{pv.get('reliability', '?')} | "
                                f"{fmt_num(pv['errors'])} |"
                            )

            search = workload_metrics([r for r in parse_jsonl(sdir / "search.jsonl") if "verb" in r])
            if search.get("total"):
                lines.append(
                    f"\n- Search: {fmt_num(search['total'])} queries @ {fmt_num(search['ops_per_s'])}/s, "
                    f"error_rate {search['error_rate']*100:.1f}% "
                    f"(ok-only p50/p90/p99 = "
                    f"{fmt_num(search['p50_ms_ok'])}/{fmt_num(search.get('p90_ms_ok'))}/{fmt_num(search['p99_ms_ok'])} ms)"
                )
                if "per_verb" in search and search["per_verb"]:
                    lines.append("")
                    lines.append(
                        "  | Query | Count | err% | ops/s | ok p50 ms | ok p90 ms | ok p99 ms | reliability | err codes |"
                    )
                    lines.append("  |---|---|---|---|---|---|---|---|---|")
                    # Sort by descending error rate so the publication-worthy
                    # rows (servers that reject specific queries) jump out.
                    sorted_verbs = sorted(
                        search["per_verb"].items(),
                        key=lambda kv: (-kv[1].get("error_rate", 0.0), kv[0]),
                    )
                    for q, pv in sorted_verbs:
                        codes = ", ".join(f"{c}×{n}" for c, n in pv.get("top_err_codes") or []) or "—"
                        lines.append(
                            f"  | {q} | {fmt_num(pv['count'])} | "
                            f"{pv['error_rate']*100:.1f}% | "
                            f"{fmt_num(pv['ops_per_s'])} | "
                            f"{fmt_num(pv['p50_ms_ok'])} | {fmt_num(pv.get('p90_ms_ok'))} | "
                            f"{fmt_num(pv['p99_ms_ok'])} | "
                            f"{pv.get('reliability', '?')} | "
                            f"{codes} |"
                        )

            res = resource_peak(sdir / "ingest.resources.csv")
            if res.get("samples"):
                lines.append(
                    f"\n- Peak during ingest: CPU {fmt_num(res['peak_cpu_pct'])}%, "
                    f"RSS {fmt_num(res['peak_mem_gib'])} GiB"
                )
    return "\n".join(lines)


def maybe_render_charts(run_dir: Path) -> str:
    """Charts are optional. Skip cleanly if matplotlib isn't installed."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return "\n\n*(matplotlib not installed — skipping timeseries charts)*\n"
    note_lines = ["\n\n## Resource timeseries (Stage 2 ingest)\n"]
    chart_dir = run_dir / "charts"
    chart_dir.mkdir(exist_ok=True)
    any_chart = False
    for sid in SERVERS_ORDER:
        csv_path = run_dir / "stage2" / sid / "ingest.resources.csv"
        if not csv_path.exists():
            continue
        rows_by_container: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
        with csv_path.open() as f:
            for row in csv.DictReader(f):
                t = float(row.get("ts", 0) or 0)
                cpu = float(row.get("cpu_pct", 0) or 0)
                mem_gib = float(row.get("mem_bytes", 0) or 0) / 1024**3
                rows_by_container[row.get("container", "")].append((t, cpu, mem_gib))
        if not rows_by_container:
            continue
        fig, (ax_cpu, ax_mem) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        t0 = min(v[0][0] for v in rows_by_container.values() if v)
        for c, rows in rows_by_container.items():
            xs = [r[0] - t0 for r in rows]
            ax_cpu.plot(xs, [r[1] for r in rows], label=c, linewidth=0.9)
            ax_mem.plot(xs, [r[2] for r in rows], label=c, linewidth=0.9)
        ax_cpu.set_ylabel("CPU %")
        ax_cpu.set_title(f"{SERVER_LABELS[sid]} — Stage 2 ingest")
        ax_cpu.legend(loc="upper right", fontsize=8)
        ax_mem.set_ylabel("RSS GiB")
        ax_mem.set_xlabel("seconds")
        ax_mem.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        chart_path = chart_dir / f"{sid}.stage2.ingest.png"
        fig.savefig(chart_path, dpi=120)
        plt.close(fig)
        note_lines.append(f"![{SERVER_LABELS[sid]} stage 2 ingest](charts/{sid}.stage2.ingest.png)\n")
        any_chart = True
    if not any_chart:
        return "\n\n*(no stage2 data yet — charts skipped)*\n"
    return "\n".join(note_lines)


def render_ramp(run_dir: Path) -> str:
    """Render a ramp-mode report.

    Expects layout: <run_dir>/checkpoint_NNNNNNNN/<server>/{ingest,crud,search}.jsonl,
    i.e. checkpoint-first, server-second (cold-start per (ckpt, server) pair).

    Output: one wide table per metric (rows = checkpoints, cols = servers),
    plus a per-server deep-dive block.
    """
    checkpoint_dirs = sorted(run_dir.glob("checkpoint_*"))
    if not checkpoint_dirs:
        return ""
    checkpoints = [int(d.name.split("_")[1]) for d in checkpoint_dirs]
    servers_present = []
    for sid in SERVERS_ORDER:
        if any((run_dir / d.name / sid).exists() for d in checkpoint_dirs):
            servers_present.append(sid)

    def collect(server_id: str, ckpt: int) -> dict:
        sdir = run_dir / f"checkpoint_{ckpt:08d}" / server_id
        out: dict = {"ckpt": ckpt, "server": server_id}
        if not sdir.exists():
            return out
        out["ingest"] = ingest_metrics(parse_jsonl(sdir / "ingest.jsonl"))
        out["crud"]   = workload_metrics([r for r in parse_jsonl(sdir / "crud.jsonl") if "verb" in r])
        out["search"] = workload_metrics([r for r in parse_jsonl(sdir / "search.jsonl") if "verb" in r])
        out["peak"]   = resource_peak(sdir / "resources.csv")
        return out

    grid = {(sid, ck): collect(sid, ck) for sid in servers_present for ck in checkpoints}

    lines: list[str] = ["\n\n---\n\n## Ramp results (cold-start per checkpoint)\n"]
    lines.append("Each (checkpoint, server) cell is an independent cold-DB ingest + workload run.\n")

    def wide_table(title: str, extract) -> list[str]:
        head = ["N patients"] + [SERVER_LABELS[s] for s in servers_present]
        out = [f"\n### {title}\n", "| " + " | ".join(head) + " |",
               "|" + "|".join(["---:"] * len(head)) + "|"]
        for ck in checkpoints:
            row = [f"{ck:,}"]
            for sid in servers_present:
                cell = grid.get((sid, ck)) or {}
                row.append(extract(cell))
            out.append("| " + " | ".join(row) + " |")
        return out

    lines += wide_table(
        "Ingest throughput (resources/s accepted)",
        lambda c: fmt_num((c.get("ingest") or {}).get("resources_per_s")),
    )
    lines += wide_table(
        "Ingest p99 latency per bundle (ms)",
        lambda c: fmt_num((c.get("ingest") or {}).get("latency_p99_ms")),
    )
    lines += wide_table(
        "CRUD ops/s (all verbs blended)",
        lambda c: fmt_num((c.get("crud") or {}).get("ops_per_s")),
    )
    lines += wide_table(
        "CRUD p99 latency (ms)",
        lambda c: fmt_num((c.get("crud") or {}).get("p99_ms")),
    )
    lines += wide_table(
        "Search qps",
        lambda c: fmt_num((c.get("search") or {}).get("ops_per_s")),
    )
    lines += wide_table(
        "Search ok-only p99 latency (ms)",
        lambda c: fmt_num((c.get("search") or {}).get("p99_ms_ok")),
    )
    lines += wide_table(
        "Search error rate (%)",
        lambda c: f"{(c.get('search') or {}).get('error_rate', 0)*100:.1f}",
    )
    lines += wide_table(
        "Peak CPU% during cycle",
        lambda c: fmt_num((c.get("peak") or {}).get("peak_cpu_pct")),
    )
    lines += wide_table(
        "Peak RSS (GiB) during cycle",
        lambda c: fmt_num((c.get("peak") or {}).get("peak_mem_gib"), digits=1),
    )

    # Per-query deep-dive: one section per server, one row per (checkpoint, query)
    # showing whether the query worked at all and how fast on the success path.
    # This is the row that lets a reader see "Aidbox doesn't support $expand"
    # vs "Aidbox is genuinely slow on $expand" — which the aggregate hides.
    lines.append("\n\n### Per-query search breakdown (ok-only latency)\n")
    lines.append("Each cell shows `err% / ok p99 ms`. err% > 50 means the query is effectively unsupported on that server.\n")
    # Discover the union of query names across all (server, checkpoint).
    all_queries: set[str] = set()
    for cell in grid.values():
        s = cell.get("search") or {}
        for q in (s.get("per_verb") or {}):
            all_queries.add(q)
    if all_queries:
        for sid in servers_present:
            lines.append(f"\n#### {SERVER_LABELS[sid]}\n")
            head = ["Query \\ N patients"] + [f"{ck:,}" for ck in checkpoints]
            lines.append("| " + " | ".join(head) + " |")
            lines.append("|" + "|".join(["---:"] * len(head)) + "|")
            for q in sorted(all_queries):
                row = [q]
                for ck in checkpoints:
                    cell = grid.get((sid, ck)) or {}
                    pv = ((cell.get("search") or {}).get("per_verb") or {}).get(q)
                    if not pv:
                        row.append("—")
                    else:
                        err_pct = pv.get("error_rate", 0.0) * 100.0
                        p99 = pv.get("p99_ms_ok") or 0.0
                        row.append(f"{err_pct:.0f}% / {fmt_num(p99)}")
                lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def maybe_render_ramp_charts(run_dir: Path) -> str:
    """One chart per metric, x=checkpoint size (log scale), y=value. Log-log
    naturally on a 2^N ramp."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    checkpoint_dirs = sorted(run_dir.glob("checkpoint_*"))
    if not checkpoint_dirs:
        return ""
    checkpoints = [int(d.name.split("_")[1]) for d in checkpoint_dirs]
    chart_dir = run_dir / "charts"
    chart_dir.mkdir(exist_ok=True)

    def series_for(metric_fn):
        """Return {server_id: (xs, ys)} using metric_fn(server_dir) -> float|None."""
        out: dict[str, tuple[list[int], list[float]]] = {}
        for sid in SERVERS_ORDER:
            xs, ys = [], []
            for cp in checkpoints:
                sdir = run_dir / f"checkpoint_{cp:08d}" / sid
                if not sdir.exists():
                    continue
                v = metric_fn(sdir)
                if v is None:
                    continue
                xs.append(cp)
                ys.append(v)
            if xs:
                out[sid] = (xs, ys)
        return out

    def write_chart(title: str, ylabel: str, series, filename: str, logy: bool = False) -> str:
        if not series:
            return ""
        fig, ax = plt.subplots(figsize=(10, 6))
        for sid, (xs, ys) in series.items():
            ax.plot(xs, ys, marker="o", markersize=4, linewidth=1.4, label=SERVER_LABELS[sid])
        ax.set_xscale("log")
        if logy:
            ax.set_yscale("log")
        ax.set_xlabel("Patients ingested (log)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3, which="both")
        ax.legend()
        out = chart_dir / filename
        fig.tight_layout()
        fig.savefig(out, dpi=130)
        plt.close(fig)
        return f"![{title}](charts/{filename})\n"

    out_lines = ["\n\n## Charts\n"]
    out_lines.append(write_chart(
        "Ingest throughput (resources/s)",
        "Resources / second (accepted)",
        series_for(lambda d: (ingest_metrics(parse_jsonl(d / "ingest.jsonl")) or {}).get("resources_per_s")),
        "ingest_throughput.png",
        logy=True,
    ))
    out_lines.append(write_chart(
        "Ingest p99 latency per bundle",
        "ms",
        series_for(lambda d: (ingest_metrics(parse_jsonl(d / "ingest.jsonl")) or {}).get("latency_p99_ms")),
        "ingest_p99.png",
        logy=True,
    ))
    out_lines.append(write_chart(
        "CRUD ops/s",
        "ops / second",
        series_for(lambda d: (workload_metrics([r for r in parse_jsonl(d / "crud.jsonl") if "verb" in r]) or {}).get("ops_per_s")),
        "crud_ops.png",
    ))
    out_lines.append(write_chart(
        "CRUD p99 latency",
        "ms",
        series_for(lambda d: (workload_metrics([r for r in parse_jsonl(d / "crud.jsonl") if "verb" in r]) or {}).get("p99_ms")),
        "crud_p99.png",
        logy=True,
    ))
    out_lines.append(write_chart(
        "Search p99 latency",
        "ms",
        series_for(lambda d: (workload_metrics([r for r in parse_jsonl(d / "search.jsonl") if "verb" in r]) or {}).get("p99_ms")),
        "search_p99.png",
        logy=True,
    ))
    return "\n".join(l for l in out_lines if l)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    args = ap.parse_args()
    run_dir = args.results_root / args.run_id
    if not run_dir.exists():
        print(f"ERROR: {run_dir} does not exist.", file=sys.stderr)
        return 2
    out = (
        render_headline(run_dir)
        + render_per_server(run_dir)
        + render_ramp(run_dir)
        + maybe_render_ramp_charts(run_dir)
        + maybe_render_charts(run_dir)
    )
    summary_path = run_dir / "summary.md"
    summary_path.write_text(out)
    print(f"Wrote {summary_path}")
    print()
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
