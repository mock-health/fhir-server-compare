# Performance methodology — v1.0-draft

> Independent. Reproducible. Continuously run. No dog in the fight.

This page describes how mock.health's `/performance` heatmap is generated. Every cell traces back to JSONL-per-request records from a deterministic ramp run; this document is the contract.

## What I measure

For each local FHIR R4 server in the roster I drive three workloads at escalating synthetic-population sizes and record per-request latency. The workloads (called **profiles** here to match the `/conformance` shape) are:

| Profile | Measures | Headline metric |
|---|---|---|
| **CRUD** | Mixed C/R/U/D over five FHIR resource types (Patient / Observation / Condition / Encounter / MedicationRequest), driven by 64 concurrent workers at steady state | p50 (median) latency across all verbs |
| **Search** | 30 queries spanning eight FHIR routes and four search classes (Simple / Complex / Full-text / Operation), driven concurrently | ok-only p50 (median) latency across 2xx responses |
| **Ingest** | Transaction Bundle POST against the base URL | bundles per second (ok-only throughput) |

p95 and p99 are captured in every evidence row as tail evidence, but the headline — the number that colors the heatmap cell and drives the scaling curve — is the median for latency profiles, and ok-only throughput for Ingest. See **[Why the median?](#why-the-median)** below for CRUD/Search; the next section covers why Ingest's headline is throughput instead.

Every per-checkpoint evidence row also carries a `per_verb[]` array. As of 2026-04-30 each `per_verb` item additionally carries two optional dimensions — `resource_type` (FHIR type the verb operated on) and `complexity` (search-class label). CRUD splits its four `verb` values into one row per (verb, resource_type) pair, and Search rolls up by class via the `complexity` field. See [Per-verb dimensions](#per-verb-dimensions) for the full data model.

## Ingest as transaction Bundles

Transaction Bundles are the *interactive* write path that real FHIR pipelines hit on every inbound message. HL7v2→FHIR conversion in particular turns each inbound HL7v2 message into one transaction Bundle of correlated mutations (Patient + Encounter + Observation + …) — bundles per second is what those pipelines actually feel. This benchmark was added at the suggestion of Ralph at haste.health, who asked for an open transaction-Bundle benchmark all vendors can measure against.

Why throughput, not latency, as the headline: Synthea bundles vary roughly 10× in entry count (50–500 entries), so per-bundle latency is partly a function of bundle size and not just server speed. Throughput — bundles applied per second under the loader's 32-worker pool — normalizes that. The per-Bundle p50/p95/p99 latencies remain available as tail evidence in each evidence row.

This is **not** a replacement for `$import`. `$import` is the spec-blessed bulk-load path for cold-start migrations and is correctly recommended for one-shot population. Transaction Bundles are the production write API. They're different workloads; the matrix publishes the production one because that's what an integration engineer feels every day.

## Size ladder

The ramp ladder steps through four checkpoints of cumulative patient count:

**1 000 → 4 000 → 16 000 → 64 000**

Between checkpoints the population grows incrementally (not wiped). At each checkpoint the warm server runs all three workloads. The resulting per-(server, profile, checkpoint) p50 series is what gets plotted on a log-log axis — a true power-law scaling shows up as a straight line.

Not every cell is filled. Spark in particular has only completed the 1K checkpoint so far; higher checkpoints show grey. This is deliberate honesty, not a bug.

## Hardware

Captured per run in `meta.json` and shown on the round page:

- AMD Ryzen 9 9950X (16 physical cores / 32 threads)
- 192 GiB RAM, swappiness=1, THP=`madvise`, governor=`performance`
- ext4 on NVMe
- Docker 29.2.1

Server stacks pinned via `cpuset_cpus: "0-11,16-27"` (12 physical cores). Cores 12–15 are reserved for the loader/OS/sampler so loader↔server SMT contention can't taint results. Each server gets 12 CPU + 32 GiB RAM; its backing DB (where separate) gets 6 CPU + 16 GiB.

## Query inventory (Search profile)

The search workload picks uniformly at random per request from a 30-query pool spanning seven FHIR routes (Patient, Observation, Condition, Procedure, Encounter, MedicationRequest, Metadata) and four search classes:

| Class | Count | Examples |
|---|---|---|
| **SIMPLE** — single-parameter token / string / date / reference | 18 | `patient_by_gender`, `observation_by_code`, `condition_by_code`, `observations_for_patient`, `observation_recent` (date range) |
| **COMPLEX** — multi-parameter, compound AND, `_include` / `_revinclude` | 2 | `patient_by_gender_family`, `q1_uscore_observation_combo` |
| **FULL_TEXT** — `_content` or `:text` modifier | 7 | `patient_content_search`, `observation_content_search`, `condition_code_text_modifier`, `patient_name_text_modifier`, `patient_text_narrative` |
| **OPERATION** — FHIR ops (`/metadata`, `_history`, `$expand`, `$lookup`, `$export`) | 1 | `capability_statement` |

(Counts above reflect the load-test pool — queries marked `loadtest: skip:<reason>` in `queries.yaml`, e.g. `patient_export` async, `q6_expand_valueset` terminology-not-loaded, `q7_error_unsupported_param` intentional-4xx, are excluded from the load mix but remain in the single-patient behavior matrix.)

Of the 30 load-pool queries, 13 use static parameter values; the other 17 are **runtime-sampled**: their `{{placeholder}}` values are drawn fresh per request from pools harvested against the target server at workload-start (patient ids, family / given names, condition / procedure / medication codes, practitioner and location ids). This measures cache-miss behavior on a live corpus rather than a hot 5-patient set. Per-query p50 / p95 / p99 are preserved in `evidence[].per_verb[]`.

The full per-query metadata — `complexity`, `resource_type`, `loadtest` skip rules, `expected_<server>` — lives in `config/queries.yaml` and is the single source of truth.

## Why the median?

Short version: each cell is a **2-minute run**, and a reliable tail quantile needs a lot of samples. Rule of thumb: a quantile q needs at least ~`10/(1-q)` samples to be stable, ideally ten times that.

- p99 needs ~1 000 samples minimum, ~10 000 for low noise.
- p95 needs ~200 minimum.
- p50 (the median) is stable at ~20.

For a fast cell — say HAPI CRUD at 1K — 2 minutes easily produces tens of thousands of samples, and any of these statistics is fine. But for a slow cell — Medplum search at 1K, where a single request can take multiple seconds — the same 2-minute window produces only tens of samples. A p99 computed from 30 requests is essentially `max(30 numbers)`: it moves wildly run-to-run and isn't a latency *distribution* estimate, it's an outlier reading. That's how you get apparent anomalies like "server X's p99 went *down* when the population grew 4×" — it didn't, the estimator just got noisier.

We don't want to hide the tail, but we don't want the headline to be a number we can't honestly defend. Solution: headline on the median (stable even in small samples), and keep p95 and p99 in every evidence row as tail evidence — visible in raw JSONL and round JSON, just not leading the story.

For the search profile the headline uses the **ok-only** percentile stream: latencies across 2xx responses only. Including 4xx responses would reward vendors that fail fast on unsupported queries — the opposite of the signal we want. CRUD and Ingest both expect every op to succeed, so they headline on the all-responses median — a 4xx ingest is a real failure, not a feature. The raw JSONL preserves both streams. Ingest's headline is **ok-only throughput** (bundles/sec of successful 2xx Bundle POSTs); the per-Bundle median latency rides along as tail evidence.

## Warmup

CRUD and Search workloads run a 30-second untimed warmup before the measurement window opens. JVM JIT (HAPI, Blaze), query planner caches (Postgres, Aidbox), and index warmth (MongoDB, Elasticsearch) all benefit. Ingest has no warmup because the first-write path is a real user experience.

## Vendor-recommended configuration

Each server in the roster runs with the configuration its own vendor documents (or benchmarks against). Leaving a vendor on a default that their documentation explicitly recommends against would measure "did the image ship the right knob flipped?" rather than "how fast is the engine?" — so we flip the knobs the vendor tells us to. Every such knob is listed here and in the compose file.

| Server | Setting | Source |
|---|---|---|
| **MS FHIR** | `x-bundle-processing-logic: parallel` request header on ingest | [Azure FHIR best-practices docs](https://learn.microsoft.com/en-us/azure/healthcare-apis/fhir/fhir-best-practices) |
| **Aidbox** | `BOX_FHIR_SEARCH_DEFAULT_PARAMS_TOTAL=none` (disable implicit `_total=accurate`) | [Health Samurai benchmark config](https://github.com/HealthSamurai/fhir-server-performance-benchmark/blob/main/ci_search_suite.yaml) |
| **Aidbox** | Full index set (pg_trgm + GIN `jsonb_path_ops` on 11 tables + Patient name trigram + birthdate btree) | [HealthSamurai `initbundle.json`](https://github.com/HealthSamurai/fhir-server-performance-benchmark/blob/main/infra/aidbox/initbundle.json) |
| **Aidbox** | Anonymous `AccessPolicy` installed via `BOX_INIT_BUNDLE` | Same pattern as upstream benchmark |
| **HAPI** | Embedded Lucene full-text indexing (`hapi.fhir.advanced_lucene_indexing=true`, `advanced_hsearch_indexing=true`, `lastn_enabled=true`) | [HAPI FHIR docs — Server Configurations / Lucene](https://hapifhir.io/hapi-fhir/docs/server_jpa/configuration.html#lucene--elasticsearch) |
| **Spark** | Write-path + read-path Mongo indexes (see next section) | Authored locally by matching Spark's query patterns; no vendor doc exists |

The HAPI Lucene knobs were added on 2026-04-30 after the marat-1k-shadow-v2 run showed `_content` and `code:text` queries returning HTTP 400 against vanilla HAPI. The methodology promised "Lucene-backed full-text"; the embedded backend (no Elasticsearch sidecar) delivers it. Cost: ~5–10% slower ingest from per-resource Lucene index writes, plus a few hundred MB of additional Postgres index storage. Both are documented in this row so the HAPI numbers are not "vanilla HAPI 8.8" but "HAPI 8.8 with the full-text knobs the docs recommend." Servers that don't ship Lucene (Medplum, Blaze, Spark) remain on their stock full-text behavior — the FULL_TEXT class will surface that asymmetry in the cells, which is the point.

Queries that want to pay the `_total=accurate` tax request it explicitly in the URL (e.g. `observation_search_total_accurate` in `benchmark/profiles/search.yaml`) — a URL-level parameter overrides the server default, so the "what does an accurate total cost?" measurement is still honest.

## Server-specific index bootstrap (Aidbox, Spark)

Two servers require an operator to create the backing search indexes manually. Leaving them unindexed would measure "did the vendor ship indexes?" rather than "how fast is the engine once it's configured like an operator would run it in prod?" — a distinction the benchmark makes explicitly rather than hiding.

- **Aidbox** (community edition 2603) ships every per-resource Postgres table with only a primary-key index. Every FHIR search translates to `WHERE resource @> '<jsonb>'`, so without a GIN index on the `resource` column each query is a `Seq Scan` across the full table — 12 s per query on a 63 K-patient corpus, climbing to timeouts at 64 K. Reproducible via `EXPLAIN` against any aidbox install of that version.
  - **Bootstrap step:** `loadtest/aidbox_bootstrap.py` runs during ramp, after `docker compose up aidbox` + wait-healthy and before bundle ingest. The index set is a verbatim port from Health Samurai's own benchmark — [HealthSamurai/fhir-server-performance-benchmark/infra/aidbox/initbundle.json](https://github.com/HealthSamurai/fhir-server-performance-benchmark/blob/main/infra/aidbox/initbundle.json) plus the refinements in [`ci_search_suite.yaml`](https://github.com/HealthSamurai/fhir-server-performance-benchmark/blob/main/ci_search_suite.yaml) — so the benchmark measures Aidbox against Aidbox's own recommended configuration, not against our guess at one. Nineteen statements in total: `pg_trgm` extension + GIN(`resource jsonb_path_ops`) on 11 resource tables + 4 Patient name indexes (trigram + plain via Aidbox's `aidbox_text_search`/`knife_extract_text`) + 3 Patient birthdate btree indexes (min/max/compound via `knife_extract_min_timestamptz`/`knife_extract_max_timestamptz`). Applied via Aidbox's documented `/$sql` admin endpoint; idempotent; sentinel at `<run>/aidbox_indexed.json`.
  - Creating the indexes on empty tables is instant. Postgres maintains them automatically as ingest writes rows — the same steady-state behavior HAPI/Medplum/MS FHIR get for free from their ORMs' schema management.
  - **Finding (reported separately on `/performance`):** default-config aidbox search p90 at 64 K is ~56 s with 94 % error rate; with the bootstrap applied it drops ≥1,000×. The out-of-box number is the default-config reality; the bootstrapped number is the engine's real capability. Both are published.
- **Spark** uses MongoDB, which ships without either write-path or read-path indexes on the schema Spark creates. `spark-mongo-init/01-create-indexes.js` is bind-mounted into the container's `docker-entrypoint-initdb.d` so MongoDB auto-creates them on first startup of a fresh volume. Two index sets:
  - **Write-path** (3 indexes): `searchindex.internal_id` unique, `resources.{@typename, id, @VersionId}`, `resources.{@REFERENCE, @state}`. Without them, transaction-bundle ingest COLLSCANs per upsert / per version-walk / per supercede-previous-version — measured at ~4 bundles/min and collapsing to near-zero at a few hundred patients. With them, ingest keeps pace with the loader.
  - **Read-path** (14 compound indexes on `searchindex`): `(internal_resource, <search_param>)` for `gender`, `family`, `given`, `code`, `combo-code`, `subject`, `patient`, `date`, `identifier`, `practitioner`, `location`, `fhir_id`, `_lastUpdated.start`, plus a `(internal_resource)` prefix index. Without these, every FHIR search (e.g. `Observation?code=8302-2`) COLLSCANs the entire `searchindex` collection (~3M docs at 1K patients); p50 sits in the 7 s range at 1K.
  - **Finding (reported separately on `/performance`):** default-config Spark search p50 at 1K is ~7 s with ~9% errors; with the read-path bootstrap applied p50 drops to ~380 ms (~19× faster) and throughput rises ~2.3×. Tail / err% remain elevated — specific queries (wildcard `_revinclude`, deep back-references) time out at 60s regardless of indexing — so Spark's search cell remains disqualified at 1K. Both numbers are published; the disqualification is honest regardless of which config runs.

**Caveat.** 30 s is not enough for *cold-plan* statistics on some Postgres-backed servers. If the ingest that populated the checkpoint just finished, `ANALYZE`/autovacuum may not have built good stats yet, and queries that plan differently with vs. without stats (classically `_total=accurate`, which forces a `COUNT(*)`) can take 100× longer on the first checkpoint than on later ones. In this round, Medplum's `observation_search_total_accurate` median dropped from 1,790 ms at 1K to 17 ms at 4K — not a scaling anomaly, just the planner catching up between checkpoints. A 1K cell's overall p50 can therefore be *worse* than a larger-N cell's if one query dominates the mix. The per-verb breakdown in `evidence[].per_verb[]` makes this visible at a glance — pick the affected verb in the frontend's verb chip row and the curve flattens.

## Overall p50 vs. per-verb p50

The headline p50 on each cell is the median across every query in the workload combined — a useful headline, but a noisy one when the queries have very different performance profiles. A workload with most queries fast and a handful of slow ones has a bimodal latency distribution; small shifts in the slow tail can move the overall median dramatically. When a curve looks non-monotonic, the first question to ask is *which verb changed*, not *is scaling broken*. The JSONL answers that directly: `jq '.verb, .duration_ms' search.jsonl`.

The round artifact also surfaces this without dropping to JSONL: `evidence[].per_verb[]` is sorted by `(verb, resource_type, complexity)` and carries per-row p50 / p95 / p99 / err / n. The frontend's `/performance/workloads/<id>` page exposes filter chips for each dimension; `/performance/servers/<id>/<workload>` does the same for one server in focus.

## Per-verb dimensions

The `per_verb` array shape evolved on 2026-04-30 to carry two optional dimensions alongside `verb` so the same query can split into multiple rows when the new fields differentiate it. Concretely:

- **`resource_type`** — FHIR resource type the verb operated on (`Patient`, `Observation`, `Condition`, `Encounter`, `MedicationRequest`, `Metadata`, `ValueSet`, `CodeSystem`, `Procedure`). Populated for both CRUD (k6 tags every request with the type it just operated on) and Search (the type each query targets, copied from `queries.yaml`).
- **`complexity`** — Search-class label, one of `SIMPLE`, `COMPLEX`, `FULL_TEXT`, `OPERATION`. Search-only — CRUD rows omit it.

Both fields are **optional** in the schema (`schema/round-v1.schema.json`). Pre-2026-04-30 round artifacts emit only `verb` and consumers must handle that — the harness and the frontend do, by treating absence as "any" along that dimension. New artifacts emit them on every row.

Why split CRUD: the previous shape's four C/R/U/D rows averaged across resource types, which hid that some servers' ORM paths are tuned for some types and not others (e.g. dense Observation vs. sparse MedicationRequest). The five-resource fan-out exposes that asymmetry.

Why split Search: the previous flat list mixed Lucene-required full-text queries with cheap token searches into one weighted average. Servers without a full-text path (`_content` returns 400 / 5xx) silently dragged the headline down; servers with Lucene (HAPI's embedded backend) silently bore the cost. Splitting by class — Simple vs. Complex vs. Full-Text vs. Operation — keeps the headline honest and lets the per-class cells tell the divergence story directly. The `FULL_TEXT` cell on a server without Lucene/token-table support is the headline story, not a footnote.

The frontend computes per-class summaries (n-weighted across queries) on the fly. The round artifact is the input; the rollup math (in `frontend/src/lib/performance.ts:summarizeByComplexity`) doesn't ship in the artifact because the per_verb rows are sufficient.

## Fairness check

Each cell writes `fairness.json` with the realized patient count vs. the target. Any cell that ingested <95% of the target is flagged amber in the summary. This is how we catch a silent mis-ingest — for example a body-size-limit reject loop — without having to re-verify manually.

## Checkpoint status bands (heatmap colors)

Per profile, the cell color is derived from the p50 (median) latency at the largest checkpoint the server reached:

- 🟢 **green** ≤ 100 ms (feels instant)
- 🟡 **amber** 100 ms – 1 s (sluggish but usable)
- 🔴 **red** > 1 s (clearly slow to a user)
- ⚪ **grey** no checkpoint completed

The band thresholds are deliberately coarse. Performance differences at the ms scale are noise; order-of-magnitude differences are the real story, and log-scale curves reveal them clearly on the detail page.

## What we don't measure (yet)

- **Network / disk / CPU panels** from the per-cell `resources.csv` — planned for v0.1.
- **Stable tail quantiles** — p95 and p99 require more samples than a 2-minute run produces on slow cells. We retain them as tail evidence but don't headline on them. Longer adaptive runs (duration scaling with per-cell throughput) and bootstrapped confidence intervals are both on deck.
- **Managed-cloud FHIR servers (GCP, Azure Health Data Services)** — excluded from the local-model leaderboard because the bill scales with the ramp. Azure is representable via self-hosted MS FHIR OSS; GCP has no open equivalent.

## Reproducing locally

```sh
cd ~/repo/fhir-server-compare
sudo bash scripts/setup-host.sh         # governor + THP + swappiness + ulimit
set -a; source .env; set +a             # Aidbox license, Medplum creds
make loadtest-ramp-50k                  # ~12–16 h for full 4-server ramp
make benchmark                          # parse JSONL → benchmark.json
make benchmark-publish                  # copy into ../fhir-studio/
```

The round JSON is deterministic given the same inputs.
