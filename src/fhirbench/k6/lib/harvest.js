// Port of harvest_patient_ids() + loadtest/sample_pool.py. Both run in
// k6's setup() — once per workload, before any VU iteration starts.
//
// k6's HTTP module is http.* (import from 'k6/http'); response.json() and
// response.status work the same as httpx. setup() runs single-threaded so
// no locking is needed. Results are returned from setup() and k6 hands
// them to every VU as the `data` arg of the default function.

import http from 'k6/http';
import { serverHeaders } from './context.js';

const MAX_NEXT_URL = 8192;
const MAX_PER_POOL = 500;
const PAGINATE_COUNT = 200;

// Paginate a FHIR search, returning up to `maxResources` resources.
// Mirrors loadtest/sample_pool.py::_paginate. On any failure (non-2xx,
// malformed JSON, missing next link, oversized next url) returns what
// we have so far — same defensive posture as the Python version.
function paginate(server, resourceType, elements, maxResources) {
  const out = [];
  let url = `${server.base_url}/${resourceType}?_count=${PAGINATE_COUNT}&_elements=${elements}`;
  const headers = serverHeaders(server);
  while (url && out.length < maxResources) {
    const resp = http.get(url, { headers, timeout: '300s' });
    if (resp.status < 200 || resp.status >= 300) break;
    let body;
    try {
      body = resp.json();
    } catch {
      break;
    }
    for (const e of body.entry || []) {
      const res = (e && e.resource) || {};
      if (res && Object.keys(res).length) {
        out.push(res);
        if (out.length >= maxResources) break;
      }
    }
    let nextUrl = null;
    for (const link of body.link || []) {
      if (link.relation === 'next') { nextUrl = link.url; break; }
    }
    if (!nextUrl) break;
    const resolved = resolveUrl(url, nextUrl);
    if (resolved.length > MAX_NEXT_URL) break;
    url = resolved;
  }
  return out;
}

