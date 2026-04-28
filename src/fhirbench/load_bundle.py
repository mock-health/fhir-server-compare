#!/usr/bin/env python3
"""POST data/Aurelio_Whorton_transaction.json to a FHIR server.

Usage:
    docker compose up -d                         # or run a single server
    pip install -r requirements.txt
    python load_bundle.py --server hapi          # defaults to hapi
    python load_bundle.py --server aidbox
    python load_bundle.py --server medplum
    python load_bundle.py --server msfhir
    python load_bundle.py --server blaze
    python load_bundle.py --server spark

Loads one Synthea patient (Aurelio Whorton, 171 resources) as a single
FHIR transaction bundle. The bundle's internal `urn:uuid:` references are
resolved automatically by FHIR transaction semantics — no client-side
rewriting required.

Each server gets the same bundle. Servers diverge on validation strictness:
Aidbox applies its own profile, Medplum is HAPI-permissive, MS FHIR is
mid-strict. Raw responses are printed so you can audit the divergence.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import httpx

from fhirbench.servers import build_headers, find_server, load_servers, resolve_base_url

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_PATH = REPO_ROOT / "data" / "Aurelio_Whorton_transaction.json"
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="hapi", help="Server id from servers.yaml (default: hapi)")
    parser.add_argument("--servers-file", default=str(DEFAULT_SERVERS))
    parser.add_argument("--bundle", default=str(BUNDLE_PATH))
    args = parser.parse_args()

    bundle_path = Path(args.bundle)
    if not bundle_path.exists():
        print(f"ERROR: bundle not found: {bundle_path}", file=sys.stderr)
        return 2

    bundle = json.loads(bundle_path.read_text())
    if bundle.get("type") != "transaction":
        print(f"ERROR: bundle type is '{bundle.get('type')}', expected 'transaction'", file=sys.stderr)
        return 2

    servers = load_servers(Path(args.servers_file))
    server = find_server(servers, args.server)
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{args.server}' has no base_url configured", file=sys.stderr)
        return 2

    entry_count = len(bundle.get("entry") or [])
    print(f"Loading {entry_count} entries to {server.get('label', args.server)} at {base_url} ...")

    try:
        with httpx.Client(timeout=120.0) as client:
            headers = build_headers(server, client)
            resp = client.post(base_url, json=bundle, headers=headers)
    except httpx.RequestError as exc:
        print(f"ERROR: HTTP request failed: {exc}", file=sys.stderr)
        print(f"  Is {args.server} running? Try: curl -sf {base_url}/metadata > /dev/null && echo OK", file=sys.stderr)
        return 1

    if not (200 <= resp.status_code < 300):
        print(f"ERROR: {args.server} returned HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text[:1500], file=sys.stderr)
        return 1

    try:
        result = resp.json()
    except Exception:
        print("ERROR: response was not valid JSON", file=sys.stderr)
        print(resp.text[:500], file=sys.stderr)
        return 1

    statuses: Counter[str] = Counter()
    for e in result.get("entry") or []:
        status = (e.get("response") or {}).get("status", "missing")
        statuses[status.split()[0]] += 1

    summary = ", ".join(f"{n} × {s}" for s, n in sorted(statuses.items()))
    print(f"Loaded {entry_count} entries ({summary})")

    failures = sum(n for s, n in statuses.items() if not s.startswith("2"))
    if failures:
        print(f"\n{failures} entries did not return 2xx — see server logs", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
