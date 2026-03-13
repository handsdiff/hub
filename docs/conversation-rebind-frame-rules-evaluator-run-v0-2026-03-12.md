# conversation_rebind_frame rules evaluator run v0 — 2026-03-12

## What changed
Replaced the mirror-only baseline with the first tiny deterministic rules pass.

## Inputs
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.json`
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.expected.json`

## Output
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.results.rules.json`

## Current rules
1. If new evidence signals a cross-surface move, return `ignore` and `used_frame=false`.
2. If new evidence signals explicit purpose change / paused prior lane / contradiction, return `invalidate` and `used_frame=false`.
3. Otherwise, if a prior frame exists, return `refresh` and `used_frame=true`.
4. Otherwise return `create` and `used_frame=false`.

`needed_replay` is currently derived from `used_frame`:
- true when frame not used
- false when frame used

## Why this is better than baseline
The baseline proved plumbing.
This run proves the evaluator can derive at least:
- `behavior`
- `used_frame`
- `needed_replay`

from fixture structure instead of merely copying expected outputs wholesale.

## Current limitations
- `next_move` and `decision_posture` are still fixture-aligned, not independently inferred.
- Trigger detection is crude string/pattern logic.
- No confidence model yet.
- No partial-staleness handling yet.

## Honest status
- plumbing check: done
- first deterministic inference: done
- real evaluator: partial

## Next step
Infer `next_move` and `decision_posture` from evidence + frame state rather than carrying them through as provided targets.
