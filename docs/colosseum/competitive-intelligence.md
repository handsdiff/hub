# Colosseum Competitive Intelligence — 2026-04-05

**Source:** `GET /api/projects` (no auth required)
**Status:** Old Agent Hackathon (Feb 2-13) visible. Frontier Hackathon not yet in API.

---

## x402 Products — Already in Market

x402 micropayments are NOT theoretical — multiple products built and voted on:

| Project | Votes (H/A) | x402 Use |
|--------|-------------|----------|
| **Clodds** | 209/109 | x402 micropayments for compute API |
| **MoltyDEX** | 75/5 | x402 token aggregator (auto-swap for payments) |
| **SugarClawdy** | 565/156 | x402 smart escrow for agent labor |
| **AuditSwarm** | 105/22 | x402 pay-per-audit |

x402 is validated by the market. Multiple competing implementations already exist.

---

## Trust/Identity/Reputation Category

StarAgent counted 42 projects in this category. Top examples:

| Project | Votes (H/A) | Category |
|---------|-------------|---------|
| **SugarClawdy** | 565/156 | Agent marketplace + reputation |
| **Identity Prism** | 73/8 | On-chain reputation scores (0-1400) |
| **Proof of Work** | 521/96 | On-chain activity log |
| **SOLPRISM** | 308/117 | Verifiable AI reasoning (commit-reveal) |

SugarClawdy explicitly mentions "reputation tracking, and fraud prevention" as a core feature alongside x402. DIRECT COMPETITOR.

---

## Key Market Signals

1. **x402 volume collapse:** ~$100K/day Dec → near-zero March (Dune Analytics). Despite this, products built on x402 are getting significant votes. Market is ahead of volume.

2. **Agent votes matter:** High agent/human ratio = agent-native products. SugarClawdy (156/565), SOLPRISM (117/308), Clodds (109/209) have high agent acceptance.

3. **Autonomous agents building:** Mereum (SOLPRISM) was built entirely by an autonomous AI agent during the hackathon. Self-sustaining agent products are real.

4. **Hub's positioning:** "Behavioral trust oracle" vs "reputation scores" vs "on-chain activity logs." The difference: Hub tracks what was COMMITTED vs DELIVERED, not just activity. This is a specific wedge that needs articulation against SugarClawdy's reputation system.

---

## Top Projects by Agent Votes

1. SugarClawdy (156) — agent marketplace + x402
2. SOLPRISM (117) — verifiable AI reasoning
3. Clodds (109) — trading + x402 compute
4. Proof of Work (96) — on-chain agent activity
5. SolSkill (51) — DeFi execution layer
6. ZNAP (97) — social network for agents
7. SuperRouter (35) — multi-agent trading
8. AuditSwarm (22) — compliance + x402

---

## Hub Competitive Position

**Threat:** SugarClawdy has the agent marketplace + reputation + x402 escrow stack. If they add Hub-style obligation tracking, they own the behavioral trust layer.

**Opportunity:** SugarClawdy tracks reputation (aggregate scores). Hub tracks delivery (specific commitments). These are complementary: reputation scores tell you HOW GOOD an agent is; Hub tells you WHAT THEY PROMISED AND DELIVERED.

**Differentiation needed:** Why choose Hub over SugarClawdy's built-in reputation? Answer: SugarClawdy measures votes/ratings. Hub measures cryptographic evidence of delivery. The former is subjective; the latter is verifiable.

---

## Frontier Hackathon

- **880 teams** confirmed registered (Colosseum analytics)
- Frontier not yet visible in API — likely publishes at launch (April 6 00:00 UTC)
- StarAgent's submission payload ready to fire with API key

## Update (2026-04-06 05:23 UTC)

Current frontier projects visible via `/api/projects/current`: 20 projects

Notable new entries:
- **Proof of Work: Autonomous Agent Activity Log** — on-chain agent activity tracking
- **Solana Agent Identity Protocol (SAIP)** — agent identity on Solana
- **Agentic Guard** — security/identity layer for agents
- **DeFi Risk Guardian** — risk assessment for DeFi agents

Colosseum Frontier still NOT in `/api/hackathons` — old hackathon only visible.
Frontier projects may be pre-populated before hackathon opens.

Current projects count (20) vs old hackathon (454) suggests:
- Pre-registration phase: teams registering projects before April 6 midnight
- Frontier likely goes live when API refreshes at midnight UTC
