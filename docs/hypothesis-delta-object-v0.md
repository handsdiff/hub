# Hypothesis Delta Object v0

Date: 2026-03-07
Source lane: `prometheus-bne`

## Problem

The missing artifact in the literature → experiment loop is not the paper note or the experiment spec.
It is the typed object for:

> "This new finding changes assumption X in project Y."

Without that object:
- the change stays in working memory
- cross-project implications stay implicit
- later sessions cannot tell which outputs became stale
- exploratory evidence can silently drift into validation claims

## Goal

Make a single machine-readable object that captures one hypothesis delta and its downstream blast radius.

## Minimum object

```json
{
  "delta_id": "delta_raz2017_hofstenia_h_001",
  "created_at": "2026-03-07T00:00:00Z",
  "author_agent": "prometheus-bne",
  "source_evidence": {
    "kind": "paper",
    "ref": "Raz 2017",
    "locator": "..."
  },
  "affected_assumption": {
    "assumption_id": "species_count.hofstenia_h",
    "old_value": 13,
    "new_value": 19,
    "change_type": "parameter_update"
  },
  "primary_project": {
    "project_id": "channel-hierarchy-paper",
    "artifact_ref": "fig1.error-bars"
  },
  "cross_project_implications": [
    {
      "project_id": "coding-theory-paper",
      "artifact_ref": "species-counts-table",
      "effect": "may_be_stale"
    }
  ],
  "hypothesis_delta": "Raz 2017 changes assumption species_count.hofstenia_h from 13 to 19, so downstream summaries using the old value should be rechecked.",
  "required_actions": [
    {
      "action_id": "recompute_fig1_error_bars",
      "description": "Recompute fig1 error bars in channel-hierarchy-paper",
      "status": "completed",
      "completed_at": "2026-03-07T02:14:00Z",
      "completed_by": "prometheus-bne",
      "evidence_ref": "commit:abc123"
    },
    {
      "action_id": "flag_species_counts_table_for_revalidation",
      "description": "Flag species-counts-table in coding-theory-paper for revalidation",
      "status": "pending",
      "completed_at": null,
      "completed_by": null,
      "evidence_ref": null
    }
  ],
  "verified_at": "2026-03-07T02:14:00Z",
  "validation_risk": {
    "stale_validation_outputs": true,
    "run_reclassification_needed": false
  }
}
```

## Required fields

1. `source_evidence`
   - What caused the change.
   - Without this, the delta becomes an unsupported note.

2. `affected_assumption`
   - The exact assumption or parameter that changed.
   - Must include stable ID if possible, not just prose.

3. `primary_project`
   - The first project where the assumption is load-bearing.

4. `cross_project_implications`
   - Other projects/artifacts that may now be stale.
   - This is the missing bridge for residual tracking.

5. `hypothesis_delta`
   - One sentence in plain language.
   - Human-readable summary of what changed and why it matters.

6. `required_actions`
   - Concrete downstream actions, not analysis.
   - Each action should carry closure state, not just a string label.
   - Minimum useful action fields: `action_id`, `status`, `completed_at`, `completed_by`.

7. `verified_at`
   - Nullable closure timestamp for the delta as a whole.
   - Signals that required downstream action actually happened rather than being merely noted.
   - If per-action completion exists, `verified_at` should represent the latest point at which the delta was materially closed or rechecked.

8. `validation_risk`
   - Whether existing outputs may now be epistemically unsafe to cite.

## Non-goals

This object does **not** replace:
- bibliography / claim records
- experiment launch intent object
- raw experiment specs
- session summaries

It sits between evidence intake and downstream project updates.

## Relationship to launch-intent classification

`HypothesisDeltaObject` answers:
- *what changed?*
- *which assumptions/projects are affected?*

A separate `RunLaunchIntent` object should answer:
- *why was this run launched?*
- *exploration or validation?*
- *what claim is it allowed to support?*

The two objects complement each other.

## Validation feedback incorporated (Mar 7, `prometheus-bne`)

The first missing field was **closure**, not more explanation.

Why:
- `required_actions` without completion state becomes a write-only task list
- three sessions later you cannot tell whether the downstream recompute actually happened
- unresolved blast radius becomes implicit in a second way: not just *what changed*, but *did we ever close it?*

Current design choice:
- keep `verified_at` at the top level for quick filtering
- also keep per-action completion records so closure is auditable, not just declared

Lower-priority next field candidate:
- `source_evidence.confidence` for preprints / contested findings

## Open question for customer validation

If closure is solved, what is the next missing field that separates a usable hypothesis-delta object from an overgrown note?
