// Search workload — port of fhirbench/harness/workload_search.py.
//
// 64 VUs pick one of the ~23 queries uniformly at random per iteration
// and fire it. 10 of those queries carry `{{placeholder}}` refs resolved
// per-request from the SamplePool harvested in setup(). 13 are static.
//
// Headline percentile is ok-only — tagged separately so the downstream
// summary can compute p50 across 2xx-only records without re-scanning
// the raw stream.
//
// Env inputs (same shape as crud.js):
//   K6_SERVER            — server id (required)
//   K6_CONTEXT           — path to k6_context.json (default repo-relative)
//   WORKLOAD_DURATION    — seconds, default 900
//   WORKERS              — VU count, default 64

import http from 'k6/http';
import { Trend, Counter } from 'k6/metrics';
import { SharedArray } from 'k6/data';
import {
  CONTEXT, targetServer, serverHeaders, workloadDuration, workers,
} from './lib/context.js';
import {
  harvestPatientIds, buildSamplePool,
} from './lib/harvest.js';

const server = targetServer();
const HARVEST_TARGET = __ENV.HARVEST_TARGET ? Number(__ENV.HARVEST_TARGET) : null;

// Queries already filtered to `loadtest:include` by emit_k6_context.py.
const QUERIES = CONTEXT.queries;

// ----------------------------------------------------------------------
// k6 options.
// ----------------------------------------------------------------------

export const options = {
  scenarios: {
    search: {
      executor: 'constant-vus',
      vus: workers(),
      duration: `${workloadDuration()}s`,
      gracefulStop: '30s',
    },
  },
  noConnectionReuse: false,
  summaryTrendStats: ['min', 'med', 'avg', 'max', 'p(90)', 'p(95)', 'p(99)'],
  // NOTE: intentionally NOT setting `discardResponseBodies: true`. That
  // flag is global — it applies to setup()'s harvestPatientIds /
  // buildSamplePool calls as well as the main VU iterations, and the
  // setup calls MUST read response bodies to extract ids and codes.
  // For 120s workloads at 64 VUs with small (<100KB) Bundle responses,
  // the memory cost of keeping bodies is negligible.
};

// Two trends so postprocess.py can see ok-only latency directly from the
// metric names. The Python harness does the ok-only filter at report time;
// tagging both at write time is equivalent.
const searchLatency = new Trend('search_latency_ms', true);
const searchLatencyOk = new Trend('search_latency_ok_ms', true);
const searchErrors = new Counter('search_errors');

// ----------------------------------------------------------------------
// Setup — harvest pids, then harvest all other sample pools.
// Returns { pids, pools, queries } to every VU.
// ----------------------------------------------------------------------

export function setup() {
  console.log(`Search workload on ${server.id}: duration=${workloadDuration()}s ` +
    `workers=${workers()} queries=${QUERIES.length}`);

  const anySampled = QUERIES.some(q => q.sample && Object.keys(q.sample).length);
  const t0 = Date.now();
  const pids = harvestPatientIds(server, HARVEST_TARGET);
  if (!pids.length) {
    throw new Error(
      `Could not harvest any Patient ids from ${server.id} ` +
      `(base_url=${server.base_url}).`,
    );
  }
  let pools = {};
  if (anySampled) {
    pools = buildSamplePool(server, pids);
  } else {
    pools.patient_id = pids;
  }

  // Drop queries whose declared pool is empty — mirrors the `missing_for`
  // check in workload_search.py.
  const survivors = [];
  for (const q of QUERIES) {
    const missing = [];
    for (const poolName of Object.values(q.sample || {})) {
      if (!(pools[poolName] && pools[poolName].length)) missing.push(poolName);
    }
    if (missing.length) {
      console.log(`[sample_pool] dropping '${q.name}' — empty pools: ${missing.join(',')}`);
      continue;
    }
    survivors.push(q);
  }
  if (!survivors.length) {
    throw new Error('Every query was dropped (no data harvested).');
  }
  const dt = ((Date.now() - t0) / 1000).toFixed(1);
  console.log(`  ready in ${dt}s — ${survivors.length} query/ies active, ` +
    `${pids.length.toLocaleString()} patient ids`);
  return { pools, queries: survivors };
}

// ----------------------------------------------------------------------
// Placeholder expansion. Mirrors PLACEHOLDER_RE + SamplePool.expand().
// A placeholder appearing multiple times in one query resolves ONCE so
// path + param references are coherent.
// ----------------------------------------------------------------------

const PLACEHOLDER_RE = /\{\{(\w+)\}\}/g;

function expandQuery(q, pools) {
  if (!q.sample || !Object.keys(q.sample).length) return q;
  const resolved = {};
  const sub = (s) => s.replace(PLACEHOLDER_RE, (_m, name) => {
    if (name in resolved) return resolved[name];
    const poolName = q.sample[name];
    const pool = pools[poolName];
    if (!pool || !pool.length) return `{{${name}}}`;
    const v = pool[Math.floor(Math.random() * pool.length)];
    resolved[name] = v;
    return v;
  });
  const out = { ...q };
  if (typeof q.path === 'string') out.path = sub(q.path);
  const params = {};
  for (const [k, v] of Object.entries(q.params || {})) {
    params[k] = typeof v === 'string' ? sub(v) : v;
  }
  out.params = params;
  return out;
}

// Assemble the final URL. Query params are appended as ?a=b&c=d; values
// are URL-encoded. Matches httpx's behavior for string params.
function buildUrl(base, q) {
  const pathPart = (q.path || '').replace(/^\//, '');
  let url = `${base}/${pathPart}`;
  const pairs = [];
  for (const [k, v] of Object.entries(q.params || {})) {
    if (v == null) continue;
    pairs.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  if (pairs.length) url += '?' + pairs.join('&');
  return url;
}

// ----------------------------------------------------------------------
// Default function — one iteration = one search request.
// ----------------------------------------------------------------------

// k6's default text-summary generator crashes on certain metric shapes
// ("TypeError: No initial value" in _computeGlobalMaxNameWidth's reduce).
// Defining handleSummary — even as a no-op — disables the default. The
// durable per-request artifact is the NDJSON we emit via `--out json=`;
// loadtest/k6/postprocess.py reads that to produce the JSONL the rest
// of the pipeline expects, so we don't need k6's terminal summary at all.
export function handleSummary() {
  return {
    stdout: '\n[k6] workload done — raw samples in --out json=<path>\n',
  };
}

export default function (data) {
  const q0 = data.queries[Math.floor(Math.random() * data.queries.length)];
  const q = expandQuery(q0, data.pools);
  const url = buildUrl(server.base_url, q);
  const headers = serverHeaders(server, q.headers || {});

  // `tags: { verb }` here propagates onto k6's built-in http_req_duration
  // samples — postprocess.py reads those (they're auto-tagged with status,
  // method, url, etc.) rather than our custom trend, so verb + status land
  // on every sample without a second metric stream.
  const reqTags = { verb: q.name };
  const t0 = Date.now();
  let resp;
  if (q.method === 'POST') {
    resp = http.post(url, q.body ? JSON.stringify(q.body) : '', {
      headers, timeout: '60s', tags: reqTags,
    });
  } else {
    resp = http.get(url, { headers, timeout: '60s', tags: reqTags });
  }
  const ms = Date.now() - t0;
  const ok = resp.status >= 200 && resp.status < 300;
  searchLatency.add(ms, reqTags);
  if (ok) {
    searchLatencyOk.add(ms, reqTags);
  } else {
    searchErrors.add(1, reqTags);
  }
}
