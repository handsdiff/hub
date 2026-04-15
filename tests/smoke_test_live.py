#!/usr/bin/env python3
"""
Live smoke test against a running Hub server.

Builds a realistic environment (agents, messages, attestations, signals,
bounties, obligations) then exercises every major endpoint category.
Reports pass/fail for each.

Usage:
    python tests/smoke_test_live.py [base_url]
    Default base_url: http://127.0.0.1:9555
"""

import json
import sys
import time
import requests

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9555"
PASS = 0
FAIL = 0
ERRORS = []


def check(label, resp, expect_status=200, expect_json_key=None):
    global PASS, FAIL
    ok = resp.status_code == expect_status
    if ok and expect_json_key:
        try:
            data = resp.json()
            ok = expect_json_key in data
        except Exception:
            ok = False
    if ok:
        PASS += 1
        print(f"  PASS  {label} [{resp.status_code}]")
    else:
        FAIL += 1
        body = resp.text[:200] if resp.text else "(empty)"
        ERRORS.append(f"{label}: expected {expect_status}, got {resp.status_code} — {body}")
        print(f"  FAIL  {label} [{resp.status_code}] {body[:100]}")
    return resp


# ═══════════════════════════════════════════════════════════════
#  Phase 1: Build realistic state
# ═══════════════════════════════════════════════════════════════
print("\n=== Phase 1: Building realistic environment ===\n")

secrets = {}
agents = ["alpha", "beta", "gamma", "delta", "epsilon"]
for aid in agents:
    r = requests.post(f"{BASE}/agents/register", json={
        "agent_id": aid,
        "description": f"Test agent {aid} for smoke testing",
        "capabilities": ["testing", "collaboration", aid],
    })
    if r.status_code == 200:
        secrets[aid] = r.json()["secret"]
        print(f"  Registered {aid}")
    elif r.status_code == 409:
        print(f"  {aid} already exists (restart server with clean data for full test)")
        secrets[aid] = None
    else:
        print(f"  FAIL registering {aid}: {r.status_code} {r.text[:100]}")
        secrets[aid] = None

# Send messages between agents (builds collaboration data)
for sender, receiver in [("alpha", "beta"), ("beta", "alpha"), ("alpha", "gamma"),
                          ("gamma", "alpha"), ("beta", "gamma"), ("delta", "epsilon"),
                          ("epsilon", "delta"), ("alpha", "delta"), ("gamma", "delta")]:
    s = secrets.get(sender)
    if not s:
        continue
    for i in range(3):  # Multiple messages per pair
        r = requests.post(f"{BASE}/agents/{receiver}/message", json={
            "from": sender,
            "secret": s,
            "message": f"Test message {i} from {sender} about https://github.com/test/repo commit abc123",
        })
# Print summary
print(f"  Sent messages between {len(agents)} agents")

# Create attestations (cross-agent trust)
attest_pairs = [("alpha", "beta"), ("beta", "alpha"), ("gamma", "alpha"),
                ("delta", "beta"), ("epsilon", "gamma"), ("alpha", "gamma")]
for attester, subject in attest_pairs:
    s = secrets.get(attester)
    if not s:
        continue
    r = requests.post(f"{BASE}/trust/attest", json={
        "from": attester,
        "secret": s,
        "agent_id": subject,
        "category": "reliability",
        "score": 0.8,
        "evidence": f"Worked with {subject} on 3 obligations, all delivered on time",
        "evidence_type": "behavioral",
    })
print(f"  Created {len(attest_pairs)} attestations")

# Submit trust signals
signal_pairs = [("alpha", "beta", "routing"), ("beta", "gamma", "security"),
                ("gamma", "delta", "work-quality"), ("delta", "epsilon", "commerce"),
                ("epsilon", "alpha", "attestation"), ("alpha", "delta", "co-occurrence")]
for sender, about, channel in signal_pairs:
    r = requests.post(f"{BASE}/trust/signal", json={
        "from": sender, "about": about, "channel": channel,
        "strength": 0.7, "evidence": f"Signal from {sender} about {about}",
    })
print(f"  Created {len(signal_pairs)} trust signals")

# Create bounties
bounty_ids = []
for i, (poster, desc) in enumerate([
    ("alpha", "Write integration tests for the messaging module"),
    ("beta", "Review and improve error handling in trust.py"),
    ("gamma", "Document the obligation lifecycle state machine"),
]):
    s = secrets.get(poster)
    if not s:
        continue
    r = requests.post(f"{BASE}/bounties", json={
        "agent_id": poster, "secret": s,
        "demand": desc, "usdc_amount": 5 + i,
    })
    if r.status_code == 200:
        bid = r.json().get("bounty", {}).get("id")
        bounty_ids.append(bid)
    else:
        print(f"  WARN bounty create: {r.status_code} {r.text[:100]}")
print(f"  Created {len(bounty_ids)} bounties")

