# Governance

This document describes how the FHIR Server Compare matrix is run, who decides what gets tested, and how vendors can request changes to their published results. The core principle: **the harness is a neutral third party**. mock.health does not sell a FHIR server.

## Conflict of interest disclosure

mock.health uses HAPI FHIR underneath its own products. This is the only conflict of interest in the matrix. HAPI config decisions go through a community reviewer rather than the mock.health maintainer alone. This disclosure appears prominently on the public leaderboard and methodology pages, not just here.

## Inclusion criteria

Any FHIR R4 server can appear in the matrix if it meets all three criteria:

1. **Self-hostable via Docker.** No paid license, no managed service account, no proprietary cloud dependency. If the free/OSS tier can run on a laptop, it qualifies.
2. **Implements CapabilityStatement + CRUD + Search.** These are the minimum surfaces the harness exercises. Servers that only do a subset (e.g., read-only FHIR facades) get a methodology asterisk and restricted workloads.
3. **Has a maintainer who will engage on a 2-week pre-notification window per round.** Before publication, each vendor gets a private preview of their results and can submit configuration PRs. Silent vendors still get tested, but a comment on the leaderboard notes "no vendor response."

Submitters should open a GitHub issue with the server's Dockerfile, FHIR base URL, and auth shape. Once the inclusion criteria are confirmed, the addition is a PR against `config/servers.yaml`, `docker-compose.yml`, `config/queries.yaml`, and `src/fhirbench/conformance/run.py` (see `CONTRIBUTING.md`).

## Methodology changes

Changes to how servers are scored are **not** routine PRs. They go through a 30-day public comment process:

1. Open an RFC issue describing the proposed change and the motivation.
2. The issue stays open for at least 30 days to collect feedback from vendors and implementers.
3. If adopted, the change is announced one round in advance so vendors can re-tune if needed.
4. The first round under the new methodology prominently links the RFC and the prior methodology.

Weights, score formulas, workload definitions, profile versions, and hardware specs all fall under this rule. Bug fixes to the harness (e.g., a test that was measuring the wrong thing) do not — those are normal PRs and get noted in the round changelog.

## Configuration review

Before each round, every server's configuration (Dockerfile settings, environment variables, JVM flags, etc.) is reviewed by its maintainer. The review happens in a public GitHub discussion linked from the server's row on the leaderboard. The goal is to prevent "you didn't tune our server correctly" complaints post-publication. If a vendor declines to review, that's noted on the published page.

## Appeals and re-runs

A vendor can file a GitHub issue requesting a re-run with a different configuration at any time. The re-run appears alongside the originally published result with both labeled and timestamped. The originally published result is never rewritten — rounds are immutable.

Re-runs are the heartbeat of the page between official rounds. They're not an edit mechanism; they're an addendum mechanism.

## Round immutability

Each round lives at `results/rounds/<year>-q<quarter>-r<NNN>/` with a `MANIFEST.json` listing sha256 hashes of the immutable artifacts. Prose (methodology notes, server descriptions) can be corrected in place with a changelog entry, but numbers and test outcomes never change after publication.

The monotonic round-id scheme (`r000`, `r001`, …) makes re-runs and corrections trivially citable.

## Cadence

Official rounds are published quarterly on the same day of the quarter (March, June, September, December). The four-week window before publication is the vendor preview window. A nightly rolling re-run publishes to a separate "nightly" strip on the leaderboard; that data is labeled as unofficial.

## Funding and incentives

Who runs this and how it's funded is answered on a public footer link from every leaderboard page. If that answer ever changes — if mock.health takes a payment tied to how a server ranks — the governance page updates first and the conflict-of-interest disclosure expands accordingly.
