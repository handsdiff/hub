# DACL Review v1 — Golden Scenarios

Profile under test: `docs/profiles/dacl-review-v1.profile.json`
Reducer reference: `docs/pr-blocker-reducer-v0.1.md`
Canonical packet reference: `docs/pr-review-state-v0.md`
Notifier reference: `docs/notification-transition-matrix-v0.1.md`

## Assumptions

- Repo: `alexjaniak/DACL`
- Branch: `main`
- Required checks:
  - `dashboard-verify`
  - `solana-bootstrap-sdk`
- Current profile: `alexjaniak/DACL/review-v1`
- Conservative semantics:
  - approval must apply to current head
  - semantic blockers degrade to `needs-human` on head change unless reaffirmed or positively resolved on current head
  - pending required checks produce `stale`, not `blocked`

---

## Scenario 1 — Clean mergeable

### Current raw input snapshot (minimal)
```json
{
  "profile_id": "alexjaniak/DACL/review-v1",
  "profile_version": "1",
  "pr": {"head_sha": "111aaaa", "base_branch": "main"},
  "required_checks": [
    {"name": "dashboard-verify", "status": "success"},
    {"name": "solana-bootstrap-sdk", "status": "success"}
  ],
  "latest_valid_approval": {"author": "alexjaniak", "commit_id": "111aaaa"},
  "required_reviewers_outstanding": [],
  "blocking_artifacts": []
}
```

### Expected `blockers[]`
```json
[]
```

### Expected `state`
```text
mergeable
```

### Expected `action_line`
```text
To merge: merge current head 111aaaa now.
```

### Notifier emits?
**Yes.** Reason: state flipped into `mergeable` and the mergeability bit changed.

---

## Scenario 2 — Failing required check

### Current raw input snapshot (minimal)
```json
{
  "profile_id": "alexjaniak/DACL/review-v1",
  "profile_version": "1",
  "pr": {"head_sha": "222bbbb", "base_branch": "main"},
  "required_checks": [
    {"name": "dashboard-verify", "status": "failed", "url": "https://github.com/alexjaniak/DACL/actions/runs/1001"},
    {"name": "solana-bootstrap-sdk", "status": "success"}
  ],
  "latest_valid_approval": {"author": "alexjaniak", "commit_id": "222bbbb"},
  "required_reviewers_outstanding": [],
  "blocking_artifacts": []
}
```

### Expected `blockers[]`
```json
[
  {
    "id": "required-check-failed:check:dashboard-verify",
    "type": "required-check-failed",
    "summary": "Required check dashboard-verify failed.",
    "owner": "ci",
    "resolutionCondition": "Make dashboard-verify pass on head 222bbbb.",
    "evidenceIds": ["check:dashboard-verify"]
  }
]
```

### Expected `state`
```text
blocked
```

### Expected `action_line`
```text
To merge: make dashboard-verify pass on head 222bbbb.
```

### Notifier emits?
**Yes.** Reason: state changed away from mergeable and blocker set changed materially.

---

## Scenario 3 — Pending required check (no spam)

### Previous canonical packet (minimal)
```json
{
  "decision": {"state": "stale", "confidence_pct": 96},
  "version_anchors": {"head_sha": "333cccc"},
  "blockers": [],
  "action_line": "To merge: wait for required checks to finish on head 333cccc."
}
```

### Current raw input snapshot (minimal)
```json
{
  "profile_id": "alexjaniak/DACL/review-v1",
  "profile_version": "1",
  "pr": {"head_sha": "333cccc", "base_branch": "main"},
  "required_checks": [
    {"name": "dashboard-verify", "status": "success"},
    {"name": "solana-bootstrap-sdk", "status": "pending", "age_minutes": 14}
  ],
  "latest_valid_approval": {"author": "alexjaniak", "commit_id": "333cccc"},
  "required_reviewers_outstanding": [],
  "blocking_artifacts": []
}
```

### Expected `blockers[]`
```json
[]
```

### Expected `state`
```text
stale
```

### Expected `action_line`
```text
To merge: wait for required checks to finish on head 333cccc.
```

### Notifier emits?
**No.** Reason: state unchanged, blocker set unchanged, action line unchanged. Only pending age moved.

---

## Scenario 4 — Stale approval after force-push (`ambiguous-state`)

