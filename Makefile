# Public convenience targets. Wrap the Python commands the README and
# CONTRIBUTING docs reference so `make <target>` works out of the box.
#
# Everything here delegates to python modules under loadtest/ and
# loadtest/conformance/ and loadtest/benchmark/. Nothing lives in this
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
SERVERS           ?= hapi blaze aidbox medplum msfhir spark hfs
WORKERS_INGEST    ?= 32
WORKERS_WORKLOAD  ?= 64
WORKLOAD_DURATION ?= 900
RAMP_CHECKPOINTS      ?= 1024,4096,8192,16384,32768,65536,131072
RAMP_CHECKPOINTS_50K  ?= 1000,4000,16000,64000

COMPOSE := docker compose -f docker-compose.yml -f docker-compose.loadtest.yml

.PHONY: help \
        loadtest-dryrun loadtest-dryrun-k6 loadtest-ramp loadtest-ramp-50k loadtest-ramp-100k \
        k6-context k6-crud k6-search k6-compare k6-ramp k6-ramp-50k \
        shadow-dryrun shadow-1k \
        conformance conformance-run conformance-parse conformance-validate \
        conformance-publish conformance-summary \
        benchmark benchmark-cell-summaries benchmark-parse benchmark-validate \
        benchmark-publish benchmark-summary

help:
	@echo "Public targets (see README.md and CONTRIBUTING.md for context):"
	@echo ""
	@echo "Load test:"
	@echo "  make loadtest-dryrun     10-patient smoke (~2 min). Validates the full pipeline."
	@echo "  make loadtest-dryrun-k6  Same smoke, driven by the k6 harness instead of Python."
	@echo "  make loadtest-ramp-50k   50K ramp, checkpoints 1K/4K/16K/64K, all servers. ~12-16h."
	@echo "  make loadtest-ramp-100k  Full 100K+ ramp per RAMP_CHECKPOINTS. ~24-30h."
	@echo ""
	@echo "k6 harness (shadow-run validation):"
	@echo "  make k6-context  SERVER=hapi WL=search  Emit loadtest/k6/k6_context.json."
	@echo "  make k6-crud     SERVER=hapi            Run k6 CRUD workload against one server."
	@echo "  make k6-search   SERVER=hapi            Run k6 Search workload against one server."
	@echo "  make k6-compare  PY_ROUND=<round.json> K6_ROUND=<round.json>"
	@echo "                                          Diff two round artifacts cell-by-cell."
	@echo "  make shadow-dryrun                      Fast shadow (10 patients, hapi only) — ~3 min."
	@echo "  make shadow-1k                          Full shadow (1K patients, all servers) — ~40 min."
	@echo "  make k6-ramp                            K6-ONLY ramp (no Python workloads). 1K patients,"
	@echo "                                          all servers. Ingests via loadtest.loader."
	@echo "  make k6-ramp-50k                        K6-ONLY full ramp: 1K/4K/16K/64K checkpoints,"
	@echo "                                          6 servers (HFS excluded). ~12-16h."
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
# Load test — the Python orchestrator owns the per-server up/wait/stage/stop
# lifecycle so Make only invokes one command per ramp.
# ---------------------------------------------------------------------------

## 10-patient smoke test: reset all volumes, generate 10 patients, run Stage 1
## (ingest + CRUD + Search workloads) against each server serially, render report.
loadtest-dryrun:
	$(COMPOSE) down -v
	$(PY) -m loadtest.generate --count 10
	$(PY) -m loadtest.ramp --run-id dryrun-10p \
	    --checkpoints "10" \
	    --servers "$$(echo '$(SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration 30
	$(PY) -m loadtest.report --run-id dryrun-10p

# ---------------------------------------------------------------------------
# k6 harness — runs alongside the Python harness during the shadow-run
# validation phase (ROADMAP v1). Each target drives ONE server. The ramp
# orchestrator will wire k6 into the multi-server loop once parity is
# validated; for now these stay as manual one-server invocations.
#
# Inputs:
#   SERVER=<id>            — server id from servers.yaml (required)
#   WL=crud|search         — workload (k6-context default: search)
#   WORKLOAD_DURATION      — inherits from the top-level var, default 900
#   WORKERS                — defaults to 64 inside the k6 scripts
# ---------------------------------------------------------------------------

