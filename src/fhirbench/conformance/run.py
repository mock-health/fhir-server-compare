"""
Drive the in-process Python TestScript runner against each server in
servers.yaml. Thin orchestrator: per server, invoke `runner.py` with the
testscript directory and a per-(round, server) output directory.

Output layout:
  results/conformance/<round_id>/<server_id>/<test-id>.testreport.json

`parse_report.py` consumes that tree and emits the round-v1 conformance.json.

Usage:
  python -m fhirbench.conformance.run \\
      --round 2026-q2-r000 \\
      --server hapi \\
      --testscripts conformance/testscripts/fhir-r4-base
  python -m fhirbench.conformance.run --round 2026-q2-r000 --server all
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

import httpx
import yaml


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
from fhirbench.servers import (  # noqa: E402
    AuthedSession,
    find_server,
    load_servers,
    resolve_base_url,
)


def _profile_id_from_testscripts_dir(ts_dir: pathlib.Path) -> str | None:
    """Recover profile id from a testscripts directory path:
    conformance/testscripts/<profile_id>/ → <profile_id>."""
    try:
        rel = ts_dir.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "conformance" and parts[1] == "testscripts":
        return parts[2]
    return None


def _load_applicability(profile_id: str) -> dict | None:
    """Read the applicability block from conformance/profiles/<profile_id>.yaml."""
    path = REPO_ROOT / "profiles" / "conformance" / f"{profile_id}.yaml"
    if not path.is_file():
        return None
    try:
        spec = yaml.safe_load(path.read_text()) or {}
    except Exception:
        return None
    return spec.get("applicability")


def applicability_probe(server_cfg: dict, base_url: str,
                        applicability: dict) -> tuple[bool, str]:
    """Execute the probe; return (is_na, reason).

    is_na=True means the profile's surface isn't implemented by this server
    and downstream tests should be scored as N/A rather than run.

    Two N/A signals, both optional:
      - `na_if_status`: response code in this set ⇒ N/A
      - `na_if_body_contains`: list of {substring, reason} — if substring is
        in the response body, N/A with that reason. Lets us distinguish
        "server has a bug" from "feature requires external infra not
        configured in this deployment" when both return 500 (e.g. Aidbox
        responds 500 with body 'storage-type not specified' because Bulk
        Data requires a cloud storage backend that the local dev image
        doesn't ship — that's scope, not a defect).
    """
    probe = applicability.get("probe") or {}
    method = (probe.get("method") or "GET").upper()
    path = probe.get("path") or "/"
    headers = dict(probe.get("headers") or {})
    na_statuses = set(applicability.get("na_if_status") or [])
    body_rules = applicability.get("na_if_body_contains") or []
    default_reason = applicability.get("na_reason") or "probe matched na rule"

    url = base_url.rstrip("/") + path
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
            session = AuthedSession(server_cfg, client)
            resp = session.request(method, url, headers=headers)
    except httpx.HTTPError as e:
        # Connection error on the probe itself → treat as "unknown, run tests"
        # so we don't accidentally short-circuit the matrix on a flaky moment.
        return False, f"probe errored ({type(e).__name__}); running tests"
    if resp.status_code in na_statuses:
        return True, f"{default_reason} (probe {method} {path} → {resp.status_code})"
    body = resp.text or ""
    for rule in body_rules:
        needle = rule.get("substring") or ""
        if needle and needle in body:
            rule_reason = rule.get("reason") or default_reason
            return True, f"{rule_reason} (probe {method} {path} → {resp.status_code}, body matched {needle!r})"
    return False, f"probe {method} {path} → {resp.status_code}"


# The 6-server OSS roster — every server here can be reproduced locally
# via docker-compose.yml with no paid license or managed-service account.
ROSTER = ("hapi", "msfhir", "medplum", "aidbox", "blaze", "spark")


def smoke_check(servers_yaml: pathlib.Path, targets: list[str]) -> list[str]:
    """Probe /metadata on every target server. Returns the list of unreachable
    servers (empty = all healthy). Exists to fail fast before TestScripts run,
    so a silent boot failure (e.g., MS FHIR with bad security config) doesn't
    masquerade as a conformance failure in the matrix."""
    servers = load_servers(servers_yaml)
    unreachable: list[str] = []
    print("\n=== smoke check: GET /metadata on each server ===", file=sys.stderr)
    timeout = httpx.Timeout(10.0, connect=5.0)
    with httpx.Client(timeout=timeout) as client:
        for sid in targets:
            try:
                server = find_server(servers, sid)
                base_url = resolve_base_url(server)
                if not base_url:
                    print(f"  [skip] {sid}: empty base_url (env var unset)", file=sys.stderr)
                    unreachable.append(sid)
                    continue
                session = AuthedSession(server, client)
                resp = session.request("GET", f"{base_url.rstrip('/')}/metadata",
                                       headers={"Accept": "application/fhir+json"})
                if 200 <= resp.status_code < 300:
                    print(f"  [ok]   {sid}: {resp.status_code}", file=sys.stderr)
                else:
                    print(f"  [fail] {sid}: {resp.status_code} on /metadata", file=sys.stderr)
                    unreachable.append(sid)
            except Exception as e:
                print(f"  [fail] {sid}: {type(e).__name__}: {e}", file=sys.stderr)
                unreachable.append(sid)
    return unreachable


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--round", required=True, help="round id (e.g. 2026-q2-r000)")
    p.add_argument("--server", default="all",
                   help="server id, comma-separated, or 'all'")
    p.add_argument("--testscripts", default="conformance/testscripts/fhir-r4-base",
                   help="path under conformance/testscripts/")
    p.add_argument("--skip-smoke-check", action="store_true",
                   help="skip the pre-run /metadata probe (default: run it)")
    args = p.parse_args()

    if args.server == "all":
        targets = list(ROSTER)
    else:
        targets = [s.strip() for s in args.server.split(",") if s.strip()]
        for t in targets:
            if t not in ROSTER:
                print(f"[warn] {t}: not in roster {ROSTER}", file=sys.stderr)

    testscripts_dir = REPO_ROOT / args.testscripts
    if not testscripts_dir.is_dir():
        raise SystemExit(f"testscripts dir not found: {testscripts_dir}")

    if not args.skip_smoke_check:
        unreachable = smoke_check(REPO_ROOT / "config" / "servers.yaml", targets)
        if unreachable:
            print(
                f"\n[fatal] smoke check failed for: {' '.join(unreachable)}\n"
                f"        Run `docker compose ps` and check those containers.\n"
                f"        Re-run with --skip-smoke-check to bypass this gate "
                f"(do NOT use this for published rounds — silent boot failures\n"
                f"        get scored as conformance failures, which misattributes\n"
                f"        the defect to the server).",
                file=sys.stderr,
            )
            sys.exit(2)

    profile_id = _profile_id_from_testscripts_dir(testscripts_dir)
    applicability = _load_applicability(profile_id) if profile_id else None
    servers_cfg = load_servers(REPO_ROOT / "config" / "servers.yaml") if applicability else None

    failed: list[str] = []
    for sid in targets:
        out_dir = REPO_ROOT / "results" / "conformance" / args.round / sid
        out_dir.mkdir(parents=True, exist_ok=True)
        log_path = out_dir / "_run.log"
        print(f"\n=== {sid} ===", file=sys.stderr)

        # Per-profile applicability probe. If the server doesn't implement the
        # profile's surface at all, write a marker and skip test execution —
        # parse_report.py picks up the marker and emits na-outcome cells.
        marker_path = out_dir / f"_applicability_{profile_id}.json" if profile_id else None
        if applicability and profile_id and servers_cfg is not None:
            try:
                server_cfg = find_server(servers_cfg, sid)
                base_url = resolve_base_url(server_cfg)
                if base_url:
                    is_na, reason = applicability_probe(
                        server_cfg, base_url, applicability)
                    if is_na:
                        # Remove any TestReports from prior runs that belong to
                        # THIS profile, so they don't shadow the N/A verdict
                        # when parse_report folds the round. TestReports are
                        # named <testscript-stem>.testreport.json, so we match
                        # by the set of testscript stems under the profile dir.
                        ts_stems = {
                            p.stem for p in testscripts_dir.rglob("*.json")
                        }
                        for tr in out_dir.glob("*.testreport.json"):
                            if tr.stem.replace(".testreport", "") in ts_stems:
                                tr.unlink()
                        marker_path.write_text(json.dumps({
                            "status": "na",
                            "reason": reason,
                            "profile_id": profile_id,
                        }, indent=2) + "\n")
                        print(f"  [na]   skipping {profile_id}: {reason}",
                              file=sys.stderr)
                        continue
                    else:
                        # Remove any stale marker from a prior N/A run so the
                        # new pass/fail results aren't shadowed.
                        if marker_path.exists():
                            marker_path.unlink()
            except Exception as e:
                print(f"  [warn] applicability probe errored for {sid}: {e} — "
                      "running tests anyway", file=sys.stderr)

        with log_path.open("w") as logf:
            r = subprocess.run(
                [sys.executable, "-m", "fhirbench.conformance.runner",
                 "--server", sid,
                 "--testscripts", str(testscripts_dir),
                 "--testreport-dir", str(out_dir)],
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            logf.write(r.stdout)
        # Echo runner stdout to user's terminal too.
        print(r.stdout.rstrip(), file=sys.stderr)
        if r.returncode != 0:
            failed.append(sid)

    print(f"\n[done] reports in: results/conformance/{args.round}/", file=sys.stderr)
    if failed:
        print(f"[warn] failures: {' '.join(failed)} (see _run.log per server)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
