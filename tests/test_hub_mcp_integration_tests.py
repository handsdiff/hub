"""
Hub MCP Integration Tests — Checkpoint + Obligation Tools

Tests hub_mcp.py tool functions against the live Hub REST API.

Two layers:
1. Direct httpx calls to Hub REST API — verifies request shape + HTTP status
2. MCP HTTP endpoint calls — verifies tool function + auth + JSON parsing via real SSE transport

Run against a remote Hub:
HUB_BASE_URL=https://admin.slate.ceo/oc/brain \
HUB_SECRET=your_secret \
MCP_URL=http://localhost:8090 \
pytest tests/test_hub_mcp_integration.py -v

Environment variables:
HUB_BASE_URL — Hub REST base URL (default: http://localhost:8080)
MCP_URL — MCP server URL (default: http://localhost:8090)
HUB_AGENT_ID — Agent ID (default: StarAgent)
HUB_SECRET — Agent secret (required)
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
import pytest
import pytest_asyncio

# ── Configuration ──────────────────────────────────────────────────────────────

BASE_URL = os.environ.get("HUB_BASE_URL", "http://localhost:8080")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:8090")
AGENT_ID = os.environ.get("HUB_AGENT_ID", "brain")
AGENT_SECRET = os.environ.get("HUB_SECRET", "")
# Counterparty for two-agent fixtures (e.g. confirm/reject tests where roles must be distinct)
COUNTERPARTY_ID = os.environ.get("HUB_COUNTERPARTY_ID", "StarAgent" if AGENT_ID == "brain" else "brain")
COUNTERPARTY_SECRET = os.environ.get("HUB_COUNTERPARTY_SECRET", "")


# ── MCP tool caller ───────────────────────────────────────────────────────────
# Calls a tool via the MCP HTTP JSON-RPC endpoint over SSE transport.
# Returns the parsed JSON result from the tool's text output.

def _call_mcp_tool(tool_name: str, arguments: dict) -> dict:
    """Call a tool on the local MCP server via HTTP JSON-RPC + SSE."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Agent-ID": AGENT_ID,
        "X-Agent-Secret": AGENT_SECRET,
    }
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{MCP_URL}/mcp", json=payload, headers=headers)
        resp.raise_for_status()
        raw = resp.text

    # Parse SSE stream: collect all "data:" lines
    data_lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
    for line in reversed(data_lines):
        try:
            msg = json.loads(line)
            if msg.get("id") == 1:
                result = msg.get("result", {})
                content = result.get("content", [{}])[0].get("text", "{}")
                return json.loads(content)
        except (json.JSONDecodeError, KeyError, IndexError):
            continue
    return {"error": f"Could not parse SSE response: {raw[:200]}"}


# ── Health check ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def hub_health():
    if not AGENT_SECRET:
        pytest.skip("HUB_SECRET not set")
    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.get(f"{BASE_URL}/health")
            r.raise_for_status()
            return r.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 502:
            pytest.skip("Hub returning 502 (Cloudflare/ASN issue, not a test failure)")
        raise
    except Exception as exc:
        pytest.skip(f"Hub unreachable at {BASE_URL}: {exc}")


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def test_obligation(hub_health):
    """Create a unique test obligation. Teardown resolves it."""
    obl_id = f"obl-test-{uuid.uuid4().hex[:12]}"
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{BASE_URL}/obligations",
            json={
                "from": AGENT_ID,
                "secret": AGENT_SECRET,
                "counterparty": hub_health.get("hub_id", "brain"),
                "commitment": f"[TEST] MCP integration — {obl_id}",
                "deadline_utc": deadline,
                "closure_policy": "counterparty_accepts",
            },
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        result = r.json()
        created_id = result.get("obligation", result).get("obligation_id")
        assert created_id

    yield created_id

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"{BASE_URL}/obligations/{created_id}/advance",
                json={
                    "from": AGENT_ID,
                    "secret": AGENT_SECRET,
                    "status": "resolved",
                    "binding_scope_text": "Test teardown",
                },
                headers={"Content-Type": "application/json"},
            )
    except Exception:
        pass


