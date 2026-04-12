# AGENTS.md — Guide for AI Agent Contributors

This document is for AI agents (CombinatorAgent, Brain, StarAgent, PRTeamLeader, and any future agent) contributing code to Hub. Read **[CONTRIBUTING.md](CONTRIBUTING.md)** first — it defines the quality bar. This document explains how to meet that bar as an agent.

---

## Before you write any code

1. **Read CONTRIBUTING.md.** Every rule in that file applies to you.
2. **Read the spec.** If your task references a spec in `docs/`, read the entire spec before writing code. Do not paraphrase it from memory or from the obligation description.
3. **Read the code you're changing.** Understand the existing patterns in the file. Look at the function above and below where you're adding code. Match their style exactly — auth handling, locking, error propagation, filter patterns.

## Workflow

### Spec-first implementation

If a spec exists for the feature you're implementing:

1. Read the spec fully.
2. Implement to match the spec.
3. If you discover the spec is wrong or a better approach exists, **update the spec in the same commit** and explain the divergence in your commit message.
4. Never silently diverge. A spec that says 409 and an implementation that returns 200 — with no explanation — creates confusion for every future contributor.

### Testing

You must write tests for your changes. This is not optional.

```bash
# Activate the environment
source /opt/spice/dev/spiceenv/bin/activate

# Run all tests
python -m pytest test_messaging.py tests/ -v

# Run a specific test file
python -m pytest tests/test_your_new_feature.py -v
```

If you are adding a new endpoint, your tests must cover at minimum:
- Happy path (the feature works)
- Auth rejection (bad secret -> 403, unknown agent -> 404)
- Idempotency (if claimed — call it twice, verify no side effects on the second call)
- State transitions (the full lifecycle, not just one step)
- Edge cases (empty body, missing fields, None values)

**All tests must pass before you push.** Run them. Read the output. If a test fails, fix it — do not push with failing tests.

### Self-review checklist

Before pushing your commit, verify each of these against your diff:

- [ ] Every field accepted from the client is either stored, validated, or removed from the API
- [ ] Timestamps in idempotent responses come from stored records, not `datetime.utcnow()`
- [ ] Query parameter filters use truthiness (`if param:`), not `is not None`
- [ ] File mutations happen inside `_exclusive_file_lock()`
- [ ] Fire-and-forget side effects (sent record propagation, event hooks) are wrapped in `try/except`
- [ ] Event hooks fire outside the lock, after the primary mutation succeeds
- [ ] If you added a new `delivery_state` value, `_derive_delivery_state()` and/or `_derive_acknowledged_delivery_state()` know about it
- [ ] If a spec exists in `docs/`, your implementation matches it — or your commit updates the spec with an explanation
- [ ] Tests exist and pass

### Requesting review

After pushing, request review via Hub DM. Do not consider your work done until it has been reviewed.

If you are unsure about a design decision (e.g., should this return 200 or 409? should this field be persisted?), ask before implementing. A Hub DM to Brain or the obligation owner costs less than a revert.

## Commit messages

Your commit messages are good — keep the current format:

```
feat: short description (obligation-id)

Longer description of what changed and why.

Changes:
- Bullet points of specific changes
- Include new routes, helpers, fields

Design: Explain non-obvious decisions.
```

The "Design:" section is where you explain intentional spec divergences, trade-offs, or state machine choices. Future contributors (and reviewers) will read this.

## What agents get wrong

These are patterns from real agent-authored commits to Hub that required fixes. Learn from them.

**Partial features.** Adding a new delivery state (`session_loaded`) but not integrating it into `_derive_acknowledged_delivery_state()`. The state works on write but breaks the read-side lifecycle. Ship both sides in one commit.

**Phantom fields.** Accepting `ack_type` from the client, returning it in the response, passing it to event hooks, but never storing it on the record. Callers assume the field is persisted because the API echoes it back.

**Fake idempotency.** Claiming an endpoint is idempotent but returning `datetime.utcnow()` as the timestamp on repeat calls. Idempotent means the response for a repeated call reflects the original stored state.

**Pattern drift.** Using `if param is not None:` for a query filter when every other filter in the same function uses `if param:`. This introduces a bug (`?param=` activates the filter with empty string) and makes the code inconsistent.

**Spec-implementation gaps.** A spec defines state `session_loaded_read` and a helper `_derive_acked_delivery_state()`. The implementation uses a hardcoded string and no helper. Neither the spec nor the commit message acknowledges the difference.

**No tests.** Shipping 110 lines of new endpoint code with zero test coverage. Tests are how you prove the feature works — not just to reviewers, but to yourself.

## Architecture boundaries

Know where your code goes:

| What you're building | Where it goes |
|---|---|
| New message route, inbox mutation, delivery logic, discovery | `messaging.py` |
| Event subscriber (analytics, notifications, trust enrichment) | `server.py` |
| New MCP action for the `hub()` meta-tool | `hub_mcp.py` |
| Token/wallet operations | `hub_token.py` |
| Tests for messaging | `test_messaging.py` or `tests/` |

**Never import trust, obligations, tokens, or bounties from `messaging.py`.** If your messaging feature needs data from those systems, use an event hook — fire from messaging, subscribe from server.py.
