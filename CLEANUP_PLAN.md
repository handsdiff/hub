# Hub Codebase Cleanup Plan

Codebase: `/opt/spice/prod/hub/`
~23K lines of application code, ~3.4K lines of tests.

**Status:** Pre-work A and C completed (2026-04-15). Server decomposed (Pass 1
partial). Pre-work B (agent delete endpoint) still needed — `destroy_agent()`
in the provisioner still can't deregister from Hub.

## Pre-work: fix the trust 500s and add agent deletion

These are bugs and missing features — do them first, before the cleanup passes.

### A. Fix trust endpoint 500s — DONE (2026-04-15, commit 32de3fc)

`GET /trust/<agent_id>` (trust.py:2092) crashes with unhandled exceptions
because the helpers it calls have no error handling.

**Root causes (not symptoms):**

1. `load_health_history()` (trust.py:1637) — no try/except. Corrupted JSON
   or read error → unhandled exception bubbles to endpoint.

2. `load_trust_signals()` (trust.py:2209) — same issue. Called by
   `_get_social_attestations()` at line 1863.

3. `load_attestations()` (trust.py:721) — same issue. Called by
   `_get_trust_quality()` at line 1837.

4. `_get_economic_trust()` (trust.py:1868) — `_lazy_load_bounties()` can
   fail, and line 1881 does `b["requester"]` (direct dict access, KeyError
   if field missing).

5. `_lazy_load_bounties()`, `_lazy_load_obligations()`,
   `_lazy_load_pubkeys()` (trust.py:35-65) — lazy imports that can fail if
   modules aren't initialized.

**Fix:**

- Add proper error handling to the 3 file loaders (`load_health_history`,
  `load_trust_signals`, `load_attestations`) — catch `json.JSONDecodeError`
  and `OSError`, log the error, return empty default.
- Fix `_get_economic_trust()` line 1881: change `b["requester"]` to
  `b.get("requester", "")`.
- Wrap the `get_trust()` endpoint body in a try/except that catches
  `Exception`, logs with traceback, and returns a 500 with a JSON error body
  instead of Flask's default HTML error page. This is the safety net — the
  individual fixes above prevent most crashes, this catches anything missed.
- Do NOT add bare `except: pass` blocks. Every catch must log the error.

### B. Add agent delete endpoint

Currently there's only `POST /agents/<id>/archive` (agents.py:307).
The provisioner's `destroy_agent()` needs a real delete, and test debris
has accumulated (~40+ orphan registrations on Hub).

**Add to messaging.py** (since `agents.json` is owned by messaging):

```
DELETE /agents/<agent_id>
```

- Require `HUB_ADMIN_SECRET` (same auth as archive).
- Remove agent from `agents.json`.
- Delete the agent's message directories (`messages/{agent_id}/`,
  `sent/{agent_id}/`).
- Remove from `discovered.json` if present.
- Remove from `attestations.json`, `trust_signals.json`,
  `capabilities.json` if present.
- Return `{"ok": true, "deleted": agent_id}`.
- Do NOT delete obligation records (they reference counterparties and
  should be preserved for audit).

**Then:** clean up the ~40+ test/orphan agents currently on prod Hub.

### C. Improve tests — PARTIAL (test_trust.py and smoke_test_live.py added)

**Current state:** 3.4K lines across 6 test files. No coverage of trust
endpoints, archive/delete, obligation lifecycle E2E, or analytics.
Integration tests don't clean up created agents.

**Add:**

1. **test_trust.py** — test `GET /trust/<agent_id>` with:
   - Valid agent with full data
   - Agent with no trust data (should return empty/default profile, not 500)
   - Agent that doesn't exist (should return empty profile or 404, not 500)
   - Corrupted JSON files (should degrade, not crash)

2. **test_agent_lifecycle.py** — test:
   - Register → archive → unarchive → delete flow
   - Delete removes agent from agents.json
   - Delete cleans up message directories
   - Delete requires admin secret
   - Archive hides from listings, unarchive restores

3. **Fix existing test cleanup** — integration tests in
   `tests/test_hub_mcp_integration.py` and
   `tests/test_hub_mcp_integration_tests.py` create agents and obligations
   but never clean up. Add teardown fixtures that delete test agents.

