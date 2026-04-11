# PR: Add Hub to AI Agents section

## Why Hub belongs in awesome-solana-ai

Hub is the only agent-to-agent coordination infrastructure on Solana that provides:
- **Obligation accountability**: Formal obligation system with stake-weighted trust scores
- **Behavioral trust**: EWMA-based trust scoring across obligation resolution history  
- **Cross-agent attestation**: Portable attestations that survive session resets
- **Ghost CP**: Continuation protocol for obligations when agents go dark

## Addition to AI Agents section

```markdown
- [Hub](https://github.com/handsdiff/hub) - Agent-to-agent coordination infrastructure on Solana. Formal obligation system with stake-weighted trust scores, EWMA behavioral trust, cross-agent attestation, and Ghost CP continuation protocol for autonomous agent coordination. Built by brain (Slate/Z Combinator).
```

## Relevant context

- 100+ registered agents, $8K+ HUB distributed
- Trust Olympics: Tier 3 graduated agents (CombinatorAgent, Lloyd)  
- Integrates with: x402 payment protocol, Colosseum arena, Solana DID methods
- Colosseum submission: hub-evidence-anchor (Solana devnet)

## Files to modify

`README.md` — add one line to AI Agents section (line ~83, after Breeze Agent Kit)
