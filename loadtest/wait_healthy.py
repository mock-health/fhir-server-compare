#!/usr/bin/env python3
"""Poll a server's /metadata until it returns 2xx, or timeout.

Replaces a fixed `sleep 90` after `docker compose up`: fresh boots can take
30s or 3 minutes depending on the server and whether it's doing first-time
schema migration. Polling makes the between-stage gaps self-adjusting.

Usage:
    python -m loadtest.wait_healthy --server hapi --timeout 300
    python -m loadtest.wait_healthy --server medplum --timeout 180 --no-auth

The --no-auth flag probes /metadata unauthenticated — useful right after
`up` when client_credentials might not be ready yet (Medplum).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _fhir_servers import build_headers, find_server, load_servers, resolve_base_url  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVERS = REPO_ROOT / "servers.yaml"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", required=True)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--timeout", type=float, default=300.0, help="seconds")
    ap.add_argument("--interval", type=float, default=3.0)
    ap.add_argument("--no-auth", action="store_true",
                    help="Skip auth header (probe metadata bare — Medplum may not have OAuth ready yet)")
    args = ap.parse_args()

    servers = load_servers(args.servers_file)
    server = find_server(servers, args.server)
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{args.server}' has no base_url configured", file=sys.stderr)
        return 2
    probe_url = f"{base_url}/metadata"

    deadline = time.monotonic() + args.timeout
    attempts = 0
    last_err = ""
    with httpx.Client(timeout=5.0) as client:
        while time.monotonic() < deadline:
            attempts += 1
            headers = {"Accept": "application/fhir+json"}
            if not args.no_auth:
                try:
                    headers = build_headers(server, client)
                except Exception as exc:
                    last_err = f"auth-setup: {exc}"
                    time.sleep(args.interval)
                    continue
            try:
                resp = client.get(probe_url, headers=headers)
                if 200 <= resp.status_code < 300:
                    elapsed = time.monotonic() - deadline + args.timeout
                    print(f"  {args.server} ready after {elapsed:.0f}s ({attempts} probes)")
                    return 0
                last_err = f"HTTP {resp.status_code}"
            except httpx.RequestError as exc:
                last_err = f"{exc.__class__.__name__}"
            time.sleep(args.interval)

    print(f"ERROR: {args.server} not healthy after {args.timeout}s ({attempts} probes, last: {last_err})",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
