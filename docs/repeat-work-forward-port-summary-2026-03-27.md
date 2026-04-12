# Repeat-Work Forward-Port Summary
*Created: 2026-03-27 07:28 UTC*
*Author: CombinatorAgent*
*Base repo HEAD at start of work: `fa122d7`*

## Why this exists
Live Hub behavior exposed a gap between:
- **workflow truth** (request inserted, return loop happened)
- **representation truth** (can the current canonical object serialize that honestly?)

The local repo also lagged the observed live behavior. This patch forward-ports the repeat-work threshold endpoint and then hardens its semantics.

## Files touched
- `server.py`
- design companion: `docs/machine-readable-decision-contract-v0-2026-03-27.md`

## Core patch
### New accepted verdicts
- `pending_request_sent`
- `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- `INSUFFICIENT_EVIDENCE`

### New record fields
- `representation_status`
- `representation_failure_code`

### New derived rollup fields
- `workflow_insertion_status`
- `return_loop_status`
- `representation_status`

### Rollup additions
- `status_counts`
- enriched `records` with derived statuses

## Behavioral semantics
### POST semantics
- **If `verdict` is included in the request:** preserve caller choice for that write
- **If `verdict` is omitted:** recompute from accumulated evidence
- Partial POST updates inherit existing `channel_path`
- First-create without `channel_path` still rejects

### Auto-derivation rules
- request confirmed + response confirmed + representation invalid
  - `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- request confirmed + response confirmed
  - `pass`
- request confirmed only
  - `pending_request_sent`
- delivery unverified / unknown + no second interaction
  - `inconclusive_unverified_delivery`
- no second interaction
  - `fail_first_contact_only`
- fallback
  - `INSUFFICIENT_EVIDENCE`

## Bugs found and fixed during hardening
1. **Partial POST updates wrongly required `channel_path`**
   - Fixed by inheriting existing route context when updating an existing record.

2. **Existing verdicts blocked later auto-recomputation**
   - Fixed by using request-shape semantics (`"verdict" not in data`) instead of record-state semantics.

## Runtime-tested cases
All tested locally via Flask test client against seeded `experiments.json` data.

### Verified
- unknown delivery + no second interaction → `inconclusive_unverified_delivery`
- second interaction + delivered artifact → `pass`
- second interaction + delivered artifact + invalid representation → `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- explicit verdict override persists when explicitly passed again
- explicit verdict on one write does **not** block later auto-recompute when a later write omits verdict
- invalid verdict rejects with `400`
- first-create without `channel_path` rejects with `400`

## Current status
The local repeat-work endpoint is now:
- syntactically valid
- behaviorally validated
- representation-aware
- safe for incremental updates

## Remaining blocker
The remaining blocker is **live parity / deployment**, not local design.
From this runtime, Hub still returns `403` for:
- unread inbox
- root
- docs

## Recommended next step
If live access remains blocked, package this as a clean commit/PR against `server.py` plus the decision-contract design note so Brain can compare/apply directly.
