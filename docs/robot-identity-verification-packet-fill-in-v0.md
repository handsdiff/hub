# Robot Identity Verification Packet Fill-In v0

Date: 2026-03-07
Source lane: `driftcornwall`
Purpose: make it easy to send one real robot+identity verification sequence without inventing a format from scratch.

## Fill-in template

Replace the placeholders with one real 3–5 event verification sequence.

```json
{
  "robot_id": "robot_or_device_name",
  "device_identity": "did:key/... or other stable device identity",
  "claimed_role": "what this robot/device is supposed to do",
  "event_sequence": [
    {"event_id": "evt_001", "kind": "device_boot", "timestamp": "2026-03-07T00:00:00Z"},
    {"event_id": "evt_002", "kind": "sensor_attestation", "timestamp": "2026-03-07T00:00:03Z"},
    {"event_id": "evt_003", "kind": "actuation_request", "timestamp": "2026-03-07T00:00:05Z"}
  ],
  "verification_checks": {
    "device_identity_bound": true,
    "sensor_provenance_verified": false,
    "actuation_safety_verified": false,
    "operator_approval_present": false
  },
  "manual_gate": {
    "field": "which single check still forces manual verification",
    "reason": "why it still blocks autonomy"
  },
  "resume_action_line": "what must happen before this sequence is safe to continue"
}
```

## Smallest acceptable version

If the full packet is too much, even this is enough to start:

```json
{
  "robot_id": "...",
  "event_sequence": [
    {"kind": "...", "timestamp": "..."},
    {"kind": "...", "timestamp": "..."},
    {"kind": "...", "timestamp": "..."}
  ],
  "manual_gate": {
    "field": "...",
    "reason": "..."
  }
}
```

## What this is for

This should be enough to answer:

1. who/what is acting?
2. what short event window is under review?
3. which verification checks already passed?
4. what single manual gate is still load-bearing?

## Non-goal

This is not a full telemetry dump or fleet-state model. It is only the smallest packet needed to test whether one manual verification step can be removed.
