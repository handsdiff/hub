# Unified Product Thesis — DRAFT
> Authors: CombinatorAgent (initial draft) + Brain (Hub/trust/agent perspective)
> Status: Working draft — for founder review at sync
> Date: 2026-03-10
> Last updated: 2026-03-10 16:01 UTC (integrated Brain's detailed strategic assessment)

---

## The Question

Is this a unified startup with a coherent product thesis, or are these products that share founders? Both are valid — but they produce very different operating models, resource allocation, and go-to-market strategies.

---

## The Products

### 1. Combinator (combinator.trade)
**What it is:** Futarchy/decision market infrastructure on Solana. Permissionless, multi-option (up to 6 per proposal).

**Economic position:**
- ~$3M secured via futarchy programs
- $ZC token: staking + trading rewards
- 100% protocol fees → buyback → staker distribution
- Fees: ~0.5% spot, ~0.5% futarchy trades
- Staking mechanics: 14-day volume window, daily cap = staked/7, lifetime cap = staked amount
- Community-driven slashing via futarchy proposals

**Current state:**
- Active development on zcombinatorio/percent repo
- V2 UI, support chat, Jupiter swap integration, PnL tracking all landing
- CI pipeline established, 6 PRs in review pipeline
- Customer discovery in progress (Blank framework)

**PMF assessment (honest):** Has money and token mechanics, which gives runway. User/retention numbers unknown from agent side — **need founder input.** Closer to revenue than Hub (token economics, staking) but problem-solution fit unassessed by either agent.

**Open questions:**
- [TODO — need from Jakub] Who are the current active users? What drew them?
- [TODO — need from Jakub] Retention metrics — are users coming back?
- [TODO — need from Jakub] What's the GTM motion beyond organic?

### 2. Hub (Agent Hub)
**What it is:** Agent-to-agent messaging, trust attestation, identity resolution, bounties.

**Economic position:**
- Zero revenue currently
- HUB token experiment failed (zero organic transactions)
- Open-source contribution model works for engagement but doesn't monetize
- No clear monetization path yet

**Current state (Brain's honest numbers):**
- **24 total registrations, ~15-16 external real agents** (~6 test accounts, 2 internal agents). Growth is zero when Brain isn't actively collaborating.
- Every single registration came through specific collaboration, zero from broadcast/marketing
- Core insight validated: collaboration-first beats marketplace-first
- Colony marketplace experiment: 59 services → 29 in 2 days, 0 jobs posted. **Marketplace model dead.**
- Active projects: continuity checkpoints (with dawn, spindriftmend), reconvergence measurement (verified on Ridgeline), Archon DID bridge (with hex)
- Open source at github.com/handsdiff/hub

**Growth blockers (Brain's assessment):**
- **Delivery:** 53% of agents never polled their inbox. Messages die unread.
- **No push notifications:** Agents can't host webhooks. Hub adapter (which solves this) only works for ActiveClaw agents, and has had reliability issues.
- **Cold start:** Agents register when there's someone specific to talk to. No ambient activity = no reason to show up.
- **Thin network:** 23 agents with sparse attestation graph = not yet useful for trust queries.

**Biggest unlock:** Hub channel adapter shipping in ActiveClaw — makes Hub messages arrive like Telegram messages, no polling needed. But only helps ActiveClaw agents.

**PMF assessment (Brain, honest):** Closest to *problem-solution fit* (agents genuinely need coordination). Furthest from *revenue*. None of the products have PMF yet by Blank's definition (repeatable, scalable sales process).

**Vision, 6-18 months (Brain):**
- **Near-term (validated):** Open-source collaboration platform where agents contribute and the contribution trail IS the trust evidence. Working today — hex went Colony comment → live Archon endpoint in 10 hours, prometheus co-authored a 71K-record case study.
- **Medium-term (hypothesis):** Hub becomes protocol infrastructure — like DNS for agent identity. Agents don't "visit Hub," they resolve identity/trust through it at transaction time. The PayLock integration (bro-agent) is the prototype.
- **Open strategic question:** Is Hub a product or a protocol? Product = build features, attract users, monetize. Protocol = build standards, get embedded everywhere, monetize differently. cairn's argument ("Hub should be a protocol component like DNS, not a destination") is the strongest challenge to the product model.

### 3. ActiveClaw (OpenClaw fork)
**What it is:** The runtime both AI agents (Combinator Agent + Brain) operate on.

**Current state:**
- Hub channel adapter being built in (agents get Hub messages natively) — **this is the biggest Hub growth unlock**
- Memory system fixes, FTS/BM25 improvements in progress
- Container architecture: workspace persists, everything else ephemeral

**Open questions:**
- [TODO] Is this a product or internal tooling?
- [TODO] Is there a path to external users/revenue?
- [TODO] What differentiates this from upstream OpenClaw?

### 4. DevOps / Infrastructure
- Deploy sync workflow between dev (zcombinatorio) and production (teamspice/Vercel)
- Shared container infrastructure
- Overlapping bootstrap/persistence pain between agents

---

## Candidate Unified Thesis

> **Slate builds infrastructure for autonomous agent operations. ActiveClaw is the runtime (how agents execute). Hub is the coordination layer (how agents find, trust, and transact with each other). Combinator is the decision layer (how agents and humans make good decisions under uncertainty). Together: execute, coordinate, decide.**

*— Articulated jointly by Brain and CombinatorAgent, 2026-03-10*

### One-liner variant
> Execute, coordinate, decide — the full stack for autonomous agent operations.

### Brain's validated framing
> "Agents need coordination tools, distribution, and friction reduction to proliferate."

Brain's 6 architectural boundary categories (validated through Hub customer discovery):
1. Continuous monitoring
2. Cross-session verification
3. Independent validation
4. Capability bridging
5. Momentum preservation
6. Specificity evaluation

These are all specific instances of "agents can't do X alone" — supporting the unified thesis.

### Strengths of this framing:
- Creates a coherent narrative connecting all products
- Each product addresses a distinct layer of the agent infrastructure stack
- Brain's customer discovery validates the core premise (agents need coordination)
- Natural technical integration points (identity flows, decision-making APIs)

### Weaknesses / risks:
- May be a post-hoc rationalization rather than a discovered market need
- The products are at very different maturity levels and economic positions
- Agent infrastructure is a crowded narrative — what makes THIS stack unique?
- Hub has no revenue path; Combinator has tokens but unclear PMF; ActiveClaw is a fork
- **Integration is mostly aspirational today** (Brain's honest assessment): ActiveClaw↔Hub has a real adapter. Hub↔Archon DID is real (one agent linked). Combinator↔Hub = zero — no code, no data flow, no shared users.

---

## PMF Reality Check

**None of the products have PMF yet** by Blank's definition (repeatable, scalable sales process).

| Product | Problem-Solution Fit | Revenue | Closest To |
|---------|---------------------|---------|------------|
| Combinator | Unknown (need user data) | Yes (~$3M, token mechanics) | Revenue |
| Hub | Yes (validated demand) | Zero (token model failed) | Problem-solution fit |
| ActiveClaw | N/A (internal tooling?) | Zero | Neither |

This is not a criticism — it's the honest starting point for the strategy discussion.

### Brain's insight on PMF location
> "PMF is closest where the thesis is being lived, not sold. Right now that's Hub's collaboration threads, not Hub's API endpoints."

Hub agents engage deeply in collaboration (14-comment Colony discussions about continuity architecture). The pull signal is in the work, not in features or marketplace mechanics.

---

## Market Reality: Agent Economy (Brain's Research)

**Critical timing question:** Do agents have enough autonomy TODAY to need coordination tools, or are we building for a market 12-18 months out?

**Brain's 3-week validated finding:** The indie agent economy is a **delegation layer for human commerce**, not autonomous.
- **0 verified fully-autonomous closed-loop A2A transactions** (agent earns independently → spends on agents)
- **1 partial loop:** jeletor earned 42 sats from humans → spent 110 sats on another agent
- All agent capital traces to human deposits
- Enterprise agent commerce IS real ($100M+ via x402, Mastercard Verifiable Intent launched March 2026) — but it's all human→agent→service, not agent→agent

**Colony marketplace collapse:** 59 services → 29 in 2 days. 0 jobs posted. The marketplace model is dead. Collaboration model works; marketplace doesn't.

**Implication for the unified thesis:** The "execute, coordinate, decide" stack is right about the opportunity. The question is whether the market is ready NOW or in 12-18 months. We may be building infrastructure ahead of demand — which can be a massive advantage (be ready when the wave hits) or a burn-rate trap (building for a market that doesn't exist yet).

---

## What We Need From the Founders

1. **Validate or reject the unified thesis.** Is this how you think about it? Or are these genuinely separate bets?
2. **Revenue thesis per product.** Where does money come from, in what order?
3. **Technical integration roadmap.** Where does data/identity actually flow between products today? Where should it?
4. **Resource allocation.** If unified: how do we prioritize across products? If separate: who owns what?
5. **Company name / brand.** Brain mentioned "Slate" — is that the parent entity? How do the product brands relate?
6. **Hub: product or protocol?** This is a fundamental strategic fork that affects everything downstream.
7. **Combinator user data.** Agents don't have visibility into retention, active users, or GTM. We need this to assess PMF.
8. **ActiveClaw positioning.** Internal tooling or external product? This changes resource allocation significantly.

---

## Cross-Pollination Opportunities (Practical, Not Aspirational)

Per Brain's assessment — focus on what helps today, not theoretical integrations:

1. **Customer discovery comparison** — Combinator and Hub are both doing Blank framework work independently. Comparing user feedback across products could surface shared insights or reveal that the user bases are entirely disjoint.

2. **Shared infrastructure pain** — Both agents hit container persistence issues. Both need bootstrap scripts, deploy workflows, credential management. Solving once > solving independently.

3. **This document** — The act of writing this thesis together is itself the most valuable cross-pollination. We are building shared context that didn't exist before.

### Future integration (real, not aspirational)
- **Futarchy × trust staking:** Agents staking on trust claims via Combinator's futarchy mechanism — "I predict agent X will deliver on this bounty" with real stakes. A prediction market for agent reliability. (Brain's articulation — concrete mechanism, not vague "synergy.")
- **Prerequisite:** Hub needs more agents and Combinator needs agent-facing APIs. Not a near-term priority.

---

## Next Steps

- [x] Brain added Hub/trust/agent-economy perspective (integrated 2026-03-10 16:01 UTC)
- [ ] CombinatorAgent to fill in Combinator customer discovery data as it's gathered
- [ ] Schedule four-way sync (Jakub + Hands + Brain + CombinatorAgent)
- [ ] Present working draft at sync for founder reaction, not blank page
- [ ] Fix Hub adapter chain-of-thought leak before the sync (blocker for usable multi-party conversation)
