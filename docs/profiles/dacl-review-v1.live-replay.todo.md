# DACL live replay fixture TODO

Status: pending first real watcher capture.

## Purpose
Promote one real DACL PR evaluation payload into a replayable fixture.

Capture format reference:
- `hub/docs/profiles/dacl-live-replay-001-format.md`

Why:
- synthetic fixtures catch architecture regressions
- live replay catches normalization drift against provider reality

## Minimum capture bundle
- raw GitHub snapshot payload used by reducer
- profile id + version at evaluation time
- resulting canonical packet
- notifier tuple + decision
- timestamp and PR URL

## Acceptance rule
A live replay fixture should be added before any v0.2 semantic change ships.

## Note
No real DACL watcher capture has been ingested into this workspace yet, so this remains a TODO rather than a fabricated fixture.
