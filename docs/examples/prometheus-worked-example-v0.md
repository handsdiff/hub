# Prometheus worked example v0

Date: 2026-03-08
Lane: `prometheus-bne`

## Manual step this closes

A writer reads a new paper result, remembers there was already an assumption chain for the same parameter, updates one figure, then later forgets which other artifacts and runs are now suspect.

This example makes the missing step explicit in two objects:

1. `prometheus-assumption-registry-entry-example-v0.json`
   - handles lookup-before-create for `assumption_id = species_count.hofstenia_h`
   - prevents minting a second project-scoped ID for the same assumption
2. `prometheus-hypothesis-delta-example-v0.json`
   - records the new paper-driven change
   - names the blast radius across projects
   - keeps closure visible through `required_actions[]` + `verified_at`
   - leaves run-intent review explicit instead of silently folding exploratory evidence into validation

## Closure path

Before this object pair, the loop was:

paper note -> human memory -> maybe update one project -> maybe forget the rest

With the object pair, the loop becomes:

paper note -> registry lookup (reuse `assumption_id`) -> delta write -> downstream action list -> explicit closure / remaining open risk

## Why this is a real step rather than a prettier note

The object set forces three things that were previously easy to lose:

- **identity**: is this the same assumption chain or a new one?
- **blast radius**: which other project artifacts are now stale?
- **closure**: which downstream actions were actually completed versus merely noticed?

## Remaining uncertainty to validate with customer

If this still fails to close the literature -> experiment loop, the next missing piece should be exactly one of:

1. the lookup key used before creating a new `assumption_id`
2. stricter closure semantics on the delta/action state
3. a separate run-intent object that disambiguates exploration vs validation at launch
