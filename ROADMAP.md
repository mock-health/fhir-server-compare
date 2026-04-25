# Roadmap

Where the FHIR Server Compare harness is going. Dates are targets, not commitments.

## Current state — v0 (2026-Q2)

- 7 OSS servers in the matrix: HAPI, Aidbox, Medplum, MS FHIR, Blaze, Spark, HFS.
- Single-patient behavioral matrix (`compare.py`) with 11 hand-picked queries.
- Conformance TestScripts for `fhir-r4-base`, `smart-on-fhir-v2`, and `bulk-data-v2` profiles, MUST/SHOULD/MAY buckets.
- Load test with 1K/4K/16K/64K checkpoint ladder, CRUD + Search workloads, ops/sec + p50 (median, headline) / p95 / p99 (tail evidence) latency + fairness metrics per server.
- Round artifacts (`results/rounds/<id>/*.json`) in the canonical `round-v1` schema, validated before publish.
- Copy-to-`fhir-studio` pipeline: atomic temp-dir rename, sha256 manifest, SVG badge generation.

## v1 — first official round (target: 2026-Q3)

- **Aidbox proxy-less auth.** The `auth-proxy-aidbox` sidecar is still in the conformance stack. The `aidbox.app/runme` reference config Health Samurai publishes uses a `BOX_*` env shape that forces every FHIR API call through the UI login flow (even with valid Basic credentials), so a clean drop of the proxy needs the right pairing of `BOX_SECURITY_DEV_MODE` / `BOX_SETTINGS_MODE` / init-bundle AccessPolicy. The init bundle (`conformance-docker/aidbox-init/initbundle.json`) is already mounted and provisions a `Client` + `AccessPolicy` on first boot, so the resources are pre-staged — only the env-shape sequencing is left.
- **k6 port of the load-test harness — shadow-run validation.** The k6 workloads are in the tree at [`loadtest/k6/`](./loadtest/k6/) (CRUD + Search, full parity with [`loadtest/workload_crud.py`](./loadtest/workload_crud.py) mix-mode and [`loadtest/workload_search.py`](./loadtest/workload_search.py)). A Python context emitter ([`scripts/emit_k6_context.py`](./scripts/emit_k6_context.py)) resolves `servers.yaml` + `queries.yaml` + env vars in one place so k6 never sees YAML. A post-processor ([`loadtest/k6/postprocess.py`](./loadtest/k6/postprocess.py)) reshapes k6's raw NDJSON into the `crud.jsonl` / `search.jsonl` the existing `cell_summary.py` + `parse_report.py` pipeline already understands, so the round artifact flow stays untouched. The shadow-run validator ([`scripts/compare_harnesses.py`](./scripts/compare_harnesses.py)) diffs two `benchmark.json` rounds cell-by-cell at ±10% p50 / ±0.5pp error rate / ±10% ok-throughput. Remaining work before the default runner flips from Python to k6: (1) wire `loadtest/ramp.py` to call `k6 run` per cell instead of the Python workloads, (2) run one shadow round across the full roster, (3) gate cutover on `compare_harnesses.py` → green. Phased CRUD mode (C/U/R/V/D) and Bulk Data async-polling are follow-ups; v0 ports only the mix-mode CRUD and the Search workload that the Python harness already publishes numbers for.
- **Cloud VM runner.** Move from a local laptop to a fixed AWS `c7i.4xlarge` (or equivalent) so hardware is consistent across rounds. Publish the AMI, kernel, Docker version, sysctl tuning.
- **100K-patient stage.** Current ramp tops out at 50K. Extend to 100K for the first "scale" round.
- **Additional workloads.** `$validate` against US Core profiles, `$everything` (patient summary), Bulk Data `$export`.
- **Realistic clinical queries.** "All HbA1c for diabetics with CKD on metformin" — the query shape that actually appears in production. No one else is benchmarking it.
- **Per-server profile pages.** Auto-generated from `servers.yaml` + the latest round's artifact.
- **Per-test explainer pages.** Each query and TestScript gets a permanent URL explaining what it's measuring and why.
- **SVG badges.** `/conformance/badges/<server>/<profile>.svg` rendered live; vendors embed in their READMEs (the viral bit).
- **CSV/JSON downloads** for every round, CDN-cached.
- **Scheduled GitHub Action** re-runs nightly on a small VM to catch regressions between official rounds.

## v2 — community ownership (target: 2026-Q4 → 2027-Q1)

- **Public PR submissions.** Any vendor PRs their server's Dockerfile + config; it enters the next round after review.
- **Quarterly cadence live.** March / June / September / December publication days, pre-announced.
- **Changelogs.** `/benchmark/changelog` and `/conformance/changelog` track every methodology change and round update.
- **FHIR DevDays talk.** "Benchmarking FHIR servers, independently" — propose for the next event.
- **Steering committee.** Invite HL7, HAPI maintainers, or a neutral party (Darren Devitt would be an ideal advisory-board pick) to co-steward. The moment the governance page has a non-mock.health name, adoption accelerates.

## v3 — annual headline rounds

- **The Million-Patient Round.** Once a year, scale-out test to 1M patients. Named artifact, pre-announced date, coordinated vendor tuning window.
- **Network round.** Current runs are all loopback-networked. Add a round that measures across realistic WAN latency — closer to production deployment shape.

## Workloads under consideration

Not yet in the matrix; order is indicative, not committed:

- `$validate` against US Core, USCDI, International Patient Summary
- SMART app launch flows (headless OAuth2 + PKCE flow against each server)
- Event notification / subscription throughput
- GraphQL FHIR (where supported) as a separate read-path benchmark
- `$translate` / `$lookup` terminology ops (careful — this is Health Samurai's wheelhouse; cite, don't duplicate)

## Servers under consideration

Round-ready candidates being evaluated for inclusion:

- **LinuxForHealth FHIR** (IBM lineage). Deferred from v0; valuable but resource-intensive to add.
- **FHIRbase 2.x** if the project un-freezes.
- **Pathling** for analytics-oriented comparisons.

Servers considered and dropped (recorded here so we don't re-litigate):

- **FHIRbase (frozen per maintainer; Aidbox represents the Postgres-extension lineage)**
- **Firely Server commercial** — 200-entry transaction bundle cap breaks Synthea loads; no documented public-benchmark-publication rights on the eval license. Spark covers the Firely lineage.
- **Ballerina FHIR** — language-specific module; audience is Ballerina users, not FHIR-evaluation teams.

## Non-goals

- Competing with Inferno on certification. This is a comparison harness, not a certification suite.
- Producing a single "best FHIR server" composite score for conformance. Conformance is a heatmap — single-scalar rankings would paper over dimensions that matter differently to different readers.
- Benchmarking managed cloud services (Azure, GCP, AWS HealthLake, Aidbox Cloud). The matrix promise is local reproducibility on any laptop; managed services break that promise by design. If the landscape shifts — e.g., a managed service publishes a locally-runnable preview image — we'll reconsider.
