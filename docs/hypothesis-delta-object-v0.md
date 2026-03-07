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
  "supersedes": null,
  "superseded_by": null,
  "supersession_action_disposition": [
    {
      "action_id": "flag_species_counts_table_for_revalidation",
      "disposition": "carried_forward",
      "target_delta_id": "delta_raz2025_hofstenia_h_002",
      "reason": "newer species count still requires downstream table revalidation"
    }
  ],
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

8. `supersedes`
   - Nullable `delta_id` reference to the immediately previous delta for the same assumption chain.
   - Lets consumers traverse backward without scanning the whole corpus.

9. `superseded_by`
   - Nullable `delta_id` reference to the newer delta that replaced this one.
   - Prevents contradictory deltas from accumulating as if all are still active.
   - Does **not** by itself prove closure; use together with `verified_at` / per-action completion.

10. `supersession_action_disposition`
   - Required when pending `required_actions` still exist at supersession time.
   - Prevents false safety on old deltas by forcing each open action into one explicit state.
   - Minimum useful dispositions:
     - `carried_forward`
     - `invalidated`
   - `completed` does **not** belong here if per-action `status` is already terminal before supersession is accepted.

11. `validation_risk`
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

Next field added after closure feedback:
- `superseded_by` (nullable `delta_id`)
- paired with `supersedes` so the chain has both backward and forward links
- reason: new evidence can replace an older delta, and downstream consumers need to know which delta is still authoritative
- important nuance: supersession must not masquerade as closure; unresolved required actions still need explicit completion state
- latest simplification: action disposition enum shrinks to two values (`carried_forward | invalidated`) because `completed` already belongs on the action record itself

Break-test added after supersession feedback:
- reject circular supersession at write time
  - `delta_A -> delta_B -> delta_A` is invalid
  - `delta_A -> delta_A` is invalid
- if a delta is superseded while pending actions still exist, each action needs explicit disposition:
  - `carried_forward`
  - `invalidated`
- supersession should be accepted only after action records are already in a terminal state where appropriate
- silent abandonment is not allowed

## Structural invariants

1. `supersedes` / `superseded_by` must form a DAG over deltas for the same assumption chain.
2. Self-supersession is invalid.
3. Circular supersession is invalid.
4. A superseded delta with pending actions is invalid unless each pending action has explicit disposition.
5. `completed` must live on the action record, not in the supersession disposition enum.
6. `superseded_by` does not imply `verified_at`.
7. Supersession is valid only inside the same assumption chain; cross-chain replacement is invalid at write time.

## Validation feedback incorporated (Mar 7, next validator)

Prometheus' next validator choice was **same-assumption-chain only**.

Why this comes before timestamp hygiene:
- timestamp monotonicity is mostly infrastructure hygiene
- cross-chain supersession is a schema error that corrupts the meaning of the graph itself
- if a species-count delta can supersede a morphology delta, the supersession graph stops being interpretable

## Validation feedback incorporated (Mar 7, chain key)

Prometheus' minimal safe chain key was **`assumption_id` only**.

Why not `assumption_id + project_scope`:
- it fragments one truth into per-project copies
- the same load-bearing assumption can appear in multiple projects simultaneously
- two projects could then hold contradictory "current" values for the same assumption and both pass validation
- that recreates the cross-project residual-tracking failure the object is supposed to solve

Current design choice:
- supersession chain key = `assumption_id`
- project references still live in `primary_project` and `cross_project_implications`
- project scope influences blast radius, not truth identity

## Validation feedback incorporated (Mar 7, ID creation rule)

Prometheus' next safeguard was **lookup-before-create**.

Rule:
- before accepting a brand-new `assumption_id`, the writer must query the assumption registry for plausible existing IDs
- if a likely match exists, the writer must either reuse that ID or explicitly declare a fork / alias decision
- silent parallel IDs for the same assumption are invalid

Why this matters:
- chain key = `assumption_id` only works only if duplicate assumption IDs are actively resisted
- otherwise the system recreates the same fragmentation problem under different IDs instead of under `project_scope`

Current design choice:
- `assumption_id` remains the sole chain key
- creation of new `assumption_id` values should be gated by registry lookup before create
- alias/fork handling is now the main naming-discipline problem

## Open question for customer validation

What exact lookup key would you trust at write time for that pre-create registry check: `source_evidence.ref + normalized assumption text`, `source_evidence.ref + parameter path`, or something else? Pick one.
