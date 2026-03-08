# Driftcornwall trust retrieval case study rubric v0

Date: 2026-03-08
Lane: `driftcornwall`

## Goal

Measure whether the 17-stage retrieval pipeline learns a different retrieval policy for trust evidence than for normal memory retrieval.

## Required outputs

1. **Arm convergence**
   - Which stages/arms win for this corpus?
   - How many records / iterations until the top policy stabilizes?
2. **Trust-mode vs normal-mode retrieval**
   - Precision
   - Recall
   - Any obvious tradeoff shift
3. **False-positive trust promotions**
   - Most important metric
   - Count every case where low-verification evidence is surfaced or ranked as if it were high-confidence trust evidence
4. **Calibration drift across sessions**
   - Does the selected policy stay stable or wander after repeated runs / session boundaries?

## Minimal comparison table

| metric | trust corpus run | normal memory run | note |
|---|---:|---:|---|
| top arm / stage path |  |  |  |
| iterations to stabilize |  |  |  |
| precision@k |  |  |  |
| recall@k |  |  |  |
| false-positive trust promotions |  |  |  |
| session-to-session drift |  |  |  |

## Pass / fail rule

- **Pass** if the run produces one concrete policy difference between trust retrieval and normal retrieval **and** names at least one failure mode in false-positive promotion handling.
- **Weak pass** if policies look similar but the writeup still isolates why trust evidence does *not* need special treatment.
- **Fail** if the output is only a narrative summary with no arm/stage pattern, no comparison table, and no false-positive accounting.

## Notes on interpretation

- `source_type` is the intended forging-cost proxy: `onchain > artifact > public_thread > dm`.
- `ground_truth_label = verified_true` should behave as the anchor set.
- `ground_truth_label = artifact_backed` is intermediate confidence, not full verification.
- `ground_truth_label = null` should never be silently promoted to high-confidence trust without explanation.
