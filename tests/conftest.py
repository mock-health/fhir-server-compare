"""Shared pytest fixtures.

The fhirbench package is intentionally non-public-API — these tests cover the
load-bearing pure functions (server discovery, round-artifact aggregation,
TestScript runner primitives) so a refactor or contributor change can't
silently break them.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def sample_servers() -> list[dict]:
    """Minimal in-memory servers.yaml-shaped roster for unit tests."""
    return [
        {
            "id": "hapi",
            "label": "HAPI",
            "base_url": "http://localhost:8080/fhir",
            "version": "8.0.0",
            "auth": {"type": "none"},
        },
        {
            "id": "aidbox",
            "label": "Aidbox",
            "base_url": "http://localhost:8888",
            "version": "2603",
            "auth": {"type": "basic", "client_id": "x", "client_secret": "y"},
        },
        {
            "id": "medplum",
            "label": "Medplum",
            "base_url": "http://localhost:8103/fhir/R4",
            "version": "5.1.8",
            "auth": {
                "type": "client_credentials",
                "token_url": "http://localhost:8103/oauth2/token",
                "client_id": "${MEDPLUM_CLIENT_ID}",
                "client_secret": "${MEDPLUM_CLIENT_SECRET:-fallback}",
            },
        },
    ]
