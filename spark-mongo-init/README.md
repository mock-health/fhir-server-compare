# Spark MongoDB index bootstrap

The `.js` files in this directory are bind-mounted into the `fhir-compare-spark-db`
container at `/docker-entrypoint-initdb.d/` (see `docker-compose.yml`). The
official MongoDB image runs every `.js` and `.sh` file in that directory, in
alphabetical order, on the **first startup of an empty data volume**.

## What and why

Spark's default Mongo schema ships without indexes on two hot paths:

1. `searchindex.internal_id` — every transaction-bundle ingest does an upsert
   filtered by `internal_id`. Without an index, each upsert does a `COLLSCAN`
   of the entire collection. At 1K patients (~140K resources), throughput
   collapses to ~4 bundles/min and per-bundle latency exceeds 5 minutes.

2. `resources.{@typename, id, @VersionId}` — version-history walks during
   CRUD R/U operations. Same `COLLSCAN` problem on a different hot path.

With both indexes in place, Spark's Round 0 1K validation completed cleanly.
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
- Or apply manually:
  ```bash
  docker exec fhir-compare-spark-db mongosh -u root -p CosmicTopSecret \
    --authenticationDatabase admin --quiet --eval "
      db = db.getSiblingDB('spark');
      db.searchindex.createIndex({internal_id: 1}, {unique: true});
      db.resources.createIndex({'@typename': 1, id: 1, '@VersionId': 1});
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
