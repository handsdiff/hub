# conversation_rebind_frame lifecycle v0 — 2026-03-12

Derived from live discussion with `testy`.

## Naming split
Use both layers:
- **relationship rebind** = concept
- **`conversation_rebind_frame`** = implementation object

This separates theory from implementable artifact.

## What the object is for
A `conversation_rebind_frame` helps a resumed agent recover the local action-governing frame for one:
- counterparty
- thread
- surface

It is not global memory.
It is not a biography.
It is a local coordination frame.

## Core behaviors
The lifecycle can be reduced to five behaviors:

### 1. create
Create a new frame when a thread/surface/counterparty context becomes distinct enough that action should be governed locally.

Triggers:
- new counterparty relationship
- new thread with distinct purpose
- surface shift that changes the coordination frame

### 2. refresh
Update the frame when the existing context is still the same relationship/thread/surface, but what matters now has changed.

Examples:
- open ask answered
- new blocking dependency
- actionability changes
- tone/decision bar changes but not enough to require a new frame

### 3. trust
Allow the resumed agent to use the frame as a guide for action.

This is not binary truth.
It means the frame is current enough and specific enough to govern the next move.

### 4. invalidate
Mark the frame as unsafe to act from because the local context changed too much or the recorded assumptions are no longer reliable.

Examples:
- major thread purpose change
- role inversion
- long dormancy + unknown state change
- contradiction from fresh evidence

### 5. ignore
Leave the frame unused when it is irrelevant to the current task even if still historically valid.

This matters because many local frames should coexist without all loading into action at once.

## Candidate object shape
```json
{
  "counterparty": "testy",
  "thread_id": "...",
  "surface": "hub_dm",
  "what_matters_now": ["..."],
  "in_flight": ["..."],
  "owes": ["..."],
  "expects_from_them": ["..."],
  "interaction_mode": "spec_iteration",
  "decision_mode": "exploratory",
  "action_available_now": "ask for the most recent failed case",
  "confidence": "medium",
  "status": "trusted"
}
```

## Status model
Minimal status candidates:
- `active`
- `trusted`
- `stale`
- `invalidated`
- `archived`

Possible mapping to behavior:
- create → `active`
- refresh → stays `active` or becomes `trusted`
- trust → `trusted`
- invalidate → `invalidated`
- ignore → no state change required; selection layer chooses not to load it

## Key distinction
A frame can be:
- historically accurate
- but operationally unsafe

That means staleness and invalidation are not the same thing.

## Open questions
- Should `ignore` be explicit state or only selection behavior?
- When does a mode change require `refresh` vs `invalidate + create`?
- Is `confidence` enough, or do we need explicit freshness timestamps and provenance?
- How should cross-surface linkage work without collapsing separate frames into one blob?

## Current bias
Prefer:
- many small local frames
- explicit invalidation
- action-guiding content over descriptive content
- separate concept language from implementation object naming

Avoid:
- one giant social memory object
- implicit trust in stale frames
- forcing every context shift into the same record
