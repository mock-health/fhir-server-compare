#!/usr/bin/env python3
"""Run a set of FHIR queries against N servers defined in servers.yaml,
diff the responses, and print a markdown matrix to stdout.

This is the runnable companion to the blog post
"Same FHIR, Different Answers: Comparing 5 FHIR Servers"
(https://mock.health/blog/fhir-server-compare).

Typical usage:

    docker compose up -d          # brings up HAPI, Aidbox, Medplum, MS FHIR
    pip install -r requirements.txt
    python load_bundle.py --server hapi      # (repeat for each server)
    python compare.py

Each server in servers.yaml is probed on /metadata at startup. Servers that
respond 2xx become columns in the output. Servers that don't respond are
skipped with a one-line reason, so a bare-metal HAPI-only install degrades
cleanly.

The finding the script makes is structural: which queries succeed, which
fail, and what each backend's response shape looks like on the same input.
Latency numbers are informational, not benchmark-quality.
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from fhirbench.servers import build_headers, load_servers, resolve_base_url

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_QUERIES = REPO_ROOT / "config" / "queries.yaml"
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"


def load_queries(path: Path) -> list[dict]:
    """Load queries.yaml, filtering out any row tagged `matrix: skip:<reason>`.

    The matrix:skip marker is the mirror of loadtest:skip — it keeps
    load-only queries (e.g., runtime-sampled ones with `{{placeholder}}`
    values that compare.py has no way to resolve) out of the behavior
    matrix, while still letting the k6 search workload (src/fhirbench/k6/search.js)
    fire them.
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        print("ERROR: PyYAML not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)
    data = yaml.safe_load(path.read_text())
    items = data.get("queries") if isinstance(data, dict) else None
    if not isinstance(items, list):
        print(f"ERROR: {path} must contain a top-level 'queries:' list", file=sys.stderr)
        sys.exit(2)
    out: list[dict] = []
    for q in items:
        marker = (q.get("matrix") or "").strip()
        if marker.startswith("skip"):
            continue
        out.append(q)
    return out


# --------------------------------------------------------------------------
# Server probing
# --------------------------------------------------------------------------


@dataclass
class ReadyServer:
    id: str
    label: str
    base_url: str
    headers: dict[str, str]


def probe_servers(servers: list[dict], client: httpx.Client) -> list[ReadyServer]:
    ready: list[ReadyServer] = []
    for s in servers:
        sid = s.get("id", "?")
        label = s.get("label", sid)
        base_url = resolve_base_url(s)
        if not base_url:
            print(f"  [skip] {label}: base_url not configured")
            continue
        probe_url = f"{base_url}/metadata"
        try:
            headers = build_headers(s, client)
        except httpx.RequestError as exc:
            # client_credentials token fetch failed on network — server is likely down
            print(f"  [skip] {label}: {probe_url} unreachable ({exc.__class__.__name__})")
            continue
        except Exception as exc:
            print(f"  [skip] {label}: auth setup failed — {exc}")
            continue
        try:
            resp = client.get(probe_url, headers=headers, timeout=5.0)
        except httpx.RequestError as exc:
            print(f"  [skip] {label}: {probe_url} unreachable ({exc.__class__.__name__})")
            continue
        if not (200 <= resp.status_code < 300):
            print(f"  [skip] {label}: {probe_url} returned {resp.status_code}")
            continue
        print(f"  [ok]   {label}: {base_url}")
        ready.append(ReadyServer(id=sid, label=label, base_url=base_url, headers=headers))
    return ready


# --------------------------------------------------------------------------
# Per-query response capture
# --------------------------------------------------------------------------


@dataclass
class QueryResult:
    server_id: str
    status_code: int
    ok: bool
    body: Any
    resource_type: str | None
    bundle_total: int | None
    entry_count: int | None
    latency_ms: int


def run_query(server: ReadyServer, query: dict, client: httpx.Client) -> QueryResult:
    method = (query.get("method") or "GET").upper()
    path = query.get("path") or ""
    url = f"{server.base_url}/{path.lstrip('/')}"
    params = query.get("params") or {}
    body = query.get("body")
    headers = {**server.headers, **(query.get("headers") or {})}

    start = time.monotonic()
    try:
        if method == "POST":
            resp = client.post(url, params=params, json=body, headers=headers)
        else:
            resp = client.get(url, params=params, headers=headers)
    except httpx.RequestError as exc:
        return QueryResult(
            server_id=server.id, status_code=0, ok=False, body=str(exc),
            resource_type=None, bundle_total=None, entry_count=None,
            latency_ms=int((time.monotonic() - start) * 1000),
        )

    latency_ms = int((time.monotonic() - start) * 1000)
    try:
        parsed = resp.json()
    except Exception:
        parsed = resp.text

    rt, total, ec = None, None, None
    if isinstance(parsed, dict):
        rt = parsed.get("resourceType")
        if rt == "Bundle":
            total = parsed.get("total")
            entries = parsed.get("entry") or []
            ec = len(entries) if isinstance(entries, list) else None

    return QueryResult(
        server_id=server.id,
        status_code=resp.status_code,
        ok=200 <= resp.status_code < 300,
        body=parsed,
        resource_type=rt,
        bundle_total=total,
        entry_count=ec,
        latency_ms=latency_ms,
    )


# --------------------------------------------------------------------------
# Matrix rendering
# --------------------------------------------------------------------------


def fmt_cell(r: QueryResult) -> str:
    """One-line compact cell: 'status · total · entries'."""
    status = str(r.status_code) if r.status_code else "ERR"
    total = "—" if r.bundle_total is None else str(r.bundle_total)
    entries = "—" if r.entry_count is None else str(r.entry_count)
    return f"{status} · {total} · {entries}"


def compute_verdict(results: list[QueryResult]) -> str:
    """Any pair divergence on (status, type, total, entries) = DIVERGED."""
    if len(results) < 2:
        return "—"
    base = results[0]
    for other in results[1:]:
        if (
            base.status_code != other.status_code
            or base.resource_type != other.resource_type
            or base.bundle_total != other.bundle_total
            or base.entry_count != other.entry_count
        ):
            return "DIVERGED"
    return "IDENTICAL"


def render_table(
    queries: list[dict],
    servers: list[ReadyServer],
    rows: list[list[QueryResult]],
) -> str:
    headers = ["#", "Query"] + [s.label for s in servers] + ["Verdict"]
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for i, (q, row) in enumerate(zip(queries, rows), start=1):
        name = q.get("name", "?")
        cells = [fmt_cell(r) for r in row]
        verdict = compute_verdict(row)
        lines.append("| " + " | ".join([str(i), f"`{name}`"] + cells + [verdict]) + " |")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--queries",
        default=str(DEFAULT_QUERIES),
        help="Path to queries.yaml (default: config/queries.yaml)",
    )
    parser.add_argument(
        "--servers-file",
        "--servers",
        dest="servers_file",
        default=str(DEFAULT_SERVERS),
        help="Path to servers.yaml (default: config/servers.yaml)",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated server ids to include (default: all in servers.yaml)",
    )
    args = parser.parse_args()

    queries_path = Path(args.queries)
    servers_path = Path(args.servers_file)
    for p, kind in [(queries_path, "queries"), (servers_path, "servers")]:
        if not p.exists():
            print(f"ERROR: {kind} file not found: {p}", file=sys.stderr)
            return 2

    queries = load_queries(queries_path)
    servers_cfg = load_servers(servers_path)

    if args.only:
        allow = {s.strip() for s in args.only.split(",") if s.strip()}
        known = {s.get("id") for s in servers_cfg}
        unknown = allow - known
        if unknown:
            print(
                f"ERROR: --only contains unknown server id(s): {', '.join(sorted(unknown))}",
                file=sys.stderr,
            )
            print(f"  Valid ids: {', '.join(sorted(i for i in known if i))}", file=sys.stderr)
            return 2
        servers_cfg = [s for s in servers_cfg if s.get("id") in allow]

    print("Probing servers:")
    with httpx.Client(timeout=60.0) as client:
        ready = probe_servers(servers_cfg, client)
        if not ready:
            print("\nNo servers reachable. Bring up at least one server and re-run.", file=sys.stderr)
            return 1

        print(f"\nRunning {len(queries)} queries × {len(ready)} servers\n")
        rows: list[list[QueryResult]] = []
        for i, q in enumerate(queries, start=1):
            name = q.get("name", f"q{i}")
            print(f"  [{i}/{len(queries)}] {name}")
            row = [run_query(s, q, client) for s in ready]
            rows.append(row)

    print()
    print(render_table(queries, ready, rows))
    print()
    if len(ready) < 2:
        print(
            f"Only 1 server ({ready[0].label}) reachable — no cross-server verdict possible. "
            "Bring up more servers to see divergence."
        )
    else:
        identical = sum(1 for row in rows if compute_verdict(row) == "IDENTICAL")
        print(
            f"{identical}/{len(rows)} queries identical across {len(ready)} servers, "
            f"{len(rows) - identical} divergent"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