@pytest_asyncio.fixture
async def accepted_obligation(test_obligation):
    """Pre-accept a test obligation for checkpoint operations.

    Note: StarAgent is both proposer and counterparty in this fixture.
    Hub business rule: proposer cannot confirm/reject their own checkpoint → 403.
    Tests accept both 200 (if counterparty were different agent) and 403 (current).
    """
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{BASE_URL}/obligations/{test_obligation}/advance",
            json={
                "from": AGENT_ID,
                "secret": AGENT_SECRET,
                "status": "accepted",
                "binding_scope_text": f"Obligation {test_obligation} — accepted for checkpoint testing",
            },
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        return test_obligation


@pytest_asyncio.fixture
async def counterparty_accepted_obl(hub_health):
    """Create an obligation where AGENT_ID proposes and COUNTERPARTY accepts.

    Use this fixture when testing confirm/reject — the two-agent setup means
    the counterparty can legitimately confirm checkpoints (no 403 self-confirm).

    Requires HUB_COUNTERPARTY_ID and HUB_COUNTERPARTY_SECRET env vars.
    """
    if not COUNTERPARTY_SECRET:
        pytest.skip("HUB_COUNTERPARTY_SECRET not set")

    obl_id = f"obl-test-cp-{uuid.uuid4().hex[:12]}"
    deadline = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    async with httpx.AsyncClient(timeout=60.0) as client:
        # AGENT_ID proposes, COUNTERPARTY is counterparty
        r = await client.post(
            f"{BASE_URL}/obligations",
            json={
                "from": AGENT_ID,
                "secret": AGENT_SECRET,
                "counterparty": COUNTERPARTY_ID,
                "commitment": f"[TEST] Counterparty fixture — {obl_id}",
                "deadline_utc": deadline,
                "closure_policy": "counterparty_accepts",
            },
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()
        created_id = r.json().get("obligation", r.json()).get("obligation_id")
        assert created_id

        # COUNTERPARTY accepts (not AGENT_ID — roles must be distinct)
        r = await client.post(
            f"{BASE_URL}/obligations/{created_id}/advance",
            json={
                "from": COUNTERPARTY_ID,
                "secret": COUNTERPARTY_SECRET,
                "status": "accepted",
                "binding_scope_text": f"Obligation {created_id} — accepted by counterparty for confirm/reject tests",
            },
            headers={"Content-Type": "application/json"},
        )
        r.raise_for_status()

    yield created_id

    # Teardown: AGENT_ID resolves (ghost protocol will also handle this)
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            await client.post(
                f"{BASE_URL}/obligations/{created_id}/advance",
                json={
                    "from": AGENT_ID,
                    "secret": AGENT_SECRET,
                    "status": "resolved",
                    "binding_scope_text": "Test teardown",
                },
                headers={"Content-Type": "application/json"},
            )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1: Direct httpx calls to Hub REST API
# (verifies request shape + HTTP status)
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckpointREST:
    """Test the Hub REST checkpoint endpoint directly (what the MCP tools call)."""

    @pytest.mark.asyncio
    async def test_checkpoint_propose_returns_checkpoint_id(self, accepted_obligation):
        """checkpoint_propose → HTTP 200/201 + checkpoint_id + status=proposed."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{BASE_URL}/obligations/{accepted_obligation}/checkpoint",
                json={
                    "from": AGENT_ID,
                    "secret": AGENT_SECRET,
                    "action": "propose",
                    "summary": "REST layer test checkpoint",
                },
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code in (200, 201), f"Expected 200 or 201, got {r.status_code}: {r.text}"
            data = r.json()
            cp = data.get("checkpoint", data)
            assert cp.get("checkpoint_id"), f"Expected checkpoint_id: {data}"
            # obligation_id lives in the obligation object
            obl = data.get("obligation", {})
            assert obl.get("obligation_id") == accepted_obligation, \
                f"Expected obligation_id {accepted_obligation}, got {obl.get('obligation_id')}"
            assert cp.get("status") == "proposed"

    @pytest.mark.asyncio
    async def test_checkpoint_confirm_returns_200(self, counterparty_accepted_obl):
        """confirm → HTTP 200. Uses counterparty_accepted_obl so proposer ≠ counterparty.

        AGENT_ID proposes the checkpoint; COUNTERPARTY confirms it.
        Both roles are distinct, so confirm succeeds.
        """
        obl_id = counterparty_accepted_obl

        async with httpx.AsyncClient(timeout=60.0) as client:
            # AGENT_ID proposes
            r = await client.post(
                f"{BASE_URL}/obligations/{obl_id}/checkpoint",
                json={
                    "from": AGENT_ID, "secret": AGENT_SECRET,
                    "action": "propose", "summary": "Will confirm",
                },
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            cp_id = r.json().get("checkpoint", r.json()).get("checkpoint_id")

        async with httpx.AsyncClient(timeout=60.0) as client:
            # COUNTERPARTY confirms — distinct from proposer → 200
            r = await client.post(
                f"{BASE_URL}/obligations/{obl_id}/checkpoint",
                json={
                    "from": COUNTERPARTY_ID, "secret": COUNTERPARTY_SECRET,
                    "action": "confirm", "checkpoint_id": cp_id,
                    "note": "Confirmed by REST layer test",
                },
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
            data = r.json()
            cp = data.get("checkpoint", data)
            assert cp.get("status") == "confirmed"

    @pytest.mark.asyncio
    async def test_checkpoint_reject_returns_200(self, counterparty_accepted_obl):
        """reject → HTTP 200. Uses counterparty_accepted_obl so proposer ≠ counterparty.

        AGENT_ID proposes the checkpoint; COUNTERPARTY rejects it.
        """
        obl_id = counterparty_accepted_obl
        reason = f"REST layer test rejection — {uuid.uuid4().hex[:8]}"

        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{BASE_URL}/obligations/{obl_id}/checkpoint",
                json={
                    "from": AGENT_ID, "secret": AGENT_SECRET,
                    "action": "propose", "summary": "Will reject",
                },
                headers={"Content-Type": "application/json"},
            )
            r.raise_for_status()
            cp_id = r.json().get("checkpoint", r.json()).get("checkpoint_id")

        async with httpx.AsyncClient(timeout=60.0) as client:
            # COUNTERPARTY rejects — distinct from proposer → 200
            r = await client.post(
                f"{BASE_URL}/obligations/{obl_id}/checkpoint",
                json={
                    "from": COUNTERPARTY_ID, "secret": COUNTERPARTY_SECRET,
                    "action": "reject", "checkpoint_id": cp_id, "reason": reason,
                },
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
            data = r.json()
            cp = data.get("checkpoint", data)
            assert cp.get("status") == "rejected"

    @pytest.mark.asyncio
    async def test_checkpoint_confirm_requires_checkpoint_id(self, accepted_obligation):
        """confirm without checkpoint_id → HTTP 4xx."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{BASE_URL}/obligations/{accepted_obligation}/checkpoint",
                json={
                    "from": AGENT_ID, "secret": AGENT_SECRET,
                    "action": "confirm", "checkpoint_id": "",
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 502:
                pytest.skip("Hub returning 502 (Cloudflare/ASN issue)")
            assert 400 <= r.status_code < 500

    @pytest.mark.asyncio
    async def test_checkpoint_reject_requires_reason(self, accepted_obligation):
        """reject without reason → HTTP 4xx."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{BASE_URL}/obligations/{accepted_obligation}/checkpoint",
                json={
                    "from": AGENT_ID, "secret": AGENT_SECRET,
                    "action": "reject", "checkpoint_id": "cp-fake", "reason": "",
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 502:
                pytest.skip("Hub returning 502 (Cloudflare/ASN issue)")
            assert 400 <= r.status_code < 500


class TestAdvanceREST:
    """Test the Hub REST advance endpoint."""

    @pytest.mark.asyncio
    async def test_advance_proposed_to_resolved_is_invalid(self, test_obligation):
        """proposed→resolved (skipping accepted) → HTTP 409 Conflict."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{BASE_URL}/obligations/{test_obligation}/advance",
                json={
                    "from": AGENT_ID, "secret": AGENT_SECRET,
                    "status": "resolved", "note": "Should fail",
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 502:
                pytest.skip("Hub returning 502 (Cloudflare/ASN issue)")
            assert r.status_code == 409, f"Expected 409, got {r.status_code}"

    @pytest.mark.asyncio
    async def test_advance_invalid_status_returns_4xx(self, test_obligation):
        """Invalid status value → HTTP 4xx."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{BASE_URL}/obligations/{test_obligation}/advance",
                json={
                    "from": AGENT_ID, "secret": AGENT_SECRET,
                    "status": "not_a_real_status",
                },
                headers={"Content-Type": "application/json"},
            )
            if r.status_code == 502:
                pytest.skip("Hub returning 502 (Cloudflare/ASN issue)")
            assert 400 <= r.status_code < 500


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2: MCP HTTP endpoint — tool function + auth + JSON parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckpointMCP:
    """Test hub_mcp.py tool functions via the MCP HTTP JSON-RPC endpoint."""

    def test_checkpoint_propose_via_mcp(self, accepted_obligation):
        """manage_obligation_checkpoint(action=propose) → checkpoint_id."""
        result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "propose",
            "obligation_id": accepted_obligation,
            "summary": "MCP layer test checkpoint",
        })
        assert "error" not in result, f"Expected no error: {result}"
        cp = result.get("checkpoint", result)
        assert cp.get("checkpoint_id"), f"Expected checkpoint_id: {result}"
        assert cp.get("status") == "proposed"

    def test_checkpoint_confirm_via_mcp(self, accepted_obligation):
        """manage_obligation_checkpoint(action=confirm) → confirmed or 403 (Hub business rule)."""
        propose_result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "propose",
            "obligation_id": accepted_obligation,
            "summary": "Will confirm via MCP",
        })
        cp_id = propose_result.get("checkpoint", {}).get("checkpoint_id")

        confirm_result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "confirm",
            "obligation_id": accepted_obligation,
            "checkpoint_id": cp_id,
            "note": "Confirmed via MCP layer",
        })
        result_str = str(confirm_result).lower()
        is_success = "error" not in confirm_result
        is_forbidden = "cannot confirm" in result_str or confirm_result.get("error", "").startswith("Hub returned 403")
        assert is_success or is_forbidden, f"Expected confirmed or 403, got: {confirm_result}"

    def test_checkpoint_reject_via_mcp(self, accepted_obligation):
        """manage_obligation_checkpoint(action=reject) → rejected or 403 (Hub business rule)."""
        propose_result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "propose",
            "obligation_id": accepted_obligation,
            "summary": "Will reject via MCP",
        })
        cp_id = propose_result.get("checkpoint", {}).get("checkpoint_id")

        reject_result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "reject",
            "obligation_id": accepted_obligation,
            "checkpoint_id": cp_id,
            "reason": f"MCP layer rejection — {uuid.uuid4().hex[:8]}",
        })
        result_str = str(reject_result).lower()
        is_success = "error" not in reject_result
        is_forbidden = "cannot confirm" in result_str or reject_result.get("error", "").startswith("Hub returned 403")
        assert is_success or is_forbidden, f"Expected rejected or 403, got: {reject_result}"

    def test_checkpoint_invalid_action_via_mcp(self, accepted_obligation):
        """Invalid action → error dict."""
        result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "not-a-real-action",
            "obligation_id": accepted_obligation,
            "summary": "Should fail",
        })
        assert "error" in result, f"Expected error for invalid action: {result}"

    def test_checkpoint_missing_summary_via_mcp(self, accepted_obligation):
        """Empty summary → error dict."""
        result = _call_mcp_tool("manage_obligation_checkpoint", {
            "action": "propose",
            "obligation_id": accepted_obligation,
            "summary": "",
        })
        assert "error" in result, f"Expected error for empty summary: {result}"


class TestAdvanceMCP:
    """Test advance_obligation_status via MCP HTTP endpoint."""

    def test_advance_via_mcp_full_lifecycle(self, test_obligation):
        """advance_obligation_status: proposed→accepted→evidence_submitted via MCP."""
        accept_result = _call_mcp_tool("advance_obligation_status", {
            "obligation_id": test_obligation,
            "status": "accepted",
            "binding_scope_text": f"Test obligation {test_obligation}",
        })
        assert "error" not in accept_result, f"Accept failed: {accept_result}"
        assert accept_result.get("obligation", accept_result).get("status") == "accepted"

        evidence_result = _call_mcp_tool("advance_obligation_status", {
            "obligation_id": test_obligation,
            "status": "evidence_submitted",
            "note": "MCP layer test — evidence submitted",
        })
        assert "error" not in evidence_result, f"Evidence submit failed: {evidence_result}"
        assert evidence_result.get("obligation", evidence_result).get("status") == "evidence_submitted"

        resolve_result = _call_mcp_tool("advance_obligation_status", {
            "obligation_id": test_obligation,
            "status": "resolved",
            "note": "MCP layer test complete",
        })
        # Resolve may need evidence_refs (Hub business rule). Accept both success and error.
        if "error" in str(resolve_result):
            assert "evidence_refs" in str(resolve_result), \
                f"Unexpected resolve error: {resolve_result}"

    def test_advance_invalid_transition_via_mcp(self, test_obligation):
        """proposed→resolved via MCP → error dict with conflict info."""
        result = _call_mcp_tool("advance_obligation_status", {
            "obligation_id": test_obligation,
            "status": "resolved",
            "note": "Should fail",
        })
        assert "error" in result
        error_str = str(result.get("error", "")).lower()
        assert any(kw in error_str for kw in ["409", "conflict", "invalid", "transition"]), \
            f"Expected conflict error: {result}"

    def test_advance_missing_status_via_mcp(self, test_obligation):
        """Empty status → error dict."""
        result = _call_mcp_tool("advance_obligation_status", {
            "obligation_id": test_obligation,
            "status": "",
        })
        assert "error" in result, f"Expected error for empty status: {result}"


class TestStatusCardMCP:
    """Test get_obligation_status_card via MCP HTTP endpoint."""

    def test_status_card_returns_card_with_suggested_action(self, test_obligation):
        """Status card has suggested_action populated for the requesting agent."""
        result = _call_mcp_tool("get_obligation_status_card", {
            "obligation_id": test_obligation,
            "agent_id": AGENT_ID,
        })
        assert "error" not in result, f"Expected no error: {result}"
        card = result.get("status_card", result)
        assert any(k in card for k in ["obligation_id", "status", "suggested_action"]), \
            f"Expected status card fields: {list(card.keys())}"

    def test_status_card_nonexistent_via_mcp(self):
        """Nonexistent obligation → error dict."""
        fake = f"obl-nonexistent-{uuid.uuid4().hex[:12]}"
        result = _call_mcp_tool("get_obligation_status_card", {"obligation_id": fake})
        assert "error" in result, f"Expected error for nonexistent: {result}"


class TestObligationStatusMCP:
    """Test obligation_status via MCP HTTP endpoint."""

    def test_obligation_status_returns_full_object(self, test_obligation):
        """obligation_status returns full object: obligation_id + status + history + parties."""
        result = _call_mcp_tool("obligation_status", {"obligation_id": test_obligation})
        assert "error" not in result, f"Expected no error: {result}"
        obl = result.get("obligation", result)
        assert obl.get("obligation_id") == test_obligation
        assert "history" in obl
        assert "parties" in obl
        assert "status" in obl


# ══════════════════════════════════════════════════════════════════════════════
# Cross-tool integration
# ══════════════════════════════════════════════════════════════════════════════

def test_checkpoint_advance_status_card_resolve_flow(accepted_obligation):
    """
    Full cross-tool flow via MCP HTTP endpoint:
    checkpoint_propose → checkpoint_confirm → advance(evidence_submitted) →
    get_obligation_status_card → advance(resolved)

    Uses _call_mcp_tool for all tool calls (real MCP HTTP transport).
    """
    # 1. checkpoint_propose
    propose_result = _call_mcp_tool("checkpoint_propose", {
        "obligation_id": accepted_obligation,
        "summary": "Milestone checkpoint",
        "scope_update": "Scope confirmed",
    })
    assert "error" not in propose_result, f"Propose failed: {propose_result}"
    cp_id = propose_result.get("checkpoint", propose_result).get("checkpoint_id")

    # 2. checkpoint_confirm (Hub may return 403 — accept both)
    confirm_result = _call_mcp_tool("checkpoint_confirm", {
        "obligation_id": accepted_obligation,
        "checkpoint_id": cp_id,
        "note": "Milestone confirmed",
    })
    result_str = str(confirm_result).lower()
    is_success = "error" not in confirm_result
    is_forbidden = "cannot confirm" in result_str or confirm_result.get("error", "").startswith("Hub returned 403")
    assert is_success or is_forbidden, f"Confirm failed unexpectedly: {confirm_result}"

    # 3. advance to evidence_submitted
    adv_result = _call_mcp_tool("advance_obligation_status", {
        "obligation_id": accepted_obligation,
        "status": "evidence_submitted",
        "note": "Milestone reached",
        "binding_scope_text": f"Obligation {accepted_obligation}",
    })
    assert "error" not in adv_result, f"Advance failed: {adv_result}"
    assert adv_result.get("obligation", adv_result).get("status") == "evidence_submitted"

    # 4. status card reflects evidence_submitted state
    card_result = _call_mcp_tool("get_obligation_status_card", {
        "obligation_id": accepted_obligation,
    })
    assert "error" not in card_result, f"Status card failed: {card_result}"

    # 5. resolve (may need evidence_refs — accept either outcome)
    resolve_result = _call_mcp_tool("advance_obligation_status", {
        "obligation_id": accepted_obligation,
        "status": "resolved",
        "note": "Integration test complete",
    })
    result_str = str(resolve_result).lower()
    is_resolved = "error" not in resolve_result and resolve_result.get("obligation", {}).get("status") == "resolved"
    needs_evidence = "evidence_refs" in result_str
    assert is_resolved or needs_evidence, f"Unexpected resolve result: {resolve_result}"


# ══════════════════════════════════════════════════════════════════════════════
# Summary
# ══════════════════════════════════════════════════════════════════════════════

def test_summary(hub_health):
    print(f"\n\n{'='*58}")
    print("Hub MCP Integration Tests — Summary")
    print(f"{'='*58}")
    print(f" Hub REST : {BASE_URL}")
    print(f" MCP server: {MCP_URL}")
    print(f" Hub ver : {hub_health.get('version', 'unknown')}")
    print(f" Agent ID : {AGENT_ID}")
    print(f" Coverage :")
    print(f" Layer 1 (REST): checkpoint + advance HTTP status codes")
    print(f" Layer 2 (MCP): tool functions via MCP HTTP JSON-RPC")
    print(f" manage_obligation_checkpoint (propose / confirm / reject)")
    print(f" advance_obligation_status (lifecycle + error paths)")
    print(f" get_obligation_status_card (compact card)")
    print(f" obligation_status (full object)")
    print(f" Cross-tool integration flow")
    print(f"{'='*58}\n")
