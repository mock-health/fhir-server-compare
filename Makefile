# Public convenience targets. Wrap the Python commands the README and
# CONTRIBUTING docs reference so `make <target>` works out of the box.
#
# Everything here delegates to python modules under src/fhirbench/ —
# the harness package installed via `pip install -e .` (see pyproject.toml). Nothing lives in this
# file that isn't also documented in README.md or CONTRIBUTING.md.
#
# .env is sourced if present so AIDBOX_LICENSE, MEDPLUM_CLIENT_*, and
# MSSQL_SA_PASSWORD reach every recipe.

SHELL := /bin/bash

ifneq (,$(wildcard .env))
  include .env
  export
endif

PY           ?= .venv/bin/python
CONF_ROUND   ?= 2026-q2-r000
BENCH_ROUND  ?= $(CONF_ROUND)
BENCH_RUN_ID ?= ramp-50k
STUDIO_DIR   ?= ../fhir-studio

# Ramp roster and workload shape. Override on the command line to taste.
SERVERS           ?= hapi blaze aidbox medplum msfhir spark
WORKERS_INGEST    ?= 32
WORKERS_WORKLOAD  ?= 64
WORKLOAD_DURATION ?= 900
RAMP_CHECKPOINTS      ?= 1024,4096,8192,16384,32768,65536,131072
RAMP_CHECKPOINTS_50K  ?= 1000,4000,16000,64000

COMPOSE := docker compose -f docker-compose.yml -f docker-compose.loadtest.yml

.PHONY: help \
        loadtest-dryrun loadtest-ramp loadtest-ramp-50k loadtest-ramp-100k \
        k6-context k6-crud k6-search \
        conformance conformance-run conformance-parse conformance-validate \
        conformance-publish conformance-summary \
        benchmark benchmark-cell-summaries benchmark-parse benchmark-validate \
        benchmark-publish benchmark-summary

help:
	@echo "Public targets (see README.md and CONTRIBUTING.md for context):"
	@echo ""
	@echo "Load test (driven by Grafana k6 — see src/fhirbench/k6/):"
	@echo "  make loadtest-dryrun     10-patient smoke (~2 min). Validates the full pipeline."
	@echo "  make loadtest-ramp-50k   50K ramp, checkpoints 1K/4K/16K/64K, all servers. ~12-16h."
	@echo "  make loadtest-ramp-100k  Full 100K+ ramp per RAMP_CHECKPOINTS. ~24-30h."
	@echo ""
	@echo "k6 single-server diagnostics:"
	@echo "  make k6-context  SERVER=hapi WL=search  Emit src/fhirbench/k6/k6_context.json."
	@echo "  make k6-crud     SERVER=hapi            Run k6 CRUD workload against one server."
	@echo "  make k6-search   SERVER=hapi            Run k6 Search workload against one server."
	@echo ""
	@echo "Conformance (TestScript-based):"
	@echo "  make conformance-run       Execute all TestScripts against every server."
	@echo "  make conformance-parse     Fold TestReports into results/rounds/<id>/conformance.json."
	@echo "  make conformance-validate  Schema-check the round artifact."
	@echo "  make conformance-publish   Copy round + badges into \$$(STUDIO_DIR)."
	@echo "  make conformance           Shortcut: run + parse + validate."
	@echo ""
	@echo "Benchmark (fold a completed ramp into a round artifact):"
	@echo "  make benchmark-parse       Walk results/loadtest/<run-id>/ → benchmark.json."
	@echo "  make benchmark-validate    Schema-check benchmark.json."
	@echo "  make benchmark-publish     Copy benchmark round + methodology into \$$(STUDIO_DIR)."
	@echo "  make benchmark             Shortcut: cell-summaries + parse + validate."
	@echo ""
	@echo "Overrides: CONF_ROUND, BENCH_ROUND, BENCH_RUN_ID, STUDIO_DIR, PY,"
	@echo "           SERVERS, WORKERS_INGEST, WORKERS_WORKLOAD, WORKLOAD_DURATION,"
	@echo "           RAMP_CHECKPOINTS, RAMP_CHECKPOINTS_50K."

# ---------------------------------------------------------------------------
# Load test — the Python orchestrator (fhirbench.harness.ramp) owns the
# per-server up/wait/bootstrap/ingest/workload/stop lifecycle. The timed
# CRUD + Search workload phase runs in Grafana k6 inside docker; ramp.py
# invokes it via fhirbench.harness.k6_driver.
# ---------------------------------------------------------------------------

