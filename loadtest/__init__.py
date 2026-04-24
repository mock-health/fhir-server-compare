"""fhir-server-compare load test package.

Companion to the blog post on 5-server FHIR performance. The three HS-matched
stages (1K empty -> 100K -> +1K incremental) live in `stage.py`; the three
workload types (Batch ingest, CRUD, Search) live in `loader.py`,
`workload_crud.py`, `workload_search.py`. All four plug into `metrics.py` for
per-op timing capture and `resources.py` for docker-stats sampling.

Intentionally NOT a public API — modules import each other freely.
"""
