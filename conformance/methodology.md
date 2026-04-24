# Conformance methodology — v1.0-draft

> Independent. Reproducible. Continuously run. No dog in the fight.

This page describes how mock.health's conformance heatmap is generated. Every cell on `/conformance` traces back to a deterministic test run; this document is the contract.

## What I test

For each FHIR R4 server in our roster, I run the same set of declarative **TestScript** Resources against it and record the results. A TestScript is a [FHIR R4 Resource](https://hl7.org/fhir/R4/testscript.html) — every server in the roster can in principle read its own tests. I organize tests by **profile** (a coherent body of conformance work) and within a profile by **bucket** (`MUST`, `SHOULD`, `MAY`).

### v0.3 active profiles

| Profile | Status | Tests | Notes |
|---|---|---|---|
| FHIR R4 base spec | active | 23 | CRUD shape, search semantics, modifiers, includes, terminology operations, history, error semantics |
| Bulk Data kickoff surface | active | 6 | `Patient/$export` request shape + 202/Content-Location response. **Kickoff only — not full async lifecycle.** |
| SMART on FHIR v2 | archived | 8 | Deferred to Round 1. See note below. |
| US Core 6.1.0 | not yet tested | — | Inferno-driven; planned for Round 1 (June 2026). Needs per-server LOINC/SNOMED/RxNorm terminology pre-load. |

Profiles deliberately scoped to a **kickoff** subset carry that word in the column label so readers don't conflate them with full IG conformance. The full `Bulk Data Access IG v2` tests (~30 normative checks via Inferno) ship in Round 1.

### Why SMART is not a column in v0.3

The SMART discovery profile was scored in v0.1–v0.2 but dropped from the v0.3 matrix. Reason: three of the seven roster servers (HAPI, MS FHIR, Blaze) do not ship SMART discovery in their default Docker image. Their failures reflect **compose configuration choices** (no OAuth provider bolted on, security disabled for loadtest parity), not intrinsic server behavior. A column where half the reds measure our ops setup rather than the vendor's code was misleading readers, so I retired it.

The TestScripts and profile spec are preserved under `conformance/{profiles,testscripts}/_archived/smart-on-fhir-v2/` for revival in Round 1 when I pair each server with the vendor-recommended OAuth layer (Keycloak/smart-launcher for HAPI, Azure AD for MS FHIR, etc.) and can produce a fair apples-to-apples comparison.

### Why I chose this slice for v0.3

mock.health's value-proposition is "I know what real FHIR clients trip over." That lives heavily in **search-parameter semantics** — modifiers, prefixes, includes, the silent-ignore footgun. The matrix biases toward this depth on the FHIR R4 base column (the column most likely to surprise real-world consumers), with a kickoff-surface check on Bulk Data as an honest early signal. Full conformance for the auth/async-export profiles ships in Round 1 with Inferno.

## Bucketing

I deliberately **do not produce a single composite conformance score**. Conformance is multi-dimensional and a single number invites unfair vendor comparison. Instead, every cell shows `passed/total` per `MUST`/`SHOULD`/`MAY` bucket, and the cell color reflects the simple total percentage:

- 🟢 **green** ≥ 95%
- 🟡 **amber** 70–94%
- 🔴 **red** < 70%
- ⚪ **grey** not yet tested
- ▫️ **N/A** not applicable — profile surface not reachable or requires infra I don't provide (see Applicability below)

## Applicability: N/A vs. fail

Some server/profile pairs can't be scored at all. When that happens the cell is **N/A**, not red. The rule is mechanical, not editorial: each profile may declare an **applicability probe** (a single HTTP request) plus match-rules on the response status code or body. If the probe trips, the cell is N/A with an on-record reason string; otherwise the profile's tests run normally.

The distinction that matters is *why* the probe failed:

- **Route not implemented.** Blaze, Spark, HFS don't route `Patient/$export` at all — they return 4xx with "invalid id" or plain 404. The whole Bulk Data v2 surface is absent, so running kickoff tests would score 0/6 against a feature the vendor never claimed to ship. N/A with reason "Patient/$export operation not implemented."
- **Infra dependency absent.** Aidbox implements bulk data in code, but its dev image requires a cloud storage backend (GCP/Azure/AWS) that I don't provision. The probe returns 500 with `"storage-type not specified"` — body-matched to N/A. The reason string points to the config that would turn the cell on.

**Partial implementation is not N/A.** If the probe succeeds, every test is scored on its own merits even when some fail against a SHOULD or MAY. Medplum is the canonical example: kickoff works for Patient and System export, but `Group/$export` returns 404 (the Medplum server source defines `patientExportHandler` and `bulkExportHandler` only; no group handler). That's a real feature gap against a SHOULD in Bulk Data IG v2 and shows up as amber 5/6 — the right signal to a reader shopping for a server with Group-cohort exports.

Collapsing feature gaps into N/A would launder measurable capability differences behind a policy knob, which defeats the point of the matrix. Applicability exists only for the "I cannot measure this server on this profile" case.

`MUST` is reserved for FHIR R4 base spec **must** statements (e.g., "the server MUST expose a CapabilityStatement at `/metadata`"). `SHOULD` captures spec recommendations that affect interoperability but aren't strict (e.g., "the server SHOULD honor `_total=accurate`"). `MAY` captures optional behaviors (e.g., "the server MAY implement `_has` reverse chaining").

## Server configuration

The conformance matrix tests servers brought up via `docker-compose.yml` PLUS the conformance-features overlay (`docker-compose.conformance-features.yml`). The overlay enables documented opt-in feature flags so I measure the server's capability when configured per the vendor's published guidance — not the bare-image default that often disables features for safety.

| Server | Image tag | Conformance overlay flips |
|---|---|---|
| HAPI | `hapiproject/hapi:latest` | `hapi.fhir.bulk_export_enabled=true`, `hapi.fhir.batch.enabled=true`, `hapi.fhir.scheduling_disabled=false` (enables `Patient/$export` + the supporting batch scheduler) |
| Aidbox | `healthsamurai/aidboxone:latest` | `BOX_FEATURES_BULK_EXPORT_ENABLED=true`, `BOX_BULK_STORAGE_BACKEND=file`, `BOX_BULK_STORAGE_FILE_DIR=/tmp/aidbox-bulk` (enables `$export`; storage destination still requires Aidbox-Project resource config not handled by env vars in this image — Aidbox's Bulk Data column scores reflect this) |
| Medplum | `medplum/medplum-server:latest` | none — Medplum ships Bulk Data on by default |
| MS FHIR | `mcr.microsoft.com/healthcareapis/r4-fhir-server:latest` | none — default config |
| Blaze | `samply/blaze:latest` | none — research server; default config |
| Spark | `sparkfhir/spark:r4-latest` | none — reference impl; default config |
| HFS | local build (`hfs-docker/Dockerfile.fork`) | none — Rust impl, Postgres+Elasticsearch backend |

The exact env vars are auditable in `docker-compose.yml` + `docker-compose.conformance-features.yml`.

## What's NOT tested in v0.3

Honesty about scope is the trust contract for this matrix. The following are explicitly out of scope:

- **SMART on FHIR** — entire profile deferred. Half the roster doesn't ship SMART in its default container; a column where half the reds score our ops work rather than the vendor's code doesn't earn its place. Round 1 will reintroduce SMART with each server paired with its recommended OAuth layer for a fair comparison.
- **Full Bulk Data Access IG v2 conformance** (~30 normative checks via Inferno Bulk Data test kit) — including async status URL polling, NDJSON output validation, `output[].url` retrieval, manifest correctness, JWT-signed Backend Services authentication. v0.3 only tests the kickoff surface (request shape + 202 + Content-Location).
- **Aidbox Bulk Data end-to-end.** Aidbox's `$export` requires a storage destination configured via an Aidbox-Project resource (admin API), not the env vars our overlay sets. Kickoff returns 500 "storage-type not specified" until the destination is wired. Documented as a partial-fix in the overlay.
- **Commercial / paid-only server features** across all vendors.
- **JWT-signed `client_credentials`** (SMART Backend Services) — our auth helper supports basic, bearer, and OAuth2 standard `client_credentials` but not RFC 7523 JWT-signed grants. Round 1.
- **Body-level FHIRPath assertions.** Today body asserts use substring (`responseBody contains "X"`) + a whitespace-tolerant `matchesRegex`. FHIRPath via `fhirpath-py` is on the roadmap.
- **Reset-to-fixture betIen rounds.** Each server is tested in whatever data state it happens to be in. Search-result-count assertions are deliberately avoided because of this.
- **US Core 6.1.0** — needs Inferno US Core test kit + per-server LOINC/SNOMED/RxNorm/ICD-10 terminology pre-load. Days of operational work per server. Round 1 (June 2026).

## Runner

The reference test executor is `loadtest/conformance/runner.py` — a small Python program that walks each TestScript's `test[].action[]`, makes the `operation` HTTP call, evaluates the `assert` against the response, and emits a FHIR `TestReport` Resource per script.

Supported assert types and operators (see `runner.py` docstring for the canonical list):

| Assert type | Operators | Notes |
|---|---|---|
| `responseCode` | `equals`, `notEquals`, `in`, `notIn`, `contains`, `notContains` | Status code as string |
| `headerField` (+ `value`) | same | Single response header |
| `contentType` | same + `matchesMimeClass` | `matchesMimeClass` parses the MIME structurally (strips parameters, loIrcases, handles RFC 6839 `+suffix` grammar). Use this to match `application/json` AND `application/fhir+json` AND `application/json; charset=utf-8` against the bare class `"json"` |
| `responseBody` | `contains` (default), `notContains`, `equals`, `notEquals`, `in`, `notIn`, `matchesRegex` | Substring or regex match against raw response body. `matchesRegex` is DOTALL + IGNORECASE, tolerates pretty-printed vs compact JSON whitespace. |
| `response: "okay"` | — | Convenience: passes if status is 2xx |

Operations support `pathFromRoot: true` which strips the FHIR base path off `base_url` to probe Ill-known endpoints at the server origin (used by the informational `discovery-also-at-root` SHOULD test).

I initially planned to use AEGIS testscript-engine (the open executor that poIrs Touchstone certification). It's broken against current `fhir_models` gem versions (`FHIR::TestReport::TestScript::Action` namespace removed); rather than wait for a fix, I wrote this small Python runner that supports the TestScript subset our suite needs. Pull requests Ilcome.

## Reproducibility

Anyone can rerun a round locally:

```sh
git clone https://github.com/mock-health/fhir-server-compare
cd fhir-server-compare
cp .env.example .env       # fill in AIDBOX_LICENSE (free) and MEDPLUM_CLIENT_*

# Bring up servers WITH conformance feature flags enabled:
docker compose -f docker-compose.yml -f docker-compose.conformance-features.yml up -d

# Run the conformance sIep against all 7 (smoke-checks /metadata before each profile):
make conformance CONF_ROUND=2026-q2-r000

# Inspect the matrix (all 3 profiles):
make conformance-summary CONF_ROUND=2026-q2-r000
```

The `make conformance` target runs both TestScript directories sequentially (fhir-r4-base → bulk-data-v2) and accumulates per-server results in `results/conformance/<round>/<server>/`. Before each profile pass, the runner probes `/metadata` on every roster server and aborts if any are unreachable — silent boot failures get scored as conformance failures otherwise, which misattributes the defect to the server.

## Cadence

- **Quarterly** headline rounds (March, June, September, December). Each round is frozen at `/conformance/rounds/<round-id>` and never edited.
- **Nightly** rolling runs against the trunk of the harness, surfaced separately as "unofficial" data. (Round 1.)

## Governance

mock.health does not ship a FHIR server. I use HAPI under the hood for the synthetic-data sandbox, which is disclosed on every page and reflected in the matrix as a row alongside every other server (no preferential treatment in the methodology). Methodology changes go through a 30-day public comment period via GitHub issues. Any vendor can file an issue to re-run their server with a new config; the re-run appears next to the published result with both labeled.

## Limits

- v0.3 covers FHIR R4 base spec (23 tests) + Bulk Data kickoff surface (6 tests). SMART, full Bulk Data, and US Core ship in Round 1 (June 2026) with Inferno integration and per-server OAuth pairings.
- Empty stores: each server is tested in whatever state it happens to be in at run time. A future revision will reset to a known fixture before testing. Until then, treat `red` cells as a starting point for conversation, not a grade.
- The `MAY` bucket on the FHIR R4 base profile primarily asserts non-5xx response (that the server doesn't crash on the parser). Both 200 (works) and 4xx (gracefully not implemented) pass; only 5xx (crash) fails. This deliberately separates "implemented vs declined" from "implemented vs broken."

## Provenance

Every cell links to one or more `TestReport` JSON files in `results/conformance/<round>/<server>/`. Click any cell on the heatmap to drill in.
