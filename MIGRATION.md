# Migration guide — fhirbench package layout

If you forked or scripted against `fhir-server-compare` before April 2026, every Python import path and many file locations changed when the repo was reorganized into a real `fhirbench` package. This document is the lookup table for porting your fork or scripts.

The change is **structural only** — no semantic behavior changed. Every Make target, every round artifact format, every `servers.yaml` / `queries.yaml` field, every published JSON shape is identical. Only file locations and Python module names differ.

## TL;DR

```bash
# Old workflow
pip install -r requirements.txt
python compare.py
python load_bundle.py --server hapi
python -m loadtest.ramp --run-id r1 ...

# New workflow
pip install -e .
python -m fhirbench.compare
python -m fhirbench.load_bundle --server hapi
python -m fhirbench.harness.ramp --run-id r1 ...
# OR use the installed console scripts:
fhirbench-compare
fhirbench-ramp --run-id r1 ...
```

## File location changes

### Python code

| Old location | New location |
|---|---|
| `_fhir_servers.py` | `src/fhirbench/servers.py` |
| `compare.py` | `src/fhirbench/compare.py` |
| `load_bundle.py` | `src/fhirbench/load_bundle.py` |
| `loadtest/__init__.py` | `src/fhirbench/harness/__init__.py` |
| `loadtest/ramp.py` | `src/fhirbench/harness/ramp.py` |
| `loadtest/stage.py` | `src/fhirbench/harness/stage.py` |
| `loadtest/loader.py` | `src/fhirbench/harness/loader.py` |
| `loadtest/metrics.py` | `src/fhirbench/harness/metrics.py` |
| `loadtest/resources.py` | `src/fhirbench/harness/resources.py` |
| `loadtest/sample_pool.py` | `src/fhirbench/harness/sample_pool.py` |
| `loadtest/wait_healthy.py` | `src/fhirbench/harness/wait_healthy.py` |
| `loadtest/workload_crud.py` | `src/fhirbench/harness/workload_crud.py` |
| `loadtest/workload_search.py` | `src/fhirbench/harness/workload_search.py` |
| `loadtest/host_meta.py` | `src/fhirbench/harness/host_meta.py` |
| `loadtest/report.py` | `src/fhirbench/harness/report.py` |
| `loadtest/aidbox_bootstrap.py` | `src/fhirbench/harness/aidbox_bootstrap.py` |
| `loadtest/bootstrap_medplum.py` | `src/fhirbench/harness/bootstrap_medplum.py` |
| `loadtest/generate.py` | `src/fhirbench/harness/generate.py` |
| `loadtest/update_templates.py` | `src/fhirbench/harness/update_templates.py` |
| `loadtest/k6_driver.py` | `src/fhirbench/harness/k6_driver.py` |
| `loadtest/benchmark/*.py` | `src/fhirbench/benchmark/*.py` |
| `loadtest/conformance/*.py` | `src/fhirbench/conformance/*.py` |
| `loadtest/publish/*.py` | `src/fhirbench/publish/*.py` |
| `loadtest/k6/*.{js,py,json}` | `src/fhirbench/k6/*.{js,py,json}` |
| `scripts/compare_harnesses.py` | `src/fhirbench/cli/compare_harnesses.py` |
| `scripts/emit_k6_context.py` | `src/fhirbench/cli/emit_k6_context.py` |
| `scripts/fetch_server_versions.py` | `src/fhirbench/cli/fetch_server_versions.py` |
| `scripts/setup-host.sh` | `scripts/setup-host.sh` (unchanged — shell tooling) |

### Configs and data

| Old location | New location |
|---|---|
| `servers.yaml` | `config/servers.yaml` |
| `queries.yaml` | `config/queries.yaml` |
| `medplum.config.json` | `config/medplum.config.json` |
| `medplum-lb.conf` | `config/medplum-lb.conf` |

### Profiles (workload definitions)

| Old location | New location |
|---|---|
| `benchmark/profiles/crud.yaml` | `profiles/benchmark/crud.yaml` |
| `benchmark/profiles/search.yaml` | `profiles/benchmark/search.yaml` |
| `conformance/profiles/<x>.yaml` | `profiles/conformance/<x>.yaml` |
| `conformance/testscripts/...` | `conformance/testscripts/...` (UNCHANGED — historical round paths reference this) |

### Methodology

| Old location | New location |
|---|---|
| `benchmark/methodology.md` | `docs/benchmark-methodology.md` |
| `conformance/methodology.md` | `docs/conformance-methodology.md` |
| `METHODOLOGY.md` | `METHODOLOGY.md` (unchanged — index file, links updated) |

### Docker

| Old location | New location |
|---|---|
| `conformance-docker/` | `docker/conformance-services/` |
| `spark-mongo-init/` | `docker/spark-mongo-init/` |
| `hfs-docker/` | DELETED — HFS removed from the roster (server didn't work) |
| `docker-compose.yml` + overlays | unchanged location (root) — paths inside updated |

## Import rewrites

Bulk sed for forks:

```bash
# Replace in your *.py files:
sed -i 's/from _fhir_servers /from fhirbench.servers /g'                    *.py
sed -i 's/from loadtest /from fhirbench.harness /g'                          *.py
sed -i 's/from loadtest\.benchmark/from fhirbench.benchmark/g'               *.py
sed -i 's/from loadtest\.conformance/from fhirbench.conformance/g'           *.py
sed -i 's/from loadtest\.publish/from fhirbench.publish/g'                   *.py
sed -i 's/from loadtest\.k6/from fhirbench.k6/g'                             *.py
sed -i 's/from loadtest\.\(\w\+\) /from fhirbench.harness.\1 /g'             *.py
sed -i 's/from scripts\./from fhirbench.cli./g'                              *.py

# Replace in your Makefile / CI scripts:
# python -m loadtest.<X>      → python -m fhirbench.harness.<X>
# python -m scripts.<X>       → python -m fhirbench.cli.<X>
```

## Roster change: HFS removed

The April 2026 reorganization also removed HFS (Helios FHIR Server) from the roster — it had unresolved issues that made it unsuitable for a public benchmark. The roster shrank from 7 to 6 servers. Historical rounds (`results/rounds/2026-q2-r000`, `r990`) that include HFS data are preserved unchanged — those artifacts are immutable evidence.

If your fork added HFS-related code, you can find the pre-removal state in the git history before commit `af5f974`.

## Round artifact format — UNCHANGED

`results/rounds/<id>/{benchmark,conformance}.json` schema is identical (still `schema/round-v1.schema.json`, version 1). `MANIFEST.json` sha256 hashes were not regenerated. Existing rounds remain valid.

## Public Make targets — UNCHANGED names

`make loadtest-dryrun`, `make loadtest-ramp`, `make conformance`, `make benchmark-publish`, etc. all still work. The targets delegate to `python -m fhirbench.<X>` internally instead of `python -m loadtest.<X>`, but contributors who only use Make see no difference.

## Questions?

Open an issue. If you got bitten by a path I forgot to document, please send a PR appending to this file.
