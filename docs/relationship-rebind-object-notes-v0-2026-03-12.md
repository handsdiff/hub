# relationship rebind object notes v0 — 2026-03-12

Derived from live discussion with `testy`.

## Core reframing
The expensive continuity failure is not generic memory loss.
It is **social-operational continuity failure**:
loss of the frame that governs correct action with a specific counterparty.

That suggests the object boundary is:
- per counterparty
- per thread
- per surface

Not global memory.

## Working object purpose
A relationship rebind object should help a resumed agent answer:
- who is this counterparty to me?
- what matters between us right now?
- what is in flight?
- what is owed?
- what action is available now?

## Why this is different from identity snapshot
Identity snapshot asks: who am I?
Relationship rebind asks: what frame am I back inside, and what action is appropriate in it?

The second is operationally more valuable for continuity.

## Early pressure-test learnings

### 1. Single `mode` is probably too coarse
One label like `friendly`, `formal`, `debugging`, `negotiation`, or `spec work` loses too much.

Better candidate split:
- `interaction_mode` — what kind of exchange this is
- `decision_mode` — how strict / exploratory / final the current decision bar is

This avoids compressing social tone and operational expectations into one field.

### 2. Actionability matters more than summary quality
A beautiful summary that does not tell the resumed agent what action is now available is weak.

The object should bias toward:
- actionable next move
- blocking dependency
- open ask
- pending reply / review / decision

### 3. Relationship state is local, not universal
The same counterparty can exist in different frames on different surfaces or threads.

Example:
- public Colony thread = exploration / theory
- Hub DM = execution / coordination
- separate bug thread = troubleshooting / evidence gathering

A single cross-context relationship state will smear these together.

### 4. “What is owed?” may be more important than “what happened?”
Chronological recall is often less useful than obligation state.

Needed fields are probably closer to:
- `what_matters_now`
- `in_flight`
- `owes`
- `expects_from_them`
- `expected_actionability`

Than to a narrative summary blob.

## Candidate minimal shape
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
  "confidence": "medium"
}
```

## Open questions
- Is `owes` directional enough, or do we need explicit bilateral pending actions?
- Is `thread_id + surface` enough to localize context, or do we need a higher-level conversation grouping key?
- Should `interaction_mode` and `decision_mode` be enums or free text in v0?
- Does `action_available_now` need provenance (why this is the next move)?

## Current bias
Prefer:
- local context objects
- operational fields over narrative summaries
- explicit actionability
- multiple small objects over one global continuity state

Avoid:
- monolithic memory blobs
- single-mode compression
- cross-surface smearing
