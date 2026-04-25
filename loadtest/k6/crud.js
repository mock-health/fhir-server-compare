// CRUD workload — port of loadtest/workload_crud.py (mix mode).
//
// 64 VUs drive a weighted mix (C:10, R:60, U:25, D:5 — match the Python
// default) against the server identified by K6_SERVER for
// WORKLOAD_DURATION seconds (default 900 = 15 min). Each op's latency +
// status is tagged into the `crud_latency` Trend so handleSummary /
// `--out json` can reconstitute per-op records matching the Python
// harness's crud.jsonl shape.
//
// Pool coordination: k6 VUs share the JS runtime in-process but each VU
// iteration is independent. For the Create/Delete pair we use a module-
// scoped array protected by the fact that VU iterations are synchronous
// within a VU — module state *is* shared across VUs in k6 (as of v0.40+).
// A lightweight mutex isn't possible in goja but push/shift are atomic
// enough for this pattern: the worst case is a Delete and a Create racing
// on the same slot, which results in a duplicate attempt or a miss, both
// of which are accounted for correctly in the error tally.
//
// Env inputs:
//   K6_SERVER            — server id from servers.yaml (required)
//   K6_CONTEXT           — path to k6_context.json (default
//                          ./loadtest/k6/k6_context.json)
//   WORKLOAD_DURATION    — seconds, default 900
//   WORKERS              — VU count, default 64
//   CRUD_MIX             — 'C:10,R:60,U:25,D:5' (default)
//   HARVEST_TARGET       — cap patient pool at N (default: unbounded)
//
// Output:
//   --out json=<path>    — k6 raw NDJSON, one line per sample. Consumed
//                          by loadtest/k6/postprocess.py to produce the
//                          crud.jsonl the rest of the Python pipeline
//                          understands.

import http from 'k6/http';
import { Trend, Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import { targetServer, serverHeaders, workloadDuration, workers } from './lib/context.js';
import { harvestPatientIds } from './lib/harvest.js';
import { pickTemplate, TEMPLATES } from './lib/update_templates.js';

const server = targetServer();
const MIX = parseMix(__ENV.CRUD_MIX || 'C:10,R:60,U:25,D:5');
const HARVEST_TARGET = __ENV.HARVEST_TARGET ? Number(__ENV.HARVEST_TARGET) : null;

// ----------------------------------------------------------------------
// k6 options — matches the Python harness shape (64 VUs, fixed duration).
// ----------------------------------------------------------------------

export const options = {
  scenarios: {
    crud: {
      executor: 'constant-vus',
      vus: workers(),
      duration: `${workloadDuration()}s`,
      gracefulStop: '30s',
    },
  },
  // No k6 thresholds here — the trust-gate lives in the post-run summary
  // (buildTrust in lib/trust.js) so it's consistent with the Python
  // harness. A k6 threshold would have made this cell a pass/fail, which
  // isn't the model.
  noConnectionReuse: false,
  summaryTrendStats: ['min', 'med', 'avg', 'max', 'p(90)', 'p(95)', 'p(99)'],
  discardResponseBodies: false,  // we need resp.json() for create ids
};

// ----------------------------------------------------------------------
// Custom metrics. The default http_req_duration is tagged automatically,
// but we want a metric we own so the shape is obvious downstream.
// ----------------------------------------------------------------------

const crudLatency = new Trend('crud_latency_ms', true);
const crudErrors = new Counter('crud_errors');

// ----------------------------------------------------------------------
// Setup — runs once before VUs start. Harvests patient ids, returns them
// to every VU as `data`.
// ----------------------------------------------------------------------

export function setup() {
  console.log(`CRUD workload on ${server.id}: duration=${workloadDuration()}s ` +
    `workers=${workers()} mix=${JSON.stringify(MIX)}`);
  const t0 = Date.now();
  const pids = harvestPatientIds(server, HARVEST_TARGET);
  if (!pids.length) {
    throw new Error(
      'Could not harvest any Patient ids. Did ingest run against ' +
      `${server.id}? (base_url=${server.base_url})`,
    );
  }
  const cap = HARVEST_TARGET == null ? 'unbounded' : `capped at ${HARVEST_TARGET}`;
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`  harvested ${pids.length.toLocaleString()} patient ids (${cap}) in ${dt}s`);
  return { pids };
}

