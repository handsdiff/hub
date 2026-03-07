# Notification Transition Matrix v0.1

Purpose: decide when the PR watcher should emit a human-visible notification after comparing the previous canonical PR packet with the newly computed packet.

Canonical packet reference: `docs/pr-review-state-v0.md`
Reducer diff contract: `docs/pr-blocker-reducer-v0.1.md`
Golden scenarios: `docs/profiles/dacl-review-v1.golden-scenarios.md`

## Inputs to the notifier

Each evaluation reduces to this tuple:

```text
(old_state, new_state, blocker_set_changed, action_line_changed, confidence_drop_bucket, mergeable_flip)
```

Where:
- `old_state`, `new_state` ∈ `mergeable | blocked | stale | needs-human`
- `blocker_set_changed` ∈ `true | false`
- `action_line_changed` ∈ `true | false`
- `confidence_drop_bucket` ∈ `none | small | medium | large`
- `mergeable_flip` ∈ `true | false`

`mergeable_flip = true` iff `old_state == mergeable` XOR `new_state == mergeable`.
Direction still matters, but it is derived from `(old_state, new_state)` rather than encoded separately.

## Required normalizations before matrix evaluation

### `blocker_set_changed`
Use reducer output, not raw artifact churn.

Count as changed when blocker identity or meaning changed materially:
- blocker id added or removed
- blocker type changed
- blocker owner changed
- resolution condition changed materially
- confidence changed enough to alter blocker meaning
- blocker applicability to current head changed materially

Do **not** count as changed when only cosmetic metadata moved:
- ages or timestamps only
- excerpt trimming only
- CI run id changed for the same required check name
- same blocker restated in another bot comment

### `action_line_changed`
`true` only when the human should do something materially different.

Examples:
- `wait for required checks to finish` -> `make dashboard-verify pass`
- `reconfirm reviewer intent on current head` -> `merge current head now`

Do **not** mark true for punctuation-only or wording-only rewrites.

### `confidence_drop_bucket`
Map raw score changes into four buckets before matrix evaluation:
- `none` = no drop, or score increased
- `small` = drop stayed within the same non-alert band
- `medium` = crossed a meaningful confidence threshold
- `large` = sharp fall into low-confidence / high-risk territory

Default band suggestion:
- `high >= 85`
- `medium 60..84`
- `low < 60`

## Evaluation rule

Evaluate rows in priority order.
The **first matching row wins**.

## Transition matrix

| Priority | old_state | new_state | blocker_set_changed | action_line_changed | confidence_drop_bucket | mergeable_flip | Emit? | Reason | Priority Class |
|---|---|---|---:|---:|---|---:|---|---|---|
| 1 | not `mergeable` | `mergeable` | * | * | * | `true` | Yes | `mergeable-flip` | high |
| 2 | `mergeable` | not `mergeable` | * | * | * | `true` | Yes | `mergeable-flip` | high |
| 3 | any | any different from old | * | * | * | `false` | Yes | `state-change` | normal |
| 4 | same as old | same as old | `true` | * | * | `false` | Yes | `blocker-set-change` | normal |
| 5 | same as old | same as old | `false` | `true` | * | `false` | Yes | `action-line-change` | normal |
| 6 | same as old | same as old | `false` | `false` | `medium` | `false` | Yes | `confidence-drop` | normal |
| 7 | same as old | same as old | `false` | `false` | `large` | `false` | Yes | `confidence-drop` | high |
| 8 | `stale` | `stale` | `false` | `false` | `none` | `false` | No | `no-op` | low |
| 9 | `stale` | `stale` | `false` | `false` | `small` | `false` | No | `no-op` | low |
| 10 | `blocked` | `blocked` | `false` | `false` | `none` | `false` | No | `no-op` | low |
| 11 | `blocked` | `blocked` | `false` | `false` | `small` | `false` | No | `no-op` | low |
| 12 | `needs-human` | `needs-human` | `false` | `false` | `none` | `false` | No | `no-op` | low |
| 13 | `needs-human` | `needs-human` | `false` | `false` | `small` | `false` | No | `no-op` | low |
| 14 | any | any | `false` | `false` | `none` | `false` | No | `no-op` | low |

## Explicit no-emit rows

These are the rows that must stay silent even if timestamps or ages changed:

1. **Age-only reevaluation**
   - same state
   - same blockers
   - same action line
   - same confidence bucket
   - only packet timestamps advanced

2. **Pending-age increments**
   - state remains `stale`
   - required check is still `pending`
   - action line still says “wait for required checks to finish”
   - only `age_minutes` increased

3. **Same ambiguity, no new evidence**
   - state remains `needs-human`
   - same `ambiguous-state` blocker ids
   - same action line
   - confidence drop only `small`

## Canonical no-emit examples

### 1) Pending check age increments
Old:
```json
{
  "decision": {"state": "stale", "confidence_pct": 96},
  "blockers": [],
  "action_line": "To merge: wait for required checks to finish on head 333cccc."
}
```

New:
```json
{
  "decision": {"state": "stale", "confidence_pct": 96},
  "blockers": [],
  "action_line": "To merge: wait for required checks to finish on head 333cccc."
}
```

Difference:
- only `required_checks[].age_minutes` changed

Result:
```text
No emit
```

### 2) Revalidated blocked state with same failing check
Old blocker ids:
```json
["required-check-failed:check:dashboard-verify"]
```

New blocker ids:
```json
["required-check-failed:check:dashboard-verify"]
```

Difference:
- same blocker id
- same action line
- same state
- no confidence threshold crossing

Result:
```text
No emit
```

### 3) Same ambiguity, no new evidence
Old:
```json
{
  "decision": {"state": "needs-human", "confidence_pct": 58},
  "blockers": [{"id": "ambiguous-state:thread:901"}],
  "action_line": "Do not merge because prior approval was for superseded SHA; reconfirm reviewer intent on current head 555eeee."
}
```

New:
```json
{
  "decision": {"state": "needs-human", "confidence_pct": 55},
  "blockers": [{"id": "ambiguous-state:thread:901"}],
  "action_line": "Do not merge because prior approval was for superseded SHA; reconfirm reviewer intent on current head 555eeee."
}
```

Difference:
- `confidence_drop_bucket = small`

Result:
```text
No emit
```

## Canonical emit examples

### 1) Became mergeable
Old state `blocked` -> new state `mergeable`

Result:
```text
Emit (`mergeable-flip`)
```

### 2) Same state, different blocker set
Old blockers:
```json
[]
```

New blockers:
```json
["required-check-failed:check:dashboard-verify"]
```

Result:
```text
Emit (`blocker-set-change`)
```

### 3) Same blockers, different action line
Old action line:
```text
To merge: wait for required checks to finish on head 333cccc.
```

New action line:
```text
To merge: ask alexjaniak to reaffirm resolution on current head 333cccc.
```

Result:
```text
Emit (`action-line-change`)
```

### 4) Confidence threshold crossed
Old confidence band: `high`
New confidence band: `medium`
State, blockers, and action line unchanged.

Result:
```text
Emit (`confidence-drop`)
```

## Recommended dedupe key

After deciding to emit, store a dedupe signature so repeated deliveries do not fan out:

```text
{repo}#{pr_number}:{new_state}:{blocker_set_hash}:{action_line_hash}:{mergeable_flip}:{confidence_drop_bucket}
```

If the next evaluation produces the same signature, suppress duplicate delivery.

## Summary rule

If the human would learn nothing new, do not ping.
If the human can act differently because of the new packet, emit.