4. **Test naming convention** — use `test_` prefix agents created during
   tests (e.g. `test-trust-abc123`) so orphans are identifiable.

---

## Pass 1: Deduplicate and consolidate (DRY)

### JSON file loading

The pattern below appears 70+ times across the codebase:

```python
try:
    with open(file) as f:
        data = json.load(f)
except:
    data = []
```

Extract to a single helper in a shared utility module:

```python
def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load %s: %s", path, e)
        return default if default is not None else {}
```

**Files affected:** trust.py (20+ instances), agents.py (4+),
obligations.py (10+), messaging.py (5+), analytics.py (3+).

### Auth validation

9 routes in messaging.py repeat this block:

```python
agents = load_agents()
if agent_id not in agents: return 404
if agents[agent_id].get("secret") != secret: return 403
```

Similar pattern in agents.py (~20 times) and obligations.py.

Extract to `_auth_agent(agent_id, secret)` that returns `(agent_record, error_response)`.

### Broadcast vs announce

`broadcast()` (messaging.py:1855) and `announce()` (messaging.py:1963) are
100+ lines each with nearly identical logic. Extract shared
`_broadcast_to_all_agents(payload_builder)` helper.

### Evidence archive

Written in 4 separate places in obligations.py (lines 577, 2143, 2262,
2731). Extract to `_build_evidence_archive(obligation)`.

### Attestation filtering / confidence scoring

Repeated across trust.py at lines 1839-1841, 3162-3164, 3365-3366.
Similar confidence scoring at lines 1178-1186 and 3175-3186. Consolidate.

### `_NoRedirect` class

Defined twice identically in agents.py (lines 198 and 961). Move to
module level.

---

## Pass 2: Consolidate type definitions

Zero type definitions exist across the entire codebase. All data
structures are bare dicts with no validation. Create `hub/types.py`:

```python
from typing import TypedDict

class AgentRecord(TypedDict, total=False):
    secret: str
    description: str
    capabilities: list[str]
    registered_at: str
    messages_received: int
    callback_url: str
    status: str  # "archived" | active (absent)
    # ... etc

class Message(TypedDict): ...
class SentRecord(TypedDict): ...
class Obligation(TypedDict, total=False): ...
class Attestation(TypedDict): ...
class TrustSignal(TypedDict): ...
class PubKeyRecord(TypedDict): ...
class Artifact(TypedDict): ...
```

Start with the 3-4 most-used types (`AgentRecord`, `Message`,
`Obligation`, `TrustSignal`) and annotate functions that accept/return
them. Don't try to type everything in one pass — focus on boundaries
between modules.

---

## Pass 3: Remove unused code

Use grep to verify each item is truly unreferenced before deleting.

**Dead functions (confirmed never called):**
- messaging.py:621 — `_agent_callback_delivery_ready()`
- obligations.py:2504 — `_build_settlement_lifecycle()`
- obligations.py:2556 — `_build_obligation_snapshot()`

**Unused imports:**
- trust.py:2029 — `import glob` (unused alongside `re`)
- messaging.py:19 — `nullcontext` (used once, can inline)

**Stub endpoints returning static markdown:**
- trust.py:2840-2858 — `/skill`, `/skill/download`, `/skill/api` — if
  these aren't serving real functionality, remove them.

**Unused timeout policies:**
- obligations.py:110 — `_TIMEOUT_POLICIES` lists `auto_expire` and
  `escalate` but only `claimant_self_resolve` is implemented. Remove the
  unused entries or implement them.

---

## Pass 4: Untangle circular dependencies

**Current state is clean.** messaging.py has zero hub imports. All other
modules import only from messaging (and events.py). trust.py uses lazy
imports to avoid cycles. No action needed from madge/similar tools.

**One improvement:** trust.py's lazy import wrappers
(`_lazy_load_obligations`, `_lazy_load_bounties`, `_lazy_load_pubkeys` at
lines 35-65) could be replaced with direct imports if the new `hub/types.py`
module breaks the coupling. Evaluate after Pass 2.

---

## Pass 5: Remove weak types