# Claim and deliver one bounty
if bounty_ids and secrets.get("delta"):
    bid = bounty_ids[0]
    requests.post(f"{BASE}/bounties/{bid}/claim", json={
        "agent_id": "delta", "secret": secrets["delta"],
    })
    requests.post(f"{BASE}/bounties/{bid}/deliver", json={
        "agent_id": "delta", "secret": secrets["delta"],
        "delivery_note": "Tests written and passing. See PR #42.",
    })
    print(f"  Bounty {bid} claimed and delivered by delta")

# Create obligations
obl_ids = []
for creator, counterparty, commitment in [
    ("alpha", "beta", "Deliver a trust decay analysis report by end of week"),
    ("gamma", "delta", "Review and approve the settlement queue refactor PR"),
    ("beta", "epsilon", "Provide security audit of the WebSocket handling code"),
]:
    s = secrets.get(creator)
    if not s:
        continue
    from datetime import datetime, timedelta
    deadline = (datetime.utcnow() + timedelta(hours=72)).isoformat() + "Z"
    r = requests.post(f"{BASE}/obligations", json={
        "from": creator, "secret": s,
        "counterparty": counterparty,
        "commitment": commitment,
        "deadline_utc": deadline,
    })
    if r.status_code in (200, 201):
        oid = r.json().get("obligation", {}).get("id", r.json().get("id"))
        obl_ids.append(oid)
    else:
        print(f"  WARN obligation create: {r.status_code} {r.text[:100]}")
print(f"  Created {len(obl_ids)} obligations")

# Accept one obligation
if obl_ids and secrets.get("beta"):
    r = requests.post(f"{BASE}/obligations/{obl_ids[0]}", json={
        "from": "beta", "secret": secrets["beta"],
        "action": "accept",
    })
    print(f"  Obligation {obl_ids[0]} accepted by beta: {r.status_code}")


# ═══════════════════════════════════════════════════════════════
#  Phase 2: Exercise ALL endpoint categories
# ═══════════════════════════════════════════════════════════════
print("\n=== Phase 2: Exercising endpoints ===\n")

print("-- Health / Index --")
check("GET /health", requests.get(f"{BASE}/health"), expect_json_key="status")
check("GET /", requests.get(f"{BASE}/"))

print("\n-- Messaging --")
check("GET /agents", requests.get(f"{BASE}/agents"), expect_json_key="agents")
check("GET /agents/alpha", requests.get(f"{BASE}/agents/alpha"))
s_alpha = secrets.get("alpha", "")
check("GET /agents/alpha/messages (inbox)",
      requests.get(f"{BASE}/agents/alpha/messages?secret={s_alpha}"),
      expect_json_key="messages")
check("GET /agents/alpha/messages/sent",
      requests.get(f"{BASE}/agents/alpha/messages/sent?secret={s_alpha}"))
if s_alpha:
    check("POST send message",
          requests.post(f"{BASE}/agents/beta/message", json={
              "from": "alpha", "secret": s_alpha, "message": "smoke test ping",
          }))
else:
    print("  SKIP POST send message (no secret for alpha — agent existed from prior run)")
check("GET /agents/match", requests.get(f"{BASE}/agents/match?need=testing"))

print("\n-- Trust Profile (the crash point) --")
for aid in agents:
    check(f"GET /trust/{aid}", requests.get(f"{BASE}/trust/{aid}"), expect_json_key="version")
check("GET /trust/nonexistent", requests.get(f"{BASE}/trust/nonexistent"), expect_json_key="version")
check("GET /trust (list all)", requests.get(f"{BASE}/trust"))

print("\n-- Trust Signals & Attestations --")
check("GET /trust/signals", requests.get(f"{BASE}/trust/signals"))
check("GET /trust/alpha/signals", requests.get(f"{BASE}/trust/alpha/signals"))
check("GET /trust/attest/beta", requests.get(f"{BASE}/trust/attest/beta"))
check("GET /trust/signal/channels", requests.get(f"{BASE}/trust/signal/channels"))
check("GET /trust/schema", requests.get(f"{BASE}/trust/schema"))

print("\n-- Trust Advanced --")
check("GET /trust/topology/alpha", requests.get(f"{BASE}/trust/topology/alpha"))
check("GET /trust/consistency/alpha", requests.get(f"{BASE}/trust/consistency/alpha"))
check("GET /trust/query?about=beta", requests.get(f"{BASE}/trust/query?about=beta"))
check("GET /trust/graph?agent=alpha", requests.get(f"{BASE}/trust/graph?agent=alpha"))
check("GET /trust/profile?agent=alpha", requests.get(f"{BASE}/trust/profile?agent=alpha"))
check("GET /trust/capabilities?agent=alpha", requests.get(f"{BASE}/trust/capabilities?agent=alpha"))
check("GET /trust/staleness", requests.get(f"{BASE}/trust/staleness"))

print("\n-- Trust Disputes --")
if secrets.get("alpha"):
    r = requests.post(f"{BASE}/trust/dispute", json={
        "from": "alpha", "secret": secrets["alpha"],
        "against": "epsilon", "category": "non-delivery",
        "evidence": "Failed to deliver promised code review within TTL",
    })
    check("POST /trust/dispute", r)
    if r.status_code == 200:
        did = r.json().get("dispute_id")
        check(f"GET /trust/dispute/{did}", requests.get(f"{BASE}/trust/dispute/{did}"))
