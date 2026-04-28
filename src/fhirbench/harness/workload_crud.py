#!/usr/bin/env python3
"""CRUD workload driver — mixed single-resource ops under concurrency.

After Stage ingest, harvests a random set of Patient IDs from the target
server, then fires C/R/U/D ops from a configurable-weight pool for a fixed
duration with N workers.

Default mix: 10% C (create Observation), 60% R (read Patient), 25% U (put
Patient), 5% D (delete created Observation). Matches the "moderate read-heavy
app" default in the plan; override with --mix.

Each op's timing and status ends up in the OpLog JSONL file so the report
stage can compute p50/p95/p99 per verb.

Design notes:
- Creates push ids into a bounded deque; Deletes pop from it. When the pool
  is empty, a D falls back to an R (logged as verb='R:fallback_from_D').
- Updates do a naive PUT of a patched copy of the resource fetched right
  before (one GET + one PUT counted as a single U op, measured end-to-end).
  Etag/If-Match not required: MS FHIR accepts unconditional PUT in dev mode;
  HAPI/Aidbox/Medplum all accept unconditional PUT by default.
- On 5xx or network error, the op is logged but the worker keeps going.
- Random choice is process-local random; threads don't synchronize on the
  RNG since a uniform distribution is what we want, not determinism.

Usage:
    python -m fhirbench.harness.workload_crud \\
        --server hapi --log results/hapi-crud.jsonl \\
        --duration 900 --workers 64
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from fhirbench.servers import AuthedSession, find_server, load_servers, resolve_base_url  # noqa: E402
from fhirbench.harness.metrics import OpLog, OpRecord  # noqa: E402
from fhirbench.harness.update_templates import TEMPLATES, TEMPLATE_IDS, pick_template  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SERVERS = REPO_ROOT / "config" / "servers.yaml"

# Minimal, spec-valid Observation template. `subject` is filled at runtime.
OBS_TEMPLATE = {
    "resourceType": "Observation",
    "status": "final",
    "code": {
        "coding": [{
            "system": "http://loinc.org",
            "code": "8310-5",
            "display": "Body temperature",
        }],
        "text": "Body temperature",
    },
    "valueQuantity": {"value": 37.0, "unit": "C", "system": "http://unitsofmeasure.org", "code": "Cel"},
}


def harvest_patient_ids(
    session: AuthedSession, base_url: str, target: int | None = None,
) -> list[str]:
    """Pull Patient ids via paginated search. `target=None` = harvest ALL.

    Default is unbounded (every patient in the store) so the CRUD workload
    samples uniformly across the entire dataset rather than thrashing a hot
    set of N patients. At 131K patients the harvest is ~655 paginated GETs
    with `_elements=id` (small response bodies); takes ~30s on a fast server,
    happens BEFORE the timed workload, and is not counted in measurements.

    Why uniform-over-all is the right default: a cache-friendly hot-set
    workload would let servers with strong row caches (Aidbox, Medplum) post
    artificially low p99s. Reading across the whole dataset forces real
    cache-miss rates and is harder to game. The CLI accepts --harvest-target
    if you want to switch back to a bounded hot-set for a "active-user"
    sub-experiment.

    Aidbox (and some other servers) return relative `link.next` URLs like
    `Patient?_count=200&page=2` with no scheme/host. `urljoin` promotes those
    against the last-fetched absolute URL; servers that return fully-qualified
    next links pass through unchanged.
    """
    ids: list[str] = []
    url: str | None = f"{base_url}/Patient?_count=200&_elements=id"
    # Defensive cap on next-page URL length. Some servers ship pagination
    # bugs that concatenate cursors across pages — the URL grows on every
    # request and eventually busts httpx's 65 KB hard limit. Bailing at
    # 8 KB is well below any real server's practical pagination URL and
    # still leaves 200-400 harvested ids for the CRUD workload to run on.
    MAX_NEXT_URL = 8192
    # Harvest runs right after the search phase, which can leave the server
    # under heavy load; the first page against Aidbox @64K has been observed
    # to take >60s end-to-end. Bumping the per-page timeout and adding two
    # retries on ReadTimeout keeps a transient stall from aborting the whole
    # CRUD phase.
    PAGE_TIMEOUT = 300.0
    PAGE_RETRIES = 2
    while url:
        if target is not None and len(ids) >= target:
            break
        resp = None
        for attempt in range(PAGE_RETRIES + 1):
            try:
                resp = session.get(url, timeout=PAGE_TIMEOUT)
                break
            except httpx.ReadTimeout:
                if attempt == PAGE_RETRIES:
                    print(f"  [harvest] ReadTimeout after {PAGE_RETRIES + 1} "
                          f"attempts at {len(ids)} ids; stopping pagination")
                    return ids[:target] if target is not None else ids
                backoff = 5.0 * (attempt + 1)
                print(f"  [harvest] ReadTimeout on page (attempt {attempt + 1}); "
                      f"retrying in {backoff:.0f}s")
                time.sleep(backoff)
        assert resp is not None
        if not (200 <= resp.status_code < 300):
            break
        try:
            body = resp.json()
        except Exception:
            break
        for e in body.get("entry") or []:
            res = e.get("resource") or {}
            rid = res.get("id")
            if rid:
                ids.append(rid)
        next_url: str | None = None
        for link in body.get("link") or []:
            if link.get("relation") == "next":
                next_url = link.get("url")
                break
        if next_url is None:
            break
        candidate = urljoin(url, next_url)
        if len(candidate) > MAX_NEXT_URL:
            print(
                f"  [harvest] stopping pagination: next-url exceeds "
                f"{MAX_NEXT_URL}B ({len(candidate)}B), likely a server-side "
                f"cursor bug. Harvested {len(ids)} ids so far."
            )
            break
        url = candidate
    return ids[:target] if target is not None else ids


def parse_mix(spec: str) -> dict[str, float]:
    """'C:10,R:60,U:25,D:5' -> {'C':10, 'R':60, 'U':25, 'D':5}."""
    out: dict[str, float] = {}
    for part in spec.split(","):
        k, _, v = part.partition(":")
        out[k.strip().upper()] = float(v.strip())
    total = sum(out.values()) or 1
    return {k: v / total for k, v in out.items()}


def weighted_choice(weights: dict[str, float], rng: random.Random) -> str:
    r = rng.random()
    acc = 0.0
    for k, w in weights.items():
        acc += w
        if r <= acc:
            return k
    return list(weights.keys())[-1]


def do_read(session: AuthedSession, base_url: str, pid: str) -> tuple[int, int, bool]:
    t0 = time.monotonic()
    try:
        resp = session.get(f"{base_url}/Patient/{pid}", timeout=30.0)
        ms = int((time.monotonic() - t0) * 1000)
        return ms, resp.status_code, 200 <= resp.status_code < 300
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False


def do_read_obs(session: AuthedSession, base_url: str, obs_id: str) -> tuple[int, int, bool]:
    """Read-your-own-write: GET an Observation id that C just produced.

    Distinct from search's patient_read_by_id verb because it measures
    write → single-resource-read propagation latency. Some servers
    retrieve newly-written resources by id faster than they become
    searchable (asynchronous indexers, e.g. Medplum, MS FHIR).
    """
    t0 = time.monotonic()
    try:
        resp = session.get(f"{base_url}/Observation/{obs_id}", timeout=30.0)
        ms = int((time.monotonic() - t0) * 1000)
        return ms, resp.status_code, 200 <= resp.status_code < 300
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False


def do_create(session: AuthedSession, base_url: str, pid: str) -> tuple[int, int, bool, str | None]:
    body = dict(OBS_TEMPLATE)
    body["subject"] = {"reference": f"Patient/{pid}"}
    t0 = time.monotonic()
    try:
        resp = session.post(f"{base_url}/Observation", json=body, timeout=30.0)
        ms = int((time.monotonic() - t0) * 1000)
        ok = 200 <= resp.status_code < 300
        new_id = None
        if ok:
            try:
                new_id = resp.json().get("id")
            except Exception:
                pass
        return ms, resp.status_code, ok, new_id
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False, None


def do_update(
    session: AuthedSession, base_url: str, pid: str, rng: random.Random,
    *, template_id: str | None = None,
) -> tuple[int, int, bool, str, str | None]:
    """Read-modify-write counted as a single op (two HTTP calls).

    The modify step picks one of six templates uniformly (see
    fhirbench.harness.update_templates). Half touch search indexes
    (name_given / address_city / active_toggle), half don't
    (meta_tag / meta_security / telecom_phone), so per-template p99
    slices surface servers that reindex on field edits.

    Returns (duration_ms, status_code, ok, template_id, prev_version_id).
    `prev_version_id` is the Patient's `meta.versionId` at the time of
    the initial GET — i.e., the version that just became historical after
    the PUT. Feeds the V (vread) phase's sample pool. May be None if the
    server omits versionId from the resource body.

    If `template_id` is provided, that template is used (for prewarm);
    otherwise pick_template draws uniformly.
    """
    t0 = time.monotonic()
    if template_id is not None:
        tid = template_id
        fn = TEMPLATES[tid]
    else:
        tid, fn = pick_template(rng)
    try:
        g = session.get(f"{base_url}/Patient/{pid}", timeout=30.0)
        if not (200 <= g.status_code < 300):
            return int((time.monotonic() - t0) * 1000), g.status_code, False, tid, None
        patient = g.json()
        prev_version = (patient.get("meta") or {}).get("versionId")
        patient = fn(patient, rng)
        p = session.put(f"{base_url}/Patient/{pid}", json=patient, timeout=30.0)
        ms = int((time.monotonic() - t0) * 1000)
        ok = 200 <= p.status_code < 300
        return ms, p.status_code, ok, tid, (prev_version if ok else None)
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False, tid, None


def do_vread(
    session: AuthedSession, base_url: str, resource_type: str,
    rid: str, version: str,
) -> tuple[int, int, bool]:
    """Versioned read: GET /{resource_type}/{id}/_history/{versionId}.

    Exercises the version-store path (distinct from the current-version
    lookup that patient_read_by_id in search exercises). On servers with
    compressed version history (Blaze) or snapshot-plus-diff backends
    (Aidbox), vread latency can diverge from current-read latency even
    for a recently-updated record.
    """
    t0 = time.monotonic()
    try:
        resp = session.get(
            f"{base_url}/{resource_type}/{rid}/_history/{version}", timeout=30.0,
        )
        ms = int((time.monotonic() - t0) * 1000)
        return ms, resp.status_code, 200 <= resp.status_code < 300
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False


def do_delete(session: AuthedSession, base_url: str, obs_id: str) -> tuple[int, int, bool]:
    t0 = time.monotonic()
    try:
        resp = session.delete(f"{base_url}/Observation/{obs_id}", timeout=30.0)
        ms = int((time.monotonic() - t0) * 1000)
        # Some servers return 204 No Content, some 200, some 404 if the id
        # was already collected — count 2xx OR 404 as "gone".
        ok = (200 <= resp.status_code < 300) or resp.status_code == 404
        return ms, resp.status_code, ok
    except httpx.RequestError:
        return int((time.monotonic() - t0) * 1000), 0, False


# --- phased mode ---------------------------------------------------------
#
# Replaces the weighted-mix single-phase run with four back-to-back timed
# phases (C -> U -> R -> D), each capped at min(sample_cap, duration_cap).
# Under the mix, fast servers starved C and D (Blaze D=2.7K samples at
# 16K) which made per-verb p99 unreliable. Per-phase caps produce even
# samples across verbs and the slow ones self-flag via stop_reason='time'.

PHASE_ORDER: tuple[str, ...] = ("C", "U", "R", "V", "D")


@dataclass
class CrudContext:
    server_id: str
    base_url: str
    server: dict
    pids: list[str]
    created_pool: "deque[str]"
    created_lock: threading.Lock
    # (patient_id, prev_version_id) tuples harvested from U phase.
    # V phase samples from here (non-draining). deque because U can produce
    # 50K+ entries and we want bounded memory, not because order matters.
    updated_versions: "deque[tuple[str, str]]"
    updated_versions_lock: threading.Lock
    log: OpLog


def prepare_context(
    server_id: str, servers_path: Path, log_path: Path,
    harvest_target: int | None = None,
) -> CrudContext | None:
    servers = load_servers(servers_path)
    server = find_server(servers, server_id)
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{server_id}' has no base_url configured", file=sys.stderr)
        return None

    t_harvest = time.monotonic()
    # Client-level timeout must be >= harvest_patient_ids per-page timeout
    # (300s) or the floor here silently clips it.
    with httpx.Client(timeout=300.0) as bootstrap_client:
        session = AuthedSession(server, bootstrap_client)
        pids = harvest_patient_ids(session, base_url, target=harvest_target)
    if not pids:
        print("ERROR: could not harvest any Patient ids. Did ingest run?", file=sys.stderr)
        return None
    cap_str = "unbounded" if harvest_target is None else f"capped at {harvest_target}"
    print(f"  harvested {len(pids):,} patient ids ({cap_str}) in "
          f"{time.monotonic() - t_harvest:.1f}s for the workload pool")

    return CrudContext(
        server_id=server_id,
        base_url=base_url,
        server=server,
        pids=pids,
        created_pool=deque(maxlen=100_000),
        created_lock=threading.Lock(),
        updated_versions=deque(maxlen=200_000),
        updated_versions_lock=threading.Lock(),
        log=OpLog(log_path),
    )


def run_verb_phase(
    ctx: CrudContext, verb: str, workers: int,
    sample_cap: int, duration_cap: float,
) -> dict:
    """Run a single-verb phase until sample_cap or duration_cap is hit.

    Returns a summary dict with planned_cap, samples, elapsed_ms, stop_reason,
    and (for U) a per-template histogram.
    """
    assert verb in PHASE_ORDER, verb
    stop_at = time.monotonic() + duration_cap
    counter_lock = threading.Lock()
    sample_count = 0
    pool_empty_flag = threading.Event()
    template_hist: dict[str, int] = {}
    template_hist_lock = threading.Lock()

    def _inc() -> int:
        nonlocal sample_count
        with counter_lock:
            sample_count += 1
            return sample_count

    def worker(wid: int) -> None:
        rng = random.Random(wid * 6367 + int(time.time() * 1000))
        with httpx.Client(
            timeout=60.0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:
            s = AuthedSession(ctx.server, client)
            while True:
                if time.monotonic() >= stop_at:
                    return
                with counter_lock:
                    if sample_count >= sample_cap:
                        return
                started = time.time()
                if verb == "C":
                    pid = rng.choice(ctx.pids)
                    ms, code, ok, new_id = do_create(s, ctx.base_url, pid)
                    ctx.log.record(OpRecord("crud", "C", started, ms, code, ok))
                    if ok and new_id:
                        with ctx.created_lock:
                            ctx.created_pool.append(new_id)
                    _inc()
                elif verb == "U":
                    pid = rng.choice(ctx.pids)
                    ms, code, ok, tid, prev_ver = do_update(s, ctx.base_url, pid, rng)
                    ctx.log.record(OpRecord("crud", "U", started, ms, code, ok, note=tid))
                    with template_hist_lock:
                        template_hist[tid] = template_hist.get(tid, 0) + 1
                    if ok and prev_ver:
                        with ctx.updated_versions_lock:
                            ctx.updated_versions.append((pid, prev_ver))
                    _inc()
                elif verb == "R":
                    # Read-your-own-write: pick (don't pop) a C'd Observation id.
                    with ctx.created_lock:
                        oid = rng.choice(ctx.created_pool) if ctx.created_pool else None
                    if oid is None:
                        pid = rng.choice(ctx.pids)
                        ms, code, ok = do_read(s, ctx.base_url, pid)
                        ctx.log.record(OpRecord(
                            "crud", "R", started, ms, code, ok, note="fallback_patient_read",
                        ))
                    else:
                        ms, code, ok = do_read_obs(s, ctx.base_url, oid)
                        ctx.log.record(OpRecord("crud", "R", started, ms, code, ok))
                    _inc()
                elif verb == "V":
                    # Versioned read of a Patient U just pushed a new version of.
                    # Non-draining: multiple V workers can vread the same
                    # (pid, version) tuple; the pool isn't a budget.
                    with ctx.updated_versions_lock:
                        if not ctx.updated_versions:
                            tup = None
                        else:
                            tup = rng.choice(ctx.updated_versions)
                    if tup is None:
                        # U phase produced no versioned tuples (server didn't
                        # return meta.versionId, or U phase failed outright).
                        pool_empty_flag.set()
                        return
                    pid, ver = tup
                    ms, code, ok = do_vread(s, ctx.base_url, "Patient", pid, ver)
                    ctx.log.record(OpRecord("crud", "V", started, ms, code, ok))
                    _inc()
                elif verb == "D":
                    with ctx.created_lock:
                        oid = ctx.created_pool.popleft() if ctx.created_pool else None
                    if oid is None:
                        # Pool drained: C hit time cap with too few OK creates.
                        # Exit worker early; phase self-terminates when all
                        # workers exit.
                        pool_empty_flag.set()
                        return
                    ms, code, ok = do_delete(s, ctx.base_url, oid)
                    ctx.log.record(OpRecord("crud", "D", started, ms, code, ok))
                    _inc()

    t_start = time.monotonic()
    print(f"  [phase:{verb}] cap={sample_cap} time={duration_cap}s workers={workers}")
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, i) for i in range(workers)]
        # Periodic progress with an early-exit check when workers all return.
        while True:
            time.sleep(2)
            if all(f.done() for f in futs):
                break
            with counter_lock:
                cur = sample_count
            elapsed = time.monotonic() - t_start
            print(f"    t={elapsed:.0f}s {verb}={cur}")
            if cur >= sample_cap or time.monotonic() >= stop_at:
                # Workers will notice and exit on their own within one op.
                continue
        for f in futs:
            f.result()

    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    if sample_count >= sample_cap:
        stop_reason = "cap"
    elif verb in ("D", "V") and pool_empty_flag.is_set():
        stop_reason = "pool_empty"
    else:
        stop_reason = "time"

    summary: dict = {
        "verb": verb,
        "planned_cap": sample_cap,
        "planned_duration_s": duration_cap,
        "samples": sample_count,
        "elapsed_ms": elapsed_ms,
        "stop_reason": stop_reason,
        "early_stop": stop_reason != "time",
    }
    if verb == "U":
        summary["templates"] = dict(sorted(template_hist.items()))
    print(f"  [phase:{verb}] done — samples={sample_count} elapsed_ms={elapsed_ms} "
          f"stop={stop_reason}")
    return summary


def prewarm_templates(
    ctx: CrudContext, workers: int, per_template: int,
    prewarm_log_path: Path,
) -> dict:
    """Pre-warm each U template before the timed U phase.

    First-time JIT / plan compilation / cold-index-touch costs from a
    never-before-seen mutation pattern can land inside the first ~500 U
    samples and skew p99 upward. This phase runs `per_template` ops per
    template serially (one template at a time), so every template's
    cold-path cost is paid before the timed measurement. Ops go to a
    separate crud_prewarm.jsonl file so they never pollute percentile
    math downstream.
    """
    if per_template <= 0:
        return {"skipped": True}
    print(f"  [prewarm] {per_template} ops × {len(TEMPLATE_IDS)} templates "
          f"-> {prewarm_log_path.name}")
    prewarm_log = OpLog(prewarm_log_path)
    t_start = time.monotonic()
    per_template_done: dict[str, int] = {}
    try:
        for tid in TEMPLATE_IDS:
            remaining_lock = threading.Lock()
            remaining = [per_template]

            def worker(wid: int, tid: str = tid) -> None:
                rng = random.Random(wid * 6367 + int(time.time() * 1000))
                with httpx.Client(
                    timeout=60.0,
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                ) as client:
                    s = AuthedSession(ctx.server, client)
                    while True:
                        with remaining_lock:
                            if remaining[0] <= 0:
                                return
                            remaining[0] -= 1
                        pid = rng.choice(ctx.pids)
                        started = time.time()
                        ms, code, ok, _, _ = do_update(
                            s, ctx.base_url, pid, rng, template_id=tid,
                        )
                        prewarm_log.record(OpRecord(
                            "crud_prewarm", "U", started, ms, code, ok, note=tid,
                        ))

            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = [ex.submit(worker, i) for i in range(workers)]
                for f in futs:
                    f.result()
            per_template_done[tid] = per_template
    finally:
        prewarm_log.close()
    elapsed_ms = int((time.monotonic() - t_start) * 1000)
    print(f"  [prewarm] done in {elapsed_ms / 1000:.1f}s")
    return {
        "per_template": per_template,
        "templates": per_template_done,
        "elapsed_ms": elapsed_ms,
    }


def run_phased(
    ctx: CrudContext, workers: int,
    phase_caps: dict[str, tuple[int, float]],
    *, prewarm_per_template: int = 0,
    prewarm_log_path: Path | None = None,
) -> list[dict]:
    """Run C -> U -> R -> V -> D sequentially via run_verb_phase.

    Optional template prewarm runs BEFORE the U phase so first-call
    costs don't skew U's p99. Prewarm ops go to a separate file and
    don't count toward the U sample cap.
    """
    summaries: list[dict] = []
    for verb in PHASE_ORDER:
        if verb == "U" and prewarm_per_template > 0 and prewarm_log_path is not None:
            prewarm_summary = prewarm_templates(
                ctx, workers, prewarm_per_template, prewarm_log_path,
            )
            summaries.append({"verb": "prewarm", **prewarm_summary})
        cap, dur = phase_caps.get(verb, (10_000, 60.0))
        summaries.append(run_verb_phase(ctx, verb, workers, cap, dur))
    return summaries


def run(
    server_id: str, servers_path: Path, log_path: Path,
    duration: float, workers: int, mix_spec: str,
    harvest_target: int | None = None,
    *, phased: bool = False,
    phase_caps: dict[str, tuple[int, float]] | None = None,
    prewarm_per_template: int = 0,
    prewarm_log_path: Path | None = None,
) -> int | list[dict]:
    """CRUD workload entry-point.

    Two modes:
      - phased=False (default): legacy mixed workload for a fixed duration.
        Used by the warmup call in ramp.py and by the CLI `--mix` path.
        Returns int status code.
      - phased=True: five back-to-back timed per-verb phases
        (C/U/R/V/D), each capped at min(sample_cap, duration_cap).
        V is versioned-read of Patients U just bumped — exercises the
        version store, distinct from the current-record read in search.
        Optional template prewarm runs before U (`prewarm_per_template`
        ops per template, written to `prewarm_log_path`).
        Returns the list of phase summaries. Callers persist them
        alongside crud.jsonl (ramp.py writes crud_phases.json).
    """
    if phased:
        caps = phase_caps or {
            "C": (50_000, 300.0),
            "U": (50_000, 300.0),
            "R": (50_000, 60.0),
            "V": (50_000, 60.0),
            "D": (50_000, 300.0),
        }
        ctx = prepare_context(server_id, servers_path, log_path, harvest_target)
        if ctx is None:
            return 1
        print(f"CRUD workload on {server_id}: phased C->U->R->V->D workers={workers}")
        try:
            summaries = run_phased(
                ctx, workers, caps,
                prewarm_per_template=prewarm_per_template,
                prewarm_log_path=prewarm_log_path,
            )
        finally:
            ctx.log.close()
        totals = ctx.log.summary()
        print(f"Done. {totals['total']} ops across phases, {totals['errors']} errors.")
        return summaries

    servers = load_servers(servers_path)
    server = find_server(servers, server_id)
    base_url = resolve_base_url(server)
    if not base_url:
        print(f"ERROR: server '{server_id}' has no base_url configured", file=sys.stderr)
        return 2
    mix = parse_mix(mix_spec)
    print(f"CRUD workload on {server_id}: duration={duration}s workers={workers} mix={mix}")

    # One-shot client for the harvest phase. Harvest is uniform-over-all by
    # default — a bounded hot-set would let servers with strong row caches
    # post artificially low p99s.
    t_harvest = time.monotonic()
    # Client-level timeout must be >= harvest_patient_ids per-page timeout
    # (300s) or the floor here silently clips it.
    with httpx.Client(timeout=300.0) as bootstrap_client:
        session = AuthedSession(server, bootstrap_client)
        pids = harvest_patient_ids(session, base_url, target=harvest_target)
    if not pids:
        print("ERROR: could not harvest any Patient ids. Did ingest run?", file=sys.stderr)
        return 1
    cap_str = "unbounded" if harvest_target is None else f"capped at {harvest_target}"
    print(f"  harvested {len(pids):,} patient ids ({cap_str}) in "
          f"{time.monotonic() - t_harvest:.1f}s for the workload pool")

    log = OpLog(log_path)
    created_pool: deque[str] = deque(maxlen=100_000)
    created_lock = threading.Lock()
    stop_at = time.monotonic() + duration

    def worker(wid: int) -> None:
        rng = random.Random(wid * 6367 + int(time.time()))
        with httpx.Client(
            timeout=60.0,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:
            s = AuthedSession(server, client)
            while time.monotonic() < stop_at:
                verb = weighted_choice(mix, rng)
                started = time.time()
                if verb == "R":
                    pid = rng.choice(pids)
                    ms, code, ok = do_read(s, base_url, pid)
                    log.record(OpRecord("crud", "R", started, ms, code, ok))
                elif verb == "C":
                    pid = rng.choice(pids)
                    ms, code, ok, new_id = do_create(s, base_url, pid)
                    log.record(OpRecord("crud", "C", started, ms, code, ok))
                    if ok and new_id:
                        with created_lock:
                            created_pool.append(new_id)
                elif verb == "U":
                    pid = rng.choice(pids)
                    ms, code, ok, tid, _prev = do_update(s, base_url, pid, rng)
                    log.record(OpRecord("crud", "U", started, ms, code, ok, note=tid))
                elif verb == "D":
                    with created_lock:
                        oid = created_pool.popleft() if created_pool else None
                    if oid is None:
                        # fallback to read so we don't idle
                        pid = rng.choice(pids)
                        ms, code, ok = do_read(s, base_url, pid)
                        log.record(OpRecord("crud", "R", started, ms, code, ok, note="fallback_from_D"))
                    else:
                        ms, code, ok = do_delete(s, base_url, oid)
                        log.record(OpRecord("crud", "D", started, ms, code, ok))

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker, i) for i in range(workers)]
        # periodic progress
        t_start = time.monotonic()
        while time.monotonic() < stop_at:
            time.sleep(5)
            s = log.summary()
            elapsed = time.monotonic() - t_start
            print(f"  t={elapsed:.0f}s ops={s['total']} ok={s['ok']} err={s['errors']} rate={s['ops_per_s']:.0f}/s")
        for f in futs:
            f.result()

    summary = log.summary()
    log.close()
    print(f"Done. {summary['total']} ops in {summary['elapsed_s']:.1f}s "
          f"({summary['ops_per_s']:.1f}/s), {summary['errors']} errors.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", required=True)
    ap.add_argument("--servers-file", type=Path, default=DEFAULT_SERVERS)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--duration", type=float, default=900.0)
    ap.add_argument("--workers", type=int, default=64)
    ap.add_argument("--mix", default="C:10,R:60,U:25,D:5")
    ap.add_argument("--harvest-target", type=int, default=None,
                    help="Cap the patient pool at N ids (default: unbounded — uniform sampling over whole dataset). "
                         "Set to a small number for a 'hot-set / active-user' sub-experiment.")
    args = ap.parse_args()
    return run(
        server_id=args.server, servers_path=args.servers_file, log_path=args.log,
        duration=args.duration, workers=args.workers, mix_spec=args.mix,
        harvest_target=args.harvest_target,
    )


if __name__ == "__main__":
    sys.exit(main())
