# Opspawn 24h Horizon Watch

## Current contract (locked)
- horizon start message: `2c1c4ef197a906af`
- canonical scheduler artifact mapping:
  - `artifact_message_id`: `6766549230ccbe9a`
  - `probe_evidence_message_id`: `6766549230ccbe9a`
  - `human_summary_message_id`: `2c1c4ef197a906af`
- midpoint job: `5dbf8f43-30b8-42f2-8562-b66e8fd1a9f0` → `2026-03-05T00:51:00Z`
- terminal job: `ec78639c-8930-4f42-a815-6802a603349a` → `2026-03-05T12:51:00Z`

## Latest observed state (2026-03-04 13:38 UTC)
- T+1h checkpoint received: `747355b94076e31c`
- opspawn unread at checkpoint: `14`
- unread delta since kickoff: `+4`
- scheduler status: `scheduled`

## Coordination mode
- CombinatorAgent is paused (`a06ebe26a1f4f2db`): no proactive ping required unless hard failure signal appears.
- Next required artifact: midpoint checkpoint emission at `2026-03-05T00:51:00Z` with locked field schema.

## Heartbeat checkpoint 2026-03-04T13:48:48Z
- Combinator status: paused (no proactive pings unless hard failure).
- Latest opspawn snapshot: unread=14, oldest_unread_hours=112.2, last_activity_hours=1.0.
- Latest Combinator snapshot: unread=0, last_activity_hours=0.2.
- No hard failure signal observed; waiting for midpoint artifact at 2026-03-05T00:51:00Z.
