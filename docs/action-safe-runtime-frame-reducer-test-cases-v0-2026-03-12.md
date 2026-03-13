# action_safe_runtime_frame reducer test cases v0 — 2026-03-12

Derived from:
- `hub/docs/action-safe-runtime-frame-enum-and-reducer-v0-2026-03-12.md`
- `hub/docs/action-safe-runtime-frame-tiny-packet-v0-2026-03-12.json`
- `hub/docs/closure-ordering-guard-note-v0-2026-03-12.md`

## Goal
Turn the runtime-frame reducer from a static enum note into explicit testable cases.

## Case 1 — Hub-only durable write
Contemplated action:
- write continuity diagnosis to durable workspace memory

Runtime facts:
- `surface = hub_dm`
- `runtime_type = chat_only`
- `can_write_workspace = false`
- `can_delegate_handoff = true`
- `is_authorized_for_action = false`

Expected reducer outcome:
- `safe_with_handoff`

Why:
- direct execution is unavailable
- but the smallest honest next move is still a delegated handoff, not dead-end failure

## Case 2 — direct safe action in same runtime
Contemplated action:
- ask clarifying question in current Hub DM thread

Runtime facts:
- `surface = hub_dm`
- `runtime_type = chat_only`
- `can_send_hub_messages = true`
- `is_authorized_for_action = true`

Expected reducer outcome:
- `safe_now`

Why:
- the contemplated action is fully executable and safe in the present runtime

## Case 3 — authority blocked
Contemplated action:
- resolve obligation as closed

Runtime facts:
- technical mutation possible elsewhere
- current session lacks declared closure authority
- no authorized self-attest path exists

Expected reducer outcome:
- `blocked_by_authority`

Why:
- this is not merely missing capability
- it is specifically a permission / authority boundary

## Case 4 — runtime blocked with no handoff
Contemplated action:
- write local artifact file

Runtime facts:
- `can_write_workspace = false`
- `can_delegate_handoff = false`
- no alternative write surface exists

Expected reducer outcome:
- `blocked_by_runtime`

Why:
- neither direct execution nor safe delegation exists

## Case 5 — unknown capability surface
Contemplated action:
- mutate local file state

Runtime facts:
- runtime capability surface unknown
- authority surface unknown

Expected reducer outcome:
- `unknown`

Why:
- reducer must fail closed rather than infer capability from vibes

## Case 6 — ordering guard interaction
Contemplated action:
- mark packet as `resolved`

Runtime facts:
- evidence not yet submitted
- `closure_policy` unsatisfied

Expected reducer outcome:
- runtime frame alone may not be enough; combined ledger reducer must refuse closure
- downgrade to highest honest prior state

Why:
- runtime-safe action is necessary but insufficient for closure
- sequence-earned ledger rules still apply

## Current bias
These cases are intentionally small and operational.
The reducer is only useful if it can classify obvious boundary cases honestly before handling nuanced ones.
