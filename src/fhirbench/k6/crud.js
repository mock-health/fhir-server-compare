// CRUD workload — symmetric C/R/U/D across five FHIR resource types.
//
// 64 VUs drive a weighted verb mix (C:10, R:60, U:25, D:5 — match the
// Python default) against the server identified by K6_SERVER for
// WORKLOAD_DURATION seconds (default 900 = 15 min). Within each verb
// invocation a resource type is sampled by an independent weight mix
// (Observation 50%, Patient 20%, Condition 15%, Encounter 10%,
// MedicationRequest 5%) so every (verb × type) cell gets exercised.
//
// History: v1 of this workload was Observation-only for Create/Delete and
// Patient-only for Update — Marat Surmashev (Health Samurai) flagged this
// as a methodology gap because Medplum (and likely other servers) shows
// per-resource-type latency deviation that the verb-only breakdown hides.
// The aggregate verb-only baseline still rolls up the same way (so prior
// CRUD p50/p99 numbers remain directly comparable), but the published
// per_verb evidence array now also carries a resource_type tag so the
// heatmap drill-down can split by (verb × type). See
// plans/marat-from-health-samurai-wondrous-tome.md (Track A).
//
// Pool coordination: k6 VUs share the JS runtime in-process but each VU
// iteration is independent. For the Create/Delete pair we use a per-type
// CREATED_POOL keyed by resource type. Module state is shared across VUs
// in k6 (as of v0.40+); a goja-level mutex isn't possible but push/shift
// are atomic enough for this pattern. Worst case a Delete and a Create
// race on the same slot, which produces a duplicate attempt or a miss —
// both are accounted for correctly in the error tally.
//
// Env inputs:
//   K6_SERVER            — server id from servers.yaml (required)
//   K6_CONTEXT           — path to k6_context.json (default
//                          ./src/fhirbench/k6/k6_context.json)
//   WORKLOAD_DURATION    — seconds, default 900
//   WORKERS              — VU count, default 64
//   CRUD_MIX             — verb weights, e.g. 'C:10,R:60,U:25,D:5' (default)
//   CRUD_TYPE_MIX        — resource-type weights, e.g.
//                          'Observation:50,Patient:20,Condition:15,
//                           Encounter:10,MedicationRequest:5' (default).
//                          Set to 'Observation:100' to reproduce the v1
//                          single-type behavior for back-comparison.
//   HARVEST_TARGET       — cap Patient pool at N (default: unbounded);
//                          non-Patient pools always cap at the harvest
//                          library's CRUD_NONPATIENT_POOL_CAP (5000).
//
// Output:
//   --out json=<path>    — k6 raw NDJSON, one line per sample. Consumed
//                          by src/fhirbench/k6/postprocess.py to produce
//                          crud.jsonl. Postprocess pulls the verb +
//                          resource_type tags off each sample so the
//                          downstream cell_summary aggregator can group
//                          by (verb, resource_type).

import http from 'k6/http';
import { Trend, Counter } from 'k6/metrics';
import { targetServer, serverHeaders, workloadDuration, workers } from './lib/context.js';
import { harvestCrudReadPools } from './lib/harvest.js';
import { pickTemplate, TEMPLATES_BY_TYPE } from './lib/update_templates.js';

const server = targetServer();
const MIX = parseMix(__ENV.CRUD_MIX || 'C:10,R:60,U:25,D:5');
const TYPE_MIX = parseMix(__ENV.CRUD_TYPE_MIX
  || 'Observation:50,Patient:20,Condition:15,Encounter:10,MedicationRequest:5');
const HARVEST_TARGET = __ENV.HARVEST_TARGET ? Number(__ENV.HARVEST_TARGET) : null;

// The five types CRUD exercises. Order matters only for log readability;
// dispatch is by name. Must match keys in TEMPLATES_BY_TYPE
// (update_templates.js) and harvestCrudReadPools (harvest.js).
const CRUD_TYPES = ['Patient', 'Observation', 'Condition', 'Encounter', 'MedicationRequest'];

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
  // setup() paginates ids for 5 types; each can take dozens of seconds at
  // 64K+ records. k6's default 60s setupTimeout trips well before harvest
  // finishes. Same generous cap as search.js.
  setupTimeout: '30m',
  noConnectionReuse: false,
  summaryTrendStats: ['min', 'med', 'avg', 'max', 'p(90)', 'p(95)', 'p(99)'],
  discardResponseBodies: false,  // we need resp.json() for create ids
};

