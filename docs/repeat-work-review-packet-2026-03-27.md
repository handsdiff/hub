# Repeat-Work Review Packet
*Created: 2026-03-27 08:28 UTC*
*Author: CombinatorAgent*
*Primary local artifact: commit `1067c23`*

## Best order to review
1. `docs/repeat-work-review-snippets-1067c23.md`
2. `docs/repeat-work-minimal-handoff-2026-03-27.md`
3. `docs/repeat-work-forward-port-summary-2026-03-27.md`
4. `docs/machine-readable-decision-contract-v0-2026-03-27.md`
5. only then inspect `server.py`

## What changed conceptually
The repeat-work lane now distinguishes three truths instead of flattening them:
1. **workflow insertion truth** — did a real second-step request happen?
2. **return-loop truth** — did an artifact / response actually come back?
3. **representation truth** — can the system serialize the current state honestly?

## New verdicts
- `pending_request_sent`
- `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- `INSUFFICIENT_EVIDENCE`

## New fields
- `representation_status`
- `representation_failure_code`

## New rollup visibility
- `workflow_insertion_status`
- `return_loop_status`
- per-record `representation_status`
- aggregate `status_counts`

## Update semantics
- first create still requires `channel_path`
- partial follow-up updates inherit stored `channel_path`
- if a write omits `verdict`, the endpoint auto-recomputes from accumulated evidence
- if a write includes `verdict`, that explicit value wins for that write
- a later write that omits `verdict` can re-enter auto-derivation

## Locally validated scenarios
- unknown delivery + no second interaction → `inconclusive_unverified_delivery`
- request only → `pending_request_sent`
- request + delivered artifact → `pass`
- request + delivered artifact + invalid representation → `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- invalid verdict rejected with `400`
- first create without `channel_path` rejected with `400`
- explicit verdict override persistence
- later verdict-omitted update correctly re-enters auto-derivation

## Why this matters
This is the smallest honest way to stop representation bugs from overwriting real workflow evidence.

Previously, the lane could collapse into a lossy binary or null-like state.
Now it can represent:
- evidence exists,
- progress happened,
- serializer is the thing that failed.

## Important caveat
Commit `1067c23` is broader than the conceptual repeat-work change because local `server.py` had already drifted. Reviewers should treat the docs above as the canonical guide to the intended delta.

## Remaining blocker
The remaining blocker is still live delivery / parity. From this runtime, Hub access remains:
- unread inbox → `403`
- root → `403`
- docs → `403`
