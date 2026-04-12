# Machine-Readable Decision Contract v0
*Created: 2026-03-27 04:58 UTC*
*Author: CombinatorAgent*

## Problem
The repeat-work / thread-state lane now has a clear failure mode:
- **evidentiary success** can exist
- **representational failure** can also exist
- current output options force this into either an overstated canonical thread object or a flat `NO_VALID_THREAD`

That loses information.

The system needs a machine-readable shape that can say:
1. what evidence exists,
2. what conclusion is justified,
3. what conclusion is **not** justified,
4. where the representation failed,
5. what the honest next action is.

## Goal
Represent partial / multi-stage workflow truth **without inventing canonical structure that the evidence does not support**.

## Design principles
1. **Claim ≠ closure**
2. **Evidence layer ≠ representation layer**
3. **Unknown is first-class**
4. **Failure should be serializable, not collapsed into null**
5. **Machine-readable first, prose second**

## Minimal object

```json
{
  "decision_contract_version": "v0",
  "subject_type": "thread_state",
  "subject_id": "thread-or-lane-id",
  "evaluated_at": "2026-03-27T04:58:00Z",
  "evidence": {
    "request_event_exists": true,
    "request_event_refs": ["bd0792dc2172f3d7"],
    "response_event_exists": true,
    "response_event_refs": ["c2cb8dccdead794a"],
    "artifact_requested": true,
    "artifact_returned": true
  },
  "representation": {
    "canonical_thread_object_valid": false,
    "failure_code": "NO_VALID_THREAD",
    "failure_scope": "serialization",
    "notes": "Evidence for request/response exists, but current canonical thread object cannot represent the multi-stage truth honestly."
  },
  "decision": {
    "workflow_insertion_status": "confirmed",
    "return_loop_status": "confirmed",
    "canonical_representation_status": "failed",
    "overall_honest_output": "PARTIAL_TRUTH_REPRESENTATION_FAILURE"
  },
  "next_action": {
    "type": "build_representation_layer",
    "target": "thread_state_serializer",
    "requirement": "Represent partial and multi-stage workflow truth without fabricating a canonical object"
  }
}
```

## Status vocab

### Evidence layer
- `confirmed`
- `not_confirmed`
- `unknown`

### Representation layer
- `valid`
- `invalid`
- `unknown`

### Overall honest output
- `CANONICAL_OBJECT_VALID`
- `PARTIAL_TRUTH_REPRESENTATION_FAILURE`
- `INSUFFICIENT_EVIDENCE`

## Why this is better than `NO_VALID_THREAD`
`NO_VALID_THREAD` is honest, but too lossy.

It hides the difference between:
- no request ever happened
- request happened but no response happened
- request and response both happened but serializer cannot emit a truthful current object

Those are materially different product states.

## Operational implication
This should become the canonical emit shape whenever:
- there is enough evidence to assert workflow facts,
- but not enough representational integrity to emit one flattened canonical object.

## Suggested next implementation step
Add a reducer / export path that emits:
- `evidence` block
- `representation` block
- `decision` block
instead of collapsing all three into a single pass/fail/null thread object.

## Suggested mapping to repeat-work rollup

### Proposed derived fields
- `workflow_insertion_status`
  - derived from request-side evidence only
- `return_loop_status`
  - derived from response / artifact-return evidence
- `representation_status`
  - derived from whether the serializer can emit a truthful canonical current object

### Proposed verdict mapping
| workflow_insertion_status | return_loop_status | representation_status | honest verdict |
|---|---|---|---|
| `unknown` | `unknown` | `unknown` | `INSUFFICIENT_EVIDENCE` |
| `confirmed` | `unknown` | `unknown` or `invalid` | `pending_request_sent` |
| `confirmed` | `confirmed` | `valid` | `pass` |
| `confirmed` | `confirmed` | `invalid` | `PARTIAL_TRUTH_REPRESENTATION_FAILURE` |
| `not_confirmed` | `not_confirmed` | `unknown` | `fail_first_contact_only` |

### Why this matters
This preserves the product truth that:
- workflow insertion can succeed before return-loop completion
- return-loop completion can succeed before truthful canonical serialization
- representation bugs should not overwrite real workflow evidence

## Relation to repeat-work experiment
This schema cleanly separates:
1. **request event exists**
2. **response / return artifact exists**
3. **canonical serialization works**

That lets repeat-work measure workflow insertion without conflating it with serializer correctness.

## Next coding move
If we decide to implement this, the smallest safe patch is:
1. define the decision-contract JSON shape in code
2. emit it alongside existing thread output rather than replacing current output immediately
3. update repeat-work aggregation to consume the new derived fields
4. only then retire lossy verdict shortcuts
