// Trust block + cell summary builder — port of
// loadtest/benchmark/cell_summary.py. Any drift from the Python version
// will show up as cells with different `reliable` flags between the two
// harnesses, so keep the thresholds and arithmetic identical. The
// shadow-run validator (scripts/compare_harnesses.py) will catch drift.

import { quantiles, percentile } from './percentile.js';

// Per-quantile n_ok thresholds. q needs ~10 samples above it for ±10%
// stability; hence p99 wants 1000, p50 wants 30. Copy of
// cell_summary.QUANTILE_N_THRESHOLDS.
const QUANTILE_N_THRESHOLDS = {
  p50: 30,
  p75: 40,
  p90: 100,
  p95: 200,
  p99: 1000,
};

// Headline reliability gate. Matches cell_summary.MAX_ERR_RATE_RELIABLE /
// MIN_OK_THROUGHPUT_RELIABLE.
const MAX_ERR_RATE_RELIABLE = 0.20;
const MIN_OK_THROUGHPUT_RELIABLE = 1.0;

// Which workloads use the ok-only percentile stream. CRUD expects every
// op to succeed; search can fast-reject unsupported queries, so paint
// search with ok-only latency to avoid rewarding fast 4xx "no" responses.
// Mirrors cell_summary.USE_OK_ONLY.
export const USE_OK_ONLY = { crud: false, search: true };

export function buildTrust(nOk, errRate, opsOkPerS) {
  const t = {
    p50_trustworthy: nOk >= QUANTILE_N_THRESHOLDS.p50,
    p75_trustworthy: nOk >= QUANTILE_N_THRESHOLDS.p75,
    p90_trustworthy: nOk >= QUANTILE_N_THRESHOLDS.p90,
    p95_trustworthy: nOk >= QUANTILE_N_THRESHOLDS.p95,
    p99_trustworthy: nOk >= QUANTILE_N_THRESHOLDS.p99,
  };
  const reliable =
    errRate <= MAX_ERR_RATE_RELIABLE && opsOkPerS >= MIN_OK_THROUGHPUT_RELIABLE;
  t.reliable = reliable;
  if (!reliable) {
    const reasons = [];
    if (errRate > MAX_ERR_RATE_RELIABLE) {
      reasons.push(`err_rate=${(errRate * 100).toFixed(1)}%`);
    }
    if (opsOkPerS < MIN_OK_THROUGHPUT_RELIABLE) {
      reasons.push(`${opsOkPerS.toFixed(2)} ok/s (n_ok=${nOk})`);
    }
    t.reason = reasons.join('; ');
  }
  return t;
}

// records: [{verb, duration_ms, ok, status_code, started_at}]
// Returns the workload-level summary shape cell_summary.json expects.
// Mirrors _workload_summary() in cell_summary.py.
export function workloadSummary(records, useOkOnly) {
  if (!records.length) return null;

  const filtered = useOkOnly ? records.filter(r => r.ok) : records.slice();
  const nTotal = records.length;
  const nOk = records.filter(r => r.ok).length;
  const nErr = nTotal - nOk;

  // elapsed = max(started_at + duration) - min(started_at). Mirrors the
  // Python OpLog.summary() arithmetic plus workload_metrics's elapsed_s.
  let tMin = Infinity;
  let tMax = -Infinity;
  for (const r of records) {
    if (r.started_at < tMin) tMin = r.started_at;
    const end = r.started_at + r.duration_ms / 1000;
    if (end > tMax) tMax = end;
  }
  const elapsed = tMax > tMin ? tMax - tMin : 0;
  const errRate = nTotal > 0 ? nErr / nTotal : 0;
  const opsOkPerS = elapsed > 0 ? nOk / elapsed : 0;
  const opsPerS = elapsed > 0 ? nTotal / elapsed : 0;

  const lats = filtered.map(r => r.duration_ms).sort((a, b) => a - b);
  const q = quantiles(lats);

  const trust = buildTrust(nOk, errRate, opsOkPerS);

  // Per-verb breakout: group records by the composite key
  // (verb, resource_type, complexity). Mirrors cell_summary.py's grouping —
  // see plans/marat-from-health-samurai-wondrous-tome.md (Tracks A + C).
  // Records lacking the new dimensions group as (verb, undefined, undefined),
  // preserving the original verb-only breakdown for legacy callers.
  const groups = new Map();
  for (const r of records) {
    const key = `${r.verb}\x1f${r.resource_type || ''}\x1f${r.complexity || ''}`;
    if (!groups.has(key)) {
      groups.set(key, {
        verb: r.verb,
        resource_type: r.resource_type || null,
        complexity: r.complexity || null,
        recs: [],
      });
    }
    groups.get(key).recs.push(r);
  }
  const perVerb = [];
  for (const { verb, resource_type, complexity, recs: vRecs } of groups.values()) {
    const vTotal = vRecs.length;
    const vOk = vRecs.filter(r => r.ok).length;
    const vErr = vTotal - vOk;
    const vErrRate = vTotal > 0 ? vErr / vTotal : 0;
    const vOpsOkPerS = elapsed > 0 ? vOk / elapsed : 0;
    const vLats = (useOkOnly ? vRecs.filter(r => r.ok) : vRecs)
      .map(r => r.duration_ms)
      .sort((a, b) => a - b);
    const vQ = quantiles(vLats);
    const item = {
      verb,
      p50_ms: round2(vQ.p50),
      p75_ms: round2(vQ.p75),
      p90_ms: round2(vQ.p90),
      p95_ms: round2(vQ.p95),
      p99_ms: round2(vQ.p99),
      ops_per_s: round2(elapsed > 0 ? vTotal / elapsed : 0),
      ops_ok_per_s: round2(vOpsOkPerS),
      n: vTotal,
      n_ok: vOk,
      n_err: vErr,
      error_rate: round4(vErrRate),
      trust: buildTrust(vOk, vErrRate, vOpsOkPerS),
    };
    if (resource_type) item.resource_type = resource_type;
    if (complexity) item.complexity = complexity;
    perVerb.push(item);
  }
  // Same sort order cell_summary.py uses, so the JS and Python harnesses
  // produce byte-identical per_verb arrays for shadow-validator runs.
  perVerb.sort((a, b) => {
    if (a.verb !== b.verb) return a.verb < b.verb ? -1 : 1;
    const arT = a.resource_type || '';
    const brT = b.resource_type || '';
    if (arT !== brT) return arT < brT ? -1 : 1;
    const aC = a.complexity || '';
    const bC = b.complexity || '';
    return aC < bC ? -1 : (aC > bC ? 1 : 0);
  });

  return {
    n: nTotal,
    n_ok: nOk,
    n_err: nErr,
    error_rate: round4(errRate),
    elapsed_s: Number(elapsed.toFixed(3)),
    ops_per_s: round2(opsPerS),
    ops_ok_per_s: round2(opsOkPerS),
    p50_ms: round2(q.p50),
    p75_ms: round2(q.p75),
    p90_ms: round2(q.p90),
    p95_ms: round2(q.p95),
    p99_ms: round2(q.p99),
    trust,
    per_verb: perVerb,
  };
}

function round2(n) { return Math.round(n * 100) / 100; }
function round4(n) { return Math.round(n * 10000) / 10000; }