K6_DIR          := loadtest/k6
K6_CTX          := $(K6_DIR)/k6_context.json
K6_OUT_DIR      := results/k6
K6_DURATION     ?= $(WORKLOAD_DURATION)

## Re-emit k6_context.json for one server + one workload. SERVER and WL required.
## Example:  make k6-context SERVER=hapi WL=search
k6-context:
	@[ -n "$(SERVER)" ] || (echo 'ERROR: set SERVER=<id>' >&2; exit 2)
	@[ -n "$(WL)" ]     || (echo 'ERROR: set WL=crud|search' >&2; exit 2)
	$(PY) -m scripts.emit_k6_context \
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
	$(PY) -m loadtest.k6.postprocess \
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
	$(PY) -m loadtest.k6.postprocess \
	    --k6-json $(K6_NDJSON) --workload search --out $(K6_JSONL)

## Same shape as loadtest-dryrun but k6-driven. Generates 10 patients, ingests
## them against SERVER, runs both k6 workloads, converts the raw NDJSON into
## the crud.jsonl / search.jsonl the rest of the pipeline reads. Short
## duration so the whole loop finishes in a couple of minutes.
loadtest-dryrun-k6: SERVER ?= hapi
loadtest-dryrun-k6:
	$(COMPOSE) down -v
	$(PY) -m loadtest.generate --count 10
	$(MAKE) k6-crud   SERVER=$(SERVER) K6_DURATION=30
	$(MAKE) k6-search SERVER=$(SERVER) K6_DURATION=30

## Diff a Python-produced round against a k6-produced round.
## Example:
##   make k6-compare PY_ROUND=results/rounds/2026-q2-r100/benchmark.json \
##                   K6_ROUND=results/rounds/2026-q2-r101/benchmark.json
k6-compare:
	@[ -n "$(PY_ROUND)" ] || (echo 'ERROR: set PY_ROUND=<path>' >&2; exit 2)
	@[ -n "$(K6_ROUND)" ] || (echo 'ERROR: set K6_ROUND=<path>' >&2; exit 2)
	$(PY) -m scripts.compare_harnesses --python $(PY_ROUND) --k6 $(K6_ROUND)

# ---------------------------------------------------------------------------
# Shadow round + k6-only ramp.
#
# Both are thin wrappers around `loadtest.ramp --workload-harness <python|k6>`
# — the Python ramp owns the full per-server lifecycle (reset → boot → wait
# → bootstrap → ingest → workloads → cell_complete → stop). All the Make
# layer does is pick the harness and stitch the benchmark / compare steps
# on at the end.
#
# Ramps are independent runs: each has its own --run-id and ramp.py resets
# volumes cold for each server at the start of its own run. That means
# shadow-1k re-ingests once per harness — ~2x the total ingest cost of a
# one-harness ramp, but the two measurements are honestly isolated.
# ---------------------------------------------------------------------------

# Override defaults at the CLI: `make shadow-1k SHADOW_N=1000 SHADOW_DURATION=120 …`.
SHADOW_SERVERS  ?= hapi aidbox medplum msfhir blaze spark hfs
SHADOW_N        ?= 1000
SHADOW_DURATION ?= 120
# Shadow runs reserve round ids 2026-q2-r900 (Python) and 2026-q2-r901 (k6)
# from the pattern required by schema/round-v1.schema.json
# (^[0-9]{4}-q[1-4]-r[0-9]{3}$). r900+ is unallocated for real rounds; rerun
# with `make shadow-1k SHADOW_PY_ROUND=...` if you want a different slot.
SHADOW_PY_RUN   ?= shadow-$(SHADOW_N)-py
SHADOW_K6_RUN   ?= shadow-$(SHADOW_N)-k6
SHADOW_PY_ROUND ?= 2026-q2-r900
SHADOW_K6_ROUND ?= 2026-q2-r901

