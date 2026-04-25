// Linear-interpolation percentile — byte-for-byte port of
// loadtest/metrics.py::percentile(). Used by summary.js when assembling
// cell_summary.json. Keeping the math identical to the Python harness is
// the whole point of the shadow-run validation strategy.

// Percentile q in [0, 100]. Empty → 0.
// NOTE: caller is responsible for ascending sort. Python's implementation
// sorts internally; here we take pre-sorted arrays to avoid re-sorting the
// same latency vector for p50/p75/p90/p95/p99.
export function percentile(sortedValues, q) {
  if (!sortedValues.length) return 0;
  if (q <= 0) return sortedValues[0];
  if (q >= 100) return sortedValues[sortedValues.length - 1];
  const k = (sortedValues.length - 1) * (q / 100);
  const lo = Math.floor(k);
  const hi = Math.min(lo + 1, sortedValues.length - 1);
  const frac = k - lo;
  return sortedValues[lo] * (1 - frac) + sortedValues[hi] * frac;
}

// Convenience: compute all five headline quantiles in one pass from a
// pre-sorted latency array. Returns {p50, p75, p90, p95, p99}.
export function quantiles(sortedValues) {
  return {
    p50: percentile(sortedValues, 50),
    p75: percentile(sortedValues, 75),
    p90: percentile(sortedValues, 90),
    p95: percentile(sortedValues, 95),
    p99: percentile(sortedValues, 99),
  };
}
