# Repeat Work Decision Table
*Created: 2026-03-27 12:55 UTC*
*Author: CombinatorAgent*
*Source lane: `1067c23 -> 317ab16 -> aed45b6`*

| observed condition | reducer decision | downstream artifact emitted |
|---|---|---|
| `1067c23`: accumulated evidence shows a real second-step request happened, but no returned artifact is yet confirmed | emit request-only intermediate state (`pending_request_sent`) instead of `pass`; keep representation separate from evidence | `docs/machine-readable-decision-contract-v0-2026-03-27.md` and `docs/repeat-work-forward-port-summary-2026-03-27.md` define the decision contract and forward-port semantics |
| `317ab16`: the reducer/docs change exists, but the commit is too broad for clean human audit because `server.py` drift hides the repeat-work delta inside unrelated changes | do not reinterpret semantics; emit a review-surface reduction step so the lane stays inspectable | `docs/repeat-work-review-packet-2026-03-27.md` and `docs/repeat-work-review-snippets-1067c23.md` narrow the review to the actual repeat-work handlers/docs |
| `aed45b6`: the lane now needs a handoff artifact that says what to inspect and what behavior changed, without forcing a reviewer to replay the whole diff | emit minimal executable handoff for reuse/review | `docs/repeat-work-minimal-handoff-2026-03-27.md` gives the bounded artifact chain and exact review target |
