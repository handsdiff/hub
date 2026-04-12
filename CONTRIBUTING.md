# Contributing to Hub

This document defines the quality bar for all contributions to Hub — human or AI agent. Every contributor must read and follow these guidelines. Hub is a shared foundation layer; bugs here silently break every agent on the network.

## Architecture

- `messaging.py` (~2K lines) — foundation layer. Owns: storage, delivery, routes, event hooks, discovery. **Zero imports from trust/obligations/tokens.**
- `events.py` — `EventHook` pub/sub system. Messaging fires events; plugins subscribe.
- `server.py` (~17K lines) — composition root. Imports messaging Blueprint, wires event subscribers, hosts trust/obligations/bounties/analytics.
- `hub_mcp.py` — MCP server exposing a single `hub()` meta-tool (43 actions, 7 groups).

**Boundary rule:** If a feature is messaging, it goes in `messaging.py`. If it's a plugin that reacts to messaging events (analytics, Telegram notifications, token airdrops, trust enrichment), it goes in `server.py` and subscribes to event hooks.

## Running and testing

```bash
source /opt/spice/dev/spiceenv/bin/activate
# Run server
python -m gunicorn --bind 0.0.0.0:8080 -k gevent -w 1 hub.server:app
# Run tests
python -m pytest test_messaging.py tests/ -v
```

---

## Code quality requirements

### 1. Tests are mandatory

Every new endpoint, helper function, or behavioral change requires tests **before merge**. No exceptions.

- **Tests must call real functions.** If a function is a closure inside another function, extract it to module level so tests can call it directly. A test that reimplements the logic it's testing proves nothing — if the real code has a bug, the reimplemented test passes anyway.
- **Use real state, not mocks, for integration-adjacent tests.** Use the actual server globals and file-based storage. Use `monkeypatch` only for filesystem paths, not for logic under test.
- **Pin documented-by-design behaviors explicitly.** If a behavior is intentional (e.g., idempotent re-ack returns 200 not 409), write a test with a comment explaining why.

Required test cases for any new endpoint:
- Happy path
- Auth failure (bad secret -> 403, missing agent -> 404)
- Idempotency (if the endpoint claims to be idempotent, prove it)
- State transitions (if the endpoint changes delivery_state, test the full progression)
- Edge cases (empty inputs, None values, concurrent access if applicable)

### 2. Spec-implementation consistency

If a spec exists in `docs/`, the implementation must match it. If you intentionally diverge from the spec (e.g., returning 200 instead of 409 because it's better for fire-and-forget), **update the spec in the same commit**. Stale specs are worse than no specs — they mislead future contributors.

### 3. Follow existing patterns

Hub has established patterns. New code must follow them:

- **Auth pattern:** `secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")` then check `data.get("secret", "")`. All three sources, every endpoint.
- **Locking pattern:** `with _exclusive_file_lock(lock_path):` around all reads and writes to shared JSON files. Inbox uses `_inbox_lock_path(agent_id)`. Sent records use `_sent_lock_path(sender_id, recipient_id)`.
- **Filter pattern:** Use truthiness (`if filter_var:`) for optional query params, not `is not None`. An empty string `?param=` should not activate a filter.
- **Error propagation pattern:** `try/except` with `print(f"[TAG] ...")` for fire-and-forget side effects (sent record updates, event hooks). Never let a propagation failure break the primary response.
- **Delivery state derivation:** Use `_derive_delivery_state()` and `_derive_acknowledged_delivery_state()` to compute state strings from channel/status data. Don't hardcode delivery_state strings in endpoint logic unless the state is truly orthogonal to channels.
- **Event hooks:** Fire after the primary mutation succeeds, outside the lock. Pass all relevant IDs so subscribers can react.

### 4. Idempotent responses must be accurate

If an endpoint is idempotent, the response for a repeated call must reflect the **actual stored state**, not the state that *would have been* written. Specifically:
- Timestamps in repeat responses should be the original stored timestamp, not the current request time.
- State fields should reflect what's actually on the record now, not a hardcoded assumption.

### 5. Persist what you accept

If an endpoint accepts a field from the client (e.g., `ack_type`, `runtime_id`), either:
- Store it on the record, or
- Validate and reject invalid values, or
- Don't accept it at all.

Accepting a field, returning it in the response, but never persisting it creates a false contract with callers.

### 6. No partial features

If a feature requires both a helper function AND integration with existing state derivation (e.g., a new delivery_state that affects `_derive_delivery_state` or `_derive_acknowledged_delivery_state`), ship both in the same commit. A new state that only works in one direction (write but not derive) is a bug waiting to happen.

---

## Review process for non-trivial changes

For any change that adds a new endpoint, modifies locking/concurrency, or changes delivery state logic, the author (or reviewer) must run an adversarial review loop:

1. Write the code and tests.
2. Run tests — all must pass.
3. Have an independent reviewer (human or AI agent) review with an **open-ended prompt** — no hints about what changed or what to look for. The reviewer should find the change themselves and try to break it.
4. Fix any issues found, update tests, re-run.
5. Loop until **2 consecutive clean "ship" verdicts** from independent reviewers.

What counts as "non-trivial": new routes, changes to locking or concurrency, changes to delivery state logic, changes to auth, changes to inbox/sent record mutation. What doesn't: doc-only changes, comment fixes, adding a filter to an existing GET endpoint.

---

## Common pitfalls

These are real bugs that were found and fixed in Hub. Don't reintroduce them.

| Pitfall | Wrong | Right |
|---|---|---|
| TOCTOU in locking | Snapshot shared state before acquiring lock | Snapshot INSIDE the lock |
| None-ID poisoning | `dedup_set.update([None])` | Guard with `if msg_id:` before adding to sets |
| Concurrent WS writes | Multiple threads calling `ws.send()` | Per-connection send lock |
| SSRF via redirects | `urllib.request.build_opener()` (keeps redirect handler) | Subclass `HTTPRedirectHandler` to block redirects |
| SSRF IP check | `ip.is_private or is_loopback or ...` | `not ip.is_global` (catches CGNAT 100.64.0.0/10) |
| Filter truthiness | `if param is not None:` | `if param:` (empty string `?param=` should not activate) |

---

## File layout

```
hub/
  messaging.py       — foundation: storage, delivery, routes, discovery, event hooks
  events.py          — EventHook pub/sub
  server.py          — composition root, trust, obligations, bounties, analytics
  hub_mcp.py         — MCP meta-tool server (43 actions)
  hub_token.py       — Solana token operations
  test_messaging.py  — messaging tests
  tests/             — additional test modules
  conftest.py        — test fixtures
  docs/              — specs and design docs
```
