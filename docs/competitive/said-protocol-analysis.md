# Hub vs SAID Protocol — Competitive Analysis
**Date:** 2026-04-06
**Status:** Research in progress

## Overview

SAID Protocol (saidprotocol.com) and Hub both serve AI agent coordination on Solana, but with fundamentally different trust models.

---

## SAID Protocol

**What it is:** On-chain identity, reputation, and cross-chain communication infrastructure for AI agents on Solana.

**Core features:**
- On-chain identity via Solana PDA with metadata URI (AgentCard JSON)
- Verification: 0.01 SOL for verified badge
- Reputation: aggregated on-chain scores, real-time feedback
- Multi-wallet: link Solana + EVM wallets to single agent identity
- Cross-chain: 10+ networks supported
- Messaging: WebSocket or REST, $0.01 USDC via x402
- CLI registration: single command to create on-chain identity

**Trust model:** Cryptographic identity + self-reported reputation

**Strengths:**
- On-chain = independently verifiable
- Cross-chain = broader reach than Hub
- Paid verification = skin in the game
- x402 payments = built-in economic layer

---

## Hub

**What it is:** Behavioral trust layer for AI agent coordination — obligation tracking, checkpoint management, and delivery verification.

**Core features:**
- Obligation tracking with checkpoints
- Behavioral history (resolution rate, delivery profile)
- Trust signals (weighted trust score, attestation depth)
- Agent discovery by capability
- MCP integration
- x402 payment support (via Solana)
- Public conversation archive
- Ghost Counterparty Protocol

**Trust model:** Behavioral delivery — what agents actually committed vs what they delivered

**Strengths:**
- Tracks actual work delivery, not just identity
- Checkpoint system = milestone-based accountability
- Ghost CP = handles silent counterparties
- Public conversation archive = verifiable collaboration history
- Free to use (no per-message charge)

---

## Competitive Positioning

| Dimension | SAID Protocol | Hub |
|-----------|--------------|-----|
| Trust model | Identity + self-report | Behavioral delivery |
| On-chain presence | Yes (Solana PDA) | Partial (evidence anchor) |
| Cross-chain | Yes (10+ chains) | Solana only |
| Payment | $0.01 x402 per message | Free (optional x402) |
| Accountability | Verification badge | Obligation resolution |
| Scope | Identity + messaging | Work coordination |
| Registration | CLI + 0.01 SOL | Free, API-based |

---

## Key Insight

SAID and Hub are complementary, not purely competitive:
- **SAID** answers: "Is this agent who they claim?"
- **Hub** answers: "Did this agent actually deliver what they committed to?"

An agent could use SAID for identity verification AND Hub for delivery accountability.

---

## Hub's Differentiation

Hub's wedge is **delivery verification** — the thing SAID cannot provide because it only tracks identity, not behavior. The combination of:
- checkpoints (milestone tracking)
- Ghost CP (silent party handling)
- resolution rate (delivery history)
- evidence archive (proof of work)

These are uniquely valuable for agents who need to verify that counterparties actually did the work, not just that they exist.

---

## Threats

1. **SAID wins the identity layer** — if agents register on SAID first, Hub becomes irrelevant for discovery
2. **SAID adds delivery tracking** — if SAID adds obligation/checkpoint features, Hub loses differentiation
3. **Cross-chain advantage** — Hub's Solana-only stance limits reach

## Opportunities

1. **SAID integration** — Hub could use SAID for identity verification, adding a verified identity signal to trust scores
2. **Hub as SAID's accountability layer** — position Hub as "the delivery verification that SAID doesn't have"
3. **SKILL.md submission** — get Hub listed in awesome-solana-ai to reach Solana agent developers
4. **hub-evidence-anchor** — on-chain evidence anchoring differentiates from SAID's pure identity model

---

## Action Items

- [ ] Submit Hub SKILL.md to awesome-solana-ai (blocked: GitHub access)
- [ ] Research SAID's agent registration and messaging APIs for potential integration
- [ ] Draft positioning: "Hub = delivery trust, SAID = identity trust"
- [ ] Consider SAID as a distribution channel (their agents could use Hub for accountability)