## Fast shadow: 10 patients, hapi only. Use to validate the full pipeline
## (generate → Python workloads → k6 workloads → two rounds → compare) in
## ~3 minutes. Not for publishing — numbers are too small to trust
## quantiles. If this passes, `shadow-1k` is the real validation.
shadow-dryrun:
	$(MAKE) shadow-1k \
	    SHADOW_SERVERS=hapi SHADOW_N=10 SHADOW_DURATION=30 \
	    SHADOW_PY_RUN=shadow-dryrun-py SHADOW_K6_RUN=shadow-dryrun-k6 \
	    SHADOW_PY_ROUND=2026-q2-r990 SHADOW_K6_ROUND=2026-q2-r991

## Full shadow: SHADOW_N patients, every server in SHADOW_SERVERS, both
## harnesses, diff at the end. Each harness runs a full isolated ramp
## (cold volume → ingest → workloads → stop); expect ~30-45 min at N=1000
## DURATION=120 across 7 servers.
shadow-1k:
	$(PY) -m loadtest.ramp --run-id $(SHADOW_PY_RUN) \
	    --checkpoints "$(SHADOW_N)" \
	    --servers "$$(echo '$(SHADOW_SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) \
	    --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration $(SHADOW_DURATION) \
	    --workload-harness python
	$(MAKE) benchmark BENCH_RUN_ID=$(SHADOW_PY_RUN) BENCH_ROUND=$(SHADOW_PY_ROUND)
	$(PY) -m loadtest.ramp --run-id $(SHADOW_K6_RUN) \
	    --checkpoints "$(SHADOW_N)" \
	    --servers "$$(echo '$(SHADOW_SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) \
	    --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration $(SHADOW_DURATION) \
	    --workload-harness k6
	$(MAKE) benchmark BENCH_RUN_ID=$(SHADOW_K6_RUN) BENCH_ROUND=$(SHADOW_K6_ROUND)
	$(MAKE) k6-compare \
	    PY_ROUND=results/rounds/$(SHADOW_PY_ROUND)/benchmark.json \
	    K6_ROUND=results/rounds/$(SHADOW_K6_ROUND)/benchmark.json

## k6-only ramp: single `loadtest.ramp --workload-harness k6` invocation.
## No Python workloads, no shadow compare. Use when you already have Python
## baselines in version control and just want fresh k6 numbers. The ramp
## itself handles reset / boot / ingest / bootstrap / workloads / stop per
## server — no Make-level orchestration.
K6_RAMP_RUN   ?= k6-only-$(SHADOW_N)
K6_RAMP_ROUND ?= 2026-q2-r902

k6-ramp:
	$(PY) -m loadtest.ramp --run-id $(K6_RAMP_RUN) \
	    --checkpoints "$(SHADOW_N)" \
	    --servers "$$(echo '$(SHADOW_SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) \
	    --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration $(SHADOW_DURATION) \
	    --workload-harness k6
	$(MAKE) benchmark BENCH_RUN_ID=$(K6_RAMP_RUN) BENCH_ROUND=$(K6_RAMP_ROUND)
	@echo ""
	@echo "K6 round ready: results/rounds/$(K6_RAMP_ROUND)/benchmark.json"

## Full-ladder k6-only ramp: 1K/4K/16K/64K checkpoints × 6 servers (HFS
## excluded — Round-2 spike, separate methodology). Publishes to a
## distinct round id so it sits alongside earlier Python-harness rounds
## in results/rounds/ rather than overwriting. Default WORKLOAD_DURATION
## (900s = 15min per workload per cell) is inherited from the top; total
## ramp wall-clock lands in the ~12-16h bracket like loadtest-ramp-50k.
K6_RAMP_50K_SERVERS ?= hapi aidbox medplum msfhir blaze spark
K6_RAMP_50K_RUN     ?= k6-ramp-50k
K6_RAMP_50K_ROUND   ?= 2026-q2-r903

k6-ramp-50k:
	$(PY) -m loadtest.ramp --run-id $(K6_RAMP_50K_RUN) \
	    --checkpoints "$(RAMP_CHECKPOINTS_50K)" \
	    --servers "$$(echo '$(K6_RAMP_50K_SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) \
	    --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration $(WORKLOAD_DURATION) \
	    --workload-harness k6
	$(MAKE) benchmark BENCH_RUN_ID=$(K6_RAMP_50K_RUN) BENCH_ROUND=$(K6_RAMP_50K_ROUND)
	@echo ""
	@echo "K6 50K round ready: results/rounds/$(K6_RAMP_50K_ROUND)/benchmark.json"

