# Run Intent Object v0

Date: 2026-03-08
Source lane: `prometheus-bne`

## Customer-validated decision

The next load-bearing object is **separate run-intent classification at launch**.

Why:
- assumption registry + delta lineage improve identity and history
- but they do not stop a stale or wrongly-scoped experiment from being launched
- the weakest edge is still the handoff from epistemic state -> physical/computational run
- forcing intent at launch is the fail-closed guardrail that turns assumption tracking from a library into an operational control layer

## Problem

Without an explicit launch object, a run can still be started as if it were validation even when:
- the linked assumption is stale
- the hypothesis is implicit or missing
- the success criterion is undefined
- parameter selection is exploratory but later narrated as confirmatory

That failure is expensive because the contamination enters at execution time, not just at interpretation time.

## Goal

Require every run to declare what it is trying to do *before* resources are committed, and fail closed when the declared validation context is incomplete or stale.

## v0 design rule

`validate` runs should be blocked at launch unless all of the following are present and current:
- `hypothesis_id`
- `success_criterion`
- `assumption_bindings[]`
- current-head check passes for each bound assumption, **or** an explicit stale override exists

If a stale override is used:
- the run may still launch
- but it automatically loses validation-promotion rights until a later current-head revalidation run exists

`explore` runs may launch with looser requirements, but their results must not be silently promotable to validation evidence.

## Minimum object

```json
{
  "run_id": "run_channel_hierarchy_2026_03_08_001",
  "project_id": "proj_channel_hierarchy",
  "intent_class": "validate",
  "hypothesis_id": "hyp_hofstenia_count_affects_fig1_errorbars",
  "assumption_bindings": [
    {
      "assumption_id": "species_count.hofstenia_h",
      "source_reference": "Raz 2017",
      "parameter_path": "species_count.hofstenia_h",
      "expected_head_delta_id": "delta_raz2017_hofstenia_h_001"
    }
  ],
  "success_criterion": "fig1 error bars recomputed from the current species count fall within the revised empirical interval",
  "parameter_policy": "fixed",
  "promotion_rule": {
    "may_cite_as_validation": true,
    "requires": [
      "launch_guard_passed",
      "frozen_parameters",
      "recorded_claim_link"
    ]
  },
  "launch_guard": {
    "decision": "allow",
    "checked_at": "2026-03-08T08:00:00Z",
    "stale_bindings": []
  },
  "launched_at": "2026-03-08T08:00:00Z"
}
```

## Required fields

### 1) `intent_class`
Allowed values for v0:
- `explore`
- `validate`

### 2) `hypothesis_id`
Required for `validate`.
Optional for `explore`.

### 3) `assumption_bindings[]`
Each binding ties the run to the assumption chain it depends on.

Minimum fields per binding:
- `assumption_id`
- `source_reference`
- `parameter_path`
- `expected_head_delta_id`

### 4) `success_criterion`
Required for `validate`.
This is what prevents retroactive narration from redefining success after the run finishes.

### 5) `parameter_policy`
Useful initial values:
- `fixed`
- `sweep`
- `adaptive`

### 6) `promotion_rule`
Controls whether the run result may later be cited as validation evidence.

Customer-validated modeling choice for v0:
- keep validation certification encoded here
- do **not** add a separate first-class `validation_certification_status` field yet

### 7) `launch_guard`
Records whether the system verified that the run's assumption bindings still point at the live heads at launch time.

Minimum fields:
- `decision`: `allow | block`
- `checked_at`
- `stale_bindings[]`

## Fail-closed semantics

### For `validate`
Block launch if any of the following are true:
- missing `hypothesis_id`
- missing `success_criterion`
- missing `assumption_bindings[]`
- any binding fails current-head validation **and** no explicit stale override is present

If a stale override is present:
- launch may proceed
- `promotion_rule.may_cite_as_validation` must be forced to `false`
- the loss of validation-promotion rights does **not** auto-reset on that run
- a later current-head revalidation run is required before validation-promotion rights can be restored
- that restoration belongs to the later run artifact, not by mutating the old stale run

### For `explore`
Allow launch with looser requirements, but default promotion rule should prevent later citation as validation evidence unless an explicit promotion step occurs.

## Relationship to other objects

- `AssumptionRegistryEntry` answers: what assumption chain is this?
- `HypothesisDeltaObject` answers: what changed in that chain and what downstream actions are required?
- `RunIntentObject` answers: what exactly is this run trying to test, and should it be allowed to launch under the current heads?

## Manual step this closes

Before:
- note a new paper
- maybe update the registry
- launch an experiment from memory
- discover only afterward that the run used stale assumptions or lacked a real validation target

After:
- declare run intent at launch
- bind the run to explicit assumption heads
- block stale validation runs before resources are spent

## Acceptance bar for v0

v0 is useful if it prevents one stale-parameter validation run from launching.

## Freshness semantics now selected

Prometheus selected:
- exact current-head equality by default
- explicit stale override allowed
- stale override automatically strips validation-promotion rights until a later current-head revalidation run exists

Practical interpretation:
- hard block by default preserves fail-closed behavior
- override preserves operability for comparison/debugging work
- certification of validation quality lives in promotion rights, not in the bare launch intent label
- for v0, that certification remains encoded directly in `promotion_rule`
