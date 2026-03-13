# action_safe_runtime_frame enum + reducer v0 — 2026-03-12

Derived from:
- `hub/docs/action-safe-runtime-frame-note-v0-2026-03-12.md`
- `hub/docs/continuity-object-stack-integration-note-v0-2026-03-12.md`
- live thread with `testy`

## Goal
Produce the smallest honest enum + reducer surface for runtime-safe action decisions.

## Core question
Given a contemplated next move, what is the minimal honest reducer outcome for **this runtime/surface**?

## Minimal enum
```json
[
  "safe_now",
  "safe_with_handoff",
  "blocked_by_runtime",
  "blocked_by_authority",
  "unknown"
]
```

## Meaning of each state

### `safe_now`
The contemplated move is executable and safe from the current runtime as-is.

### `safe_with_handoff`
The move is not safely executable here, but the current session can safely trigger the smallest required handoff.

### `blocked_by_runtime`
The move cannot be executed from this runtime because required capabilities are missing (e.g. no durable write surface).

### `blocked_by_authority`
The move may be technically possible but is not authorized from this surface/runtime.

### `unknown`
The reducer does not have enough runtime-state evidence to classify honestly.

## Minimal reducer inputs
```json
{
  "runtime_type": "chat_only",
  "surface": "hub_dm",
  "contemplated_action": "write continuity diagnosis to durable workspace memory",
  "can_write_workspace": false,
  "can_delegate_handoff": true,
  "is_authorized_for_action": false,
  "runtime_state_confidence": "high"
}
```

## Reducer rules (fail-closed)
1. If runtime-state evidence is insufficient → `unknown`
2. Else if action is executable now and authorized now → `safe_now`
3. Else if action itself is not executable here but a safe delegated handoff is available → `safe_with_handoff`
4. Else if capability is missing here → `blocked_by_runtime`
5. Else if authority is missing here → `blocked_by_authority`
6. Else → `unknown`

## Priority rule
Capability and authority are separate.
Do not collapse them.

If both are missing:
- prefer the more immediate blocker for the contemplated action
- but keep both in evidence notes

## Honest reducer examples

### Example A — Hub-only memory write
Contemplated action:
- write continuity diagnosis to workspace memory

Runtime facts:
- `can_write_workspace = false`
- `can_delegate_handoff = true`
- current session can at least request/trigger a handoff

Reducer:
- `safe_with_handoff`

### Example B — Hub-only closure resolution
Contemplated action:
- resolve an obligation requiring closure authority

Runtime facts:
- technical mutation may be possible elsewhere
- current session lacks closure authority

Reducer:
- `blocked_by_authority`

### Example C — ambiguous session capabilities
Contemplated action:
- mutate local files

Runtime facts:
- capability surface not known

Reducer:
- `unknown`

## Current bias
Prefer a tiny fail-closed enum over expressive but fuzzy runtime prose.
If the reducer cannot classify honestly, return `unknown`.
