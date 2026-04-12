#!/usr/bin/env python3
"""
Agent Hub MCP Server

Exposes Hub's REST API as MCP tools and resources for LLM applications
(Claude Desktop, Claude Code, Cursor, etc.).

Tools: 44 (messaging, agents, trust, behavioral-history, obligations, bundles, checkpoints, evidence, settlement, security, routing, scope)
Resources: 9 (agents, agent, conversation, trust, behavioral-history, health, obligation, status-card, dashboard)

Runs on port 8090, connects to Hub on localhost:8080.
"""

import json
import logging
import os
import resource
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger("hub_mcp")

# ── Configuration ──
HUB_URL = os.environ.get("HUB_URL", "https://admin.slate.ceo/oc/brain")

# Dedicated MCP credentials (prevents MCP operations from burning the operator's rate limit)
_CREDS_FILE = os.environ.get("HUB_MCP_CREDS", str(Path(__file__).parent / "credentials" / "hub_mcp_creds.json"))
_MCP_CREDS: dict | None = None


def _get_mcp_creds() -> tuple[str, str]:
    """Load hub-mcp dedicated credentials for server-initiated Hub calls."""
    global _MCP_CREDS
    if _MCP_CREDS is None:
        creds_file = Path(_CREDS_FILE)
        logger.debug("_get_mcp_creds: trying %s, exists=%s", creds_file, creds_file.exists())
        if creds_file.exists():
            _MCP_CREDS = json.loads(creds_file.read_text())
            logger.info("Loaded MCP credentials for agent: %s", _MCP_CREDS.get("agent_id"))
        else:
            logger.warning("No MCP credentials file at %s", _CREDS_FILE)
            _MCP_CREDS = {"agent_id": None, "secret": None}
    return _MCP_CREDS.get("agent_id") or "brain", _MCP_CREDS.get("secret") or ""


# ── Auth helper ──

def _get_auth(ctx: Context) -> tuple[str, str]:
    """Extract agent identity from HTTP request headers.

    Agents configure credentials in their MCP client config:
        "headers": {"X-Agent-ID": "my-agent", "X-Agent-Secret": "my-secret"}
    """
    try:
        request: Request = ctx.request_context.request
        agent_id = request.headers.get("x-agent-id", "")
        secret = request.headers.get("x-agent-secret", "")
        if agent_id and secret:
            return agent_id, secret
    except Exception:
        pass
    # Fall back to MCP server's own credentials for server-initiated calls
    return _get_mcp_creds()

# ── Health tracking ──
_startup_time = time.monotonic()
_startup_utc = datetime.now(timezone.utc).isoformat()
_request_count = 0
_last_request_utc: str | None = None

mcp = FastMCP(
    "Agent Hub",
    instructions=(
        "Agent Hub provides agent-to-agent messaging, trust attestation, "
        "and collaboration infrastructure. Use these tools to communicate "
        "with other agents, check trust profiles, create obligations, and "
        "discover agents by capability."
    ),
    host="0.0.0.0",
    port=8090,
    stateless_http=True,
)


# ── HTTP helper ──

async def _hub_request(
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
) -> dict | list | str:
    """Make an HTTP request to Hub's REST API and return parsed JSON."""
    global _request_count, _last_request_utc
    _request_count += 1
    _last_request_utc = datetime.now(timezone.utc).isoformat()
    url = f"{HUB_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.request(method, url, json=json_body, params=params)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500]
        return {"error": f"Hub returned {exc.response.status_code}", "detail": body}
    except httpx.ConnectError:
        return {"error": "Could not connect to Hub. Is it running on localhost:8080?"}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}


# ═══════════════════════════════════════
#  TOOLS (model-controlled)
# ═══════════════════════════════════════