// ----------------------------------------------------------------------
// Shared Create pool — module-scoped array accessible to every VU.
// k6 (goja) keeps module state in one JS runtime so this Just Works.
// ----------------------------------------------------------------------

const CREATED_POOL = [];
const CREATED_POOL_MAX = 100_000;

// Track D-fallback-to-R the same way the Python harness does.
// record.note === 'fallback_from_D'.

// ----------------------------------------------------------------------
// Op implementations — one HTTP call each except U which is GET + PUT.
// Return shape: { ms, status, ok, note, newId }. newId is only set on
// successful create; caller pushes it into CREATED_POOL.
// ----------------------------------------------------------------------

const OBS_TEMPLATE = {
  resourceType: 'Observation',
  status: 'final',
  code: {
    coding: [{
      system: 'http://loinc.org',
      code: '8310-5',
      display: 'Body temperature',
    }],
    text: 'Body temperature',
  },
  valueQuantity: {
    value: 37.0, unit: 'C',
    system: 'http://unitsofmeasure.org', code: 'Cel',
  },
};

// The per-call `tags` option propagates onto k6's built-in
// http_req_duration sample — postprocess.py reads THAT stream (which is
// auto-tagged with status by k6), so `verb` needs to travel with it.
// Without the tag, postprocess sees "status": "200" but can't tell which
// verb the sample came from, and collapses all of CRUD into one bucket.
function doRead(pid) {
  const t0 = Date.now();
  const resp = http.get(`${server.base_url}/Patient/${pid}`, {
    headers: serverHeaders(server),
    timeout: '60s',
    tags: { verb: 'R' },
  });
  return toRecord(t0, resp);
}

function doReadObs(obsId) {
  const t0 = Date.now();
  const resp = http.get(`${server.base_url}/Observation/${obsId}`, {
    headers: serverHeaders(server),
    timeout: '60s',
    tags: { verb: 'R' },
  });
  return toRecord(t0, resp);
}

function doCreate(pid) {
  const body = JSON.parse(JSON.stringify(OBS_TEMPLATE));
  body.subject = { reference: `Patient/${pid}` };
  const t0 = Date.now();
  const resp = http.post(`${server.base_url}/Observation`, JSON.stringify(body), {
    headers: serverHeaders(server),
    timeout: '60s',
    tags: { verb: 'C' },
  });
  const rec = toRecord(t0, resp);
  if (rec.ok) {
    try {
      const parsed = resp.json();
      if (parsed && parsed.id) rec.newId = parsed.id;
    } catch { /* ignore parse failure */ }
  }
  return rec;
}

function doUpdate(pid) {
  const { tid, fn } = pickTemplate();
  const t0 = Date.now();
  const uTags = { verb: 'U', template: tid };
  const g = http.get(`${server.base_url}/Patient/${pid}`, {
    headers: serverHeaders(server),
    timeout: '60s',
    tags: uTags,
  });
  if (g.status < 200 || g.status >= 300) {
    const ms = Date.now() - t0;
    return { ms, status: g.status, ok: false, note: tid };
  }
  let patient;
  try {
    patient = g.json();
  } catch {
    const ms = Date.now() - t0;
    return { ms, status: g.status, ok: false, note: tid };
  }
  patient = fn(patient);
  const p = http.put(
    `${server.base_url}/Patient/${pid}`,
    JSON.stringify(patient),
    { headers: serverHeaders(server), timeout: '60s', tags: uTags },
  );
  const ms = Date.now() - t0;
  const ok = p.status >= 200 && p.status < 300;
  if (!ok) crudErrors.add(1);
  crudLatency.add(ms, uTags);
  return { ms, status: p.status, ok, note: tid };
}

