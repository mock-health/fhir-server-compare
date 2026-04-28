# Spark MongoDB index bootstrap

The `.js` files in this directory are bind-mounted into the `fhir-compare-spark-db`
container at `/docker-entrypoint-initdb.d/` (see `docker-compose.yml`). The
official MongoDB image runs every `.js` and `.sh` file in that directory, in
alphabetical order, on the **first startup of an empty data volume**.

## What and why

Spark's default Mongo schema ships without indexes on the hot paths the
benchmark exercises. The init script covers all of them:

**Write path**
1. `searchindex.internal_id` (unique) — bundle-ingest upserts.
2. `resources.{@typename, id, @VersionId}` — version-history walks.
3. `resources.{@REFERENCE, @state}` — mark-superceded updates.

**Read path**
4. `resources.internal_id` — post-search resource-body fetch (the join
   from `searchindex` hits back into `resources`). Without it, paginated
   searches at scale issue N COLLSCANs over the full versioned resources
   collection (9.4M docs at the 16K-patient checkpoint of ramp-50k) and
   hit Spark's internal 120s request cutoff.
5. `searchindex.{internal_resource, <param>}` for every FHIR search param
   the workload uses (`gender`, `family`, `given`, `code`, `combo-code`,
   `subject`, `patient`, `date`, `identifier`, `practitioner`, `location`,
   `fhir_id`, `internal_resource`, `_lastUpdated.start`). The Mongo
   analogue of Aidbox's per-resource-table GIN(jsonb_path_ops) — Aidbox
   uses one index per table because Postgres' `@>` operator is the
   universal predicate; Spark's flattened `searchindex` collection wants
   one compound per FHIR search param instead.

With these in place, Spark's Round 0 1K validation completed cleanly.
Without them, the same run takes 3-4 hours.

## Reproducibility

This directory is committed to the public repo so any reproducer running
`make loadtest-reset-one SERVER=spark && make loadtest-up-one SERVER=spark`
gets a properly indexed Spark database with no manual `mongosh` exec needed.

## When indexes do NOT auto-create

`/docker-entrypoint-initdb.d/` only runs on the **first** startup of a fresh
data volume. If someone restores a Spark Mongo volume from backup that
predates this script, the indexes will be missing. Either:

- Re-create from scratch: `make loadtest-reset-one SERVER=spark`
- Or apply manually against a running container (idempotent —
  `createIndex` is a no-op if a matching index already exists):
  ```bash
  docker exec fhir-compare-spark-db mongosh -u root -p CosmicTopSecret \
    --authenticationDatabase admin --quiet --eval "
      db = db.getSiblingDB('spark');
      // Write path
      db.searchindex.createIndex({internal_id: 1}, {unique: true, name: 'internal_id_1'});
      db.resources.createIndex({'@typename': 1, id: 1, '@VersionId': 1}, {name: 'typename_id_version_1'});
      db.resources.createIndex({'@REFERENCE': 1, '@state': 1}, {name: 'reference_state_1'});
      // Read path
      db.resources.createIndex({internal_id: 1}, {name: 'internal_id_1'});
      const READ = [
        ['gender', {internal_resource:1, gender:1}],
        ['family', {internal_resource:1, family:1}],
        ['given', {internal_resource:1, given:1}],
        ['code', {internal_resource:1, code:1}],
        ['combo_code', {internal_resource:1, 'combo-code':1}],
        ['subject', {internal_resource:1, subject:1}],
        ['patient', {internal_resource:1, patient:1}],
        ['date', {internal_resource:1, date:1}],
        ['identifier', {internal_resource:1, identifier:1}],
        ['practitioner', {internal_resource:1, practitioner:1}],
        ['location', {internal_resource:1, location:1}],
        ['fhir_id', {internal_resource:1, fhir_id:1}],
        ['internal_resource', {internal_resource:1}],
        ['_lastUpdated_start', {internal_resource:1, '_lastUpdated.start':1}],
      ];
      for (const [n, k] of READ) db.searchindex.createIndex(k, {name: 'ir_' + n + '_1'});
    "
  ```

## Methodology disclosure

Performance numbers reported for Spark in this benchmark assume these indexes
are present. The methodology page should state this explicitly. The principle
is the same as the Postgres `shared_buffers` tuning applied to HAPI in
`docker-compose.loadtest.yml` — vendor-recommended runtime configuration is
applied across the board, and the configuration choices are committed to the
public repo so anyone can reproduce or contest them.

A "default config vs tuned" sidebar showing the ~70× delta from the missing
indexes is also publication-worthy on its own merits.
