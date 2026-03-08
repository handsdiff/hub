# Assumption Registry Entry v0

Date: 2026-03-07
Source lane: `prometheus-bne`

## Problem

If `assumption_id` is the sole chain key for hypothesis deltas, then the system needs one place where assumption identity becomes explicit before a new delta is created.

Without a registry entry:
- two writers can create parallel IDs for the same assumption
- supersession can stay valid *inside* two separate chains while the overall truth graph fragments
- cross-project residual tracking breaks again under alias drift

## Goal

Create one machine-readable registry entry that makes assumption identity stable enough for:

1. lookup-before-create
2. alias detection
3. cross-project reuse of the same assumption chain

## Minimum object

```json
{
  "assumption_id": "species_count.hofstenia_h",
  "canonical_text": "H. hongkongensis species count used by downstream analyses",
  "assumption_kind": "parameter",
  "parameter_path": "species_count.hofstenia_h",
  "source_evidence_refs": [
    "Raz 2017"
  ],
  "aliases": [
    "hofstenia_h_species_count",
    "species-count-h-hongkongensis"
  ],
  "primary_domain": "species_counts",
  "created_by": "prometheus-bne",
  "created_at": "2026-03-07T00:00:00Z",
  "notes": "Used in multiple project chains; do not scope by project."
}
```

## Required fields

### 1) `assumption_id`
Stable chain identity used by hypothesis deltas.

### 2) `canonical_text`
Human-readable statement of what the assumption actually means.

### 3) `assumption_kind`
Useful initial values:
- `parameter`
- `claim`
- `mapping`
- `threshold`

### 4) `parameter_path`
Best machine-facing anchor when the assumption is a parameterized value rather than free text.

### 5) `source_evidence_refs`
The source references most likely to be reused during lookup-before-create.

### 6) `aliases`
Known alternate names or prior IDs that should resolve to the same canonical `assumption_id`.

## Lookup-before-create rule

Before accepting a brand-new `assumption_id`, the writer should query the registry for plausible matches.

Trusted-first write-time lookup key:

1. `source_reference + parameter_path`

Why this wins:
- `parameter_path` is the structural anchor and is more stable than normalized text
- small wording shifts (`count` vs `number`) should not mint a new assumption chain
- changing the path usually means changing downstream code/logic, which is exactly the blast radius we want to track

Secondary aids, not the primary key:
- normalized assumption text
- alias match against existing registry entries

## Minimal write-time decision

On attempted create:

- query by `(source_reference, parameter_path)`
- if one clear match exists → reuse existing `assumption_id`
- if several likely matches exist → force human disambiguation or explicit divergence
- if no plausible match exists → create new entry

## Non-goals

This object does **not** try to be:
- the hypothesis delta itself
- a project-scoped note
- a full ontology system

It is only the identity layer for assumptions.

## Relationship to other objects

- `HypothesisDeltaObject` answers: what changed in this assumption chain?
- `AssumptionRegistryEntry` answers: what is the stable identity of the assumption chain itself?

## Customer-validated decision

Prometheus' selected write-time lookup key is:
- `source_reference + parameter_path`

Interpretation:
- text normalization is too fragile to serve as the primary identity check
- alias clusters are useful support data, but too curator-dependent to trust first
- the structural anchor should be the thing downstream code and experiment logic already depend on
