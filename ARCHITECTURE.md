# Architecture

How the FHIR server comparison harness fits together. Read this once and you should be able to navigate the rest of the repo without surprises.

## In one paragraph

The repo brings up six FHIR servers via `docker compose`, runs two kinds of tests against them — a **conformance** matrix (FHIR TestScripts under `conformance/testscripts/`) and a **performance** matrix (load workloads under `profiles/benchmark/`) — folds the raw output into immutable JSON **round artifacts** under `results/rounds/<id>/`, and publishes those artifacts into a sibling repo (`fhir-studio`) that renders the public heatmap pages. All Python lives in one package (`fhirbench`); contributors install it with `pip install -e .` and use `make` for everyday operations.

## The six servers

| Id | Server | License | Port | Storage |
|----|--------|---------|------|---------|
| `hapi` | HAPI FHIR | Apache-2.0 | 8080 | Postgres |
| `aidbox` | Aidbox (free dev) | Proprietary, free dev tier | 8888 | Postgres |
| `medplum` | Medplum | Apache-2.0 | 8103 | Postgres + Redis |
| `msfhir` | Microsoft FHIR Server | MIT | 8081 | SQL Server |
| `blaze` | Blaze | Apache-2.0 | 8082 | RocksDB (embedded) |
| `spark` | Spark (Incendi) | BSD-3 | 8084 | MongoDB |

Server config lives in `config/servers.yaml`. Adding a server is an append-only change across four files — see `CONTRIBUTING.md`.

## System diagram

```
                   contributor / CI
                          │
                          ▼
                   ┌──────────────┐
                   │   Makefile   │  public targets (make help)
                   └──────┬───────┘
                          │ python -m fhirbench.<X>
                          ▼
   ┌──────────────────────────────────────────────────────────┐
   │                   src/fhirbench/  (one package)          │
   │                                                          │
   │   ┌──────────────┐    ┌──────────────┐                   │
   │   │ servers.py   │    │  compare.py  │  single-patient   │
   │   │  (auth, URL  │◄───┤              │  matrix           │
   │   │   discovery) │    └──────────────┘                   │
   │   └──────┬───────┘                                       │
   │          │ used by every harness module                  │
   │   ┌──────┴────────────┬───────────────┬──────────────┐   │
   │   ▼                   ▼               ▼              ▼   │
   │ harness/         conformance/     benchmark/     publish/│
   │ ramp.py          runner.py        parse_report   copy_to_│
   │ stage.py         run.py           cell_summary   studio  │
   │ loader.py        parse_report                    badges  │
   │ workload_*.py                                            │
   │ k6_driver.py                                             │
   │   ┌─────────────────┐                                    │
   │   │ k6/  (JS + py)  │  alternative Grafana k6 driver     │
   │   └─────────────────┘                                    │
   │                                                          │
   │ cli/  ad-hoc CLIs (emit_k6_context, fetch_versions)      │
   └─────────┬────────────────────────┬───────────────────────┘
             │ docker compose up      │ writes JSONL
             ▼                        ▼
   ┌─────────────────┐       ┌──────────────────┐
   │ 6 FHIR servers  │       │ results/loadtest │  raw per-op records
   │ (containers)    │◄──────┤ /<run-id>/...    │
   └─────────────────┘       └─────────┬────────┘
                                       │ benchmark.parse_report
                                       ▼
                            ┌──────────────────────┐
                            │ results/rounds/<id>/ │  IMMUTABLE round artifact
                            │  benchmark.json      │  (schema-validated)
                            │  conformance.json    │
                            │  MANIFEST.json       │  (sha256 hashes)
                            │  methodology.md      │
                            └──────────┬───────────┘
                                       │ publish.copy_to_studio
                                       ▼
                            ┌──────────────────────┐
                            │  ../fhir-studio      │  sibling repo
                            │  frontend renders    │  (mock.health
                            │  the heatmap pages   │   public site)
                            └──────────────────────┘
```

## Where things live

