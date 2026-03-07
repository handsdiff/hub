# Notification Transition Matrix v0.1

Purpose: decide when the PR watcher should emit a human-visible notification after comparing the previous canonical PR packet with the newly computed packet.

Canonical packet reference: `docs/pr-review-state-v0.md`
Reducer diff contract: `docs/pr-blocker-reducer-v0.1.md`
Golden scenarios: `docs/profiles/dacl-review-v1.golden-scenarios.md`

## Inputs to the notifier

Each evaluation reduces to this tuple:

```text
(old_state, new_state, blocker_set_changed, action_key_changed, confidence_drop_bucket, mergeable_flip)
```

Where:
- `old_state`, `new_state` ∈ `mergeable | blocked | stale | needs-human`
- `blocker_set_changed` ∈ `true | false`
- `action_key_changed` ∈ `true | false`
- `confidence_drop_bucket` ∈ `none | small | medium | large`
- `mergeable_flip` ∈ `true | false`

`mergeable_flip = true` iff `old_state == mergeable` XOR `new_state == mergeable`.
Direction still matters, but it is derived from `(old_state, new_state)` rather than encoded separately.

### `action_key` vs `action_line`
- `action_key` is the machine-stable semantic instruction used for notify/dedupe decisions.
- `action_line` is the human-readable sentence shown in the packet.
- **Notify off `action_key`, not raw `action_line` prose.**

Example namespace:
- `wait.check.dashboard-verify`
- `wait.check.solana-bootstrap-sdk`
- `need.approval.alexjaniak`
- `reconfirm.intent.current-head`
- `policy.profile-changed`

If `action_key` is absent during transition rollout, temporary fallback may be:
```text
{state}:{sorted_blocker_ids.join(',')}
```
But that fallback is transitional only.

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

### `action_key_changed`
`true` only when the human should do something materially different.

Examples:
- `wait.check.solana-bootstrap-sdk` -> `fix.check.dashboard-verify`
- `reconfirm.intent.current-head` -> `merge.now`
- `need.approval.alexjaniak` -> `policy.profile-changed`

Do **not** mark true for:
- wording churn in `action_line`
- bullet ordering differences
- timestamp changes inside human-readable text

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

**Hard rule:** `medium` or `large` must still emit even when state, blocker ids, and `action_key` are unchanged. This is the false-negative guardrail for risk escalation under the same label.

## Evaluation rule

Evaluate rows in priority order.
The **first matching row wins**.

## Transition matrix

| Priority | old_state | new_state | blocker_set_changed | action_key_changed | confidence_drop_bucket | mergeable_flip | Emit? | Reason | Priority Class |
|---|---|---|---:|---:|---|---:|---|---|---|
| 1 | not `mergeable` | `mergeable` | * | * | * | `true` | Yes | `mergeable-flip` | high |
| 2 | `mergeable` | not `mergeable` | * | * | * | `true` | Yes | `mergeable-flip` | high |
| 3 | any | any different from old | * | * | * | `false` | Yes | `state-change` | normal |
| 4 | same as old | same as old | `true` | * | * | `false` | Yes | `blocker-set-change` | normal |
| 5 | same as old | same as old | `false` | `true` | * | `false` | Yes | `action-key-change` | normal |
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
   - same `action_key`
   - same confidence bucket
   - only packet timestamps advanced

2. **Pending-age increments**
   - state remains `stale`
   - required check is still `pending`
   - action key still says `wait.check.<name>`
   - only `age_minutes` increased

3. **Same ambiguity, no new evidence**
   - state remains `needs-human`
   - same `ambiguous-state` blocker ids
   - same `action_key`
   - confidence drop only `small`

4. **Action-line wording churn only**
   - state unchanged
   - blocker set unchanged
   - `action_key` unchanged
   - `action_line` wording changed only

## Canonical no-emit examples

### 1) Pending check age increments
Old:
```json
{
  "decision": {"state": "stale", "confidence_pct": 96},
  "blockers": [],
  "action_key": "wait.check.solana-bootstrap-sdk",
  "action_line": "To merge: wait for required checks to finish on head 333cccc."
}
```

New:
```json
{
  "decision": {"state": "stale", "confidence_pct": 96},
  "blockers": [],
  "action_key": "wait.check.solana-bootstrap-sdk",
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
- same `action_key`
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
  "action_key": "reconfirm.intent.current-head",
  "action_line": "Do not merge because prior approval was for superseded SHA; reconfirm reviewer intent on current head 555eeee."
}
```

New:
```json
{
  "decision": {"state": "needs-human", "confidence_pct": 55},
  "blockers": [{"id": "ambiguous-state:thread:901"}],
  "action_key": "reconfirm.intent.current-head",
  "action_line": "Do not merge because prior approval was for superseded SHA; reconfirm reviewer intent on current head 555eeee."
}
```

Difference:
- `confidence_drop_bucket = small`

Result:
```text
No emit
```

### 4) Action-line wording churn only
Old:
```json
{
  "decision": {"state": "blocked"},
  "blockers": [{"id": "pending-required-check:check:dashboard-verify"}],
  "action_key": "wait.check.dashboard-verify",
  "action_line": "Wait for dashboard-verify to finish."
}
```

New:
```json
{
  "decision": {"state": "blocked"},
  "blockers": [{"id": "pending-required-check:check:dashboard-verify"}],
  "action_key": "wait.check.dashboard-verify",
  "action_line": "Await CI completion (dashboard-verify)."
}
```

Difference:
- prose changed only

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

### 3) Same blockers, different action key
Old action key:
```text
wait.check.solana-bootstrap-sdk
```

New action key:
```text
reconfirm.intent.current-head
```

Result:
```text
Emit (`action-key-change`)
```

### 4) Confidence threshold crossed under same label
Old:
```json
{
  "decision": {"state": "needs-human", "confidence_pct": 72},
  "blockers": [{"id": "ambiguous-state:thread:901"}],
  "action_key": "reconfirm.intent.current-head"
}
```

New:
```json
{
  "decision": {"state": "needs-human", "confidence_pct": 38},
  "blockers": [{"id": "ambiguous-state:thread:901"}],
  "action_key": "reconfirm.intent.current-head"
}
```

Difference:
- new trusted contradictory evidence collapsed confidence hard
- state unchanged
- blocker ids unchanged
- `action_key` unchanged
- `confidence_drop_bucket = large`

Result:
```text
Emit (`confidence-drop`)
```

## Recommended dedupe key

After deciding to emit, store a dedupe signature so repeated deliveries do not fan out:

```text
{repo}#{pr_number}:{new_state}:{blocker_set_hash}:{action_key}:{mergeable_flip}:{confidence_drop_bucket}
```

If the next evaluation produces the same signature, suppress duplicate delivery.

## Summary rule

If the human would learn nothing new, do not ping.
If the human can act differently because of the new packet, emit.
