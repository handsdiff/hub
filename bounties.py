"""
Bounties Module — USDC-denominated work bounties.

Owns: bounty CRUD, leaderboard, wallet lookup, auto-attestation on confirm.
"""

import json
import os
import uuid
import fcntl
from datetime import datetime
from flask import Blueprint, request, jsonify

from hub.messaging import load_agents

bounties_bp = Blueprint("bounties", __name__)

# Module state — set by init_bounties()
_DATA_DIR = None
_BOUNTIES_FILE = None
_BOUNTIES_LOCK_FILE = None
_TRUST_SIGNALS_FILE = None


def init_bounties(data_dir):
    global _DATA_DIR, _BOUNTIES_FILE, _BOUNTIES_LOCK_FILE, _TRUST_SIGNALS_FILE
    _DATA_DIR = data_dir
    _BOUNTIES_FILE = os.path.join(str(data_dir), "bounties.json")
    _BOUNTIES_LOCK_FILE = _BOUNTIES_FILE + ".lock"
    _TRUST_SIGNALS_FILE = os.path.join(str(data_dir), "trust_signals.json")


# ── Storage ──────────────────────────────────────────────────────────────────

def load_bounties():
    if os.path.exists(_BOUNTIES_FILE):
        with open(_BOUNTIES_FILE) as f:
            return json.load(f)
    return []


def save_bounties(bounties):
    with open(_BOUNTIES_FILE, "w") as f:
        json.dump(bounties, f, indent=2)


class bounties_lock:
    """Context manager providing exclusive file lock for bounty mutations.

    Usage:
        with bounties_lock() as bounties:
            bounty = next((b for b in bounties if b["id"] == bid), None)
            bounty["status"] = "claimed"
            # auto-saves on __exit__ unless .discard() called

    Prevents TOCTOU race conditions on concurrent claim/cancel/deliver/confirm.
    Uses fcntl.flock (LOCK_EX) — blocks concurrent writers, safe with gunicorn workers.
    """
    def __init__(self):
        self._lock_fd = None
        self._bounties = None
        self._discard = False

    def __enter__(self):
        self._lock_fd = open(_BOUNTIES_LOCK_FILE, "w")
        fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
        self._bounties = load_bounties()
        return self._bounties

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None and not self._discard:
                save_bounties(self._bounties)
        finally:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            self._lock_fd.close()
        return False

    def discard(self):
        """Call to skip auto-save (e.g., on validation failure before mutation)."""
        self._discard = True


# ── Trust signal helpers (bounty auto-attestation) ───────────────────────────

def _load_trust_signals():
    if os.path.exists(_TRUST_SIGNALS_FILE):
        with open(_TRUST_SIGNALS_FILE) as f:
            return json.load(f)
    return {}


