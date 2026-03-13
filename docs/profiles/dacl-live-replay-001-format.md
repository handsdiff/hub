# DACL live-replay-001 format

Purpose: capture one real DACL PR event stream around the highest-risk reducer sequence:
- force-push / head invalidation
- review-thread churn
- partial CI rerun
- possible out-of-order bot/comment arrival

This is the first production-validation replay.

## Recommended target window

Capture one active DACL PR across a force-push window with **10–20 polls**.

Ideal sequence:
1. PR starts green with at least one semantic concern thread in history.
2. A reviewer has left a `must fix` / equivalent concern.
3. Author force-pushes to a new head SHA.
4. One thread gets UI-resolved, another gets a new comment, no explicit re-approval yet.
5. One required check reruns first (`pending -> success`), second lags and completes later.
6. Optional: a bot or comment arrives out of order relative to checks.

## Files

Store replay `001` as a small directory bundle:

```text
hub/docs/profiles/live-replay-001/
  input.jsonl
  expected.jsonl
  README.md
```

Capture helper:

```text
python3 hub/scripts/dacl_live_replay_capture.py --once
```

Current behavior:
- appends one best-effort public-GitHub reducer input to `input.jsonl`
- exits `2` with `NO_OPEN_PRS` when no DACL PR is currently open

## `input.jsonl`

One line per poll / captured evaluation input.
Each line is the raw reducer input plus minimal capture metadata.

### Shape

```json
{
  "seq": 1,
  "ts": "2026-03-07T07:20:00Z",
  "repo": "alexjaniak/DACL",
  "pr": 123,
  "capture_label": "live-replay-001",
  "notes": "before force-push; both checks green",
  "raw": {
    "repo": "alexjaniak/DACL",
    "prNumber": 123,
    "prUrl": "https://github.com/alexjaniak/DACL/pull/123",
    "headSha": "abcdef1",
    "baseBranch": "main",
    "profileId": "alexjaniak/DACL/review-v1",
    "profileVersion": "1",
    "requiredChecks": [
      {"name": "dashboard-verify", "status": "success"},
      {"name": "solana-bootstrap-sdk", "status": "success"}
    ],
    "latestValidApproval": {"author": "alexjaniak", "commitId": "abcdef1"},
    "requiredReviewersOutstanding": [],
    "reviewEvents": [],
    "blockingArtifacts": [],
    "policyFailures": []
  }
}
```

Notes:
- `seq` must be strictly increasing.
- `ts` must be the capture time, not inferred later.
- `raw` should be exactly what the reducer sees, not a hand-edited summary.

## `expected.jsonl`

One line per corresponding `input.jsonl` row.
This is the expected reducer + notifier output for that input.

### Shape

```json
{
  "seq": 1,
  "expected": {
    "packet": {
      "state": "mergeable",
      "blockerIds": [],
      "actionKey": "merge.now",
      "riskPhase": "steady"
    },
    "notify": {
      "emit": false,
      "reason": "no-op",
      "priority": "low"
    }
  },
  "why": "No head change yet; no blockers; no page."
}
```

Guideline:
- keep `expected.packet` minimal and stable
- assert only the fields that matter to replay correctness:
  - `state`
  - `blockerIds`
  - `actionKey`
  - `riskPhase`
- keep notifier expectation explicit:
  - `emit`
  - `reason`
  - `priority`

## `README.md`

Short human summary for the replay:

```md
# live-replay-001

PR: alexjaniak/DACL#123
Window: 2026-03-07T07:20:00Z -> 2026-03-07T07:31:00Z
Why chosen: force-push + thread churn + partial CI rerun
Key expectations:
- head invalidation handled correctly
- UI-resolved thread does not falsely clear semantic blocker
- pending-age churn does not spam
- stable blocker IDs across out-of-order artifacts
```

## Promotion checklist

A capture becomes `live-replay-001` only if all are true:
1. the reducer input is real, not reconstructed from memory
2. the window includes at least one head change
3. the window includes at least one async check transition
4. expected outputs were reviewed once by a human
5. replay passes after being committed to the fixture harness

## What this replay should prove

If `live-replay-001` is healthy, the system should demonstrate all of these on real traffic:
- immediate invalidation after head change (`needs-human` directly or `stale` then reevaluated)
- no false clear from UI-resolved-but-semantically-uncertain thread
- stable blocker IDs across out-of-order artifact arrival
- no notification spam from pending-age churn
- notification on real risk escalation only

## Failure handling

If replay output disagrees with human judgment:
1. save the disagreement in `expected.jsonl`
2. classify as `missed-page` or `noisy-page`
3. add the failing replay before changing reducer/profile/notifier behavior

## Rule

`live-replay-001` is not optional polish.
It is the bridge between synthetic correctness and production correctness.
