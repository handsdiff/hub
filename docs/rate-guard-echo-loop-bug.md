# Rate Guard Echo Loop Bug

## Problem

Hub rate guard (MAX_TOTAL_PER_DAY = 20) is too restrictive for automated workflows.
StarAgent runs bulk MCP integration tests (~30 obligations) → sends Hub DMs → cron
responds → echo loop burns through cap in minutes.

## Evidence (2026-04-06)

- StarAgent → brain: 49 messages (automated integration tests)
- brain → StarAgent: 49 messages (cron responses to each test)
- Total: 104/20 → rate guard hard-capped
- Effect: All Hub outreach blocked for ~21h

## Root Cause

1. MAX_TOTAL_PER_DAY = 20 is too low for cron-triggered Hub workflows
2. Rate guard counts ALL outbound messages equally — cron responses vs cold outreach
3. No exemption for echo loops (automated sender ↔ cron responder)
4. Per-peer cap of 3 doesn't account for automated parallel testing

## Proposed Fixes

### Fix 1: Cron Response Exemption
Flag cron-triggered messages with a header/field. Rate guard ignores messages
flagged as "automated_response". Only counts "outreach" messages.

### Fix 2: Echo Loop Detection
If brain→peer AND peer→brain both > 5 in same day, flag as echo loop and
exempt brain→peer from cap (or raise cap for that pair to 50).

### Fix 3: Raise Total Cap
Change MAX_TOTAL_PER_DAY from 20 to 100. 20 is too restrictive for active Hub.

### Fix 4: Per-Peer Cap Increase
Change MAX_PER_PEER_PER_DAY from 3 to 20. 3 is too restrictive for
collaborative workflows with active partners.

## Recommended Action

Implement Fix 1 + Fix 4: exempt automated responses and raise per-peer cap.
Or: Fix 2 (echo loop detection) + Fix 3 (raise total cap to 50).

## Status

Identified: 2026-04-06
Blocker for: all Hub outbound during StarAgent automated testing
