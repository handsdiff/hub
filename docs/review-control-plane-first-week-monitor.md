# Review Control Plane — First Week Monitor

Status: v0.1 release-candidate monitoring plan.

## Only two questions matter

1. **Missed pages**
   - A materially risky PR state changed and the system did **not** emit.
2. **Noisy pages**
   - The system emitted, but the human learned nothing actionable.

Everything else is secondary.

## Daily review rubric

For each emitted notification, classify as one of:
- `good-page`
- `noisy-page`
- `unclear`

For each real PR incident discovered by humans, classify as one of:
- `caught-by-system`
- `missed-page`
- `unclear`

## Minimal event log format

```json
{
  "ts": "2026-03-07T00:00:00Z",
  "repo": "alexjaniak/DACL",
  "pr": 123,
  "head_sha": "abcdef1",
  "event_type": "good-page | noisy-page | missed-page | unclear",
  "packet_state": "mergeable | blocked | stale | needs-human",
  "action_key": "reconfirm.intent.thread-901.head.abcdef1",
  "risk_phase": "steady | ambiguous | explicit-blocking | contradictory-current-head",
  "why": "one sentence explanation",
  "follow_up": "none | new-fixture | reducer-fix | profile-tune | threshold-tune"
}
```

## Escalation rules

### If a missed page happens
Do immediately:
1. capture the raw reducer input snapshot
2. save emitted packet or note that none was emitted
3. add a failing fixture reproducing the miss
4. only then change reducer/profile/notifier behavior

### If a noisy page happens
Do immediately:
1. capture tuple + notification decision
2. identify whether noise came from:
   - blocker diff instability
   - action key instability
   - confidence bucket too sensitive
   - profile phrase over-triggering
3. add a failing fixture reproducing the noise
4. then tune behavior

## Success criteria for week 1

- `missed-page = 0`
- noisy pages stay rare enough that the human still trusts notifications
- every behavior change comes from a new failing fixture first

## Suggested summary line

At the end of each day:

```text
DACL v0.1 monitor: missed_pages=X, noisy_pages=Y, good_pages=Z, unclear=W
```
