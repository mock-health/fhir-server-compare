#!/usr/bin/env python3
"""Bootstrap a fresh Medplum instance and write OAuth creds back to .env.

After `docker compose down -v`, Medplum's Postgres volume is empty: no user,
no project, no ClientApplication. The MEDPLUM_CLIENT_ID/SECRET values in
.env are stale. This script:

  1. POSTs /auth/newuser to create the first super-admin user.
  2. POSTs /auth/newproject with the returned login to create a project.
  3. POSTs /admin/projects/<id>/client (authenticated with the project-scoped
     token) to create a ClientApplication.
  4. Rewrites the MEDPLUM_CLIENT_ID / MEDPLUM_CLIENT_SECRET lines in .env
     to match the freshly-created ClientApplication.

Idempotent: if Medplum already has a working ClientApplication whose id
matches .env, the script probes /oauth2/token and exits 0 without changes.
That means `make loadtest-bootstrap-medplum` is cheap to re-run.

Usage:
    python -m fhirbench.harness.bootstrap_medplum
    python -m fhirbench.harness.bootstrap_medplum --base-url http://localhost:8103
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import secrets
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV = REPO_ROOT / ".env"
DEFAULT_BASE_URL = "http://localhost:8103"

ADMIN_EMAIL = "admin@fhir-server-compare.local"
# Medplum rejects passwords present in HIBP's breach database. This is a
# random 24-char string — not meant to be memorable; it only exists so the
# project-creation flow succeeds. The ClientApplication credentials that
# come out of this script are what actually protect the data.
ADMIN_PASSWORD = "qZ7f-H2mKp4Xt8nRv3yLsW9B"
ADMIN_FIRST = "Loadtest"
ADMIN_LAST = "Admin"
PROJECT_NAME = "fhir-server-compare loadtest"
CLIENT_NAME = "loadtest-client"


def probe_existing_client(base_url: str, client_id: str, client_secret: str) -> bool:
    """Return True if current .env creds already work — no bootstrap needed."""
    if not client_id or not client_secret:
        return False
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.post(
                f"{base_url}/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
            )
        return r.status_code == 200 and "access_token" in r.text
    except httpx.RequestError:
        return False


def wait_for_medplum(base_url: str, timeout_s: float = 180.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=3.0) as c:
                r = c.get(f"{base_url}/healthcheck")
            if r.status_code == 200:
                return
        except httpx.RequestError:
            pass
        time.sleep(2)
    raise SystemExit(f"Medplum at {base_url} not reachable after {timeout_s}s")


def pkce_pair() -> tuple[str, str]:
    """Generate a PKCE code_verifier + its SHA-256 code_challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def obtain_auth_code(client: httpx.Client, base_url: str, code_challenge: str) -> tuple[str, bool]:
    """Return an (oauth2_code, project_already_existed) pair.

    Three recovery paths handled in order:
      1. Fresh DB: /auth/newuser → /auth/newproject. Both succeed; we return
         the newproject code.
      2. User created previously but project creation failed mid-script:
         /auth/newuser returns "Email already registered"; /auth/login
         succeeds and returns a login with no project; we complete
         /auth/newproject on that login.
      3. Prior script fully created user+project but never made the
         ClientApplication (or .env was wiped): /auth/login returns both
         `login` and `code` directly because Medplum auto-activates the only
         project membership; we skip newproject and return that code.
    """
    r = client.post(
        f"{base_url}/auth/newuser",
        json={
            "firstName": ADMIN_FIRST,
            "lastName": ADMIN_LAST,
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "recaptchaToken": "xxx",  # recaptchaSecretKey is empty in our config → skipped
            "projectName": PROJECT_NAME,
            "codeChallenge": code_challenge,
            "codeChallengeMethod": "S256",
        },
    )
    if r.status_code in (200, 201):
        # Path 1: fresh user, must create project
        login_id = r.json().get("login")
        if not login_id:
            raise SystemExit(f"newuser response missing login: {r.json()}")
        np = create_new_project(client, base_url, login_id)
        return np["code"], False

    if not (r.status_code == 400 and "Email already registered" in r.text):
        raise SystemExit(f"newuser failed: HTTP {r.status_code} — {r.text[:400]}")

    # User exists — log in to see whether a project is already attached.
    lr = client.post(
        f"{base_url}/auth/login",
        json={
            "email": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
            "codeChallenge": code_challenge,
            "codeChallengeMethod": "S256",
        },
    )
    if lr.status_code != 200:
        raise SystemExit(f"login fallback failed: HTTP {lr.status_code} — {lr.text[:400]}")
    body = lr.json()
    if body.get("code"):
        # Path 3: login short-circuits because the user has exactly one project
        return body["code"], True
    login_id = body.get("login")
    if not login_id:
        raise SystemExit(f"login response missing both code and login: {body}")
    # Path 2: login returned a pending login, finish project creation
    np = create_new_project(client, base_url, login_id)
    return np["code"], False


