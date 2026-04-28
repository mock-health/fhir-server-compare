"""Unit tests for fhirbench.conformance.runner — TestScript primitives."""
from __future__ import annotations

import pytest

from fhirbench.conformance import runner


def test_resolve_method_uses_explicit_method():
    """Explicit `method` field wins over inferred type code."""
    op = {"method": "POST", "url": "/Patient"}
    assert runner.resolve_method(op) == "POST"


def test_resolve_method_uppercases():
    op = {"method": "get", "url": "/Patient"}
    assert runner.resolve_method(op) == "GET"


def test_resolve_method_infers_from_type_code():
    """When no explicit method, infer from type.code (FHIR TestScript convention)."""
    assert runner.resolve_method({"type": {"code": "create"}}) == "POST"
    assert runner.resolve_method({"type": {"code": "update"}}) == "PUT"
    assert runner.resolve_method({"type": {"code": "delete"}}) == "DELETE"


def test_resolve_method_defaults_to_get():
    """No method, no type code → GET (the FHIR read default)."""
    assert runner.resolve_method({}) == "GET"
    assert runner.resolve_method({"type": {"code": "read"}}) == "GET"
    assert runner.resolve_method({"type": {"code": "search-type"}}) == "GET"


def test_build_url_capabilities():
    """type.code 'capabilities' → /metadata."""
    op = {"type": {"code": "capabilities"}}
    assert runner.build_url("http://x.example/fhir", op) == "http://x.example/fhir/metadata"


def test_build_url_history_with_resource():
    op = {"type": {"code": "history"}, "resource": "Patient"}
    assert runner.build_url("http://x.example/fhir", op).endswith("/Patient/_history")


def test_build_url_resource_only():
    """resource without params → /<base>/<resource>."""
    op = {"resource": "Patient"}
    out = runner.build_url("http://x.example/fhir", op)
    assert out == "http://x.example/fhir/Patient"


def test_build_url_with_query_params():
    """params starting with '?' attach to the resource."""
    op = {"resource": "Patient", "params": "?_count=10"}
    out = runner.build_url("http://x.example/fhir", op)
    assert out == "http://x.example/fhir/Patient?_count=10"


def test_build_url_strips_trailing_slash_from_base():
    """Base with trailing slash shouldn't produce double slashes in the path."""
    op = {"resource": "Patient"}
    out = runner.build_url("http://x.example/fhir/", op)
    assert "//Patient" not in out


def test_test_run_state_dataclass():
    """TestRunState carries response/request state across actions in one test."""
    state = runner.TestRunState()
    assert state.last_response is None
    assert state.last_request is None
    assert state.last_headers == {}