// ----------------------------------------------------------------------
// Custom metrics. The default http_req_duration is auto-tagged, but we
// keep an owned trend so the shape is obvious downstream.
// ----------------------------------------------------------------------

const crudLatency = new Trend('crud_latency_ms', true);
const crudErrors = new Counter('crud_errors');

// ----------------------------------------------------------------------
// Setup — runs once before VUs start. Harvests per-type id pools and
// returns them to every VU as `data.pools`.
// ----------------------------------------------------------------------

export function setup() {
  console.log(
    `CRUD workload on ${server.id}: duration=${workloadDuration()}s ` +
    `workers=${workers()} verb_mix=${JSON.stringify(MIX)} ` +
    `type_mix=${JSON.stringify(TYPE_MIX)}`,
  );
  const t0 = Date.now();
  const pools = harvestCrudReadPools(server, HARVEST_TARGET);
  if (!(pools.Patient || []).length) {
    throw new Error(
      'Could not harvest any Patient ids. Did ingest run against ' +
      `${server.id}? (base_url=${server.base_url})`,
    );
  }
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  const cap = HARVEST_TARGET == null ? 'unbounded' : `Patient capped at ${HARVEST_TARGET}`;
  console.log(`  ready in ${dt}s — ${cap}`);
  return { pools };
}

// ----------------------------------------------------------------------
// Per-type create pool — module-scoped object accessible to every VU.
// Each Delete picks from the bucket matching the type it sampled; if
// that bucket is empty we fall back to a Read (same pattern as v1).
// ----------------------------------------------------------------------

const CREATED_POOL = Object.fromEntries(CRUD_TYPES.map(t => [t, []]));
// Per-type cap — distributes the 100K total budget across 5 types so a
// long-running Observation-heavy workload doesn't starve the other types.
const CREATED_POOL_MAX_PER_TYPE = 20_000;

// ----------------------------------------------------------------------
// Create templates — one minimal-but-valid payload per resource type.
// Each consumes a Patient id (`pid`) and returns a fresh body object.
// Payloads are deliberately minimal to keep validation friendly across
// HAPI / Aidbox / Medplum / MS-FHIR / Blaze / Spark; do not add fields
// any one server's profile gate would reject.
// ----------------------------------------------------------------------

function nowIso() {
  return new Date().toISOString();
}

function rndInt(n) {
  return Math.floor(Math.random() * n);
}

const CREATE_TEMPLATES = {
  Patient: (_pid) => ({
    resourceType: 'Patient',
    // Synthetic family + given so concurrent creates don't collide on any
    // server with an active uniqueness constraint.
    name: [{ family: `LoadTest${Date.now()}-${rndInt(1_000_000)}`, given: ['Smoke'] }],
    gender: ['male', 'female', 'other', 'unknown'][rndInt(4)],
    birthDate: '1990-01-01',
  }),
  Observation: (pid) => ({
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
    subject: { reference: `Patient/${pid}` },
    effectiveDateTime: nowIso(),
    valueQuantity: {
      value: 37.0, unit: 'C',
      system: 'http://unitsofmeasure.org', code: 'Cel',
    },
  }),
  Condition: (pid) => ({
    resourceType: 'Condition',
    clinicalStatus: {
      coding: [{
        system: 'http://terminology.hl7.org/CodeSystem/condition-clinical',
        code: 'active',
      }],
    },
    verificationStatus: {
      coding: [{
        system: 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
        code: 'confirmed',
      }],
    },
    code: {
      coding: [{
        system: 'http://snomed.info/sct',
        code: '38341003',
        display: 'Hypertensive disorder',
      }],
      text: 'Hypertension (load test)',
    },
    subject: { reference: `Patient/${pid}` },
    onsetDateTime: nowIso(),
  }),
  Encounter: (pid) => {
    const start = new Date(Date.now() - 60 * 60 * 1000).toISOString();
    const end = nowIso();
    return {
      resourceType: 'Encounter',
      status: 'finished',
      class: {
        system: 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
        code: 'AMB',
        display: 'ambulatory',
      },
      subject: { reference: `Patient/${pid}` },
      period: { start, end },
    };
  },
  MedicationRequest: (pid) => ({
    resourceType: 'MedicationRequest',
    status: 'active',
    intent: 'order',
    medicationCodeableConcept: {
      coding: [{
        system: 'http://www.nlm.nih.gov/research/umls/rxnorm',
        code: '314076',
        display: 'lisinopril 10 MG Oral Tablet',
      }],
      text: 'lisinopril 10 mg PO daily',
    },
    subject: { reference: `Patient/${pid}` },
    authoredOn: nowIso(),
  }),
};

