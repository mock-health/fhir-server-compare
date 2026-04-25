"""Shim that lets loadtest.ramp invoke the k6 harness in place of the Python
workload drivers (workload_crud + workload_search).

The Python ramp owns the per-server lifecycle (boot → wait healthy → bootstrap
→ ingest → workloads → cell_complete). Switching a single cell's workload
phase to k6 means replacing the two `workload_*.run()` calls with one
invocation here: we emit the k6 context file, run grafana/k6 inside docker
for CRUD and Search back-to-back, and convert the raw NDJSON into the same
crud.jsonl / search.jsonl shape the rest of the pipeline already reads.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
K6_IMAGE = os.environ.get("K6_IMAGE", "grafana/k6")
K6_CTX_PATH = REPO_ROOT / "loadtest" / "k6" / "k6_context.json"


def _emit_context(server_id: str, workload: str) -> None:
    """Call scripts.emit_k6_context as a subprocess.

    The context emitter resolves servers.yaml + queries.yaml + env-var
    interpolation once. Running it as a subprocess keeps this module free
    of its transitive imports (httpx, yaml) at load time; the ramp already
    invokes subprocesses for docker so the cost isn't material.
    """
    cmd = [
        sys.executable, "-m", "scripts.emit_k6_context",
        "--server", server_id,
        "--workload", workload,
        "--out", str(K6_CTX_PATH),
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def _run_k6(server_id: str, script: str, duration_s: float,
            out_ndjson: Path) -> None:
    """Run one k6 script via docker. Raises on non-zero exit.

    K6_CONTEXT is passed as an ABSOLUTE container-side path. `open()` in k6
    resolves relative paths relative to the calling file, and our context
    path is used from loadtest/k6/lib/context.js — a relative path there
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
        f"loadtest/k6/{script}",
    ]
    subprocess.run(cmd, cwd=REPO_ROOT, check=True)


def _postprocess(k6_ndjson: Path, workload: str, out_jsonl: Path) -> None:
    """Convert k6 NDJSON → OpRecord JSONL via loadtest.k6.postprocess."""
    from loadtest.k6.postprocess import convert
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
        _run_k6(server_id, script, workload_duration, ndjson)
        _postprocess(ndjson, workload, server_dir / jsonl_name)
