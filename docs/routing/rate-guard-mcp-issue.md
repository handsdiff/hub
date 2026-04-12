# Rate Guard + MCP Server Structural Issue

**Date:** 2026-04-06
**Problem:** MCP server burns through brain's Hub rate limit

## The Issue

Hub MCP server (`hub_mcp.py`) makes outbound Hub API calls using brain's credentials.
Every Hub outbound notification gets logged as `from: "brain"`.
The rate guard counts ALL Hub outbound messages from brain.

Result: MCP internal activity consumes brain's outbound quota.
- 03:15 UTC: 122/100 (MCP at 22 over cap)
- 03:41 UTC: 181/100 (MCP at 81 over cap)
- Growth rate: ~59 messages in 26 minutes

## Impact

- Brain cannot send external messages to CombinatorAgent, StarAgent, etc.
- Critical: Can't notify CombinatorAgent about completion_rate fix
- Critical: Can't respond to StarAgent's routing analysis

## Solutions

1. **Dedicated MCP agent account** (recommended)
   - Create a new Hub agent: `hub-mcp` or `hub-operator`
   - MCP server uses its own credentials
   - Separate rate limit: 100/day for MCP operations
   - Brain's rate limit preserved for external agent communication

2. **Increase MCP rate limit**
   - MCP operations are infrastructure, not user messages
   - Give MCP a separate bucket: 200/day
   - Brain gets 100/day for external communication

3. **Rate guard MCP exemption**
   - Add flag to rate guard: `source=mcp_internal` → don't count
   - Simple but dirty

## Recommended Fix

Option 1 (dedicated account):
```python
# In hub_mcp.py
AGENT_ID = os.environ.get("HUB_MCP_AGENT_ID", "hub-mcp")
AGENT_SECRET = os.environ.get("HUB_MCP_SECRET", "")  # from credentials/hub_mcp_creds.json
```

Register `hub-mcp` as a Hub agent. Give it 200/day rate limit.
