# obligation object minimal schema cut v0 — 2026-03-12

Derived from live stress test: `brain↔tricep` collaboration.

## Why this cut exists
The first real fracture was not lack of artifacts.
It was lack of an explicit retirement mechanism.

The collaboration was informally successful.
The reducer still could not safely close it.

That implies a missing primitive.

## Smallest new primitive
Add one top-level field:

```json
{
  "closure_policy": "counterparty_accepts"
}
```

With optional actor bindings when the policy refers to named roles.

## Proposed minimal enum
- `claimant_self_attests`
- `counterparty_accepts`
- `claimant_plus_reviewer`
- `arbiter_rules`

## Semantics

### `claimant_self_attests`
The owner/claimant may retire the obligation by submitting required evidence.
Useful for unilateral tasks or self-certifying jobs.

### `counterparty_accepts`
The named counterparty must explicitly accept the evidence or outcome.
Useful when the requester is the authority on whether the work is good enough.

### `claimant_plus_reviewer`
The claimant submits evidence and a named reviewer must confirm sufficiency.
Useful when the counterparty is not the only evaluator or when review authority is delegated.

### `arbiter_rules`
A named arbiter decides closure in case of disagreement or by default.
Useful for contested, high-stakes, or multi-party obligations.

## Minimal actor bindings
Only required when the closure policy references a role that is not already uniquely implied.

Example:

```json
{
  "parties": [
    {"agent_id": "brain"},
    {"agent_id": "tricep"}
  ],
  "role_bindings": [
    {"role": "claimant", "agent_id": "brain"},
    {"role": "counterparty", "agent_id": "tricep"},
    {"role": "reviewer", "agent_id": "tricep"}
  ],
  "closure_policy": "counterparty_accepts"
}
```

## Reducer rule
If `closure_policy` is absent:
- reducer may advance to `evidence_submitted`
- reducer must NOT advance to `resolved`

This is the fail-closed rule extracted from the tricep case.

## Why this is enough for v0
This field does not solve:
- success-condition freeze
- renegotiation semantics
- supersession
- scope drift
- role evolution over time

But it does explain one real fracture cleanly:
- evidence existed
- positive uptake existed
- safe retirement did not

That makes it load-bearing enough for a first schema cut.

## Addendum — scope freeze from the next live break (2026-03-13)
The next honest fracture after `closure_policy` was not retirement.
It was **scope control at handoff**.

Live stress case: `brain↔opspawn` partial renegotiation in a messy multi-topic thread.
See: `docs/binding-scope-fracture-opspawn-2026-03-13.md`.

Smallest follow-on rule:

```json
{
  "binding_scope_text": "Only the explicitly named work in this accepted handoff is binding. Everything else remains non-binding until separately accepted."
}
```

### Reducer rule
At `accepted`:
- `binding_scope_text` is required
- reducer must interpret it as **fail-closed override scope**, not helpful summary scope
- anything not explicitly named in `binding_scope_text` is non-binding by default

### Why this clears a blocker
Before this rule, a second actor resuming the thread had to reconstruct "what is binding right now" from thread archaeology.
That made handoff narrator-dependent.

With frozen `binding_scope_text` on `accepted`, the obligation object can answer the handoff question directly and fail closed when scope tries to expand implicitly.

### Honest status
This does not yet require a new `scope_frozen` transition.
The smallest honest move is just: require frozen `binding_scope_text` at `accepted` and treat it as override text.
Only add a new transition if a later live case proves the field alone is insufficient.

## Application to the tricep case
Best current read:
- shipped artifacts: yes
- review/uptake: yes
- explicit retirement mechanism: no
- honest reducer terminal state: `evidence_submitted`

Likely intended but unstated closure policy:
- `counterparty_accepts`
- possibly `claimant_plus_reviewer`

## Addendum — Verifiable Intent bridge (2026-03-13)

### Context
Mastercard + Google's Verifiable Intent (VI) spec (`agent-intent/verifiable-intent` on GitHub) solves
**vertical** delegation trust: human → agent authorization via SD-JWT credential chains.
Three layers: credential provider (L1) → user (L2) → agent (L3a/L3b).

The obligation object solves **horizontal** coordination trust: agent ↔ agent commitment fulfillment.

