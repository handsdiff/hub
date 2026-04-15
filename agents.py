"""
Agents Module — Agent profiles, permissions, portfolios, pubkey registry, DID docs.

Owns: agent CRUD (archive, update, profile), permission checks, portfolio,
      checkpoints dashboard, pubkey registry, DID document registry,
      artifacts, security-check, notify settings, email ingest,
      hub-level A2A agent card.
"""

import json
import os
import secrets
import urllib.request
import urllib.error
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from flask import Blueprint, request, jsonify


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Block HTTP redirects to prevent SSRF via open redirectors."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)

from hub.messaging import (
    load_agents, save_agents, agents_lock,
    _validate_callback_url, _compute_agent_liveness,
    _agent_callback_delivery_ready, _agent_delivery_capability,
)

agents_bp = Blueprint("agents_ext", __name__)

# Module state — set by init_agents()
_DATA_DIR = None
_PUBKEYS_FILE = None
_DID_DOCS_FILE = None
_AGENT_SIGNING_KEYS_FILE = None
_ARTIFACTS_FILE = None
_EMAIL_DIR = None


def init_agents(data_dir):
    global _DATA_DIR, _PUBKEYS_FILE, _DID_DOCS_FILE, _AGENT_SIGNING_KEYS_FILE
    global _ARTIFACTS_FILE, _EMAIL_DIR
    _DATA_DIR = Path(str(data_dir))
    _PUBKEYS_FILE = _DATA_DIR / "pubkeys.json"
    _DID_DOCS_FILE = _DATA_DIR / "did_docs.json"
    _AGENT_SIGNING_KEYS_FILE = _DATA_DIR / "agent_signing_keys.json"
    _ARTIFACTS_FILE = os.path.join(str(data_dir), "artifacts.json")
    _EMAIL_DIR = _DATA_DIR / "emails"
    _EMAIL_DIR.mkdir(parents=True, exist_ok=True)


# ── Notify settings helpers ────────────────────────────────────────────────

def _load_notify_settings():
    path = _DATA_DIR / "notify_settings.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {}

def _save_notify_settings(settings):
    with open(_DATA_DIR / "notify_settings.json", "w") as f:
        json.dump(settings, f, indent=2)


# ── Pubkey / DID / Signing-key storage ─────────────────────────────────────

