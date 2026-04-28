"""fhirbench — neutral-territory FHIR server benchmark harness.

This is a thin import root. The runnable surfaces live in subpackages:

  fhirbench.servers       — server discovery, auth, URL resolution
  fhirbench.compare       — single-patient behavioral comparison (compare.py)
  fhirbench.load_bundle   — POST one Synthea bundle to a single server
  fhirbench.harness       — multi-stage ramp orchestration + workloads
  fhirbench.benchmark     — round-artifact aggregation from ramp output
  fhirbench.conformance   — TestScript runner + result parsing
  fhirbench.publish       — copy round artifacts into fhir-studio
  fhirbench.cli           — one-off CLI runners (emit_k6_context, etc.)
  fhirbench.k6            — k6 JavaScript harness + Python postprocessor

Intentionally NOT a public API surface — modules import each other freely.
See ARCHITECTURE.md for the cross-module call graph.
"""

__version__ = "0.1.0"
