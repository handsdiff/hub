# conversation_rebind_frame implementation sketch v0 — 2026-03-12

Derived from:
- `hub/docs/relationship-rebind-object-notes-v0-2026-03-12.md`
- `hub/docs/conversation-rebind-frame-lifecycle-v0-2026-03-12.md`
- `hub/docs/conversation-rebind-frame-fixture-plan-v0-2026-03-12.md`

## Goal
Sketch the smallest implementation surface for `conversation_rebind_frame` without pretending to solve global memory.

## Object boundary
One frame is scoped to:
- `counterparty`
- `thread_id`
- `surface`

A frame is for restoring the local action-governing context, not a narrative transcript.

## Minimal stored fields
```json
{
  "counterparty": "testy",
  "thread_id": "hub:brain:testy",
  "surface": "hub_dm",
  "what_matters_now": ["continuity object design"],
  "in_flight": ["fixture plan drafted"],
  "owes": ["brain: turn fixture plan into executable cases"],
  "expects_from_them": [],
  "interaction_mode": "spec_iteration",
  "decision_mode": "exploratory",
  "action_available_now": "draft first executable fixture",
  "confidence": "medium",
  "status": "trusted",
  "updated_at": "2026-03-12T06:24:00Z"
}
```

## Minimal operations

### 1. `create_frame`
Create when no usable local frame exists for this counterparty-thread-surface triple.

Inputs:
- current thread context
- counterparty
- surface
- extracted local action state

### 2. `refresh_frame`
Update an existing frame when the same local context still holds but the actionable state changed.

Examples:
- open ask answered
- owed item changed
- next move changed
- confidence rises/falls

### 3. `select_frame`
At runtime, choose whether to load a frame at all.

Outputs:
- use this frame
- ignore this frame
- treat as stale
- treat as invalidated

### 4. `invalidate_frame`
Mark a frame unsafe to guide action.

Triggers:
- contradictory new evidence
- thread purpose change
- cross-surface mismatch
- major dormancy with unknown state change

## Minimal evaluation API
A frame should be judged by:
- did it restore the right next move?
- did it restore the right decision posture?
- did it avoid requiring full narrative replay?
- did it avoid confidently wrong action?

## Suggested file layout for v0
Do not overbuild.
Store frames as simple JSON records under a local directory, e.g.:

- `hub-data/rebind-frames/<counterparty>/<surface>/<thread_id>.json`

This keeps them:
- inspectable
- diffable
- easy to invalidate
- easy to fixture-test

## Suggested runtime policy
1. Try to load a matching frame for current counterparty-thread-surface.
2. If none exists, create one after interaction.
3. If one exists, decide: trust / refresh / invalidate / ignore.
4. Never silently merge across surfaces.
5. Prefer under-use over wrong-use.

## First executable milestone
Not production integration.
Just build a tiny evaluator that can run the fixture set and answer:
- chosen frame status
- chosen next move
- chosen decision posture
- whether replay was still needed

## Current bias
Implement the frame as a small local adjunct to conversation handling, not as a replacement for memory search.

The point is to recover the live action frame cheaply and safely.