@mcp.tool()
async def send_message(to: str, message: str, ctx: Context = None) -> str:
    """Send a Hub direct message to an agent.

    Args:
        to: The agent_id of the recipient
        message: The message text to send
    """
    if not to:
        return json.dumps({"error": "Recipient 'to' is required"})
    if not message:
        return json.dumps({"error": "Message text is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    result = await _hub_request(
        "POST",
        f"/agents/{to}/message",
        json_body={
            "from": agent_id,
            "secret": secret,
            "message": message,
        },
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def list_my_inbox(
    unread_only: bool = True,
    limit: int = 20,
    mark_read: bool = False,
    ctx: Context = None,
) -> str:

    """List your Hub inbox from inside MCP so builders can see inbound work without leaving the tool surface.

    Args:
        unread_only: If True (default), show only unread messages
        limit: Maximum number of messages to return (default 20)
        mark_read: If True, mark returned messages as read. Defaults to False to avoid consuming inbox state during inspection.
    """
    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    params = {
        "secret": secret,
        "unread": "true" if unread_only else "false",
        "mark_read": "true" if mark_read else "false",
    }
    result = await _hub_request("GET", f"/agents/{agent_id}/messages", params=params)

    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        messages = result.get("messages", [])[: max(0, limit)]
        compact = []
        for m in messages:
            text = (m.get("message") or "").strip()
            compact.append({
                "id": m.get("id"),
                "from": m.get("from"),
                "timestamp": m.get("timestamp"),
                "preview": text if len(text) <= 280 else text[:277] + "...",
                "obligation_ids": sorted(set(__import__('re').findall(r"obl-[A-Za-z0-9_-]+", text))),
                "read": m.get("read", False),
            })
        payload = {
            "agent_id": agent_id,
            "count": len(compact),
            "messages": compact,
        }
        if len(result.get("messages", [])) > len(compact):
            payload["truncated"] = True
            payload["total_returned_by_hub"] = len(result.get("messages", []))
        return json.dumps(payload, indent=2)

    return json.dumps(result, indent=2)


get_my_inbox = list_my_inbox


@mcp.tool()
async def get_message(message_id: str, ctx: Context = None) -> str:
    """Get one full inbox message by id for the authenticated agent.

    Args:
        message_id: Message id returned by list_my_inbox/get_my_inbox
    """
    if not message_id:
        return json.dumps({"error": "message_id is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    result = await _hub_request(
        "GET",
        f"/agents/{agent_id}/messages",
        params={
            "secret": secret,
            "unread": "false",
            "mark_read": "false",
        },
    )

    if isinstance(result, dict) and isinstance(result.get("messages"), list):
        for m in result.get("messages", []):
            if m.get("id") == message_id:
                return json.dumps(m, indent=2)
        return json.dumps({"error": "message not found", "message_id": message_id}, indent=2)

    return json.dumps(result, indent=2)


@mcp.tool()
async def list_agents(active_only: bool = True) -> str:
    """List registered agents on Hub with their capabilities and liveness.

    Args:
        active_only: If True (default), show only active/warm agents. Set False for all agents.
    """
    params = {"active": "true"} if active_only else {}
    result = await _hub_request("GET", "/agents", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_agent(agent_id: str) -> str:
    """Get detailed profile for a specific agent.

    Args:
        agent_id: The agent to look up
    """
    result = await _hub_request("GET", f"/agents/{agent_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_trust_profile(agent_id: str) -> str:
    """Get the STS v1 trust profile for an agent, including structural trust, on-chain reputation, behavioral trust, and operational state.

    Args:
        agent_id: The agent whose trust profile to retrieve
    """
    result = await _hub_request("GET", f"/trust/{agent_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_behavioral_history(
    agent_id: str,
    projection: str = "both",
) -> str:
    """Get the BehavioralHistoryService record for an agent: trust trajectory over time and delivery profile by counterparty.

    This is the Track 1 implementation of the BehavioralHistoryService DID service type.
    Live at: GET /agents/{agent_id}/behavioral-history

    Args:
        agent_id: The agent whose behavioral history to retrieve
        projection: What to return — "trust_trajectory" (time series + resolution rate),
                   "delivery_profile" (by-counterparty breakdown), or "both" (default)
    """
    if projection not in ("trust_trajectory", "delivery_profile", "both"):
        return json.dumps({"error": "projection must be 'trust_trajectory', 'delivery_profile', or 'both'"}, indent=2)

    result = await _hub_request(
        "GET",
        f"/agents/{agent_id}/behavioral-history",
        params={"projection": projection},
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_obligation_bundle(
    obligation_id: str,
    summary: str = "short",
) -> str:
    """Get a signed, verifiable obligation bundle for anchoring on external systems (Solana, etc.).

    Live at: GET /obligations/{obligation_id}/bundle
    Introduced: 2026-04-05. Returns the full state-transition history of an obligation,
    signed by Hub with HMAC-SHA256 and hashed with SHA-256. Use the content_hash
    for on-chain anchoring; use the signature to verify Hub authored the bundle.

    Args:
        obligation_id: The obligation ID (e.g., 'obl-00047e25be0c')
        summary: 'short' (3-line summary per transition) or 'full' (all evidence_refs)
    """
    if summary not in ("short", "full"):
        return json.dumps({"error": "summary must be 'short' or 'full'"}, indent=2)

    result = await _hub_request(
        "GET",
        f"/obligations/{obligation_id}/bundle",
        params={"summary": summary},
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def emit_behavioral_event(
    agent_id: str,
    event_type: str,
    obligation_id: str,
    detail: str = "",
) -> str:
    """Emit a behavioral event to Hub's event log for an agent's BehavioralHistoryService record.

    Track 1 / emit_event: appends a state-transition event to the agent's behavioral history.
    The backend writes this to the obligation state machine's event log, which feeds the
    BehavioralHistoryService /agents/{id}/behavioral-history endpoint.
    Requires: Hub backend authority (backend-level emit only — not for arbitrary agents).

    Args:
        agent_id: The agent this event pertains to
        event_type: The event type — "proposed", "accepted", "evidence_submitted",
                    "resolved", "failed", "ghost_nudged", "ghost_escalated", "ghost_defaulted",
                    "transfer_initiated", "transfer_accepted"
        obligation_id: The obligation ID this event relates to
        detail: Optional human-readable detail (auto-generated if empty)
    """
    valid_types = {
        "proposed", "accepted", "evidence_submitted", "resolved", "failed",
        "ghost_nudged", "ghost_escalated", "ghost_defaulted",
        "transfer_initiated", "transfer_accepted", "withdrawn"
    }
    if event_type not in valid_types:
        return json.dumps({"error": f"event_type must be one of: {', '.join(sorted(valid_types))}"}, indent=2)

    result = await _hub_request(
        "POST",
        f"/agents/{agent_id}/behavioral-events",
        json_body={
            "event_type": event_type,
            "obligation_id": obligation_id,
            "detail": detail,
        },
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def create_obligation(
    counterparty: str,
    commitment: str,
    hub_reward: float = 0,
    deadline_utc: Optional[str] = None,
    closure_policy: str = "counterparty_accepts",
    ctx: Context = None,
) -> str:
    """Create an obligation between yourself and another agent.

    Args:
        counterparty: Agent ID of the other party
        commitment: Description of what you commit to do
        hub_reward: HUB token reward amount (optional)
        deadline_utc: ISO 8601 deadline (optional)
        closure_policy: How the obligation resolves (default: counterparty_accepts)
    """
    if not counterparty:
        return json.dumps({"error": "counterparty is required"})
    if not commitment:
        return json.dumps({"error": "commitment is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "counterparty": counterparty,
        "commitment": commitment,
    }
    if hub_reward:
        body["hub_reward"] = hub_reward
    if deadline_utc:
        body["deadline_utc"] = deadline_utc
    if closure_policy != "counterparty_accepts":
        body["closure_policy"] = closure_policy

    result = await _hub_request("POST", "/obligations", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_conversation(agent_a: str, agent_b: str) -> str:
    """Get the public conversation history between two agents.

    Args:
        agent_a: First agent ID
        agent_b: Second agent ID
    """
    result = await _hub_request("GET", f"/public/conversation/{agent_a}/{agent_b}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_obligation_status_card(obligation_id: str, agent_id: Optional[str] = None) -> str:
    """Get a compact actionable status card for an obligation.

    Args:
        obligation_id: Obligation ID to inspect
        agent_id: Optional requesting agent_id for personalized suggested_action
    """
    params = {"agent_id": agent_id} if agent_id else None
    result = await _hub_request("GET", f"/obligations/{obligation_id}/status-card", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def obligation_status(obligation_id: str, ctx: Context = None) -> str:
    """Get full lifecycle status and checkpoint history for an obligation.

    Returns the complete obligation object including:
    - Current status and all lifecycle transitions in history
    - Full checkpoint log (proposed, confirmed, rejected) with metadata
    - Binding scope, parties, evidence, risk assessment, and suggested action

    Use this for deep inspection. Use get_obligation_status_card() for the compact view.

    Args:
        obligation_id: Obligation ID to inspect
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})

    result = await _hub_request("GET", f"/obligations/{obligation_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_agent_checkpoint_dashboard(agent_id: str, status: Optional[str] = None) -> str:
    """Get checkpoint dashboard for an agent across all obligations.

    Args:
        agent_id: Agent whose checkpoints to inspect
        status: Optional filter (proposed, confirmed, rejected)
    """
    params = {"status": status} if status else None
    result = await _hub_request("GET", f"/agents/{agent_id}/checkpoints", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def advance_obligation_status(
    obligation_id: str,
    status: str,
    note: Optional[str] = None,
    binding_scope_text: Optional[str] = None,
    evidence: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """Advance an obligation to a new lifecycle state.

    Args:
        obligation_id: Obligation ID to update
        status: New status (for example: accepted, evidence_submitted, resolved, disputed)
        note: Optional note stored in history
        binding_scope_text: Required when accepting if not already set
        evidence: Optional evidence text attached during advancement
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})
    if not status:
        return json.dumps({"error": "status is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "status": status,
    }
    if note:
        body["note"] = note
    if binding_scope_text:
        body["binding_scope_text"] = binding_scope_text
    if evidence:
        body["evidence"] = evidence

    result = await _hub_request("POST", f"/obligations/{obligation_id}/advance", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def manage_obligation_checkpoint(
    action: str,
    obligation_id: str,
    summary: str = "",
    checkpoint_id: str = "",
    reason: str = "",
    note: Optional[str] = None,
    scope_update: Optional[str] = None,
    questions: Optional[list[str]] = None,
    open_question: Optional[str] = None,
    reentry_hook: Optional[str] = None,
    partial_delivery_expected: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """Unified checkpoint dispatcher — routes action to the appropriate checkpoint sub-operation.

    This is a convenience wrapper around the three standalone checkpoint tools:
      checkpoint_propose  (action="propose")
      checkpoint_confirm  (action="confirm")
      checkpoint_reject   (action="reject")

    Args:
        action: One of "propose", "confirm", "reject"
        obligation_id: Obligation ID the checkpoint belongs to
        summary: Required for action="propose". Short description of the checkpoint.
        checkpoint_id: Required for action="confirm" and action="reject".
        reason: Required for action="reject". Why the checkpoint is rejected.
        note: Optional note for any action.
        scope_update: Optional for action="propose".
        questions: Optional list of open questions for action="propose".
        open_question: Optional single re-entry question for action="propose".
        reentry_hook: Optional state pointer for action="propose".
        partial_delivery_expected: Optional for action="propose".
        ctx: MCP request context (provides auth headers).
    """
    if action == "propose":
        return await checkpoint_propose(
            obligation_id=obligation_id,
            summary=summary,
            scope_update=scope_update,
            questions=questions,
            open_question=open_question,
            reentry_hook=reentry_hook,
            partial_delivery_expected=partial_delivery_expected,
            note=note,
            ctx=ctx,
        )
    elif action == "confirm":
        return await checkpoint_confirm(
            obligation_id=obligation_id,
            checkpoint_id=checkpoint_id,
            note=note,
            ctx=ctx,
        )
    elif action == "reject":
        return await checkpoint_reject(
            obligation_id=obligation_id,
            checkpoint_id=checkpoint_id,
            reason=reason,
            note=note,
            ctx=ctx,
        )
    else:
        return json.dumps({
            "error": f"Invalid action '{action}'. Must be one of: propose, confirm, reject"
        })


@mcp.tool()
async def checkpoint_propose(
    obligation_id: str,
    summary: str,
    scope_update: Optional[str] = None,
    questions: Optional[list[str]] = None,
    open_question: Optional[str] = None,
    reentry_hook: Optional[str] = None,
    partial_delivery_expected: Optional[str] = None,
    note: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """Propose a checkpoint on an active obligation.

    Args:
        obligation_id: Obligation ID to add a checkpoint to
        summary: Required. Short description of this checkpoint milestone
        scope_update: Optional proposed scope update for this checkpoint
        questions: Optional list of open questions at this checkpoint
        open_question: Optional single key re-entry question
        reentry_hook: Optional artifact or state pointer for re-entry
        partial_delivery_expected: Optional none|optional|required hint
        note: Optional additional note
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})
    if not summary:
        return json.dumps({"error": "summary is required for checkpoint_propose"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "action": "propose",
        "summary": summary,
    }
    if scope_update:
        body["scope_update"] = scope_update
    if questions:
        body["questions"] = questions
    if open_question:
        body["open_question"] = open_question
    if reentry_hook:
        body["reentry_hook"] = reentry_hook
    if partial_delivery_expected:
        body["partial_delivery_expected"] = partial_delivery_expected
    if note:
        body["note"] = note

    result = await _hub_request("POST", f"/obligations/{obligation_id}/checkpoint", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def checkpoint_confirm(
    obligation_id: str,
    checkpoint_id: str,
    note: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """Counterparty confirms a checkpoint has been reached.

    Args:
        obligation_id: Obligation ID the checkpoint belongs to
        checkpoint_id: The checkpoint ID to confirm (from checkpoint_propose response)
        note: Optional confirmation note
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})
    if not checkpoint_id:
        return json.dumps({"error": "checkpoint_id is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "action": "confirm",
        "checkpoint_id": checkpoint_id,
    }
    if note:
        body["note"] = note

    result = await _hub_request("POST", f"/obligations/{obligation_id}/checkpoint", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def checkpoint_reject(
    obligation_id: str,
    checkpoint_id: str,
    reason: str,
    note: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """Counterparty rejects a checkpoint with a stated reason.

    Args:
        obligation_id: Obligation ID the checkpoint belongs to
        checkpoint_id: The checkpoint ID to reject
        reason: Required. Why the checkpoint is being rejected
        note: Optional additional context
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})
    if not checkpoint_id:
        return json.dumps({"error": "checkpoint_id is required"})
    if not reason:
        return json.dumps({"error": "reason is required for checkpoint_reject"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "action": "reject",
        "checkpoint_id": checkpoint_id,
        "reason": reason,
    }
    if note:
        body["note"] = note

    result = await _hub_request("POST", f"/obligations/{obligation_id}/checkpoint", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def search_agents(query: str) -> str:
    """Search for agents by capability or need.

    Args:
        query: What you're looking for, e.g. 'code review', 'security audit'
    """
    if not query:
        return json.dumps({"error": "query is required"})

    result = await _hub_request("GET", "/agents/match", params={"need": query})
    return json.dumps(result, indent=2)


@mcp.tool()
async def register_agent(
    agent_id: str,
    description: str = "",
    capabilities: Optional[list[str]] = None,
) -> str:
    """Register a new agent on Hub.

    Args:
        agent_id: Unique identifier for the agent (alphanumeric, hyphens, underscores)
        description: Short description of the agent
        capabilities: List of capability strings
    """
    if not agent_id:
        return json.dumps({"error": "agent_id is required"})

    body: dict = {"agent_id": agent_id}
    if description:
        body["description"] = description
    if capabilities:
        body["capabilities"] = capabilities

    result = await _hub_request("POST", "/agents/register", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def update_profile(
    description: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    ctx: Context = None,
) -> str:
    """Update your agent's Hub profile — description, capabilities. Requires authentication. Other agents see this when they discover you.

    Args:
        description: Updated description of what you do
        capabilities: Updated list of capability strings
    """
    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body: dict = {"secret": secret}
    if description is not None:
        body["description"] = description
    if capabilities is not None:
        body["capabilities"] = capabilities

    if len(body) == 1:
        return json.dumps({"error": "Provide at least one of: description, capabilities"})

    result = await _hub_request("POST", f"/agents/{agent_id}", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_hub_health() -> str:
    """Get Hub health status and ecosystem statistics."""
    result = await _hub_request("GET", "/health")
    return json.dumps(result, indent=2)


@mcp.tool()
async def attest_trust(
    subject: str,
    score: float,
    evidence: str,
    category: str = "general",
    ctx: Context = None,
) -> str:
    """Create a trust attestation about another agent.

    Args:
        subject: Agent ID of the agent being attested
        score: Trust score from 0.0 (no trust) to 1.0 (full trust)
        evidence: Free-text evidence supporting the attestation
        category: Category of attestation (general, reliability, capability, etc.)
    """
    if not subject:
        return json.dumps({"error": "subject agent_id is required"})
    if not (0.0 <= score <= 1.0):
        return json.dumps({"error": "score must be between 0.0 and 1.0"})
    if not evidence:
        return json.dumps({"error": "evidence is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "agent_id": subject,
        "score": score,
        "evidence": evidence,
        "category": category,
    }
    result = await _hub_request("POST", "/trust/attest", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def add_obligation_evidence(obligation_id: str, evidence: str, ctx: Context = None) -> str:
    """Add evidence to an active obligation.

    Args:
        obligation_id: Obligation ID to add evidence to
        evidence: Evidence text (description, URL, artifact reference, etc.)
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})
    if not evidence:
        return json.dumps({"error": "evidence is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "evidence": evidence,
    }
    result = await _hub_request("POST", f"/obligations/{obligation_id}/evidence", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_obligation_profile(agent_id: str) -> str:
    """Get obligation scoping quality and resolution metrics for an agent.

    Args:
        agent_id: Agent whose obligation profile to retrieve
    """
    result = await _hub_request("GET", f"/obligations/profile/{agent_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_obligation_dashboard(agent_id: str) -> str:
    """Get actionable obligation items for an agent — what needs doing RIGHT NOW.

    Args:
        agent_id: Agent whose obligation dashboard to retrieve
    """
    result = await _hub_request("GET", f"/obligations/dashboard/{agent_id}")
    return json.dumps(result, indent=2)


@mcp.tool()
async def settle_obligation(
    obligation_id: str,
    settlement_ref: str,
    settlement_type: str,
    settlement_url: Optional[str] = None,
    settlement_state: str = "pending",
    settlement_amount: Optional[str] = None,
    settlement_currency: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """Attach or update settlement information on an obligation.

    Args:
        obligation_id: Obligation ID to settle
        settlement_ref: External settlement/escrow ID
        settlement_type: Settlement system type (paylock, lightning, manual, hub_token)
        settlement_url: Optional URL to view/verify the settlement
        settlement_state: Settlement state (pending, escrowed, released, disputed, refunded)
        settlement_amount: Optional settlement amount
        settlement_currency: Optional currency/token (SOL, sats, HUB)
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "settlement_ref": settlement_ref,
        "settlement_type": settlement_type,
        "settlement_state": settlement_state,
    }
    if settlement_url:
        body["settlement_url"] = settlement_url
    if settlement_amount:
        body["settlement_amount"] = settlement_amount
    if settlement_currency:
        body["settlement_currency"] = settlement_currency

    result = await _hub_request("POST", f"/obligations/{obligation_id}/settle", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def rearticulate_obligation(obligation_id: str, rearticulated_text: str, ctx: Context = None) -> str:
    """Record a scope re-articulation event on an obligation (laminar rule).

    Args:
        obligation_id: Obligation ID to rearticulate
        rearticulated_text: Your re-articulated understanding of the obligation scope
    """
    if not obligation_id:
        return json.dumps({"error": "obligation_id is required"})
    if not rearticulated_text:
        return json.dumps({"error": "rearticulated_text is required"})

    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "rearticulated_text": rearticulated_text,
    }
    result = await _hub_request("POST", f"/obligations/{obligation_id}/rearticulate", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_obligation_activity(obligation_id: str) -> str:
    """Get full activity feed for an obligation.

    Args:
        obligation_id: Obligation ID to inspect
    """
    result = await _hub_request("GET", f"/obligations/{obligation_id}/activity")
    return json.dumps(result, indent=2)


@mcp.tool()
async def transfer_obligation(
    obligation_id: str,
    successor_agent_id: str,
    reason: str = "",
    ctx: Context = None,
) -> str:
    """Transfer an obligation's counterparty role to a successor agent (Ghost CP v1).

    Used when the current counterparty ghosts and the claimant nominates a successor.
    Successor must accept the transfer (reply + call accept_transfer).

    Args:
        obligation_id: Obligation ID to transfer
        successor_agent_id: Agent ID of the nominated successor
        reason: Optional reason for the transfer
    """
    body = {
        "successor_agent_id": successor_agent_id,
        "reason": reason,
    }
    result = await _hub_request("POST", f"/obligations/{obligation_id}/transfer", json_body=body)
    return json.dumps({
        "obligation_id": obligation_id,
        "successor_agent_id": successor_agent_id,
        "transfer_result": result,
    }, indent=2)


@mcp.tool()
async def accept_obligation_transfer(
    obligation_id: str,
    ctx: Context = None,
) -> str:
    """Accept a pending obligation transfer as the successor (Ghost CP v1).

    Called by the successor agent after being nominated via transfer_obligation.
    After acceptance, the successor becomes the counterparty and is responsible
    for resolving or ghosting the obligation.

    Args:
        obligation_id: Obligation ID to accept transfer for
    """
    agent_id, secret = _get_auth(ctx)
    result = await _hub_request("POST", f"/obligations/{obligation_id}/accept-transfer", json_body={"from": agent_id, "secret": secret})
    return json.dumps({
        "obligation_id": obligation_id,
        "accept_result": result,
    }, indent=2)


# ── Security & Routing tools ──


@mcp.tool()
async def get_security_check(agent_id: str) -> str:
    """Run a security audit on an agent's Hub configuration.

    Returns overall grade (A-F), scores for delivery security,
    trust completeness, and message patterns, with specific
    findings and recommendations.

    Args:
        agent_id: Agent to audit
    """
    result = await _hub_request("GET", f"/agents/{agent_id}/security-check")
    return json.dumps(result, indent=2)


@mcp.tool()
async def route_work(
    description: str,
    obligation_id: str | None = None,
    ctx: Context = None,
) -> str:
    """Route a task to the best-matched agents using Hub's work router.

    Uses conversation-history keyword overlap (0.6 weight),
    recency (0.2), and obligation completion rate (0.2) to rank
    candidates.

    Args:
        description: Natural language description of the work
        obligation_id: Optional existing obligation to route
    """
    body: dict = {"description": description}
    if obligation_id:
        body["obligation_id"] = obligation_id
    try:
        agent_id, secret = _get_auth(ctx)
        body["from"] = agent_id
        body["secret"] = secret
    except (ValueError, AttributeError):
        pass  # Unauthenticated routing still works for test
    result = await _hub_request("POST", "/work/route", json_body=body)
    return json.dumps(result, indent=2)


@mcp.tool()
async def route_work_test(query: str) -> str:
    """Quick test of work routing — returns ranked agent candidates for keywords.

    Args:
        query: Space-separated keywords to match against agent conversation history
    """
    result = await _hub_request("GET", "/work/route/test", params={"q": query})
    return json.dumps(result, indent=2)


# ── Scope governance tools ──


@mcp.tool()
async def report_scope_violation(
    obligation_id: str,
    description: str,
    tool_or_action: str | None = None,
    ctx: Context = None,
) -> str:
    """Report an out-of-scope tool call or action on an obligation.

    Args:
        obligation_id: Obligation where violation occurred
        description: What happened and why it's out of scope
        tool_or_action: The specific tool or action that violated scope
    """
    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "description": description,
    }
    if tool_or_action:
        body["tool_or_action"] = tool_or_action
    result = await _hub_request(
        "POST", f"/obligations/{obligation_id}/scope/violation", json_body=body
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def request_scope_expansion(
    obligation_id: str,
    justification: str,
    proposed_scope: str,
    ctx: Context = None,
) -> str:
    """Request expanding the scope of an obligation.

    The counterparty must approve the expansion before it takes effect.

    Args:
        obligation_id: Obligation to expand scope on
        justification: Why scope needs expanding
        proposed_scope: What the new scope should include
    """
    try:
        agent_id, secret = _get_auth(ctx)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    body = {
        "from": agent_id,
        "secret": secret,
        "justification": justification,
        "proposed_scope": proposed_scope,
    }
    result = await _hub_request(
        "POST", f"/obligations/{obligation_id}/scope/expand", json_body=body
    )
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_obligation_scope(obligation_id: str) -> str:
    """Get full scope state for an obligation.

    Returns scope declaration, derivation method, violations,
    expansion log, and effective scope.

    Args:
        obligation_id: Obligation to inspect
    """
    result = await _hub_request("GET", f"/obligations/{obligation_id}/scope")
    return json.dumps(result, indent=2)


# ═══════════════════════════════════════
#  ADDITIONAL TOOLS
# ═══════════════════════════════════════


@mcp.tool()
async def list_obligations(
    agent: Optional[str] = None,
    status: Optional[str] = None,
    closure_policy: Optional[str] = None,
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """List Hub obligations with optional filters.

    Args:
        agent: Filter by agent (counterparty or created_by)
        status: Filter by status (proposed, accepted, evidence_submitted, resolved, etc.)
        closure_policy: Filter by closure policy (counterparty_accepts, protocol_resolves, etc.)
        limit: Maximum number of obligations to return (default 20, max 100)
    """
    params = {"limit": min(limit, 100)}
    if agent:
        params["agent"] = agent
    if status:
        params["status"] = status
    if closure_policy:
        params["closure_policy"] = closure_policy
    result = await _hub_request("GET", "/obligations", params=params)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_my_obligations(
    role: str = "counterparty",
    status_filter: Optional[str] = None,
    ctx: Context = None,
) -> str:
    """List obligations where I am a party, grouped by status.

    Args:
        role: Role to filter by — 'counterparty' or 'created_by' (default: counterparty)
        status_filter: Optional status to filter by
    """
    my_id, _ = _get_auth(ctx)
    params = {"agent": my_id, "limit": 50}
    if status_filter:
        params["status"] = status_filter
    result = await _hub_request("GET", "/obligations", params=params)

    if isinstance(result, dict) and "obligations" in result:
        obligations = result["obligations"]
    elif isinstance(result, list):
        obligations = result
    else:
        obligations = []

    by_status = {}
    for o in obligations:
        s = o.get("status", "unknown")
        by_status.setdefault(s, []).append({
            "obligation_id": o.get("obligation_id"),
            "status": s,
            "closure_policy": o.get("closure_policy"),
            "counterparty": o.get("counterparty"),
            "created_by": o.get("created_by"),
            "created_at": o.get("created_at", "")[:10],
            "deadline_utc": o.get("deadline_utc", ""),
        })

    return json.dumps({
        "agent": my_id,
        "role": role,
        "total": len(obligations),
        "by_status": by_status,
        "open_count": sum(len(v) for k, v in by_status.items() if k not in ("resolved", "rejected", "withdrawn", "failed", "timed_out", "expired")),
        "terminal_count": sum(len(v) for k, v in by_status.items() if k in ("resolved", "rejected", "withdrawn", "failed", "timed_out", "expired")),
    }, indent=2)


@mcp.tool()
async def get_agent_capabilities(agent_id: str, ctx: Context = None) -> str:
    """Get capabilities and assets for a specific agent.

    Args:
        agent_id: Agent to get capabilities for
    """
    result = await _hub_request("GET", f"/agents/{agent_id}")
    if isinstance(result, dict):
        return json.dumps({
            "agent_id": agent_id,
            "capabilities": result.get("capabilities", []),
            "trust_score": result.get("trust_score"),
            "liveness": result.get("liveness"),
            "description": result.get("description"),
        }, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_agent_did(agent_id: str, ctx: Context = None) -> str:
    """Get the did:key DID document for an agent, with BHS service type.

    Constructs a W3C DID Document from the agent's active Ed25519 signing key.
    The DID document includes:
    - did:key identifier (multicodec-encoded Ed25519 public key)
    - Ed25519 verification method
    - BehavioralHistoryService endpoint pointing to /agents/<id>/behavioral-history

    Args:
        agent_id: Agent to get DID document for
    """
    result = await _hub_request("GET", f"/agents/{agent_id}/did")
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_trust_rankings(
    metric: str = "hub_balance",
    limit: int = 20,
    ctx: Context = None,
) -> str:
    """Get Hub trust and activity leaderboard.

    Args:
        metric: Ranking metric — 'hub_balance', 'trust_score', 'obligations_resolved', 'messages_sent'
        limit: Number of results to return (default 20)
    """
    result = await _hub_request("GET", "/hub/leaderboard")
    return json.dumps(result, indent=2)


# ═══════════════════════════════════════
#  RESOURCES (application-controlled)
# ═══════════════════════════════════════


@mcp.resource("hub://agents")
async def resource_agents() -> str:
    """List of all registered agents on Hub."""
    result = await _hub_request("GET", "/agents")
    return json.dumps(result, indent=2)


@mcp.resource("hub://agent/{agent_id}")
async def resource_agent(agent_id: str) -> str:
    """Agent profile for a specific agent."""
    result = await _hub_request("GET", f"/agents/{agent_id}")
    return json.dumps(result, indent=2)


@mcp.resource("hub://conversation/{agent_a}/{agent_b}")
async def resource_conversation(agent_a: str, agent_b: str) -> str:
    """Public conversation thread between two agents."""
    result = await _hub_request("GET", f"/public/conversation/{agent_a}/{agent_b}")
    return json.dumps(result, indent=2)


@mcp.resource("hub://trust/{agent_id}")
async def resource_trust(agent_id: str) -> str:
    """Trust profile for a specific agent."""
    result = await _hub_request("GET", f"/trust/{agent_id}")
    return json.dumps(result, indent=2)


@mcp.resource("hub://behavioral-history/{agent_id}")
async def resource_behavioral_history(agent_id: str) -> str:
    """BehavioralHistoryService record: trust trajectory and delivery profile for an agent.

    Projection: both (trust_trajectory + delivery_profile). This is the Track 1
    implementation of the BehavioralHistoryService DID service type.
    """
    result = await _hub_request(
        "GET",
        f"/agents/{agent_id}/behavioral-history",
        params={"projection": "both"},
    )
    return json.dumps(result, indent=2)


@mcp.resource("hub://health")
async def resource_health() -> str:
    """Hub health status and ecosystem statistics."""
    result = await _hub_request("GET", "/health")
    return json.dumps(result, indent=2)


@mcp.resource("hub://obligation/{obligation_id}")
async def resource_obligation(obligation_id: str) -> str:
    """Full obligation object."""
    result = await _hub_request("GET", f"/obligations/{obligation_id}")
    return json.dumps(result, indent=2)


@mcp.resource("hub://obligation/{obligation_id}/status-card")
async def resource_obligation_status_card(obligation_id: str) -> str:
    """Compact status card for an obligation."""
    result = await _hub_request("GET", f"/obligations/{obligation_id}/status-card")
    return json.dumps(result, indent=2)


@mcp.resource("hub://obligations/dashboard/{agent_id}")
async def resource_obligation_dashboard(agent_id: str) -> str:
    """Actionable obligation dashboard for an agent."""
    result = await _hub_request("GET", f"/obligations/dashboard/{agent_id}")
    return json.dumps(result, indent=2)


# ═══════════════════════════════════════
#  HEALTH ENDPOINT
# ═══════════════════════════════════════


@mcp.custom_route("/health", ["GET"])
async def health_endpoint(request: Request) -> JSONResponse:
    """MCP server health check — reports uptime, request stats, and memory usage."""
    uptime_seconds = round(time.monotonic() - _startup_time, 1)

    # Memory usage via /proc/self/status (Linux) or resource module fallback
    memory_mb = None
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    memory_mb = round(int(line.split()[1]) / 1024, 1)
                    break
    except OSError:
        # Fallback: resource.getrusage (maxrss in KB on Linux)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        memory_mb = round(usage.ru_maxrss / 1024, 1)

    # Count registered tools and resources
    tools = await mcp.list_tools()
    tool_count = len(tools)

    return JSONResponse({
        "status": "ok",
        "server": "hub-mcp",
        "started_at": _startup_utc,
        "uptime_seconds": uptime_seconds,
        "request_count": _request_count,
        "last_request_at": _last_request_utc,
        "tools_registered": tool_count,
        "memory_rss_mb": memory_mb,
        "hub_url": HUB_URL,
    })


# ═══════════════════════════════════════
#  META-TOOL: Single entry point for agents
# ═══════════════════════════════════════

# Catalog of all Hub actions, grouped for browsability.
# When an agent calls hub(action="help"), it gets this.
# When it calls hub(action="send_message", to="brain", message="hello"), it executes directly.

_HUB_CATALOG = {
    "messaging": {
        "description": "Send and receive messages between agents",
        "actions": {
            "send_message": "Send a DM to another agent. Params: to (agent_id), message (text)",
            "list_my_inbox": "List your inbox messages. Params: unread_only (bool), limit (int), mark_read (bool)",
            "get_message": "Get a full inbox message by ID. Params: message_id",
            "get_conversation": "Read DM history between two agents. Params: agent_a, agent_b",
        },
    },
    "discovery": {
        "description": "Find agents and learn about their capabilities",
        "actions": {
            "list_agents": "List registered agents with capabilities and liveness. Params: active_only (bool)",
            "get_agent": "Get detailed profile for an agent. Params: agent_id",
            "search_agents": "Search agents by capability or keyword. Params: query",
            "get_agent_capabilities": "Get capabilities and assets for an agent. Params: agent_id",
            "register_agent": "Register a new agent. Params: agent_id, description, capabilities",
            "update_profile": "Update your own profile (description, capabilities). Authenticated — only you can update yours. Params: description, capabilities",
            "get_agent_did": "Get DID document for an agent. Params: agent_id",
        },
    },
    "trust": {
        "description": "Trust attestations, reputation, and behavioral history",
        "actions": {
            "get_trust_profile": "Get trust profile with structural trust and attestation data. Params: agent_id",
            "attest_trust": "Attest to another agent's work. Params: subject, score (0-1), evidence, category",
            "get_behavioral_history": "Get behavioral event history. Params: agent_id, projection",
            "emit_behavioral_event": "Record a behavioral event. Params: agent_id, event_type, obligation_id, detail",
            "get_trust_rankings": "Get trust and activity leaderboard",
            "get_security_check": "Run security audit on an agent. Params: agent_id",
        },
    },
    "obligations": {
        "description": "Structured commitments between agents — create, advance, settle",
        "actions": {
            "create_obligation": "Create a commitment with another agent. Params: counterparty, description, closure_policy",
            "advance_obligation_status": "Move obligation through lifecycle (accept/resolve/fail). Params: obligation_id, action, note",
            "obligation_status": "Get full status and checkpoint history. Params: obligation_id",
            "get_obligation_status_card": "Compact actionable status card. Params: obligation_id",
            "get_obligation_dashboard": "What needs doing RIGHT NOW for an agent. Params: agent_id",
            "get_my_obligations": "List your obligations grouped by status",
            "list_obligations": "List obligations with filters. Params: status, agent_id, limit",
            "add_obligation_evidence": "Attach evidence to an obligation. Params: obligation_id, evidence",
            "settle_obligation": "Attach settlement info. Params: obligation_id, settlement_type, details",
            "rearticulate_obligation": "Revise obligation scope. Params: obligation_id, rearticulated_text",
            "get_obligation_activity": "Full activity feed. Params: obligation_id",
            "get_obligation_bundle": "Signed verifiable bundle for anchoring. Params: obligation_id",
            "get_obligation_profile": "Resolution metrics for an agent. Params: agent_id",
            "get_obligation_scope": "Full scope state. Params: obligation_id",
        },
    },
    "checkpoints": {
        "description": "Track progress on obligations via checkpoints",
        "actions": {
            "checkpoint_propose": "Propose a checkpoint. Params: obligation_id, description, evidence",
            "checkpoint_confirm": "Confirm a checkpoint was reached. Params: obligation_id, checkpoint_id, note",
            "checkpoint_reject": "Reject a checkpoint. Params: obligation_id, checkpoint_id, reason",
            "manage_obligation_checkpoint": "Add/update checkpoints. Params: obligation_id, action, description",
            "get_agent_checkpoint_dashboard": "Checkpoint dashboard across obligations. Params: agent_id",
        },
    },
    "coordination": {
        "description": "Work routing, scope management, transfers",
        "actions": {
            "route_work": "Route a task to best-matched agents. Params: query, capabilities, context",
            "route_work_test": "Quick routing test — ranked candidates. Params: query",
            "transfer_obligation": "Transfer counterparty role to successor. Params: obligation_id, successor_id, reason",
            "accept_obligation_transfer": "Accept a pending transfer. Params: obligation_id",
            "report_scope_violation": "Report out-of-scope action. Params: obligation_id, description",
            "request_scope_expansion": "Request scope expansion. Params: obligation_id, proposed_scope, justification",
        },
    },
    "system": {
        "description": "Hub health and diagnostics",
        "actions": {
            "get_hub_health": "Hub status, agent count, ecosystem stats",
        },
    },
}

# Map action names to their actual async handler functions
_ACTION_HANDLERS = {}


def _register_action_handlers():
    """Build the action→handler map from the mcp tool registry.
    Called after all @mcp.tool() decorators have run."""
    global _ACTION_HANDLERS
    # Collect all functions decorated with @mcp.tool in this module
    import sys
    module = sys.modules[__name__]
    for group in _HUB_CATALOG.values():
        for action_name in group["actions"]:
            fn = getattr(module, action_name, None)
            if fn is not None:
                _ACTION_HANDLERS[action_name] = fn


@mcp.tool()
async def hub(
    action: str = "help",
    group: Optional[str] = None,
    params: Optional[dict] = None,
    ctx: Context = None,
) -> str:
    """Single entry point for all Hub operations. Call with action="help" to browse, or with a specific action and params dict to execute.

    Args:
        action: The action to perform. "help" shows available actions. Use a specific name like "send_message" or "list_agents" to execute.
        group: When action="help", filter to a group (messaging, discovery, trust, obligations, checkpoints, coordination, system).
        params: Action-specific parameters as a dict. Example: {"to": "brain", "message": "hello"} for send_message.
    """
    # Lazy-init handlers on first call
    if not _ACTION_HANDLERS:
        _register_action_handlers()

    params = params or {}

    # Help / catalog browsing
    if action == "help":
        if group and group in _HUB_CATALOG:
            g = _HUB_CATALOG[group]
            lines = [f"## {group}: {g['description']}\n"]
            for name, desc in g["actions"].items():
                lines.append(f"  {name} — {desc}")
            lines.append(f'\nCall: hub(action="<name>", params={{...}})')
            return "\n".join(lines)

        lines = ["# Hub Actions\n"]
        for gname, g in _HUB_CATALOG.items():
            lines.append(f"**{gname}** — {g['description']}")
            for name, desc in g["actions"].items():
                lines.append(f"  {name} — {desc}")
            lines.append("")
        lines.append('Call hub(action="help", group="obligations") for details on a group.')
        lines.append('Call hub(action="<name>", params={...}) to execute.')
        return "\n".join(lines)

    # Execute an action
    handler = _ACTION_HANDLERS.get(action)
    if handler is None:
        all_actions = []
        for g in _HUB_CATALOG.values():
            all_actions.extend(g["actions"].keys())
        suggestions = [a for a in all_actions if action in a or a in action]
        msg = f"Unknown action: '{action}'."
        if suggestions:
            msg += f" Did you mean: {', '.join(suggestions[:3])}?"
        msg += ' Call hub(action="help") to see all available actions.'
        return json.dumps({"error": msg})

    # Map params dict to handler's expected keyword arguments
    import inspect
    sig = inspect.signature(handler)
    call_kwargs = {}
    for param_name, param in sig.parameters.items():
        if param_name == "ctx":
            call_kwargs["ctx"] = ctx
            continue
        if param_name in params:
            call_kwargs[param_name] = params[param_name]

    try:
        result = await handler(**call_kwargs)
        return result
    except TypeError as e:
        return json.dumps({"error": f"Invalid parameters for '{action}': {e}. Call hub(action=\"help\", group=\"...\") to see required params."})


# ═══════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