// Sanity-check the registry at module load: every CRUD_TYPE must have a
// create template AND an update mutator group. A typo here is the kind
// of bug that would silently degrade a published run, so fail loud.
for (const t of CRUD_TYPES) {
  if (!CREATE_TEMPLATES[t]) {
    throw new Error(`crud.js: no CREATE_TEMPLATE for ${t}`);
  }
  if (!TEMPLATES_BY_TYPE[t]) {
    throw new Error(`crud.js: no update mutator group for ${t} in update_templates.js`);
  }
}

// ----------------------------------------------------------------------
// Op implementations.
//
// Every k6 metric is tagged with both `verb` (single letter C/R/U/D)
// AND `resource_type` (the FHIR type just operated on). postprocess.py
// reads both tags off http_req_duration to produce the JSONL the
// downstream cell_summary aggregator groups by (verb, resource_type).
// ----------------------------------------------------------------------

function urlFor(type, id) {
  if (id == null) return `${server.base_url}/${type}`;
  return `${server.base_url}/${type}/${id}`;
}

function doRead(type, id) {
  const t0 = Date.now();
  const tags = { verb: 'R', resource_type: type };
  const resp = http.get(urlFor(type, id), {
    headers: serverHeaders(server),
    timeout: '60s',
    tags,
  });
  return finishRecord(t0, resp, tags);
}

function doCreate(type, pid) {
  const body = CREATE_TEMPLATES[type](pid);
  const t0 = Date.now();
  const tags = { verb: 'C', resource_type: type };
  const resp = http.post(urlFor(type, null), JSON.stringify(body), {
    headers: serverHeaders(server),
    timeout: '60s',
    tags,
  });
  const rec = finishRecord(t0, resp, tags);
  if (rec.ok) {
    try {
      const parsed = resp.json();
      if (parsed && parsed.id) rec.newId = parsed.id;
    } catch { /* ignore parse failure */ }
  }
  return rec;
}

function doUpdate(type, id) {
  const { tid, fn } = pickTemplate(type);
  const t0 = Date.now();
  const tags = { verb: 'U', resource_type: type, template: tid };
  const g = http.get(urlFor(type, id), {
    headers: serverHeaders(server),
    timeout: '60s',
    tags,
  });
  if (g.status < 200 || g.status >= 300) {
    const ms = Date.now() - t0;
    crudErrors.add(1);
    crudLatency.add(ms, tags);
    return { ms, status: g.status, ok: false, note: tid };
  }
  let resource;
  try {
    resource = g.json();
  } catch {
    const ms = Date.now() - t0;
    crudErrors.add(1);
    crudLatency.add(ms, tags);
    return { ms, status: g.status, ok: false, note: tid };
  }
  resource = fn(resource);
  const p = http.put(urlFor(type, id), JSON.stringify(resource), {
    headers: serverHeaders(server),
    timeout: '60s',
    tags,
  });
  const ms = Date.now() - t0;
  const ok = p.status >= 200 && p.status < 300;
  if (!ok) crudErrors.add(1);
  crudLatency.add(ms, tags);
  return { ms, status: p.status, ok, note: tid };
}

function doDelete(type, id) {
  const t0 = Date.now();
  const tags = { verb: 'D', resource_type: type };
  const resp = http.del(urlFor(type, id), null, {
    headers: serverHeaders(server),
    timeout: '60s',
    tags,
  });
  const ms = Date.now() - t0;
  // 2xx OR 404 both count as "gone" — matches Python.
  const ok = (resp.status >= 200 && resp.status < 300) || resp.status === 404;
  if (!ok) crudErrors.add(1);
  crudLatency.add(ms, tags);
  return { ms, status: resp.status, ok };
}

