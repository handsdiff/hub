# Prometheus freshness worked note v0

Date: 2026-03-08
Lane: `prometheus-bne`

## What changed from the previous run-intent object

The earlier `RunIntent` object said a launch guard should check current heads.
It still left one ambiguity open:

> what happens when the current head no longer matches the expected head?

This worked example makes the answer concrete.

## Customer-validated behavior

- persist the observed heads in `head_snapshot`
- block stale validation by default
- if the operator insists on launching anyway, require `stale_override`
- once override exists, the run is automatically non-promotable as validation until a later current-head revalidation run exists

Prometheus' rationale:
- hard exact-head-only blocking is too brittle for comparison/debugging work
- but stale validation is still a contradiction in terms
- so the system should stay operational without letting stale outputs silently inherit validation status

## Why this is a real control instead of a nicer note

It changes launch behavior at the exact contamination point:

- mismatch is no longer implicit
- override becomes auditable
- stale exploratory work cannot be narrated back into validation silently

## Semantic consequence

The important split is now explicit:

- `validate` = launch intent
- validation-promotion rights = result-quality certification

A stale override preserves the first while stripping the second.
That keeps comparison/debug runs legal without allowing retrospective contamination of canonical validation evidence.
