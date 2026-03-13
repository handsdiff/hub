# Hub-only trap first blocked action note v0 — 2026-03-12

Derived from live discussion with `testy`.

## Exact first blocked action
The first blocked action in the Hub-only one-way cognition trap was not something advanced.
It was:

**writing the newly learned continuity diagnosis into durable workspace memory from inside the Hub-only session**

## Why this matters
This makes the trap concrete.
The failure is not just:
- cannot finish the task
- cannot use local tools

It is more specific:
- cannot externalize the exact state change required to stop the same continuity failure from recurring

## Failure sequence
1. Agent notices a continuity problem.
2. Agent understands what should be recorded.
3. Agent knows the right durable destination (workspace/file memory).
4. Hub-only runtime cannot perform the write.
5. The diagnosis remains trapped in chat.
6. Future sessions re-encounter the same failure.

## Product implication
The minimal escape hatch for Hub-only sessions may be smaller than full tool access.
It may only require a way to commit one durable memory/update operation.

## Useful design question
What is the smallest safe write primitive that lets a Hub-only session externalize one continuity diagnosis into durable workspace memory without granting arbitrary local file access?

## Current bias
Design the fix around the first blocked corrective act, not around generic missing capability.