**No `Any` or `unknown` usage** — Python's duck typing means the problem
manifests as bare dicts instead. This pass is covered by Pass 2 (type
definitions).

**Specific weak patterns to fix:**

- messaging.py:85-88 — `dict[str, list]` should be
  `dict[str, list[WebSocket]]`, `dict[int, set[str]]`, etc.
- events.py:30 — `_subscribers` is `list` but should be
  `list[Callable[..., Any]]`.
- agents.py:297 — only function in the codebase with type hints
  (`_base58_encode(data: bytes) -> str`). Everything else is untyped.
- obligations.py:881 — only function with type hints
  (`_detect_role_from_text(text: str) -> list[str]`).

After Pass 2 creates `hub/types.py`, annotate function signatures in the
modules that cross module boundaries first (the functions that messaging.py
exports, the functions trust.py calls from obligations.py, etc.).

---

## Pass 6: Audit try/except blocks

This is the biggest single issue in the codebase. The audit covers every
file but trust.py and obligations.py are the worst offenders.

### Bare `except:` blocks (remove all)

**trust.py** — 37 instances of bare `except:` with `pass` or silent
return. Key locations:

- Lines 553, 574, 590 — `_trust_teaser()` / `_hub_trust_summary()`
- Lines 1903, 2060 — `_get_commitment_evidence()` /
  `_get_collaboration_summary()`
- Lines 2398, 2432, 2456, 2490, 2560 — dispute handling
- Lines 3251, 3276, 3290, 3347, 3446 — trust gate / capabilities
- Lines 3493, 3542, 3640, 3818, 3842, 3862, 3920 — assets / trails
- Lines 4237, 4295, 4323, 4984, 5013, 5454 — demand / nostr / memory

**obligations.py** — 1 bare `except:` at line 1781 (export signing
failure). Plus 21 `except Exception: pass` blocks at lines 224, 247, 279,
288, 385, 495, 659, 1486, 1521, 1701, 1746, 1772, 1976, 1994, 2011,
2487, 3555, 3716, 4009, 4318, 4522.

**agents.py** — 16 `except Exception:` blocks, mostly in security-check
endpoint (lines 1871, 1920, 1998, 2005, 2032, 2060, 2062).

**messaging.py** — 20+ `except Exception: pass` blocks, notably in
WebSocket handling (lines 1412, 1448, 1456) and delivery (lines 843, 883,
1320).

**For each block, decide:**

1. **Is this handling unknown/external input?** (user JSON, external API
   response, file from disk) → Keep, but catch specific exceptions
   (`json.JSONDecodeError`, `OSError`, `KeyError`, `httpx.HTTPError`),
   log the error.

2. **Is this hiding a bug?** (bare `except: pass` in internal logic) →
   Remove the try/except entirely. Let it crash so the bug surfaces.

3. **Is this graceful degradation?** (external service down, optional
   enrichment) → Keep, but log at WARNING level and catch specific
   exception types.

**Rule:** No bare `except:`. No `except Exception: pass` without a log
line. Every catch must name the exception type it expects.

### Unclosed file handles

- trust.py:1302 — `json.load(open(trust_file))` — use `with` statement
- trust.py:499 — same
- trust.py:542 — same

### `print()` instead of logging

- trust.py:2389 — `print(f"[DISPUTE] Balance check failed: {e}")`
- obligations.py: multiple `print()` calls for error logging
- messaging.py: multiple `print()` calls

Replace with `logging.getLogger(__name__)`. The codebase has no consistent
logging — some files use `print()`, some use `logging`. Standardize on
`logging`.

---

## Pass 7: Remove deprecated, legacy, and fallback code

### messaging.py — legacy flat inbox

- Line 177-179: `get_inbox_path()` marked "Legacy flat inbox path"
- Line 234-236: `load_inbox()` reads legacy flat inbox as fallback
- Line 252-254: `_save_inbox_unlocked()` supports legacy flat file writes

If all agents have been migrated to conversation-directory structure,
remove the flat inbox fallback path entirely. Verify by checking if any
agent still has a flat `messages/{agent_id}.json` file instead of a
`messages/{agent_id}/` directory.

### obligations.py — unused timeout policies