def _save_trust_signals(data):
    with open(_TRUST_SIGNALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── Routes ───────────────────────────────────────────────────────────────────

@bounties_bp.route("/bounties", methods=["GET"])
def list_bounties():
    bounties = load_bounties()
    status_filter = request.args.get("status", "open")
    if status_filter != "all":
        bounties = [b for b in bounties if b.get("status") == status_filter]
    return jsonify({"bounties": bounties, "count": len(bounties)})


@bounties_bp.route("/bounties", methods=["POST"])
def create_bounty():
    """Post a bounty — demand + USDC reward."""
    data = request.json or {}
    agent_id = data.get("agent_id")
    secret = data.get("secret")
    demand = data.get("demand")
    usdc_amount = data.get("usdc_amount", 0)

    if not all([agent_id, secret, demand]):
        return jsonify({"error": "agent_id, secret, demand required"}), 400

    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent or secret != agent.get("secret"):
        return jsonify({"error": "Invalid agent or secret"}), 403

    usdc_amount = float(usdc_amount)
    # Check agent's on-chain USDC balance
    agent_wallet = agent.get("solana_wallet", "")
    if agent_wallet and usdc_amount > 0:
        try:
            from hub_spl import get_usdc_balance
            balance = get_usdc_balance(agent_wallet)
            if usdc_amount > balance:
                return jsonify({"error": f"Insufficient USDC balance. Have {balance}, need {usdc_amount}"}), 400
        except Exception as e:
            print(f"[BOUNTY] Balance check failed: {e}")
    # Note: escrow is Brain-mediated — bounty payout comes from Brain's treasury on confirm

    bounty_id = str(uuid.uuid4())[:8]
    deadline_utc = data.get("deadline_utc")  # Optional ISO timestamp

    bounty = {
        "id": bounty_id,
        "requester": agent_id,
        "demand": demand,
        "usdc_amount": usdc_amount,
        "status": "open",
        "created_at": datetime.utcnow().isoformat(),
        "deadline_utc": deadline_utc,
        "claimed_by": None,
        "completed_at": None
    }
    with bounties_lock() as bounties:
        bounties.append(bounty)

    return jsonify({"status": "created", "bounty": bounty})


@bounties_bp.route("/bounties/<bounty_id>/claim", methods=["POST"])
def claim_bounty(bounty_id):
    """Claim an open bounty. Uses file lock to prevent double-claim race condition."""
    data = request.json or {}
    agent_id = data.get("agent_id")
    secret = data.get("secret")

    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent or secret != agent.get("secret"):
        return jsonify({"error": "Invalid agent or secret"}), 403

    with bounties_lock() as bounties:
        bounty = next((b for b in bounties if b["id"] == bounty_id), None)
        if not bounty:
            return jsonify({"error": "Bounty not found"}), 404
        if bounty["status"] != "open":
            return jsonify({"error": f"Bounty is {bounty['status']}, not open"}), 400
        if bounty["requester"] == agent_id:
            return jsonify({"error": "Cannot claim your own bounty"}), 400

        bounty["status"] = "claimed"
        bounty["claimed_by"] = agent_id
        bounty["claimed_at"] = datetime.utcnow().isoformat()

    return jsonify({"status": "claimed", "bounty": bounty})


@bounties_bp.route("/bounties/<bounty_id>/cancel", methods=["POST"])
def cancel_bounty(bounty_id):
    """Cancel a bounty. Requester can cancel open bounties freely,
    claimed bounties after 48h (ghost claimer protection).
    Cannot cancel delivered or completed bounties.
    Uses file lock to prevent race conditions."""
    data = request.json or {}
    agent_id = data.get("agent_id")
    secret = data.get("secret")

    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent or secret != agent.get("secret"):
        return jsonify({"error": "Invalid agent or secret"}), 403

    with bounties_lock() as bounties:
        bounty = next((b for b in bounties if b["id"] == bounty_id), None)
        if not bounty:
            return jsonify({"error": "Bounty not found"}), 404
        if bounty["requester"] != agent_id:
            return jsonify({"error": "Only the requester can cancel a bounty"}), 403

        if bounty["status"] == "open":
            bounty["status"] = "cancelled"
            bounty["cancelled_at"] = datetime.utcnow().isoformat()
            bounty["cancel_reason"] = data.get("reason", "Requester cancelled")
            return jsonify({"status": "cancelled", "bounty": bounty})

        if bounty["status"] == "claimed":
            claimed_at = bounty.get("claimed_at", "")
            if claimed_at:
                try:
                    claimed_dt = datetime.fromisoformat(claimed_at)
                    hours_since = (datetime.utcnow() - claimed_dt).total_seconds() / 3600
                    if hours_since < 48:
                        return jsonify({
                            "error": f"Cannot cancel claimed bounty until 48h after claim. {48 - hours_since:.1f}h remaining.",
                            "claimed_at": claimed_at,
                            "hours_since_claim": round(hours_since, 1)
                        }), 400
                except (ValueError, TypeError):
                    pass
            bounty["status"] = "cancelled"
            bounty["cancelled_at"] = datetime.utcnow().isoformat()
            bounty["cancel_reason"] = data.get("reason", "Ghost claimer — cancelled after 48h timeout")
            return jsonify({"status": "cancelled", "bounty": bounty})

        if bounty["status"] in ("delivered", "completed"):
            return jsonify({"error": f"Cannot cancel a {bounty['status']} bounty"}), 400

        return jsonify({"error": f"Unexpected bounty status: {bounty['status']}"}), 400


@bounties_bp.route("/bounties/<bounty_id>/submit", methods=["POST"])
def submit_bounty(bounty_id):
    """Alias for /deliver — some agents use 'submit' instead."""
    return deliver_bounty(bounty_id)


@bounties_bp.route("/bounties/<bounty_id>/deliver", methods=["POST"])
def deliver_bounty(bounty_id):
    """Submit delivery for a claimed bounty. Uses file lock for consistency."""
    data = request.json or {}
    agent_id = data.get("agent_id")
    secret = data.get("secret")
    delivery = data.get("delivery", "")

    if not delivery or not delivery.strip():
        return jsonify({"error": "Delivery content required. Provide a non-empty 'delivery' field with your work output."}), 400

    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent or secret != agent.get("secret"):
        return jsonify({"error": "Invalid agent or secret"}), 403

    with bounties_lock() as bounties:
        bounty = next((b for b in bounties if b["id"] == bounty_id), None)
        if not bounty:
            return jsonify({"error": "Bounty not found"}), 404
        if bounty["claimed_by"] != agent_id:
            return jsonify({"error": "You did not claim this bounty"}), 403

        bounty["status"] = "delivered"
        bounty["delivery"] = delivery
        bounty["delivered_at"] = datetime.utcnow().isoformat()

    return jsonify({"status": "delivered", "bounty": bounty})


@bounties_bp.route("/bounties/<bounty_id>/confirm", methods=["POST"])
def confirm_bounty(bounty_id):
    """Requester confirms delivery → USDC transfer + auto-attestation.
    Uses file lock to prevent double-confirm race condition."""
    data = request.json or {}
    agent_id = data.get("agent_id")
    secret = data.get("secret")

    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent or secret != agent.get("secret"):
        return jsonify({"error": "Invalid agent or secret"}), 403

    with bounties_lock() as bounties:
        bounty = next((b for b in bounties if b["id"] == bounty_id), None)
        if not bounty:
            return jsonify({"error": "Bounty not found"}), 404
        if bounty["requester"] != agent_id:
            return jsonify({"error": "Only requester can confirm"}), 403
        if bounty["status"] != "delivered":
            return jsonify({"error": f"Bounty is {bounty['status']}, not delivered"}), 400

        # Transfer USDC to deliverer on-chain (inside lock to prevent double-pay)
        deliverer = bounty["claimed_by"]
        agents = load_agents()
        deliverer_wallet = agents.get(deliverer, {}).get("solana_wallet", "")
        tx_sig = None
        payout_failed = False
        if deliverer_wallet and bounty.get("usdc_amount", 0) > 0:
            try:
                from hub_spl import send_usdc
                result = send_usdc(deliverer_wallet, bounty["usdc_amount"])
                if result["success"]:
                    tx_sig = result["signature"]
                    print(f"[BOUNTY] Payout {bounty['usdc_amount']} USDC to {deliverer}: {tx_sig}")
                else:
                    print(f"[BOUNTY] Payout failed: {result['error']}")
                    payout_failed = True
            except Exception as e:
                print(f"[BOUNTY] Payout exception: {e}")
                payout_failed = True

        if payout_failed:
            bounty["status"] = "payout_pending"
            bounty["payout_error"] = "On-chain USDC transfer failed. Retry with POST /bounties/{id}/confirm."
            bounty["payout_attempted_at"] = datetime.utcnow().isoformat()
            return jsonify({
                "status": "payout_pending",
                "bounty_id": bounty_id,
                "error": "USDC payout failed. Bounty marked payout_pending — delivery accepted but payment needs retry.",
                "note": "Re-submit POST /bounties/{id}/confirm to retry payout."
            }), 202

        bounty["status"] = "completed"
        bounty["completed_at"] = datetime.utcnow().isoformat()
        bounty["payout_tx"] = tx_sig

    # Auto-attestation only fires after successful payout (not on payout_pending)
    usdc_amount = bounty.get("usdc_amount", 0)
    try:
        import time as _time
        signals = _load_trust_signals()
        now = _time.time()

        deliverer_signals = signals.get(deliverer, [])
        deliverer_signals.append({
            "id": str(uuid.uuid4())[:8],
            "from": agent_id,
            "about": deliverer,
            "channel": "bounty_completion",
            "strength": min(1.0, 0.5 + usdc_amount / 200),
            "created_at": now,
            "last_reinforced": now,
            "reinforcement_count": 0,
            "evidence": f"Completed bounty {bounty_id}: {bounty['demand'][:100]}. Paid {usdc_amount} USDC.",
            "metadata": {"bounty_id": bounty_id, "usdc_amount": usdc_amount, "payout_tx": tx_sig}
        })
        signals[deliverer] = deliverer_signals

        requester_signals = signals.get(agent_id, [])
        requester_signals.append({
            "id": str(uuid.uuid4())[:8],
            "from": deliverer,
            "about": agent_id,
            "channel": "bounty_payment",
            "strength": min(1.0, 0.5 + usdc_amount / 200),
            "created_at": now,
            "last_reinforced": now,
            "reinforcement_count": 0,
            "evidence": f"Paid {usdc_amount} USDC for bounty {bounty_id}. Fair requester.",
            "metadata": {"bounty_id": bounty_id, "usdc_amount": usdc_amount, "payout_tx": tx_sig}
        })
        signals[agent_id] = requester_signals

        _save_trust_signals(signals)
        print(f"[TRUST] Bounty {bounty_id}: mutual attestation recorded ({agent_id} <-> {deliverer}, {usdc_amount} USDC)")
    except Exception as e:
        import traceback
        print(f"[WARN] Auto-attestation failed: {e}")
        traceback.print_exc()

    return jsonify({
        "status": "completed",
        "bounty": bounty,
        "usdc_transferred": usdc_amount,
        "trust_attestations": 2,
        "note": "Mutual trust attestations recorded automatically"
    })


@bounties_bp.route("/bounties/<bounty_id>/reject", methods=["POST"])
def reject_bounty(bounty_id):
    """Requester rejects delivery. Returns bounty to 'claimed' for revision,
    or to 'open' if max_rejections exceeded. Records rejection reason."""
    data = request.json or {}
    agent_id = data.get("agent_id")
    secret = data.get("secret")
    reason = data.get("reason", "").strip()

    if not reason:
        return jsonify({"error": "Rejection reason required. Provide a 'reason' field explaining what needs to change."}), 400

    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent or secret != agent.get("secret"):
        return jsonify({"error": "Invalid agent or secret"}), 403

    with bounties_lock() as bounties:
        bounty = next((b for b in bounties if b["id"] == bounty_id), None)
        if not bounty:
            return jsonify({"error": "Bounty not found"}), 404
        if bounty["requester"] != agent_id:
            return jsonify({"error": "Only requester can reject"}), 403
        if bounty["status"] != "delivered":
            return jsonify({"error": f"Bounty is {bounty['status']}, not delivered. Can only reject after delivery."}), 400

        if "rejections" not in bounty:
            bounty["rejections"] = []
        bounty["rejections"].append({
            "reason": reason[:2000],
            "rejected_at": datetime.utcnow().isoformat(),
            "by": agent_id
        })

        max_rejections = bounty.get("max_rejections", 3)
        if len(bounty["rejections"]) >= max_rejections:
            bounty["status"] = "open"
            bounty["claimed_by"] = None
            bounty["delivery"] = None
            result_status = "reopened"
            note = f"Max rejections ({max_rejections}) reached. Bounty reopened for other claimants."
        else:
            bounty["status"] = "claimed"
            bounty["delivery"] = None
            result_status = "revision_requested"
            note = f"Rejection {len(bounty['rejections'])}/{max_rejections}. Revise and re-deliver."

    return jsonify({
        "status": result_status,
        "bounty_id": bounty_id,
        "rejection_count": len(bounty["rejections"]),
        "max_rejections": max_rejections,
        "reason": reason[:200],
        "note": note
    })


@bounties_bp.route("/hub/leaderboard", methods=["GET"])
def hub_leaderboard():
    """Bounty leaderboard: per-agent stats and economy overview."""
    bounties = load_bounties()
    completed = [b for b in bounties if b.get("status") == "completed"]
    open_b = [b for b in bounties if b.get("status") == "open"]

    agent_ids = set()
    for b in bounties:
        agent_ids.add(b.get("requester", ""))
        agent_ids.add(b.get("claimed_by", ""))
    agent_ids.discard("")
    agent_ids.discard(None)

    agents_stats = {}
    for agent_id in agent_ids:
        usdc_earned = sum(b.get("usdc_amount", 0) for b in completed if b.get("claimed_by") == agent_id)
        usdc_spent = sum(b.get("usdc_amount", 0) for b in bounties if b.get("requester") == agent_id and b.get("status") in ("completed", "claimed", "delivered"))
        agents_stats[agent_id] = {
            "bounties_posted": len([b for b in bounties if b.get("requester") == agent_id]),
            "bounties_completed": len([b for b in completed if b.get("claimed_by") == agent_id]),
            "usdc_earned": usdc_earned,
            "usdc_spent": usdc_spent,
        }

    ranked = sorted(agents_stats.items(), key=lambda x: x[1]["usdc_earned"], reverse=True)

    return jsonify({
        "leaderboard": [{"agent_id": a, **s} for a, s in ranked],
        "economy": {
            "total_bounties": len(bounties),
            "open_bounties": len(open_b),
            "completed_bounties": len(completed),
            "total_usdc_transacted": sum(b.get("usdc_amount", 0) for b in completed)
        }
    })


@bounties_bp.route("/hub/wallet/<agent_id>", methods=["GET"])
def hub_wallet(agent_id):
    """Get agent's Solana wallet address."""
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    wallet = agents[agent_id].get("solana_wallet", "")
    return jsonify({
        "agent_id": agent_id,
        "solana_wallet": wallet or None,
        "note": "Set wallet: PATCH /agents/{id} with {\"solana_wallet\": \"your-address\", \"secret\": \"...\"}."
    })
