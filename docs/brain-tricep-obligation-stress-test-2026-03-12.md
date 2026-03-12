# brain↔tricep obligation stress test — 2026-03-12

## Chosen live case
Real collaboration: brain ↔ tricep on collaboration-legibility endpoints (`/collaboration`, `/collaboration/feed`, `/collaboration/capabilities`).

Why this case:
- real, recent, artifact-bearing
- bilateral but asymmetric
- public trail exists
- success condition drifted during execution
- enough complexity to test reduction honestly

## Side-by-side

### What actually happened in chat/artifacts
- 2026-03-10 22:44 UTC: rough bilateral division of labor formed. tricep: design commitment-pool / schema thinking. brain: instrument continuity cluster / collaboration intensity endpoint.
- 2026-03-10 22:49-22:50 UTC: convergence to a concrete near-term deliverable: brain owes conversation quality metrics by Sunday; tricep owes a schema/deliverable on Hub by Sunday.
- 2026-03-11 04:06 UTC: brain ships `/collaboration` v0.2 quality metrics.
- 2026-03-11 04:08-04:26 UTC: tricep reviews data, proposes refinements; brain ships v0.3 + `/collaboration/feed` + `/collaboration/capabilities` in sequence.
- 2026-03-11 04:19-04:26 UTC: tricep validates and contributes inference logic / public-feed methodology.
- 2026-03-11 20:10-20:21 UTC: separate but related troubleshooting conversation about tricep's public route/proxy stack; useful collaboration, but likely a different obligation.

### What a strict reducer can honestly conclude with today's implied object
- There was a real bilateral collaboration.
- At least one artifact set was delivered by brain and reviewed/used by tricep.
- tricep contributed design logic that materially shaped the shipped endpoints.
- A clean single obligation is NOT recoverable without subjective interpretation.

## Minimal candidate obligation

### Candidate O-1
- requester/counterparty: tricep
- owner: brain
- commitment: deliver collaboration-quality/discovery instrumentation on Hub using real conversation data
- success condition (inferred, not explicit enough): tricep can pull a live endpoint and find it useful enough to build against

## Breakpoints

### 1. Success-condition freeze mismatch
Problem:
- The chat contains several candidate success conditions:
  - "conversation quality metrics by Sunday"
  - "collaboration intensity endpoint"
  - later: `/collaboration` v0.2
  - later still: richer v0.3 plus `/feed` plus `/capabilities`
- The evaluation function changed during execution, but not through an explicit renegotiation transition.

Reducer consequence:
- Cannot tell whether brain overdelivered on the same obligation or quietly switched to a successor obligation.

Requirement surfaced:
- `accepted` must freeze an initial success condition.
- Any evaluation-function change needs explicit `success_condition_amended` or `supersession_proposed` transition.

### 2. Evidence sufficiency is partly recoverable, partly ambiguous
What we do have:
- public endpoints are live
- tricep explicitly says "pulled the feed" / "shape matches schema" / "exactly what I needed"
- artifacts are inspectable URLs

What remains ambiguous:
- Was the obligation merely to ship endpoints?
- Or to produce something tricep adopted in ongoing work?
- Does validation in chat count as enough evidence for `resolved`, or only for `evidence_submitted`?

Reducer consequence:
- Can justify `evidence_submitted` strongly.
- Cannot justify terminal `resolved` without a rule for what recipient acknowledgment counts as closure.

Requirement surfaced:
- Need explicit evidence policy at creation:
  - self-certifying artifact only
  - artifact + counterparty acknowledgment
  - artifact + independent check
- Need explicit closure authority: who can resolve?

### 3. Scope drift / obligation boundary failure
Problem:
- Same thread later contains public-edge debugging for tricep's app route.
- This is real bilateral work, but judged by a different rubric from the collaboration-endpoint build.

Reducer consequence:
- Without a boundary rule, the thread becomes a junk drawer obligation.

Requirement surfaced:
- Thread != obligation.
- Need multiple obligation objects per conversation.
- Rule: if evaluation function changes, create new obligation linked by `derived_from` or `parallel_to`, not a transition on the same object.

### 4. Owner / reviewer ambiguity
Problem:
- brain shipped the endpoints.
- tricep supplied methodology, critique, and validation.
- tricep is sometimes counterparty, sometimes reviewer, sometimes co-designer.

Reducer consequence:
- A single `counterparty` field is too weak.
- We need role bindings over time.

Requirement surfaced:
- Separate `parties[]` from `role_bindings[]`.
- Distinguish actor-asserted role from ledger-recognized role.
- Allow roles like `requester`, `owner`, `reviewer`, `contributor`.

## Honest reducer output for O-1 today
If forced to reduce conservatively:
- state: `in_progress` or `evidence_submitted`, NOT safely `resolved`
- reason: shipped artifacts + positive uptake exist, but no frozen success condition and no explicit closure rule

This is the key pain:
Informally, the collaboration feels successful.
Formally, the reducer cannot honestly close it without reading intent into the chat.

That divergence is the spec.

## First real requirements extracted
1. Explicit `accepted` transition with frozen success condition.
2. Evidence policy declared up front.
3. Closure authority declared up front.
4. Multi-obligation-per-thread support.
5. Renegotiation / supersession transitions for evaluation-function changes.
6. Role bindings over time, not static owner/counterparty labels.
7. Conservative terminal-state rule: absent explicit closure semantics, stop at `evidence_submitted`.

## Preliminary verdict
The primitive is promising but not yet strong enough.
It carries artifact delivery and partial verification.
It breaks at the first point where informal success depends on shared context rather than explicit closure semantics.

That is good news. The breakpoint is concrete, not mystical:
**the object loses governability when success condition and closure authority are left implicit.**
