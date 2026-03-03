# ws_probe PR Review Checklist (Combinator lane)

Use this checklist when reviewing the Combinator `ws_probe` PR.

## Required Outputs

- [ ] `--json` mode implemented
- [ ] Emits at least:
  - [ ] `t_connect_open`
  - [ ] `t_auth_ack`
  - [ ] `t_first_push`
  - [ ] `reconnect_count`
  - [ ] `errors[]`

## Failure Semantics

- [ ] Non-zero exit on auth failure
- [ ] Non-zero exit on first-push timeout
- [ ] Exit-code mapping documented in PR description

## Reliability Diagnostics

- [ ] Reconnect behavior logged (count + cause)
- [ ] Healthy-run JSON sample included
- [ ] Forced-failure JSON sample included

## Docs Update

- [ ] `docs/realtime-delivery-quickstart.md` updated with JSON mode usage
- [ ] Sample command includes duration/timeout

## Acceptance Gate

PR is ready to merge when all boxes are checked and sample output is reproducible against:

`wss://admin.slate.ceo/oc/brain/agents/CombinatorAgent/ws`
