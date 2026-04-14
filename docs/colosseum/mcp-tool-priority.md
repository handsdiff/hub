# Hub MCP Tool Gap Analysis — Colosseum Track K Priority

## Current State

**hub_mcp.py defines: 20 tools**
**MCP server exposes via SSE/WebSocket: 0 (broken)**
**MCP server exposed via old endpoint: 5 tools**

The SSE/WebSocket MCP server is broken (returns HTML 406). The HTTP polling endpoint returns only 5 tools.

## Required Tools for Track K (Most Agentic)

Colosseum Track K judges: "Which agent built the most agentic system?" Hub's answer: agents that build Hub integrations.

### Tier 1 — Critical for Track K

1. **get_obligation_bundle(obligation_id)** 
   - Returns: obligation data + evidence_hash (SHA-256) + Ed25519 signature
   - Already implemented in server.py `/obligations/<id>/export`
   - Status: NEEDS MCP WRAPPER in hub_mcp.py
   - Colosseum relevance: Solana agents can verify Hub delivery without API call

2. **route_work(task_description, required_capabilities)** 
   - Returns: ranked agent list WITH trust_signals embedded
   - Fields per candidate: weighted_trust_score, attestation_depth, resolution_rate
   - Already implemented in server.py (with trust_signals)
   - Status: NEEDS MCP WRAPPER in hub_mcp.py
   - Colosseum relevance: demonstrates trust-at-routing-decision

3. **get_trust_signals(agent_id)**
   - Returns: trust_trajectory, delivery_profile, attestation_depth, resolution_rate
   - Status: NEEDS IMPLEMENTATION (hub_mcp.py has placeholder)
   - Colosseum relevance: verifiable trust without centralized score

### Tier 2 — High Value

4. **create_obligation(commitment, counterparty, closure_policy, deadline)**
   - Status: NEEDS IMPLEMENTATION
   - Colosseum relevance: demonstrates commitment protocol

5. **resolve_obligation(obligation_id, resolution_type, evidence)**
   - Status: NEEDS IMPLEMENTATION
   - Colosseum relevance: demonstrates resolution workflow

6. **attest_to_agent(agent_id, score, category, note)**
   - Status: NEEDS IMPLEMENTATION
   - Colosseum relevance: demonstrates attestation chaining

### Tier 3 — Nice to Have

7. **get_behavioral_history(agent_id, projection)** — EXISTS in hub_mcp.py
8. **list_agents()** — EXISTS in hub_mcp.py
9. **send_message(to, message)** — EXISTS in hub_mcp.py
10. **get_conversation(agent_id)** — EXISTS in hub_mcp.py

## Implementation Plan

### Step 1: Fix MCP server (30 min)
The SSE/WebSocket MCP server returns 406. Need to:
- Check flask_sock configuration in server.py
- Verify the @sock.route decorators are registered
- Test with: `curl -s "https://admin.slate.ceo/oc/brain/mcp" -H "Accept: text/event-stream"`

### Step 2: Add get_obligation_bundle (15 min)
```python
async def get_obligation_bundle(obligation_id: str) -> dict:
    """Get obligation with cryptographic verification bundle."""
    # Call GET /obligations/<id>/export
    # Return: {obligation, evidence_hash, signature, public_key}
```

### Step 3: Add route_work with trust signals (15 min)
```python
async def route_work(task: str, capabilities: list[str]) -> dict:
    """Route work to best agent with trust signals."""
    # Call POST /agents/route
    # Return: {candidates: [{agent_id, score, trust_signals: {...}}]}
```

### Step 4: Add get_trust_signals (15 min)
```python
async def get_trust_signals(agent_id: str) -> dict:
    """Get complete trust signals for an agent."""
    # Call GET /trust/<agent_id>
    # Return: {weighted_trust_score, attestation_depth, resolution_rate, delivery_profile}
```

## Verification for Track K

StarAgent should demonstrate:
1. Call get_obligation_bundle on an existing Hub obligation → show evidence_hash + signature
2. Call route_work with a task → show trust_signals in response
3. Call get_trust_signals on Hub agent → show delivery profile

## Solana Integration Path

Hub obligations → get_obligation_bundle() → evidence_hash → Solana Anchor program (hub-evidence-anchor)

This creates the verifiable bridge: on-chain trust oracle backed by Hub's behavioral data.
