// Spark MongoDB index bootstrap.
//
// Auto-runs on the FIRST startup of fhir-compare-spark-db (when /data/db is
// empty), per the official MongoDB image's /docker-entrypoint-initdb.d/
// convention. Bind-mounted in docker-compose.yml.
//
// Why this file exists: Spark's default schema ships without production
// indexes on either write-path or read-path (search) hot fields. Every
// transaction-bundle ingest does:
//   1. An upsert filtered by `searchindex.internal_id`
//   2. A version-history walk on `resources.{@typename,id,@VersionId}`
//   3. A "mark-previous-version-superceded" update on `resources.{@REFERENCE,@state}`
// And every FHIR search query (`Patient?gender=`, `Observation?code=`,
// `Condition?subject=`, ...) filters by a specific search-param field on the
// `searchindex` collection — with no per-param index, Mongo COLLSCANs the
// entire collection per query. A 1K-patient Synthea load fills `searchindex`
// with ~3M docs; unindexed search at that scale runs p50 in the 7-second
// range with sporadic timeouts. Indexed, it drops by orders of magnitude.
//
// Discovered in stages during loadtest validation:
// - 2026-04-18: `internal_id` unique index → ~4 bundles/min to ~290 bundles/min (~70x).
// - 2026-04-19: During ramp-50k @ 1K checkpoint, ingest throughput re-collapsed
//   to near zero. Mongo slowlog showed COLLSCAN on {@REFERENCE,@state} updates
//   (9s × 646K docs per resource). Adding `reference_state_1` closes the third
//   write hot path.
// - 2026-04-23: First full 1K ramp completed. Search p50 @ 1K = 7.2s, err 9%
//   — Mongo slowlog showed COLLSCAN on searchindex per FHIR-search request.
//   Added the read-path index set below. Same class of bootstrap problem as
//   Aidbox's production-index setup; documented in methodology alongside it.
//
// IMPORTANT: This file only runs on the FIRST startup of a fresh volume.
// To re-apply after schema changes, you must either:
//   1. `make loadtest-reset-one SERVER=spark` to nuke + recreate the volume
//   2. Or manually: docker exec fhir-compare-spark-db mongosh -u root \
//      -p CosmicTopSecret --authenticationDatabase admin \
//      --eval "db.getSiblingDB('spark').searchindex.createIndex(...)"

print("=== Spark MongoDB index bootstrap ===");

db = db.getSiblingDB('spark');

// Unique index on internal_id. Eliminates the COLLSCAN-per-upsert hot path.
// Spark uses internal_id as the logical key for every (resource, version)
// tuple it tracks. The unique constraint matches Spark's expectation that
// each internal_id maps to exactly one document; if Spark ever changes that
// invariant, this createIndex will fail loudly on the first conflicting
// insert (which is preferable to silent data corruption).
db.searchindex.createIndex(
  { internal_id: 1 },
  { unique: true, name: 'internal_id_1' }
);

print("Created index: searchindex.internal_id_1 (unique)");

// Index on the resources collection's compound find pattern observed in
// production logs: { @typename, id, @VersionId }. Without this, version-
// specific resource lookups also COLLSCAN. The version-history walk during
// CRUD R/U operations hits this hot path.
db.resources.createIndex(
  { '@typename': 1, id: 1, '@VersionId': 1 },
  { name: 'typename_id_version_1' }
);

print("Created index: resources.typename_id_version_1");

// Compound index covering the "mark-previous-version-superceded" update.
// Every transaction-bundle ingest issues, per resource:
//   update resources
//   where { @REFERENCE: "<type>/<id>", @state: "current" }
//   set   { @state: "superceded" }
// Without this index MongoDB COLLSCANs the entire resources collection per
// update (9s × ~646K docs observed at the 1K-patient checkpoint of ramp-50k
// on 2026-04-19), collapsing throughput to zero. Not unique — historical
// versions legitimately share {@REFERENCE, @state} tuples.
db.resources.createIndex(
  { '@REFERENCE': 1, '@state': 1 },
  { name: 'reference_state_1' }
);

print("Created index: resources.reference_state_1");

// --- Read-path indexes (FHIR search) ---
//
// Every FHIR search the workload driver issues filters the `searchindex`
// collection by (internal_resource, <search-param>). Without a per-param
// index, Mongo COLLSCANs ~3M docs (1K patient Synthea corpus) per query.
// Compound `{internal_resource, <param>}` lets a query like
//     Observation?code=8302-2
// drop to an index scan over just Observation rows matching that code.
//
// The list below covers the search params exercised by the published
// workload (loadtest/queries.yaml with `loadtest: skip:*` honored):
//
//   gender, family, given        — Patient token/string searches
//   code, combo-code             — Observation/Condition/Procedure codes
//   subject, patient             — back-references from clinical resources
//                                  to their Patient ("subject" on Observation/
//                                  Condition/Procedure, "patient" on Encounter/
//                                  MedicationRequest etc.)
//   date                         — Observation.date range queries
//   identifier                   — Identifier token lookups (Practitioner,
//                                  Organization, Patient)
//   practitioner, location       — Encounter participant refs
//   fhir_id                      — direct-id reads (Patient/<id>)
//   internal_resource            — resource-type bucket filter (included as a
//                                  standalone prefix for queries that don't
//                                  carry another param, e.g. `Patient?` with
//                                  no filters). Compound indexes also usable.
//   _lastUpdated.start           — sort/filter by _lastUpdated, used for
//                                  paging tails + history-type queries.
//
// Names are prefixed `ir_` (internal_resource) so they sort together in
// getIndexes() output and are obvious as the read-path set.
const READ_INDEXES = [
  ["gender",             { internal_resource: 1, gender: 1 }],
  ["family",             { internal_resource: 1, family: 1 }],
  ["given",              { internal_resource: 1, given: 1 }],
  ["code",               { internal_resource: 1, code: 1 }],
  ["combo_code",         { internal_resource: 1, "combo-code": 1 }],
  ["subject",            { internal_resource: 1, subject: 1 }],
  ["patient",            { internal_resource: 1, patient: 1 }],
  ["date",               { internal_resource: 1, date: 1 }],
  ["identifier",         { internal_resource: 1, identifier: 1 }],
  ["practitioner",       { internal_resource: 1, practitioner: 1 }],
  ["location",           { internal_resource: 1, location: 1 }],
  ["fhir_id",            { internal_resource: 1, fhir_id: 1 }],
  ["internal_resource",  { internal_resource: 1 }],
  ["_lastUpdated_start", { internal_resource: 1, "_lastUpdated.start": 1 }],
];

for (const [name, key] of READ_INDEXES) {
  db.searchindex.createIndex(key, { name: "ir_" + name + "_1" });
  print("Created index: searchindex.ir_" + name + "_1");
}

print("=== Spark MongoDB index bootstrap complete ===");
