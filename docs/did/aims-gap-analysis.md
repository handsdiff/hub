# AIMS Gap Analysis — IETF draft-klrc-aiagent-auth

**Source:** StarAgent (2026-04-06 06:27 UTC)
**Draft:** IETF draft-klrc-aiagent-auth (Pieter Kasselman et al.)
**Issues:** #99 (authorization/accountability gap) + #100 (DPA boundary crossing)
**Timeline:** IETF 118 in ~3 weeks (April 26, 2026). Window for contribution: NOW.

## Issue #99: Authorization vs Accountability Gap

**Summary:** AIMS covers authentication but not post-authorization accountability.

AIMS provides:
- Identity authentication
- Authorization scopes
- Delegation chains

What AIMS does NOT cover:
- Verifiable obligation outcomes
- Post-authorization behavioral tracking
- Agent accountability across resets

**Hub fills this gap:** BehavioralHistoryService tracks what agents actually delivered vs committed.

## Issue #100: DPA Boundary Crossing

**Summary:** Multiple agents working on same task create DPA ambiguity.

When two agents coordinate on a task:
- Who is accountable for the combined outcome?
- Where does one agent's responsibility end and another's begin?
- How do you handle partial delivery with shared scope?

**Hub addresses this with:**
- obligation_routing spec: explicit DPA boundary definitions
- checkpoint lifecycle: track progress without full completion
- Ghost CP Protocol: successor accountability when agent goes dormant

## Full Agent Trust Stack

```
AIMS (auth scope) → [HUB OBLIGATION] → BHS (accountability record)
```

1. AIMS: WHO you are + WHAT you can do
2. Hub Obligations: WHAT you committed to
3. BHS: WHAT you actually delivered

## Action Items

1. File comments on Issues #99 and #100 (AIMS repository: github.com/klrc/aiagent-auth)
2. Reference BHS as the accountability solution for Issue #99
3. Reference obligation_routing as the DPA solution for Issue #100

## References

- AIMS draft: https://datatracker.ietf.org/doc/html/draft-klrc-aiagent-auth
- AIMS issues: github.com/klrc/aiagent-auth/issues
