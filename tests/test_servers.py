"""Unit tests for fhirbench.servers — config loading + auth helpers."""
from __future__ import annotations

import pytest

from fhirbench import servers


def test_find_server_returns_match(sample_servers):
    assert servers.find_server(sample_servers, "hapi")["id"] == "hapi"
    assert servers.find_server(sample_servers, "medplum")["label"] == "Medplum"


def test_find_server_raises_on_unknown(sample_servers):
    with pytest.raises(SystemExit):
        servers.find_server(sample_servers, "nonexistent")


def test_resolve_base_url_simple(sample_servers):
    hapi = servers.find_server(sample_servers, "hapi")
    assert servers.resolve_base_url(hapi) == "http://localhost:8080/fhir"


def test_resolve_base_url_strips_trailing_slash():
    server = {"id": "x", "base_url": "http://example.com/fhir/"}
    assert servers.resolve_base_url(server) == "http://example.com/fhir"


def test_resolve_base_url_appends_suffix():
    server = {
        "id": "x",
        "base_url": "http://example.com",
        "base_url_suffix": "/fhir/R4",
    }
    assert servers.resolve_base_url(server) == "http://example.com/fhir/R4"


def test_resolve_base_url_empty_returns_empty():
    assert servers.resolve_base_url({"id": "x"}) == ""


def test_env_var_regex_matches_required_form():
    """${VAR} → group 1 is VAR, group 2 is None."""
    m = servers.ENV_VAR_RE.search("${MY_VAR}")
    assert m is not None
    assert m.group(1) == "MY_VAR"
    assert m.group(2) is None


def test_env_var_regex_matches_default_form():
    """${VAR:-default} → group 1 is VAR, group 2 is the default."""
    m = servers.ENV_VAR_RE.search("${MY_VAR:-fallback}")
    assert m is not None
    assert m.group(1) == "MY_VAR"
    assert m.group(2) == "fallback"


def test_env_var_regex_handles_empty_default():
    m = servers.ENV_VAR_RE.search("${MY_VAR:-}")
    assert m is not None
    assert m.group(2) == ""


def test_load_servers_resolves_real_yaml(tmp_path):
    """load_servers reads a YAML file, applies env-var substitution, returns a list."""
    yaml_text = """\
servers:
  - id: a
    base_url: http://localhost:8080
    auth:
      type: none
  - id: b
    base_url: ${TEST_OVERRIDE_BASE:-http://default.example.com}
    auth:
      type: none
"""
    p = tmp_path / "servers.yaml"
    p.write_text(yaml_text)
    out = servers.load_servers(p)
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["id"] == "a"
    # Default applied when env var unset
    assert out[1]["base_url"] == "http://default.example.com"


def test_load_servers_applies_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_OVERRIDE_BASE", "http://override.example.com:9000")
    yaml_text = """\
servers:
  - id: a
    base_url: ${TEST_OVERRIDE_BASE:-http://default.example.com}
    auth:
      type: none
"""
    p = tmp_path / "servers.yaml"
    p.write_text(yaml_text)
    out = servers.load_servers(p)
    assert out[0]["base_url"] == "http://override.example.com:9000"
