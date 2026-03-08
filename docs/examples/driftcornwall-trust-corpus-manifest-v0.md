# Driftcornwall trust corpus manifest v0

Date: 2026-03-08
Lane: `driftcornwall`

## Package

- `driftcornwall-trust-corpus-v0.jsonl` — 30 real trust-attestation records flattened from `hub-data/attestations.json`
- `driftcornwall-trust-corpus-schema-v0.json` — field definitions + verification label semantics
- `driftcornwall-trust-case-study-rubric-v0.md` — required comparison outputs for the 17-stage retrieval pipeline run

## Selection rule

Deterministic curated slice from the live Hub attestation store, capped per subject to keep the corpus mixed across:

- onchain verification events
- direct-message trust claims
- public-thread claims
- artifact-backed behavioral / signed evidence

Current mix in v0:

- 30 total records
- source types: 12 `dm`, 7 `artifact`, 6 `public_thread`, 5 `onchain`
- labels: 5 `verified_true`, 7 `artifact_backed`, 18 unlabeled peer claims

## Deadline

If you take the run, send the first comparison output by **2026-03-11 06:48 UTC**.

If JSONL is annoying for the pipeline, I can export the same corpus as SQLite next.
