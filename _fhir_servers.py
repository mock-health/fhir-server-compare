"""Shared server config + auth shim used by compare.py and load_bundle.py.

Underscore-prefixed to signal it's internal to this repo; not a public API.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

FHIR_CONTENT_TYPE = "application/fhir+json"
ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _interp(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} in strings."""
    if isinstance(value, str):
        def repl(m: re.Match[str]) -> str:
            var, default = m.group(1), m.group(2)
            return os.environ.get(var, default if default is not None else "")
        return ENV_VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [_interp(v) for v in value]
    if isinstance(value, dict):
        return {k: _interp(v) for k, v in value.items()}
    return value


def load_servers(path: Path) -> list[dict]:
    try:
        import yaml  # type: ignore
    except ImportError:
        print("ERROR: PyYAML not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(2)
    data = yaml.safe_load(path.read_text())
    items = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(items, list):
        print(f"ERROR: {path} must contain a top-level 'servers:' list", file=sys.stderr)
        sys.exit(2)
    return [_interp(s) for s in items]


def find_server(servers: list[dict], server_id: str) -> dict:
    for s in servers:
        if s.get("id") == server_id:
            return s
    raise SystemExit(f"ERROR: server '{server_id}' not found in servers.yaml")


def resolve_base_url(server: dict) -> str:
    base = server.get("base_url") or ""
    suffix = server.get("base_url_suffix") or ""
    if not base:
        return ""
    return base.rstrip("/") + suffix


def client_credentials_token(token_url: str, client_id: str, client_secret: str, client: httpx.Client) -> str:
    resp = client.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def build_headers(server: dict, client: httpx.Client) -> dict[str, str]:
    """Assemble the full request-header dict for one server.

    Base set: FHIR Accept + Content-Type.
    Plus auth headers based on `auth.type`.
    Plus any server-specific `extra_headers` — per-server HTTP tuning knobs
    that FHIR vendors document (e.g. MS FHIR's `x-bundle-processing-logic:
    parallel` which doubles-ish their bundle ingest throughput per their own
    best-practices doc). These override any keys we set above.
    """
    auth = server.get("auth") or {}
    atype = auth.get("type", "none")
    headers = {"Accept": FHIR_CONTENT_TYPE, "Content-Type": FHIR_CONTENT_TYPE}

    if atype == "basic":
        import base64
        token = base64.b64encode(f"{auth['username']}:{auth['password']}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    elif atype == "bearer_static":
        headers["Authorization"] = f"Bearer {auth['token']}"
    elif atype == "client_credentials":
        token = client_credentials_token(
            auth["token_url"], auth["client_id"], auth["client_secret"], client
        )
        headers["Authorization"] = f"Bearer {token}"
    elif atype != "none":
        raise ValueError(f"unknown auth type: {atype}")

    extra = server.get("extra_headers") or {}
    if isinstance(extra, dict):
        headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


class AuthedSession:
    """httpx.Client wrapper that refreshes auth on 401 and retries once.

    Long-running load tests cross invisible token-expiry boundaries (e.g.
    Medplum's 1h client_credentials TTL). Minting headers once per worker and
    reusing them forever turns every post-expiry request into 401 noise that
    poisons p99 and throughput numbers. Refresh-on-401 makes every call site
    robust to any server's token lifetime without us having to know what it is.

    The retry cost is counted in the op's measured latency — from the caller's
    perspective, that IS how long the op took. If refresh-on-401 fires often,
    the fix is masking itself and the server config needs tuning.
    """

    def __init__(self, server: dict, client: httpx.Client) -> None:
        self._server = server
        self._client = client
        self._headers = build_headers(server, client)

    @property
    def headers(self) -> dict[str, str]:
        return self._headers

    def refresh(self) -> None:
        # Keep the old headers on mint failure. The retry will still 401 and
        # the caller records that as an op failure — better than exploding the
        # worker thread when the token endpoint is briefly unhappy.
        try:
            self._headers = build_headers(self._server, self._client)
        except Exception:
            pass

    def request(
        self, method: str, url: str, *, headers: dict | None = None, **kwargs,
    ) -> httpx.Response:
        final = {**self._headers, **(headers or {})}
        resp = self._client.request(method, url, headers=final, **kwargs)
        if resp.status_code == 401:
            self.refresh()
            final = {**self._headers, **(headers or {})}
            resp = self._client.request(method, url, headers=final, **kwargs)
        return resp

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs) -> httpx.Response:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs) -> httpx.Response:
        return self.request("DELETE", url, **kwargs)
