"""
Pure-Python TestScript runner. Reads FHIR R4 TestScript Resources, executes
them against a target server, and emits a TestReport-shaped JSON per script.

Built because AEGIS testscript-engine 's TestReport builder crashes against the
current `fhir_models` gem (FHIR::TestReport::TestScript::Action namespace gone).
We support the subset of TestScript surface our conformance suite needs:

  operation:
    type.code in {search, read, vread, capabilities, history, search-type,
                  $expand, $lookup, ... etc — falls back to free-form path}
    resource          (FHIR resource type, used when constructing the URL)
    params            (string appended to <base>/<resource>; supports leading
                       "/" to override path entirely, "?" or "&" for query)
    method            (overrides default GET)
    requestHeader[]   (extra headers as {field, value} pairs)
    responseId        (binds the parsed response Id for downstream assert paths)

  assert (one or more per action):
    responseCode + operator in {equals,notEquals,in,notIn}     (status code)
    headerField  + value + operator in {equals,contains}        (response header)
    contentType  + operator in {equals,contains,matchesMimeClass}
                  matchesMimeClass parses the actual MIME structurally (strips
                  parameters, lowercases, handles RFC 6839 +suffix grammar) and
                  matches against a bare class name. Example:
                    {"contentType": "json", "operator": "matchesMimeClass"}
                  matches application/json, application/json; charset=utf-8,
                  application/fhir+json — but NOT text/json or application/jsonp.
    responseBody + operator in {contains,notContains,equals,matchesRegex,...}
                  (raw body string; default operator is `contains` since whole-body
                  equality is rarely what tests want). `matchesRegex` is whitespace-
                  tolerant (DOTALL | IGNORECASE) so JSON-key probes survive
                  pretty-printed vs compact serialization differences.
    label, description, warningOnly                              (passthrough to TestReport)

The TestReport output mirrors the FHIR R4 TestReport shape closely enough that
`parse_report.py` reads it exactly the same way it would read AEGIS output.

Usage:
  python -m loadtest.conformance.runner \\
      --server hapi \\
      --testscripts conformance/testscripts/fhir-r4-base \\
      --testreport-dir results/conformance/<round>/<server>
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from _fhir_servers import (  # noqa: E402
    AuthedSession,
    build_headers,
    find_server,
    load_servers,
    resolve_base_url,
)


@dataclass
class ActionResult:
    kind: str                   # "operation" | "assert"
    label: str
    result: str                 # "pass" | "fail" | "error" | "skip"
    message: str = ""
    detail: dict | None = None  # raw operation echo (method/url/status)


@dataclass
class TestRunState:
    """Mutable carry-state across actions inside ONE TestScript test[]."""
    last_response: httpx.Response | None = None
    last_request: dict | None = None
    last_headers: dict[str, str] = field(default_factory=dict)


# ----------------------------------- URL ------------------------------------

def _server_root(base: str) -> str:
    """Strip the FHIR base path off a base_url, returning the bare server origin.
    `http://localhost:8080/fhir` -> `http://localhost:8080`.
    `http://localhost:8085`     -> `http://localhost:8085` (no-op).
    Used by `pathFromRoot: true` operations (e.g., probing whether SMART
    discovery is hosted at the OAuth issuer URL rather than the FHIR base).
    """
    from urllib.parse import urlparse
    p = urlparse(base)
    return f"{p.scheme}://{p.netloc}"


def build_url(base: str, op: dict) -> str:
    resource = op.get("resource") or ""
    params = op.get("params") or ""
    code = ((op.get("type") or {}).get("code") or "").lower()

    # `pathFromRoot: true` → ignore the FHIR base path and use the server origin.
    # Lets a TestScript probe well-known endpoints that some servers host at the
    # server root rather than below the FHIR base. Spec says <fhir-base>/.well-known
    # is canonical; root-hosting is a separate informational signal, not a fallback.
    effective_base = _server_root(base) if op.get("pathFromRoot") else base

    if code == "capabilities":
        return f"{effective_base}/metadata"
    if code == "history" and resource:
        return f"{effective_base}/{resource}/_history{params}"

    # Free-form: params can be a full path-and-query starting with "/", a query
    # starting with "?", or a path suffix. Keep the shape user-authored.
    if params.startswith("/"):
        path = f"/{resource}{params}" if resource else params
    elif params.startswith("?") or not params:
        path = f"/{resource}{params}" if resource else params
    else:
        path = f"/{resource}/{params}" if resource else f"/{params}"

    if not path.startswith("/"):
        path = "/" + path
    return f"{effective_base.rstrip('/')}{path}"


def resolve_method(op: dict) -> str:
    if op.get("method"):
        return str(op["method"]).upper()
    code = ((op.get("type") or {}).get("code") or "").lower()
    if code in {"create"}:
        return "POST"
    if code in {"update"}:
        return "PUT"
    if code in {"delete"}:
        return "DELETE"
    return "GET"


# --------------------------------- ASSERTS ----------------------------------

def _eval_in(actual: str, target: str) -> bool:
    return actual in {x.strip() for x in target.split(",")}


def _parse_mime(value: str) -> tuple[str, str, str]:
    """Strip parameters, lowercase, split type/subtype. Returns (type, subtype, suffix).
    Handles RFC 6839 suffix grammar: application/fhir+json -> ('application', 'fhir', 'json').
    application/json -> ('application', 'json', '').
    """
    bare = value.split(";", 1)[0].strip().lower()
    if "/" not in bare:
        return (bare, "", "")
    typ, subtype = bare.split("/", 1)
    if "+" in subtype:
        sub, suffix = subtype.rsplit("+", 1)
        return (typ, sub, suffix)
    return (typ, subtype, "")


def _matches_mime_class(actual: str, klass: str) -> bool:
    """True if actual MIME is `application/{klass}` or `application/*+{klass}`.
    klass is the bare class name like "json" or "xml" (no slash, no plus).
    """
    klass = klass.strip().lower()
    typ, subtype, suffix = _parse_mime(actual)
    if typ != "application":
        return False
    return subtype == klass or suffix == klass


def _describe_fail(field: str, actual: str, expected: str, operator: str) -> str:
    """Human-readable failure message. Reads naturally for negating operators
    (notIn, notEquals, notContains, notEmpty) where the old `actual op expected`
    shape looked tautological (e.g. `responseCode=404 operator=notEquals
    expected=404` — both sides match because that's precisely why it failed).
    """
    a = repr(actual)
    e = repr(expected)
    if operator in ("equals", ""):
        return f"{field}={a} did not equal expected {e}"
    if operator == "notEquals":
        return f"{field}={a} matched disallowed value {e}"
    if operator == "in":
        return f"{field}={a} not in allowed set {{{expected}}}"
    if operator == "notIn":
        return f"{field}={a} was in disallowed set {{{expected}}}"
    if operator == "contains":
        return f"{field}={a} did not contain substring {e}"
    if operator == "notContains":
        return f"{field}={a} contained disallowed substring {e}"
    if operator == "empty":
        return f"{field}={a} was not empty"
    if operator == "notEmpty":
        return f"{field} was empty (expected a value)"
    if operator == "matchesMimeClass":
        return f"{field}={a} is not of MIME class {e}"
    if operator == "matchesRegex":
        return f"{field}={a} did not match regex {e}"
    return f"{field}={a} operator={operator!r} expected={e}"


def _apply_operator(actual: str, expected: str, operator: str) -> bool:
    if operator == "equals" or operator == "":
        return actual == expected
    if operator == "notEquals":
        return actual != expected
    if operator == "in":
        return _eval_in(actual, expected)
    if operator == "notIn":
        return not _eval_in(actual, expected)
    if operator == "contains":
        return expected in actual
    if operator == "notContains":
        return expected not in actual
    if operator == "empty":
        return not actual
    if operator == "notEmpty":
        return bool(actual)
    if operator == "matchesMimeClass":
        return _matches_mime_class(actual, expected)
    if operator == "matchesRegex":
        # Body-friendly regex match: DOTALL so `.` crosses pretty-printed newlines,
        # IGNORECASE because FHIR JSON field names are fixed but URIs and codes
        # can vary in case across implementations.
        return re.search(expected, actual, re.DOTALL | re.IGNORECASE) is not None
    raise ValueError(f"unsupported assert operator: {operator}")


def evaluate_assert(spec: dict, state: TestRunState) -> ActionResult:
    label = spec.get("label") or spec.get("description") or "(unlabelled assert)"
    raw_operator = spec.get("operator")
    operator = raw_operator or "equals"

    if state.last_response is None:
        return ActionResult("assert", label, "error",
                            "no prior response to assert against")

    resp = state.last_response

    if "responseCode" in spec:
        actual = str(resp.status_code)
        expected = str(spec["responseCode"])
        ok = _apply_operator(actual, expected, operator)
        return _outcome(spec, label, ok,
                        _describe_fail("responseCode", actual, expected, operator))
    if "contentType" in spec:
        actual = resp.headers.get("content-type", "")
        expected = str(spec["contentType"])
        ok = _apply_operator(actual, expected, operator)
        return _outcome(spec, label, ok,
                        _describe_fail("contentType", actual, expected, operator))
    if "headerField" in spec:
        actual = resp.headers.get(spec["headerField"], "")
        expected = str(spec.get("value", ""))
        ok = _apply_operator(actual, expected, operator)
        return _outcome(spec, label, ok,
                        _describe_fail(f"header[{spec['headerField']!r}]",
                                       actual, expected, operator))
    if "responseBody" in spec:
        actual = resp.text or ""
        expected = str(spec["responseBody"])
        # Body asserts default to substring match; explicit operator wins.
        body_op = raw_operator or "contains"
        ok = _apply_operator(actual, expected, body_op)
        snippet = (actual[:120] + "…") if len(actual) > 120 else actual
        return _outcome(spec, label, ok,
                        _describe_fail("responseBody", snippet, expected, body_op))
    # Minimal responseBody.exists: pass if body is non-empty.
    if spec.get("response") == "okay":
        ok = 200 <= resp.status_code < 300
        return _outcome(spec, label, ok,
                        f"status={resp.status_code} expected 2xx")
    return ActionResult("assert", label, "skip",
                        f"unsupported assert spec: {sorted(spec.keys())}")


def _outcome(spec: dict, label: str, ok: bool, detail: str) -> ActionResult:
    if ok:
        return ActionResult("assert", label, "pass", "")
    if spec.get("warningOnly"):
        return ActionResult("assert", label, "skip", f"warningOnly: {detail}")
    return ActionResult("assert", label, "fail", detail)


# -------------------------------- OPERATION ---------------------------------

def execute_operation(op: dict, base_url: str, session: AuthedSession,
                      state: TestRunState) -> ActionResult:
    label = op.get("label") or op.get("description") or "(unlabelled op)"
    url = build_url(base_url, op)
    method = resolve_method(op)
    headers: dict[str, str] = {}
    for h in op.get("requestHeader") or []:
        if "field" in h and "value" in h:
            headers[str(h["field"])] = str(h["value"])
    body = None
    if "sourceId" in op:
        # Body fixtures not implemented — note as an error so suite design
        # surfaces it explicitly rather than silently succeeding.
        return ActionResult("operation", label, "error",
                            "fixture/sourceId bodies not implemented")

    state.last_request = {"method": method, "url": url, "headers": headers}
    try:
        resp = session.request(method, url, headers=headers, content=body)
    except httpx.HTTPError as e:
        state.last_response = None
        return ActionResult("operation", label, "error", f"{type(e).__name__}: {e}")
    state.last_response = resp
    return ActionResult(
        "operation", label, "pass",
        f"{method} {url} -> {resp.status_code}",
        detail={"method": method, "url": url, "status": resp.status_code},
    )


# ---------------------------------- TEST ------------------------------------

def run_test(test_block: dict, base_url: str, session: AuthedSession) -> dict:
    """Execute one TestScript.test[] block. Returns a TestReport.test[] entry."""
    state = TestRunState()
    actions_out: list[dict] = []
    for action in test_block.get("action", []):
        if "operation" in action:
            r = execute_operation(action["operation"], base_url, session, state)
            actions_out.append({"operation": _action_to_report(r)})
        elif "assert" in action:
            r = evaluate_assert(action["assert"], state)
            actions_out.append({"assert": _action_to_report(r)})
        else:
            actions_out.append({"assert": {
                "result": "skip", "message": "unknown action shape",
            }})
    return {
        "id": test_block.get("id", ""),
        "name": test_block.get("name", ""),
        "description": test_block.get("description", ""),
        "action": actions_out,
    }


def _action_to_report(r: ActionResult) -> dict:
    out: dict[str, Any] = {
        "result": r.result,
        "message": r.message,
        "label": r.label,
    }
    if r.detail:
        out["detail"] = r.detail
    return out


# --------------------------------- DRIVER -----------------------------------

def run_testscript(ts_path: pathlib.Path, base_url: str,
                   session: AuthedSession) -> dict:
    """Execute one TestScript file. Returns a TestReport-shaped dict."""
    script = json.loads(ts_path.read_text())
    started = dt.datetime.now(dt.timezone.utc)

    setup_actions: list[dict] = []
    for action in (script.get("setup") or {}).get("action") or []:
        # We don't support setup operations yet; skip+note.
        setup_actions.append({"operation": {"result": "skip",
                                            "message": "setup not implemented"}})

    test_blocks_out: list[dict] = []
    overall_pass = True
    overall_first_fail = ""
    for tb in script.get("test") or []:
        out = run_test(tb, base_url, session)
        test_blocks_out.append(out)
        for a in out["action"]:
            for kind, payload in a.items():
                if kind == "assert" and payload.get("result") not in {"pass", "skip"}:
                    overall_pass = False
                    if not overall_first_fail:
                        overall_first_fail = payload.get("message") or payload.get("label", "")

    finished = dt.datetime.now(dt.timezone.utc)
    return {
        "resourceType": "TestReport",
        "name": script.get("name") or script.get("id") or ts_path.stem,
        "status": "completed",
        "result": "pass" if overall_pass else "fail",
        "score": 100.0 if overall_pass else 0.0,
        "issued": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms": int((finished - started).total_seconds() * 1000),
        "testScript": {
            "reference": str(ts_path.relative_to(REPO_ROOT)),
            "display": script.get("title") or script.get("name") or ts_path.stem,
        },
        "setup": {"action": setup_actions} if setup_actions else None,
        "test": test_blocks_out,
        "_runner": {
            "engine": "mock-health-py-testscript-runner",
            "engine_version": "0.1.0",
        },
        "_first_failure": overall_first_fail,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--server", required=True, help="server id from servers.yaml")
    p.add_argument("--testscripts", required=True,
                   help="path to a TestScript JSON file or directory of them")
    p.add_argument("--testreport-dir", required=True,
                   help="output directory for TestReport JSON files")
    args = p.parse_args()

    servers = load_servers(REPO_ROOT / "servers.yaml")
    server = find_server(servers, args.server)
    base_url = resolve_base_url(server)
    if not base_url:
        raise SystemExit(f"server {args.server!r} has empty base_url; check env vars")

    out_dir = pathlib.Path(args.testreport_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ts_arg = pathlib.Path(args.testscripts).resolve()
    if ts_arg.is_file():
        scripts = [ts_arg]
    else:
        scripts = sorted(ts_arg.rglob("*.json"))
    if not scripts:
        raise SystemExit(f"no TestScript files at {ts_arg}")

    timeout = httpx.Timeout(60.0, connect=10.0)
    with httpx.Client(timeout=timeout) as client:
        session = AuthedSession(server, client)
        for ts_path in scripts:
            try:
                report = run_testscript(ts_path, base_url, session)
            except Exception as e:
                report = {
                    "resourceType": "TestReport",
                    "name": ts_path.stem,
                    "status": "completed",
                    "result": "fail",
                    "issued": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "testScript": {"reference": str(ts_path.relative_to(REPO_ROOT))},
                    "test": [],
                    "_runner": {"engine": "mock-health-py-testscript-runner",
                                "engine_version": "0.1.0"},
                    "_first_failure": f"runner exception: {type(e).__name__}: {e}",
                }
            out_path = out_dir / f"{ts_path.stem}.testreport.json"
            out_path.write_text(json.dumps(report, indent=2) + "\n")
            verdict = report.get("result", "?")
            print(f"  [{verdict}] {ts_path.name} -> {out_path.name}", file=sys.stderr)

    print(f"[ok] {len(scripts)} reports in {out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
