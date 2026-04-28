# FHIR Server Compare

A runnable companion to the blog post [Same FHIR, Different Answers: Comparing 5 FHIR Servers](https://mock.health/blog/fhir-server-compare).

Load one Synthea patient into seven open-source FHIR servers, run the same queries against each, and see which servers agree and which diverge — on your own machine, against your own data. Every server runs locally via `docker compose up`; no paid licenses, no managed services, no cloud accounts.

| Server | Version | License | Image |
|--------|---------|---------|-------|
| HAPI FHIR | 8.8.0-1 | Apache-2.0 | `hapiproject/hapi` |
| Aidbox | 2603 (dev tier) | Proprietary, free dev license | `healthsamurai/aidboxone` |
| Medplum | 5.1.8 | Apache-2.0 | `medplum/medplum-server` + Postgres + Redis |
| Microsoft FHIR Server | 4.0.728 | MIT | `mcr.microsoft.com/healthcareapis/r4-fhir-server` + SQL Server |
| Blaze | 1.6.2 | Apache-2.0 | `samply/blaze` |
| Spark | 2.4.1-r4 | BSD-3 | `sparkfhir/spark` + MongoDB |

Images are pinned by sha256 digest in `docker-compose.yml` so the stack is byte-for-byte reproducible across machines and time.

## Quickstart

```bash
cp .env.example .env
# edit .env: paste your AIDBOX_LICENSE (free, no credit card, aidbox.app/signup)
docker compose up -d
# wait ~60s for all services to come up

pip install -e .
python -m fhirbench.load_bundle --server hapi
python -m fhirbench.load_bundle --server aidbox
python -m fhirbench.load_bundle --server medplum
python -m fhirbench.load_bundle --server msfhir
python -m fhirbench.load_bundle --server blaze
python -m fhirbench.load_bundle --server spark
python -m fhirbench.compare
```

`fhirbench.compare` probes every server in `config/servers.yaml` and only includes the ones that respond. Servers that aren't up or aren't reachable are skipped with a one-line log — no flags needed to opt out.

Want the fast path? Bring up just HAPI:

```bash
docker compose up -d hapi
pip install -e .
python -m fhirbench.load_bundle --server hapi
python -m fhirbench.compare
```

The matrix shows one column (HAPI) and the verdict column documents the expected behavior for the other six servers.

## What the matrix demonstrates

Each row backs up a specific claim in the blog post. The queries are the smallest set that surfaces every structural finding.

| # | Query | Claim |
|---|-------|-------|
| 1 | `capability_statement` | CapabilityStatement shape diff — every server returns a valid statement, but with different top-level fields. |
| 2 | `observation_search_default` | `Bundle.total` is `null` on HAPI / Medplum / MS FHIR by default; Aidbox / Blaze / Spark populate it. Servers disagree on whether counting is free. |
| 3 | `observation_search_total_accurate` | The fix — passing `_total=accurate` forces every compliant server to return the count. |
| 4 | `q7_error_unsupported_param` | **The silent-ignore.** Some servers 400 on a misspelled search parameter; others return 200 with the unfiltered result set. Most servers lie to you by default. |
| 5 | `observation_by_code` | `Observation?code=8480-6` (systolic BP) returns 0 on every server. Synthea encodes BP as a panel — the systolic code lives in `component[]`, not at the top level. |
| 6 | `q1_uscore_observation_combo` | The fix — `combo-code` matches `code` OR `component.code`. Returns the BP panels query #5 misses. |
| 7 | `q2_history_type` | `Patient/_history` works on HAPI / Aidbox / MS FHIR / Blaze / Spark. Medplum does not implement it. |
| 8 | `q6_expand_valueset` | `ValueSet/$expand` — implementation patchy across servers; status codes for "not loaded" vs "not implemented" vary widely. |
| 9 | `q6_lookup_loinc` | `CodeSystem/$lookup` — even bigger divergence (five status codes across six servers for the same operation). |
| 10 | `patient_revinclude_wildcard` | `_revinclude=*` — three camps: accept (HAPI / MS FHIR / Blaze), 400 (Medplum), 5xx (Aidbox / Spark). |
| 11 | `patient_export` | Bulk Data `Patient/$export`. 202 on servers that implement it; 400/404 on those that don't. |

Two additional rows are observed at **load time** rather than query time:

- **Transaction bundle size cap** — some servers reject transactions above N entries. Surfaces as a partial load in `load_bundle.py`'s output.
- **Canonical URL uniqueness** — some servers reject a second resource with the same canonical URL. Surfaces when you load the same bundle twice.

## Load test (multi-patient performance matrix)

The 1-patient behavioral matrix above lives next to a separate, larger test that ingests N Synthea patients and measures ops/sec, p99 latency, CPU, memory, and disk per server — mirroring the Health Samurai "Performance at Scale" methodology (1K → 100K → +1K incremental, CRUD + Batch + Search workloads).

The load-test stack is an overlay: HAPI switches from H2 to a dedicated Postgres, every server container gets an equal resource budget, and image digests are pinned for reproducibility.

```bash
# 10-patient smoke test — exercises every piece in ~2 minutes
make loadtest-dryrun

# Full ramp to 50K patients across all 7 servers (~12–16h elapsed)
make loadtest-ramp-50k
```

See `src/fhirbench/harness/` for the ingest loader, CRUD + search workload drivers, docker-stats resource sampler, stage orchestrator, and report generator. The search workload fires 23 queries uniformly at random per request, spanning seven FHIR routes (Patient, Observation, Condition, Procedure, Encounter, MedicationRequest, metadata) and eight filter shapes (token, string prefix, string exact, compound AND, date range, reference by patient / practitioner / location, and direct read-by-id). Ten of the queries are "runtime-sampled": their parameter values (patient ids, family names, diagnosis/procedure codes, practitioner and location ids) are drawn fresh per request from pools harvested against the target server at workload-start, so the numbers reflect cache-miss behavior on a live corpus rather than a hot 5-patient set. Per-query p50/p95/p99 is preserved in `evidence[].per_verb[]` in the round artifact. Results land under `results/loadtest/<run-id>/` as JSONL + a `summary.md` headline matrix.

## Conformance matrix

A parallel TestScript-based matrix checks each server against a collection of FHIR R4 base, SMART-on-FHIR, and Bulk Data v2 conformance profiles. Each cell is pass / fail / skipped with a spec citation.

```bash
make conformance-run       # execute TestScripts against all 7 servers
make conformance-parse     # fold into results/rounds/<id>/conformance.json
make conformance-validate  # schema-check the round artifact
```

## Files

| File | Purpose |
|------|---------|
| `pyproject.toml` | Package metadata. Run `pip install -e .` once; everything else uses the installed `fhirbench` package. |
| `Makefile` | Public entry points. `make help` lists every target. |
| `docker-compose.yml` | Brings up all 6 servers + their deps (images pinned by sha256). |
| `config/servers.yaml` | Per-server `base_url` + auth shape. Add a server by appending a block. |
| `config/queries.yaml` | 29 hand-picked queries with `expected_<server>` annotations per column (plus 10 runtime-sampled load-only entries). |
| `config/medplum.config.json`, `config/medplum-lb.conf` | Medplum server + nginx LB config. |
| `src/fhirbench/servers.py` | Shared config loader + auth shim used by every entry point. |
| `src/fhirbench/compare.py` | Probes servers, authenticates, loops queries, prints the matrix (`python -m fhirbench.compare`). |
| `src/fhirbench/load_bundle.py` | POSTs the transaction bundle to one server (`python -m fhirbench.load_bundle --server <id>`). |
| `src/fhirbench/harness/` | Multi-patient performance matrix: loader, workloads, report generator. |
| `src/fhirbench/conformance/` | TestScript runner + result parser. |
| `src/fhirbench/benchmark/` | Ramp-output → round-artifact aggregation. |
| `src/fhirbench/publish/` | Copies round artifacts into the `fhir-studio` frontend. |
| `src/fhirbench/cli/` | One-off CLI runners (k6 context emitter, harness diff, version fetcher). |
| `src/fhirbench/k6/` | k6 JavaScript harness + Python NDJSON post-processor. |
| `profiles/benchmark/`, `profiles/conformance/` | Workload profiles (YAML) consumed by the harness. |
| `conformance/testscripts/` | FHIR TestScripts (one JSON per test) per profile / requirement bucket. |
| `docs/benchmark-methodology.md`, `docs/conformance-methodology.md` | Methodology — shipped to fhir-studio on every publish. |
| `docker/` | Dockerfile bundles (conformance-services, spark-mongo-init). Compose files stay at root. |
| `schema/round-v1.schema.json` | Round artifact schema. |
| `.env.example` | Template for the credentials docker-compose needs. |

## Validation strictness varies per server

Each server enforces a different subset of FHIR R4 validation. Loading the same Synthea bundle produces different failures per server: Aidbox applies its own validation profile, Medplum is HAPI-permissive, MS FHIR is mid-strict, Blaze is structurally strict around terminology references. Run `python -m fhirbench.load_bundle --server <id>` against each and capture what fails — the observed strip-rule set **per server** is the story.

## Adding a server

1. Append a new entry to `config/servers.yaml` (set `base_url` and pick an `auth.type`).
2. Add an `expected_<newid>` column to every query in `config/queries.yaml`.
3. Add a service to `docker-compose.yml` (and pin the image by sha256 digest).
4. Run `python -m fhirbench.load_bundle --server <newid>` and `python -m fhirbench.compare` and fill in the observed behavior in `config/queries.yaml`.

See `CONTRIBUTING.md` for submission guidelines.

## Notes

- **One patient, not a thousand.** Structural divergence surfaces on the first query; volume is the performance story, not the correctness story.
- **Latency is informational, not benchmark-quality.** The single-patient matrix is cold-cache, single-threaded, sample-of-one. The load test is the serious performance story.
- **No writes except the initial load.** `compare.py` is GET-only (and `Patient/$export`, which is read-intent per the Bulk Data spec).
- **No credentials needed.** The `aidboxone:latest` image runs out-of-box with the reference starter config from `aidbox.app/runme`; everything else runs on your machine with zero-config defaults.
- **All OSS.** Every server in the matrix can be reproduced locally; no managed services, no paid tiers.

## Related work

This project is a neutral-territory observatory: one harness, every server, published numbers with methodology. It sits alongside vendor-run benchmarks, not in place of them. Two that are worth reading when working in this space:

- [**HealthSamurai/fhir-server-performance-benchmark**](https://github.com/HealthSamurai/fhir-server-performance-benchmark) — the Aidbox team's own benchmark (k6, multi-vendor, CI-driven). mock.health's Aidbox configuration, index set, and `BOX_FHIR_SEARCH_DEFAULT_PARAMS_TOTAL` setting mirror their recommendations verbatim — see [`docs/benchmark-methodology.md`](docs/benchmark-methodology.md#vendor-recommended-configuration).
- Vendor-specific performance docs linked per-server in [`docs/benchmark-methodology.md`](docs/benchmark-methodology.md).

If you run a FHIR server and have a recommended configuration, benchmark result, or methodology note you'd like reflected here, open an issue or PR.
