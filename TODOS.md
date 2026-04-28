# TODOS

Deferred work captured during the `src/fhirbench/` refactor review (2026-04-24).

---

## CODEOWNERS + branch protection + issue/PR templates

**What:** Add `.github/CODEOWNERS`, `.github/ISSUE_TEMPLATE/` (bug + feature), `.github/pull_request_template.md`. Configure branch protection on `main` via GitHub UI.

**Why:** Public benchmark repo hygiene. Signals maintained project. Reduces back-and-forth on contributor PRs.

**Pros:** One-time setup. High ROI on external contribution quality and perceived project maturity.

**Cons:** Not a code change — pure repo config. Branch protection requires GitHub admin access. Templates are bikeshed-prone.

**Context:** Outside-voice review flagged this as a public-repo-polish concern orthogonal to the structural refactor.

**Depends on / blocked by:** Refactor should land first so CODEOWNERS paths reference the new tree.

---

## Full unit test coverage of harness modules

**What:** Add pytest suites for `fhirbench.harness.loader`, `fhirbench.harness.metrics`, `fhirbench.harness.k6_driver`, `fhirbench.k6.postprocess`. The refactor PR ships only 3 starter suites (`test_servers.py`, `test_parse_report.py`, `test_runner.py`).

**Why:** Public benchmark repo credibility. Catches regressions in the load-bearing modules. Currently the only safety net is manual `make loadtest-dryrun`.

**Pros:** Protects the most fragile code paths (HTTP ingest, k6 driver, NDJSON post-processing). Sets a bar for contributors.

**Cons:** Each module has significant I/O (HTTP, subprocess, Docker stats, file I/O). Needs mocking scaffolding (responses or requests-mock, test doubles for docker stats). Estimated 2-3 days of CC work.

**Context:** Deferred from the refactor PR to keep diff reviewable. The refactor already grows test story from 0 to 3 suites — grown more from 3 to 9 in a follow-up.

**Depends on / blocked by:** Refactor merge. Test scaffolding conventions established in the 3 starter suites.

---

## Methodology source-of-truth consolidation with fhir-studio

**What:** Today `docs/benchmark-methodology.md` and `docs/conformance-methodology.md` are copies of the same content that lives in `fhir-studio/frontend/src/content/{performance,conformance}/methodology.md`. Each publish syncs them via `fhirbench.publish.copy_to_studio`. Pick one canonical location and remove the other.

**Why:** A contributor editing methodology today must know to edit only the source (`fhir-server-compare/docs/…`) and let the publish step overwrite the studio copy. Easy to edit the wrong one.

**Pros:** Single source of truth. Clearer contributor story.

**Cons:** Requires coordinated change across 2 repos. fhir-studio's build would need to pull from fhir-server-compare at build time (git submodule, GitHub raw URL fetch, or npm-published package). Adds build-time coupling.

**Context:** Known technical debt pre-refactor. Outside-voice review noted but didn't challenge.

**Depends on / blocked by:** fhir-studio build pipeline work.

---