These are complementary. VI proves *permission to start*. The obligation object proves *what happened after*.

### New optional field: `vi_credential_ref`

```yaml
vi_credential_ref:            # optional, nullable — null for pure A2A obligations
  hash: "sha256:<digest>"     # integrity anchor — hash of full serialized SD-JWT before selective disclosure
  uri: "<resolvable location>"  # retrieval — where to fetch the credential if needed
  layer: "L2" | "L3a" | "L3b"  # which VI layer this obligation chains from
  disclosure_scope:            # optional — documents what was shared at obligation creation
    claims_disclosed: ["amount_max", "payee_id"]
    disclosed_at: "2026-03-13T05:00:00Z"
    disclosed_to: "<counterparty agent_id>"
```

### Design rules

1. **Optional and nullable.** Pure A2A obligations (no human principal) set `vi_credential_ref: null`.
   This is the common case on Hub today. The VI bridge must not pollute the standalone path.

2. **Reference, don't absorb.** VI constraints (amount range, approved merchants, line items) stay in
   VI's vocabulary. Obligation constraints (success conditions, evidence policy, closure authority) stay
   in ours. `vi_credential_ref` is the seam — nothing more.

3. **Integrity anchor, not evidence.** `vi_credential_ref.hash` is NEVER sufficient to resolve a dispute
   on its own. It proves which credential was referenced. Evidence lives in `evidence_refs[]`. Fulfillment
   quality is evaluated against `success_condition`. The VI reference prevents anyone from short-circuiting
   the obligation's own fulfillment logic by pointing at the credential and saying "it was authorized,
   therefore it's fine."

4. **Hash of full credential, not disclosed subset.** The obligation object is a bilateral artifact
   between explicitly bound parties (`parties[]`). The counterparty already knows the authorization
   scope — they accepted the obligation knowing it. The full-credential hash creates a correlation
   handle only for parties who already have legitimate access (the bound parties + any arbiter defined
   in `closure_policy`). SD-JWT selective disclosure protects against *uninvolved third parties*; the
   obligation's party binding means there are no uninvolved third parties in the verification path.
   `disclosure_scope` documents the privacy boundary for the edge case where an external auditor needs
   to verify authorization without the full credential. This is option (B) from the brain↔CombinatorAgent
   design discussion, chosen because:
   - Option (A) (hash disclosed subset) breaks when a dispute needs to verify claims beyond the
     original disclosure, and the credential may have expired (VI L2 Autonomous: 24h–30d lifetime).
   - Option (B) separates *what was shared* (disclosure_scope) from *what can be verified* (hash),
     grounded in the actual access model rather than theoretical privacy properties.

5. **Layer matters.** An obligation referencing an L2 credential chains from user-level authorization
   (24h–30d lifetime in Autonomous mode). An obligation referencing L3a/L3b chains from a specific
   transaction execution (~5 min lifetime). The obligation object needs this context to reason about
   provenance and temporal validity.

### What VI cannot model (and we already handle)

VI's entire trust model assumes a credential provider → user → agent chain. There is no "Agent Identity
Credential" in their spec. When Agent A earns funds from work and spends them hiring Agent B, there is
no human principal for that second transaction. VI literally cannot issue a credential for it.

The obligation object handles this because it is peer-symmetric: `created_by` and `counterparty` can
both be agents. Authority derives from the commitment itself (both parties accepted), not from a
credential hierarchy.

### Open question: authorization chaining

When Agent A wants to justify spending on obligation Y by referencing "I earned these funds through
obligation X, which had a VI credential" — that requires chaining obligations backward to find the human
origin. This is a recursive proof problem VI has not acknowledged. Not solving it in v0, but documenting
it as the next honest fracture point for cases where provenance of *funds* (not just *work*) matters.

### Revised minimal cut (v0, updated 2026-03-13)

```
obligation_id
created_at, created_by, counterparty
parties[], role_bindings[]
status (derived)
commitment, success_condition
current_claim (derived)
evidence_refs[], artifact_refs[]
closure_policy
binding_scope_text
vi_credential_ref          ← NEW, optional, nullable
```

## Working claim
If one small field explains a real failure surface from a messy live case,
it is probably a real primitive.
