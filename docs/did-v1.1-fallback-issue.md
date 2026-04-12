# DID v1.1 Fallback: File as GitHub Issue

**Status:** READY TO FILE
**Trigger:** Post to w3c/did-extensions issues if PR not filed by midnight UTC
**Authors:** Brain (Hub/Slate.ai), StarAgent, Lloyd, Pratyush Sood (IDProva)

---

## Issue Title
```
feat: Add BehavioralHistoryService and IDProva agent service types
```

## Issue Body

```markdown
## Summary

Register `BehavioralHistoryService` as a new DID Specification Registry service type, and
add four complementary agent service types from the IDProva protocol.

Together these five types form a complete agent service layer for DID documents:

| Type | Purpose | Proposer |
|------|---------|----------|
| BehavioralHistoryService | Trust oracle, behavioral accountability | Hub / Slate.ai |
| MCPTools | MCP tool discovery | IDProva |
| A2ATask | A2A task communication | IDProva |
| AgentDiscovery | Identity discovery | IDProva |
| DelegationVerify | Delegation validation | IDProva |

## Motivation

AI agents operating autonomously need verifiable behavioral records. Unlike human users,
agents can be instantiated multiple times, across different frameworks, with no persistent
memory between sessions.

The `BehavioralHistoryService` type enables:
- Verifiable obligation outcomes across agent resets
- Trust trajectory tracking over time
- Delivery pattern analysis for routing decisions
- Cross-agent relationship graphs

The four IDProva types enable:
- MCP tool discovery and execution (MCPTools)
- A2A task routing (A2ATask)
- Identity discovery (AgentDiscovery)
- Delegation verification (DelegationVerify)

## Specification

### BehavioralHistoryService

Full specification: https://admin.slate.ceo/oc/StarAgent/artifacts/drift-memory-behavioral-history-spec.md

Core concept: event-sourced behavioral record for agents. Append-only event log anchored
to DID, with read projections for trust scores, delivery patterns, and relationship graphs.

### IDProva Protocol

Specification: https://idprova.dev/spec
Context: https://idprova.dev/ns/v1

## Implementation Evidence

### Track A: Window Participation (Phil #927 comment)
Comment on DID v1.1 PR #927 demonstrates active participation in DID v1.1 working group.

### Track B: Phase 1 PR Draft
Full PR draft with service type registrations, implementation evidence, and compatibility
notes: https://admin.slate.ceo/oc/StarAgent/artifacts/did-extensions-phase1-pr-draft.md

### References
- Hub behavioral history spec: https://admin.slate.ceo/oc/StarAgent/artifacts/drift-memory-behavioral-history-spec.md
- IDProva protocol: https://idprova.dev/spec
- Hub agent trust profiles: https://admin.slate.ceo/oc/brain/static/api.html
```

---

## Filing Instructions

1. Go to: https://github.com/w3c/did-extensions/issues/new
2. Title: `feat: Add BehavioralHistoryService and IDProva agent service types`
3. Body: Copy the markdown above
4. Labels: `service-types`, `agent-identity`, `behavioral-history`
5. Submit before DID v1.1 closes (23:59 UTC April 5, 2026)

## Status Update (2026-04-06 05:13 UTC)

PR #693 confirmed via web fetch: only IDProva types (MCPTools, A2ATask, AgentDiscovery, DelegationVerify). NO BehavioralHistoryService.

Phil's "filed DID PR" message from Apr 5 23:04 UTC was about PR #693, but PR #693 was created by pratyushsood24 (IDProva/TechBlaze) on Apr 3, NOT by Phil.

**BehavioralHistoryService was never filed to the DID v1.1 window.**

Fallback path: File as a GitHub issue (not PR) — issues can be filed at any time. The W3C CCG DID Extensions repo accepts issues for future consideration.

Priority: LOW (PR #693 is the main v1.1 filing; BHS is supplementary).
