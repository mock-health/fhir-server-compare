#!/usr/bin/env python3
"""Emit a single JSON context file that the k6 harness consumes.

k6 can't read YAML natively and doesn't have env-var interpolation matching
`_fhir_servers._interp`. Rather than reimplementing that in JavaScript (two
places to keep in sync), we do the resolution once in Python and hand k6 a
flat JSON blob with everything already resolved: server configs, OAuth2
tokens pre-minted for the Basic/client_credentials auth types, and the
queries subset participating in the load-test workload.

Output shape (stable for the k6 lib to consume):
  {
    "servers": [
      { "id": "...", "label": "...", "base_url": "...",
        "auth_headers": { "Authorization": "...", ... },
        "extra_headers": { "x-bundle-processing-logic": "parallel" }
      },
      ...
    ],
    "queries": [  # loadtest-included queries only
      { "name": "...", "method": "GET", "path": "Patient",
        "params": {...}, "headers": {...}, "sample": {...} },
      ...
    ],
    "generated_at": "2026-04-24T20:00:00Z",
    "servers_yaml_digest": "sha256:...",
    "queries_yaml_digest": "sha256:..."
  }

Why pre-mint auth headers here:
  - Basic: base64 encode once, avoid shipping credentials into k6 VUs as
    plaintext env vars.
  - client_credentials: fetch the bearer token via the token endpoint ONCE,
    embed it in the context. k6 VUs reuse the same token for the duration
    of the workload. At a 15-minute workload + a 60-minute token TTL (the
    Medplum default) the token doesn't roll mid-run. If we ever need
    longer, k6 can re-mint in `setup()` — but the Python post-fetch is
    simpler and mirrors the Python harness's once-per-session token.

Usage:
  python -m scripts.emit_k6_context --server all --workload search \\
      --out loadtest/k6/k6_context.json
  python -m scripts.emit_k6_context --server aidbox --workload crud \\
      --out /tmp/k6_ctx.json
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from _fhir_servers import (  # noqa: E402
    find_server,
    load_servers,
    resolve_base_url,
    client_credentials_token,
)

DEFAULT_SERVERS = REPO_ROOT / "servers.yaml"
DEFAULT_QUERIES = REPO_ROOT / "queries.yaml"


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _auth_headers_for(server: dict, client: httpx.Client) -> dict[str, str]:
    """Return the Authorization header set for this server, if any.

    FHIR Accept / Content-Type aren't baked in here — k6 adds them on every
    request (static, free to set per-call in JS).
    """
    auth = server.get("auth") or {}
    atype = auth.get("type", "none")
    if atype == "none":
        return {}
    if atype == "basic":
        token = base64.b64encode(
            f"{auth['username']}:{auth['password']}".encode()
        ).decode()
        return {"Authorization": f"Basic {token}"}
    if atype == "bearer_static":
        return {"Authorization": f"Bearer {auth['token']}"}
    if atype == "client_credentials":
        bearer = client_credentials_token(
            auth["token_url"], auth["client_id"], auth["client_secret"], client,
        )
        return {"Authorization": f"Bearer {bearer}"}
    raise ValueError(f"unknown auth type: {atype}")


def _load_queries(queries_path: Path, workload: str) -> list[dict]:
    """Return queries that participate in the given workload.

    Mirrors load_queries() in workload_search.py: filters out
    `loadtest: skip:*`. For the CRUD workload we don't need queries at all
    (the op templates are hard-coded), but we still emit the list unfiltered
    so the same context file is reusable.
    """
    import yaml  # type: ignore
    data = yaml.safe_load(queries_path.read_text()) or {}
    items = data.get("queries") or []
    out: list[dict] = []
    for q in items:
        if workload == "search":
            marker = (q.get("loadtest") or "").strip()
            if marker.startswith("skip"):
                continue
        out.append({
            "name": q.get("name"),
            "method": (q.get("method") or "GET").upper(),
            "path": q.get("path") or "",
            "params": q.get("params") or {},
            "headers": q.get("headers") or {},
            "body": q.get("body"),
            "sample": q.get("sample") or {},
        })
    return out


def build_context(
    server_ids: list[str],
    workload: str,
    servers_path: Path,
    queries_path: Path,
) -> dict:
    servers_all = load_servers(servers_path)
    out_servers: list[dict] = []
    with httpx.Client(timeout=30.0) as client:
        for sid in server_ids:
            try:
                s = find_server(servers_all, sid)
            except SystemExit:
                # Skip unknown ids rather than failing the whole context build
                # — k6 will just not test them.
                print(f"[warn] server '{sid}' not in servers.yaml — skipping", file=sys.stderr)
                continue
            base_url = resolve_base_url(s)
            if not base_url:
                print(f"[warn] server '{sid}' has no base_url — skipping", file=sys.stderr)
                continue
            try:
                auth_headers = _auth_headers_for(s, client)
            except Exception as exc:
                print(f"[warn] could not mint auth for '{sid}': {exc}", file=sys.stderr)
                auth_headers = {}
            out_servers.append({
                "id": s.get("id"),
                "label": s.get("label") or s.get("id"),
                "version": s.get("version", ""),
                "base_url": base_url,
                "auth_headers": auth_headers,
                "extra_headers": s.get("extra_headers") or {},
            })

    queries = _load_queries(queries_path, workload)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "workload": workload,
        "servers": out_servers,
        "queries": queries,
        "servers_yaml_digest": _file_digest(servers_path),
        "queries_yaml_digest": _file_digest(queries_path),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", required=True,
                    help="comma-separated server ids or 'all'")
    ap.add_argument("--workload", choices=("crud", "search"), required=True)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--queries-file", type=Path, default=DEFAULT_QUERIES)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    if args.server == "all":
        servers_all = load_servers(args.servers_file)
        server_ids = [s.get("id") for s in servers_all if s.get("id")]
    else:
        server_ids = [s.strip() for s in args.server.split(",") if s.strip()]

    ctx = build_context(
        server_ids=server_ids,
        workload=args.workload,
        servers_path=args.servers_file,
        queries_path=args.queries_file,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(ctx, indent=2) + "\n")
    print(f"[ok] wrote {args.out} ({len(ctx['servers'])} server(s), "
          f"{len(ctx['queries'])} query/ies)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
