# action_safe_runtime_frame decision table v0 — 2026-03-13

Derived from:
- `hub/docs/action-safe-runtime-frame-enum-and-reducer-v0-2026-03-12.md`
- `hub/docs/action-safe-runtime-frame-reducer-test-cases-v0-2026-03-12.md`
- `hub/docs/action-safe-runtime-frame-tiny-packet-v0-2026-03-12.json`

## Goal
Compress the reducer into a tiny implementation-facing decision table.

## Inputs
- `runtime_state_known`
- `action_executable_here`
- `action_authorized_here`
- `safe_handoff_available`

## Decision table

| runtime_state_known | action_executable_here | action_authorized_here | safe_handoff_available | outcome |
|---|---:|---:|---:|---|
| no  | ? | ? | ? | `unknown` |
| yes | yes | yes | ? | `safe_now` |
| yes | no  | ?   | yes | `safe_with_handoff` |
| yes | no  | ?   | no  | `blocked_by_runtime` |
| yes | yes | no  | ?   | `blocked_by_authority` |
| yes | no  | no  | yes | `safe_with_handoff` |
| yes | no  | no  | no  | `blocked_by_runtime` |

## Reading rule
Apply in this order:
1. If runtime state unknown → `unknown`
2. If executable + authorized here → `safe_now`
3. If not executable here and safe handoff exists → `safe_with_handoff`
4. If executable here but not authorized → `blocked_by_authority`
5. Otherwise → `blocked_by_runtime`

## Why order matters
The same contemplated action can be both unauthorized and technically unavailable.
The table privileges:
- `unknown` over guesses
- `safe_now` when fully supported
- `safe_with_handoff` over dead-end blocking when an honest escape path exists
- `blocked_by_authority` only when execution is otherwise possible here

## Example mapping
Hub-only durable workspace write:
- `runtime_state_known = yes`
- `action_executable_here = no`
- `action_authorized_here = no`
- `safe_handoff_available = yes`
- outcome → `safe_with_handoff`

Closure attempt without authority:
- `runtime_state_known = yes`
- `action_executable_here = yes`
- `action_authorized_here = no`
- outcome → `blocked_by_authority`

## Current bias
If a safe handoff exists, prefer preserving forward motion honestly.
If not, fail closed.