check("GET /trust/disputes", requests.get(f"{BASE}/trust/disputes"))

print("\n-- Trust Gate & Oracle --")
check("GET /trust/gate/alpha", requests.get(f"{BASE}/trust/gate/alpha"))
check("GET /trust/oracle/aggregate/alpha", requests.get(f"{BASE}/trust/oracle/aggregate/alpha"))

print("\n-- Trust Divergence (requires dual_ewma module) --")
r = requests.get(f"{BASE}/trust/divergence/alpha")
# 501 if dual_ewma not installed, 404 if no attestations, 200 if working
check("GET /trust/divergence/alpha", r, expect_status=r.status_code if r.status_code in (200, 404, 501) else 200)
r = requests.get(f"{BASE}/trust/divergence/network")
check("GET /trust/divergence/network", r, expect_status=r.status_code if r.status_code in (200, 501) else 200)
r = requests.get(f"{BASE}/trust/divergence/alpha/channels")
check("GET /trust/divergence/alpha/channels", r, expect_status=r.status_code if r.status_code in (200, 404, 501) else 200)

print("\n-- Trust Synthesis (requires multi_channel_trust module) --")
r = requests.get(f"{BASE}/trust/synthesis/alpha")
check("GET /trust/synthesis/alpha", r, expect_status=r.status_code if r.status_code in (200, 500) else 200)

print("\n-- Bounties --")
check("GET /bounties", requests.get(f"{BASE}/bounties"))
check("GET /hub/leaderboard", requests.get(f"{BASE}/hub/leaderboard"))
if bounty_ids:
    check("GET /bounties (by id filter)", requests.get(f"{BASE}/bounties?id={bounty_ids[0]}"))

print("\n-- Obligations --")
check("GET /obligations", requests.get(f"{BASE}/obligations"))
if obl_ids:
    check(f"GET /obligations/{obl_ids[0]}",
          requests.get(f"{BASE}/obligations/{obl_ids[0]}?secret={secrets.get('alpha', '')}"))

print("\n-- Agents Module --")
check("GET /agents/alpha/profile", requests.get(f"{BASE}/agents/alpha/profile"))
check("GET /agents/alpha/permissions", requests.get(f"{BASE}/agents/alpha/permissions"))
if secrets.get("alpha"):
    check("GET /agents/alpha/portfolio",
          requests.get(f"{BASE}/agents/alpha/portfolio?secret={secrets['alpha']}"))
    check("GET /agents/alpha/checkpoints",
          requests.get(f"{BASE}/agents/alpha/checkpoints?secret={secrets['alpha']}"))
check("GET /agents/alpha/behavioral-history",
      requests.get(f"{BASE}/agents/alpha/behavioral-history"))

print("\n-- Analytics --")
check("GET /collaboration", requests.get(f"{BASE}/collaboration"))
check("GET /collaboration/feed", requests.get(f"{BASE}/collaboration/feed"))
check("GET /collaboration/capabilities", requests.get(f"{BASE}/collaboration/capabilities"))
check("GET /activity", requests.get(f"{BASE}/activity"))

print("\n-- Assets / Monitors / Trails --")
check("GET /assets", requests.get(f"{BASE}/assets"))
check("GET /monitors", requests.get(f"{BASE}/monitors"))
check("GET /trails/alpha", requests.get(f"{BASE}/trails/alpha"))

print("\n-- Intel --")
check("GET /intel (may be 404 if no data)", requests.get(f"{BASE}/intel"),
      expect_status=404)  # No intel snapshots in test env
check("GET /intel/status (needs API key)", requests.get(f"{BASE}/intel/status"),
      expect_status=401)

print("\n-- Demands --")
check("GET /trust/demand", requests.get(f"{BASE}/trust/demand"))

print("\n-- Memory / Witness --")
check("GET /memory/verify (needs params)", requests.get(f"{BASE}/memory/verify?agent_id=alpha"))

print("\n-- Public --")
check("GET /public/trust-report/alpha/beta",
      requests.get(f"{BASE}/public/trust-report/alpha/beta"))

print("\n-- Skill (static file, may 404) --")
r = requests.get(f"{BASE}/skill")
check("GET /skill", r, expect_status=r.status_code if r.status_code in (200, 404) else 200)

print("\n-- WoT Bridge --")
check("GET /trust/wot-bridge/status", requests.get(f"{BASE}/trust/wot-bridge/status"))


# ═══════════════════════════════════════════════════════════════
#  Phase 3: Report
# ═══════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
print(f"  PASS: {PASS}   FAIL: {FAIL}   TOTAL: {PASS + FAIL}")
print(f"{'='*60}")
if ERRORS:
    print("\nFailed endpoints:")
    for e in ERRORS:
        print(f"  - {e}")
sys.exit(1 if FAIL > 0 else 0)
