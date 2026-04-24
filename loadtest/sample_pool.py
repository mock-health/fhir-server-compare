"""Runtime parameter sampling for templated search queries.

Harvests small, bounded lists of realistic FHIR values from the target
server before the timed search workload starts — Patient ids, patient
family/given names, Condition/Procedure/MedicationRequest codes, and
Practitioner/Location ids. Each sampled query in queries.yaml declares
which pools its `{{placeholder}}` references draw from, and the worker
substitutes a fresh random value per request at execute time.

Why sample at runtime instead of hardcoding values:
  - Hardcoding `family=Smith` works via prefix against the Synthea corpus
    (Smith123, Smith456, ...), but measuring what the server does against
    real surnames — drawn from live data — is more honest.
  - Drawing a fresh value per request exposes cache-miss cost that a
    5-patient hot set would hide.
  - Portable across datasets — someone loading their own bundle doesn't
    have to edit the YAML.
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _fhir_servers import AuthedSession  # noqa: E402

MAX_PER_POOL = 500
PAGINATE_COUNT = 200
MAX_NEXT_URL = 8192  # Same defensive cap as harvest_patient_ids (HFS quirk).
PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _paginate(
    session: AuthedSession,
    base_url: str,
    resource_type: str,
    elements: str,
    max_resources: int,
) -> list[dict]:
    """Pull up to `max_resources` resources via paginated search with _elements.

    Returns raw resources; caller extracts fields. Stops on non-2xx, empty
    page, missing next link, or next-url > 8 KB.
    """
    out: list[dict] = []
    url: str | None = (
        f"{base_url}/{resource_type}?_count={PAGINATE_COUNT}&_elements={elements}"
    )
    while url and len(out) < max_resources:
        try:
            resp = session.get(url, timeout=30.0)
        except httpx.RequestError:
            break
        if not (200 <= resp.status_code < 300):
            break
        try:
            body = resp.json()
        except Exception:
            break
        for e in body.get("entry") or []:
            res = e.get("resource") or {}
            if res:
                out.append(res)
                if len(out) >= max_resources:
                    break
        next_url = None
        for link in body.get("link") or []:
            if link.get("relation") == "next":
                next_url = link.get("url")
                break
        if not next_url:
            break
        cand = urljoin(url, next_url)
        if len(cand) > MAX_NEXT_URL:
            break
        url = cand
    return out


def _codeable_concept_token(cc: dict | None) -> str | None:
    """Extract the first 'system|code' token from a CodeableConcept."""
    if not cc:
        return None
    for c in cc.get("coding") or []:
        system = c.get("system")
        code = c.get("code")
        if system and code:
            return f"{system}|{code}"
    return None


def _harvest_patient_names(session: AuthedSession, base_url: str, field: str) -> list[str]:
    """Harvest up to MAX_PER_POOL unique family or given names."""
    seen: set[str] = set()
    for res in _paginate(session, base_url, "Patient", "name", MAX_PER_POOL * 4):
        for n in res.get("name") or []:
            if field == "family":
                v = n.get("family")
                if isinstance(v, str) and v:
                    seen.add(v)
            elif field == "given":
                g = n.get("given") or []
                if g and isinstance(g[0], str) and g[0]:
                    seen.add(g[0])
            break
        if len(seen) >= MAX_PER_POOL:
            break
    return sorted(seen)[:MAX_PER_POOL]


def _harvest_tokens(
    session: AuthedSession, base_url: str, resource_type: str, field: str,
) -> list[str]:
    """Harvest up to MAX_PER_POOL unique 'system|code' tokens from a field."""
    seen: set[str] = set()
    for res in _paginate(session, base_url, resource_type, field, MAX_PER_POOL * 8):
        tok = _codeable_concept_token(res.get(field))
        if tok:
            seen.add(tok)
        if len(seen) >= MAX_PER_POOL:
            break
    return sorted(seen)[:MAX_PER_POOL]


def _harvest_ids(
    session: AuthedSession, base_url: str, resource_type: str,
) -> list[str]:
    """Harvest up to MAX_PER_POOL resource ids."""
    out: list[str] = []
    for res in _paginate(session, base_url, resource_type, "id", MAX_PER_POOL):
        rid = res.get("id")
        if rid:
            out.append(rid)
    return out


class SamplePool:
    """Named pools of values harvested from the target server."""

    def __init__(self) -> None:
        self.pools: dict[str, list[str]] = {}

    def load(
        self,
        session: AuthedSession,
        base_url: str,
        patient_ids: list[str] | None = None,
    ) -> None:
        """Harvest all pools. Pass pre-harvested patient_ids to reuse them."""
        t0 = time.monotonic()
        if patient_ids is not None:
            self.pools["patient_id"] = list(patient_ids)
        else:
            # Defer the import: workload_crud imports this module in some
            # orchestration paths, and we want to avoid a cycle.
            from loadtest.workload_crud import harvest_patient_ids
            self.pools["patient_id"] = harvest_patient_ids(session, base_url)

        self.pools["patient_family"] = _harvest_patient_names(session, base_url, "family")
        self.pools["patient_given"] = _harvest_patient_names(session, base_url, "given")
        self.pools["condition_code"] = _harvest_tokens(session, base_url, "Condition", "code")
        self.pools["procedure_code"] = _harvest_tokens(session, base_url, "Procedure", "code")
        self.pools["medication_code"] = _harvest_tokens(
            session, base_url, "MedicationRequest", "medicationCodeableConcept",
        )
        self.pools["practitioner_id"] = _harvest_ids(session, base_url, "Practitioner")
        self.pools["location_id"] = _harvest_ids(session, base_url, "Location")

        dt = time.monotonic() - t0
        summary = ", ".join(f"{k}={len(v)}" for k, v in self.pools.items())
        print(f"[sample_pool] harvested in {dt:.1f}s: {summary}")

    def has(self, pool_name: str) -> bool:
        return bool(self.pools.get(pool_name))

    def missing_for(self, query: dict) -> list[str]:
        """Return pool names this query needs but has no values for."""
        mapping = query.get("sample") or {}
        return [pool for pool in mapping.values() if not self.has(pool)]

    def expand(self, query: dict, rng) -> dict:
        """Return a copy of query with `{{placeholders}}` substituted.

        A placeholder appearing multiple times in the same query resolves
        once — e.g., `{{patient_id}}` in both path and a param gets the
        same id — so the request is coherent.
        """
        mapping = query.get("sample") or {}
        resolved: dict[str, str] = {}

        def substitute(s: str) -> str:
            def repl(m: re.Match[str]) -> str:
                name = m.group(1)
                if name in resolved:
                    return resolved[name]
                pool_name = mapping.get(name)
                if not pool_name:
                    return m.group(0)
                pool = self.pools.get(pool_name) or []
                if not pool:
                    return m.group(0)
                v = rng.choice(pool)
                resolved[name] = v
                return v
            return PLACEHOLDER_RE.sub(repl, s)

        out = dict(query)
        if isinstance(query.get("path"), str):
            out["path"] = substitute(query["path"])
        params = query.get("params") or {}
        new_params: dict = {}
        for k, v in params.items():
            new_params[k] = substitute(v) if isinstance(v, str) else v
        out["params"] = new_params
        return out