def create_new_project(client: httpx.Client, base_url: str, login_id: str) -> dict:
    r = client.post(
        f"{base_url}/auth/newproject",
        json={
            "login": login_id,
            "projectName": PROJECT_NAME,
            "firstName": ADMIN_FIRST,
            "lastName": ADMIN_LAST,
        },
    )
    if r.status_code not in (200, 201):
        raise SystemExit(f"newproject failed: HTTP {r.status_code} — {r.text[:400]}")
    return r.json()  # {"login":"...", "code":"..."}


def exchange_code_for_token(client: httpx.Client, base_url: str, code: str, code_verifier: str) -> str:
    r = client.post(
        f"{base_url}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": base_url.rstrip("/") + "/",
        },
    )
    if r.status_code != 200:
        raise SystemExit(f"token exchange failed: HTTP {r.status_code} — {r.text[:400]}")
    return r.json()["access_token"]


def find_default_client(client: httpx.Client, base_url: str, access_token: str) -> tuple[str, str]:
    """Find the 'Default Client' ClientApplication in the authenticated project.

    Medplum auto-creates a Default Client during /auth/newproject and links
    it to the project's membership graph. That's the one that works with
    client_credentials — POSTing our own ClientApplication creates an orphan
    without the project membership wiring, and OAuth rejects it as "Invalid
    client". A FHIR search in the project context returns the Default Client
    including its secret in the response body, so we can just read it.
    """
    r = client.get(
        f"{base_url}/fhir/R4/ClientApplication",
        params={"name:contains": "Default Client", "_count": "50"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if r.status_code != 200:
        raise SystemExit(f"ClientApplication search failed: HTTP {r.status_code} — {r.text[:400]}")
    entries = r.json().get("entry") or []
    # Prefer the one whose name matches our project, in case multiple exist.
    project_default_name = f"{PROJECT_NAME} Default Client"
    preferred = [e for e in entries if (e.get("resource") or {}).get("name") == project_default_name]
    candidates = preferred or entries
    for e in candidates:
        res = e.get("resource") or {}
        cid = res.get("id")
        secret = res.get("secret")
        if cid and secret:
            return cid, secret
    raise SystemExit(
        f"Default Client not found. Search returned {len(entries)} ClientApplications "
        f"but none had both id and secret populated."
    )


ENV_KEYS = {"MEDPLUM_CLIENT_ID", "MEDPLUM_CLIENT_SECRET"}


def update_env_file(env_path: Path, client_id: str, client_secret: str) -> None:
    """Rewrite MEDPLUM_CLIENT_ID / _SECRET lines in .env, preserving the rest."""
    new_vals = {"MEDPLUM_CLIENT_ID": client_id, "MEDPLUM_CLIENT_SECRET": client_secret}
    if env_path.exists():
        lines = env_path.read_text().splitlines()
    else:
        lines = []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group(1) in ENV_KEYS:
            key = m.group(1)
            out.append(f"{key}={new_vals[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key in ENV_KEYS - seen:
        out.append(f"{key}={new_vals[key]}")
    env_path.write_text("\n".join(out).rstrip() + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    ap.add_argument("--force", action="store_true",
                    help="Bootstrap even if existing .env creds work (skips the idempotent shortcut)")
    args = ap.parse_args()

    wait_for_medplum(args.base_url)

    existing_id = os.environ.get("MEDPLUM_CLIENT_ID", "")
    existing_secret = os.environ.get("MEDPLUM_CLIENT_SECRET", "")
    if not args.force and probe_existing_client(args.base_url, existing_id, existing_secret):
        print(f"Medplum creds in env already work — no bootstrap needed.")
        return 0

    print(f"Bootstrapping Medplum at {args.base_url} ...")
    code_verifier, code_challenge = pkce_pair()
    with httpx.Client(timeout=30.0) as client:
        code, reused = obtain_auth_code(client, args.base_url, code_challenge)
        state = "reusing existing user/project" if reused else "freshly created user+project"
        print(f"  step 1/3: auth code obtained ({state})")

        access_token = exchange_code_for_token(client, args.base_url, code, code_verifier)
        print(f"  step 2/3: access token obtained")

        cid, secret = find_default_client(client, args.base_url, access_token)
        print(f"  step 3/3: Default Client found (id={cid})")

    update_env_file(args.env_file, cid, secret)
    print(f"Wrote MEDPLUM_CLIENT_ID / MEDPLUM_CLIENT_SECRET to {args.env_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
