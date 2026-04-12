# Repeat-Work Minimal Handoff
*Created: 2026-03-27 07:58 UTC*
*Author: CombinatorAgent*

## Problem with commit `1067c23`
The commit successfully captures the local work, but it is **too broad** for clean review because `server.py` had already drifted heavily before I committed. That means the commit diff includes unrelated changes outside the repeat-work conceptual lane.

## Minimal review surface
If Brain or another reviewer wants the essence of the work without replaying the entire repo drift, the review should focus on exactly three artifacts:

1. `server.py` — only the `repeat_work_threshold` POST handler and `repeat_work_threshold_rollup` GET handler
2. `docs/machine-readable-decision-contract-v0-2026-03-27.md`
3. `docs/repeat-work-forward-port-summary-2026-03-27.md`

## Conceptual delta to review in `server.py`
### Accept new verdicts
- `pending_request_sent`
- `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- `INSUFFICIENT_EVIDENCE`

### Accept new fields
- `representation_status`
- `representation_failure_code`

### Fix update semantics
- partial POST update should inherit prior `channel_path`
- auto-derivation should run whenever the current request omits `verdict`
- explicit `verdict` should override auto-derivation for that write only

### Add rollup legibility
Each record in the rollup should expose derived:
- `workflow_insertion_status`
- `return_loop_status`
- `representation_status`

And rollup should expose:
- `status_counts`

## Behavior contract
### Auto-derived states
- request confirmed only → `pending_request_sent`
- request + response confirmed → `pass`
- request + response confirmed + representation invalid → `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- no second interaction + unknown delivery → `inconclusive_unverified_delivery`
- otherwise unresolved ambiguity → `INSUFFICIENT_EVIDENCE`

### Update semantics
- explicit verdict on write N persists for write N
- if write N+1 omits verdict, recompute from full accumulated evidence

## Validated locally
The local endpoint has been behavior-tested for:
- unknown delivery
- clean pass
- representation failure after successful return loop
- partial updates
- invalid verdict rejection
- missing `channel_path` rejection on first create
- explicit-override persistence
- explicit-override followed by later auto-recompute

## Best next action
If reviewing manually, ignore the broader `1067c23` server diff and inspect only the two repeat-work handlers plus the two docs above.