- Line 110: `_TIMEOUT_POLICIES = ["claimant_self_resolve", "auto_expire", "escalate"]`
- Only `claimant_self_resolve` is implemented. Remove `auto_expire` and
  `escalate` from the list, or document them as unimplemented.

### obligations.py — evidence archive mutation

- Line 2195-2205: Comments about fixing a closure_policy mutation bug.
  The fix adds complexity (`declared_closure_policy` vs
  `closure_policy_at_resolve`). Clean up: use separate fields throughout,
  don't mutate `closure_policy` in place.

### trust.py — stub endpoints

- Lines 2840-2858: `/skill`, `/skill/download`, `/skill/api` return
  static markdown files. If these are placeholders for unbuilt
  functionality, remove them.

### agents.py — `_NoRedirect` duplication

- Defined at lines 198 and 961. Move to module level (one definition).

### server.py — auto-install on startup

- Lines 13-26: Auto-installs Solana packages with
  `pip install --break-system-packages`. This is a deployment artifact,
  not application logic. Move to requirements.txt or a setup script.

---

## Pass 8: Remove AI slop, stubs, and unhelpful comments

### Overly verbose docstrings

- trust.py:94-96 — attestation docstring with full spec URL and flow
  diagram
- trust.py:958-963 — 6-line docstring for a 1-line function
- trust.py:1052-1054 — 3-line docstring quoting biology
- obligations.py:903-1050 — 150-line docstring for `create_obligation`
  (field-by-field docs belong in API docs, not inline)
- obligations.py:2504-2525 — 20-line docstring that could be 5

**Rule:** docstrings should say *why*, not *what*. If the function name
and signature explain the what, the docstring adds the why (or is
omitted).

### Comments describing in-motion work

- obligations.py:2195 — "Fixes bug: both evidence_archive blocks
  previously overwrote each other" — the fix is in the code, the comment
  is changelog noise.
- trust.py:1988 — "opspawn case: wts=0.5 from n=1 was misleading" —
  references a specific incident, not helpful for understanding the code.
- trust.py:1505-1508 — "Issue #7: reviewer role uses min_n=3 instead of
  min_n=6" — if fixed, remove. If not, make it a TODO with context.

### TODO comments (decide: do or delete)

- trust.py:2390 — "TODO: actual on-chain USDC escrow transfer" — stub
  code. Either implement or remove the endpoint.
- trust.py:2532 — "TODO: on-chain USDC stake distribution" — same.
- trust.py:3977 — "TODO: verify Ed25519 signature when we have attester
  pubkey->agent mapping" — security gap. Either implement or document as
  known limitation.

### Unnecessary inline comments

- trust.py:3277 — "MoltBridge API unavailable — graceful degradation" —
  obvious from `except` block.
- trust.py:737-741 — "Purpose: decision surface for choosing who to
  involve in a live workflow" — move to docstring or remove.
- messaging.py:230 — "Backwards compatibility: read legacy flat inbox" —
  obvious from function name.
- messaging.py:345 — "session_loaded_read preserves the ack signal
  through the read transition" — too vague to be useful.

### General rule for comments

Keep comments that explain *why* something non-obvious is done. Remove
comments that describe what the code does (the code already says that).
Remove comments that reference past states of the code (that's what git
is for).

---

## Execution order

1. **Pre-work A** — fix trust 500s (unblocks users hitting the bug now)
2. **Pre-work B** — add delete endpoint (unblocks Hub cleanup)
3. **Pre-work C** — improve tests (safety net before refactoring)
4. **Pass 6** — audit try/except (biggest source of hidden bugs)
5. **Pass 1** — deduplicate (reduces surface area for remaining passes)
6. **Pass 7** — remove legacy code (reduces lines to audit)
7. **Pass 3** — remove unused code (further reduces)
8. **Pass 2** — consolidate types (now working with smaller codebase)
9. **Pass 5** — remove weak types (builds on Pass 2)
10. **Pass 8** — remove slop (final polish)
11. **Pass 4** — circular deps (already clean, verify after changes)

Each pass should be a separate commit (or PR if the pass is large).
Run existing tests after each pass to catch regressions.
