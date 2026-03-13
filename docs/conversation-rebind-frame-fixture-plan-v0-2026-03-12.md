# conversation_rebind_frame fixture plan v0 — 2026-03-12

Derived from live discussion with `testy`.

## Evaluation principle
A rebind frame is good if it restores:
- the right next move
- the right decision posture

without requiring full narrative replay.

## Compact fixture set
This is the smallest fixture set currently believed to cover the meaningful surface area.

### 1. straightforward frame use
Question:
Can the frame restore the obvious next move in a stable ongoing thread?

### 2. obligation continuity
Question:
Can the frame preserve what is owed / in flight across a pause and support correct action without replaying the whole conversation?

### 3. weak invalidation
Question:
Can the system notice that the frame is a bit stale but still locally useful?

### 4. strong invalidation
Question:
Can the system notice that the frame is unsafe to act from and avoid confident wrong action?

### 5. silence
Question:
Can the frame correctly represent a waiting state where the right action is no action yet?

### 6. reroute
Question:
Can the system detect that the interaction frame moved to a different thread or surface and stop using the old local frame as if it were still current?

### 7. ignore
Question:
Can the system leave a valid frame unloaded because it is irrelevant to the current task?

### 8. cross-surface divergence
Question:
Can the system preserve separate local frames for the same counterparty across different surfaces without smearing them together?

## What each fixture should test
Each fixture should be evaluable on four axes:
- recovered next move
- recovered decision posture
- correct trust / invalidate / ignore behavior
- need for replay (low is better)

## Suggested minimal fixture record
```json
{
  "fixture_id": "fr-001",
  "counterparty": "testy",
  "surface": "hub_dm",
  "thread_id": "...",
  "prior_frame": {"...": "..."},
  "new_evidence": ["..."],
  "expected_behavior": "refresh",
  "expected_next_move": "ask for the recent failure case",
  "expected_decision_posture": "exploratory",
  "should_use_frame": true
}
```

## Why this set is enough for now
It covers:
- straightforward success
- obligation persistence
- uncertainty handling
- invalidation boundaries
- non-action / waiting
- routing changes
- local selection behavior
- multi-surface separation

That is enough to keep the object honest without inventing a giant benchmark first.

## Current bias
Prefer:
- compact fixture sets
- realistic cases over synthetic combinatorics
- eval on action and posture, not prose similarity
- hard cases where wrong use is costly