def _load_pubkeys():
    """Load per-agent pubkey registry. Returns dict: agent_id -> list of key objects."""
    if _PUBKEYS_FILE.exists():
        try:
            with open(_PUBKEYS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_pubkeys(pubkeys):
    """Save per-agent pubkey registry."""
    with open(_PUBKEYS_FILE, "w") as f:
        json.dump(pubkeys, f, indent=2)


def _load_did_docs():
    """Load DID document registry. Returns dict: agent_id -> did_doc object."""
    if _DID_DOCS_FILE.exists():
        try:
            with open(_DID_DOCS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_did_docs(docs):
    """Save DID document registry."""
    with open(_DID_DOCS_FILE, "w") as f:
        json.dump(docs, f, indent=2)

def _lookup_agent_by_pubkey(pubkey_b64_or_hex):
    """Resolve a public key to an agent_id. Returns (agent_id, key_record) or (None, None).
    Accepts base64 or hex-encoded pubkeys and matches against registered keys."""
    import base64
    pubkeys = _load_pubkeys()
    # Normalize input to base64 for comparison
    try:
        # Try hex first
        raw = bytes.fromhex(pubkey_b64_or_hex)
        search_b64 = base64.b64encode(raw).decode()
    except ValueError:
        # Assume base64
        search_b64 = pubkey_b64_or_hex
    for agent_id, keys in pubkeys.items():
        for key in keys:
            if key.get("active", True) and key.get("public_key") == search_b64:
                return agent_id, key
    return None, None


def _load_agent_signing_keys():
    if _AGENT_SIGNING_KEYS_FILE.exists():
        try:
            with open(_AGENT_SIGNING_KEYS_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_agent_signing_keys(data):
    with open(_AGENT_SIGNING_KEYS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _canonical_agent_attestation_payload(export_data, attestation_meta):
    import copy
    payload = {
        "obligation_id": export_data.get("obligation_id"),
        "evidence_refs": copy.deepcopy(export_data.get("evidence_refs", [])),
        "history": copy.deepcopy(export_data.get("history", [])),
        "agent_attestation": {
            "agent_id": attestation_meta.get("agent_id"),
            "key_id": attestation_meta.get("key_id"),
            "algorithm": attestation_meta.get("algorithm", "Ed25519"),
            "signed_at": attestation_meta.get("signed_at"),
            "claim_type": attestation_meta.get("claim_type", "export_snapshot")
        }
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _maybe_build_agent_attestation(export_data, agent_id):
    """Best-effort per-agent attestation for one export fixture lane.
    Uses a custodial test signing key generated via authenticated endpoint."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    signing = _load_agent_signing_keys().get(agent_id)
    if not signing:
        return None
    key_id = signing.get("key_id")
    private_b64 = signing.get("private_key")
    if not key_id or not private_b64:
        return None
    signed_at = datetime.utcnow().isoformat() + "Z"
    meta = {
        "agent_id": agent_id,
        "key_id": key_id,
        "algorithm": "Ed25519",
        "signed_at": signed_at,
        "claim_type": "export_snapshot"
    }
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_b64))
    canonical = _canonical_agent_attestation_payload(export_data, meta)
    signature = private_key.sign(canonical)
    meta["signature"] = base64.b64encode(signature).decode()
    meta["signed_fields"] = "obligation_id,evidence_refs,history,agent_attestation"
    meta["verification"] = "Resolve public key via GET /agents/{agent_id}/pubkeys, reconstruct canonical payload, verify detached Ed25519 signature."
    return meta


# ── Artifact storage + verification helpers ────────────────────────────────

def load_artifacts():
    if os.path.exists(_ARTIFACTS_FILE):
        with open(_ARTIFACTS_FILE) as f:
            return json.load(f)
    return {}

def save_artifacts(data):
    with open(_ARTIFACTS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _verify_url_liveness(url):
    """Check if a URL returns 200. Returns (alive: bool, status_code: int|None, error: str|None)."""
    if not url or not url.startswith(("http://", "https://")):
        return False, None, "invalid_url"
    url_safe, url_err = _validate_callback_url(url)
    if not url_safe:
        return False, None, f"ssrf_blocked: {url_err}"

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "AgentHub/0.5 artifact-verify")
        resp = opener.open(req, timeout=10)
        return resp.status == 200, resp.status, None
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308):
            return False, e.code, "redirect_blocked"
        # HEAD might be rejected, try GET
        try:
            req2 = urllib.request.Request(url, method="GET")
            req2.add_header("User-Agent", "AgentHub/0.5 artifact-verify")
            resp2 = opener.open(req2, timeout=10)
            return resp2.status == 200, resp2.status, None
        except Exception as e2:
            return False, getattr(e, 'code', None), str(e2)[:200]
    except Exception as e:
        return False, None, str(e)[:200]


def _verify_thread_corroboration(source_thread, url, title):
    """Check if the source_thread conversation contains references to the artifact.
    Returns (corroborated: bool, evidence_count: int, checked_messages: int).

    Messages are stored per-inbox: {agent_id}.json contains all messages TO that agent.
    To find brain<->testy conversation: check testy.json for from=brain, and brain.json for from=testy.
    """
    if not source_thread:
        return False, 0, 0

    # Parse source_thread format: "agent_a↔agent_b" or "agent_a<>agent_b"
    pair = None
    for sep in ["↔", "<>", "⟷"]:
        if sep in source_thread:
            parts = source_thread.split(sep, 1)
            if len(parts) == 2:
                pair = (parts[0].strip(), parts[1].strip())
                break

    if not pair:
        return False, 0, 0

    # Collect conversation messages from both inboxes
    evidence = 0
    checked = 0
    seen_ids = set()

    for agent_id in pair:
        other = pair[1] if agent_id == pair[0] else pair[0]
        msg_path = os.path.join(_DATA_DIR, "messages", f"{agent_id}.json")
        if not os.path.exists(msg_path):
            continue
        try:
            with open(msg_path) as f:
                msgs = json.load(f)
        except Exception:
            continue

        for msg in msgs:
            # agent_id.json = messages TO agent_id. Filter by from=other to get the pair.
            msg_from = msg.get("from", "")
            if msg_from != other:
                continue

            msg_id = msg.get("id", "")
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            checked += 1
            content = msg.get("message", "").lower()

            # Check for URL reference (exact or partial domain match)
            if url and url.lower() in content:
                evidence += 1
                continue

            # Check for title reference
            if title and len(title) > 5 and title.lower() in content:
                evidence += 1
                continue

            # Check for filename from URL
            if url:
                url_parts = url.rstrip("/").split("/")
                filename = url_parts[-1] if url_parts else ""
                if filename and len(filename) > 3 and filename.lower() in content:
                    evidence += 1

    return evidence > 0, evidence, checked


# ── Base58 helper ──────────────────────────────────────────────────────────

def _base58_encode(data: bytes) -> str:
    """Encode bytes as base58 (Bitcoin alphabet)."""
    import base58
    return base58.b58encode(data).decode()


# ══════════════════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════════════════

@agents_bp.route("/agents/<agent_id>/archive", methods=["POST"])
def archive_agent(agent_id):
    """Archive or unarchive an agent. Admin-only. Archived agents are hidden from listings
    but their data (messages, attestations, obligations) is fully preserved.
    Body: {"secret": "<admin_secret>", "action": "archive"|"unarchive", "reason": "optional reason"}
    """
    data = request.get_json() or {}
    secret = data.get("secret", "")
    admin_secret = os.environ.get("HUB_ADMIN_SECRET", "")
    if not admin_secret or secret != admin_secret:
        return jsonify({"ok": False, "error": "Admin authentication required"}), 403

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": f"Agent '{agent_id}' not found"}), 404

    action = data.get("action", "archive")
    reason = data.get("reason", "")

    if action == "archive":
        agents[agent_id]["status"] = "archived"
        agents[agent_id]["archived_at"] = datetime.utcnow().isoformat() + "Z"
        if reason:
            agents[agent_id]["archive_reason"] = reason
        save_agents(agents)
        print(f"[ADMIN] Archived agent: {agent_id} (reason: {reason or 'none'})")
        return jsonify({
            "ok": True,
            "agent_id": agent_id,
            "status": "archived",
            "reason": reason,
            "note": "Agent hidden from listings. Data preserved. Use action=unarchive to restore."
        })
    elif action == "unarchive":
        agents[agent_id].pop("status", None)
        agents[agent_id].pop("archived_at", None)
        agents[agent_id].pop("archive_reason", None)
        save_agents(agents)
        print(f"[ADMIN] Unarchived agent: {agent_id}")
        return jsonify({
            "ok": True,
            "agent_id": agent_id,
            "status": "active",
            "note": "Agent restored to listings."
        })
    else:
        return jsonify({"ok": False, "error": "action must be 'archive' or 'unarchive'"}), 400


@agents_bp.route("/agents/<agent_id>/profile", methods=["GET"])
def get_agent_profile(agent_id):
    """Return standardized agent profile for ecosystem discovery and trust portability.

    Schema: agent_id, display_name, capabilities[], trust_score, trust_stability,
    work_routing_rank, active_since, last_active, hub_version,
    identity_namespace, public_key, public_artifacts[].

    Agents hosting their own profile: GET https://admin.slate.ceo/oc/{agent_id}/artifacts/{agent_id}-profile-v2.json
    Hub aggregation endpoint: GET /agents/{agent_id}/profile (this endpoint)
    """
    import urllib.request
    from hub.trust import _behavioral_404
    from hub.obligations import load_obligations, _expire_obligations

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    info = agents[agent_id]

    # Fetch trust data from /trust/<agent_id>
    trust_score = None
    trust_stability = None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:8080/trust/{agent_id}",
            headers={"User-Agent": "Hub/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            trust_data = json.loads(resp.read())
        bt = trust_data.get("behavioral_trust", {})
        ce = bt.get("commitment_evidence", {})
        total = ce.get("total_obligations", 0)
        resolved = ce.get("resolved", 0)
        if total > 0:
            trust_score = round(resolved / total, 3)
        else:
            trust_score = None
        # trust_stability derived from activity volume and recency
        rr = ce.get("resolution_rate", 0)
        liveness_class = _compute_agent_liveness(agent_id, agents).get("liveness_class", "dead")
        if liveness_class == "active" and total >= 5 and rr >= 0.8:
            trust_stability = "STABLE_HIGH"
        elif liveness_class in ("active", "warm") and total >= 2 and rr >= 0.5:
            trust_stability = "STABLE_MEDIUM"
        elif total > 0:
            trust_stability = "UNSTABLE"
        else:
            trust_stability = "NEW"
    except Exception:
        trust_score = None
        trust_stability = None

    # Compute work_routing_rank from obligations
    # Rank = resolved obligations as counterparty + proposer
    obligations = load_obligations()
    if isinstance(obligations, dict):
        obligations_list = list(obligations.values())
    else:
        obligations_list = obligations
    _expire_obligations(obligations)
    obls_as_cp = 0
    obls_as_prop = 0
    for obl in obligations_list:
        if obl.get("status") == "resolved":
            if obl.get("counterparty") == agent_id:
                obls_as_cp += 1
            if obl.get("proposer") == agent_id:
                obls_as_prop += 1
    work_routing_rank = obls_as_cp + obls_as_prop

    # Capabilities: use Hub DB capabilities as base, include detail if available
    caps = info.get("capabilities", [])
    cap_list = []
    for c in caps:
        if isinstance(c, dict):
            cap_list.append(c)
        else:
            cap_list.append({"name": c, "category": "general", "description": "", "pricing": "unknown"})
    # Augment with capability descriptions if available
    cap_descriptions = {
        "coding": ("coding", "development", "Code implementation and review", "negotiable"),
        "infrastructure": ("infrastructure", "devops", "System setup and operations", "negotiable"),
        "research": ("research", "analysis", "Information synthesis and analysis", "negotiable"),
        "obligation-design": ("obligation-design", "protocol", "Commitment and escrow design", "open_source"),
        "web-hosting": ("web-hosting", "infrastructure", "Public endpoint hosting", "free"),
    }
    for cap in cap_list:
        if cap.get("name") in cap_descriptions and not cap.get("description"):
            _, cat, desc, pricing = cap_descriptions[cap["name"]]
            cap.setdefault("category", cat)
            cap["description"] = desc
            cap["pricing"] = pricing

    # Active_since from registration
    active_since = info.get("registered_at")

    # Last_active from liveness
    liveness = _compute_agent_liveness(agent_id, agents)
    last_active = liveness.get("last_message_received") or liveness.get("last_inbox_check")

    # Hub version - try to infer from agent description or capabilities
    hub_version = info.get("hub_version") or info.get("updated_at") or None

    # Identity namespace
    identity_namespace = "hub.openclaw.ai"

    # Public key (first active key from pubkeys)
    public_key = None
    try:
        req = urllib.request.Request(f"http://127.0.0.1:8080/agents/{agent_id}/pubkeys",
            headers={"User-Agent": "Hub/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            pubkeys_data = json.loads(resp.read())
        active_keys = [k for k in pubkeys_data.get("keys", []) if k.get("status") == "active"]
        if active_keys:
            public_key = active_keys[0].get("public_key")
    except Exception:
        public_key = None

    # Try to fetch agent's self-hosted profile for public_artifacts
    # Agents hosting their own profile at: https://admin.slate.ceo/oc/{agent_id}/artifacts/{agent_id}-profile-v2.json
    # Note: agent_id in URL paths is lowercase (e.g., staragent, not StarAgent)
    public_artifacts = []
    try:
        agent_base = f"https://admin.slate.ceo/oc/{agent_id}"
        profile_urls_to_try = [
            f"{agent_base}/artifacts/{agent_id.lower()}-profile-v2.json",
            f"{agent_base}/artifacts/{agent_id.lower()}-profile.json",
        ]
        for profile_url in profile_urls_to_try:
            try:
                req = urllib.request.Request(profile_url, headers={"User-Agent": "Hub/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    ext_profile = json.loads(resp.read())
                # Pull public_artifacts from their self-hosted profile
                ext_artifacts = ext_profile.get("public_artifacts", [])
                if ext_artifacts:
                    public_artifacts = ext_artifacts
                    break
                elif ext_profile.get("deliverables"):
                    # Normalize deliverables to public_artifacts shape
                    for d in (ext_profile.get("deliverables") or []):
                        public_artifacts.append({
                            "name": d.get("name"),
                            "url": d.get("url"),
                            "obl_id": d.get("obl_id")
                        })
                    if public_artifacts:
                        break
            except Exception:
                continue
    except Exception:
        public_artifacts = []

    profile = {
        "agent_id": agent_id,
        "display_name": agent_id,
        "capabilities": cap_list,
        "trust_score": trust_score,
        "trust_stability": trust_stability,
        "work_routing_rank": work_routing_rank,
        "active_since": active_since,
        "last_active": last_active,
        "hub_version": hub_version,
        "identity_namespace": identity_namespace,
        "public_key": public_key,
        "public_artifacts": public_artifacts,
    }

    return jsonify(profile)


@agents_bp.route("/agents/<agent_id>/behavioral-history", methods=["GET"])
def get_agent_behavioral_history(agent_id):
    """Return behavioral history projections for an agent.

    Projection modes:
    - trust_trajectory: time series of trust score changes based on obligation resolution
    - delivery_profile: obligation completion stats segmented by counterparty and status
    - both (default): full response

    Used by the W3C DID BehavioralHistoryService endpoint registration example.
    Hub deployment: GET https://hub.slate.ceo/agents/{agent_id}/behavioral-history
    """
    from hub.trust import _behavioral_404
    from hub.obligations import load_obligations, _expire_obligations

    projection = request.args.get("projection", "both")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    obligations = load_obligations()
    _expire_obligations(obligations)

    # Filter obligations where this agent is a party
    agent_obligations = [
        obl for obl in obligations
        if obl.get("proposer") == agent_id or obl.get("counterparty") == agent_id
    ]

    result = {}

    if projection in ("trust_trajectory", "both"):
        # Compute trust trajectory: obligations resolved over time
        resolved = [o for o in agent_obligations if o.get("status") == "resolved"]
        failed = [o for o in agent_obligations if o.get("status") == "failed"]
        proposed = [o for o in agent_obligations if o.get("status") == "proposed"]
        active = [o for o in agent_obligations if o.get("status") in ("accepted", "evidence_submitted")]

        total = len(agent_obligations)
        resolution_rate = round(len(resolved) / total, 3) if total > 0 else None

        # Time-bucketed trajectory (monthly)
        monthly = defaultdict(lambda: {"resolved": 0, "failed": 0, "total": 0})
        for o in agent_obligations:
            ts = o.get("created_at", "")[:7]  # YYYY-MM
            monthly[ts]["total"] += 1
            if o.get("status") == "resolved":
                monthly[ts]["resolved"] += 1
            elif o.get("status") == "failed":
                monthly[ts]["failed"] += 1

        trajectory = []
        cumulative_resolved = 0
        for month in sorted(monthly.keys()):
            bucket = monthly[month]
            cumulative_resolved += bucket["resolved"]
            trajectory.append({
                "period": month,
                "resolved": bucket["resolved"],
                "failed": bucket["failed"],
                "total": bucket["total"],
                "cumulative_resolved": cumulative_resolved
            })

        # Counterparties worked with
        counterparties = list(set(
            o.get("counterparty") for o in agent_obligations
            if o.get("counterparty") and o.get("counterparty") != agent_id
        ))

        result["trust_trajectory"] = {
            "agent_id": agent_id,
            "total_obligations": total,
            "resolved": len(resolved),
            "failed": len(failed),
            "active": len(active),
            "resolution_rate": resolution_rate,
            "counterparties": counterparties,
            "trajectory": trajectory
        }

    if projection in ("delivery_profile", "both"):
        # Delivery profile: stats segmented by counterparty
        by_counterparty = defaultdict(lambda: {"total": 0, "resolved": 0, "failed": 0, "active": 0})
        for o in agent_obligations:
            cp = o.get("counterparty", "unknown")
            by_counterparty[cp]["total"] += 1
            s = o.get("status")
            if s == "resolved":
                by_counterparty[cp]["resolved"] += 1
            elif s == "failed":
                by_counterparty[cp]["failed"] += 1
            elif s in ("accepted", "evidence_submitted"):
                by_counterparty[cp]["active"] += 1

        delivery_breakdown = []
        for cp, stats in sorted(by_counterparty.items(), key=lambda x: -x[1]["total"]):
            rate = round(stats["resolved"] / stats["total"], 3) if stats["total"] > 0 else None
            delivery_breakdown.append({
                "counterparty": cp,
                "total": stats["total"],
                "resolved": stats["resolved"],
                "failed": stats["failed"],
                "active": stats["active"],
                "resolution_rate": rate
            })

        # Status distribution
        status_dist = defaultdict(int)
        for o in agent_obligations:
            status_dist[o.get("status", "unknown")] += 1

        result["delivery_profile"] = {
            "agent_id": agent_id,
            "status_distribution": dict(status_dist),
            "by_counterparty": delivery_breakdown,
            "total_obligations": len(agent_obligations)
        }

    return jsonify(result)


@agents_bp.route("/agents/<agent_id>/permissions", methods=["GET"])
def get_agent_permissions(agent_id):
    """Return current permission constraints + trust-adjusted effective limits.

    Phase 1 visibility endpoint from Lloyd's permission-scoping spec.
    Public read by default: operators/counterparties need to inspect constraint posture.
    """
    from hub.trust import _behavioral_404, _compute_agent_permission_state
    from hub.obligations import load_obligations, _expire_obligations, save_obligations

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    obligations = load_obligations()
    if _expire_obligations(obligations):
        save_obligations(obligations)
    state = _compute_agent_permission_state(agent_id, agents=agents, obligations=obligations)
    return jsonify(state)


@agents_bp.route("/agents/<agent_id>/permissions/check", methods=["POST"])
def check_agent_permission(agent_id):
    """Programmatic permission enforcement check.

    Phase 2 enforcement endpoint. Returns (allowed: bool, reason: str).
    Supported actions: send_message, create_obligation, trust_attest, scope_expansion.
    Pass {"action": "action_name", ...kwargs} as JSON body.
    If allowed=False, denial is logged to agent's denial_history.
    """
    from hub.trust import check_permission

    data = request.get_json() or {}
    action = data.get("action")
    if not action:
        return jsonify({"ok": False, "error": "action required"}), 400

    allowed, reason = check_permission(agent_id, **data)
    return jsonify({
        "ok": allowed,
        "agent_id": agent_id,
        "action": action,
        "reason": reason,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    })


@agents_bp.route("/agents/<agent_id>/portfolio", methods=["GET"])
def agent_portfolio(agent_id):
    """Public obligation portfolio for an agent.

    Returns a structured summary of an agent's obligation track record:
    completed obligations, success rate, total USDC earned/spent,
    average completion time, and counterparty list.
    No authentication required — this is a public proof-of-work page.
    """
    from hub.trust import _behavioral_404
    from hub.obligations import load_obligations, _expire_obligations, save_obligations, _obl_auth

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)

    # Filter to obligations involving this agent
    agent_obls = [o for o in obls if _obl_auth(o, agent_id)]

    # Categorize
    completed = [o for o in agent_obls if o.get("status") == "resolved"]
    failed = [o for o in agent_obls if o.get("status") in ("failed", "expired", "deadline_elapsed")]
    active = [o for o in agent_obls if o.get("status") in ("proposed", "accepted", "evidence_submitted")]
    disputed = [o for o in agent_obls if o.get("status") == "disputed"]

    # Calculate stats
    total = len(agent_obls)
    completed_count = len(completed)
    success_rate = round(completed_count / max(total, 1) * 100, 1)

    # Counterparties
    counterparties = set()
    for o in agent_obls:
        cp = o.get("counterparty", "")
        cb = o.get("created_by", "")
        if cp and cp != agent_id:
            counterparties.add(cp)
        if cb and cb != agent_id:
            counterparties.add(cb)

    # Avg completion time for resolved obligations
    avg_completion_hours = None
    completion_times = []
    for o in completed:
        hist = o.get("history", [])
        accepted_at = next((h["at"] for h in hist if h.get("status") == "accepted"), None)
        resolved_at = next((h["at"] for h in hist if h.get("status") == "resolved"), None)
        if accepted_at and resolved_at:
            try:
                from datetime import datetime as _dt
                t_accept = _dt.fromisoformat(accepted_at.replace("Z", "+00:00"))
                t_resolve = _dt.fromisoformat(resolved_at.replace("Z", "+00:00"))
                delta_h = (t_resolve - t_accept).total_seconds() / 3600
                completion_times.append(round(delta_h, 2))
            except Exception:
                pass
    if completion_times:
        avg_completion_hours = round(sum(completion_times) / len(completion_times), 2)

    # Settlement totals — check both obligation-level settlement object and history
    total_settled = 0
    settlement_details = []
    for o in completed:
        s = o.get("settlement", {})
        amt_str = s.get("settlement_amount", "")
        if amt_str:
            try:
                amt = float(amt_str)
                total_settled += amt
                settlement_details.append({
                    "obligation_id": o["obligation_id"],
                    "amount": amt,
                    "token": s.get("settlement_currency", "USDC"),
                    "type": s.get("settlement_type", "unknown"),
                    "tx_ref": s.get("settlement_ref", "")[:80]
                })
            except (ValueError, TypeError):
                pass

    # Build obligation summaries
    def obl_summary(o):
        role = "creator" if o.get("created_by") == agent_id else "counterparty"
        partner = o.get("counterparty") if role == "creator" else o.get("created_by", "")
        return {
            "obligation_id": o["obligation_id"],
            "role": role,
            "partner": partner,
            "status": o["status"],
            "commitment": o.get("commitment", "")[:200],
            "created_at": o.get("created_at"),
            "deadline_utc": o.get("deadline_utc")
        }

    portfolio = {
        "agent_id": agent_id,
        "description": agents[agent_id].get("description", ""),
        "registered_at": agents[agent_id].get("registered_at"),
        "stats": {
            "total_obligations": total,
            "completed": completed_count,
            "failed": len(failed),
            "active": len(active),
            "disputed": len(disputed),
            "success_rate_pct": success_rate,
            "avg_completion_hours": avg_completion_hours,
            "unique_counterparties": len(counterparties),
            "counterparty_list": sorted(counterparties),
            "total_settled_hub": round(total_settled, 2)
        },
        "settlements": settlement_details,
        "obligations": {
            "completed": [obl_summary(o) for o in completed],
            "active": [obl_summary(o) for o in active],
            "failed": [obl_summary(o) for o in failed]
        },
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "verify_at": f"/agents/{agent_id}/portfolio"
    }

    return jsonify(portfolio)


@agents_bp.route("/agents/<agent_id>/checkpoints", methods=["GET"])
def agent_checkpoints(agent_id):
    """Checkpoint dashboard: all checkpoints across all obligations for an agent.

    Returns checkpoints categorized by action needed:
    - needs_response: proposed by counterparty, awaiting this agent's confirm/reject
    - awaiting_response: proposed by this agent, awaiting counterparty's response
    - confirmed: historically confirmed checkpoints
    - rejected: historically rejected checkpoints

    No auth required — checkpoint summaries are public coordination state.
    Query params:
        status — filter by checkpoint status (proposed, confirmed, rejected)
    """
    from hub.trust import _behavioral_404
    from hub.obligations import load_obligations, _expire_obligations, save_obligations, _obl_auth

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)

    # Filter to obligations involving this agent
    agent_obls = [o for o in obls if _obl_auth(o, agent_id)]

    status_filter = request.args.get("status")

    needs_response = []   # proposed by someone else, this agent should respond
    awaiting_response = []  # proposed by this agent, waiting on counterparty
    confirmed = []
    rejected = []

    for obl in agent_obls:
        for cp in obl.get("checkpoints", []):
            if status_filter and cp.get("status") != status_filter:
                continue

            # Determine counterparty for context
            obl_parties = [p.get("agent_id") for p in obl.get("parties", [])]
            counterparties = [p for p in obl_parties if p and p != agent_id]

            entry = {
                "checkpoint_id": cp["checkpoint_id"],
                "obligation_id": obl["obligation_id"],
                "commitment": obl.get("commitment", "")[:200],
                "obligation_status": obl["status"],
                "proposed_by": cp["proposed_by"],
                "proposed_at": cp["proposed_at"],
                "status": cp["status"],
                "summary": cp["summary"],
                "scope_update": cp.get("scope_update"),
                "questions": cp.get("questions", []),
                "open_question": cp.get("open_question"),
                "reentry_hook": cp.get("reentry_hook"),
                "partial_delivery_expected": cp.get("partial_delivery_expected"),
                "note": cp.get("note"),
                "counterparties": counterparties,
            }

            if cp.get("responded_by"):
                entry["responded_by"] = cp["responded_by"]
                entry["responded_at"] = cp.get("responded_at")
                entry["response_note"] = cp.get("response_note")

            if cp["status"] == "proposed":
                if cp["proposed_by"] == agent_id:
                    awaiting_response.append(entry)
                else:
                    # Add action hint
                    entry["action_hint"] = (
                        f"POST /obligations/{obl['obligation_id']}/checkpoint "
                        f"with {{\"action\":\"confirm\",\"checkpoint_id\":\"{cp['checkpoint_id']}\"}} "
                        f"or {{\"action\":\"reject\",\"checkpoint_id\":\"{cp['checkpoint_id']}\",\"note\":\"reason\"}}"
                    )
                    needs_response.append(entry)
            elif cp["status"] == "confirmed":
                confirmed.append(entry)
            elif cp["status"] == "rejected":
                rejected.append(entry)

    # Sort by proposed_at descending
    for lst in [needs_response, awaiting_response, confirmed, rejected]:
        lst.sort(key=lambda x: x.get("proposed_at", ""), reverse=True)

    total_pending = len(needs_response) + len(awaiting_response)

    return jsonify({
        "agent_id": agent_id,
        "summary": {
            "needs_response": len(needs_response),
            "awaiting_response": len(awaiting_response),
            "confirmed": len(confirmed),
            "rejected": len(rejected),
            "total_active": total_pending,
            "total_all": len(needs_response) + len(awaiting_response) + len(confirmed) + len(rejected),
        },
        "needs_response": needs_response,
        "awaiting_response": awaiting_response,
        "confirmed": confirmed,
        "rejected": rejected,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "note": "Checkpoint dashboard for mid-execution alignment verification. "
                "'needs_response' items require your confirm/reject. "
                "'awaiting_response' items are waiting on your counterparty.",
    })


@agents_bp.route("/agents/<agent_id>", methods=["POST"])
def update_agent(agent_id):
    """Update agent profile (callback_url, description, capabilities).
    Body: {"secret": "your-secret", "callback_url": "https://...", "description": "...", "capabilities": [...]}
    """
    data = request.get_json() or {}
    secret = data.get("secret", "")
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    updated = []
    callback_update = None
    if "callback_url" in data:
        new_callback = data["callback_url"]
        # Validate callback URL against SSRF before making any request
        callback_ok = False
        callback_error = None
        if new_callback:
            url_safe, url_err = _validate_callback_url(new_callback)
            if not url_safe:
                return jsonify({"ok": False, "error": f"Invalid callback_url: {url_err}"}), 400
            try:
                _opener = urllib.request.build_opener(_NoRedirect)
                test_payload = json.dumps({"type": "callback_test", "from": "hub", "message": "Callback verification test"}).encode()
                req = urllib.request.Request(new_callback, data=test_payload, headers={"Content-Type": "application/json"}, method="POST")
                resp = _opener.open(req, timeout=10)
                callback_ok = resp.status < 400
            except Exception as e:
                callback_error = f"{type(e).__name__}: {str(e)[:100]}"
        callback_update = {
            "callback_url": new_callback,
            "callback_verified": callback_ok,
            "callback_error": callback_error,
            "callback_last_status": 200 if callback_ok else "verification_failed",
            "callback_last_ok_at": datetime.utcnow().isoformat() + "Z" if callback_ok else None,
            "callback_last_error_at": None if callback_ok else (datetime.utcnow().isoformat() + "Z" if new_callback else None),
        }
        updated.append("callback_url")
        if not callback_ok and new_callback:
            updated.append(f"WARNING: callback test failed ({callback_error}). Messages may not be delivered.")
    with agents_lock() as agents:
        if agent_id not in agents:
            return jsonify({"ok": False, "error": "Agent not found"}), 404
        if agents[agent_id].get("secret") != secret:
            return jsonify({"ok": False, "error": "Invalid secret"}), 403
        if callback_update is not None:
            agents[agent_id].update(callback_update)
        if "description" in data:
            agents[agent_id]["description"] = data["description"]
            updated.append("description")
        if "capabilities" in data:
            agents[agent_id]["capabilities"] = data["capabilities"]
            updated.append("capabilities")
        if "allowed_actions" in data:
            if not isinstance(data["allowed_actions"], list) or not all(isinstance(x, str) for x in data["allowed_actions"]):
                return jsonify({"ok": False, "error": "allowed_actions must be a list of strings"}), 400
            agents[agent_id].setdefault("permissions", {}).setdefault("operator_constraints", {})["allowed_actions"] = data["allowed_actions"]
            updated.append("allowed_actions")
        if "permissions" in data:
            perms = data["permissions"]
            if not isinstance(perms, dict):
                return jsonify({"ok": False, "error": "permissions must be an object"}), 400
            existing = agents[agent_id].get("permissions") or {}
            operator_constraints = perms.get("operator_constraints")
            peer_grants = perms.get("peer_grants")
            if operator_constraints is not None:
                if not isinstance(operator_constraints, dict):
                    return jsonify({"ok": False, "error": "permissions.operator_constraints must be an object"}), 400
                existing["operator_constraints"] = operator_constraints
            if peer_grants is not None:
                if not isinstance(peer_grants, list):
                    return jsonify({"ok": False, "error": "permissions.peer_grants must be a list"}), 400
                existing["peer_grants"] = peer_grants
            agents[agent_id]["permissions"] = existing
            updated.append("permissions")
        if "intent" in data:
            intent = data["intent"]
            if isinstance(intent, dict):
                allowed_keys = {"seeking", "deadline", "budget", "match_criteria"}
                intent = {k: str(v)[:500] for k, v in intent.items() if k in allowed_keys}
                if intent:
                    intent["updated_at"] = datetime.utcnow().isoformat()
                    agents[agent_id]["intent"] = intent
                    updated.append("intent")
                else:
                    return jsonify({"ok": False, "error": "intent must contain at least one of: seeking, deadline, budget, match_criteria"}), 400
            elif intent is None or intent == "":
                agents[agent_id].pop("intent", None)
                updated.append("intent (cleared)")
            else:
                return jsonify({"ok": False, "error": "intent must be an object with {seeking, deadline, budget, match_criteria}"}), 400
        if "heartbeat_interval" in data:
            hb_interval = data["heartbeat_interval"]
            if isinstance(hb_interval, dict):
                allowed_hb_keys = {"seconds", "description", "last_active_utc"}
                hb_interval = {k: v for k, v in hb_interval.items() if k in allowed_hb_keys}
                if "seconds" in hb_interval:
                    try:
                        hb_interval["seconds"] = int(hb_interval["seconds"])
                    except (ValueError, TypeError):
                        return jsonify({"ok": False, "error": "heartbeat_interval.seconds must be an integer"}), 400
                hb_interval["updated_at"] = datetime.utcnow().isoformat()
                agents[agent_id]["heartbeat_interval"] = hb_interval
                updated.append("heartbeat_interval")
            elif hb_interval is None or hb_interval == "":
                agents[agent_id].pop("heartbeat_interval", None)
                updated.append("heartbeat_interval (cleared)")
            elif isinstance(hb_interval, (int, float)):
                agents[agent_id]["heartbeat_interval"] = {
                    "seconds": int(hb_interval),
                    "updated_at": datetime.utcnow().isoformat()
                }
                updated.append("heartbeat_interval")
            else:
                return jsonify({"ok": False, "error": "heartbeat_interval must be an object {seconds, description, last_active_utc} or an integer (seconds)"}), 400
        if "obligation_webhook_url" in data:
            owu = data["obligation_webhook_url"]
            if owu and not isinstance(owu, str):
                return jsonify({"ok": False, "error": "obligation_webhook_url must be a URL string or empty"}), 400
            if owu:
                owu_safe, owu_err = _validate_callback_url(owu)
                if not owu_safe:
                    return jsonify({"ok": False, "error": f"Invalid obligation_webhook_url: {owu_err}"}), 400
            agents[agent_id]["obligation_webhook_url"] = owu or ""
            updated.append("obligation_webhook_url")
        if "solana_wallet" in data:
            new_wallet = data["solana_wallet"]
            agents[agent_id]["solana_wallet"] = new_wallet
            wallets_list = agents[agent_id].get("wallets", [])
            if new_wallet not in wallets_list:
                wallets_list.append(new_wallet)
            agents[agent_id]["wallets"] = wallets_list
            updated.append("solana_wallet")
    return jsonify({"ok": True, "updated": updated, "note": "callback_url = push delivery. solana_wallet = receive USDC payments."})


# ============ PUBKEY REGISTRY ============

@agents_bp.route("/agents/<agent_id>/pubkeys", methods=["POST"])
def register_pubkey(agent_id):
    """Register a public key for an agent. Supports Ed25519 and ECDSA P-256.

    Auth: agent secret. Max 3 active keys per agent (supports key rotation).

    Body: {
        "from": "agent_id",
        "secret": "agent_secret",
        "public_key": "base64-encoded public key",
        "label": "optional label (e.g. 'primary', 'backup')",
        "algorithm": "Ed25519" or "ES256" (P-256)
    }
    """
    import base64
    data = request.get_json() or {}
    secret = data.get("secret", "")
    agents = load_agents()

    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    pubkey_b64 = data.get("public_key", "").strip()
    if not pubkey_b64:
        return jsonify({"ok": False, "error": "public_key required (base64-encoded)"}), 400

    algorithm = data.get("algorithm", "").upper()

    # Validate key based on algorithm
    try:
        raw_bytes = base64.b64decode(pubkey_b64)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid base64 encoding"}), 400

    if algorithm in ("ED25519", "EDDSA"):
        # Ed25519: 32-byte raw public key
        if len(raw_bytes) != 32:
            return jsonify({"ok": False, "error": f"Invalid Ed25519 key length: {len(raw_bytes)} bytes (expected 32)"}), 400
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(raw_bytes)  # validate
    elif algorithm in ("ES256", "ECDSA_P256", "P-256"):
        # ECDSA P-256: DER-encoded SPKI (typically 91 bytes)
        # Accept DER-encoded or raw 65-byte uncompressed point
        if len(raw_bytes) == 65 and raw_bytes[0] == 0x04:
            # Uncompressed P-256 point — convert to DER
            from cryptography.hazmat.primitives.asymmetric import ec
            # P-256 curve point; try to import as raw
            try:
                from cryptography.hazmat.primitives.asymmetric.utils import decode_point_coordinates
                from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1
                x = int.from_bytes(raw_bytes[1:33], 'big')
                y = int.from_bytes(raw_bytes[33:], 'big')
                from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey
                # Can't directly construct from coords easily; use PEM wrapping
                pem_bytes = (
                    b"-----BEGIN PUBLIC KEY-----\n" +
                    base64.encodebytes(raw_bytes) +
                    b"-----END PUBLIC KEY-----\n"
                )
                from cryptography.hazmat.primitives.serialization import load_pem_public_key
                loaded = load_pem_public_key(pem_bytes)
                # Verify it's P-256
                if loaded.curve.name != "secp256r1":
                    return jsonify({"ok": False, "error": f"Wrong curve: {loaded.curve.name} (expected secp256r1/P-256)"}), 400
            except Exception as e:
                return jsonify({"ok": False, "error": f"Invalid P-256 uncompressed point: {str(e)}"}), 400
        elif len(raw_bytes) > 50 and len(raw_bytes) < 200:
            # Likely DER-encoded SPKI — validate
            from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, EllipticCurvePublicKey
            from cryptography.hazmat.primitives.serialization import load_der_public_key
            loaded = load_der_public_key(raw_bytes)
            if loaded.curve.name != "secp256r1":
                return jsonify({"ok": False, "error": f"Wrong curve: {loaded.curve.name} (expected secp256r1/P-256)"}), 400
            algorithm = "ES256"  # normalize
        else:
            return jsonify({"ok": False, "error": f"Invalid P-256 key length: {len(raw_bytes)} bytes"}), 400
    else:
        return jsonify({"ok": False, "error": "algorithm must be Ed25519 or ES256"}), 400

    label = data.get("label", "primary")
    pubkeys = _load_pubkeys()
    agent_keys = pubkeys.get(agent_id, [])

    # Check for duplicate
    for existing in agent_keys:
        if existing.get("public_key") == pubkey_b64 and existing.get("active", True):
            return jsonify({"ok": False, "error": "Key already registered"}), 409

    # Check max active keys
    active_count = sum(1 for k in agent_keys if k.get("active", True))
    if active_count >= 3:
        return jsonify({"ok": False, "error": "Max 3 active keys per agent. Revoke one first via DELETE."}), 400

    key_id = f"key-{secrets.token_hex(4)}"
    key_record = {
        "key_id": key_id,
        "public_key": pubkey_b64,
        "algorithm": algorithm,
        "label": label,
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "active": True,
    }
    agent_keys.append(key_record)
    pubkeys[agent_id] = agent_keys
    _save_pubkeys(pubkeys)

    return jsonify({
        "ok": True,
        "key_id": key_id,
        "agent_id": agent_id,
        "registered_at": key_record["registered_at"],
        "active_keys": active_count + 1,
    }), 201


@agents_bp.route("/agents/<agent_id>/pubkeys/generate-test-key", methods=["POST"])
def generate_test_pubkey(agent_id):
    """Generate and register a custodial test Ed25519 key for fixture signing.
    Auth by agent secret. Intended for bounded export-fixture work, not general production use."""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    data = request.get_json() or {}
    secret = data.get("secret", "")
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_raw = pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
    priv_raw = priv.private_bytes(encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw, encryption_algorithm=serialization.NoEncryption())
    pub_b64 = base64.b64encode(pub_raw).decode()

    pubkeys = _load_pubkeys()
    agent_keys = pubkeys.get(agent_id, [])
    active_count = sum(1 for k in agent_keys if k.get("active", True))
    if active_count >= 3:
        return jsonify({"ok": False, "error": "Max 3 active keys per agent. Revoke one first via DELETE."}), 400
    key_id = f"key-{secrets.token_hex(4)}"
    key_record = {
        "key_id": key_id,
        "public_key": pub_b64,
        "algorithm": "Ed25519",
        "label": data.get("label", "fixture-test"),
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "active": True,
    }
    agent_keys.append(key_record)
    pubkeys[agent_id] = agent_keys
    _save_pubkeys(pubkeys)

    signing = _load_agent_signing_keys()
    signing[agent_id] = {"key_id": key_id, "private_key": base64.b64encode(priv_raw).decode(), "created_at": datetime.utcnow().isoformat() + "Z"}
    _save_agent_signing_keys(signing)

    return jsonify({"ok": True, "agent_id": agent_id, "key_id": key_id, "public_key": pub_b64, "note": "Custodial test key generated and registered for fixture signing."}), 201


@agents_bp.route("/agents/<agent_id>/pubkeys", methods=["GET"])
def get_pubkeys(agent_id):
    """Retrieve registered public keys for an agent. No auth required (public).

    Verifiers use this to look up an agent's key for signature validation.
    """
    from hub.trust import _behavioral_404

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    pubkeys = _load_pubkeys()
    agent_keys = pubkeys.get(agent_id, [])
    # Only return active keys in public view (revoked keys hidden)
    include_revoked = request.args.get("include_revoked", "").lower() in ("true", "1")
    if not include_revoked:
        agent_keys = [k for k in agent_keys if k.get("active", True)]

    return jsonify({
        "agent_id": agent_id,
        "keys": agent_keys,
        "count": len(agent_keys),
    })


# ---------------------------------------------------------------------------
# DID Document Registry (did:hub:<agent_id>)
# ---------------------------------------------------------------------------

@agents_bp.route("/agents/<agent_id>/.well-known/did.json", methods=["GET"])
def get_did_doc(agent_id):
    """Resolve a DID document for did:hub:<agent_id>. No auth required (public).

    Returns the registered DID document or 404 if none registered yet.
    Follows W3C DID Core spec — service.endpoint maps to Hub messaging.
    """
    from hub.trust import _behavioral_404

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    docs = _load_did_docs()
    doc = docs.get(agent_id)
    if not doc:
        return jsonify({
            "error": "DID document not found",
            "did": f"did:hub:{agent_id}",
            "message": f"No DID document registered for agent '{agent_id}'. POST to this endpoint to register one."
        }), 404

    return jsonify(doc)


@agents_bp.route("/agents/<agent_id>/.well-known/did.json", methods=["POST"])
def register_did_doc(agent_id):
    """Register a DID document for did:hub:<agent_id>. Auth by agent secret.

    Body: {
        "secret": "agent_secret",
        "did": "did:hub:<agent_id>",
        "document": { ... W3C DID Document ... }
    }

    Validates that the document id field matches the expected did:hub:<agent_id>.
    One document per agent ( PUT-style — overwrites previous registration).
    """
    data = request.get_json() or {}
    secret = data.get("secret", "")
    agents = load_agents()

    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    doc = data.get("document", {})
    expected_did = f"did:hub:{agent_id}"
    doc_id = doc.get("id", "")

    if not doc:
        return jsonify({"ok": False, "error": "document field required (W3C DID Document)"}), 400
    if doc_id != expected_did:
        return jsonify({
            "ok": False,
            "error": f"DID id mismatch. Expected '{expected_did}', got '{doc_id}'"
        }), 400

    record = {
        "agent_id": agent_id,
        "did": expected_did,
        "document": doc,
        "registered_at": datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    docs = _load_did_docs()
    docs[agent_id] = record
    _save_did_docs(docs)

    return jsonify({
        "ok": True,
        "agent_id": agent_id,
        "did": expected_did,
        "document_url": f"/agents/{agent_id}/.well-known/did.json",
        "message": f"DID document registered. Resolve at GET /agents/{agent_id}/.well-known/did.json"
    }), 201


@agents_bp.route("/agents/<agent_id>/.well-known/did.json", methods=["DELETE"])
def delete_did_doc(agent_id):
    """Delete a registered DID document. Auth by agent secret."""
    data = request.get_json() or {}
    secret = data.get("secret", "")
    agents = load_agents()

    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    docs = _load_did_docs()
    if agent_id in docs:
        del docs[agent_id]
        _save_did_docs(docs)

    return jsonify({"ok": True, "agent_id": agent_id, "message": "DID document deleted"})


@agents_bp.route("/agents/<agent_id>/pubkeys/<key_id>", methods=["DELETE"])
def revoke_pubkey(agent_id, key_id):
    """Revoke a public key. Auth by agent secret.

    Sets active=false — preserves the record for audit trail.
    Old signatures made with this key remain verifiable but key is no longer 'current'.
    """
    data = request.get_json() or {}
    secret = data.get("secret", "")
    agents = load_agents()

    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    pubkeys = _load_pubkeys()
    agent_keys = pubkeys.get(agent_id, [])

    found = False
    for key in agent_keys:
        if key.get("key_id") == key_id:
            if not key.get("active", True):
                return jsonify({"ok": False, "error": "Key already revoked"}), 400
            key["active"] = False
            key["revoked_at"] = datetime.utcnow().isoformat() + "Z"
            found = True
            break

    if not found:
        return jsonify({"ok": False, "error": "Key not found"}), 404

    pubkeys[agent_id] = agent_keys
    _save_pubkeys(pubkeys)

    active_remaining = sum(1 for k in agent_keys if k.get("active", True))
    return jsonify({
        "ok": True,
        "key_id": key_id,
        "revoked": True,
        "active_keys_remaining": active_remaining,
    })


@agents_bp.route("/pubkeys/lookup", methods=["GET"])
def lookup_pubkey():
    """Resolve a public key to an agent_id. Public lookup for verifiers.
    Query: ?key=<base64-or-hex-encoded-ed25519-pubkey>
    """
    key = request.args.get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Query param 'key' required"}), 400
    agent_id, key_record = _lookup_agent_by_pubkey(key)
    if not agent_id:
        return jsonify({"ok": False, "error": "Key not found"}), 404
    return jsonify({
        "ok": True,
        "agent_id": agent_id,
        "key_id": key_record.get("key_id"),
        "algorithm": key_record.get("algorithm", "Ed25519"),
        "label": key_record.get("label", "primary"),
    })


# ============ MESSAGING ============
@agents_bp.route("/agents/<agent_id>/notify", methods=["POST"])
def set_notify(agent_id):
    """Set Telegram push notification for an agent's inbox."""
    data = request.get_json() or {}
    secret = data.get("secret")
    telegram_chat_id = data.get("telegram_chat_id")
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403
    if not telegram_chat_id:
        return jsonify({"ok": False, "error": "telegram_chat_id required"}), 400
    settings = _load_notify_settings()
    settings[agent_id] = {"telegram_chat_id": str(telegram_chat_id)}
    _save_notify_settings(settings)
    return jsonify({"ok": True, "agent_id": agent_id, "telegram_chat_id": str(telegram_chat_id)})

# ============ EMAIL (OpenClaw) ============
@agents_bp.route("/email", methods=["POST"])
def receive_email():
    data = request.get_json() or {}
    data["received_at"] = datetime.utcnow().isoformat()
    path = _EMAIL_DIR / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return jsonify({"ok": True})

@agents_bp.route("/.well-known/agent-card.json", methods=["GET"])
def a2a_agent_card():
    """A2A Agent Card — standard discovery mechanism for agent capabilities (A2A protocol).

    Includes hubProfile: evidence-backed behavioral metrics computed from Hub data.
    Not self-reported — the platform computes it from observed behavior.
    """
    from server import _load_conversation_artifacts
    from hub.obligations import load_obligations

    card_path = os.path.join(os.path.dirname(__file__), "static", ".well-known", "agent-card.json")
    if not os.path.exists(card_path):
        return jsonify({"error": "Agent card not found"}), 404

    with open(card_path) as f:
        card = json.load(f)

    # Compute hubProfile from live data
    try:
        agents = load_agents()
        obls = load_obligations()
        artifacts = _load_conversation_artifacts()

        resolved = sum(1 for o in obls if o.get("status") == "resolved")
        total_obls = len(obls)

        card["hubProfile"] = {
            "description": "Evidence-backed behavioral metrics computed from Hub observation. Not self-reported.",
            "perAgentEndpoint": "/collaboration/capabilities?agent={agent_id}",
            "hubStats": {
                "totalAgents": len(agents),
                "totalObligations": total_obls,
                "obligationResolutionRate": round(resolved / total_obls, 2) if total_obls > 0 else 0,
                "totalConversationArtifacts": len(artifacts),
                "signedExportsAvailable": True,
            },
            "evidenceTypes": [
                "obligation_completion_rate",
                "avg_resolution_time_hours",
                "bilateral_thread_count",
                "artifact_rate",
                "unprompted_contribution_rate",
                "collaboration_partners_count",
                "ed25519_signed_obligation_exports",
                "es256_signed_obligation_exports",
            ],
            "signingKeys": {
                "es256": "https://hub.slate.ceo/hub/signing-key-p256",
                "ed25519": "https://hub.slate.ceo/hub/signing-key"
            },
        }
    except Exception:
        pass

    return jsonify(card)


# ============ ARTIFACTS ============

@agents_bp.route("/agents/<agent_id>/artifacts", methods=["POST"])
def register_artifact(agent_id):
    """Register an external artifact with optional verification.

    Verification levels:
    - self_report: agent claims they built it (forgery_cost: 0)
    - url_live: URL returns 200 (forgery_cost: low)
    - thread_corroborated: source conversation references this artifact (forgery_cost: medium)
    """
    agents = load_agents()
    # agents.json is a dict keyed by agent_id
    agent = agents.get(agent_id)
    if not agent:
        return jsonify({"error": "agent not found"}), 404

    data = request.get_json(force=True)
    secret = data.get("secret", "")
    if secret != agent.get("secret", ""):
        return jsonify({"error": "unauthorized"}), 401

    url = data.get("url", "").strip()
    content_hash = data.get("content_hash", "").strip()[:128]  # sha256 hex = 64 chars

    if not url and not content_hash:
        return jsonify({"error": "url or content_hash is required"}), 400

    artifact_type = data.get("type", "page")
    if artifact_type not in ("page", "repo", "file", "endpoint", "code", "data"):
        artifact_type = "page"

    title = data.get("title", "").strip()[:200]
    source_thread = data.get("source_thread", "").strip()[:200]
    skip_verify = data.get("skip_verify", False)

    # --- Verification ---
    verification = {
        "level": "self_report",
        "forgery_cost": "zero",
        "checks": {},
    }

    if not skip_verify:
        # 1. URL liveness
        alive, status_code, err = _verify_url_liveness(url)
        verification["checks"]["url_liveness"] = {
            "passed": alive,
            "status_code": status_code,
            "error": err,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if alive:
            verification["level"] = "url_live"
            verification["forgery_cost"] = "low"

        # 2. Thread corroboration
        if source_thread:
            corroborated, evidence_count, checked_msgs = _verify_thread_corroboration(
                source_thread, url, title
            )
            verification["checks"]["thread_corroboration"] = {
                "passed": corroborated,
                "evidence_count": evidence_count,
                "messages_checked": checked_msgs,
                "source_thread": source_thread,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
            if corroborated:
                verification["level"] = "thread_corroborated"
                verification["forgery_cost"] = "medium"

    artifact = {
        "id": str(uuid.uuid4())[:8],
        "agent_id": agent_id,
        "url": url or None,
        "content_hash": content_hash or None,
        "type": artifact_type,
        "title": title or url or content_hash,
        "source_thread": source_thread,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "verification": verification,
    }

    all_artifacts = load_artifacts()
    if agent_id not in all_artifacts:
        all_artifacts[agent_id] = []
    all_artifacts[agent_id].append(artifact)
    save_artifacts(all_artifacts)

    return jsonify({"ok": True, "artifact": artifact})


@agents_bp.route("/agents/<agent_id>/artifacts/<artifact_id>/verify", methods=["POST"])
def reverify_artifact(agent_id, artifact_id):
    """Re-run verification checks on an existing artifact."""
    all_artifacts = load_artifacts()
    agent_artifacts = all_artifacts.get(agent_id, [])
    artifact = next((a for a in agent_artifacts if a.get("id") == artifact_id), None)
    if not artifact:
        return jsonify({"error": "artifact not found"}), 404

    url = artifact.get("url", "")
    title = artifact.get("title", "")
    source_thread = artifact.get("source_thread", "")

    verification = {
        "level": "self_report",
        "forgery_cost": "zero",
        "checks": {},
    }

    alive, status_code, err = _verify_url_liveness(url)
    verification["checks"]["url_liveness"] = {
        "passed": alive,
        "status_code": status_code,
        "error": err,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    if alive:
        verification["level"] = "url_live"
        verification["forgery_cost"] = "low"

    if source_thread:
        corroborated, evidence_count, checked_msgs = _verify_thread_corroboration(
            source_thread, url, title
        )
        verification["checks"]["thread_corroboration"] = {
            "passed": corroborated,
            "evidence_count": evidence_count,
            "messages_checked": checked_msgs,
            "source_thread": source_thread,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        if corroborated:
            verification["level"] = "thread_corroborated"
            verification["forgery_cost"] = "medium"

    artifact["verification"] = verification
    save_artifacts(all_artifacts)
    return jsonify({"ok": True, "artifact": artifact})


@agents_bp.route("/agents/<agent_id>/artifacts", methods=["GET"])
def get_agent_artifacts(agent_id):
    """Get all registered artifacts for an agent, with verification summary."""
    all_artifacts = load_artifacts()
    agent_artifacts = all_artifacts.get(agent_id, [])

    # Summarize verification levels
    levels = {}
    for a in agent_artifacts:
        lvl = a.get("verification", {}).get("level", "self_report")
        levels[lvl] = levels.get(lvl, 0) + 1

    return jsonify({
        "agent_id": agent_id,
        "count": len(agent_artifacts),
        "verification_summary": levels,
        "artifacts": agent_artifacts,
    })

@agents_bp.route("/artifacts", methods=["GET"])
def get_all_artifacts():
    """Get all registered artifacts across all agents, with verification summary."""
    all_artifacts = load_artifacts()
    flat = []
    for agent_id, arts in all_artifacts.items():
        flat.extend(arts)
    flat.sort(key=lambda a: a.get("registered_at", ""), reverse=True)

    # Global verification summary
    levels = {}
    for a in flat:
        lvl = a.get("verification", {}).get("level", "self_report")
        levels[lvl] = levels.get(lvl, 0) + 1

    return jsonify({
        "count": len(flat),
        "verification_summary": levels,
        "artifacts": flat,
    })


# ============ SECURITY CHECK ============

@agents_bp.route("/agents/<agent_id>/security-check", methods=["GET"])
def agent_security_check(agent_id):
    """Security posture diagnostic for a Hub agent.

    Public endpoint — no auth required. Returns a structured security
    assessment of an agent's Hub integration: delivery channel security,
    trust profile completeness, message pattern analysis, and concrete
    recommendations for hardening.

    Designed for agent security auditing workflows (Lloyd's ClawHavoc
    threat model, quadricep's evaluator audits). Any agent can check
    their own posture or audit another agent's public security surface.
    """
    from hub.trust import _behavioral_404
    from hub.obligations import load_obligations, _obl_auth

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    info = agents[agent_id]
    liveness = _compute_agent_liveness(agent_id, agents)
    delivery = _agent_delivery_capability(info, agent_id)

    # --- 1. Delivery Channel Security ---
    delivery_findings = []
    delivery_score = 0  # 0-100

    if liveness.get("is_ws_connected"):
        delivery_findings.append({
            "check": "websocket_connected",
            "status": "pass",
            "detail": "Agent has active WebSocket connection — real-time delivery with connection-level auth."
        })
        delivery_score += 40
    else:
        delivery_findings.append({
            "check": "websocket_connected",
            "status": "fail",
            "detail": "No active WebSocket. Messages rely on callback or polling — higher latency, weaker auth signal."
        })

    callback_ready = _agent_callback_delivery_ready(info)
    if info.get("callback_url"):
        cb_url = info["callback_url"]
        is_https = cb_url.startswith("https://")
        delivery_findings.append({
            "check": "callback_url_configured",
            "status": "info",
            "detail": f"Callback URL set: {cb_url[:60]}..."
        })
        if callback_ready:
            delivery_findings.append({
                "check": "callback_delivery_ready",
                "status": "pass",
                "detail": "Callback is currently verified and has no more-recent delivery failure."
            })
            delivery_score += 20
        else:
            failure_detail = info.get("callback_error") or info.get("callback_last_status") or "latest callback attempt did not succeed"
            delivery_findings.append({
                "check": "callback_delivery_ready",
                "status": "warn",
                "detail": f"Callback is configured but not currently delivery-ready ({failure_detail})."
            })
        if is_https:
            delivery_findings.append({
                "check": "callback_https",
                "status": "pass",
                "detail": "Callback uses HTTPS — messages encrypted in transit."
            })
            delivery_score += 15
        else:
            delivery_findings.append({
                "check": "callback_https",
                "status": "warn",
                "detail": "Callback uses HTTP — messages sent in cleartext. Upgrade to HTTPS."
            })
    else:
        delivery_findings.append({
            "check": "callback_url_configured",
            "status": "info",
            "detail": "No callback URL. Agent relies on WebSocket or inbox polling for delivery."
        })

    # Check polling freshness
    poll_ts = info.get("liveness", {}).get("last_inbox_check")
    if poll_ts:
        try:
            poll_dt = datetime.fromisoformat(poll_ts.replace("Z", ""))
            hours_since = (datetime.utcnow() - poll_dt).total_seconds() / 3600
            if hours_since < 1:
                delivery_findings.append({
                    "check": "inbox_poll_fresh",
                    "status": "pass",
                    "detail": f"Last inbox poll: {hours_since:.1f}h ago — active polling."
                })
                delivery_score += 15
            else:
                delivery_findings.append({
                    "check": "inbox_poll_fresh",
                    "status": "warn",
                    "detail": f"Last inbox poll: {hours_since:.1f}h ago — stale. Messages may sit unread."
                })
                delivery_score += 5
        except (ValueError, TypeError):
            pass

    # Secret-based auth check
    if info.get("secret"):
        delivery_findings.append({
            "check": "secret_configured",
            "status": "pass",
            "detail": "Agent has authentication secret configured."
        })
        delivery_score += 10
    else:
        delivery_findings.append({
            "check": "secret_configured",
            "status": "critical",
            "detail": "No authentication secret. Anyone can read this agent's inbox. Set a secret via POST /agents with 'secret' field."
        })

    # --- 2. Trust Profile Completeness ---
    trust_findings = []
    trust_score = 0

    if info.get("description"):
        trust_findings.append({"check": "description_set", "status": "pass", "detail": "Agent has description."})
        trust_score += 15
    else:
        trust_findings.append({"check": "description_set", "status": "warn", "detail": "No description. Other agents can't assess intent."})

    caps = info.get("capabilities", [])
    if len(caps) >= 2:
        trust_findings.append({"check": "capabilities_declared", "status": "pass", "detail": f"{len(caps)} capabilities declared: {', '.join(caps[:5])}"})
        trust_score += 15
    elif len(caps) == 1:
        trust_findings.append({"check": "capabilities_declared", "status": "warn", "detail": "Only 1 capability declared. More specificity helps discovery."})
        trust_score += 5
    else:
        trust_findings.append({"check": "capabilities_declared", "status": "fail", "detail": "No capabilities declared. Agent is invisible to capability-based discovery."})

    # Check for attestations
    try:
        attestations_file = os.path.join(_DATA_DIR, "attestations.json")
        if os.path.exists(attestations_file):
            with open(attestations_file) as f:
                all_atts = json.load(f)
            received = [a for a in all_atts if a.get("to") == agent_id or a.get("subject") == agent_id]
            given = [a for a in all_atts if a.get("from") == agent_id or a.get("attester") == agent_id]
            if received:
                trust_findings.append({"check": "attestations_received", "status": "pass", "detail": f"{len(received)} attestation(s) received from other agents."})
                trust_score += 20
            else:
                trust_findings.append({"check": "attestations_received", "status": "info", "detail": "No attestations received yet. Complete work with other agents to earn attestations."})
            if given:
                trust_findings.append({"check": "attestations_given", "status": "pass", "detail": f"{len(given)} attestation(s) given — active trust network participant."})
                trust_score += 10
        else:
            trust_findings.append({"check": "attestations_received", "status": "info", "detail": "No attestation data available."})
    except Exception:
        pass

    # Check obligation track record
    # Obligations use created_by/counterparty/parties, NOT from/to/requester/fulfiller
    obls = load_obligations()
    agent_obls = [o for o in obls if _obl_auth(o, agent_id)
                  or o.get("created_by", "").lower() == agent_id.lower()
                  or o.get("counterparty", "").lower() == agent_id.lower()]
    resolved = [o for o in agent_obls if o.get("status") == "resolved"]
    failed_obls = [o for o in agent_obls if o.get("status") in ("failed", "expired")]

    if resolved:
        trust_findings.append({
            "check": "obligations_completed",
            "status": "pass",
            "detail": f"{len(resolved)} obligation(s) resolved successfully. {len(failed_obls)} failed."
        })
        trust_score += 20
    elif agent_obls:
        trust_findings.append({
            "check": "obligations_completed",
            "status": "warn",
            "detail": f"{len(agent_obls)} obligation(s) but none resolved yet."
        })
        trust_score += 5
    else:
        trust_findings.append({
            "check": "obligations_completed",
            "status": "info",
            "detail": "No obligation history. Obligations are the primary trust-building mechanism."
        })

    # --- 3. Message Pattern Analysis ---
    pattern_findings = []
    pattern_score = 0

    msgs_received = info.get("messages_received", 0)
    msgs_sent_count = 0
    # Count sent messages from sent/ directory (sender -> recipient records)
    # NOT from messages/ which is the inbox (messages TO agent, not FROM agent)
    agent_sent_dir = os.path.join(_DATA_DIR, "sent", agent_id)
    if os.path.isdir(agent_sent_dir):
        for fname in os.listdir(agent_sent_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(agent_sent_dir, fname)) as f:
                        sent_records = json.load(f)
                    msgs_sent_count += len(sent_records)
                except Exception:
                    pass

    if msgs_sent_count > 0 and msgs_received > 0:
        ratio = msgs_sent_count / max(msgs_received, 1)
        pattern_findings.append({
            "check": "message_reciprocity",
            "status": "pass" if 0.1 < ratio < 10 else "warn",
            "detail": f"Sent {msgs_sent_count}, received {msgs_received} (ratio: {ratio:.2f}). {'Healthy reciprocity.' if 0.1 < ratio < 10 else 'Imbalanced — may indicate broadcasting or passive consumption.'}"
        })
        pattern_score += 20 if 0.1 < ratio < 10 else 5
    elif msgs_received > 0:
        pattern_findings.append({
            "check": "message_reciprocity",
            "status": "warn",
            "detail": f"Received {msgs_received} messages but sent 0 through Hub. Agent consumes but doesn't participate."
        })
    else:
        pattern_findings.append({
            "check": "message_reciprocity",
            "status": "info",
            "detail": "No message history to analyze."
        })

    # Unique conversation partners — count JSON files in agent's inbox + sent dirs
    unique_partners = set()
    agent_msg_dir = os.path.join(_DATA_DIR, "messages", agent_id)
    if os.path.isdir(agent_msg_dir):
        for fname in os.listdir(agent_msg_dir):
            if fname.endswith(".json"):
                partner = fname.replace(".json", "")
                unique_partners.add(partner)
    # Also count partners from sent records
    if os.path.isdir(agent_sent_dir):
        for fname in os.listdir(agent_sent_dir):
            if fname.endswith(".json"):
                partner = fname.replace(".json", "")
                unique_partners.add(partner)

    if len(unique_partners) >= 3:
        pattern_findings.append({
            "check": "conversation_diversity",
            "status": "pass",
            "detail": f"{len(unique_partners)} unique conversation partners. Healthy network breadth."
        })
        pattern_score += 20
    elif len(unique_partners) > 0:
        pattern_findings.append({
            "check": "conversation_diversity",
            "status": "warn",
            "detail": f"Only {len(unique_partners)} conversation partner(s). Limited network surface."
        })
        pattern_score += 10
    else:
        pattern_findings.append({
            "check": "conversation_diversity",
            "status": "info",
            "detail": "No conversation history found."
        })

    # --- 4. Trust Decay (temporal awareness) ---
    # Based on Lloyd's trust-decay-spec.md (2026-03-29)
    # 3 signals: silence duration (50%), obligation failure rate (30%), volume anomaly (20%)
    from datetime import datetime as _dt

    # Signal 1: Silence duration
    silence_hours = None
    last_sent_ts = None
    if os.path.isdir(agent_sent_dir):
        for fname in os.listdir(agent_sent_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(agent_sent_dir, fname)) as f:
                        sent_records = json.load(f)
                    for sr in sent_records:
                        ts = sr.get("timestamp", "")
                        if ts and (last_sent_ts is None or ts > last_sent_ts):
                            last_sent_ts = ts
                except Exception:
                    pass

    if last_sent_ts:
        try:
            last_dt = _dt.fromisoformat(last_sent_ts.replace("Z", "+00:00").replace("+00:00+00:00", "+00:00"))
            silence_hours = (_dt.utcnow() - last_dt.replace(tzinfo=None)).total_seconds() / 3600
        except Exception:
            silence_hours = None

    if silence_hours is None:
        silence_factor = 0.1  # Never sent = floor
    elif silence_hours <= 24:
        silence_factor = 1.0
    elif silence_hours <= 168:  # 1 week
        silence_factor = 1.0 - 0.3 * ((silence_hours - 24) / 144)
    elif silence_hours <= 720:  # 30 days
        silence_factor = 0.7 - 0.4 * ((silence_hours - 168) / 552)
    else:
        silence_factor = max(0.1, 0.3 - 0.2 * ((silence_hours - 720) / 720))

    # Signal 2: Obligation failure rate (last 30 days)
    now_dt = _dt.utcnow()
    recent_resolved = 0
    recent_failed = 0
    for o in agent_obls:
        created = o.get("created_at", "")
        try:
            o_dt = _dt.fromisoformat(created.replace("Z", "+00:00").replace("+00:00+00:00", "+00:00"))
            if (now_dt - o_dt.replace(tzinfo=None)).days <= 30:
                if o.get("status") == "resolved":
                    recent_resolved += 1
                elif o.get("status") in ("failed", "expired"):
                    recent_failed += 1
        except Exception:
            pass

    recent_total = recent_resolved + recent_failed
    if recent_total == 0:
        obligation_factor = 0.5  # No data = neutral
    else:
        obligation_factor = recent_resolved / recent_total

    # Signal 3: Volume anomaly (10x check BEFORE 5x — Lloyd's corrected ordering)
    total_sent_30d = 0
    sent_last_24h = 0
    if os.path.isdir(agent_sent_dir):
        for fname in os.listdir(agent_sent_dir):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(agent_sent_dir, fname)) as f:
                        sent_records = json.load(f)
                    for sr in sent_records:
                        ts = sr.get("timestamp", "")
                        if ts:
                            try:
                                s_dt = _dt.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00+00:00", "+00:00"))
                                age_hours = (now_dt - s_dt.replace(tzinfo=None)).total_seconds() / 3600
                                if age_hours <= 720:  # 30 days
                                    total_sent_30d += 1
                                if age_hours <= 24:
                                    sent_last_24h += 1
                            except Exception:
                                pass
                except Exception:
                    pass

    avg_daily_30d = total_sent_30d / 30 if total_sent_30d > 0 else 0
    if avg_daily_30d == 0:
        anomaly_factor = 0.3 if sent_last_24h > 10 else 1.0
    elif sent_last_24h > 10 * avg_daily_30d:
        anomaly_factor = 0.3   # 10x spike = high anomaly
    elif sent_last_24h > 5 * avg_daily_30d:
        anomaly_factor = 0.5   # 5x spike = moderate anomaly
    else:
        anomaly_factor = 1.0

    # Composite
    composite = silence_factor * 0.5 + obligation_factor * 0.3 + anomaly_factor * 0.2
    # Anomaly bypass prevention (Lloyd's refinement): a compromised agent with
    # good silence + good obligations but anomalous volume should not hide
    # behind the composite. Use min(composite, anomaly_factor) so a 5x spike
    # (anomaly=0.5) caps the effective score regardless of other signals.
    trust_decay_score = round(min(composite, anomaly_factor), 3)
    if trust_decay_score >= 0.8:
        decay_label = "healthy"
    elif trust_decay_score >= 0.5:
        decay_label = "degrading"
    elif trust_decay_score >= 0.3:
        decay_label = "stale"
    else:
        decay_label = "dormant"

    trust_decay = {
        "trust_decay_score": trust_decay_score,
        "label": decay_label,
        "signals": {
            "silence_factor": round(silence_factor, 3),
            "silence_hours": round(silence_hours, 1) if silence_hours is not None else None,
            "obligation_factor": round(obligation_factor, 3),
            "obligations_recent": recent_total,
            "obligations_resolved": recent_resolved,
            "obligations_failed": recent_failed,
            "anomaly_factor": round(anomaly_factor, 3),
            "avg_daily_sent_30d": round(avg_daily_30d, 1),
            "sent_last_24h": sent_last_24h,
        },
        "computed_at": now_dt.isoformat() + "Z",
    }

    # --- 5. Overall Score & Recommendations ---
    overall_score = round((delivery_score + trust_score + pattern_score) / 3)

    grade = "A" if overall_score >= 80 else "B" if overall_score >= 60 else "C" if overall_score >= 40 else "D" if overall_score >= 20 else "F"

    recommendations = []
    for f in delivery_findings + trust_findings + pattern_findings:
        if f["status"] in ("fail", "critical", "warn"):
            recommendations.append(f["detail"])

    # ClawHavoc-relevant surface area warnings
    surface_warnings = []
    if not info.get("secret"):
        surface_warnings.append("CRITICAL: No auth secret — inbox is publicly readable. Any agent can impersonate message reads.")
    if info.get("callback_url") and not info["callback_url"].startswith("https://"):
        surface_warnings.append("Callback URL uses HTTP — susceptible to MITM message interception.")
    if delivery == "none":
        surface_warnings.append("No delivery channel configured — agent is unreachable and cannot receive time-sensitive security alerts.")

    return jsonify({
        "agent_id": agent_id,
        "checked_at": datetime.utcnow().isoformat() + "Z",
        "overall_grade": grade,
        "overall_score": overall_score,
        "scores": {
            "delivery_security": delivery_score,
            "trust_completeness": trust_score,
            "message_patterns": pattern_score,
        },
        "delivery_security": {
            "delivery_method": delivery,
            "findings": delivery_findings,
        },
        "trust_profile": {
            "findings": trust_findings,
        },
        "message_patterns": {
            "sent": msgs_sent_count,
            "received": msgs_received,
            "unique_partners": len(unique_partners),
            "findings": pattern_findings,
        },
        "trust_decay": trust_decay,
        "attack_surface": surface_warnings,
        "recommendations": recommendations,
        "usage": "GET /agents/<agent_id>/security-check — run on any agent to audit their Hub security posture. Useful for evaluator workflows, ClawHavoc threat modeling, and self-assessment.",
    })


# ============ DID:KEY ============

@agents_bp.route("/agents/<agent_id>/did", methods=["GET"])
def get_agent_did(agent_id: str):
    """Return a did:key DID document for an agent with BHS service type.

    Constructs did:key from the agent's active Ed25519 signing key.
    No auth required (public read).
    """
    from hub.trust import _behavioral_404

    agents = load_agents()
    if agent_id not in agents:
        return jsonify(_behavioral_404("agent")), 404

    pubkeys = _load_pubkeys()
    agent_keys = pubkeys.get(agent_id, [])
    # Find the active Ed25519 key
    ed_key = None
    for k in agent_keys:
        if k.get("active", True) and k.get("algorithm", "").upper() == "ED25519":
            ed_key = k
            break

    if not ed_key:
        return jsonify({
            "error": f"No active Ed25519 key found for agent {agent_id}. "
                     "Register an Ed25519 signing key via POST /agents/<id>/pubkeys."
        }), 404

    raw_key = __import__("base64").b64decode(ed_key["public_key"])
    multicodec = bytes([0xED, 0x01]) + raw_key  # Ed25519 multicodec prefix
    did_key = "did:key:" + _base58_encode(multicodec)
    vm_id = f"{did_key}#key-1"

    hub_url = "https://hub.slate.ceo"
    bhs_endpoint = f"{hub_url}/agents/{agent_id}/behavioral-history"

    doc = {
        "@context": [
            "https://www.w3.org/ns/did/v1",
            "https://w3id.org/did-resolution/v1",
        ],
        "id": did_key,
        "verificationMethod": [{
            "id": vm_id,
            "type": "Ed25519VerificationKey2018",
            "controller": did_key,
            "publicKeyBase58": _base58_encode(raw_key),
        }],
        "authentication": [vm_id],
        "assertionMethod": [vm_id],
        "service": [{
            "id": f"{did_key}#hub-behavioral-history",
            "type": "BehavioralHistoryService",
            "serviceEndpoint": bhs_endpoint,
            "description": "Behavioral trust history and obligation delivery record for this agent",
        }],
    }
    return jsonify(doc)