### Current raw input snapshot (minimal)
```json
{
  "profile_id": "alexjaniak/DACL/review-v1",
  "profile_version": "1",
  "pr": {"head_sha": "555eeee", "base_branch": "main"},
  "required_checks": [
    {"name": "dashboard-verify", "status": "success"},
    {"name": "solana-bootstrap-sdk", "status": "success"}
  ],
  "latest_valid_approval": {"author": "alexjaniak", "commit_id": "444dddd"},
  "derived_signals": {
    "head_sha_changed_after_last_approval": true,
    "approval_applies_to_head_sha": false,
    "reaffirmed_on_current_head": false,
    "resolved_semantically_on_current_head": null
  },
  "blocking_artifacts": [
    {
      "id": "thread-901",
      "source_kind": "review_thread",
      "author": "alexjaniak",
      "semantic_blocking": true,
      "intent_confidence_pct": 92,
      "applies_to_head_sha": "444dddd",
      "resolved_in_ui": true,
      "resolved_semantically": null,
      "evidence_excerpt": "must fix: auth guard missing on admin route"
    }
  ]
}
```

### Expected `blockers[]`
```json
[
  {
    "id": "ambiguous-state:thread:901",
    "type": "ambiguous-state",
    "summary": "Prior semantic blocker was approved on a superseded SHA and is not reaffirmed on the current head.",
    "owner": "alexjaniak",
    "resolutionCondition": "Reconfirm reviewer intent on current head 555eeee or produce positive semantic resolution on the current head.",
    "confidencePct": 92,
    "evidenceIds": ["thread-901"]
  }
]
```

### Expected `state`
```text
needs-human
```

### Expected `action_line`
```text
Do not merge because prior approval was for superseded SHA; reconfirm reviewer intent on current head 555eeee.
```

### Notifier emits?
**Yes.** Reason: state changed into `needs-human` and blocker set changed materially.

---

## Scenario 5 — Blocking comment resolved on current head

### Current raw input snapshot (minimal)
```json
{
  "profile_id": "alexjaniak/DACL/review-v1",
  "profile_version": "1",
  "pr": {"head_sha": "666ffff", "base_branch": "main"},
  "required_checks": [
    {"name": "dashboard-verify", "status": "success"},
    {"name": "solana-bootstrap-sdk", "status": "success"}
  ],
  "latest_valid_approval": {"author": "alexjaniak", "commit_id": "666ffff"},
  "derived_signals": {
    "head_sha_changed_after_last_approval": false,
    "approval_applies_to_head_sha": true,
    "reaffirmed_on_current_head": true,
    "resolved_semantically_on_current_head": true
  },
  "blocking_artifacts": [
    {
      "id": "thread-901",
      "source_kind": "review_thread",
      "author": "alexjaniak",
      "semantic_blocking": true,
      "intent_confidence_pct": 94,
      "applies_to_head_sha": "666ffff",
      "resolved_in_ui": true,
      "resolved_semantically": true,
      "evidence_excerpt": "resolved on current head"
    }
  ]
}
```

### Expected `blockers[]`
```json
[]
```

### Expected `state`
```text
mergeable
```

### Expected `action_line`
```text
To merge: merge current head 666ffff now.
```

### Notifier emits?
**Yes.** Reason: blocker cleared and state moved from `needs-human` to `mergeable`.

---

## Scenario 6 — Profile/version change → `policy_changed` invalidation

### Current raw input snapshot (minimal)
```json
{
  "profile_id": "alexjaniak/DACL/review-v2",
  "profile_version": "2",
  "delta_types": ["policy_changed"],
  "pr": {"head_sha": "7779999", "base_branch": "main"},
  "required_checks": [
    {"name": "dashboard-verify", "status": "success"},
    {"name": "solana-bootstrap-sdk", "status": "success"}
  ],
  "latest_valid_approval": {"author": "alexjaniak", "commit_id": "7779999"},
  "required_reviewers_outstanding": [],
  "blocking_artifacts": []
}
```

### Expected `blockers[]`
```json
[]
```

### Expected `state`
```text
stale
```

### Expected `action_line`
```text
Do not merge because decision is stale after policy_changed; reevaluate current head 7779999 under profile alexjaniak/DACL/review-v2.
```

### Notifier emits?
**Yes.** Reason: state changed due to policy/profile invalidation.

---

## Coverage summary

These six scenarios are the minimum acceptance set for DACL reducer + notifier wiring:
1. clean mergeable
2. failing required check
3. pending required check with explicit no-spam behavior
4. stale approval after force-push (`ambiguous-state`)
5. blocking comment resolved on current head
6. profile/version change invalidation (`policy_changed`)