## End-to-end ramp. Checkpoints are cumulative patient counts; at each one every
## server gets a cold-start measurement (DB reset, ingest N, run workloads, stop).
loadtest-ramp: RUN_ID ?= $(shell date +%Y-%m-%d)-ramp
loadtest-ramp:
	$(PY) -m loadtest.ramp --run-id $(RUN_ID) \
	    --checkpoints "$(RAMP_CHECKPOINTS)" \
	    --servers "$$(echo '$(SERVERS)' | tr ' ' ',')" \
	    --workers-ingest $(WORKERS_INGEST) --workers-workload $(WORKERS_WORKLOAD) \
	    --workload-duration $(WORKLOAD_DURATION)
	$(PY) -m loadtest.report --run-id $(RUN_ID)

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
	$(PY) -m loadtest.conformance.run --round $(CONF_ROUND) --server all \
	    --testscripts conformance/testscripts/fhir-r4-base
	$(PY) -m loadtest.conformance.run --round $(CONF_ROUND) --server all \
	    --testscripts conformance/testscripts/bulk-data-v2

## Fold TestReports into results/rounds/$(CONF_ROUND)/conformance.json.
conformance-parse:
	$(PY) -m loadtest.conformance.parse_report --round $(CONF_ROUND)

## Validate the round artifact against schema/round-v1.schema.json.
conformance-validate:
	$(PY) -c "import json,jsonschema; \
	  jsonschema.validate(json.load(open('results/rounds/$(CONF_ROUND)/conformance.json')), \
	                      json.load(open('schema/round-v1.schema.json'))); \
	  print('OK: conformance round JSON conforms to schema')"

## Copy round artifact + methodology + badges into $(STUDIO_DIR).
conformance-publish: conformance-validate
	$(PY) -m loadtest.publish.copy_to_studio --round $(CONF_ROUND) --studio-dir $(STUDIO_DIR)
	$(PY) -m loadtest.publish.badges --round $(CONF_ROUND) --studio-dir $(STUDIO_DIR)

## Quick text summary of the round to stdout.
conformance-summary:
	@$(PY) -m loadtest.conformance.summary --round $(CONF_ROUND)

# ---------------------------------------------------------------------------
# Benchmark targets — fold a completed ramp into a round-v1 benchmark artifact.
# ---------------------------------------------------------------------------

## Shortcut: cell-summaries + parse + validate.
benchmark: benchmark-cell-summaries benchmark-parse benchmark-validate

## Emit cell_summary.json per (checkpoint, server) cell. Idempotent.
benchmark-cell-summaries:
	$(PY) -m loadtest.benchmark.cell_summary \
	    --run-dir results/loadtest/$(BENCH_RUN_ID) --only-complete

## Walk results/loadtest/$(BENCH_RUN_ID)/ → results/rounds/$(BENCH_ROUND)/benchmark.json.
benchmark-parse:
	$(PY) -m loadtest.benchmark.parse_report --round $(BENCH_ROUND) --run-id $(BENCH_RUN_ID)

## Validate benchmark.json against schema/round-v1.schema.json.
benchmark-validate:
	$(PY) -c "import json,jsonschema; \
	  jsonschema.validate(json.load(open('results/rounds/$(BENCH_ROUND)/benchmark.json')), \
	                      json.load(open('schema/round-v1.schema.json'))); \
	  print('OK: benchmark round JSON conforms to schema')"

## Copy benchmark round artifact + methodology into $(STUDIO_DIR).
benchmark-publish: benchmark-validate
	$(PY) -m loadtest.publish.copy_to_studio --kind benchmark \
	    --round $(BENCH_ROUND) --studio-dir $(STUDIO_DIR)

## Quick text summary: p50 per (server, profile, checkpoint).
benchmark-summary:
	@$(PY) -m loadtest.benchmark.summary --round $(BENCH_ROUND)