// Minimal urljoin-equivalent: if `next` is absolute, use it; otherwise
// resolve relative to `base`. Covers Aidbox's relative link.next URLs.
function resolveUrl(base, next) {
  if (/^https?:\/\//i.test(next)) return next;
  // Relative: preserve protocol + host from base, replace path + query.
  const m = base.match(/^(https?:\/\/[^/]+)(.*)$/i);
  if (!m) return next;
  const origin = m[1];
  if (next.startsWith('/')) return origin + next;
  // Relative to base's parent path — strip the base's query, keep path
  // up to the last slash, append next.
  const basePath = m[2].split('?')[0];
  const parent = basePath.substring(0, basePath.lastIndexOf('/') + 1) || '/';
  return origin + parent + next;
}

// Port of harvest_patient_ids — uniform sampling over ALL patient ids.
// Hot-set bias would let row-cache-heavy servers look artificially fast.
export function harvestPatientIds(server, target) {
  return harvestResourceIds(server, 'Patient', target);
}

// Generalized id harvester: paginates {resourceType}?_count=200&_elements=id
// until the target cap is reached or pagination terminates. Used by both
// harvestPatientIds and the CRUD per-type read pools (Observation,
// Condition, Encounter, MedicationRequest). Returns an array of bare ids.
//
// `target == null` means "until the server stops paging" — appropriate for
// Patient where we want every id in the corpus. For non-Patient types we
// cap at a few thousand to avoid spending the workload's setup budget on
// id harvesting alone (Synthea writes 10× more Observations than patients).
export function harvestResourceIds(server, resourceType, target) {
  const ids = [];
  let url = `${server.base_url}/${resourceType}?_count=200&_elements=id`;
  const headers = serverHeaders(server);
  let page = 0;
  while (url) {
    if (target != null && ids.length >= target) break;
    const resp = http.get(url, { headers, timeout: '300s' });
    page += 1;
    if (page === 1) {
      // Log the first-page outcome so a silent stall (non-2xx, empty
      // Bundle, auth redirect) is visible in k6 output. Subsequent
      // pages stay quiet to avoid flooding the log.
      const bodyLen = (resp.body || '').length;
      console.log(`[harvest] ${server.id} ${resourceType} page 1: status=${resp.status} body=${bodyLen}B`);
    }
    if (resp.status < 200 || resp.status >= 300) break;
    let body;
    try { body = resp.json(); } catch { break; }
    for (const e of body.entry || []) {
      const rid = (e && e.resource && e.resource.id) || null;
      if (rid) ids.push(rid);
    }
    let nextUrl = null;
    for (const link of body.link || []) {
      if (link.relation === 'next') { nextUrl = link.url; break; }
    }
    if (!nextUrl) break;
    const resolved = resolveUrl(url, nextUrl);
    if (resolved.length > MAX_NEXT_URL) break;
    url = resolved;
  }
  return target != null ? ids.slice(0, target) : ids;
}

// Per-type read pool cap for non-Patient types in the CRUD workload.
// 5000 samples is enough for 64 VUs to avoid material hot-set bias over
// a 15-minute run while keeping setup time under a minute even on slow
// servers. Patients keep their own (uncapped) target via HARVEST_TARGET.
const CRUD_NONPATIENT_POOL_CAP = 5000;

// Build the per-type read-id map for the CRUD workload. Each value is an
// array of ids — the VU loop indexes randomly into the array per read.
// Returned shape (keys are FHIR resourceType strings to match the
// CREATE_TEMPLATES / TEMPLATES_BY_TYPE keys in update_templates.js):
//   {
//     Patient:           [...],   // capped at patientTarget (if not null)
//     Observation:       [...],   // capped at CRUD_NONPATIENT_POOL_CAP
//     Condition:         [...],
//     Encounter:         [...],
//     MedicationRequest: [...],
//   }
//
// Empty arrays are valid — a server with zero Conditions ingested simply
// gets fewer Condition reads exercised. The CRUD verb dispatcher in
// crud.js falls back to the Patient pool when a non-Patient pool is
// empty so the workload doesn't idle.
export function harvestCrudReadPools(server, patientTarget) {
  const t0 = Date.now();
  const pools = {
    Patient:           harvestResourceIds(server, 'Patient',           patientTarget),
    Observation:       harvestResourceIds(server, 'Observation',       CRUD_NONPATIENT_POOL_CAP),
    Condition:         harvestResourceIds(server, 'Condition',         CRUD_NONPATIENT_POOL_CAP),
    Encounter:         harvestResourceIds(server, 'Encounter',         CRUD_NONPATIENT_POOL_CAP),
    MedicationRequest: harvestResourceIds(server, 'MedicationRequest', CRUD_NONPATIENT_POOL_CAP),
  };
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  const summary = Object.entries(pools)
    .map(([k, v]) => `${k}=${v.length.toLocaleString()}`).join(', ');
  console.log(`[crud_pools] harvested in ${dt}s: ${summary}`);
  return pools;
}

// Extract first 'system|code' token from a CodeableConcept.
function codeableConceptToken(cc) {
  if (!cc) return null;
  for (const c of cc.coding || []) {
    if (c.system && c.code) return `${c.system}|${c.code}`;
  }
  return null;
}

export function harvestPatientNames(server, field) {
  const seen = new Set();
  const resources = paginate(server, 'Patient', 'name', MAX_PER_POOL * 4);
  for (const res of resources) {
    for (const n of res.name || []) {
      if (field === 'family' && typeof n.family === 'string' && n.family) {
        seen.add(n.family);
      } else if (field === 'given') {
        const g = n.given || [];
        if (g.length && typeof g[0] === 'string' && g[0]) seen.add(g[0]);
      }
      break; // first name entry per resource, same as Python
    }
    if (seen.size >= MAX_PER_POOL) break;
  }
  return [...seen].sort().slice(0, MAX_PER_POOL);
}

export function harvestTokens(server, resourceType, field) {
  const seen = new Set();
  const resources = paginate(server, resourceType, field, MAX_PER_POOL * 8);
  for (const res of resources) {
    const tok = codeableConceptToken(res[field]);
    if (tok) seen.add(tok);
    if (seen.size >= MAX_PER_POOL) break;
  }
  return [...seen].sort().slice(0, MAX_PER_POOL);
}

export function harvestIds(server, resourceType) {
  const out = [];
  const resources = paginate(server, resourceType, 'id', MAX_PER_POOL);
  for (const res of resources) {
    if (res.id) out.push(res.id);
  }
  return out;
}

// Build the full sample-pool bundle. Returned dict shape matches the
// Python SamplePool.pools dict. Called from setup() in search.js.
export function buildSamplePool(server, patientIds) {
  const started = Date.now();
  const pools = {
    patient_id: patientIds.slice(),
    patient_family: harvestPatientNames(server, 'family'),
    patient_given: harvestPatientNames(server, 'given'),
    condition_code: harvestTokens(server, 'Condition', 'code'),
    procedure_code: harvestTokens(server, 'Procedure', 'code'),
    medication_code: harvestTokens(
      server, 'MedicationRequest', 'medicationCodeableConcept',
    ),
    practitioner_id: harvestIds(server, 'Practitioner'),
    location_id: harvestIds(server, 'Location'),
  };
  const dt = ((Date.now() - started) / 1000).toFixed(1);
  const summary = Object.entries(pools)
    .map(([k, v]) => `${k}=${v.length}`).join(', ');
  console.log(`[sample_pool] harvested in ${dt}s: ${summary}`);
  return pools;
}
