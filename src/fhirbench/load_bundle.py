#!/usr/bin/env python3
"""POST one Synthea patient transaction Bundle to a FHIR server.

Usage:
    docker compose up -d                         # or run a single server
    pip install -e .
    python -m fhirbench.load_bundle --server hapi          # defaults to hapi
    python -m fhirbench.load_bundle --server aidbox
    python -m fhirbench.load_bundle --server medplum
    python -m fhirbench.load_bundle --server msfhir
    python -m fhirbench.load_bundle --server blaze
    python -m fhirbench.load_bundle --server spark

If `data/loadtest/fhir/` is empty on first run, this auto-invokes
`fhirbench.harness.generate --count 1` to clone+build Synthea and produce
one deterministic patient (seed=42, Massachusetts/Boston). The first
patient bundle, sorted alphabetically, is the one POSTed; with the fixed
seed, every fresh clone gets the same patient. Pass `--bundle path.json`
to override.

Prerequisites first: Synthea patient bundles use conditional references
to `Practitioner?identifier=...` and `Organization?identifier=...`. Those
references 404 unless the matching `practitionerInformation*.json` and
`hospitalInformation*.json` bundles have been ingested first. We POST
prerequisites in alphabetical order, then the patient bundle.

Each server gets the same bundles. Servers diverge on validation
strictness: Aidbox applies its own profile, Medplum is HAPI-permissive,
MS FHIR is mid-strict. Raw responses are printed so you can audit the
divergence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import httpx

from fhirbench.servers import build_headers, find_server, load_servers, resolve_base_url

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"
DEFAULT_FHIR_DIR = REPO_ROOT / "data" / "loadtest" / "fhir"
DEFAULT_PREREQ_DIR = REPO_ROOT / "data" / "loadtest" / "prerequisites"
ENV_FILE = REPO_ROOT / ".env"


def _reload_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            os.environ[k] = v


def _maybe_bootstrap_medplum(server: dict, servers_file: Path) -> dict:
    """If Medplum auth creds are blank, run bootstrap_medplum and reload config.

    bootstrap_medplum is idempotent: it probes /oauth2/token first and exits
    quickly if existing creds work. Triggering it on blank creds means a
    fresh-clone user following README quickstart against `--server medplum`
    no longer hits a 401 on an unprovisioned ClientApplication.
    """
    if server.get("id") != "medplum":
        return server
    auth = server.get("auth") or {}
    if auth.get("client_id") and auth.get("client_secret"):
        return server
    print("Medplum credentials blank in .env; running bootstrap_medplum ...", file=sys.stderr)
    try:
        subprocess.run(
            [sys.executable, "-m", "fhirbench.harness.bootstrap_medplum"],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: bootstrap_medplum failed (exit {exc.returncode})", file=sys.stderr)
        print("  Is Medplum running? Try: docker compose ps medplum", file=sys.stderr)
        sys.exit(1)
    _reload_env_file(ENV_FILE)
    return find_server(load_servers(servers_file), "medplum")


def _resolve_patient_bundle(explicit: str | None) -> Path:
    """Pick the bundle to POST.

    Precedence: --bundle wins. Otherwise the first patient bundle in
    data/loadtest/fhir/, alphabetical. If that directory is empty, run
    `fhirbench.harness.generate --count 1` first; with the default seed=42
    Synthea is deterministic, so every fresh clone gets the same patient.
    """
    if explicit:
        p = Path(explicit)
        if not p.exists():
            print(f"ERROR: --bundle file not found: {p}", file=sys.stderr)
            sys.exit(2)
        return p

    bundles = sorted(DEFAULT_FHIR_DIR.glob("*.json")) if DEFAULT_FHIR_DIR.exists() else []
    if not bundles:
        print(
            f"No patient bundles in {DEFAULT_FHIR_DIR.relative_to(REPO_ROOT)}; "
            "running fhirbench.harness.generate --count 1 ...",
            file=sys.stderr,
        )
        try:
            subprocess.run(
                [sys.executable, "-m", "fhirbench.harness.generate", "--count", "1"],
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: generate failed (exit {exc.returncode})", file=sys.stderr)
            print(
                "  Synthea needs Java + git on PATH. See "
                "src/fhirbench/harness/generate.py for the build details.",
                file=sys.stderr,
            )
            sys.exit(1)
        bundles = sorted(DEFAULT_FHIR_DIR.glob("*.json"))
    if not bundles:
        print(f"ERROR: still no bundles in {DEFAULT_FHIR_DIR} after generate", file=sys.stderr)
        sys.exit(1)
    return bundles[0]


def _post_bundle(client: httpx.Client, server: dict, base_url: str, path: Path) -> int:
    """POST one transaction Bundle. Returns failure count among the entries."""
    bundle = json.loads(path.read_text())
    if bundle.get("type") != "transaction":
        print(f"ERROR: {path.name}: bundle type is '{bundle.get('type')}', expected 'transaction'",
              file=sys.stderr)
        return 1

    entry_count = len(bundle.get("entry") or [])
    print(f"  POST {path.name} ({entry_count} entries) ...")
    try:
        headers = build_headers(server, client)
        resp = client.post(base_url, json=bundle, headers=headers)
    except httpx.RequestError as exc:
        print(f"ERROR: HTTP request failed: {exc}", file=sys.stderr)
        print(f"  Is the server running? Try: curl -sf {base_url}/metadata > /dev/null && echo OK",
              file=sys.stderr)
        return entry_count or 1

    if not (200 <= resp.status_code < 300):
        print(f"ERROR: server returned HTTP {resp.status_code}", file=sys.stderr)
        print(resp.text[:1500], file=sys.stderr)
        return entry_count or 1

    try:
        result = resp.json()
    except Exception:
        print("ERROR: response was not valid JSON", file=sys.stderr)
        print(resp.text[:500], file=sys.stderr)
        return entry_count or 1

    statuses: Counter[str] = Counter()
    for e in result.get("entry") or []:
        status = (e.get("response") or {}).get("status", "missing")
        statuses[status.split()[0]] += 1
    summary = ", ".join(f"{n} × {s}" for s, n in sorted(statuses.items()))
    print(f"    {entry_count} entries ({summary})")
    return sum(n for s, n in statuses.items() if not s.startswith("2"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", default="hapi", help="Server id from servers.yaml (default: hapi)")
    parser.add_argument("--servers-file", default=str(DEFAULT_SERVERS),
                        help="Path to servers.yaml (default: config/servers.yaml)")
    parser.add_argument("--bundle", default=None,
                        help="Path to a specific transaction Bundle JSON. "
                             "Default: first bundle in data/loadtest/fhir/, generated on first run.")
    parser.add_argument("--prereq-dir", default=str(DEFAULT_PREREQ_DIR),
                        help="Directory of practitioner/hospital prerequisite bundles "
                             "to POST first (default: data/loadtest/prerequisites/)")
    parser.add_argument("--skip-prereqs", action="store_true",
                        help="Skip the prerequisite bundles. Patient bundles will fail to "
                             "resolve Practitioner/Organization conditional references unless "
                             "the server already has them.")
    args = parser.parse_args()

    bundle_path = _resolve_patient_bundle(args.bundle)

    servers = load_servers(Path(args.servers_file))
    server = find_server(servers, args.server)
    server = _maybe_bootstrap_medplum(server, Path(args.servers_file))
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{args.server}' has no base_url configured", file=sys.stderr)
        return 2

    label = server.get("label", args.server)
    print(f"Loading to {label} at {base_url}")

    failures = 0
    with httpx.Client(timeout=120.0) as client:
        if not args.skip_prereqs:
            prereq_dir = Path(args.prereq_dir)
            prereqs = sorted(prereq_dir.glob("*.json")) if prereq_dir.exists() else []
            if prereqs:
                print(f"Prerequisites: {len(prereqs)} bundle(s) from {prereq_dir.relative_to(REPO_ROOT)}")
                for p in prereqs:
                    failures += _post_bundle(client, server, base_url, p)
        print(f"Patient bundle: {bundle_path.relative_to(REPO_ROOT)}")
        failures += _post_bundle(client, server, base_url, bundle_path)

    if failures:
        print(f"\n{failures} entries did not return 2xx — see server logs", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
