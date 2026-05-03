# Security

## Default passwords are weak — and that's intentional

The docker-compose stack ships with trivial default passwords (`medplum/medplum`, `aidbox/aidbox`, `CosmicTopSecret` for Spark's MongoDB, `Fhir_Compare_Pass1!` for SQL Server). These containers are **for local reproducibility only**. Do not expose them beyond `localhost`, and do not treat this stack as a production deployment reference.

If you want to use any of these servers in production, consult each vendor's production deployment guide — every one of them has real auth, TLS, backup, and hardening guidance that this repo deliberately skips in the name of a one-command demo.

## Reporting a vulnerability

If you believe you've found a security issue in the harness itself — the Python comparison scripts, the TestScript runner, the publish pipeline, or anything in this repo — open a private security advisory on GitHub or email security@mock.health. Please do not open a public issue for sensitive reports.

For vulnerabilities in the FHIR servers themselves, report upstream to the respective projects.

## What this repo is and is not

- **Is:** a reproducible comparison harness that stands up 6 OSS FHIR servers locally and runs the same test suite against each.
- **Is not:** a production deployment guide, a certified certification suite, or a security audit tool.