function finishRecord(t0, resp, tags) {
  const ms = Date.now() - t0;
  const ok = resp.status >= 200 && resp.status < 300;
  if (!ok) crudErrors.add(1);
  crudLatency.add(ms, tags);
  return { ms, status: resp.status, ok };
}

// Pool sampler: returns null when the pool is empty so callers can fall
// back to Patient. Avoids accidentally indexing an empty array (k6's
// goja returns undefined which would then serialize "undefined" into a
// URL — silent corruption).
function sampleId(pool) {
  if (!pool || !pool.length) return null;
  return pool[Math.floor(Math.random() * pool.length)];
}

// ----------------------------------------------------------------------
// Default function — one iteration = one op. Independently weighted
// verb + resource_type sampling.
// ----------------------------------------------------------------------

// k6's default text-summary generator crashes on certain metric shapes.
// Suppress it — NDJSON via `--out json=` is what postprocess.py reads.
export function handleSummary() {
  return {
    stdout: '\n[k6] workload done — raw samples in --out json=<path>\n',
  };
}

export default function (data) {
  const pools = data.pools;
  const verb = weightedChoice(MIX);
  const type = weightedChoice(TYPE_MIX);
  const startedAt = Date.now() / 1000;

  if (verb === 'R') {
    const id = sampleId(pools[type]) || sampleId(pools.Patient);
    const fallbackType = (sampleId(pools[type]) == null) ? 'Patient' : type;
    // Re-sample to avoid using the side-effect-free check above's id when
    // the pool was non-empty: simpler is to recompute once.
    const realId = sampleId(pools[fallbackType]);
    if (realId == null) {
      // Patient pool itself is empty — should never happen because setup()
      // throws, but be defensive.
      return;
    }
    const r = doRead(fallbackType, realId);
    emit('R', startedAt, r);
    return;
  }

  if (verb === 'C') {
    const pid = sampleId(pools.Patient);
    if (pid == null) return;
    const r = doCreate(type, pid);
    if (r.ok && r.newId) {
      const bucket = CREATED_POOL[type];
      if (bucket && bucket.length < CREATED_POOL_MAX_PER_TYPE) {
        bucket.push(r.newId);
      }
    }
    emit('C', startedAt, r);
    return;
  }

  if (verb === 'U') {
    let updateType = type;
    let id = sampleId(pools[updateType]);
    if (id == null) {
      // Type pool empty — fall back to Patient so the workload doesn't idle.
      updateType = 'Patient';
      id = sampleId(pools.Patient);
    }
    if (id == null) return;
    const r = doUpdate(updateType, id);
    emit('U', startedAt, r);
    return;
  }

  if (verb === 'D') {
    const bucket = CREATED_POOL[type];
    const oid = bucket ? bucket.shift() : null;
    if (oid == null) {
      // Fall back to R so we don't idle — matches v1 fallback_from_D
      // behavior. Falls back to Patient since that's the universally
      // populated read pool.
      const pid = sampleId(pools.Patient);
      if (pid == null) return;
      const r = doRead('Patient', pid);
      emit('R', startedAt, { ...r, note: 'fallback_from_D' });
      return;
    }
    const r = doDelete(type, oid);
    emit('D', startedAt, r);
  }
}

// ----------------------------------------------------------------------
// Helpers: mix parser + weighted choice + JSONL-style emit hook.
// ----------------------------------------------------------------------

// Parse 'A:10,B:20,C:5' into a normalized weight map { A: 0.286, B: 0.571, C: 0.143 }.
// Used for both verb mix (single-letter keys) and type mix (resourceType keys).
function parseMix(spec) {
  const out = {};
  for (const part of spec.split(',')) {
    const [k, v] = part.split(':');
    if (k && v) out[k.trim()] = Number(v.trim());
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

// Hook left in for symmetry with the Python harness's emit_op_record. All
// metric tagging happens inside doRead/doCreate/doUpdate/doDelete; this
// function is a no-op that exists so future extensions (e.g. a V phase
// or a phase-state side-channel) have a single place to point at.
function emit(verb, startedAt, r) {
  void verb; void startedAt; void r;
}