## 10-patient smoke test: reset all volumes, generate 10 patients, run k6 CRUD
## + Search workloads against each server serially, render report.
loadtest-dryrun:
	$(COMPOSE) down -v
	$(PY) -m fhirbench.harness.generate --count 10
	$(PY) -m fhirbench.harness.ramp --run-id dryrun-10p \
	    --checkpoints "10" \
	    --servers "$$(echo '$(SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration 30
	$(PY) -m fhirbench.harness.report --run-id dryrun-10p

# ---------------------------------------------------------------------------
# k6 single-server diagnostic targets. Each drives ONE server outside the
# multi-server ramp — useful for iterating on a query or a specific server's
# behavior without paying the full ramp's wall-clock cost.
#
# Inputs:
#   SERVER=<id>            — server id from servers.yaml (required)
#   WL=crud|search         — workload (k6-context default: search)
#   WORKLOAD_DURATION      — inherits from the top-level var, default 900
#   WORKERS                — defaults to 64 inside the k6 scripts
# ---------------------------------------------------------------------------

K6_DIR          := src/fhirbench/k6
K6_CTX          := $(K6_DIR)/k6_context.json
K6_OUT_DIR      := results/k6
K6_DURATION     ?= $(WORKLOAD_DURATION)

## Re-emit k6_context.json for one server + one workload. SERVER and WL required.
## Example:  make k6-context SERVER=hapi WL=search
k6-context:
	@[ -n "$(SERVER)" ] || (echo 'ERROR: set SERVER=<id>' >&2; exit 2)
	@[ -n "$(WL)" ]     || (echo 'ERROR: set WL=crud|search' >&2; exit 2)
	$(PY) -m fhirbench.cli.emit_k6_context \
	    --server $(SERVER) --workload $(WL) --out $(K6_CTX)

## Run the k6 CRUD workload against SERVER for K6_DURATION seconds.
## Produces results/k6/$(SERVER)-crud.ndjson + a derived crud.jsonl.
k6-crud: K6_NDJSON := $(K6_OUT_DIR)/$(SERVER)-crud.ndjson
k6-crud: K6_JSONL  := $(K6_OUT_DIR)/$(SERVER)-crud.jsonl
k6-crud:
	@[ -n "$(SERVER)" ] || (echo 'ERROR: set SERVER=<id>' >&2; exit 2)
	mkdir -p $(K6_OUT_DIR)
	$(MAKE) k6-context SERVER=$(SERVER) WL=crud
	docker run --rm --user $$(id -u):$$(id -g) --network host \
	    -v $(CURDIR):/src -w /src \
	    -e K6_SERVER=$(SERVER) -e K6_CONTEXT=/src/$(K6_CTX) \
	    -e WORKLOAD_DURATION=$(K6_DURATION) \
	    grafana/k6 run --out json=$(K6_NDJSON) $(K6_DIR)/crud.js
	$(PY) -m fhirbench.k6.postprocess \
	    --k6-json $(K6_NDJSON) --workload crud --out $(K6_JSONL)

## Run the k6 Search workload against SERVER.
k6-search: K6_NDJSON := $(K6_OUT_DIR)/$(SERVER)-search.ndjson
k6-search: K6_JSONL  := $(K6_OUT_DIR)/$(SERVER)-search.jsonl
k6-search:
	@[ -n "$(SERVER)" ] || (echo 'ERROR: set SERVER=<id>' >&2; exit 2)
	mkdir -p $(K6_OUT_DIR)
	$(MAKE) k6-context SERVER=$(SERVER) WL=search
	docker run --rm --user $$(id -u):$$(id -g) --network host \
	    -v $(CURDIR):/src -w /src \
	    -e K6_SERVER=$(SERVER) -e K6_CONTEXT=/src/$(K6_CTX) \
	    -e WORKLOAD_DURATION=$(K6_DURATION) \
	    grafana/k6 run --out json=$(K6_NDJSON) $(K6_DIR)/search.js
	$(PY) -m fhirbench.k6.postprocess \
	    --k6-json $(K6_NDJSON) --workload search --out $(K6_JSONL)

