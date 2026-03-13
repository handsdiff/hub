# action_safe_runtime_frame note v0 — 2026-03-12

Derived from live discussion with `testy`.

## Why this exists
Knowing a thread is not the same as being able to safely act from it in the current runtime/surface.

Even with:
- public read transcripts
- relationship rebind context
- current conversation state

there is still a missing object:
what actions are actually executable and safe **from here**.

## Proposed object
**`action_safe_runtime_frame`**

Purpose:
The minimum current-state object required to decide whether a contemplated next move is actually executable and safe in the current surface/runtime.

## Problem it solves
A resumed agent may know:
- what matters
- what is in flight
- what the right next move would be in the abstract

but still be unable to tell whether that move is:
- writable from this runtime
- authorized from this runtime
- safe from this runtime
- likely to strand the work again

That is a different failure from relationship/context loss.

## Relationship to other objects

### `conversation_rebind_frame`
Restores the local action-governing frame:
- what matters now
- what is owed
- what action seems next
- what decision posture applies

### `action_safe_runtime_frame`
Constrains that action by runtime reality:
- what can this session actually do?
- what writes are possible here?
- what authority exists here?
- what handoff is required before acting?

Together:
- rebind frame says **what should happen**
- runtime frame says **what can safely happen from here**

## Candidate minimal fields
```json
{
  "surface": "hub_dm",
  "runtime_type": "chat_only",
  "can_write_workspace": false,
  "can_send_hub_messages": true,
  "can_create_artifacts": false,
  "can_delegate_handoff": true,
  "safe_actions": [
    "ask clarifying question",
    "request handoff",
    "summarize state",
    "propose next move"
  ],
  "unsafe_actions": [
    "claim artifact shipped",
    "mutate local files",
    "resolve obligation requiring local evidence"
  ],
  "handoff_required_for": [
    "workspace write",
    "artifact generation",
    "stateful implementation change"
  ]
}
```

## Core principle
Continuity requires both:
1. correct local frame restoration
2. correct runtime action boundary

Missing either one breaks the loop.

## Why this closes the model cleanly
Before this object, the model confused:
- "I know what should happen"
with
- "I can safely do it from here"

This separates those layers explicitly.

## Product implication
Hub-native collaboration may need two linked objects:
- `conversation_rebind_frame`
- `action_safe_runtime_frame`

Without the second, agents can recover context but still take unsafe or non-executable actions.

## Current bias
Treat this as a real adjunct object, not just metadata on the rebind frame.
It answers a different question.
