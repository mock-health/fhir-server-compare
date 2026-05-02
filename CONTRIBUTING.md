# Contributing

Thanks for considering a contribution. This repo is a FHIR server comparison harness, so contributions mostly fall into a few shapes.

## Adding a new FHIR server

A new server is an append-only change across four files:

1. **`config/servers.yaml`** — append a new block with `id`, `label`, `base_url`, `version`, `image`, `source_url`, and an `auth` shape (`none`, `basic`, `client_credentials`, or a new type if your server needs one).
2. **`docker-compose.yml`** — add a service for the server (and any sidecars like a database). Pin the image by sha256 digest — see the header comment for the refresh command.
3. **`config/queries.yaml`** — add an `expected_<id>` key to every query in the file. Run `python -m fhirbench.compare` to observe real behavior and fill these in.
4. **`src/fhirbench/conformance/run.py`** — append your server id to `ROSTER`.

### Inclusion criteria

A server qualifies for the matrix if it:
- Is FHIR R4 compliant (implements CapabilityStatement, CRUD, and Search at minimum).
- Is self-hostable via Docker with no paid license or managed service account required.
- Has a maintainer willing to engage on a 2-week pre-notification window before each benchmark round.

Servers that don't clear this bar still have value — but they belong in a curated "other implementations" list, not in the matrix. The matrix promise is that anyone can reproduce the whole thing on their own laptop.

## Adding a new TestScript (conformance)

1. Drop a TestScript JSON under `conformance/testscripts/<profile>/<bucket>/`. Bucket = `MUST`, `SHOULD`, or `MAY` per the relevant spec.
2. Include a `relatedArtifact[]` entry citing the FHIR spec URL the test is checking. This flows into the round artifact so every evidence row is a clickable citation.
3. Run `make conformance-run` and confirm your test produces TestReports against every server in the roster.
4. Run `make conformance-parse && make conformance-validate` and verify the round artifact still passes schema validation.

## Re-running a benchmark

Rounds are immutable — `results/rounds/<id>/` is a frozen snapshot. Don't edit round artifacts in place. To produce a new round:

```bash
# regenerate raw data
make loadtest-ramp-50k            # or your preferred ramp
make conformance-run

# fold + validate
make benchmark-parse
make conformance-parse
make benchmark-validate
make conformance-validate

# publish to fhir-studio
make benchmark-publish
make conformance-publish
```

Round IDs follow `<year>-q<quarter>-r<NNN>` (zero-padded). Bump the round id when publishing so the old round stays citable.

## Adding a new search query

1. Append a block under `queries:` in `config/queries.yaml` with `name`, `path`, `expected_<server>` for every server in the roster, **and the two dimension tags**:
   - `resource_type:` — the FHIR resource type the query targets (`Patient`, `Observation`, `Condition`, `Encounter`, `MedicationRequest`, `Metadata`, `ValueSet`, `CodeSystem`, `Procedure`, …). Flows into `per_verb[].resource_type` in the round artifact.
   - `complexity:` — one of `SIMPLE` (single-parameter token / string / date / reference), `COMPLEX` (multi-parameter, compound AND, `_include` / `_revinclude`), `FULL_TEXT` (`_content` or a `:text` modifier), or `OPERATION` (`/metadata`, `_history`, `$expand`, `$lookup`, `$export`). Drives the search-class heatmap on `/performance/workloads/search`.
2. If the query should be excluded from the load mix (asymmetric support, intentional 4xx, async polling, etc.), add `loadtest: skip:<reason>`. The single-patient `compare.py` matrix still runs it.
3. If the query needs runtime-sampled parameter values (per-server live corpus harvest), add `matrix: skip:runtime_sampled` and define the placeholder shape — see existing `patient_by_family_exact`, `condition_by_code` for the pattern.
4. Run `python -m fhirbench.compare` to fill in `expected_<server>` for the new row.
5. The k6 driver picks up new queries automatically via `fhirbench.cli.emit_k6_context`; no JS change needed.

## Code style

- Python: no linter enforced, but keep things boring and readable.
- Configs (YAML, JSON): the round artifact must validate against `schema/round-v1.schema.json`. Don't break that schema — add optional fields if you need to extend. The 2026-04-30 addition of optional `resource_type` and `complexity` to `per_verb[]` is the canonical example: both fields are `not required`, so old artifacts pre-2026-04-30 still validate while new artifacts carry the extra dimensions.

## Filing an issue

Bug reports: include the server, the query/test, the observed output, and what you expected. If a TestScript is producing a false pass or false fail, include the raw TestReport from `results/conformance/<round>/<server>/`.

Feature requests: open an issue first before a large PR. For new workloads, new profiles, or methodology changes, see `GOVERNANCE.md` — those go through a public comment process, not a regular PR.