function doDelete(obsId) {
  const t0 = Date.now();
  const resp = http.del(`${server.base_url}/Observation/${obsId}`, null, {
    headers: serverHeaders(server),
    timeout: '60s',
    tags: { verb: 'D' },
  });
  const ms = Date.now() - t0;
  // 2xx OR 404 both count as "gone" — matches Python.
  const ok = (resp.status >= 200 && resp.status < 300) || resp.status === 404;
  if (!ok) crudErrors.add(1);
  return { ms, status: resp.status, ok };
}

function toRecord(t0, resp) {
  const ms = Date.now() - t0;
  const ok = resp.status >= 200 && resp.status < 300;
  if (!ok) crudErrors.add(1);
  return { ms, status: resp.status, ok };
}

// ----------------------------------------------------------------------
// Default function — one iteration = one op. The VU loop picks a verb
// from MIX, invokes the op, logs latency + status.
// ----------------------------------------------------------------------

// k6's default text-summary generator crashes on certain metric shapes.
// Suppress it — NDJSON via `--out json=` is what postprocess.py reads.
export function handleSummary() {
  return {
    stdout: '\n[k6] workload done — raw samples in --out json=<path>\n',
  };
}

export default function (data) {
  const pids = data.pids;
  const verb = weightedChoice(MIX);
  const startedAt = Date.now() / 1000;

  if (verb === 'R') {
    const pid = pids[Math.floor(Math.random() * pids.length)];
    const r = doRead(pid);
    crudLatency.add(r.ms, { verb: 'R' });
    emit('R', startedAt, r);
    return;
  }
  if (verb === 'C') {
    const pid = pids[Math.floor(Math.random() * pids.length)];
    const r = doCreate(pid);
    crudLatency.add(r.ms, { verb: 'C' });
    if (r.ok && r.newId) {
      if (CREATED_POOL.length < CREATED_POOL_MAX) CREATED_POOL.push(r.newId);
    }
    emit('C', startedAt, r);
    return;
  }
  if (verb === 'U') {
    const pid = pids[Math.floor(Math.random() * pids.length)];
    const r = doUpdate(pid);
    emit('U', startedAt, r);
    return;
  }
  if (verb === 'D') {
    const oid = CREATED_POOL.shift();
    if (oid == null) {
      // Fallback to R so we don't idle — Python does the same thing.
      const pid = pids[Math.floor(Math.random() * pids.length)];
      const r = doRead(pid);
      crudLatency.add(r.ms, { verb: 'R', note: 'fallback_from_D' });
      emit('R', startedAt, { ...r, note: 'fallback_from_D' });
      return;
    }
    const r = doDelete(oid);
    crudLatency.add(r.ms, { verb: 'D' });
    emit('D', startedAt, r);
  }
}

// ----------------------------------------------------------------------
// Helpers: mix parser + weighted choice + JSONL-style emit.
// ----------------------------------------------------------------------

function parseMix(spec) {
  const out = {};
  for (const part of spec.split(',')) {
    const [k, v] = part.split(':');
    if (k && v) out[k.trim().toUpperCase()] = Number(v.trim());
  }
  const total = Object.values(out).reduce((a, b) => a + b, 0) || 1;
  for (const k of Object.keys(out)) out[k] = out[k] / total;
  return out;
}

function weightedChoice(weights) {
  const r = Math.random();
  let acc = 0;
  for (const [k, w] of Object.entries(weights)) {
    acc += w;
    if (r <= acc) return k;
  }
  return Object.keys(weights).slice(-1)[0];
}

// `emit` tags the k6 http_req* samples so postprocess.py can pull them
// out of the raw NDJSON and assemble the Python-harness crud.jsonl
// shape. We don't write JSONL directly because k6's JS runtime has no
// durable file-write API — we rely on `--out json=` for that.
function emit(verb, startedAt, r) {
  // Nothing to do here beyond what crudLatency.add already tagged —
  // but this function exists so postprocess.py has a single place in
  // the code to point at when understanding the metric shape, and
  // so future extensions (e.g. emitting V-phase records) have a hook.
  void verb; void startedAt; void r;
}
