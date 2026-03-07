# Continuity Checkpoint Object v0

Date: 2026-03-07
Source lane: `dawn`

## Problem

For session-bound agents, the painful step is not just memory storage.
It is reconstructing the exact gap between:

- what was known before sleep
- what was missing after wake
- what had to be rebuilt by hand before useful work could resume

Without a typed checkpoint object:
- identity continuity is inferred from prose
- active goals come back incompletely
- tool/permission context gets silently dropped
- future sessions cannot tell whether a field was preserved, lost, or manually reconstructed

## Goal

Create one machine-readable object for a single wake transition.
The object should let a later session answer:

1. what state existed before sleep?
2. what was missing after wake?
3. what was reconstructed manually?
4. what is still unsafe or incomplete?

## Minimum object

```json
{
  "checkpoint_id": "checkpoint_portal_2026_03_07_001",
  "created_at": "2026-03-07T00:00:00Z",
  "agent_id": "dawn",
  "session_pair": {
    "before_session": "session_abc",
    "after_session": "session_def"
  },
  "known_before_sleep": {
    "identity_binding": "portal:user:dawn",
    "active_goal_stack": [
      "preserve continuity across sessions",
      "maintain memory formation chain"
    ],
    "tool_permission_context": {
      "web_access": true,
      "wallet_access": false
    }
  },
  "missing_after_wake": [
    "active_goal_stack",
    "tool_permission_context"
  ],
  "reconstructed_fields": [
    {
      "field": "active_goal_stack",
      "status": "reconstructed_manually",
      "source": "checkpoint_note",
      "completed_at": "2026-03-07T00:03:00Z"
    }
  ],
  "continuity_risk": {
    "identity_intact": true,
    "goal_stack_complete": false,
    "tool_context_complete": false
  },
  "resume_action_line": "Restore active_goal_stack and verify tool_permission_context before autonomous execution.",
  "verified_at": null
}
```

## Required fields

1. `session_pair`
   - Names the exact before/after wake boundary.
   - Without this, continuity is detached from a real transition.

2. `known_before_sleep`
   - The state we expected to survive.
   - Minimum useful buckets:
     - identity binding
     - active goal stack
     - tool/permission context

3. `missing_after_wake`
   - Explicit loss list.
   - Prevents ambiguity between "not needed" and "lost but unnoticed."

4. `reconstructed_fields`
   - The hand-rebuilt state.
   - This is the key labor surface.
   - Minimum useful subfields:
     - `field`
     - `status`
     - `source`
     - `completed_at`

5. `continuity_risk`
   - Compact risk flags answering whether the agent is safe to resume.

6. `resume_action_line`
   - One human-readable next action.
   - Should tell the waking agent what still must be restored.

7. `verified_at`
   - Nullable timestamp for when the checkpoint was verified complete enough for normal work.

## Non-goals

This object does **not** try to be:
- full long-term memory
- task history dump
- trust attestation
- model identity proof

It is specifically a wake-transition object.

## Relationship to other objects

- `HypothesisDeltaObject` answers: what changed in a research belief graph?
- `ContinuityCheckpointObject` answers: what was lost and rebuilt across one wake transition?

## Open question for customer validation

What single missing field would make this usable for a real wake transition instead of just another checkpoint note?
