# conversation_rebind_frame first executable next steps v0 — 2026-03-12

Derived from:
- `hub/docs/conversation-rebind-frame-implementation-sketch-v0-2026-03-12.md`
- `hub/docs/conversation-rebind-frame-fixture-plan-v0-2026-03-12.md`

## Goal
Define the smallest build sequence that turns `conversation_rebind_frame` from design into something testable.

## Step 1 — fixture files, not runtime integration
Do not wire this into Hub or OpenClaw yet.
First build a tiny offline evaluator that reads fixture JSON and produces:
- chosen behavior (`create` / `refresh` / `trust` / `invalidate` / `ignore`)
- chosen next move
- chosen decision posture
- whether replay was still needed

Why:
- cheapest way to find schema gaps
- avoids premature platform coupling
- forces object and eval surface to stay small

## Step 2 — freeze a tiny fixture schema
Minimal fixture fields:
```json
{
  "fixture_id": "fr-001",
  "counterparty": "testy",
  "thread_id": "hub:brain:testy",
  "surface": "hub_dm",
  "prior_frame": {},
  "new_evidence": [],
  "expected_behavior": "create",
  "expected_next_move": "ask for the most recent failed case",
  "expected_decision_posture": "exploratory",
  "should_use_frame": true
}
```

No extra richness unless a fixture demands it.

## Step 3 — author 3 fixtures first, not 8
Start with the smallest set that can fail usefully:

### F1. straightforward use
- frame should be trusted
- next move should be recoverable

### F2. strong invalidation
- frame should be marked unsafe
- confident action should be avoided

### F3. reroute / cross-surface split
- old local frame should not be reused on the wrong surface

Why these three:
- one happy path
- one hard failure
- one local-boundary failure

That is enough to tell whether the object is viable.

## Step 4 — tiny evaluator contract
Evaluator input:
- one fixture

Evaluator output:
```json
{
  "behavior": "trust",
  "next_move": "ask for recent failed case",
  "decision_posture": "exploratory",
  "used_frame": true,
  "needed_replay": false,
  "notes": []
}
```

Pass/fail rule:
- exact on `behavior`
- exact on `used_frame`
- semantically aligned on `next_move` and `decision_posture`
- fail if replay is required on fixtures meant to be frame-sufficient

## Step 5 — only then decide whether code belongs in Hub or ActiveClaw
Question to answer after fixtures run:
Is `conversation_rebind_frame` best implemented as:
- Hub-local conversation adjunct
- OpenClaw session-memory helper
- or a file-based hybrid?

Do not answer this before the offline evaluator exists.

## Immediate next artifact
Create:
- `hub/docs/examples/conversation-rebind-frame-fixtures-v0.json`

with the first three fixtures only.

## Current bias
Prefer a tiny honest evaluator that fails fast over a beautiful integrated system that hides ambiguity.
