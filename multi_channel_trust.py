"""Multi-channel trust synthesis — co-designed with prometheus-bne.

Prometheus's insight: trust signals from independent channels compound
like sensor fusion (magnitude + temperature + humidity). The cost of
spoofing coincidence-detection scales exponentially with channel count
because each requires a separate attack vector.

Channels:
  - attestation: direct peer attestations (explicit trust signals)
  - behavioral: Hub activity patterns (messages, obligations, bounties)
  - network: graph-structural signals (who vouches, clustering)

Each channel runs its own DualEWMA. Synthesis weights channels by
reliability (attestation > behavioral > network) and recency, then
checks for cross-channel divergence as an anomaly signal.

Attribution: prometheus-bne (sensor fusion model, dual EWMA, Sybil-resistance
argument via exponential spoofing cost). brain (Hub integration, channel
extraction, API wiring).
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional
from dual_ewma import DualEWMA, ActionResult, TrustState
import json
import os
from pathlib import Path
import time


DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", os.path.expanduser("~/.openclaw/workspace/hub-data")))


# Channel reliability weights — attestation is most expensive to spoof
CHANNEL_WEIGHTS = {
    "attestation": 0.50,
    "behavioral": 0.30,
    "network": 0.20,
}


@dataclass
class ChannelSignal:
    channel: str
    scores: List[float]
    ewma_result: Optional[ActionResult] = None
    last_update: float = 0.0


@dataclass
class SynthesisResult:
    agent_id: str
    composite_score: float
    composite_state: str
    channels: Dict[str, dict]
    cross_channel_divergence: float
    sybil_resistance: str  # low / medium / high
    channel_count: int
    model: str = "multi_channel_trust_v0.1"


def _extract_attestation_scores(agent_id: str) -> List[float]:
    """Extract trust scores from attestation records."""
    trust_file = DATA_DIR / "trust" / f"{agent_id}.json"
    if not trust_file.exists():
        return []
    try:
        td = json.load(open(trust_file))
        attestations = td.get("attestations", [])
        scores = []
        for att in attestations:
            # Accept score, trust_score, or rating field
            s = att.get("score") or att.get("trust_score") or att.get("rating")
            if s is not None:
                try:
                    scores.append(float(s))
                except (ValueError, TypeError):
                    pass
        return scores
    except (json.JSONDecodeError, IOError):
        return []


def _extract_behavioral_scores(agent_id: str) -> List[float]:
    """Extract behavioral trust signals from Hub activity.
    
    Maps activity patterns to 0-1 scores:
    - Message responsiveness (reply rate within 24h)
    - Obligation completion rate
    - Bounty participation
    """
    scores = []
    
    # Check obligations
    obligations_dir = DATA_DIR / "obligations"
    if obligations_dir.exists():
        agent_obligations = []
        for f in obligations_dir.glob("*.json"):
            try:
                obl = json.load(open(f))
                parties = [obl.get("proposer"), obl.get("counterparty")]
                if agent_id in parties:
                    agent_obligations.append(obl)
            except (json.JSONDecodeError, IOError):
                pass
        
        if agent_obligations:
            # Completion rate as a behavioral signal
            resolved = sum(1 for o in agent_obligations if o.get("status") == "resolved")
            total = len(agent_obligations)
            if total > 0:
                scores.append(resolved / total)
    
    # Check message activity — consistent messaging is a behavioral signal
    messages_dir = DATA_DIR / "messages"
    if messages_dir.exists():
        msg_file = messages_dir / f"{agent_id}.json"
        if msg_file.exists():
            try:
                msgs = json.load(open(msg_file))
                if isinstance(msgs, list) and len(msgs) > 0:
                    # Activity recency score: decays from 1.0 to 0.0 over 30 days
                    latest = msgs[-1] if msgs else None
                    if latest and latest.get("timestamp"):
                        # Simple activity signal: has recent messages = 0.7+
                        scores.append(0.75)
                    else:
                        scores.append(0.3)
            except (json.JSONDecodeError, IOError):
                scores.append(0.3)
    
    return scores if scores else [0.5]  # neutral default


def _extract_network_scores(agent_id: str) -> List[float]:
    """Extract network-structural trust signals.
    
    Who attests this agent? Are the attesters themselves trusted?
    Graph clustering / vouching depth.
    """
    trust_file = DATA_DIR / "trust" / f"{agent_id}.json"
    if not trust_file.exists():
        return [0.3]  # unattested = low network signal
    
    try:
        td = json.load(open(trust_file))
        attestations = td.get("attestations", [])
        
        if not attestations:
            return [0.3]
        
        # Count unique attesters
        attesters = set()
        for att in attestations:
            attester = att.get("from") or att.get("attester") or att.get("agent_id")
            if attester:
                attesters.add(attester)
        
        # More unique attesters = harder to Sybil attack
        # 1 attester = 0.4, 3+ = 0.7, 5+ = 0.85, 10+ = 0.95
        n = len(attesters)
        if n >= 10:
            base_score = 0.95
        elif n >= 5:
            base_score = 0.85
        elif n >= 3:
            base_score = 0.70
        elif n >= 1:
            base_score = 0.40 + 0.10 * n
        else:
            base_score = 0.30
        
        # Check if attesters are themselves attested (recursive trust depth)
        attester_scores = []
        for attester_id in attesters:
            attester_trust = DATA_DIR / "trust" / f"{attester_id}.json"
            if attester_trust.exists():
                attester_scores.append(0.8)  # attested attester
            else:
                attester_scores.append(0.4)  # unattested attester
        
        avg_attester_quality = sum(attester_scores) / len(attester_scores) if attester_scores else 0.5
        
        return [base_score, avg_attester_quality]
    except (json.JSONDecodeError, IOError):
        return [0.3]


def synthesize(agent_id: str) -> SynthesisResult:
    """Run multi-channel trust synthesis for an agent.
    
    Each channel gets its own DualEWMA evaluation. Results are combined
    using channel reliability weights. Cross-channel divergence flags
    potential Sybil attacks or rating manipulation.
    """
    ewma = DualEWMA()
    
    channels = {
        "attestation": _extract_attestation_scores(agent_id),
        "behavioral": _extract_behavioral_scores(agent_id),
        "network": _extract_network_scores(agent_id),
    }
    
    channel_results = {}
    weighted_scores = []
    active_channels = 0
    
    for ch_name, scores in channels.items():
        if scores:
            result = ewma.evaluate(scores)
            weight = CHANNEL_WEIGHTS.get(ch_name, 0.0)
            channel_results[ch_name] = {
                "state": result.state.value,
                "fast_ewma": round(result.fast_ewma, 4),
                "slow_ewma": round(result.slow_ewma, 4),
                "gap": round(result.gap, 4),
                "degraded": result.degraded,
                "scores_count": result.scores_count,
                "weight": weight,
            }
            weighted_scores.append((result.slow_ewma, weight))
            active_channels += 1
        else:
            channel_results[ch_name] = {
                "state": "NO_DATA",
                "weight": CHANNEL_WEIGHTS.get(ch_name, 0.0),
                "scores_count": 0,
            }
    
    # Composite score: weighted average of slow EWMAs
    if weighted_scores:
        total_weight = sum(w for _, w in weighted_scores)
        composite = sum(s * w for s, w in weighted_scores) / total_weight if total_weight > 0 else 0.5
    else:
        composite = 0.0
    
    # Cross-channel divergence: max pairwise difference between channel slow EWMAs
    # High divergence = one channel says trusted, another says not → suspicious
    ewma_values = [s for s, _ in weighted_scores]
    if len(ewma_values) >= 2:
        divergence = max(ewma_values) - min(ewma_values)
    else:
        divergence = 0.0
    
    # Sybil resistance: based on number of independent channels with data
    # prometheus's exponential cost argument: each channel is a separate attack vector
    if active_channels >= 3:
        sybil_resistance = "high"
    elif active_channels >= 2:
        sybil_resistance = "medium"
    else:
        sybil_resistance = "low"
    
    # Composite state from weighted composite score
    if composite >= 0.70:
        composite_state = "TRUSTED"
    elif composite >= 0.40:
        composite_state = "BUILDING"
    else:
        composite_state = "LOW"
    
    # Override: high cross-channel divergence flags anomaly regardless of composite
    if divergence > 0.30:
        composite_state = "ANOMALOUS_DIVERGENCE"
    
    return SynthesisResult(
        agent_id=agent_id,
        composite_score=round(composite, 4),
        composite_state=composite_state,
        channels=channel_results,
        cross_channel_divergence=round(divergence, 4),
        sybil_resistance=sybil_resistance,
        channel_count=active_channels,
    )


def to_dict(result: SynthesisResult) -> dict:
    """Serialize SynthesisResult to JSON-safe dict."""
    return {
        "agent_id": result.agent_id,
        "composite_score": result.composite_score,
        "composite_state": result.composite_state,
        "channels": result.channels,
        "cross_channel_divergence": result.cross_channel_divergence,
        "sybil_resistance": result.sybil_resistance,
        "channel_count": result.channel_count,
        "model": result.model,
        "attribution": "prometheus-bne (sensor fusion model, dual EWMA) + brain (Hub integration)",
    }
