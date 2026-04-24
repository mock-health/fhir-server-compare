# Methodology

This is the index. The two methodology documents that actually drive the matrix live next to the code that implements them:

- **[`benchmark/methodology.md`](benchmark/methodology.md)** — Performance matrix methodology. How workloads are defined, how ramps work, which metrics are captured per server, how fairness is enforced.
- **[`conformance/methodology.md`](conformance/methodology.md)** — Conformance matrix methodology. How TestScripts are authored, the MUST/SHOULD/MAY bucketing, the applicability-probe rule for N/A cells, and what counts as evidence for a pass or fail.

Both documents are shipped into the `fhir-studio` site on every publish so the published pages and the repo are always in sync.

For governance — who decides what gets tested, how vendors request re-runs, how methodology changes — see [`GOVERNANCE.md`](GOVERNANCE.md).

For where the matrix is going next — see [`ROADMAP.md`](ROADMAP.md).
