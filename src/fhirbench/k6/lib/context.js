// Load the k6 context blob emitted by scripts/emit_k6_context.py.
//
// k6 `open()` reads the file exactly once, at init time, and resolves
// relative paths relative to the file that CALLED open() — not the entry
// script and not the working directory. This file lives at
// loadtest/k6/lib/context.js, so a relative path here hops up one dir to
// loadtest/k6/.
//
// The context path can be overridden with the K6_CONTEXT env var. Callers
// should pass an absolute path (e.g. /src/loadtest/k6/k6_context.json when
// k6 runs inside the grafana/k6 docker container with -v $REPO:/src) so
// the resolution is unambiguous regardless of which file triggers open().

const CONTEXT_PATH = __ENV.K6_CONTEXT || '../k6_context.json';

const RAW = open(CONTEXT_PATH);
const CONTEXT = JSON.parse(RAW);

export { CONTEXT };

// Resolve one server by id. Mirrors find_server() in _fhir_servers.py.
// Returns null (not throws) if absent so callers can log + skip the cell
// instead of blowing up the whole run.
export function findServer(serverId) {
  for (const s of CONTEXT.servers) {
    if (s.id === serverId) return s;
  }
  return null;
}

// Assemble the full request-header dict for one server.
// Mirrors build_headers() in _fhir_servers.py — auth headers first, then
// any vendor-specific extras (e.g. MS FHIR's x-bundle-processing-logic).
const FHIR_CT = 'application/fhir+json';
export function serverHeaders(server, extraFromQuery) {
  const out = {
    Accept: FHIR_CT,
    'Content-Type': FHIR_CT,
    ...(server.auth_headers || {}),
    ...(server.extra_headers || {}),
    ...(extraFromQuery || {}),
  };
  return out;
}

// Pick the single server this k6 run targets. One k6 invocation = one
// server per the mock.health harness design (per-cell runs). The id comes
// from the K6_SERVER env var; we fail loudly if it's missing because a
// k6 run that accidentally hits the wrong server would poison its cell.
export function targetServer() {
  const id = __ENV.K6_SERVER;
  if (!id) {
    throw new Error(
      'K6_SERVER env var not set. Set it to a server id from servers.yaml ' +
      '(e.g. K6_SERVER=hapi).',
    );
  }
  const s = findServer(id);
  if (!s) {
    throw new Error(
      `K6_SERVER='${id}' not found in context (${CONTEXT_PATH}). ` +
      `Known: ${CONTEXT.servers.map(x => x.id).join(', ')}.`,
    );
  }
  return s;
}

// Workload duration in seconds. Matches the Python harness default.
export function workloadDuration() {
  return Number(__ENV.WORKLOAD_DURATION || 900);
}

// Worker / VU count. The Python harness uses 64; k6's VU model maps 1:1.
export function workers() {
  return Number(__ENV.WORKERS || 64);
}
