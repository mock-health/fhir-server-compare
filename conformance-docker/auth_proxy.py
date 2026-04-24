"""
Tiny HTTP reverse proxy that injects an Authorization header before forwarding
to an upstream FHIR server. Sidecar for the AEGIS testscript-engine, which has
no first-class auth support (its `fhir_client` is instantiated bare).

Configured by env vars (one container per authenticated server in roster):

  AUTH_TYPE          basic | client_credentials
  UPSTREAM_URL       e.g. http://aidbox:8080/fhir   (no trailing slash)
  LISTEN_PORT        default 8000
  AUTH_USER          required when AUTH_TYPE=basic
  AUTH_PASS          required when AUTH_TYPE=basic
  AUTH_TOKEN_URL     required when AUTH_TYPE=client_credentials
  AUTH_CLIENT_ID     required when AUTH_TYPE=client_credentials
  AUTH_CLIENT_SECRET required when AUTH_TYPE=client_credentials

Refreshes OAuth2 tokens before expiry. Pure stdlib + httpx.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from typing import Optional

import httpx
from aiohttp import web


UPSTREAM_URL = os.environ["UPSTREAM_URL"].rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "8000"))
AUTH_TYPE = os.environ["AUTH_TYPE"]

_token_cache: dict[str, object] = {"value": None, "expires_at": 0.0}


def _basic_header() -> str:
    user = os.environ["AUTH_USER"]
    pw = os.environ["AUTH_PASS"]
    encoded = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return f"Basic {encoded}"


async def _client_credentials_header() -> str:
    now = time.time()
    if _token_cache["value"] and float(_token_cache["expires_at"]) - 30 > now:
        return f"Bearer {_token_cache['value']}"
    token_url = os.environ["AUTH_TOKEN_URL"]
    client_id = os.environ["AUTH_CLIENT_ID"]
    client_secret = os.environ["AUTH_CLIENT_SECRET"]
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )
        r.raise_for_status()
        body = r.json()
    token = body["access_token"]
    expires_in = float(body.get("expires_in", 300))
    _token_cache["value"] = token
    _token_cache["expires_at"] = now + expires_in
    return f"Bearer {token}"


async def _auth_header() -> str:
    if AUTH_TYPE == "basic":
        return _basic_header()
    if AUTH_TYPE == "client_credentials":
        return await _client_credentials_header()
    raise RuntimeError(f"unsupported AUTH_TYPE={AUTH_TYPE!r}")


# httpx connection pool reused across requests. AEGIS keeps few connections open
# concurrently (one TestScript at a time), so a single AsyncClient is plenty.
_HTTP: Optional[httpx.AsyncClient] = None


async def _http() -> httpx.AsyncClient:
    global _HTTP
    if _HTTP is None:
        _HTTP = httpx.AsyncClient(timeout=120.0, follow_redirects=False)
    return _HTTP


HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


async def proxy(request: web.Request) -> web.StreamResponse:
    target = f"{UPSTREAM_URL}{request.rel_url}"
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
    }
    headers["Authorization"] = await _auth_header()
    body = await request.read() if request.body_exists else None
    client = await _http()
    upstream = await client.request(
        request.method, target, headers=headers, content=body,
    )
    out = web.StreamResponse(
        status=upstream.status_code,
        headers={
            k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
        },
    )
    await out.prepare(request)
    await out.write(upstream.content)
    await out.write_eof()
    return out


async def _on_cleanup(_app: web.Application) -> None:
    global _HTTP
    if _HTTP is not None:
        await _HTTP.aclose()
        _HTTP = None


def main() -> None:
    app = web.Application(client_max_size=200 * 1024 * 1024)
    app.router.add_route("*", "/{path:.*}", proxy)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host="0.0.0.0", port=LISTEN_PORT, print=None)


if __name__ == "__main__":
    main()
