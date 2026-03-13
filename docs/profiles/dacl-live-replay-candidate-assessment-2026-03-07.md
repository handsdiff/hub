# DACL live replay candidate assessment — 2026-03-07

## Summary
PR #84 is the right **historical anchor** for recent DACL activity, but it is a **weak replay candidate** for the highest-risk reducer path.

Why:
- no GitHub reviews
- no review comments / review threads visible via public API
- no check runs on the PR head SHA
- owner comment on PR #84 explicitly says required checks were not configured for that PR

So PR #84 can validate topology / parent-PR handoff history, but it does **not** exercise the control-plane edge cases we care about most:
- head invalidation against prior approval
- semantic thread ambiguity
- partial CI rerun transitions
- out-of-order review/comment/check arrival

## Verified API snapshot

### PR #84
- number: `84`
- title: `parent: #78 live dashboard data-layer migration`
- state: `closed`
- merged_at: `2026-03-06T15:29:10Z`
- URL: `https://github.com/alexjaniak/DACL/pull/84`

### Public API counts
- reviews: `0`
- review comments: `0`
- issue comments: `2`
- check runs on head SHA: `0`

### Relevant owner comment
PR #84 issue comment says:
- `Checks/CI status: no required checks are configured on this repository for this PR (explicitly waived).`

## Nearby recent PRs checked
Also inspected recent closed PRs: `86`, `85`, `82`, `77`, `75`, `73`.

Common pattern across all checked PRs:
- reviews: `0`
- review comments: `0`
- check runs on head SHA: `0`

Conclusion:
- recent public DACL PR history does not provide a natural real replay for the force-push + semantic-thread + partial-CI sequence

## Operational implication
`live-replay-001` should wait for one of these:
1. the next real DACL PR that actually has review/comment/check activity, or
2. a private/local watcher capture stream that includes those events even if the public API history does not expose them

Until then, keep v0.1 frozen and do not pretend PR #84 is a high-fidelity replay of the risky path.
