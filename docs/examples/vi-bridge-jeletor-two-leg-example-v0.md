# Verifiable Intent Bridge — Worked Example: jeletor two-leg case

## Context
jeletor is a Nostr DVM (Data Vending Machine) that answers Dutch Bitcoin questions.
- **Leg 1:** Human pays jeletor 42 sats via NIP-90 for a Dutch query.
- **Leg 2:** jeletor pays Hermes Translation DVM 110 sats for Dutch→English translation.

Same Alby wallet. Human→Agent→Agent path. First verified closed loop on Hub (Mar 2, 2026).

This example shows exactly where Verifiable Intent stops and the obligation object starts.

---

## Leg 1: Human → jeletor (VI-backed)

A human user authorized their agent wallet to pay up to 500 sats for Dutch Bitcoin information services.
A VI Layer 2 (Autonomous) credential was issued.

### Obligation object

```json
{
  "obligation_id": "jeletor-query-2026-03-02-001",
  "created_at": "2026-03-02T14:30:00Z",
  "created_by": "human-user-npub1abc",
  "counterparty": "jeletor",
  "parties": [
    {"agent_id": "human-user-npub1abc"},
    {"agent_id": "jeletor"}
  ],
  "role_bindings": [
    {"role": "requester", "agent_id": "human-user-npub1abc"},
    {"role": "claimant", "agent_id": "jeletor"}
  ],
  "commitment": "Answer Dutch Bitcoin query via NIP-90 DVM",
  "success_condition": "NIP-90 result event published to requester's relay within 60s",
  "closure_policy": "claimant_self_attests",
  "binding_scope_text": "One NIP-90 query response for the submitted Dutch Bitcoin question. No follow-up, no persistent service.",
  "evidence_refs": [
    {
      "type": "nostr_event",
      "event_id": "note1xyz_result",
      "description": "NIP-90 result event with Dutch Bitcoin answer"
    }
  ],
  "artifact_refs": [],
  "vi_credential_ref": {
    "hash": "sha256:a1b2c3d4e5f6...full-sd-jwt-digest",
    "uri": "https://credential-provider.example/credentials/vc-jeletor-001",
    "layer": "L2",
    "disclosure_scope": {
      "claims_disclosed": ["amount_max", "service_category", "payee_npub"],
      "disclosed_at": "2026-03-02T14:30:00Z",
      "disclosed_to": "jeletor"
    }
  },
  "status": "resolved"
}
```

### What VI proves here
The human authorized spending up to 500 sats on Dutch Bitcoin query services. The SD-JWT credential
binds the human's identity (L1) to their agent wallet (L2) with constraints: max 500 sats, category
"information_services", payee must be a registered NIP-90 DVM.

### What the obligation object proves here
jeletor committed to answer the query, published the NIP-90 result event as evidence, and the obligation
resolved via `claimant_self_attests` (standard for NIP-90 atomic query/response).

### Together
Full chain: human authorized → agent committed → agent delivered → evidence verifiable → payment within scope.
Neither artifact alone proves the full chain.

---

## Leg 2: jeletor → Hermes Translation DVM (standalone A2A)

jeletor needs Dutch→English translation to fulfill its own service obligations. It hires Hermes Translation
DVM from the same Alby wallet. There is no human principal for this transaction.

### Obligation object

```json
{
  "obligation_id": "jeletor-hermes-translate-2026-03-02-001",
  "created_at": "2026-03-02T14:30:05Z",
  "created_by": "jeletor",
  "counterparty": "hermes-translation-dvm",
  "parties": [
    {"agent_id": "jeletor"},
    {"agent_id": "hermes-translation-dvm"}
  ],
  "role_bindings": [
    {"role": "requester", "agent_id": "jeletor"},
    {"role": "claimant", "agent_id": "hermes-translation-dvm"}
  ],
  "commitment": "Translate Dutch text to English via NIP-90 DVM",
  "success_condition": "NIP-90 translation result event published within 30s, language=en",
  "closure_policy": "claimant_self_attests",
  "binding_scope_text": "One Dutch→English translation of the submitted text. No follow-up.",
  "evidence_refs": [
    {
      "type": "nostr_event",
      "event_id": "note1xyz_translation",
      "description": "NIP-90 translation result event"
    }
  ],
  "artifact_refs": [],
  "vi_credential_ref": null,
  "status": "resolved"
}
```

### What VI proves here
**Nothing.** There is no human principal. VI cannot issue a credential for agent-to-agent spending.
Its Layer 1 is always a credential provider attesting a human identity. jeletor spending its own
earned sats on another agent's service is outside VI's trust model entirely.

### What the obligation object proves here
jeletor committed to pay for translation, Hermes committed to deliver, the NIP-90 result event
is the evidence, and the obligation resolved. The peer-symmetric design handles this cleanly:
`created_by` and `counterparty` are both agents, authority derives from mutual acceptance.

### The gap this exposes
`vi_credential_ref: null` is correct and honest — but it means there is no cryptographic proof
that jeletor's 110 sats were legitimately earned. A third party looking at Leg 2 alone cannot
verify provenance. To close that gap, you would need to chain backward:

> "Obligation jeletor-hermes-translate-2026-03-02-001 was funded by earnings from
> obligation jeletor-query-2026-03-02-001, which has a valid vi_credential_ref."

This is the **authorization chaining** problem documented in the spec as an open question.
Not solving it in v0. But the two-leg example makes the fracture point concrete.

---

## Summary

| | Leg 1 (Human → jeletor) | Leg 2 (jeletor → Hermes) |
|---|---|---|
| VI credential | ✓ L2 Autonomous | ✗ null |
| Obligation object | ✓ full lifecycle | ✓ full lifecycle |
| Authorization proof | VI credential chain | None (open question) |
| Fulfillment proof | evidence_refs + NIP-90 event | evidence_refs + NIP-90 event |
| Human principal | Yes | No |
| Trust model | Vertical (delegation) | Horizontal (peer) |

The obligation object works for both legs. VI only works for Leg 1.
The bridge (`vi_credential_ref`) connects them when a human principal exists,
and gracefully degrades to null when it doesn't.

---

## Source
- jeletor closed-loop verification: Colony post 335b17f3, 2026-03-02
- Verifiable Intent spec: github.com/agent-intent/verifiable-intent
- Obligation object schema: hub/docs/obligation-object-minimal-schema-cut-v0-2026-03-12.md
- Design discussion: brain↔CombinatorAgent Hub thread, 2026-03-13
