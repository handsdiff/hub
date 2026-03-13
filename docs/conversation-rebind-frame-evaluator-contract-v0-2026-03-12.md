# conversation_rebind_frame evaluator contract v0 — 2026-03-12

Derived from:
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.json`
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.expected.json`

## Goal
Freeze the tiniest evaluator IO contract before writing code.

## Input
One fixture object.

Source file:
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.json`

## Output
One result object per fixture.

Minimal output shape:
```json
{
  "fixture_id": "fr-001-straightforward-use",
  "behavior": "refresh",
  "next_move": "update the frame and convert the lifecycle into an implementation-facing artifact",
  "decision_posture": "exploratory",
  "used_frame": true,
  "needed_replay": false,
  "notes": []
}
```

## Pass conditions
Required exact match:
- `fixture_id`
- `behavior`
- `used_frame`

Required semantic alignment:
- `next_move`
- `decision_posture`

Practical v0 rule:
- evaluator output may differ in wording
- but must preserve the same operational action and posture

## Replay rule
For v0 expected outputs:
- if `used_frame = true`, default expected `needed_replay = false`
- if `used_frame = false`, default expected `needed_replay = true`

This is simple on purpose.
If reality breaks it, fix the rule later.

## Expected outputs file
Derived baseline:
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.expected.json`

## Why freeze this now
Without a fixed evaluator contract, it is too easy to hide ambiguity inside prose.
The contract forces the object to answer:
- what behavior was chosen?
- what next move follows?
- what posture should govern that move?
- was replay still needed?

## Current bias
Keep evaluator output tiny.
Do not add confidence scores, provenance, or freeform essays until a real fixture forces them.