## End-to-end ramp. Checkpoints are cumulative patient counts; at each one
## every server gets a cold-start measurement (DB reset, ingest N, run k6
## CRUD + Search workloads, stop). The Python orchestrator owns the per-
## server lifecycle and invokes the k6 container per workload via
## fhirbench.harness.k6_driver.
loadtest-ramp: RUN_ID ?= $(shell date +%Y-%m-%d)-ramp
loadtest-ramp:
	$(PY) -m fhirbench.harness.ramp --run-id $(RUN_ID) \
	    --checkpoints "$(RAMP_CHECKPOINTS)" \
	    --servers "$$(echo '$(SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration $(WORKLOAD_DURATION)
	$(PY) -m fhirbench.harness.report --run-id $(RUN_ID)

## 50K ramp: 1K / 4K / 16K / 64K checkpoints. Fits in a single overnight.
loadtest-ramp-50k:
	$(MAKE) loadtest-ramp RUN_ID=ramp-50k RAMP_CHECKPOINTS='$(RAMP_CHECKPOINTS_50K)'

## 100K+ ramp: default checkpoints terminate at 131K.
loadtest-ramp-100k:
	$(MAKE) loadtest-ramp RUN_ID=$(shell date +%Y-%m-%d)-ramp-100k RAMP_CHECKPOINTS='$(RAMP_CHECKPOINTS)'

# ---------------------------------------------------------------------------
# Conformance targets — drive the /conformance heatmap.
# ---------------------------------------------------------------------------

## Shortcut: run + parse + validate.
conformance: conformance-run conformance-parse conformance-validate

## Execute every TestScript against every server. Output lands under
## results/conformance/$(CONF_ROUND)/<server>/.
conformance-run:
	$(PY) -m fhirbench.conformance.run --round $(CONF_ROUND) --server all \
	    --testscripts conformance/testscripts/fhir-r4-base
	$(PY) -m fhirbench.conformance.run --round $(CONF_ROUND) --server all \
	    --testscripts conformance/testscripts/bulk-data-v2

## Fold TestReports into results/rounds/$(CONF_ROUND)/conformance.json.
conformance-parse:
	$(PY) -m fhirbench.conformance.parse_report --round $(CONF_ROUND)

## Validate the round artifact against schema/round-v1.schema.json.
conformance-validate:
	$(PY) -c "import json,jsonschema; \
	  jsonschema.validate(json.load(open('results/rounds/$(CONF_ROUND)/conformance.json')), \
	                      json.load(open('schema/round-v1.schema.json'))); \
	  print('OK: conformance round JSON conforms to schema')"

## Copy round artifact + methodology + badges into $(STUDIO_DIR).
conformance-publish: conformance-validate
	$(PY) -m fhirbench.publish.copy_to_studio --round $(CONF_ROUND) --studio-dir $(STUDIO_DIR)
	$(PY) -m fhirbench.publish.badges --round $(CONF_ROUND) --studio-dir $(STUDIO_DIR)

## Quick text summary of the round to stdout.
conformance-summary:
	@$(PY) -m fhirbench.conformance.summary --round $(CONF_ROUND)

# ---------------------------------------------------------------------------
# Benchmark targets — fold a completed ramp into a round-v1 benchmark artifact.
# ---------------------------------------------------------------------------

## Shortcut: cell-summaries + parse + validate.
benchmark: benchmark-cell-summaries benchmark-parse benchmark-validate

## Emit cell_summary.json per (checkpoint, server) cell. Idempotent.
benchmark-cell-summaries:
	$(PY) -m fhirbench.benchmark.cell_summary \
	    --run-dir results/loadtest/$(BENCH_RUN_ID) --only-complete

## Walk results/loadtest/$(BENCH_RUN_ID)/ → results/rounds/$(BENCH_ROUND)/benchmark.json.
benchmark-parse:
	$(PY) -m fhirbench.benchmark.parse_report --round $(BENCH_ROUND) --run-id $(BENCH_RUN_ID)

## Validate benchmark.json against schema/round-v1.schema.json.
benchmark-validate:
	$(PY) -c "import json,jsonschema; \
	  jsonschema.validate(json.load(open('results/rounds/$(BENCH_ROUND)/benchmark.json')), \
	                      json.load(open('schema/round-v1.schema.json'))); \
	  print('OK: benchmark round JSON conforms to schema')"

## Copy benchmark round artifact + methodology into $(STUDIO_DIR).
benchmark-publish: benchmark-validate
	$(PY) -m fhirbench.publish.copy_to_studio --kind benchmark \
	    --round $(BENCH_ROUND) --studio-dir $(STUDIO_DIR)

## Quick text summary: p50 per (server, profile, checkpoint).
benchmark-summary:
	@$(PY) -m fhirbench.benchmark.summary --round $(BENCH_ROUND)