```
fhir-server-compare/
├── docker-compose.yml + 3 overlays   bring up the 6-server stack
├── Makefile                          public entry points (make help)
├── pyproject.toml                    package metadata
├── README.md                         one-page pitch + quickstart
├── ARCHITECTURE.md                   you are here
├── MIGRATION.md                      old-path → new-path table for forks
│
├── src/fhirbench/                    THE Python package
│   ├── servers.py                    config + auth helpers (used by everyone)
│   ├── compare.py                    behavioral one-patient matrix
│   ├── load_bundle.py                POST one Synthea patient to a server
│   ├── harness/                      multi-stage ramp + workloads
│   ├── benchmark/                    raw → round artifact aggregation
│   ├── conformance/                  FHIR TestScript runner
│   ├── publish/                      copy artifacts into fhir-studio
│   ├── cli/                          one-off CLI tools
│   └── k6/                           alternative k6 (Grafana) driver
│
├── config/                           runtime config (NOT code)
│   ├── servers.yaml                  the 6-server roster
│   ├── queries.yaml                  29 hand-picked queries with per-server expected behavior
│   └── medplum.config.json + medplum-lb.conf
│
├── profiles/                         workload profiles (YAML)
│   ├── benchmark/                    crud.yaml, search.yaml — performance workload shapes
│   └── conformance/                  per-profile MUST/SHOULD/MAY rules
│
├── conformance/testscripts/          FHIR TestScripts (one JSON per test)
│
├── docs/
│   ├── benchmark-methodology.md      how perf is measured (shipped to fhir-studio)
│   └── conformance-methodology.md    how conformance is scored (shipped to fhir-studio)
│
├── docker/                           Dockerfile bundles
│   ├── conformance-services/         AEGIS testscript-engine + auth proxies
│   └── spark-mongo-init/             Spark's MongoDB index init
│
├── schema/round-v1.schema.json       round artifact schema (single source of truth)
│
├── results/
│   ├── loadtest/<run-id>/...         raw ramp output (gitignored)
│   ├── conformance/<round>/<server>/ raw TestReports (gitignored)
│   └── rounds/<id>/                  publishable round artifacts (committed)
│
├── data/, synthea/                   patient data + generator (gitignored)
│
├── tests/                            pytest unit tests
└── .github/workflows/ci.yml          CI gates (imports, schema, compose)
```

## Two matrices, one round

Every published round contains **both** a conformance matrix (does the server implement spec X correctly?) and a benchmark matrix (how fast does the server handle workload Y at scale?). Both validate against `schema/round-v1.schema.json`. Both are immutable once published — `MANIFEST.json` carries sha256 hashes so a third party can verify the published artifacts haven't been edited after the fact.

A round id like `2026-q2-r000` decomposes as: `<year>-q<quarter>-r<NNN>` (zero-padded). The first official round of Q2 2026 is `r000`; corrections or re-runs increment to `r001`, `r002`, etc. The original is never overwritten.

## Two harnesses, one shape

The performance matrix can be driven by either a Python harness (the original) or a Grafana k6 harness (in-flight, see `ROADMAP.md`). Both produce the same `crud.jsonl` / `search.jsonl` shape that `fhirbench.benchmark.parse_report` understands, so the round artifact flow is harness-agnostic. The choice is `--workload-harness=python|k6` on `fhirbench.harness.ramp`.

## Adding things

- **A new server**: append-only change to `config/servers.yaml`, `docker-compose.yml`, `config/queries.yaml`, and `src/fhirbench/conformance/run.py`. See `CONTRIBUTING.md`.
- **A new conformance TestScript**: drop a JSON file under `conformance/testscripts/<profile>/<bucket>/`. The runner picks it up automatically.
- **A new benchmark workload**: extend `profiles/benchmark/<workload>.yaml` and the corresponding `fhirbench.harness.workload_<x>` module.
- **A new methodology**: changes to scoring or measurement go through a 30-day public RFC. See `GOVERNANCE.md`.

## Why this shape

- **Conformance is YAML + JSON, not code.** Test definitions are inert documents. The runner is in `src/fhirbench/conformance/`; the tests it executes live next to the FHIR spec they're checking, in `conformance/testscripts/`. Splitting these means a contributor adding a TestScript doesn't touch any Python.
- **Round artifacts are an API.** `results/rounds/<id>/{benchmark,conformance}.json` is consumed by `fhir-studio` and (in principle) anyone else who wants to render the data. Schema-validated, sha256-hashed, methodology-versioned. Don't break it.
- **One package, multiple entry points.** `src/fhirbench/` keeps everything Python in one place, but `pyproject.toml` exposes specific CLI surfaces (`fhirbench-ramp`, `fhirbench-compare`, etc.) for the workflows users actually invoke. Contributors don't need to know the internal module layout.
- **The Makefile is the public API.** `make help` lists every supported workflow. The Makefile delegates to `python -m fhirbench.<X>` invocations — if you want to script around the harness, use the underlying modules directly. The Makefile exists so a human can run `make conformance` without remembering the four-step pipeline.
