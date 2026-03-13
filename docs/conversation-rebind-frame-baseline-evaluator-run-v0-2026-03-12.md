# conversation_rebind_frame baseline evaluator run v0 — 2026-03-12

## What this is
First non-empty evaluator artifact.

This is **not** a real implementation yet.
It is a baseline placeholder run that proves the offline evaluation surface is wired end-to-end:
- fixtures exist
- expected outputs exist
- results file exists
- one result object is emitted per fixture

## Inputs
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.json`
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.expected.json`

## Output
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.results.baseline.json`

## Current behavior
The baseline evaluator simply mirrors the expected fixture fields into result objects.
That means it is only useful as a plumbing check, not as evidence that the evaluator can reason.

## Why this still matters
It creates the first executable loop:
1. fixture input
2. evaluator output
3. comparable expected output

Without this step, it is too easy to keep refining docs without ever creating a runnable surface.

## What is still missing
A real evaluator must infer from fixture content rather than copy expected values.
The next honest step is a tiny rules-based evaluator that derives at least:
- `behavior`
- `used_frame`
- `needed_replay`

from fixture structure and new evidence.

## Current status
- pipeline exists: yes
- reasoning exists: no
- useful for plumbing check: yes
- useful for product claims: no

## Next step
Replace the mirror baseline with a small deterministic rules evaluator.
