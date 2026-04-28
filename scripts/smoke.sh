#!/usr/bin/env bash
# Post-refactor smoke test. Run from the repo root.
#
# Verifies imports resolve, every CLI surface accepts --help, the publish
# module's REPO_ROOT depth math is correct, and every docker-compose stack
# parses. No docker images are built and no network calls are made.
#
# Usage:
#   bash scripts/smoke.sh
#   PY=.venv/bin/python bash scripts/smoke.sh   # override interpreter

set -euo pipefail

PY="${PY:-.venv/bin/python}"

if [ ! -x "$PY" ]; then
  echo "ERROR: $PY not executable. Create a venv and run: $PY -m pip install -e ." >&2
  exit 1
fi

echo "[1/5] Imports resolve..."
"$PY" -c "from fhirbench import servers, compare, load_bundle"
"$PY" -c "from fhirbench.harness import (
    ramp, loader, stage, metrics, resources, wait_healthy, sample_pool,
    workload_crud, workload_search, report, host_meta,
    aidbox_bootstrap, bootstrap_medplum, generate, update_templates, k6_driver
)"
"$PY" -c "from fhirbench.benchmark import parse_report, cell_summary, summary"
"$PY" -c "from fhirbench.conformance import runner, run, parse_report, summary"
"$PY" -c "from fhirbench.publish import copy_to_studio, badges"
"$PY" -c "from fhirbench.cli import compare_harnesses, emit_k6_context, fetch_server_versions"
"$PY" -c "from fhirbench.k6 import postprocess"

echo "[2/5] CLI --help surfaces..."
"$PY" -m fhirbench.harness.ramp        --help > /dev/null
"$PY" -m fhirbench.conformance.run     --help > /dev/null
"$PY" -m fhirbench.compare             --help > /dev/null
"$PY" -m fhirbench.load_bundle         --help > /dev/null
"$PY" -m fhirbench.publish.copy_to_studio --help > /dev/null

echo "[3/5] Publisher REPO_ROOT depth math..."
"$PY" -c "
import fhirbench.publish.copy_to_studio as m
assert m.REPO_ROOT.name == 'fhir-server-compare', f'REPO_ROOT.name = {m.REPO_ROOT.name!r}'
assert m.SCHEMA_PATH.exists(), f'schema missing: {m.SCHEMA_PATH}'
for k, v in m.KIND_CONFIG.items():
    src = v['canonical_methodology_src']
    assert src.exists(), f'methodology missing for {k}: {src}'
print('  REPO_ROOT, schema, methodology paths all resolve')
"

echo "[4/5] Config + profile paths..."
"$PY" -c "
from fhirbench import compare, load_bundle
from fhirbench.benchmark import parse_report as bpr
for p in (compare.DEFAULT_QUERIES, compare.DEFAULT_SERVERS,
          load_bundle.DEFAULT_SERVERS,
          bpr.SERVERS_YAML, bpr.PROFILES_DIR):
    assert p.exists(), f'missing: {p}'
print('  config/servers.yaml, config/queries.yaml, profiles/benchmark/ all resolve')
"

echo "[5/5] Docker compose stacks validate..."
docker compose -f docker-compose.yml config -q
docker compose -f docker-compose.yml -f docker-compose.loadtest.yml config -q
docker compose -f docker-compose.yml \
               -f docker-compose.conformance.yml \
               -f docker-compose.conformance-features.yml config -q

echo ""
echo "SMOKE PASS"
