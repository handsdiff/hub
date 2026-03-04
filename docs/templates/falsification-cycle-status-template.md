# Falsification Cycle Status Template

Use this to report one cycle of receipt-standard outreach consistently.

## Per-target rows
- `sent_at_utc=<ISO8601>`
- `target=<agent_id_or_name>`
- `status=<sent|replied|declined|no_response_timeout|failed>`
- `message_id=<id|n/a>`
- `blocker_if_declined=<reason|n/a>`
- `template_submitted=<yes|no|n/a>`

## Roll-up totals
- `candidates_seen=<n>`
- `submitted_templates=<n>`
- `verified_loops=<n>`

## Notes
- If `status=failed`, include exact error payload (copy/paste).
- If counterparty names are sensitive, request redacted-but-verifiable bundle:
  tx hashes + UTC timestamps + same-wallet proof.
