# live-replay-001

Status: scaffolded, waiting for first real DACL capture.

## Intended target
- repo: `alexjaniak/DACL`
- replay type: force-push + thread churn + partial CI rerun

## Files
- `input.jsonl` — raw reducer inputs, one JSON object per captured poll
- `expected.jsonl` — expected packet + notifier outputs, one JSON object per input line

## Workflow
1. append real captured reducer inputs to `input.jsonl`
2. derive reviewed expected outputs into `expected.jsonl`
3. run replay against reducer + notifier
4. if replay disagrees with human judgment, add failing fixture before behavior changes

Helper command:
```bash
python3 hub/scripts/dacl_live_replay_capture.py --once
```

## Current blocker
As of `2026-03-07T07:20Z`, `alexjaniak/DACL` has no open pull requests via the public GitHub API.
So `live-replay-001` is waiting on the next active DACL PR / force-push window rather than on more design work.

## Historical candidate assessment
See:
- `hub/docs/profiles/dacl-live-replay-candidate-assessment-2026-03-07.md`

Current conclusion:
- PR `#84` is the most recent historical anchor, but not a good high-risk replay candidate from the public API because it has no reviews, no review comments, and no check runs.

## Schema reference
See:
- `hub/docs/profiles/dacl-live-replay-001-format.md`
