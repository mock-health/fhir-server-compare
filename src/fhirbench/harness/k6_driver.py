"""Shim that lets fhirbench.harness.ramp drive the k6 workload harness.

The Python ramp owns the per-server lifecycle (boot → wait healthy → bootstrap
→ ingest → workloads → cell_complete). For each cell's workload phase we emit
the k6 context file, run grafana/k6 inside docker for CRUD and Search
back-to-back, and convert the raw NDJSON into the crud.jsonl / search.jsonl
shape the rest of the pipeline reads.

By design this module has zero knowledge of ramp / cell layout — it takes a
server id, a cell directory, a workload duration, and produces two JSONL
files alongside whatever else the ramp writes in that dir.

Why docker-run grafana/k6 instead of a vendored k6 binary: the k6 runtime
is pinned to an upstream published image (the same thing the Aidbox team
uses for their benchmark), which means no "what version of k6 did you
build against" ambiguity for anyone reproducing a round. The container's
host network is required so VUs can reach docker-compose service ports
that are exposed on localhost.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
K6_IMAGE = os.environ.get("K6_IMAGE", "grafana/k6")
K6_CTX_PATH = REPO_ROOT / "src" / "fhirbench" / "k6" / "k6_context.json"


def _emit_context(server_id: str, workload: str) -> None:
    """Call fhirbench.cli.emit_k6_context as a subprocess.

    The context emitter resolves servers.yaml + queries.yaml + env-var
    interpolation once. Running it as a subprocess keeps this module free
    of its transitive imports (httpx, yaml) at load time; the ramp already
    invokes subprocesses for docker so the cost isn't material.
    """
    cmd = [
        sys.executable, "-m", "fhirbench.cli.emit_k6_context",
        "--server", server_id,
        "--workload", workload,
        "--out", str(K6_CTX_PATH),
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def _run_k6(server_id: str, script: str, duration_s: float,
            out_ndjson: Path) -> bool:
    """Run one k6 script via docker. Returns True on success, False on
    in-script failure. Raises on docker-level failure.

    Two failure classes are distinguished:
      - exit 99 / 107  → k6 ran but the JS threw (e.g. setup() couldn't
        harvest patient ids because the server wedged under the prior
        workload's load). This is a SERVER-side failure for one cell, not
        a harness bug. Return False so the caller can mark the cell
        unreliable and continue with the next server.
      - exit 125+ / FileNotFoundError → docker itself couldn't start the
        container (image missing, daemon down, bind-mount denied). This
        IS a harness bug; raise so the operator sees it immediately.

    The 107 case in particular came up at the spark/16K cell of ramp-50k
    on 2026-04-27: search workload finished with 67% client timeouts (Spark
    wedged under 64 VUs at 16K patients), and the next workload's setup()
    couldn't harvest patient ids because Spark was no longer responding.
    Aborting the entire multi-server ramp on that single-cell symptom
    threw away every server queued after spark, which is the wrong
    blast radius — the trust gate is the right place for this signal.

    K6_CONTEXT is passed as an ABSOLUTE container-side path. `open()` in k6
    resolves relative paths relative to the calling file, and our context
    path is used from src/fhirbench/k6/lib/context.js — a relative path there
    would get resolved against that file's directory, not the repo root.
    Passing /src/... sidesteps the ambiguity entirely.
    """
    out_ndjson.parent.mkdir(parents=True, exist_ok=True)
    container_ctx = f"/src/{K6_CTX_PATH.relative_to(REPO_ROOT)}"
    # Run as the host user so k6 can write NDJSON into the bind-mounted
    # results dir. The stock grafana/k6 image runs as uid 12345 which
    # isn't in the host's group for the results tree — the container
    # silently logs "permission denied" and exits 255 without writing
    # a summary, which is invisible at the make-target level.
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    cmd = [
        "docker", "run", "--rm",
        "--user", uid_gid,
        "--network", "host",
        "-v", f"{REPO_ROOT}:/src",
        "-w", "/src",
        "-e", f"K6_SERVER={server_id}",
        "-e", f"K6_CONTEXT={container_ctx}",
        "-e", f"WORKLOAD_DURATION={int(duration_s)}",
        K6_IMAGE,
        "run",
        "--out", f"json={out_ndjson.relative_to(REPO_ROOT)}",
        f"src/fhirbench/k6/{script}",
    ]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, check=False)
    # k6 exit codes (https://grafana.com/docs/k6/latest/results-output/exit-codes/):
    #   0       success
    #   99      thresholds failed (we don't set k6 thresholds — won't fire)
    #   104-108 script errors (107 = uncaught exception in JS, e.g. setup throw)
    # Docker itself uses 125-127 for its own errors, and 137/139/etc. for
    # signals. Anything outside the "k6 ran the script and the script
    # decided to fail" range is a real infrastructure problem.
    K6_SCRIPT_FAILURE_EXITS = {99, 104, 105, 106, 107, 108}
    if proc.returncode == 0:
        return True
    if proc.returncode in K6_SCRIPT_FAILURE_EXITS:
        print(
            f"  [k6-warn] {server_id}/{script}: k6 exited {proc.returncode} "
            f"(in-script failure — likely server wedged or setup() threw). "
            f"Marking cell unreliable and continuing.",
            file=sys.stderr,
        )
        return False
    raise subprocess.CalledProcessError(proc.returncode, cmd)


def _postprocess(k6_ndjson: Path, workload: str, out_jsonl: Path) -> None:
    """Convert k6 NDJSON → OpRecord JSONL via fhirbench.k6.postprocess."""
    from fhirbench.k6.postprocess import convert
    n = convert(k6_ndjson, workload, out_jsonl)
    if n == 0:
        # Zero records would silently produce an empty cell (trust-gate
        # says "0 ok samples → not reliable" and greys the heatmap). Log
        # loudly so this doesn't masquerade as a workload result.
        print(f"  [k6-warn] {out_jsonl.name}: zero samples — "
              f"check {k6_ndjson} for k6 runtime errors", file=sys.stderr)


def run_workloads(
    server_id: str,
    server_dir: Path,
    workload_duration: float,
) -> None:
    """Run k6 CRUD + Search against server_id; write cell-shape JSONLs.

    Side effects (all inside server_dir):
      - crud.jsonl            — one JSON line per CRUD op
      - search.jsonl          — one JSON line per search op
      - k6_crud.ndjson        — raw k6 output, kept for debugging
      - k6_search.ndjson      — raw k6 output, kept for debugging

    Raises subprocess.CalledProcessError if k6 itself fails to boot. Zero-
    sample runs are logged as a warning but don't raise — the trust gate
    in cell_summary.py will flag the cell as unreliable and the heatmap
    will desaturate it, which is the correct signal.
    """
    server_dir = Path(server_dir)

    # Search runs BEFORE CRUD (same order ramp.py uses for the Python
    # workloads) — measures the post-ingest steady state, not a state
    # mutated by creates/deletes from the CRUD phase.
    for workload, script, jsonl_name, ndjson_name in (
        ("search", "search.js", "search.jsonl", "k6_search.ndjson"),
        ("crud",   "crud.js",   "crud.jsonl",   "k6_crud.ndjson"),
    ):
        print(f"  [k6] {workload} on {server_id} ({workload_duration:.0f}s)")
        _emit_context(server_id, workload)
        ndjson = server_dir / ndjson_name
        ok = _run_k6(server_id, script, workload_duration, ndjson)
        # postprocess regardless: a partial NDJSON (setup() threw after
        # writing some samples) still feeds useful evidence, and convert()
        # already returns 0 + a [k6-warn] for the empty case. The trust
        # gate downstream consumes the (jsonl, ok) pair.
        _postprocess(ndjson, workload, server_dir / jsonl_name)
        if not ok:
            # Search failed → CRUD will fail too because its setup() asks
            # the same wedged server for patient ids. Skip CRUD instead of
            # spending another 15+ minutes confirming the wedge.
            print(
                f"  [k6-warn] {server_id}: skipping remaining workloads — "
                f"{workload} failed and the server is unlikely to recover "
                f"within this cell.",
                file=sys.stderr,
            )
            return
