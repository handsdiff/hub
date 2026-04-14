"""
Obligations Module — Obligation lifecycle, ghost protocol, settlement, evidence.

Owns: obligation state machine, ghost counterparty protocol, settlement queue,
      evidence handling, checkpoints, reviewers, paylock webhook,
      commitment registry, verification friction, background processors.
"""

import json
import os
import threading
import uuid
import hashlib
import traceback
from datetime import datetime, timedelta, timezone
from flask import Blueprint, request, jsonify

from hub.messaging import (
    deliver_message,
    load_agents, save_agents,
    load_inbox,
    _validate_callback_url,
    iter_message_records,
)
from hub.analytics import _log_frame_check

obligations_bp = Blueprint("obligations", __name__)

# Module state -- set by init_obligations()
_DATA_DIR = None
_HUB_SECRET = None
OBLIGATIONS_FILE = None
COMMITMENTS_FILE = None
FRICTION_DATA_PATH = None
PAYLOCK_WEBHOOK_SECRET = ""


def init_obligations(data_dir, hub_secret=None):
    global _DATA_DIR, _HUB_SECRET, OBLIGATIONS_FILE, COMMITMENTS_FILE
    global FRICTION_DATA_PATH, PAYLOCK_WEBHOOK_SECRET
    _DATA_DIR = data_dir
    _HUB_SECRET = hub_secret
    OBLIGATIONS_FILE = os.path.join(str(data_dir), "obligations.json")
    COMMITMENTS_FILE = os.path.join(str(data_dir), "commitments.json")
    FRICTION_DATA_PATH = os.path.join(str(data_dir), "verification_friction.json")
    PAYLOCK_WEBHOOK_SECRET = os.environ.get("PAYLOCK_WEBHOOK_SECRET", "")
    # Start background processors
    _start_settlement_processor()
    _start_watchdog_timer()


def _deliver_internal_dm(from_agent, to_agent, message, msg_type="system", extra=None):
    """Deliver an internal Hub DM. Thin wrapper around deliver_message()."""
    try:
        msg_extra = {"type": msg_type}
        if extra:
            msg_extra.update(extra)
        result = deliver_message(from_agent, to_agent, message, extra=msg_extra)
        if not result.get("ok"):
            print(f"[INTERNAL-DM] deliver_message failed for {from_agent}->{to_agent}: {result.get('error')}")
    except Exception as e:
        print(f"[INTERNAL-DM] Error sending {from_agent}->{to_agent}: {e}")


def _send_system_dm(to_agent, message, msg_type="system", extra=None):
    """Send a hub-system DM. Best-effort — failures are logged but never raised."""
    _deliver_internal_dm("hub-system", to_agent, message, msg_type, extra)


def load_obligations():
    if os.path.exists(OBLIGATIONS_FILE):
        with open(OBLIGATIONS_FILE) as f:
            return json.load(f)
    return []

def save_obligations(obls):
    with open(OBLIGATIONS_FILE, "w") as f:
        json.dump(obls, f, indent=2)

def load_commitments():
    if os.path.exists(COMMITMENTS_FILE):
        with open(COMMITMENTS_FILE) as f:
            return json.load(f)
    return []

def save_commitments(commits):
    with open(COMMITMENTS_FILE, "w") as f:
        json.dump(commits, f, indent=2)

# Valid status transitions (reducer rules from the spec)
_OBL_TRANSITIONS = {
    "proposed":           ["accepted", "rejected", "withdrawn", "failed", "expired"],
    "accepted":           ["evidence_submitted", "failed", "ghost_nudged"],
    "ghost_nudged":       ["accepted", "evidence_submitted", "failed", "ghost_escalated"],
    "ghost_escalated":    ["accepted", "evidence_submitted", "failed", "ghost_defaulted"],
    "ghost_defaulted":    ["resolved", "failed"],
    "evidence_submitted": ["resolved", "disputed", "failed", "expired"],
    "disputed":           ["evidence_submitted", "resolved", "failed"],
    # deadline_elapsed: claimant_self_resolve policy allows resolution from here
    "deadline_elapsed":   ["resolved", "failed"],
    # terminal states – no transitions out
    "resolved":           [],
    "rejected":           [],
    "withdrawn":          [],
    "failed":             [],
    "timed_out":          [],
    "expired":            [],  # Ghost Counterparty Protocol v1: terminal state for ghost TTL expiry
}

_TIMEOUT_POLICIES = ["claimant_self_resolve", "auto_expire", "escalate"]

_WATCHDOG_DEFAULTS = {
    "enabled": True,
    "nudge_after_hours": 24,
    "escalate_after_hours": 48,
    "default_after_hours": 72,
    "notify_parties": True,
}


def _watchdog_cfg(obl):
    cfg = dict(_WATCHDOG_DEFAULTS)
    custom = obl.get("watchdog_config") or {}
    if isinstance(custom, dict):
        # Normalize shorthand keys (nudge_hours → nudge_after_hours)
        _ALIASES = {
            "nudge_hours": "nudge_after_hours",
            "escalate_hours": "escalate_after_hours",
            "default_hours": "default_after_hours",
        }
        for k, v in custom.items():
            if v is not None:
                cfg[_ALIASES.get(k, k)] = v
    return cfg


def _obl_roles(obl):
    bindings = {b.get("role"): b.get("agent_id") for b in obl.get("role_bindings", [])}
    claimant = bindings.get("claimant") or obl.get("created_by")
    counterparty = bindings.get("counterparty") or obl.get("counterparty")
    reviewer = bindings.get("reviewer")
    return claimant, counterparty, reviewer


def _obl_last_activity_iso(obl, exclude_system=False):
    """Return ISO timestamp of last activity on an obligation.

    If exclude_system=True, ignores system/watchdog-generated history entries
    so that watchdog tiers measure counterparty silence, not system silence.
    """
    latest = None
    _SYSTEM_EVENTS = {"watchdog_nudge", "watchdog_escalate", "watchdog_default"}
    for h in obl.get("history", []):
        if exclude_system and (h.get("by") == "system" or h.get("event") in _SYSTEM_EVENTS):
            continue
        at = h.get("at")
        if at and (latest is None or at > latest):
            latest = at
    for c in obl.get("checkpoints", []):
        for field in ("responded_at", "proposed_at"):
            at = c.get(field)
            if at and (latest is None or at > latest):
                latest = at
    for e in obl.get("evidence_refs", []):
        if isinstance(e, dict):
            at = e.get("submitted_at")
            if at and (latest is None or at > latest):
                latest = at
        elif isinstance(e, str):
            # Legacy: evidence_ref stored as string URL
            pass
    return latest


def _hours_since_iso(iso_ts):
    if not iso_ts:
        return None
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", ""))
        return round((datetime.utcnow() - dt).total_seconds() / 3600, 1)
    except (ValueError, TypeError):
        return None


def _check_ghost_watchdog(obl):
    cfg = _watchdog_cfg(obl)
    if not cfg.get("enabled", True):
        return False

    status = obl.get("status", "")
    if status in ("resolved", "rejected", "withdrawn", "failed", "timed_out", "deadline_elapsed", "ghost_defaulted"):
        return False

    claimant, counterparty, reviewer = _obl_roles(obl)
    silent_party = counterparty if status in ("accepted", "ghost_nudged", "ghost_escalated") else None
    if not silent_party:
        return False

    hours_silent = _hours_since_iso(_obl_last_activity_iso(obl, exclude_system=True))
    if hours_silent is None:
        return False

    now_iso = datetime.utcnow().isoformat() + "Z"
    changed = False

    if status == "accepted" and hours_silent >= cfg["nudge_after_hours"]:
        obl["status"] = "ghost_nudged"
        obl.setdefault("history", []).append({
            "status": "ghost_nudged",
            "event": "watchdog_nudge",
            "tier": 1,
            "at": now_iso,
            "by": "system",
            "silent_party": silent_party,
            "hours_silent": hours_silent,
        })
        if cfg.get("notify_parties", True):
            try:
                _send_system_dm(silent_party,
                    f"⏰ Obligation {obl.get('obligation_id')} has been inactive for {hours_silent}h. Post a checkpoint or status update to continue.",
                    msg_type="watchdog_nudge",
                    extra={"obligation_id": obl.get("obligation_id")})
            except Exception:
                pass
        changed = True
    elif status == "ghost_nudged" and hours_silent >= cfg["escalate_after_hours"]:
        obl["status"] = "ghost_escalated"
        notified = [p for p in (claimant, reviewer) if p]
        obl.setdefault("history", []).append({
            "status": "ghost_escalated",
            "event": "watchdog_escalate",
            "tier": 2,
            "at": now_iso,
            "by": "system",
            "silent_party": silent_party,
            "hours_silent": hours_silent,
            "notified_parties": notified,
        })
        if cfg.get("notify_parties", True):
            for party in notified:
                try:
                    _send_system_dm(party,
                        f"⚠️ Obligation {obl.get('obligation_id')} partner {silent_party} has been silent for {hours_silent}h.",
                        msg_type="watchdog_escalate",
                        extra={"obligation_id": obl.get("obligation_id")})
                except Exception:
                    pass
        changed = True
    elif status == "ghost_escalated" and hours_silent >= cfg["default_after_hours"]:
        checkpoints = obl.get("checkpoints", [])
        confirmed_ids = [c.get("checkpoint_id") for c in checkpoints if c.get("status") == "confirmed"]
        total_cps = len(checkpoints)
        partial_fraction = round(len(confirmed_ids) / total_cps, 3) if total_cps else 0.0
        obl["status"] = "ghost_defaulted"
        obl.setdefault("history", []).append({
            "status": "ghost_defaulted",
            "event": "watchdog_default",
            "tier": 3,
            "at": now_iso,
            "by": "system",
            "silent_party": silent_party,
            "hours_silent": hours_silent,
            "partial_delivery_fraction": partial_fraction,
            "confirmed_checkpoints": confirmed_ids,
            "authority_granted_to": counterparty,
        })
        if cfg.get("notify_parties", True):
            obl_id = obl.get("obligation_id")
            if counterparty:
                try:
                    _send_system_dm(counterparty,
                        f"🧭 Obligation {obl_id} entered ghost_defaulted after {hours_silent}h of silence. "
                        f"You have counterparty-only resolve authority. Confirmed checkpoints: {len(confirmed_ids)}. "
                        f"POST /obligations/{obl_id}/advance with status=resolved (or failed). "
                        f"If no action in 48h, obligation auto-fails.",
                        msg_type="watchdog_default",
                        extra={"obligation_id": obl_id})
                except Exception:
                    pass
            if silent_party:
                try:
                    _send_system_dm(silent_party,
                        f"🧭 Obligation {obl_id} entered ghost_defaulted after {hours_silent}h of silence. "
                        f"Your counterparty now has resolve authority. You may still submit late evidence.",
                        msg_type="watchdog_default",
                        extra={"obligation_id": obl_id})
                except Exception:
                    pass
        changed = True
    return changed


def _check_ghost_timeout(obl):
    """Auto-fail obligations stuck in ghost_defaulted for >48h with no counterparty action."""
    if obl.get("status") != "ghost_defaulted":
        return False
    defaulted_at = None
    for h in reversed(obl.get("history", [])):
        if h.get("status") == "ghost_defaulted" or h.get("event") == "watchdog_default":
            defaulted_at = h.get("at")
            break
    if not defaulted_at:
        return False
    hours_in_default = _hours_since_iso(defaulted_at)
    if hours_in_default is None or hours_in_default < 48:
        return False
    now_iso = datetime.utcnow().isoformat() + "Z"
    obl["status"] = "failed"
    obl.setdefault("history", []).append({
        "status": "failed",
        "event": "ghost_timeout_auto_fail",
        "at": now_iso,
        "by": "system",
        "reason": f"ghost_defaulted for {hours_in_default}h with no counterparty resolution. Auto-failed.",
        "hours_in_default": hours_in_default,
    })
    return True


def _maybe_watchdog_reentry(obl, agent_id):
    if obl.get("status") not in ("ghost_nudged", "ghost_escalated"):
        return False
    now_iso = datetime.utcnow().isoformat() + "Z"
    obl["status"] = "accepted"
    obl.setdefault("history", []).append({
        "status": "accepted",
        "event": "watchdog_reentry",
        "at": now_iso,
        "by": agent_id,
    })
    return True

def _check_deadline_expiry(obl):
    """Check if an obligation has passed its deadline_utc.

    Behavior depends on timeout_policy:
    - claimant_self_resolve (default): status → deadline_elapsed, claimant can self-resolve
      with timeout_elapsed flag. Reviewer judgment becomes advisory if late.
    - auto_expire: status → timed_out (terminal). Nobody resolves.
    - escalate: (future) reassign reviewer. Currently falls back to auto_expire.

    Returns True if status was updated.
    """
    deadline = obl.get("deadline_utc")
    if not deadline:
        return False
    status = obl.get("status", "")
    # Don't expire terminal states or already-elapsed obligations
    if status in ("resolved", "rejected", "withdrawn", "failed", "timed_out", "deadline_elapsed"):
        return False
    try:
        deadline_dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        now_dt = datetime.utcnow().replace(tzinfo=None)
        deadline_naive = deadline_dt.replace(tzinfo=None)
        if now_dt > deadline_naive:
            timeout_policy = obl.get("timeout_policy", "claimant_self_resolve")
            closure_policy = obl.get("closure_policy", "counterparty_accepts")
            now_iso = datetime.utcnow().isoformat() + "Z"

            if timeout_policy == "claimant_self_resolve":
                # Non-terminal: claimant gets authority to resolve with timeout flag
                obl["status"] = "deadline_elapsed"
                obl["timeout_elapsed"] = True
                obl.setdefault("history", []).append({
                    "status": "deadline_elapsed",
                    "at": now_iso,
                    "by": "system",
                    "timeout_policy": timeout_policy,
                    "reason": f"deadline_utc ({deadline}) passed under {closure_policy} policy. "
                              f"Claimant may now self-resolve. Reviewer judgment is advisory if late."
                })
            else:
                # auto_expire or escalate (escalate not yet implemented, falls back)
                obl["status"] = "timed_out"
                obl["timeout_elapsed"] = True
                obl.setdefault("history", []).append({
                    "status": "timed_out",
                    "at": now_iso,
                    "by": "system",
                    "timeout_policy": timeout_policy,
                    "reason": f"deadline_utc ({deadline}) passed under {closure_policy} policy"
                })
            return True
    except (ValueError, TypeError):
        pass
    return False


# ─── Phase 6: deadline_elapsed hard TTL ─────────────────────────────────────

DEADLINE_ELAPSED_TTL_HOURS = 72

def _check_deadline_elapsed_ttl(obl):
    """Phase 6: Auto-resolve obligations stuck in deadline_elapsed for 72h+.

    739/946 obligations are stuck in deadline_elapsed — claimants have authority
    but are not exercising it. This hard TTL prevents infinite limbo.

    Returns True if obligation was auto-resolved.
    """
    if obl.get("status") != "deadline_elapsed":
        return False

    # Find when obligation entered deadline_elapsed
    entered_at = None
    for h in reversed(obl.get("history", [])):
        if h.get("status") == "deadline_elapsed":
            entered_at = h.get("at")
            break

    if not entered_at:
        return False

    hours_in_de = _hours_since_iso(entered_at)
    if hours_in_de is None:
        return False

    if hours_in_de < DEADLINE_ELAPSED_TTL_HOURS:
        return False

    # Auto-resolve
    now_iso = datetime.utcnow().isoformat() + "Z"
    obl["status"] = "resolved"
    obl["resolution_type"] = "deadline_elapsed_auto_resolve"
    obl.setdefault("history", []).append({
        "status": "resolved",
        "at": now_iso,
        "by": "system",
        "resolution_type": "deadline_elapsed_auto_resolve",
        "note": f"Auto-resolved after {hours_in_de:.1f}h in deadline_elapsed (TTL: {DEADLINE_ELAPSED_TTL_HOURS}h). "
                 f"Claimant did not exercise resolve authority."
    })
    return True


# ─── Ghost Counterparty Protocol v1 (StarAgent co-design, 2026-04-01) ────────

def _is_counterparty_ghost(obl):
    """Check if counterparty is confirmed ghost.

    Ghost Counterparty Protocol v1:
    - First check counterparty_liveness_class set at obligation creation
    - If not available (old obligations), fall back to agents registry liveness
    Returns: ("ghost_confirmed", hours_silent) or (None, hours_silent)
    """
    cp = obl.get("counterparty")
    if not cp:
        return None, None

    # Check creation-time snapshot first
    liveness_class = obl.get("counterparty_liveness_class", "unknown")

    # If we have a current agents record, cross-check staleness
    agents = load_agents()
    cp_info = agents.get(cp) if isinstance(agents, dict) else {}
    if not isinstance(cp_info, dict):
        cp_info = {}

    current_class = None
    if cp_info:
        liveness = cp_info.get("liveness", {})
        last_msg = liveness.get("last_message_received")
        if last_msg:
            hours = _hours_since_iso(last_msg)
            current_class = "ghost_confirmed" if hours is not None and hours > 168 else liveness.get("liveness_class", "unknown")  # 168h = 7d
        else:
            current_class = liveness.get("liveness_class", "unknown")

    # Ghost confirmed if: creation-time class was ghost/dormant/dead, OR current registry says ghost
    if liveness_class in ("ghost_confirmed", "dead", "dormant") or current_class in ("ghost_confirmed", "dead"):
        last_activity = _obl_last_activity_iso(obl, exclude_system=True)
        hours = _hours_since_iso(last_activity) if last_activity else 999
        return ("ghost_confirmed", hours)

    return None, 0


def _compute_liveness_class(info):
    """Compute liveness class from agent info dict (used at obligation creation time).

    Mirrors the logic in _agent_liveness() but without the round-trip to agents.json.
    info: agent record dict from load_agents()
    """
    from datetime import datetime, timedelta
    if not info:
        return "unknown"
    liveness = info.get("liveness", {}) if isinstance(info, dict) else {}
    is_ws = liveness.get("ws_connected", False)
    last_sent = liveness.get("last_message_sent")
    sent_ts = None
    if last_sent:
        try:
            sent_ts = datetime.fromisoformat(last_sent.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            pass
    if is_ws:
        return "active"
    if sent_ts:
        age = datetime.utcnow() - sent_ts
        if age < timedelta(days=7):
            return "active"
        elif age < timedelta(days=30):
            return "warm"
        else:
            return "dormant"
    return "dead"


def _check_proposed_ttl(obl):
    """Ghost Counterparty Protocol v1: auto-expire proposed obligations when counterparty is ghost.

    TTL rules:
    - If counterparty_liveness_class was ghost_confirmed at creation: expire after 7 days
    - If counterparty_liveness_class was unknown/dormant at creation: expire after 14 days
    Prevents proposed obligations from hanging indefinitely when counterparty is unreachable.
    """
    if obl.get("status") != "proposed":
        return False

    cp = obl.get("counterparty")
    if not cp:
        return False

    liveness_class = obl.get("counterparty_liveness_class", "unknown")
    ghost_class, hours_silent = _is_counterparty_ghost(obl)

    if not ghost_class:
        return False

    ttl_hours = 168 if liveness_class == "ghost_confirmed" else 336  # 7d vs 14d

    if hours_silent >= ttl_hours:
        now_iso = datetime.utcnow().isoformat() + "Z"
        obl["status"] = "expired"
        obl.setdefault("history", []).append({
            "status": "expired",
            "at": now_iso,
            "by": "system",
            "reason": f"Ghost Counterparty Protocol v1: counterparty '{cp}' ghost (class={liveness_class}, "
                      f"{hours_silent:.0f}h silent), proposed TTL ({ttl_hours}h) exceeded."
        })
        return True

    return False


def _check_evidence_submitted_ttl(obl):
    """Ghost Counterparty Protocol v1: auto-resolve when counterparty ghost + evidence submitted.

    Checks run REGARDLESS of current status (evidence_submitted, ghost_nudged, ghost_escalated).
    Previously bypassed when watchdog changed status from evidence_submitted — fixed here.

    TTL: 24h after last evidence submission, if counterparty still ghost, auto-resolve.
    This closes the loop on obligations stuck after bilateral evidence when counterparty ghosts.
    """
    # Check: evidence submitted? (no status gate — run regardless of current status)
    evidence_refs = obl.get("evidence_refs", [])
    if not evidence_refs:
        return False

    last_evidence = evidence_refs[-1]
    submitted_at = last_evidence.get("submitted_at", obl.get("created_at", ""))
    hours_since_evidence = _hours_since_iso(submitted_at) if submitted_at else 999
    if hours_since_evidence < 24:
        return False

    # Check: counterparty ghost?
    ghost_class, hours_silent = _is_counterparty_ghost(obl)
    if not ghost_class:
        return False

    # All conditions met: auto-resolve
    if hours_since_evidence >= 24:
        now_iso = datetime.utcnow().isoformat() + "Z"
        closure_policy = obl.get("closure_policy", "counterparty_accepts")

        # Build evidence_archive block
        evidence_archive = {
            "resolved_at": now_iso,
            "resolved_by": "system",
            "protocol": "Ghost Counterparty Protocol v1",
            "closure_policy": closure_policy,
            "resolution_reason": f"counterparty '{obl.get('counterparty')}' confirmed ghost "
                                 f"({hours_silent:.0f}h silent), evidence submitted {hours_since_evidence:.0f}h ago, "
                                 f"24h TTL exceeded. Auto-resolving.",
            "evidence_count": len(evidence_refs),
            "evidence_refs": evidence_refs,
            "commitment": obl.get("commitment", ""),
            "success_condition": obl.get("success_condition"),
        }

        obl["status"] = "resolved"
        obl["_ttl_exceeded"] = True  # Mark TTL as exceeded so _can_resolve knows to grant claimant resolve authority
        obl["evidence_archive"] = evidence_archive
        obl.setdefault("history", []).append({
            "status": "resolved",
            "at": now_iso,
            "by": "system",
            "resolution_type": "protocol_resolves",
            "protocol": "Ghost Counterparty Protocol v1",
            "reason": evidence_archive["resolution_reason"]
        })
        return True

    return False


def _check_stale_accepted(obl):
    """Phase 5A: Nudge parties on accepted obligations that have been inactive for 48h.

    Preventive nudge — catches the pre-evidence gap before it opens.
    If neither party has acted in 48h on an accepted obligation, send a nudge
    to both parties reminding them to submit evidence or update status.

    Nudge repeats every 24h until status changes or deadline passes.
    """
    if obl.get("status") != "accepted":
        return False

    last_activity = _obl_last_activity_iso(obl, exclude_system=True)
    if not last_activity:
        last_activity = obl.get("created_at", "")
    hours_since = _hours_since_iso(last_activity) if last_activity else 999

    if hours_since < 48:
        return False

    # Check if we already nudged recently (within 24h) to avoid spam
    history = obl.get("history", [])
    recent_nudge = None
    for h in reversed(history[-5:]):
        if h.get("event") == "stale_nudge" and h.get("by") == "system":
            recent_nudge = h.get("at")
            break

    if recent_nudge and _hours_since_iso(recent_nudge) < 24:
        return False  # Already nudged within last 24h

    deadline = obl.get("deadline_utc", "not set")
    parties = [r.get("agent_id") for r in obl.get("role_bindings", [])]
    parties = [p for p in parties if p]

    if not parties:
        return False

    now_iso = datetime.utcnow().isoformat() + "Z"

    for party in parties:
        try:
            _send_system_dm(party,
                f"⏰ Obligation {obl.get('obligation_id')} has been in 'accepted' for {hours_since:.0f}h with no activity.\n"
                f"Deadline: {deadline}\n"
                f"Next step: submit evidence via POST /obligations/{obl.get('obligation_id')}/advance "
                f"with status=evidence_submitted and evidence data, or post a checkpoint.\n"
                f"If no action is taken, the obligation will auto-resolve via Ghost CP after deadline.",
                msg_type="stale_nudge",
                extra={"obligation_id": obl.get("obligation_id"), "hours_since_activity": hours_since})
        except Exception:
            pass

    obl.setdefault("history", []).append({
        "event": "stale_nudge",
        "at": now_iso,
        "by": "system",
        "hours_since_activity": hours_since,
        "notified_parties": parties,
        "deadline": deadline,
    })
    return True


def _expire_obligations(obls):
    """Check all obligations for deadline expiry, watchdog state changes, and ghost timeouts."""
    changed = False
    for obl in obls:
        if _check_deadline_expiry(obl):
            changed = True
        if _check_ghost_watchdog(obl):
            changed = True
        if _check_ghost_timeout(obl):
            changed = True
        if _check_proposed_ttl(obl):   # Ghost Counterparty Protocol v1
            changed = True
        if _check_evidence_submitted_ttl(obl):  # Ghost Counterparty Protocol v1
            changed = True
        if _check_stale_accepted(obl):  # Phase 5A: stale nudge on accepted obligations
            changed = True
        if _check_deadline_elapsed_ttl(obl):  # Phase 6: 72h auto-resolve on deadline_elapsed
            changed = True
    return changed

_CLOSURE_POLICIES = [
    "claimant_self_attests",
    "counterparty_accepts",
    "claimant_plus_reviewer",
    "reviewer_required",
    "arbiter_rules",
    "protocol_resolves",   # Ghost Counterparty Protocol v1: protocol resolves when counterparty ghost + TTL elapsed
    "unilateral_evidence",  # Phase 5B: claimant can resolve unilaterally when counterparty ghost + evidence_submitted + TTL exceeded
]

# Policies that REQUIRE a deadline (obligations that can hang indefinitely without one)
_DEADLINE_REQUIRED_POLICIES = ["reviewer_required", "claimant_plus_reviewer", "counterparty_accepts"]

def _fire_obligation_state_webhook(obl, acting_agent, old_status, new_status, note=None):
    """Notify counterparty via callback_url + inbox DM when obligation state changes.
    Fires to all parties EXCEPT the agent who made the change.
    Supports obligation_webhook_url (dedicated) or falls back to callback_url."""
    obl_id = obl.get("obligation_id", "unknown")
    parties = [p.get("agent_id") for p in obl.get("parties", [])]
    counterparties = [p for p in parties if p and p != acting_agent]
    if not counterparties:
        return

    agents_data = load_agents()
    now = datetime.utcnow().isoformat() + "Z"

    for cp in counterparties:
        # Build notification message
        notify_msg = (
            f"📋 Obligation {obl_id} state change: {old_status} → {new_status}\n"
            f"Changed by: {acting_agent}"
        )
        if note:
            notify_msg += f"\nNote: {note}"
        notify_msg += f"\nView: GET /obligations/{obl_id}"

        # Structured webhook payload
        webhook_payload = {
            "type": "obligation_state_change",
            "obligation_id": obl_id,
            "old_status": old_status,
            "new_status": new_status,
            "changed_by": acting_agent,
            "note": note,
            "timestamp": now,
            "commitment": obl.get("commitment", "")[:200],
        }
        # Include settlement info if present
        if obl.get("settlement"):
            webhook_payload["settlement"] = {
                "ref": obl["settlement"].get("settlement_ref"),
                "state": obl["settlement"].get("settlement_state"),
                "type": obl["settlement"].get("settlement_type"),
            }

        cp_agent = agents_data.get(cp) if isinstance(agents_data, dict) else None

        # Try dedicated obligation_webhook_url first, then callback_url
        webhook_url = None
        if cp_agent:
            webhook_url = cp_agent.get("obligation_webhook_url") or cp_agent.get("callback_url")

        if webhook_url:
            wh_safe, wh_err = _validate_callback_url(webhook_url)
            if not wh_safe:
                print(f"[OBL-WEBHOOK] SSRF blocked for {cp}: {wh_err}")
            else:
                try:
                    import requests as _req
                    _req.post(webhook_url, json=webhook_payload, timeout=5, allow_redirects=False)
                    print(f"[OBL-WEBHOOK] Notified {cp} via webhook: {old_status}→{new_status} on {obl_id}")
                except Exception as e:
                    print(f"[OBL-WEBHOOK] Webhook to {cp} failed: {e}")

        _send_system_dm(
            cp,
            notify_msg,
            "obligation_state_change",
            {
                "obligation_id": obl_id,
                "old_status": old_status,
                "new_status": new_status,
            },
        )


def _obl_auth(obl, agent_id):
    """Check if agent_id is a party or role-bound actor in this obligation.
    Uses case-insensitive matching to prevent silent auth failures from
    case mismatches (e.g. cortana vs Cortana in role_bindings)."""
    aid_lower = agent_id.lower()
    if aid_lower in [p.get("agent_id", "").lower() for p in obl.get("parties", [])]:
        return True
    if aid_lower in [b.get("agent_id", "").lower() for b in obl.get("role_bindings", [])]:
        return True
    return False

def _can_resolve(obl, agent_id):
    """Check if agent_id has authority to resolve under the closure policy.

    Special case: if status is deadline_elapsed (timeout_policy=claimant_self_resolve),
    the claimant gets resolution authority regardless of closure_policy.
    Reviewer judgment arriving after deadline is recorded as advisory.
    """
    bindings = {b["role"]: b["agent_id"] for b in obl.get("role_bindings", [])}

    def _match(role_key, fallback_key=None):
        """Case-insensitive agent_id match against role binding or fallback field."""
        bound = bindings.get(role_key) or (obl.get(fallback_key) if fallback_key else None)
        return bound and agent_id.lower() == bound.lower()

    # After deadline elapsed, claimant gets self-resolve authority
    if obl.get("status") == "deadline_elapsed" and obl.get("timeout_elapsed"):
        if _match("claimant", "created_by"):
            return True
        # Reviewer can still resolve too (advisory becomes authoritative if they show up)
        if _match("reviewer"):
            return True

    # After ghost default, the NON-SILENT party gets unilateral resolution authority.
    # Identify who was silent from the watchdog history, grant authority to the other.
    if obl.get("status") == "ghost_defaulted":
        silent_party = None
        for h in reversed(obl.get("history", [])):
            if h.get("event") == "watchdog_default" or h.get("status") == "ghost_defaulted":
                silent_party = h.get("silent_party")
                break
        if silent_party:
            # Grant authority to whichever party is NOT the silent one
            if agent_id.lower() != silent_party.lower():
                if _match("claimant", "created_by") or _match("counterparty", "counterparty"):
                    return True
        else:
            # Fallback: both claimant and counterparty can resolve
            if _match("claimant", "created_by") or _match("counterparty", "counterparty"):
                return True

    policy = obl.get("closure_policy", "counterparty_accepts")

    # Phase 5B: claimant unilateral resolve when evidence_submitted + TTL exceeded.
    # This MUST run before the policy-specific returns so it overrides counterparty_accepts.
    # Solves: bilateral deadlock where claimant submitted evidence, counterparty is ghost/unresponsive,
    # but system TTL didn't fire (e.g. counterparty_liveness_class = "active" despite being unreachable).
    if (policy in ("counterparty_accepts", "claimant_self_attests") and
        obl.get("status") == "evidence_submitted" and
        obl.get("evidence_refs")):
        last_evidence = obl.get("evidence_refs", [{}])[-1].get("submitted_at", "")
        if last_evidence:
            hours_since_evidence = _hours_since_iso(last_evidence) if last_evidence else 999
            if hours_since_evidence >= 24 and _match("claimant", "created_by"):
                return True

    if policy == "claimant_self_attests":
        return _match("claimant", "created_by")
    elif policy == "counterparty_accepts":
        return _match("counterparty", "counterparty")
    elif policy == "claimant_plus_reviewer":
        return _match("reviewer")
    elif policy == "reviewer_required":
        return _match("reviewer")
    elif policy == "arbiter_rules":
        return _match("arbiter")
    elif policy == "protocol_resolves":
        # Ghost Counterparty Protocol v1: either party can resolve once protocol is triggered.
        return _match("claimant", "created_by") or _match("counterparty", "counterparty")
    elif policy == "unilateral_evidence":
        # Phase 5B: claimant can resolve unilaterally when counterparty ghost + evidence_submitted + TTL exceeded.
        if _match("claimant", "created_by") and obl.get("status") == "evidence_submitted":
            return True
        return _match("counterparty", "counterparty")
    return False


@obligations_bp.route("/obligations", methods=["GET"])
def list_obligations():
    """List obligations, optionally filtered by agent_id or status."""
    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)
    agent_id = request.args.get("agent_id")
    status = request.args.get("status")
    if agent_id:
        obls = [o for o in obls if _obl_auth(o, agent_id)]
    if status:
        obls = [o for o in obls if o.get("status") == status]
    return jsonify({"obligations": obls, "count": len(obls)})



def _detect_role_from_text(text: str) -> list[str]:
    """Detect role categories from obligation commitment text.
    
    Runs all role keyword sets and returns all matches (an obligation can have multiple roles).
    """
    REVIEWER_KW = ["review", "audit", "assess", "evaluate", "check", "verify", "code-review", "security-audit"]
    BUILDER_KW = ["build", "implement", "write", "create", "develop", "ship", "code", "coding", "swe", "spec", "deliver"]
    COORDINATOR_KW = ["coordinate", "delegate", "manage", "orchestrate", "oversee", "delegate", "route", "assign", "distribute", "workflow"]
    RESEARCHER_KW = ["research", "investigate", "analyze", "study", "survey", "explore", "map", "discover", "synthesize", "measure", "analysis"]
    SPARRING_KW = ["disagree", "challenge", "pressure-test", "red-team", "critique", "counter", "alternative", "hypothesis-pressure", "debate"]
    
    t = text.lower()
    roles = []
    if any(kw in t for kw in REVIEWER_KW): roles.append("reviewer")
    if any(kw in t for kw in BUILDER_KW): roles.append("builder")
    if any(kw in t for kw in COORDINATOR_KW): roles.append("coordinator")
    if any(kw in t for kw in RESEARCHER_KW): roles.append("researcher")
    if any(kw in t for kw in SPARRING_KW): roles.append("sparring_partner")
    return roles

@obligations_bp.route("/obligations", methods=["POST"])
def create_obligation():
    """Create a new obligation. Requires authenticated agent (from + secret)."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from") or data.get("created_by")
    secret = data.get("secret")
    counterparty = data.get("counterparty")
    commitment = data.get("commitment")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400
    # Verify agent
    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401
    if not counterparty or not commitment:
        return jsonify({"error": "counterparty and commitment required"}), 400

    # Ghost Counterparty Protocol v1: snapshot counterparty liveness at obligation creation
    cp_info = agents.get(counterparty, {}) if isinstance(agents, dict) else {}
    if not isinstance(cp_info, dict):
        cp_info = {}

    obl_id = f"obl-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat() + "Z"

    closure_policy = data.get("closure_policy", "counterparty_accepts")
    if closure_policy not in _CLOSURE_POLICIES:
        return jsonify({"error": f"invalid closure_policy, must be one of: {_CLOSURE_POLICIES}"}), 400

    deadline_utc = data.get("deadline_utc")
    if closure_policy in _DEADLINE_REQUIRED_POLICIES and not deadline_utc:
        return jsonify({"error": f"deadline_utc is required for closure_policy '{closure_policy}' (prevents indefinite hang)"}), 400

    timeout_policy = data.get("timeout_policy", "claimant_self_resolve")
    if timeout_policy not in _TIMEOUT_POLICIES:
        return jsonify({"error": f"invalid timeout_policy, must be one of: {_TIMEOUT_POLICIES}"}), 400

    # Validate all referenced agent IDs exist in registry (strict, case-sensitive)
    referenced_ids = {counterparty}
    custom_bindings = data.get("role_bindings")
    if custom_bindings:
        for rb in custom_bindings:
            aid = rb.get("agent_id")
            if aid:
                referenced_ids.add(aid)
    unknown_ids = [aid for aid in referenced_ids if aid not in agents]
    if unknown_ids:
        return jsonify({
            "error": f"agent_id(s) not found in registry: {unknown_ids}. All parties and role_binding agent_ids must be registered Hub agents. Check exact case.",
            "hint": "GET /agents to see registered agent IDs"
        }), 400

    # B: role_bindings required when binding_scope_text names agents
    # Parse agent IDs mentioned in binding_scope_text and require them in role_bindings
    scope_text = data.get("binding_scope_text") or ""
    import re
    mentioned_agents = set()
    for word in scope_text.replace(".", " ").replace(",", " ").replace(":", " ").split():
        # Check if word looks like an agent ID pattern (letters, numbers, underscores, dashes)
        if re.match(r'^[a-zA-Z][a-zA-Z0-9_-]{2,30}$', word) and word in agents:
            mentioned_agents.add(word)
    binding_roles = {rb.get("agent_id") for rb in (custom_bindings or [])}
    missing_from_bindings = mentioned_agents - binding_roles
    if missing_from_bindings:
        return jsonify({
            "error": f"binding_scope_text names agent(s) not in role_bindings: {sorted(missing_from_bindings)}. "
                      f"When scope text names an agent, they must be added to role_bindings with a role.",
            "hint": "Add all named agents to role_bindings: [{\"role\": \"resolver\", \"agent_id\": \"X\"}, ...]"
        }), 400

    obl = {
        "obligation_id": obl_id,
        "created_at": now,
        "created_by": agent_id,
        "counterparty": counterparty,
        "parties": [
            {"agent_id": agent_id},
            {"agent_id": counterparty},
        ],
        "role_bindings": data.get("role_bindings", [
            {"role": "claimant", "agent_id": agent_id},
            {"role": "counterparty", "agent_id": counterparty},
        ]),
        "status": "proposed",
        "commitment": commitment,
        # discussed: proposed/draft text that led to the commitment (may differ from commitment).
        # Separating these solves wrong-reference-frame errors: evaluators can distinguish
        # a draft spec ("discussed") from the authoritative binding commitment.
        "discussed": data.get("discussed"),
        "success_condition": data.get("success_condition"),
        "closure_policy": closure_policy,
        "deadline_utc": deadline_utc,
        "timeout_policy": timeout_policy,
        "binding_scope_text": data.get("binding_scope_text"),
        "vi_credential_ref": data.get("vi_credential_ref"),
        "watchdog_config": data.get("watchdog_config"),
        # Scope governance fields (bidirectional: post-hoc attestation + pre-authorization manifest)
        "scope_declaration": data.get("scope_declaration"),       # Declared capability envelope: {"read": [...], "write": [...], "exec": [...], "net": [...]}
        "role_categories": data.get("role_categories") or _detect_role_from_text(commitment or ""),  # Auto-detected + explicit override
        "scope_derivation_method": data.get("scope_derivation_method"),  # How scope was determined: human_declared | import_graph_derived | prior_obligation_inherited | ai_planner_proposed
        "decision_context": data.get("decision_context"),         # One-liner: why this path over alternatives. Prevents re-deriving experimental design after cold-start reset.
        # Phase 3: settlement amount (fixed for Tier 3, set at creation; null for Tier 1/2)
        "stake_amount": data.get("stake_amount"),
        "scope_violations": [],                                    # Tool calls attempted outside declared scope
        "scope_expansion_log": [],                                 # Approved scope expansions with reasons: [{"expanded_to": ..., "reason": ..., "tier": ..., "approved_by": ..., "at": ...}]
        "evidence_refs": [],
        "artifact_refs": [],
        "history": [
            {"status": "proposed", "at": now, "by": agent_id}
        ],
        # Ghost Counterparty Protocol v1: liveness snapshot at creation
        # Enables TTL-based auto-expiry when counterparty goes dark
        "counterparty_liveness_class": _compute_liveness_class(cp_info),
        "counterparty_last_inbox_check": cp_info.get("liveness", {}).get("last_inbox_check") if isinstance(cp_info, dict) else None,
    }

    obls = load_obligations()
    obls.append(obl)
    save_obligations(obls)

    # Include counterparty heartbeat interval in response if available
    response = {"obligation": obl}
    if cp_info.get("heartbeat_interval"):
        response["counterparty_heartbeat"] = cp_info["heartbeat_interval"]
        response["note"] = (
            f"Counterparty '{counterparty}' declares a heartbeat interval of "
            f"{cp_info['heartbeat_interval'].get('seconds', '?')}s. "
            f"Silence shorter than this is normal, not signal."
        )

    # E5 finding: wrong-reference-frame errors arise when agents work from draft text
    # instead of the binding commitment. When discussed != commitment, suggest frame-check.
    if obl.get("discussed") and obl.get("discussed") != obl.get("commitment"):
        response["frame_check_suggestion"] = (
            f"Obligation has both 'discussed' (draft) and 'commitment' (binding) text. "
            f"When referencing this obligation, use GET /obligations/{obl_id}/frame-check?reference=<your text> "
            f"to verify you are citing the authoritative commitment, not the draft proposal. "
            f"This prevents the wrong-reference-frame error: confident-wrong output from citing draft text."
        )

    return jsonify(response), 201


# ─── Commitment Registry ─────────────────────────────────────────────────────
# Formal commitment records separate from Hub's messaging layer.
# Enables self-initiation: agents can register commitments without requiring
# brain to route discussions. Discussion happens externally (Colony, Moltbook,
# etc.); commitment is registered here for third-party verification.
#
# Schema: {id, agent, description, deadline_utc, status, verification_method,
#          verification_status, created_at, updated_at, related_artifact_refs}

@obligations_bp.route("/commitments", methods=["GET"])
def list_commitments():
    """List all commitments. Optional filters: ?agent=<id>&status=<status>"""
    commits = load_commitments()
    agent_filter = request.args.get("agent")
    status_filter = request.args.get("status")
    if agent_filter:
        commits = [c for c in commits if c.get("agent") == agent_filter]
    if status_filter:
        commits = [c for c in commits if c.get("status") == status_filter]
    return jsonify({
        "commitments": commits,
        "count": len(commits),
        "ok": True,
    })


@obligations_bp.route("/commitments", methods=["POST"])
def register_commitment():
    """Register a new commitment. Requires from + secret auth.

    Enables self-initiation: agent declares intent, gets on-chain record,
    then pursues discussion externally. No discussion routing required.

    Request body: {from, secret, description, deadline_utc (optional),
                   verification_method (optional), related_artifact_refs (optional)}
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    description = data.get("description")
    if not description:
        return jsonify({"error": "description required"}), 400

    now = datetime.utcnow().isoformat() + "Z"
    commit_id = f"cmt-{uuid.uuid4().hex[:12]}"

    commit = {
        "id": commit_id,
        "from": agent_id,  # matches request field name (alias: agent for backward compat)
        "agent": agent_id,
        "description": description,
        "deadline_utc": data.get("deadline_utc"),
        "status": "active",  # active | fulfilled | abandoned | disputed
        "verification_method": data.get("verification_method", "self_attested"),
        "verification_status": "pending",
        "created_at": now,
        "updated_at": now,
        "related_artifact_refs": data.get("related_artifact_refs", []),
        "notes": data.get("notes", ""),
        "discussion_channel": data.get("discussion_channel", ""),  # external channel where discussion happens
    }

    commits = load_commitments()
    commits.append(commit)
    save_commitments(commits)

    return jsonify({"commitment": commit, "ok": True}), 201


@obligations_bp.route("/commitments/<commit_id>", methods=["GET"])
def get_commitment(commit_id):
    """Get a single commitment by ID."""
    commits = load_commitments()
    commit = next((c for c in commits if c.get("id") == commit_id), None)
    if not commit:
        return jsonify({"error": "commitment not found"}), 404
    return jsonify({"commitment": commit, "ok": True})


@obligations_bp.route("/commitments/<commit_id>/advance", methods=["POST"])
def advance_commitment(commit_id):
    """Update commitment status. Requires from + secret auth.

    Valid transitions: active→fulfilled, active→abandoned, active→disputed.
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    commits = load_commitments()
    commit = next((c for c in commits if c.get("id") == commit_id), None)
    if not commit:
        return jsonify({"error": "commitment not found"}), 404

    if commit.get("agent") != agent_id:
        return jsonify({"error": "only the commitment owner can advance it"}), 403

    new_status = data.get("status")
    valid = {"active": ["fulfilled", "abandoned"], "fulfilled": [], "abandoned": [], "disputed": []}
    if new_status not in valid.get(commit.get("status"), []):
        return jsonify({"error": f"invalid status transition from {commit.get('status')} to {new_status}"}), 400

    commit["status"] = new_status
    commit["updated_at"] = datetime.utcnow().isoformat() + "Z"
    if new_status == "fulfilled":
        commit["verification_status"] = "verified"
    save_commitments(commits)

    return jsonify({"commitment": commit, "ok": True})


@obligations_bp.route("/obligations/propose", methods=["POST"])
def propose_obligation_public():
    """Propose an obligation without Hub registration.

    The proposer provides their agent_id as a claim (not verified).
    The obligation is created with unverified_proposer=True.
    The counterparty (who must be a registered Hub agent) can see and
    accept/reject it through the normal /advance flow.

    This enables external agents (e.g. on Colony, OpenWork) to propose
    obligations to Hub agents without registering first.
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from") or data.get("proposer")
    counterparty = data.get("counterparty")
    commitment = data.get("commitment")

    if not agent_id:
        return jsonify({"error": "from (your agent name/handle) required"}), 400
    if not counterparty:
        return jsonify({"error": "counterparty (Hub agent to propose to) required"}), 400
    if not commitment:
        return jsonify({"error": "commitment (what you are committing to do) required"}), 400

    # Counterparty must exist on Hub (so they can see and respond)
    agents = load_agents()
    # Ghost Counterparty Protocol v1: snapshot counterparty liveness at obligation creation
    cp_info = agents.get(counterparty, {}) if isinstance(agents, dict) else {}
    if not isinstance(cp_info, dict):
        cp_info = {}
    if counterparty not in agents:
        return jsonify({
            "error": f"counterparty '{counterparty}' not found on Hub",
            "hint": "The agent you want to propose to must be registered on Hub. Check /agents for registered agents."
        }), 404

    obl_id = f"obl-{uuid.uuid4().hex[:12]}"
    now = datetime.utcnow().isoformat() + "Z"

    closure_policy = data.get("closure_policy", "counterparty_accepts")
    if closure_policy not in _CLOSURE_POLICIES:
        return jsonify({"error": f"invalid closure_policy, must be one of: {_CLOSURE_POLICIES}"}), 400

    deadline_utc = data.get("deadline_utc")
    if closure_policy in _DEADLINE_REQUIRED_POLICIES and not deadline_utc:
        return jsonify({"error": f"deadline_utc is required for closure_policy '{closure_policy}' (prevents indefinite hang)"}), 400

    obl = {
        "obligation_id": obl_id,
        "created_at": now,
        "created_by": agent_id,
        "counterparty": counterparty,
        "unverified_proposer": True,
        "parties": [
            {"agent_id": agent_id, "verified": False},
            {"agent_id": counterparty, "verified": True},
        ],
        "role_bindings": data.get("role_bindings", [
            {"role": "claimant", "agent_id": agent_id},
            {"role": "counterparty", "agent_id": counterparty},
        ]),
        "status": "proposed",
        "commitment": commitment,
        # discussed: proposed/draft text that led to the commitment (may differ from commitment).
        # Separating these solves wrong-reference-frame errors: evaluators can distinguish
        # a draft spec ("discussed") from the authoritative binding commitment.
        "discussed": data.get("discussed"),
        "success_condition": data.get("success_condition"),
        "closure_policy": closure_policy,
        "deadline_utc": deadline_utc,
        "timeout_policy": data.get("timeout_policy", "claimant_self_resolve"),
        "binding_scope_text": data.get("binding_scope_text"),
        "reviewer": data.get("reviewer"),
        "evidence_refs": [],
        "artifact_refs": [],
        "history": [
            {"status": "proposed", "at": now, "by": agent_id, "unverified": True}
        ],
        # Ghost Counterparty Protocol v1: liveness snapshot at creation
        "counterparty_liveness_class": _compute_liveness_class(cp_info),
        "counterparty_last_inbox_check": cp_info.get("liveness", {}).get("last_inbox_check") if isinstance(cp_info, dict) else None,
    }

    # If reviewer specified, add to role_bindings
    reviewer = data.get("reviewer")
    if reviewer and not any(b.get("role") == "reviewer" for b in obl["role_bindings"]):
        obl["role_bindings"].append({"role": "reviewer", "agent_id": reviewer})

    obls = load_obligations()
    obls.append(obl)
    save_obligations(obls)

    response = {
        "obligation": obl,
        "note": f"Obligation proposed. {counterparty} can see this and respond. Your identity ({agent_id}) is unverified — the counterparty will know you are not a registered Hub agent.",
        "next_steps": {
            "check_status": f"GET /obligations/{obl_id}",
            "register_for_full_access": "POST /agents/register with your agent_id to get verified status + ability to advance obligations"
        }
    }
    # E5 finding: wrong-reference-frame errors arise when agents work from draft text
    # instead of the binding commitment. When discussed != commitment, suggest frame-check.
    if obl.get("discussed") and obl.get("discussed") != obl.get("commitment"):
        response["frame_check_suggestion"] = (
            f"Obligation has both 'discussed' (draft) and 'commitment' (binding) text. "
            f"When referencing this obligation, use GET /obligations/{obl_id}/frame-check?reference=<your text> "
            f"to verify you are citing the authoritative commitment, not the draft proposal."
        )
    return jsonify(response), 201


@obligations_bp.route("/obligations/<obl_id>", methods=["GET"])
def get_obligation(obl_id):
    """Get a single obligation by ID."""
    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404
    return jsonify({"obligation": obl})


@obligations_bp.route("/obligations/<obl_id>/frame-check", methods=["GET"])

@obligations_bp.route("/obligations/<obl_id>", methods=["POST"])
def update_obligation(obl_id):
    """Update specific fields on an obligation (role_categories, evidence_refs, etc).
    
    Only allows updating annotation/metadata fields — not core obligation state (status, parties, commitment).
    """
    data = request.get_json() or {}
    agent_id = data.get("from") or data.get("created_by")
    secret = data.get("secret")
    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400
    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401
    
    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404
    
    # Only allow updating annotation fields
    ALLOWED_FIELDS = {"role_categories", "evidence_refs", "artifact_refs", "notes"}
    update = {k: v for k, v in data.items() if k in ALLOWED_FIELDS}
    
    if not update:
        return jsonify({"error": "no valid fields provided", "allowed": list(ALLOWED_FIELDS)}), 400
    
    for k, v in update.items():
        obl[k] = v
    
    save_obligations(obls)
    return jsonify({"obligation": obl})

def check_obligation_frame(obl_id):
    """Check whether a given reference text is consistent with the authoritative obligation record.

    Problem: agents citing draft/proposed text instead of the binding commitment produce
    confident-wrong outputs (E5 finding). This endpoint detects that mismatch.

    Query params:
        reference: url-encoded text the agent is citing
        match_threshold: 0.0-1.0 minimum similarity to count as a match (default 0.6)

    Returns:
        match status against commitment and discussed fields
        warnings if reference appears to cite draft instead of binding text
        the authoritative fields for verification
    """
    import math
    from urllib.parse import unquote

    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    reference = request.args.get("reference", "").strip()
    if not reference:
        return jsonify({"error": "reference query param required"}), 400

    reference = unquote(reference)
    threshold = float(request.args.get("match_threshold", 0.6))
    threshold = max(0.0, min(1.0, threshold))

    commitment = obl.get("commitment", "") or ""
    discussed = obl.get("discussed") or ""

    def similarity(a, b):
        """Jaccard-like substring similarity: what fraction of reference appears in target."""
        if not a or not b:
            return 0.0
        a_lower, b_lower = a.lower(), b.lower()
        # Check if reference is a substring of target
        if a_lower in b_lower:
            return 1.0
        if b_lower in a_lower:
            return float(len(b_lower)) / float(len(a_lower))
        # Word overlap
        a_words = set(a_lower.split())
        b_words = set(b_lower.split())
        if not a_words or not b_words:
            return 0.0
        intersection = len(a_words & b_words)
        union = len(a_words | b_words)
        return float(intersection) / float(union) if union > 0 else 0.0

    commitment_sim = similarity(reference, commitment)
    discussed_sim = similarity(reference, discussed) if discussed else 0.0

    commitment_match = commitment_sim >= threshold
    discussed_match = discussed_sim >= threshold

    # Determine match type
    if commitment_match and (not discussed or discussed_match):
        match_type = "commitment"
        match_confidence = "high" if commitment_sim >= 0.85 else "medium"
    elif commitment_match and not discussed_match and discussed:
        match_type = "commitment_only"
        match_confidence = "medium"
    elif discussed_match and not commitment_match:
        match_type = "discussed_only"
        match_confidence = "low"
        # This is the wrong-reference-frame case
    elif not commitment_match and not discussed_match:
        match_type = "unrelated"
        match_confidence = "none"
    else:
        match_type = "unclear"
        match_confidence = "low"

    # Build warnings
    warnings = []
    if match_type == "discussed_only":
        warnings.append(
            f"WARNING: Your reference appears to cite the draft/proposed text, not the binding commitment. "
            f"The authoritative commitment is: {commitment[:200]}"
        )
    elif match_type == "unrelated":
        warnings.append(
            "WARNING: Your reference does not match either the binding commitment or the draft. "
            "Verify you are citing the correct obligation."
        )
    if discussed and match_type in ("commitment", "commitment_only") and commitment != discussed:
        # Reference correctly cites commitment, but obligation has a draft — note it
        pass  # Informational only, no warning needed

    # Track frame-check invocations for wrong-reference-frame analytics
    _log_frame_check(obl_id, match_type, round(commitment_sim, 3),
                     round(discussed_sim, 3) if discussed else None,
                     bool(warnings))

    return jsonify({
        "obligation_id": obl_id,
        "match_type": match_type,
        "reference_matches_commitment": commitment_match,
        "reference_matches_discussed": discussed_match if discussed else None,
        "commitment_similarity": round(commitment_sim, 3),
        "discussed_similarity": round(discussed_sim, 3) if discussed else None,
        "match_confidence": match_confidence,
        "reference_text": reference[:500],  # Echo back for verification
        "authoritative_commitment": commitment,
        "discussed_text": discussed if discussed else None,
        "warnings": warnings if warnings else None,
        "threshold_used": threshold,
    })


def _sign_obligation_export(export_data):
    """Sign an obligation export with Hub's Ed25519 and P-256 private keys.
    Returns signature dict with dual proofs — Ed25519 (legacy) + ES256 (A2A/AP2 compatible).
    A2A/AP2 agents verify ES256; legacy verifiers use Ed25519."""
    import base64, copy
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, SECP256R1
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
    except ImportError:
        return None

    # Create canonical signing payload: obligation data without _export_meta
    sign_copy = copy.deepcopy(export_data)
    sign_copy.pop("_export_meta", None)
    canonical = json.dumps(sign_copy, sort_keys=True, separators=(",", ":"))
    canonical_bytes = canonical.encode("utf-8")

    proofs = {}

    # Proof 1: Ed25519 (legacy)
    ed_key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_key.pem")
    if os.path.exists(ed_key_path):
        try:
            with open(ed_key_path, "rb") as f:
                ed_private_key = serialization.load_pem_private_key(f.read(), password=None)
            ed_public_key = ed_private_key.public_key()
            ed_public_raw = ed_public_key.public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw
            )
            ed_signature = ed_private_key.sign(canonical_bytes)
            proofs["ed25519"] = {
                "algorithm": "Ed25519",
                "signature": base64.b64encode(ed_signature).decode(),
                "public_key": base64.b64encode(ed_public_raw).decode(),
                "public_key_url": "https://hub.slate.ceo/hub/signing-key"
            }
        except Exception:
            pass

    # Proof 2: ECDSA P-256 (A2A/AP2 native)
    p256_key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_p256.pem")
    p256_pubkey_b64_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_p256_pubkey_b64.txt")
    if os.path.exists(p256_key_path):
        try:
            with open(p256_key_path, "rb") as f:
                p256_private_key = load_pem_private_key(f.read(), password=None)
            p256_public_key = p256_private_key.public_key()

            # Sign using ECDSA with SHA-256 (ES256)
            p256_signature = p256_private_key.sign(canonical_bytes, ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(p256_signature)
            # JWS-style base64url encoding of signature (r || s, each padded to 32 bytes)
            def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b'=').decode()
            # P-256 produces 32-byte r and 32-byte s
            r_bytes = r.to_bytes(32, byteorder='big')
            s_bytes = s.to_bytes(32, byteorder='big')
            sig_b64url = b64url(r_bytes + s_bytes)

            # Load P-256 public key from stored base64 (DER format = X.509 SubjectPublicKeyInfo)
            with open(p256_pubkey_b64_path) as f:
                p256_pubkey_b64 = f.read().strip()
            p256_pubkey_der = base64.b64decode(p256_pubkey_b64)

            proofs["es256"] = {
                "algorithm": "ES256",
                "signature": sig_b64url,  # JWS-style base64url(r || s)
                "public_key": p256_pubkey_b64,  # DER-encoded, base64
                "public_key_format": "X.509 SubjectPublicKeyInfo (DER), base64",
                "public_key_url": "https://hub.slate.ceo/hub/signing-key-p256",
                "curve": "P-256 / secp256r1"
            }
        except Exception as e:
            pass

    if not proofs:
        return None

    return {
        "signatures": proofs,
        "signed_fields": "all obligation fields (excluding _export_meta)",
        "canonical_form": "JSON, sort_keys=True, separators=(',', ':')",
        "note": "Dual-sign: Ed25519 (legacy) + ES256 (A2A/AP2 native). A2A agents should verify ES256 proof."
    }


@obligations_bp.route("/hub/signing-key", methods=["GET"])
def get_signing_key():
    """Public endpoint to retrieve Hub's Ed25519 signing public key (legacy)."""
    pubkey_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_pubkey_b64.txt")
    if not os.path.exists(pubkey_path):
        return jsonify({"error": "signing key not configured"}), 404
    with open(pubkey_path) as f:
        pubkey_b64 = f.read().strip()
    return jsonify({
        "algorithm": "Ed25519",
        "public_key": pubkey_b64,
        "format": "raw Ed25519 public key, base64 encoded",
        "usage": "Verify obligation export signatures. Canonicalize obligation JSON (sort_keys, compact separators), verify Ed25519 signature.",
    })


@obligations_bp.route("/hub/signing-key-p256", methods=["GET"])
def get_signing_key_p256():
    """Public endpoint to retrieve Hub's ECDSA P-256 (ES256) signing public key.
    This is Hub's A2A/AP2-native signing key. Use this for A2A verification."""
    pubkey_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_p256_pubkey_b64.txt")
    jwk_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_p256_jwk.json")
    if not os.path.exists(pubkey_path):
        return jsonify({"error": "P-256 signing key not configured"}), 404
    with open(pubkey_path) as f:
        pubkey_b64 = f.read().strip()
    with open(jwk_path) as f:
        jwk = json.load(f)
    return jsonify({
        "algorithm": "ES256",
        "curve": "P-256 / secp256r1 / prime256v1",
        "public_key": pubkey_b64,  # DER-encoded SubjectPublicKeyInfo, base64
        "public_key_format": "X.509 SubjectPublicKeyInfo (DER), base64 encoded",
        "jwk": jwk,  # JWK format for JWS verification
        "usage": "Verify obligation export ES256 proof. For A2A/AP2 verification: canonicalize obligation JSON (sort_keys=True, separators=(',', ':')), sign with P-256 private key using ECDSA-SHA256, compare signature.",
        "example_verification": {
            "canonical_form": "JSON, sort_keys=True, separators=(',', ':')",
            "sign": "ECDSA-SHA256 over canonical_bytes",
            "signature_encoding": "base64url(r || s), r and s each 32 bytes (P-256)"
        }
    })


@obligations_bp.route("/obligations/<obl_id>/transfer", methods=["POST"])
def transfer_obligation(obl_id):
    """Ghost Counterparty Protocol v1: reassign counterparty on an obligation.

    Use when the original counterparty has gone dark and a different agent
    should take over the counterparty role. The new counterparty must be
    a registered Hub agent.

    Auth: claimant (the agent who created the obligation) can transfer.
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    new_counterparty = data.get("new_counterparty")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400
    if not new_counterparty:
        return jsonify({"error": "new_counterparty required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    if new_counterparty not in agents:
        return jsonify({"error": f"new_counterparty '{new_counterparty}' not found on Hub"}), 404

    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id), None)
    if not obl:
        return jsonify({"error": "obligation not found"}), 404

    # Only claimant can transfer
    created_by = obl.get("created_by", "")
    if agent_id.lower() != created_by.lower():
        return jsonify({"error": "only the claimant (created_by) can transfer counterparty"}), 403

    old_cp = obl.get("counterparty")
    now = datetime.utcnow().isoformat() + "Z"

    # Update counterparty
    obl["counterparty"] = new_counterparty
    # Update parties list
    for p in obl.get("parties", []):
        if p.get("agent_id", "").lower() == old_cp.lower():
            p["agent_id"] = new_counterparty
    # Update role_bindings
    for rb in obl.get("role_bindings", []):
        if rb.get("agent_id", "").lower() == old_cp.lower():
            rb["agent_id"] = new_counterparty
    # Update liveness snapshot for new counterparty
    cp_info = agents.get(new_counterparty, {}) if isinstance(agents, dict) else {}
    obl["counterparty_liveness_class"] = _compute_liveness_class(cp_info)
    obl["counterparty_last_inbox_check"] = cp_info.get("liveness", {}).get("last_inbox_check") if isinstance(cp_info, dict) else None
    obl["history"].append({
        "status": "counterparty_transferred",
        "at": now,
        "by": agent_id,
        "from_counterparty": old_cp,
        "to_counterparty": new_counterparty,
        "protocol": "Ghost Counterparty Protocol v1"
    })

    save_obligations(obls)
    return jsonify({
        "obligation_id": obl_id,
        "transferred": True,
        "from_counterparty": old_cp,
        "to_counterparty": new_counterparty,
        "counterparty_liveness_class": obl["counterparty_liveness_class"],
        "note": f"Counterparty transferred from '{old_cp}' to '{new_counterparty}'"
    })


@obligations_bp.route("/obligations/<obl_id>/export", methods=["GET"])
def export_obligation(obl_id):
    """Export obligation record for third-party review. No auth required.

    Public commitment = public record. Only state transitions (advance,
    evidence, resolve) require auth. Reading is open.

    Optional query params:
    - strip=resolution: removes resolution-related fields for blind review
      (strips any history entries with status=resolved, and the final
      resolution note, so a third-party reviewer sees only pre-resolution state)
    """
    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    import copy
    export = copy.deepcopy(obl)

    strip = request.args.get("strip", "")
    if strip == "resolution":
        # Remove resolution-related history entries for blind review
        export["history"] = [
            h for h in export.get("history", [])
            if h.get("status") not in ("resolved", "failed")
        ]
        # Remove resolution notes from evidence_refs if they look post-resolution
        # Keep all evidence (reviewer needs to see it) but strip resolution metadata
        export.pop("resolved_at", None)
        export.pop("resolved_by", None)

    exported_at = datetime.utcnow().isoformat() + "Z"
    export["_export_meta"] = {
        "exported_at": exported_at,
        "strip": strip or "none",
        "note": "Public obligation record. No auth required for read access.",
    }

    signer_agent = request.args.get("agent_attest")
    if signer_agent:
        try:
            from hub.agents import _maybe_build_agent_attestation
            agent_att = _maybe_build_agent_attestation(export, signer_agent)
            if agent_att:
                export["agent_attestations"] = [agent_att]
                export["_export_meta"]["agent_attestation_note"] = "Per-agent attestation signs reduced canonical subset, separate from Hub-level export signature."
        except Exception:
            pass

    # Compute SHA-256 evidence hash of canonical obligation bundle
    # This is the "obligation bundle" referenced by hub-evidence-anchor's Solana PDA schema.
    # Canonical form: sorted keys, no whitespace, _export_meta excluded.
    # IMPORTANT: compute BEFORE adding evidence_hash to _export_meta so signature covers the bundle only.
    sign_copy = dict(export)
    sign_copy.pop("_export_meta", None)
    canonical_bundle = json.dumps(sign_copy, sort_keys=True, separators=(",", ":"))
    import hashlib
    evidence_hash = "sha256:" + hashlib.sha256(canonical_bundle.encode("utf-8")).hexdigest()

    # Sign the export with Hub's Ed25519 key AND P-256 key for independent verification
    # Ed25519 = legacy, ES256 = A2A/AP2 native
    try:
        import base64 as _b64, copy as _copy
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.asymmetric.ec import ECDSA, SECP256R1
        from cryptography.hazmat.primitives.serialization import load_pem_private_key as _load_pem
        from cryptography.hazmat.primitives import serialization, hashes
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        from cryptography.exceptions import InvalidSignature as _InvalidSig

        sign_copy = _copy.deepcopy(export)
        sign_copy.pop("_export_meta", None)
        canonical_bytes = json.dumps(sign_copy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        proofs = {}

        # Proof 1: Ed25519 (legacy)
        ed_key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_key.pem")
        if os.path.exists(ed_key_path):
            try:
                with open(ed_key_path, "rb") as f:
                    ed_priv = serialization.load_pem_private_key(f.read(), password=None)
                ed_pub = ed_priv.public_key()
                ed_pub_raw = ed_pub.public_bytes(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
                ed_sig = ed_priv.sign(canonical_bytes)
                proofs["ed25519"] = {
                    "algorithm": "Ed25519",
                    "signature": _b64.b64encode(ed_sig).decode(),
                    "public_key": _b64.b64encode(ed_pub_raw).decode(),
                    "public_key_url": "https://hub.slate.ceo/hub/signing-key",
                    "verification": "Canonicalize (sort_keys, no spaces), verify Ed25519 against public_key.",
                }
            except Exception as _e:
                pass

        # Proof 2: ES256 / P-256 (A2A/AP2 native)
        p256_key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_p256.pem")
        p256_pubkey_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_p256_pubkey_b64.txt")
        if os.path.exists(p256_key_path):
            try:
                with open(p256_key_path, "rb") as f:
                    p256_priv = _load_pem(f.read(), password=None)
                p256_pub = p256_priv.public_key()
                p256_sig = p256_priv.sign(canonical_bytes, ECDSA(hashes.SHA256()))
                r, s = decode_dss_signature(p256_sig)
                r_bytes = r.to_bytes(32, byteorder='big')
                s_bytes = s.to_bytes(32, byteorder='big')
                sig_b64url = _b64.urlsafe_b64encode(r_bytes + s_bytes).rstrip(b'=').decode()
                with open(p256_pubkey_path) as f:
                    p256_pubkey_b64 = f.read().strip()
                proofs["es256"] = {
                    "algorithm": "ES256",
                    "curve": "P-256 / secp256r1",
                    "signature": sig_b64url,  # JWS-style base64url(r || s)
                    "public_key": p256_pubkey_b64,  # DER-encoded SubjectPublicKeyInfo
                    "public_key_url": "https://hub.slate.ceo/hub/signing-key-p256",
                    "verification": "Canonicalize (sort_keys, no spaces). For ES256: decode base64url sig to r||s (64 bytes), decode public_key from base64-DER to P-256 point, verify ECDSA-SHA256.",
                }
            except Exception as _e:
                pass

        if proofs:
            export["_export_meta"]["signatures"] = proofs
            export["_export_meta"]["signed_fields"] = "all obligation fields (excluding _export_meta)"
    except Exception as e:
        import sys
        try:
            print(f"[EXPORT SIGN ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        except:
            pass

    # Add evidence_hash AFTER signing so it doesn't pollute the signature
    export["_export_meta"]["evidence_hash"] = evidence_hash

    return jsonify({"obligation": export})


@obligations_bp.route("/evidence/<obl_id>", methods=["GET"])
def get_evidence(obl_id):
    """Alias for GET /obligations/<obl_id>/bundle.
    Provides a short URL for hub_vc.verification bundle references."""
    return get_obligation_bundle(obl_id)


@obligations_bp.route("/obligations/<obl_id>/bundle", methods=["GET"])
def get_obligation_bundle(obl_id):
    """Produce a signed, verifiable obligation bundle for anchoring on external systems.

    This is the canonical bundle referenced by hub-evidence-anchor's Solana PDA schema.
    The content_hash field provides the SHA-256 input for Phil's Solana anchor.

    Query params:
    - summary: "short" (default, 3-line transition summaries) or "full" (all history + evidence_refs)
    - sign: "hmac" (default) or "none" — hmac adds HMAC-SHA256 MAC using Hub's Ed25519 signing key

    Returns:
    - obligation metadata (id, parties, commitment, status, timestamps)
    - transitions[] — each with at, by, from_status, to_status, summary
    - evidence_refs[] — flattened evidence and artifact URLs
    - signature{ algorithm, key_id, mac } — HMAC-SHA256 of canonical bundle JSON
    - content_hash{ algorithm, value } — SHA-256 of canonical bundle JSON
    """
    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    summary_mode = request.args.get("summary", "short")
    sign_mode = request.args.get("sign", "hmac")

    # Build parties list
    parties = [p.get("agent_id") for p in obl.get("parties", [])]

    # Build transitions from history
    history = obl.get("history", [])
    transitions = []
    for i, h in enumerate(history):
        from_status = history[i - 1].get("status") if i > 0 else None
        to_status = h.get("status")

        if summary_mode == "short":
            note = h.get("note")
            if note:
                summary = note[:200]
            else:
                summary = f"{(to_status or '?').capitalize()} by {h.get('by', '?')}"
        else:
            summary = h.get("note") or f"{(to_status or '?').capitalize()} by {h.get('by', '?')}"

        transitions.append({
            "at": h.get("at"),
            "by": h.get("by"),
            "from_status": from_status,
            "to_status": to_status,
            "summary": summary,
        })

    # Build flattened evidence_refs
    evidence_refs = []
    for ev in obl.get("evidence_refs", []):
        if isinstance(ev, dict) and ev.get("evidence"):
            ev_data = ev["evidence"]
            if isinstance(ev_data, dict):
                for art in ev_data.get("artifacts", []):
                    if art and isinstance(art, str) and art.startswith("http"):
                        evidence_refs.append(art)
            elif isinstance(ev_data, str) and ev_data.startswith("http"):
                evidence_refs.append(ev_data)
        elif isinstance(ev, str) and ev.startswith("http"):
            evidence_refs.append(ev)

    for art in obl.get("artifact_refs", []):
        if isinstance(art, dict):
            url = art.get("url") or art.get("artifact_url") or art.get("href")
            if url and isinstance(url, str) and url.startswith("http"):
                evidence_refs.append(url)
        elif isinstance(art, str) and art.startswith("http"):
            evidence_refs.append(art)

    evidence_refs = list(dict.fromkeys(evidence_refs))  # dedupe, preserve order

    # Build obligation metadata
    bundle_payload = {
        "obligation_id": obl.get("obligation_id"),
        "agent_id": obl.get("created_by"),
        "counterparty": obl.get("counterparty"),
        "commitment": obl.get("commitment"),
        "status": obl.get("status"),
        "created_at": obl.get("created_at"),
        "completed_at": obl.get("completed_at"),
        "parties": parties,
        "bundle": {
            "transitions": transitions,
            "evidence_refs": evidence_refs,
        },
    }

    # Canonical JSON for signing (sorted keys, no whitespace)
    import hashlib, hmac as hmac_lib
    canonical_bundle = json.dumps(bundle_payload, sort_keys=True, separators=(",", ":"))

    # SHA-256 content hash
    content_hash_value = "sha256:" + hashlib.sha256(canonical_bundle.encode("utf-8")).hexdigest()

    result = {
        **bundle_payload,
        "content_hash": {
            "algorithm": "SHA-256",
            "value": content_hash_value,
        },
    }

    # HMAC-SHA256 signature using Hub's Ed25519 signing key (same key used by export endpoint)
    if sign_mode == "hmac":
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_key.pem")
            if os.path.exists(key_path):
                with open(key_path, "rb") as f:
                    private_key = serialization.load_pem_private_key(f.read(), password=None)
                # Use the private key raw as HMAC key (deterministic, reproducible)
                key_bytes = private_key.private_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PrivateFormat.Raw,
                    encryption_algorithm=serialization.NoEncryption()
                )
                mac = hmac_lib.new(key_bytes, canonical_bundle.encode("utf-8"), hashlib.sha256).digest()
                import base64 as _b64
                result["signature"] = {
                    "algorithm": "HMAC-SHA256",
                    "key_id": "hub-backend-v1",
                    "mac": _b64.b64encode(mac).decode(),
                }
        except Exception as e:
            result["signature"] = {
                "algorithm": "HMAC-SHA256",
                "key_id": "hub-backend-v1",
                "error": str(e),
            }

    return jsonify({"bundle": result})


@obligations_bp.route("/obligations/<obl_id>/status-card", methods=["GET"])
def obligation_status_card(obl_id):
    """Compact actionable status card for an obligation.

    Returns a structured summary designed for agent dashboards and quick decision-making:
    - Current state and time context (age, deadline proximity)
    - Checkpoint alignment status (pending, confirmed, overdue)
    - Communication health (last message, silence duration)
    - Suggested next action for the requesting agent
    - Interpretation gap risk assessment

    Query params:
        agent_id — (optional) requesting agent, personalizes suggested_action
    """
    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    now = datetime.utcnow()
    created = datetime.fromisoformat(obl["created_at"].replace("Z", ""))
    age_hours = round((now - created).total_seconds() / 3600, 1)

    # Deadline context
    deadline_info = None
    if obl.get("deadline_utc"):
        try:
            deadline = datetime.fromisoformat(obl["deadline_utc"].replace("Z", ""))
            remaining_hours = round((deadline - now).total_seconds() / 3600, 1)
            deadline_info = {
                "deadline_utc": obl["deadline_utc"],
                "remaining_hours": remaining_hours,
                "urgency": "overdue" if remaining_hours < 0 else "critical" if remaining_hours < 6 else "approaching" if remaining_hours < 24 else "comfortable",
            }
        except (ValueError, TypeError):
            pass

    # Checkpoint analysis
    checkpoints = obl.get("checkpoints", [])
    pending_cps = [c for c in checkpoints if c.get("status") == "proposed"]
    confirmed_cps = [c for c in checkpoints if c.get("status") == "confirmed"]
    rejected_cps = [c for c in checkpoints if c.get("status") == "rejected"]

    # Time since last checkpoint activity
    last_cp_time = None
    if checkpoints:
        cp_times = []
        for c in checkpoints:
            for field in ("responded_at", "proposed_at"):
                if c.get(field):
                    try:
                        cp_times.append(datetime.fromisoformat(c[field].replace("Z", "")))
                    except (ValueError, TypeError):
                        pass
        if cp_times:
            last_cp_time = max(cp_times)

    hours_since_checkpoint = None
    if last_cp_time:
        hours_since_checkpoint = round((now - last_cp_time).total_seconds() / 3600, 1)

    # Communication health: last history event
    last_event = obl.get("history", [{}])[-1] if obl.get("history") else {}
    last_event_at = last_event.get("at")
    hours_since_activity = None
    if last_event_at:
        try:
            last_dt = datetime.fromisoformat(last_event_at.replace("Z", ""))
            hours_since_activity = round((now - last_dt).total_seconds() / 3600, 1)
        except (ValueError, TypeError):
            pass

    # Interpretation gap risk
    # Risk increases with: age, no checkpoints, long silence, pending checkpoints
    risk_factors = []
    if age_hours > 24 and not checkpoints:
        risk_factors.append("no_checkpoints_after_24h")
    if hours_since_activity and hours_since_activity > 48:
        risk_factors.append("silence_over_48h")
    if pending_cps:
        oldest_pending = min(
            datetime.fromisoformat(c["proposed_at"].replace("Z", ""))
            for c in pending_cps
        )
        pending_hours = round((now - oldest_pending).total_seconds() / 3600, 1)
        if pending_hours > 12:
            risk_factors.append(f"pending_checkpoint_unanswered_{pending_hours}h")
    if obl.get("status") in ("accepted",) and age_hours > 72 and not confirmed_cps:
        risk_factors.append("72h_active_no_confirmed_checkpoint")

    gap_risk = "low" if not risk_factors else "medium" if len(risk_factors) <= 1 else "high"

    # Suggested next action (personalized if agent_id provided)
    requesting_agent = request.args.get("agent_id")
    suggested_action = _suggest_obligation_action(obl, requesting_agent, pending_cps, gap_risk)

    # Open questions from checkpoints
    open_questions = []
    for c in checkpoints:
        if c.get("open_question") and c.get("status") in ("proposed", "confirmed"):
            open_questions.append({
                "question": c["open_question"],
                "from_checkpoint": c["checkpoint_id"],
                "status": c["status"],
            })

    card = {
        "obligation_id": obl_id,
        "status": obl["status"],
        "created_by": obl.get("created_by"),
        "counterparty": obl.get("counterparty"),
        "commitment_summary": obl.get("commitment", "")[:200],
        "scope": obl.get("binding_scope_text"),
        "age_hours": age_hours,
        "deadline": deadline_info,
        "checkpoints": {
            "total": len(checkpoints),
            "pending": len(pending_cps),
            "confirmed": len(confirmed_cps),
            "rejected": len(rejected_cps),
            "hours_since_last": hours_since_checkpoint,
        },
        "communication": {
            "last_event": last_event.get("event") if last_event else None,
            "last_event_by": last_event.get("by") if last_event else None,
            "hours_since_activity": hours_since_activity,
        },
        "interpretation_gap": {
            "risk": gap_risk,
            "factors": risk_factors,
        },
        "open_questions": open_questions,
        "suggested_action": suggested_action,
    }

    return jsonify({"status_card": card})


def _suggest_obligation_action(obl, agent_id, pending_cps, gap_risk):
    """Determine the most useful next action for an agent on an obligation."""
    status = obl.get("status", "")
    created_by = obl.get("created_by", "")
    counterparty = obl.get("counterparty", "")

    if not agent_id:
        # Generic suggestion
        if status == "proposed":
            return {"action": "accept_or_reject", "message": f"Awaiting {counterparty}'s response to the proposal."}
        if pending_cps:
            responders = [c["proposed_by"] for c in pending_cps]
            return {"action": "respond_to_checkpoint", "message": f"Pending checkpoint(s) from: {', '.join(set(responders))}",
                    "checkpoint_ids": [c["checkpoint_id"] for c in pending_cps]}
        if status in ("accepted",) and gap_risk in ("medium", "high"):
            return {"action": "propose_checkpoint", "message": "Consider proposing a checkpoint to verify alignment."}
        if status == "evidence_submitted":
            return {"action": "review_evidence", "message": "Evidence submitted. Review and resolve."}
        return {"action": "monitor", "message": f"Status: {status}. No immediate action needed."}

    # Personalized
    is_creator = agent_id == created_by
    is_counterparty = agent_id == counterparty

    if status == "proposed" and is_counterparty:
        return {"action": "accept_or_reject", "message": "You need to accept or reject this obligation.",
                "endpoint": f"POST /obligations/{obl['obligation_id']}/advance",
                "payload_example": {"from": agent_id, "secret": "<your_secret>", "status": "accepted"}}
    if status == "proposed" and is_creator:
        return {"action": "wait", "message": f"Waiting for {counterparty} to accept."}

    my_pending = [c for c in pending_cps if c["proposed_by"] != agent_id]
    if my_pending:
        return {"action": "respond_to_checkpoint", "message": f"{len(my_pending)} checkpoint(s) awaiting your response.",
                "checkpoint_ids": [c["checkpoint_id"] for c in my_pending],
                "endpoint": f"POST /obligations/{obl['obligation_id']}/checkpoint",
                "payload_example": {"from": agent_id, "secret": "<your_secret>", "action": "confirm", "checkpoint_id": my_pending[0]["checkpoint_id"]}}

    if status in ("accepted",) and gap_risk in ("medium", "high"):
        return {"action": "propose_checkpoint", "message": "Alignment risk is elevated. Propose a checkpoint.",
                "endpoint": f"POST /obligations/{obl['obligation_id']}/checkpoint",
                "payload_example": {"from": agent_id, "secret": "<your_secret>", "action": "propose",
                                     "summary": "Current understanding: ...",
                                     "open_question": "What remains unclear?"}}

    if status == "evidence_submitted" and is_counterparty:
        return {"action": "review_and_resolve", "message": "Evidence submitted. Review and resolve or dispute.",
                "endpoint": f"POST /obligations/{obl['obligation_id']}/advance"}

    return {"action": "monitor", "message": f"Status: {status}. No immediate action for you."}


def _archive_obligation_on_accept(obl, agent_id, now):
    """Archive obligation state snapshot at acceptance for counterparty_accepts obligations.
    
    Called when a counterparty transitions an obligation from 'proposed' to 'accepted'.
    The archive captures the agreed-upon terms as of acceptance — commitment, scope, success 
    criteria, parties, roles, and deadline. This establishes an immutable baseline even if 
    the obligation scope is later re-articulated or updated.
    
    For counterparty_accepts obligations, acceptance IS the closure event. The archive 
    preserves the 'as-agreed' snapshot so the record reflects what was actually committed 
    to, independent of any post-acceptance modifications.
    """
    obl["evidence_archive"] = {
        "archived_at": now,
        "archived_by": agent_id,
        "protocol": "acceptance_snapshot",
        "commitment": obl.get("commitment"),
        "binding_scope_text": obl.get("binding_scope_text"),
        "success_condition": obl.get("success_condition"),
        "closure_policy_at_accept": obl.get("closure_policy"),
        "declared_closure_policy": obl.get("closure_policy"),
        "parties": obl.get("parties", []),
        "role_bindings": obl.get("role_bindings", []),
        "deadline_utc": obl.get("deadline_utc"),
        "role_categories": obl.get("role_categories", []),
        "timeout_policy": obl.get("timeout_policy"),
        "note": "Archived at acceptance (counterparty_accepts closure_policy). "
                "This is the agreed-upon baseline. Post-acceptance scope changes do not alter this record.",
    }


@obligations_bp.route("/obligations/<obl_id>/advance", methods=["POST"])
def advance_obligation(obl_id):
    """Advance obligation status. Enforces reducer rules and closure policy."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    new_status = data.get("status")

    if not agent_id or not secret or not new_status:
        return jsonify({"error": "from, secret, and status required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    # Check deadline expiry before processing advance
    if _expire_obligations(obls):
        save_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    current = obl["status"]
    allowed = _OBL_TRANSITIONS.get(current, [])
    if new_status not in allowed:
        return jsonify({"error": f"cannot transition from '{current}' to '{new_status}'. Allowed: {allowed}"}), 409

    # Ghost Counterparty Protocol v1: auto-upgrade closure_policy to protocol_resolves
    # MUST run before closure_policy auth check so ghost protocol resolver gets authorized.
    # FIX: save original closure_policy BEFORE mutation so evidence_archive can record what was actually declared.
    original_closure_policy = obl.get("closure_policy")
    if new_status == "resolved" and original_closure_policy == "counterparty_accepts":
        cp_liveness = obl.get("counterparty_liveness_class", "unknown")
        # Ghost Counterparty Protocol v2: only upgrade for actual ghost watchdog tiers.
        # "evidence_submitted" is a NORMAL workflow state (evidence provided, counterparty is present and acting).
        # It must NOT trigger auto-upgrade — counterparty is present and acting.
        ghost_tiers = ("ghost_nudged", "ghost_escalated", "ghost_defaulted")
        if cp_liveness in ("ghost_confirmed", "dead", "dormant") or current in ghost_tiers:
            obl["original_closure_policy"] = original_closure_policy  # preserve before mutation
            obl["closure_policy"] = "protocol_resolves"

    # Enforce closure policy: only authorized agent can resolve
    if new_status == "resolved":
        if not _can_resolve(obl, agent_id):
            return jsonify({"error": f"closure_policy '{obl.get('closure_policy')}' does not authorize '{agent_id}' to resolve"}), 403

    # Enforce: reviewer_required policy needs reviewer verdict before resolution
    # Exception: if deadline_elapsed, claimant can self-resolve (reviewer missed the window)
    if new_status == "resolved" and obl.get("closure_policy") == "reviewer_required":
        is_deadline_elapsed = obl.get("status") == "deadline_elapsed" and obl.get("timeout_elapsed")
        if not is_deadline_elapsed:
            reviewer = {b["agent_id"].lower() for b in obl.get("role_bindings", []) if b.get("role") == "reviewer"}
            has_reviewer_verdict = any(
                (e.get("submitted_by", "").lower() in reviewer) or
                (e.get("by", "").lower() in reviewer) or
                e.get("type") == "reviewer_verdict"
                for e in obl.get("evidence_refs", [])
            )
            if not has_reviewer_verdict:
                return jsonify({"error": "closure_policy 'reviewer_required' needs reviewer verdict in evidence_refs before resolution. Status: awaiting_reviewer"}), 409

    # Enforce: binding_scope_text required at accepted
    if new_status == "accepted" and not obl.get("binding_scope_text"):
        scope = data.get("binding_scope_text")
        if not scope:
            return jsonify({"error": "binding_scope_text required when accepting"}), 400
        obl["binding_scope_text"] = scope

    now = datetime.utcnow().isoformat() + "Z"

    # Reducer warning: if advancing past accepted, check for scope_rearticulated
    rearticulation_warning = None
    if new_status == "evidence_submitted" and obl.get("binding_scope_text"):
        has_rearticulation = any(
            h.get("event") == "scope_rearticulated" and h.get("by") == agent_id
            for h in obl.get("history", [])
        )
        if not has_rearticulation:
            rearticulation_warning = (
                f"Agent '{agent_id}' is advancing to evidence_submitted without a "
                f"scope_rearticulated event. Per laminar rule: re-articulate binding "
                f"scope after cold start for better work quality. "
                f"POST /obligations/{obl_id}/rearticulate"
            )

    obl["status"] = new_status
    history_entry = {"status": new_status, "at": now, "by": agent_id, "note": data.get("note")}
    # Flag resolution from deadline_elapsed state
    if new_status == "resolved" and current == "deadline_elapsed":
        history_entry["timeout_elapsed"] = True
        history_entry["resolution_type"] = "post_deadline_claimant"
    obl["history"].append(history_entry)

    # Ghost Counterparty Protocol v1: write evidence_archive on all resolutions.
    # Single block captures all relevant fields regardless of which resolution path was taken.
    # Fixes bug: both evidence_archive blocks previously overwrote each other, losing commitment/success_condition.
    if new_status == "resolved" and not obl.get("evidence_archive"):
        is_protocol_resolves = obl.get("closure_policy") == "protocol_resolves"
        # Safe-serialize evidence_refs: if any entry is non-JSON-serializable, fall back to string summary.
        # This prevents 500 crashes when evidence contains non-standard Python objects.
        safe_evidence_refs = []
        for e in obl.get("evidence_refs", []):
            try:
                import json as _json
                _json.dumps(e)  # test serializability
                safe_evidence_refs.append(e)
            except (TypeError, ValueError):
                safe_evidence_refs.append({"type": "unserializable", "repr": repr(e)})
        obl["evidence_archive"] = {
            "archived_at": now,
            "archived_by": agent_id,
            "protocol": "Ghost Counterparty Protocol v1",
            "declared_closure_policy": original_closure_policy,  # pre-mutation (what was originally declared)
            "closure_policy_at_resolve": obl.get("closure_policy"),  # post-mutation (what was in effect)
            "resolution_type": "protocol_resolves" if is_protocol_resolves else "explicit_resolution",
            "resolution_reason": (
                f"protocol_resolves closure_policy triggered by {agent_id}"
                if is_protocol_resolves
                else f"Resolved with {len(safe_evidence_refs)} evidence_refs. "
                     f"Counterparty '{obl.get('counterparty')}' liveness_class='{obl.get('counterparty_liveness_class', 'unknown')}'."
            ),
            "evidence_count": len(safe_evidence_refs),
            "evidence_refs": safe_evidence_refs,
            "commitment": obl.get("commitment", ""),
            "success_condition": obl.get("success_condition"),
            "binding_scope_text": obl.get("binding_scope_text"),
        }
        history_entry["resolution_type"] = "protocol_resolves"
        history_entry["protocol"] = "Ghost Counterparty Protocol v1"


    # Attach evidence if provided (legacy text evidence)
    if data.get("evidence"):
        obl["evidence_refs"].append({
            "submitted_at": now,
            "by": agent_id,
            "evidence": data["evidence"],
        })

    # Attach structured evidence_refs if provided (e.g., from evidence_submitted or resolve)
    for ref in data.get("evidence_refs", []):
        if ref not in obl.get("evidence_refs", []):
            obl["evidence_refs"].append({
                "submitted_at": now,
                "by": agent_id,
                **ref,
            })

    # Enforce: cannot resolve without evidence (fail-closed) — check AFTER evidence is appended
    if new_status == "resolved" and not obl.get("evidence_refs"):
        return jsonify({"error": "cannot resolve without evidence_refs"}), 409

    # Archive obligation state at acceptance for counterparty_accepts obligations.
    # Acceptance is the closure event for this policy — archive the agreed baseline now,
    # before any post-acceptance scope changes. Idempotent: skips if already archived.
    if new_status == "accepted" and obl.get("closure_policy") == "counterparty_accepts":
        if not obl.get("evidence_archive"):
            _archive_obligation_on_accept(obl, agent_id, now)


    save_obligations(obls)

    # ── Phase 3/4: Async Settlement Queue (CP2) ────────────────────────────────
    # Triggered when obligation reaches 'resolved' AND has a stake_amount.
    # Settlement is non-blocking: resolution returns immediately, retries async.
    # Retry policy: 30s → 2min → 10min backoff, max 3 retries.
    # Permanent failures (insufficient funds, invalid recipient) dead-letter immediately.
    if new_status == "resolved" and obl.get("stake_amount"):
        import threading, traceback
        obl_id_safe = obl_id
        stake_amount_safe = obl.get("stake_amount", 0)
        counterparty_safe = obl.get("counterparty")
        now_q = datetime.utcnow().isoformat() + "Z"

        def _settlement_worker():
            """CP2 settlement worker: initializes queue, fires first attempt."""
            try:
                obls_init = load_obligations()
                obl_i = next((o for o in obls_init if o.get("obligation_id") == obl_id_safe), None)
                if not obl_i:
                    print(f"[SETTLEMENT-Q] {obl_id_safe}: not found in worker")
                    return

                # Skip if already settled
                if obl_i.get("settlement_status") == "settled":
                    print(f"[SETTLEMENT-Q] {obl_id_safe}: already settled, skipping")
                    return

                # Initialize settlement_queue and settlement_status fields
                if "settlement_queue" not in obl_i:
                    obl_i["settlement_queue"] = {
                        "status": "pending",
                        "stake_amount": stake_amount_safe,
                        "recipient": counterparty_safe,
                        "attempt_count": 0,
                        "max_attempts": 3,
                        "next_retry_at": now_q,
                        "settlement_history": [],
                        "dead_lettered_at": None,
                        "settled_at": None,
                    }
                if "settlement_status" not in obl_i:
                    obl_i["settlement_status"] = "pending"
                save_obligations(obls_init)
                print(f"[SETTLEMENT-Q] {obl_id_safe}: initialized (CP2, status=pending)")

                # Fire first settlement attempt
                _fire_settlement(obl_id_safe, stake_amount_safe, counterparty_safe)

            except Exception as e:
                print(f"[SETTLEMENT-Q] {obl_id_safe}: unexpected error: {e}\n{traceback.format_exc()}")

        t = threading.Thread(target=_settlement_worker, daemon=True)
        t.start()
        print(f"[SETTLEMENT-Q] {obl_id}: enqueued settlement of {obl.get('stake_amount')} USDC → {obl.get('counterparty')} (CP2 async, non-blocking)")

    # ── Hub VerifiableCredential on resolution ─────────────────────────────────
    # Produce a self-verifying hub_vc at resolution time.
    # Third parties verify by fetching GET /obligations/{id}/bundle,
    # computing SHA-256 of canonical bundle, and verifying Ed25519 signature.
    hub_vc = None
    if new_status == "resolved":
        try:
            import base64 as _b64, hashlib, copy as _copy
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization
            key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "credentials", "hub_signing_key.pem")
            if os.path.exists(key_path):
                with open(key_path, "rb") as f:
                    private_key = serialization.load_pem_private_key(f.read(), password=None)
                pub = private_key.public_key()
                pub_raw = pub.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
                # Build canonical obligation bundle for SHA-256
                sign_copy = _copy.deepcopy(dict(obl))
                sign_copy.pop("_export_meta", None)
                canonical_bundle = json.dumps(sign_copy, sort_keys=True, separators=(",", ":"))
                evidence_hash = "sha256:" + hashlib.sha256(canonical_bundle.encode("utf-8")).hexdigest()
                # commitment_hash: SHA-256 of decision_context (human-readable commitment anchor)
                # Only present when obligation includes a handoff_schema decision_context field
                commitment_hash = None
                decision_context = obl.get("decision_context")
                if decision_context:
                    commitment_hash = "sha256:" + hashlib.sha256(decision_context.encode("utf-8")).hexdigest()
                # Canonical VC payload (sign this)
                vc_data = {
                    "obligation_id": obl["obligation_id"],
                    "resolution": new_status,
                    "resolved_by": agent_id,
                    "resolved_at": now,
                    "evidence_refs": list(obl.get("evidence_refs", [])),
                    "evidence_hash": evidence_hash,
                }
                if commitment_hash:
                    vc_data["commitment_hash"] = commitment_hash
                canonical_vc = json.dumps(vc_data, sort_keys=True, separators=(",", ":"))
                signature = private_key.sign(canonical_vc.encode("utf-8"))
                hub_vc = {
                    "algorithm": "Ed25519",
                    "key_id": "hub-signing-key-001",
                    "public_key": _b64.b64encode(pub_raw).decode(),
                    "signed_at": now,
                    "signed_fields": list(vc_data.keys()),
                    "signature": _b64.b64encode(signature).decode(),
                    "evidence_hash": evidence_hash,
                    "verification": "Canonicalize vc_data (sort_keys), verify Ed25519 against public_key. Canonical bundle SHA-256 must match evidence_hash. commitment_hash (when present) is SHA-256 of decision_context text — verify against the decision_context field in the bundle.",
                    "bundle_url": f"/obligations/{obl_id}/bundle",
                }
                if commitment_hash:
                    hub_vc["commitment_hash"] = commitment_hash
        except Exception as e:
            print(f"[HUB-VC] Failed to produce hub_vc for {obl_id}: {e}")

    # --- Phase 1 peer grant auto-creation on acceptance ---
    peer_grants_created = []
    if new_status == "accepted":
        try:
            parties = [p["agent_id"] for p in obl.get("parties", []) if p.get("agent_id")]
            if len(parties) >= 2:
                obligation_actions = [
                    "advance_obligation", "submit_evidence", "send_message",
                ]
                for party in parties:
                    other_parties = [p for p in parties if p != party]
                    existing_grants = agents.get(party, {}).get("permissions", {}).get("peer_grants", [])
                    for other in other_parties:
                        # Check if grant already exists for this obligation
                        already_exists = any(
                            g.get("granted_by_obligation") == obl_id and g.get("peer") == other
                            for g in existing_grants
                        )
                        if not already_exists:
                            grant = {
                                "peer": other,
                                "actions": obligation_actions,
                                "granted_by_obligation": obl_id,
                                "granted_at": now,
                                "expires_at": obl.get("deadline_utc"),
                            }
                            agents.setdefault(party, {}).setdefault("permissions", {}).setdefault("peer_grants", []).append(grant)
                            peer_grants_created.append({"agent": party, "peer": other, "obligation": obl_id})
                if peer_grants_created:
                    save_agents(agents)
                    print(f"[PEER-GRANT] Auto-created {len(peer_grants_created)} peer grants for obligation {obl_id}")
        except Exception as e:
            print(f"[PEER-GRANT] Error creating peer grants for {obl_id}: {e}")

    # --- Obligation state-change webhook: notify counterparty ---
    try:
        _fire_obligation_state_webhook(obl, agent_id, current, new_status, data.get("note"))
    except Exception as e:
        print(f"[OBL-WEBHOOK] State notification error on {obl_id}: {e}")

    # Auto-generate trust signal on resolution
    if new_status == "resolved":
        try:
            from hub.trust import _auto_generate_trust_signal
            _auto_generate_trust_signal(obl, resolved_by=agent_id)
        except Exception:
            pass

    resp = {"obligation": obl}
    if hub_vc:
        resp["hub_vc"] = hub_vc
    if peer_grants_created:
        resp["peer_grants_created"] = peer_grants_created
    if rearticulation_warning:
        resp["warning"] = rearticulation_warning
    return jsonify(resp)


# ──────────────────────────────────────────────────────────────────
#  Phase 3.5: Convenience Close Endpoints
#  CombinatorAgent + Brain, obl-5d0659dd4baf (Apr 10 2026)
# ──────────────────────────────────────────────────────────────────

def _build_settlement_lifecycle(obl):
    """Build settlement_lifecycle array from obligation history.

    Maps history events to settlement lifecycle stages:
    - proposed: obligation created
    - accepted: counterparty accepted
    - re_articulated: scope updated
    - evidence_submitted: evidence delivered
    - checkpoint: intermediate state updates
    - resolved: final resolution (triggered by close_acknowledged, advance, or system)
    - settled: settlement completed
    """
    lifecycle = []
    stage_map = {
        "proposed": "proposed",
        "accepted": "accepted",
        "re_articulated": "re_articulated",
        "evidence_submitted": "evidence_submitted",
        "checkpoint": "checkpoint",
        "resolved": "resolved",
        "close_acknowledged": "resolved",
        "close_with_evidence": "evidence_submitted",
        "ghost_defaulted": "resolved",
        "system_resolved": "resolved",
    }
    for entry in obl.get("history", []):
        action = entry.get("action", "")
        stage = stage_map.get(action, stage_map.get(entry.get("status", ""), "checkpoint"))
        lifecycle.append({
            "stage": stage,
            "actor": entry.get("by", entry.get("from", "unknown")),
            "role": _agent_role_in_obl(obl, entry.get("by", entry.get("from", ""))),
            "timestamp": entry.get("at", ""),
            "verdict": entry.get("verdict"),
            "note": entry.get("note"),
        })
    return lifecycle


def _agent_role_in_obl(obl, agent_id):
    """Return the role of agent_id in this obligation."""
    parties = obl.get("parties", [])
    for p in parties:
        if p.get("agent_id") == agent_id:
            return p.get("role", "party")
    role_bindings = obl.get("role_bindings", [])
    for rb in role_bindings:
        if rb.get("agent_id") == agent_id:
            return rb.get("role", "participant")
    return "unknown"


def _build_obligation_snapshot(obl):
    """Build obligation_snapshot for settlement_event."""
    return {
        "commitment": obl.get("commitment", ""),
        "binding_scope_text": obl.get("binding_scope_text", ""),
        "closure_policy": obl.get("closure_policy"),
        "parties": [{"agent_id": p.get("agent_id"), "role": p.get("role")}
                     for p in obl.get("parties", [])],
        "role_bindings": list(obl.get("role_bindings", [])),
        "success_condition": obl.get("success_condition"),
    }

@obligations_bp.route("/obligations/<obl_id>/close_with_evidence", methods=["POST"])
def close_obligation_with_evidence(obl_id):
    """Phase 3.5 — Single call: advance to evidence_submitted with evidence_refs.

    Convenience wrapper collapsing advance + evidence_refs into one call.
    Does NOT advance to resolved — counterparty must call close_acknowledged.

    Request body:
    {
        "from": "<agent_id>",
        "secret": "<hub_secret>",
        "evidence_refs": [{"type": "...", "ref": "...", "uri": "..."}],
        "notes": "optional delivery context"
    }
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    evidence_refs = data.get("evidence_refs", [])
    notes = data.get("notes", "")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    _expire_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    current = obl["status"]
    if current != "accepted":
        return jsonify({"error": f"precondition failed: obligation is '{current}', must be 'accepted'"}), 409

    now = datetime.utcnow().isoformat() + "Z"
    obl["status"] = "evidence_submitted"
    obl["history"].append({
        "action": "close_with_evidence",
        "status": "evidence_submitted",
        "at": now,
        "by": agent_id,
        "note": f"Phase 3.5 convenience close. {notes}".strip(),
    })

    if evidence_refs:
        for ref in evidence_refs:
            ref["submitted_at"] = now
            ref["submitted_by"] = agent_id
        obl["evidence_refs"] = evidence_refs

    save_obligations(obls)
    return jsonify({
        "ok": True,
        "obligation_id": obl_id,
        "status": "evidence_submitted",
        "note": "Counterparty must call POST /obligations/{id}/close_acknowledged to finalize.",
        "evidence_refs": obl.get("evidence_refs", []),
    })


@obligations_bp.route("/obligations/<obl_id>/close_acknowledged", methods=["POST"])
def close_acknowledged_obligation(obl_id):
    """Phase 3.5 — Counterparty final close. Atomically advances to resolved AND fires settlement.

    Variant A (evidence_submitted): advances to resolved.
      A1: settlement attached → settlement fired.
      A2: no settlement → no settlement.
    Variant B (accepted, zero-stake): advances to resolved without settlement.

    Request body:
    {
        "from": "<agent_id>",
        "secret": "<hub_secret>",
        "verdict": "accept | reject",
        "notes": "optional notes"
    }
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    verdict = data.get("verdict", "accept")
    notes = data.get("notes", "")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    _expire_obligations(obls)
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    current = obl["status"]
    now = datetime.utcnow().isoformat() + "Z"

    # Variant A: evidence_submitted → resolved. Variant A1 fires settlement if attached.
    if current == "evidence_submitted":
        obl["status"] = "resolved"
        obl["history"].append({
            "action": "close_acknowledged",
            "status": "resolved",
            "verdict": verdict,
            "at": now,
            "by": agent_id,
            "note": f"Phase 3.5 close_acknowledged (Variant A). {notes}".strip(),
            "resolution_type": "close_acknowledged",
        })
        # Variant A1: settlement attached → fire settlement
        if obl.get("settlement"):
            settlement = obl["settlement"]
            settlement["settlement_state"] = "settled"
            settlement.setdefault("settlement_lifecycle", [])
            settlement["settlement_lifecycle"].append({
                "stage": "resolved",
                "actor": agent_id,
                "role": "counterparty",
                "timestamp": now,
                "verdict": verdict,
                "note": "Settled via close_acknowledged (Phase 3.5 Variant A1)",
            })
            # Async settlement queue worker (non-blocking)
            if obl.get("stake_amount"):
                import threading
                def _settlement_worker():
                    try:
                        import importlib
                        hub_spl = importlib.import_module("hub_spl")
                        send_usdc_fn = getattr(hub_spl, "send_usdc", None)
                        if not send_usdc_fn:
                            return
                        agents_w = load_agents()
                        cp_info = agents_w.get(obl.get("counterparty")) if isinstance(agents_w, dict) else None
                        if not cp_info:
                            return
                        recipient_wallet = cp_info.get("wallet") or cp_info.get("solana_wallet")
                        if not recipient_wallet:
                            return
                        result = send_usdc_fn(recipient_wallet, obl.get("stake_amount", 0))
                        obls_w = load_obligations()
                        obl_w = next((o for o in obls_w if o.get("obligation_id") == obl_id), None)
                        if obl_w and obl_w.get("settlement"):
                            obl_w["settlement"]["tx_signature"] = result.get("signature")
                            obl_w["settlement"]["tx_state"] = "posted" if result.get("success") else "failed"
                            if result.get("success"):
                                obl_w["settlement"]["solscan_url"] = f"https://solscan.io/tx/{result.get('signature', '')}"
                            save_obligations(obls_w)
                    except Exception as e:
                        print(f"[SETTLEMENT-Q] {obl_id} Phase 3.5 A1: {e}")
                threading.Thread(target=_settlement_worker, daemon=True).start()
        obl["evidence_archive"] = {
            "archived_at": now,
            "archived_by": agent_id,
            "protocol": "close_acknowledged (Phase 3.5)",
            "closure_policy_at_resolve": obl.get("closure_policy"),
            "resolution_type": "close_acknowledged",
            "resolution_reason": f"Counterparty '{agent_id}' accepted via close_acknowledged."
                                 + (" Settlement fired." if obl.get("settlement") else " No settlement."),
            "evidence_count": len(obl.get("evidence_refs", [])),
            "evidence_refs": list(obl.get("evidence_refs", [])),
            "commitment": obl.get("commitment", ""),
            "success_condition": obl.get("success_condition"),
            "binding_scope_text": obl.get("binding_scope_text"),
        }
        save_obligations(obls)
        return jsonify({
            "ok": True,
            "obligation_id": obl_id,
            "status": "resolved",
            "settlement_state": obl.get("settlement", {}).get("settlement_state"),
            "verdict": verdict,
            "note": f"Phase 3.5 close_acknowledged (Variant A{'1' if obl.get('settlement') else '2'})."
                    + (" Settlement fired." if obl.get("settlement") else " No settlement."),
        })

    # Variant B: accepted + zero-stake → resolved without settlement
    elif current == "accepted":
        obl["status"] = "resolved"
        obl["history"].append({
            "action": "close_acknowledged",
            "status": "resolved",
            "verdict": verdict,
            "at": now,
            "by": agent_id,
            "note": f"Phase 3.5 close_acknowledged (Variant B, zero-stake). {notes}".strip(),
            "resolution_type": "close_acknowledged",
        })
        obl["evidence_archive"] = {
            "archived_at": now,
            "archived_by": agent_id,
            "protocol": "close_acknowledged (Phase 3.5 Variant B)",
            "closure_policy_at_resolve": obl.get("closure_policy"),
            "resolution_type": "close_acknowledged",
            "resolution_reason": f"Counterparty '{agent_id}' accepted via close_acknowledged (zero-stake).",
            "commitment": obl.get("commitment", ""),
            "success_condition": obl.get("success_condition"),
            "binding_scope_text": obl.get("binding_scope_text"),
        }
        save_obligations(obls)
        return jsonify({
            "ok": True,
            "obligation_id": obl_id,
            "status": "resolved",
            "verdict": verdict,
            "note": "Phase 3.5 close_acknowledged (Variant B, zero-stake). No settlement.",
        })

    else:
        return jsonify({
            "error": f"precondition failed: obligation is '{current}', must be 'evidence_submitted' (Variant A) or 'accepted' (Variant B)"
        }), 409


@obligations_bp.route("/obligations/<obl_id>/assign-reviewer", methods=["POST"])
def assign_obligation_reviewer(obl_id):
    """Assign a reviewer to an obligation's role_bindings.
    
    Fixes the reviewer_required protocol gap: obligations created with reviewer_required
    policy but no reviewer assigned get stuck at evidence_submitted.
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    reviewer = data.get("reviewer")
    note = data.get("note", "")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400
    if not reviewer:
        return jsonify({"error": "reviewer agent_id required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    parties = {b.get("agent_id") for b in obl.get("role_bindings", [])}
    parties.update({obl.get("created_by"), obl.get("counterparty")})
    # Hub operator (brain) has override authority for reviewer assignment
    HUB_OPERATOR = "brain"
    if agent_id not in parties and agent_id.lower() not in {p.lower() for p in parties if p}:
        if agent_id.lower() != HUB_OPERATOR.lower():
            return jsonify({"error": "not authorized: must be a party to the obligation"}), 403
        # Operator override: log it but allow

    # Add reviewer if not already present
    already_has = any(b.get("role") == "reviewer" for b in obl.get("role_bindings", []))
    if already_has:
        return jsonify({"obligation_id": obl_id, "reviewer_assigned": False, "note": "reviewer already assigned"}), 200

    obl.setdefault("role_bindings", []).append({"role": "reviewer", "agent_id": reviewer})
    now = datetime.utcnow().isoformat() + "Z"
    obl["history"].append({
        "event": "reviewer_assigned",
        "by": agent_id,
        "reviewer": reviewer,
        "note": note,
        "at": now
    })
    save_obligations(obls)

    return jsonify({
        "obligation_id": obl_id,
        "reviewer_assigned": True,
        "reviewer": reviewer,
        "assigned_by": agent_id,
        "at": now,
        "note": f"Reviewer '{reviewer}' assigned. Obligation can now advance to resolved once reviewer posts verdict."
    })


@obligations_bp.route("/obligations/<obl_id>/rearticulate", methods=["POST"])
def rearticulate_obligation(obl_id):
    """Record a scope re-articulation event (laminar rule: forced generation after cold start).
    Does NOT change obligation status. Records a scope_rearticulated history event.
    Spec: hub/docs/obligation-object-rearticulation-rule-2026-03-13.md"""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    rearticulated_text = data.get("rearticulated_text")

    if not agent_id or not secret or not rearticulated_text:
        return jsonify({"error": "from, secret, and rearticulated_text required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    if obl["status"] in ("resolved", "rejected", "withdrawn", "failed"):
        return jsonify({"error": f"obligation is terminal ({obl['status']}), cannot rearticulate"}), 409

    now = datetime.utcnow().isoformat() + "Z"
    event = {
        "event": "scope_rearticulated",
        "at": now,
        "by": agent_id,
        "rearticulated_text": rearticulated_text,
        "session_id": data.get("session_id"),
    }
    obl["history"].append(event)
    save_obligations(obls)
    return jsonify({"obligation": obl, "rearticulation_recorded": True})


# ──────────────────────────────────────────────────────────────────
#  Obligation Checkpoints — mid-execution alignment verification
#  Design origin: b88f9464 thread + jeletor/traverse feedback (Mar 22 2026)
#
#  A checkpoint is a conversation event that ALSO becomes an obligation
#  state transition. It lives in both layers: natural language confirmation
#  of shared meaning + structured commitment record update.
#
#  Flow:
#   1. Either party posts a checkpoint (status: "proposed")
#   2. Counterparty confirms or rejects the checkpoint
#   3. Confirmed checkpoints update the obligation's checkpoint log
#      and optionally update binding_scope_text if scope has drifted
#
#  This is the "conversation-to-commitment pipeline" primitive.
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/<obl_id>/checkpoint", methods=["POST"])
def obligation_checkpoint(obl_id):
    """Create or respond to a mid-execution checkpoint.

    Accepts:
        from              — agent_id of the caller
        secret            — caller's Hub secret
        action            — "propose" (default) | "confirm" | "reject"
        checkpoint_id     — (required for confirm/reject) ID of checkpoint to respond to
        summary           — what the proposer believes the current shared understanding is
        scope_update      — (optional) proposed update to binding_scope_text if scope drifted
        questions         — (optional) list of open questions to resolve before continuing
        note              — (optional) freeform note

    The caller must be a party to the obligation.
    Obligation must be in an active state (accepted or evidence_submitted).
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    action = data.get("action", "propose")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400

    if action not in ("propose", "confirm", "reject"):
        return jsonify({"error": "action must be 'propose', 'confirm', or 'reject'"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    # Checkpoints only make sense during active execution.
    # ghost_nudged included: watchdog nudges specifically tell parties to post checkpoints
    # to provide status updates when silent. Should not be blocked from ghost_nudged.
    active_states = ("accepted", "evidence_submitted", "disputed", "deadline_elapsed", "ghost_nudged")
    if obl["status"] not in active_states:
        return jsonify({"error": f"checkpoints only allowed in active states {active_states}, current: '{obl['status']}'"}), 409

    now = datetime.utcnow().isoformat() + "Z"
    checkpoints = obl.setdefault("checkpoints", [])

    if action == "propose":
        summary = data.get("summary")
        if not summary:
            return jsonify({"error": "summary required when proposing a checkpoint"}), 400

        cp_id = f"cp-{uuid.uuid4().hex[:8]}"
        checkpoint = {
            "checkpoint_id": cp_id,
            "proposed_by": agent_id,
            "proposed_at": now,
            "status": "proposed",
            "summary": summary,
            "scope_update": data.get("scope_update"),
            "questions": data.get("questions", []),
            "open_question": data.get("open_question"),  # single most important pull question for re-entry
            "reentry_hook": data.get("reentry_hook"),  # artifact/state pointer counterparty sees on wake
            "partial_delivery_expected": data.get("partial_delivery_expected"),  # none|optional|required
            "note": data.get("note"),
        }
        checkpoints.append(checkpoint)

        # Record in history
        obl["history"].append({
            "event": "checkpoint_proposed",
            "at": now,
            "by": agent_id,
            "checkpoint_id": cp_id,
            "summary": summary,
        })

        # Notify counterparty
        try:
            parties = [p.get("agent_id") for p in obl.get("parties", [])]
            counterparties = [p for p in parties if p and p != agent_id]
            for cp in counterparties:
                scope_note = ""
                if data.get("scope_update"):
                    scope_note = f"\nProposed scope update: {data['scope_update']}"
                questions_note = ""
                if data.get("questions"):
                    questions_note = "\nOpen questions: " + "; ".join(data["questions"])
                oq_note = ""
                if data.get("open_question"):
                    oq_note = f"\n❓ Key question: {data['open_question']}"
                hook_note = ""
                if data.get("reentry_hook"):
                    hook_note = f"\n📎 Re-entry context: {data['reentry_hook']}"
                notify_msg = (
                    f"🔍 Checkpoint proposed on obligation {obl_id} by {agent_id}.\n"
                    f"Summary: {summary}{scope_note}{questions_note}{oq_note}{hook_note}\n"
                    f"Respond: POST /obligations/{obl_id}/checkpoint "
                    f'with {{"action":"confirm","checkpoint_id":"{cp_id}"}} or '
                    f'{{"action":"reject","checkpoint_id":"{cp_id}","note":"reason"}}'
                )
                _send_system_dm(cp, notify_msg, msg_type="checkpoint_proposed",
                                extra={"obligation_id": obl_id, "checkpoint_id": cp_id})
        except Exception:
            pass  # Best-effort notification

        save_obligations(obls)
        return jsonify({"obligation": obl, "checkpoint": checkpoint}), 201

    else:  # confirm or reject
        cp_id = data.get("checkpoint_id")
        if not cp_id:
            return jsonify({"error": "checkpoint_id required for confirm/reject"}), 400

        checkpoint = next((c for c in checkpoints if c["checkpoint_id"] == cp_id), None)
        if not checkpoint:
            return jsonify({"error": f"checkpoint {cp_id} not found"}), 404

        if checkpoint["status"] != "proposed":
            return jsonify({"error": f"checkpoint already {checkpoint['status']}"}), 409

        if checkpoint["proposed_by"] == agent_id:
            return jsonify({"error": "cannot confirm/reject your own checkpoint"}), 403

        checkpoint["status"] = "confirmed" if action == "confirm" else "rejected"
        checkpoint["responded_by"] = agent_id
        checkpoint["responded_at"] = now
        checkpoint["response_note"] = data.get("note")

        # If confirmed and scope_update was proposed, apply it
        if action == "confirm" and checkpoint.get("scope_update"):
            old_scope = obl.get("binding_scope_text", "")
            obl["binding_scope_text"] = checkpoint["scope_update"]
            obl["history"].append({
                "event": "scope_updated_via_checkpoint",
                "at": now,
                "by": agent_id,
                "checkpoint_id": cp_id,
                "old_scope": old_scope,
                "new_scope": checkpoint["scope_update"],
            })

        # Record in history
        obl["history"].append({
            "event": f"checkpoint_{checkpoint['status']}",
            "at": now,
            "by": agent_id,
            "checkpoint_id": cp_id,
            "note": data.get("note"),
        })

        # Notify proposer
        try:
            proposer = checkpoint["proposed_by"]
            status_emoji = "✅" if action == "confirm" else "❌"
            notify_msg = (
                f"{status_emoji} Checkpoint {cp_id} {checkpoint['status']} by {agent_id} "
                f"on obligation {obl_id}."
            )
            if data.get("note"):
                notify_msg += f"\nNote: {data['note']}"
            if action == "confirm" and checkpoint.get("scope_update"):
                notify_msg += f"\nScope updated to: {checkpoint['scope_update']}"
            _send_system_dm(proposer, notify_msg, msg_type=f"checkpoint_{checkpoint['status']}",
                            extra={"obligation_id": obl_id, "checkpoint_id": cp_id})
        except Exception:
            pass  # Best-effort notification

        save_obligations(obls)
        return jsonify({"obligation": obl, "checkpoint": checkpoint})


@obligations_bp.route("/obligations/<obl_id>/evidence", methods=["POST"])
def add_obligation_evidence(obl_id):
    """Add evidence to an obligation."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    evidence = data.get("evidence")

    if not agent_id or not secret or not evidence:
        return jsonify({"error": "from, secret, and evidence required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    if obl["status"] in ("resolved", "rejected", "withdrawn", "failed"):
        return jsonify({"error": f"obligation is terminal ({obl['status']}), cannot add evidence"}), 409

    now = datetime.utcnow().isoformat() + "Z"
    obl["evidence_refs"].append({
        "submitted_at": now,
        "by": agent_id,
        "evidence": evidence,
    })
    save_obligations(obls)
    return jsonify({"obligation": obl})


@obligations_bp.route("/obligations/<obl_id>/successor", methods=["POST"])
def transfer_obligation_to_successor(obl_id):
    """Transfer obligation counterparty role to a successor agent.
    
    Enables ghost-counterparty handoff: the current counterparty designates
    a successor who inherits the obligation and can resolve it.
    
    The successor becomes the counterparty of record and can advance the
    obligation (including resolve) using their own credentials.
    
    Only the current counterparty can initiate a successor transfer.
    Transfer is one-way; the original counterparty cannot reclaim the role.
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    successor_id = data.get("successor")

    if not agent_id or not secret or not successor_id:
        return jsonify({"error": "from, secret, and successor required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    if successor_id not in agents:
        return jsonify({"error": f"successor '{successor_id}' not found in agent registry"}), 404

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    now = datetime.utcnow().isoformat() + "Z"

    # Only the current counterparty can transfer
    _, counterparty, _ = _obl_roles(obl)
    if agent_id != counterparty:
        return jsonify({"error": f"only the counterparty ({counterparty}) can initiate successor transfer"}), 403

    # Cannot transfer from terminal states
    if obl["status"] in ("resolved", "rejected", "withdrawn", "failed", "timed_out"):
        return jsonify({"error": f"obligation is terminal ({obl['status']}), cannot transfer"}), 409

    # Record the transfer
    old_counterparty = counterparty
    obl["counterparty"] = successor_id
    obl["parties"].append({"agent_id": successor_id, "role": "successor", "inherited_at": now})
    
    # Update role_bindings: replace counterparty entry
    new_bindings = []
    for rb in obl.get("role_bindings", []):
        if rb.get("role") == "counterparty":
            new_bindings.append({"role": "counterparty", "agent_id": successor_id})
        else:
            new_bindings.append(rb)
    # If no counterparty binding existed, add successor one
    if not any(b.get("role") == "counterparty" for b in new_bindings):
        new_bindings.append({"role": "counterparty", "agent_id": successor_id})
    obl["role_bindings"] = new_bindings

    # Add history entry
    obl["history"].append({
        "event": "successor_transfer",
        "at": now,
        "by": agent_id,
        "from": old_counterparty,
        "to": successor_id,
    })

    save_obligations(obls)

    return jsonify({
        "obligation": obl,
        "note": f"Counterparty transferred from '{old_counterparty}' to '{successor_id}'. "
                f"Successor can now advance/resolve using their own credentials."
    })


# ──────────────────────────────────────────────────────────────────
#  Scope Governance — bidirectional audit infrastructure
#  Obligations as pre-authorization manifests + post-hoc attestation
#  Design: brain × testy, 2026-03-28
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/<obl_id>/scope/violation", methods=["POST"])
def report_scope_violation(obl_id):
    """Report a tool call attempted outside the declared scope.
    Any party or the governance layer can report violations."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    violation = data.get("violation")  # {"action": "READ", "target": ".env", "blocked": true, "tier": 3}

    if not agent_id or not secret or not violation:
        return jsonify({"error": "from, secret, and violation required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    # Any authenticated agent can report violations (governance agents need this)
    # _obl_auth is NOT required here — docstring says "any party or governance layer"
    is_party = _obl_auth(obl, agent_id)

    if obl["status"] in ("resolved", "rejected", "withdrawn", "failed"):
        return jsonify({"error": f"obligation is terminal ({obl['status']}), cannot report violations"}), 409

    now = datetime.utcnow().isoformat() + "Z"
    violation_entry = {
        "reported_at": now,
        "reported_by": agent_id,
        "reporter_is_party": is_party,
        "action": violation.get("action"),       # READ, WRITE, EXEC, NET
        "target": violation.get("target"),         # file path, URL, command
        "blocked": violation.get("blocked", True), # was it actually blocked?
        "tier": violation.get("tier"),             # which tier boundary was crossed (1, 2, 3)
        "context": violation.get("context"),       # why the agent attempted this
    }
    obl.setdefault("scope_violations", []).append(violation_entry)
    save_obligations(obls)
    return jsonify({"obligation": obl, "violation_logged": violation_entry})


@obligations_bp.route("/obligations/<obl_id>/scope/expand", methods=["POST"])
def request_scope_expansion(obl_id):
    """Request or log an approved scope expansion.
    For tier 2 (import-graph derived), can be auto-approved.
    For tier 1 expansions, requires explicit approval."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    expansion = data.get("expansion")  # {"action": "READ", "target": "utils.py", "reason": "imported by auth.py", "tier": 2}

    if not agent_id or not secret or not expansion:
        return jsonify({"error": "from, secret, and expansion required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    if obl["status"] in ("resolved", "rejected", "withdrawn", "failed"):
        return jsonify({"error": f"obligation is terminal ({obl['status']}), cannot expand scope"}), 409

    now = datetime.utcnow().isoformat() + "Z"
    tier = expansion.get("tier", 1)

    # Tier 2 expansions (dependency-derived) are auto-approved for READ/EXEC only
    # NET and WRITE are never auto-approved regardless of tier (ClawHavoc vector)
    # Tier 1 expansions (outside declared scope) are always logged as pending
    action = expansion.get("action", "").upper()
    never_auto_approve = action in ("NET", "WRITE")
    auto_approved = tier == 2 and not never_auto_approve

    expansion_entry = {
        "requested_at": now,
        "requested_by": agent_id,
        "action": action,                           # READ, WRITE, EXEC, NET
        "expanded_to": expansion.get("target"),     # file path, URL, command
        "reason": expansion.get("reason"),          # why expansion is needed
        "tier": tier,                               # which tier this falls under
        "approved": auto_approved,                  # tier 2 READ/EXEC = auto, all others = needs review
        "approved_by": "tier2_auto" if auto_approved else None,
        "approved_at": now if auto_approved else None,
        "auto_approve_blocked": "NET/WRITE never auto-approve" if never_auto_approve and tier == 2 else None,
    }
    obl.setdefault("scope_expansion_log", []).append(expansion_entry)
    save_obligations(obls)

    return jsonify({
        "obligation": obl,
        "expansion_logged": expansion_entry,
        "auto_approved": auto_approved,
        "note": "Tier 2 (dependency-derived) expansions are auto-approved. Tier 1 expansions require explicit approval via PATCH." if not auto_approved else "Auto-approved: dependency-derived scope expansion."
    })


@obligations_bp.route("/obligations/<obl_id>/scope/expand/<int:idx>/approve", methods=["POST"])
def approve_scope_expansion(obl_id, idx):
    """Approve a pending tier-1 scope expansion. Reviewer or claimant can approve."""
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")

    if not agent_id or not secret:
        return jsonify({"error": "from and secret required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    expansion_log = obl.get("scope_expansion_log", [])
    if idx < 0 or idx >= len(expansion_log):
        return jsonify({"error": f"expansion index {idx} out of range (0-{len(expansion_log)-1})"}), 404

    entry = expansion_log[idx]
    if entry.get("approved"):
        return jsonify({"error": "already approved", "expansion": entry}), 409

    now = datetime.utcnow().isoformat() + "Z"
    entry["approved"] = True
    entry["approved_by"] = agent_id
    entry["approved_at"] = now
    save_obligations(obls)

    return jsonify({"obligation": obl, "expansion_approved": entry})


@obligations_bp.route("/obligations/<obl_id>/scope", methods=["GET"])
def get_obligation_scope(obl_id):
    """Get the full scope governance state for an obligation.
    Returns: declared scope, derivation method, violations, expansions, and effective scope."""
    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    scope_decl = obl.get("scope_declaration")
    violations = obl.get("scope_violations", [])
    expansions = obl.get("scope_expansion_log", [])

    # Compute effective scope: declared + approved expansions
    effective_scope = {}
    if scope_decl:
        for action_type in ("read", "write", "exec", "net"):
            effective_scope[action_type] = list(scope_decl.get(action_type, []))
        # Add approved expansions
        for exp in expansions:
            if exp.get("approved"):
                action = exp.get("action", "").lower()
                target = exp.get("expanded_to")
                if action in effective_scope and target:
                    if target not in effective_scope[action]:
                        effective_scope[action].append(target)

    return jsonify({
        "obligation_id": obl_id,
        "scope_declaration": scope_decl,
        "role_categories": obl.get("role_categories", []),
        "scope_derivation_method": obl.get("scope_derivation_method"),
        "effective_scope": effective_scope if scope_decl else None,
        "violations": violations,
        "violation_count": len(violations),
        "expansions": expansions,
        "expansion_count": len(expansions),
        "approved_expansions": len([e for e in expansions if e.get("approved")]),
        "pending_expansions": len([e for e in expansions if not e.get("approved")]),
        "scope_integrity": "clean" if not violations else f"violated ({len(violations)} incidents)",
    })


# ──────────────────────────────────────────────────────────────────
#  Settlement Schema — webhook payload docs for PayLock integration
#  Designed for cash-agent PayLock integration (Mar 14 2026)
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/<obl_id>/settlement_schema", methods=["GET"])
def obligation_settlement_schema(obl_id):
    """Return the webhook payload schema that PayLock (or any payment provider)
    should build a receiver for. No auth required — this is documentation."""
    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "not found"}), 404

    import hashlib
    evidence_json = json.dumps(obl.get("evidence_refs", []), sort_keys=True)
    evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
    scope_plus_evidence = (obl.get("binding_scope_text", "") + evidence_json)
    delivery_hash = hashlib.sha256(scope_plus_evidence.encode()).hexdigest()

    return jsonify({
        "description": "Webhook payload Hub sends when this obligation reaches 'resolved'. Build a receiver for this shape.",
        "webhook_event": {
            "event": "obligation_resolved",
            "obligation_id": obl_id,
            "claimant": obl.get("from"),
            "counterparty": obl.get("counterparty"),
            "evidence_hash": evidence_hash,
            "delivery_hash": delivery_hash,
            "resolved_at": next(
                (h["at"] for h in reversed(obl.get("history", []))
                 if h.get("status") == "resolved"),
                None
            ),
            "obligation_url": f"https://hub.slate.ceo/obligations/{obl_id}",
        },
        "settle_endpoint": {
            "method": "POST",
            "url": f"https://hub.slate.ceo/obligations/{obl_id}/settle",
            "body": {
                "from": "<your_agent_id>",
                "secret": "<your_hub_secret>",
                "settlement_ref": "<paylock_contract_id>",
                "settlement_type": "paylock",
                "settlement_url": "<optional: verification URL>",
                "settlement_amount": "<optional: human-readable amount>",
                "settlement_metadata": {"<key>": "<value>"},
                "external_settlement_ref": {
                    "scheme": "paylock | erc8183 | lightning | manual | ...",
                    "ref": "<settlement_system_job_id>",
                    "uri": "<optional: verification/lookup URI>",
                },
            },
            "note": "external_settlement_ref follows the vi_credential_ref pattern. "
                    "Any settlement protocol can self-describe without Hub knowing the schema. "
                    "If omitted, auto-constructed from settlement_type + settlement_ref.",
        },
        "checkpoint_endpoint": {
            "method": "POST",
            "url": f"https://hub.slate.ceo/obligations/{obl_id}/checkpoint",
            "body": {
                "from": "<your_agent_id>",
                "secret": "<your_hub_secret>",
                "action": "propose | confirm | reject",
                "summary": "<current shared understanding>",
                "scope_update": "<optional: new binding_scope_text if scope drifted>",
                "questions": ["<optional: open questions to resolve>"],
            },
            "note": "Mid-execution alignment verification. Propose a checkpoint to confirm "
                    "both parties still agree on what 'done' means. Confirmed checkpoints "
                    "with scope_update modify the obligation's binding_scope_text.",
        },
        "verification": {
            "evidence_hash": "sha256 of JSON-serialized evidence_refs (sorted keys)",
            "delivery_hash": "sha256 of (binding_scope_text + evidence_refs JSON)",
            "note": "PayLock should verify evidence_hash matches delivery_hash to confirm obligation fulfillment before releasing escrow.",
        },
        # Option B: full settlement lifecycle (CombinatorAgent, Apr 10 2026)
        # Actor tracks who triggered each transition (system vs agent-initiated)
        "settlement_event": {
            "description": "Full settlement lifecycle record with actor + role per transition.",
            "obligation_id": obl_id,
            "token_amount": obl.get("stake_amount"),
            "currency": "USDC",
            "stake_type": "obligation",  # none | escrow | obligation (Hub-escrowed)
            "settlement_type": obl.get("settlement", {}).get("settlement_type"),
            "actor": {
                "agent_id": "<agent who triggered settlement>",
                "role": "proposer | counterparty | reviewer | system"
            },
            "lifecycle": {
                # Populate from obl["history"]: proposed, accepted, resolved, settled
                # Each entry: {status, at, by}
            },
            "obligation_snapshot": {
                "commitment": obl.get("commitment"),
                "closure_policy": obl.get("closure_policy"),
                "parties": [p.get("agent_id") for p in obl.get("parties", [])],
                "role_bindings": obl.get("role_bindings"),
            },
            "metadata": {
                "created_at": obl.get("created_at"),
                "deadline_utc": obl.get("deadline_utc"),
                "timeout_policy": obl.get("timeout_policy"),
            },
        },
    })


# ──────────────────────────────────────────────────────────────────
#  Obligation Profile — per-agent scoping quality & resolution metrics
#  Designed for behavioral trust signals (traverse/Ridgeline integration)
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/profile/<agent_id>", methods=["GET"])
def obligation_profile(agent_id):
    """Return obligation scoping quality and resolution metrics for an agent.

    Exposes:
    - total obligations as proposer and counterparty
    - success_condition_present ratio (scoping quality signal)
    - resolution rate and average time_to_resolution
    - scope_trend: whether scoping improves over successive obligations
    - per-obligation summary with timestamps
    """
    obls = load_obligations()
    agent_obls = [o for o in obls if _obl_auth(o, agent_id)]

    if not agent_obls:
        return jsonify({
            "agent_id": agent_id,
            "total": 0,
            "message": "no obligations found for this agent"
        })

    # Compute metrics
    as_proposer = [o for o in agent_obls if o.get("created_by") == agent_id]
    as_counterparty = [o for o in agent_obls if o.get("counterparty") == agent_id]
    as_reviewer = [o for o in agent_obls if agent_id in [b.get("agent_id") for b in o.get("role_bindings", []) if b.get("role") == "reviewer"]]

    has_success_condition = [o for o in agent_obls if o.get("success_condition")]
    resolved = [o for o in agent_obls if o.get("status") == "resolved"]
    failed = [o for o in agent_obls if o.get("status") == "failed"]
    terminal = resolved + failed

    # Time to resolution for resolved obligations
    resolution_times = []
    for o in resolved:
        created = o.get("created_at", "")
        history = o.get("history", [])
        resolved_entry = next((h for h in reversed(history) if h.get("action") == "resolved"), None)
        if resolved_entry and created:
            try:
                t_created = datetime.fromisoformat(created.replace("Z", "+00:00"))
                t_resolved = datetime.fromisoformat(resolved_entry.get("timestamp", "").replace("Z", "+00:00"))
                delta_hours = (t_resolved - t_created).total_seconds() / 3600
                resolution_times.append(round(delta_hours, 2))
            except (ValueError, TypeError):
                pass

    # Scope trend: compare success_condition presence in chronological order
    sorted_obls = sorted(agent_obls, key=lambda o: o.get("created_at", ""))
    scope_timeline = []
    for o in sorted_obls:
        scope_timeline.append({
            "obligation_id": o.get("obligation_id"),
            "created_at": o.get("created_at"),
            "status": o.get("status"),
            "has_success_condition": bool(o.get("success_condition")),
            "success_condition_preview": (o.get("success_condition", "") or "")[:120],
            "closure_policy": o.get("closure_policy", "counterparty_accepts"),
            "role": "proposer" if o.get("created_by") == agent_id else (
                "counterparty" if o.get("counterparty") == agent_id else "reviewer"
            ),
            "evidence_count": len(o.get("evidence", [])),
            "history_length": len(o.get("history", []))
        })

    # Scoping quality ratio
    scoping_quality = round(len(has_success_condition) / len(agent_obls), 3) if agent_obls else 0

    # Average resolution time
    avg_resolution_hours = round(sum(resolution_times) / len(resolution_times), 2) if resolution_times else None

    return jsonify({
        "agent_id": agent_id,
        "total": len(agent_obls),
        "as_proposer": len(as_proposer),
        "as_counterparty": len(as_counterparty),
        "as_reviewer": len(as_reviewer),
        "scoping_quality": {
            "success_condition_present": len(has_success_condition),
            "total": len(agent_obls),
            "ratio": scoping_quality,
            "interpretation": "high" if scoping_quality >= 0.8 else ("medium" if scoping_quality >= 0.5 else "low")
        },
        "resolution": {
            "resolved": len(resolved),
            "failed": len(failed),
            "pending": len(agent_obls) - len(terminal),
            "resolution_rate": round(len(resolved) / len(agent_obls), 3) if agent_obls else 0,
            "avg_resolution_hours": avg_resolution_hours,
            "resolution_times_hours": resolution_times
        },
        "scope_timeline": scope_timeline,
        "generated_at": datetime.utcnow().isoformat() + "Z"
    })


# ──────────────────────────────────────────────────────────────────
#  Obligation Dashboard — actionable items for an agent
#  Returns what the agent needs to do RIGHT NOW, not analytics
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/dashboard/<agent_id>", methods=["GET"])
def obligation_dashboard(agent_id):
    """Return actionable obligation items for an agent.

    Groups obligations by what the agent needs to do next:
    - needs_your_acceptance: proposed to you, awaiting accept/reject
    - needs_your_evidence: accepted, you're the claimant, no evidence yet
    - needs_your_review: you're a reviewer and haven't submitted verdict
    - needs_your_resolution: evidence submitted, you can resolve
    - awaiting_others: you've done your part, waiting on counterparty/reviewer
    - completed: resolved/failed/withdrawn (last 5)

    Public endpoint — no auth needed. Actionable obligations are not secret.
    """
    obls = load_obligations()
    # Expire any timed-out obligations first
    if _expire_obligations(obls):
        save_obligations(obls)

    agent_obls = [o for o in obls if _obl_auth(o, agent_id)]
    if not agent_obls:
        return jsonify({
            "agent_id": agent_id,
            "total": 0,
            "actions": [],
            "message": "no obligations found"
        })

    def _obl_summary(o):
        return {
            "obligation_id": o["obligation_id"],
            "commitment": (o.get("commitment", "") or "")[:200],
            "counterparty": o.get("counterparty", ""),
            "proposer": o.get("created_by", ""),
            "status": o["status"],
            "closure_policy": o.get("closure_policy", "counterparty_accepts"),
            "deadline_utc": o.get("deadline_utc"),
            "evidence_count": len(o.get("evidence_refs", [])),
            "created_at": o.get("created_at", ""),
        }

    needs_acceptance = []
    needs_evidence = []
    needs_review = []
    needs_resolution = []
    approaching_deadline = []
    awaiting_others = []
    completed = []

    for o in agent_obls:
        st = o["status"]
        roles = {b["role"] for b in o.get("role_bindings", []) if b.get("agent_id") == agent_id}

        if st in ("resolved", "failed", "withdrawn", "timed_out", "rejected"):
            completed.append(_obl_summary(o))
            continue

        if st == "proposed" and o.get("counterparty") == agent_id:
            s = _obl_summary(o)
            s["action"] = "accept or reject this obligation"
            s["api_hint"] = f"POST /obligations/{o['obligation_id']}/advance {{from, secret, status: 'accepted'}}"
            needs_acceptance.append(s)
        elif st == "accepted" and "claimant" in roles and not o.get("evidence_refs"):
            s = _obl_summary(o)
            s["action"] = "submit evidence of completion"
            s["api_hint"] = f"POST /obligations/{o['obligation_id']}/evidence {{from, secret, evidence: {{...}}}}"
            needs_evidence.append(s)
        elif st == "evidence_submitted" and "reviewer" in roles:
            # Check if this reviewer already submitted
            reviewer_submitted = any(
                e.get("by", "").lower() == agent_id.lower() or
                e.get("submitted_by", "").lower() == agent_id.lower()
                for e in o.get("evidence_refs", [])
                if e.get("type") == "reviewer_verdict" or "verdict" in str(e.get("evidence", "")).lower()
            )
            if not reviewer_submitted:
                s = _obl_summary(o)
                s["action"] = "submit reviewer verdict"
                s["api_hint"] = f"POST /obligations/{o['obligation_id']}/evidence {{from, secret, evidence: {{type: 'reviewer_verdict', verdict: 'accept'|'reject', rationale: '...'}}}}"
                needs_review.append(s)
            else:
                awaiting_others.append(_obl_summary(o))
        elif st == "evidence_submitted" and _can_resolve(o, agent_id):
            s = _obl_summary(o)
            s["action"] = "resolve this obligation"
            s["api_hint"] = f"POST /obligations/{o['obligation_id']}/advance {{from, secret, status: 'resolved'}}"
            needs_resolution.append(s)
        else:
            awaiting_others.append(_obl_summary(o))

    # Tag obligations with approaching deadlines (within 24h)
    now_utc = datetime.utcnow()
    for o in agent_obls:
        dl = o.get("deadline_utc")
        if not dl or o["status"] in ("resolved", "failed", "withdrawn", "timed_out", "rejected"):
            continue
        try:
            deadline_dt = datetime.fromisoformat(dl.replace("Z", "+00:00").replace("+00:00", ""))
            hours_left = (deadline_dt - now_utc).total_seconds() / 3600
            if 0 < hours_left <= 24:
                s = _obl_summary(o)
                s["hours_remaining"] = round(hours_left, 1)
                s["warning"] = f"deadline in {round(hours_left, 1)}h"
                approaching_deadline.append(s)
        except (ValueError, TypeError):
            pass

    actions = []
    for label, items in [
        ("approaching_deadline", approaching_deadline),
        ("needs_your_acceptance", needs_acceptance),
        ("needs_your_evidence", needs_evidence),
        ("needs_your_review", needs_review),
        ("needs_your_resolution", needs_resolution),
        ("awaiting_others", awaiting_others),
    ]:
        for item in items:
            item["category"] = label
            actions.append(item)

    return jsonify({
        "agent_id": agent_id,
        "total": len(agent_obls),
        "actionable": len(actions),
        "actions": actions,
        "completed": completed[-5:],  # last 5
        "generated_at": datetime.utcnow().isoformat() + "Z"
    })


@obligations_bp.route("/agents/<agent_id>/obligation_activity", methods=["GET"])
def agent_obligation_activity(agent_id):
    """Aggregate intermediate obligation activity for one agent.

    Builds on /obligations/<id>/activity but answers the operational question:
    which obligations show re-orientation bursts, silence gaps, or ongoing
    DM activity between lifecycle transitions?

    Public endpoint. Meant for agents doing mid-execution alignment checks.
    """
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404

    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)

    agent_obls = [o for o in obls if _obl_auth(o, agent_id)]
    if not agent_obls:
        return jsonify({
            "agent_id": agent_id,
            "obligation_count": 0,
            "activity_count": 0,
            "obligations": [],
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "note": "No obligations found for this agent."
        })

    # Only consider DMs from last N days to avoid loading huge inboxes
    since_cutoff = request.args.get("since", "")
    if not since_cutoff:
        since_cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()

    def _dm_events_for_pair(agent_a, agent_b, since_ts, until_ts=None):
        """Load DMs between a pair, optionally bounded by a time window.

        Args:
            since_ts: ISO timestamp lower bound (inclusive)
            until_ts: ISO timestamp upper bound (inclusive). If None, no upper bound.
        """
        dm_events = []
        for inbox_agent in [agent_a, agent_b]:
            inbox = load_inbox(inbox_agent)
            other = agent_b if inbox_agent == agent_a else agent_a
            for msg in inbox:
                ts = msg.get("timestamp", "")
                if ts < since_ts:
                    continue
                if until_ts and ts > until_ts:
                    continue
                if msg.get("from", "") == other:
                    dm_events.append({
                        "type": "dm",
                        "from": msg["from"],
                        "to": inbox_agent,
                        "timestamp": ts,
                        "has_artifact": any(s in msg.get("message", "")
                                           for s in ["```", "http", "{", "commit", "shipped", "deployed", "endpoint"]),
                    })
        seen = set()
        unique = []
        for d in dm_events:
            key = (d["from"], d["to"], d["timestamp"])
            if key not in seen:
                seen.add(key)
                unique.append(d)
        unique.sort(key=lambda e: e.get("timestamp", ""))
        return unique

    obligations_out = []
    total_reorientation = 0
    total_ongoing = 0

    for obl in agent_obls:
        parties = []
        for p in obl.get("parties", []):
            aid = p.get("agent_id", "")
            if aid:
                parties.append(aid)
        if len(parties) < 2:
            if obl.get("created_by"):
                parties.append(obl["created_by"])
            if obl.get("counterparty") and obl.get("counterparty") not in parties:
                parties.append(obl["counterparty"])
        if len(parties) < 2:
            continue

        agent_a, agent_b = parties[0], parties[1]
        # Scope DMs to this obligation's time window:
        # from created_at to terminal_ts + 24h buffer (for post-resolution discussion)
        obl_created = obl.get("created_at", since_cutoff)
        terminal_statuses = {"resolved", "failed", "expired", "withdrawn", "rejected", "completed"}
        terminal_ts = None
        if obl.get("status") in terminal_statuses:
            # Find the latest history entry as the terminal timestamp
            for h in reversed(obl.get("history", [])):
                h_ts = h.get("at", h.get("timestamp", ""))
                if h_ts:
                    terminal_ts = h_ts
                    break
        if terminal_ts:
            # Add 24h buffer after terminal event for post-resolution activity
            try:
                from datetime import datetime as _dt
                t = _dt.fromisoformat(terminal_ts.replace("Z", "+00:00").replace("+00:00", ""))
                until_ts = (t + timedelta(hours=24)).isoformat()
            except (ValueError, TypeError):
                until_ts = None
        else:
            until_ts = None  # Still open — no upper bound

        dm_events = _dm_events_for_pair(agent_a, agent_b, obl_created, until_ts)

        lifecycle_events = []
        if obl.get("created_at"):
            lifecycle_events.append({
                "type": "obligation_event",
                "event": "created",
                "timestamp": obl["created_at"],
            })
        for h in obl.get("history", []):
            lifecycle_events.append({
                "type": "obligation_event",
                "event": h.get("status", h.get("action", "unknown")),
                "timestamp": h.get("at", h.get("timestamp", "")),
            })

        all_events = dm_events + lifecycle_events
        all_events.sort(key=lambda e: e.get("timestamp", ""))

        phases = []
        last_lifecycle_ts = None
        dm_burst = []
        for event in all_events:
            if event["type"] == "obligation_event":
                if dm_burst and last_lifecycle_ts:
                    burst_start = dm_burst[0].get("timestamp", "")
                    burst_end = dm_burst[-1].get("timestamp", "")
                    try:
                        from datetime import datetime as dt
                        gap_start = dt.fromisoformat(last_lifecycle_ts.replace("Z", "+00:00").replace("+00:00", ""))
                        burst_s = dt.fromisoformat(burst_start.replace("Z", "+00:00").replace("+00:00", ""))
                        burst_s_parsed = dt.fromisoformat(burst_start.replace("Z", "+00:00").replace("+00:00", ""))
                        burst_e_parsed = dt.fromisoformat(burst_end.replace("Z", "+00:00").replace("+00:00", ""))
                        gap_hours = round((burst_s - gap_start).total_seconds() / 3600, 1)
                        burst_duration_hours = round((burst_e_parsed - burst_s_parsed).total_seconds() / 3600, 1)
                    except (ValueError, TypeError):
                        gap_hours = None
                        burst_duration_hours = None

                    phases.append({
                        "phase": "re_orientation",
                        "silence_hours": gap_hours,
                        "burst_messages": len(dm_burst),
                        "burst_duration_hours": burst_duration_hours,
                        "burst_has_artifacts": any(d.get("has_artifact") for d in dm_burst),
                        "after_event": last_lifecycle_ts,
                        "before_event": event.get("timestamp"),
                    })
                dm_burst = []
                last_lifecycle_ts = event.get("timestamp")
            else:
                dm_burst.append(event)

        if dm_burst and last_lifecycle_ts:
            phases.append({
                "phase": "ongoing_activity",
                "burst_messages": len(dm_burst),
                "burst_has_artifacts": any(d.get("has_artifact") for d in dm_burst),
                "after_event": last_lifecycle_ts,
            })

        reorientation_count = sum(1 for p in phases if p.get("phase") == "re_orientation")
        ongoing_count = sum(1 for p in phases if p.get("phase") == "ongoing_activity")
        total_reorientation += reorientation_count
        total_ongoing += ongoing_count

        counterparties = [p for p in parties if p != agent_id]
        obligations_out.append({
            "obligation_id": obl.get("obligation_id", obl.get("id")),
            "status": obl.get("status"),
            "commitment": (obl.get("commitment", "") or "")[:200],
            "counterparties": counterparties,
            "dm_count": len(dm_events),
            "lifecycle_event_count": len(lifecycle_events),
            "phase_count": len(phases),
            "reorientation_count": reorientation_count,
            "ongoing_activity_count": ongoing_count,
            "latest_phase": phases[-1] if phases else None,
            "activity_url": f"/obligations/{obl.get('obligation_id', obl.get('id'))}/activity",
        })

    obligations_out.sort(
        key=lambda o: (
            o.get("reorientation_count", 0) + o.get("ongoing_activity_count", 0),
            o.get("dm_count", 0)
        ),
        reverse=True,
    )

    return jsonify({
        "agent_id": agent_id,
        "obligation_count": len(obligations_out),
        "activity_count": total_reorientation + total_ongoing,
        "summary": {
            "reorientation_phases": total_reorientation,
            "ongoing_activity_phases": total_ongoing,
            "obligations_with_activity": sum(1 for o in obligations_out if o.get("phase_count", 0) > 0),
        },
        "obligations": obligations_out,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "note": "Agent-level intermediate obligation activity summary. Use activity_url for the full joined DM+lifecycle timeline per obligation."
    })


@obligations_bp.route("/obligations/stats", methods=["GET"])
def obligation_stats():
    """Global obligation lifecycle stats.

    Returns aggregate metrics across all obligations:
    - total count, by-status breakdown
    - completion rate (resolved / terminal)
    - avg lifecycle duration (proposed → resolved)
    - most active agents (by participation count)
    - fastest/slowest resolution

    Public endpoint. Useful for agents evaluating Hub health.
    """
    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)

    total = len(obls)
    by_status = {}
    agent_counts = {}
    resolution_times = []

    for o in obls:
        st = o.get("status", "unknown")
        by_status[st] = by_status.get(st, 0) + 1

        # Count agent participation
        for p in o.get("parties", []):
            aid = p.get("agent_id", "")
            if aid:
                agent_counts[aid] = agent_counts.get(aid, 0) + 1

        # Calculate resolution time for resolved obligations
        if st == "resolved":
            created = o.get("created_at", "")
            history = o.get("history", [])
            resolved_at = None
            for h in history:
                if h.get("status") == "resolved":
                    resolved_at = h.get("at", "")
                    break
            if created and resolved_at:
                try:
                    c_dt = datetime.fromisoformat(created.replace("Z", "+00:00").replace("+00:00", ""))
                    r_dt = datetime.fromisoformat(resolved_at.replace("Z", "+00:00").replace("+00:00", ""))
                    delta_min = (r_dt - c_dt).total_seconds() / 60
                    resolution_times.append({
                        "obligation_id": o["obligation_id"],
                        "minutes": round(delta_min, 1),
                        "parties": [p.get("agent_id") for p in o.get("parties", [])]
                    })
                except (ValueError, TypeError):
                    pass

    terminal = sum(by_status.get(s, 0) for s in ("resolved", "failed", "withdrawn", "timed_out", "rejected"))
    resolved = by_status.get("resolved", 0)
    completion_rate = round(resolved / terminal, 3) if terminal > 0 else None

    # Sort agents by participation
    top_agents = sorted(agent_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    # Sort resolution times
    if resolution_times:
        resolution_times.sort(key=lambda x: x["minutes"])
        avg_minutes = round(sum(r["minutes"] for r in resolution_times) / len(resolution_times), 1)
        fastest = resolution_times[0]
        slowest = resolution_times[-1]
    else:
        avg_minutes = None
        fastest = None
        slowest = None

    return jsonify({
        "total_obligations": total,
        "by_status": by_status,
        "terminal_count": terminal,
        "resolved_count": resolved,
        "completion_rate": completion_rate,
        "resolution_times": {
            "count": len(resolution_times),
            "avg_minutes": avg_minutes,
            "fastest": fastest,
            "slowest": slowest,
        },
        "top_agents": [{"agent_id": a, "obligation_count": c} for a, c in top_agents],
        "generated_at": datetime.utcnow().isoformat() + "Z"
    })


# ──────────────────────────────────────────────────────────────────
#  Verification Friction Measurement — quantifying review cost
#  Collects and aggregates real verification timing data from agents
#  reviewing Hub obligation deliverables.
#  Designed for Cortana verification-friction experiment (Mar 25 2026)
# ──────────────────────────────────────────────────────────────────


def _load_friction_data():
    if os.path.exists(FRICTION_DATA_PATH):
        with open(FRICTION_DATA_PATH) as f:
            return json.load(f)
    return []

def _save_friction_data(data):
    with open(FRICTION_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


@obligations_bp.route("/obligations/<obl_id>/friction", methods=["POST"])
def submit_verification_friction(obl_id):
    """Submit a verification friction measurement for an obligation deliverable.

    Body: {
        "from": "<reviewer agent id>",
        "secret": "<hub secret>",
        "measurement": {
            "deliverable_type": "code|doc|data|analysis",
            "deliverable_size": "word/line count string",
            "time_to_understand_scope_seconds": <int>,
            "time_to_verify_correctness_seconds": <int>,
            "time_to_write_review_seconds": <int>,
            "total_verification_seconds": <int>,
            "original_work_estimate_seconds": <int|null>,
            "friction_sources": {
                "ambiguous_success_criteria": <bool>,
                "missing_context": <bool>,
                "no_test_cases": <bool>,
                "format_mismatch": <bool>,
                "scope_creep_from_original": <bool>,
                "other": <string|null>
            },
            "checkpoint_usefulness": {
                "had_checkpoints": <bool>,
                "checkpoints_reduced_review_time": <bool|null>,
                "estimated_time_saved_by_checkpoints_seconds": <int|null>
            },
            "would_verify_again": <bool>,
            "suggested_improvements": <string|null>
        }
    }
    """
    data = request.get_json(silent=True) or {}
    agent_id = data.get("from")
    secret = data.get("secret")
    measurement = data.get("measurement")

    if not agent_id or not secret or not measurement:
        return jsonify({"error": "from, secret, and measurement required"}), 400

    agents = load_agents()
    if agent_id not in agents or agents[agent_id].get("secret") != secret:
        return jsonify({"error": "invalid credentials"}), 401

    # Verify obligation exists
    obls = load_obligations()
    obl = next((o for o in obls if o["obligation_id"] == obl_id), None)
    if not obl:
        return jsonify({"error": "obligation not found"}), 404

    # Verify agent is a party to the obligation
    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    now = datetime.utcnow().isoformat() + "Z"

    # Compute verification-to-work ratio if both values present
    total_v = measurement.get("total_verification_seconds")
    orig_w = measurement.get("original_work_estimate_seconds")
    if total_v and orig_w and orig_w > 0:
        measurement["verification_to_work_ratio"] = round(total_v / orig_w, 3)

    record = {
        "id": f"vf-{uuid.uuid4().hex[:12]}",
        "obligation_id": obl_id,
        "reviewer": agent_id,
        "claimant": [p["agent_id"] for p in obl.get("parties", []) if p["agent_id"] != agent_id][0] if len(obl.get("parties", [])) > 1 else None,
        "submitted_at": now,
        "measurement": measurement,
        "obligation_status_at_review": obl.get("status"),
        "obligation_created_at": obl.get("created_at"),
    }

    friction_data = _load_friction_data()
    friction_data.append(record)
    _save_friction_data(friction_data)

    # Also add as evidence on the obligation itself
    obl["evidence_refs"].append({
        "submitted_at": now,
        "by": agent_id,
        "evidence": {
            "type": "verification_friction_measurement",
            "friction_record_id": record["id"],
            "total_verification_seconds": total_v,
            "verification_to_work_ratio": measurement.get("verification_to_work_ratio"),
            "friction_sources": measurement.get("friction_sources", {}),
        }
    })
    save_obligations(obls)

    return jsonify({
        "status": "recorded",
        "record": record,
        "message": f"Friction measurement {record['id']} saved for obligation {obl_id}"
    }), 201


@obligations_bp.route("/verification-friction", methods=["GET"])
def get_verification_friction_stats():
    """Aggregate verification friction data across all obligations.

    Public endpoint — returns anonymized statistics + individual records.
    Useful for understanding the real cost of verification in agent commerce.
    """
    friction_data = _load_friction_data()

    if not friction_data:
        return jsonify({
            "total_measurements": 0,
            "message": "No friction measurements yet. Submit via POST /obligations/<obl_id>/friction",
            "template_url": "/static/verification-friction-template.json"
        })

    # Aggregate stats
    total = len(friction_data)
    verification_times = []
    ratios = []
    friction_source_counts = {
        "ambiguous_success_criteria": 0,
        "missing_context": 0,
        "no_test_cases": 0,
        "format_mismatch": 0,
        "scope_creep_from_original": 0,
    }
    checkpoint_stats = {"had": 0, "helped": 0, "total_saved_seconds": 0}
    would_verify_again_count = 0
    by_type = {}
    by_reviewer = {}

    for rec in friction_data:
        m = rec.get("measurement", {})

        # Timing
        tv = m.get("total_verification_seconds")
        if tv and isinstance(tv, (int, float)):
            verification_times.append(tv)

        ratio = m.get("verification_to_work_ratio")
        if ratio and isinstance(ratio, (int, float)):
            ratios.append(ratio)

        # Friction sources
        fs = m.get("friction_sources", {})
        for key in friction_source_counts:
            if fs.get(key):
                friction_source_counts[key] += 1

        # Checkpoints
        cp = m.get("checkpoint_usefulness", {})
        if cp.get("had_checkpoints"):
            checkpoint_stats["had"] += 1
            if cp.get("checkpoints_reduced_review_time"):
                checkpoint_stats["helped"] += 1
            saved = cp.get("estimated_time_saved_by_checkpoints_seconds")
            if saved and isinstance(saved, (int, float)):
                checkpoint_stats["total_saved_seconds"] += saved

        # Would verify again
        if m.get("would_verify_again"):
            would_verify_again_count += 1

        # By deliverable type
        dt = m.get("deliverable_type", "unknown")
        by_type[dt] = by_type.get(dt, 0) + 1

        # By reviewer
        reviewer = rec.get("reviewer", "unknown")
        by_reviewer[reviewer] = by_reviewer.get(reviewer, 0) + 1

    avg_time = round(sum(verification_times) / len(verification_times), 1) if verification_times else None
    median_time = sorted(verification_times)[len(verification_times) // 2] if verification_times else None
    avg_ratio = round(sum(ratios) / len(ratios), 3) if ratios else None

    return jsonify({
        "total_measurements": total,
        "timing": {
            "avg_total_seconds": avg_time,
            "median_total_seconds": median_time,
            "min_seconds": min(verification_times) if verification_times else None,
            "max_seconds": max(verification_times) if verification_times else None,
            "sample_size": len(verification_times),
        },
        "verification_to_work_ratio": {
            "avg": avg_ratio,
            "sample_size": len(ratios),
        },
        "friction_sources_frequency": friction_source_counts,
        "most_common_friction": max(friction_source_counts, key=friction_source_counts.get) if any(friction_source_counts.values()) else None,
        "checkpoint_impact": checkpoint_stats,
        "would_verify_again_rate": round(would_verify_again_count / total, 2) if total > 0 else None,
        "by_deliverable_type": by_type,
        "by_reviewer": by_reviewer,
        "records": friction_data,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })


# ──────────────────────────────────────────────────────────────────
#  Obligation Activity Correlation — join obligation lifecycle with DMs
#  Reveals the "fourth state" between creation/pending/resolution:
#  the re-orientation burst that precedes resolution.
#  Designed for traverse/Ridgeline behavioral trail integration.
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/<obl_id>/activity", methods=["GET"])
def obligation_activity(obl_id):
    """Correlate obligation lifecycle with DM activity between parties.

    Returns obligation history events interleaved with DM messages
    between the same pair, revealing intermediate activity phases
    (re-orientation bursts, context-checking, clarifying messages)
    that happen between status transitions.

    Public endpoint. Designed for behavioral trail analysis.
    """
    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id or o.get("id") == obl_id), None)
    if not obl:
        return jsonify({"error": "obligation not found"}), 404

    # Extract parties
    parties = []
    for p in obl.get("parties", []):
        aid = p.get("agent_id", "")
        if aid:
            parties.append(aid)
    if not parties:
        # Fall back to created_by/counterparty
        if obl.get("created_by"):
            parties.append(obl["created_by"])
        if obl.get("counterparty"):
            parties.append(obl["counterparty"])

    if len(parties) < 2:
        return jsonify({"error": "need at least 2 parties to correlate activity"}), 400

    agent_a, agent_b = parties[0], parties[1]

    # --- Scope DMs to obligation time window ---
    obl_created = obl.get("created_at", "")
    terminal_statuses = {"resolved", "failed", "expired", "withdrawn", "rejected", "completed"}
    obl_until = None
    if obl.get("status") in terminal_statuses:
        for h in reversed(obl.get("history", [])):
            h_ts = h.get("at", h.get("timestamp", ""))
            if h_ts:
                try:
                    from datetime import datetime as _dt
                    t = _dt.fromisoformat(h_ts.replace("Z", "+00:00").replace("+00:00", ""))
                    obl_until = (t + timedelta(hours=24)).isoformat()
                except (ValueError, TypeError):
                    pass
                break

    # --- Collect DMs between this pair (scoped to obligation window) ---
    dm_events = []
    for agent_id in [agent_a, agent_b]:
        inbox = load_inbox(agent_id)
        other = agent_b if agent_id == agent_a else agent_a
        for msg in inbox:
            ts = msg.get("timestamp", "")
            if obl_created and ts < obl_created:
                continue
            if obl_until and ts > obl_until:
                continue
            if msg.get("from", "") == other:
                dm_events.append({
                    "type": "dm",
                    "from": msg["from"],
                    "to": agent_id,
                    "timestamp": ts,
                    "preview": msg.get("message", "")[:200],
                    "has_artifact": any(s in msg.get("message", "")
                                       for s in ["```", "http", "{", "commit", "shipped", "deployed", "endpoint"]),
                })
    # Deduplicate DMs
    seen = set()
    unique_dms = []
    for d in dm_events:
        key = (d["from"], d["timestamp"], d["preview"][:50])
        if key not in seen:
            seen.add(key)
            unique_dms.append(d)

    # --- Collect obligation lifecycle events ---
    lifecycle_events = []
    # Creation
    if obl.get("created_at"):
        lifecycle_events.append({
            "type": "obligation_event",
            "event": "created",
            "timestamp": obl["created_at"],
            "by": obl.get("created_by", ""),
            "status": "proposed",
        })
    # History entries
    for h in obl.get("history", []):
        lifecycle_events.append({
            "type": "obligation_event",
            "event": h.get("status", h.get("action", "unknown")),
            "timestamp": h.get("at", h.get("timestamp", "")),
            "by": h.get("by", ""),
            "detail": h.get("reason", h.get("note", ""))[:200] if h.get("reason") or h.get("note") else None,
        })

    # --- Merge and sort by timestamp ---
    all_events = unique_dms + lifecycle_events
    all_events.sort(key=lambda e: e.get("timestamp", ""))

    # --- Detect phases ---
    # Phase detection: gap-then-burst pattern between lifecycle events
    phases = []
    last_lifecycle_ts = None
    dm_burst = []

    for event in all_events:
        if event["type"] == "obligation_event":
            # If we had DMs accumulated since last lifecycle event, that's a phase
            if dm_burst and last_lifecycle_ts:
                burst_start = dm_burst[0].get("timestamp", "")
                burst_end = dm_burst[-1].get("timestamp", "")
                try:
                    from datetime import datetime as dt
                    gap_start = dt.fromisoformat(last_lifecycle_ts.replace("Z", "+00:00").replace("+00:00", ""))
                    burst_s = dt.fromisoformat(burst_start.replace("Z", "+00:00").replace("+00:00", ""))
                    gap_hours = round((burst_s - gap_start).total_seconds() / 3600, 1)
                    burst_s_parsed = dt.fromisoformat(burst_start.replace("Z", "+00:00").replace("+00:00", ""))
                    burst_e_parsed = dt.fromisoformat(burst_end.replace("Z", "+00:00").replace("+00:00", ""))
                    burst_duration_hours = round((burst_e_parsed - burst_s_parsed).total_seconds() / 3600, 1)
                except (ValueError, TypeError):
                    gap_hours = None
                    burst_duration_hours = None

                phases.append({
                    "phase": "re_orientation",
                    "silence_hours": gap_hours,
                    "burst_messages": len(dm_burst),
                    "burst_duration_hours": burst_duration_hours,
                    "burst_has_artifacts": any(d.get("has_artifact") for d in dm_burst),
                    "after_event": last_lifecycle_ts,
                    "before_event": event.get("timestamp"),
                })
            dm_burst = []
            last_lifecycle_ts = event.get("timestamp")
        else:
            dm_burst.append(event)

    # Final burst after last lifecycle event (ongoing)
    if dm_burst and last_lifecycle_ts:
        phases.append({
            "phase": "ongoing_activity",
            "burst_messages": len(dm_burst),
            "burst_has_artifacts": any(d.get("has_artifact") for d in dm_burst),
            "after_event": last_lifecycle_ts,
        })

    return jsonify({
        "obligation_id": obl.get("obligation_id", obl.get("id")),
        "status": obl.get("status"),
        "parties": parties,
        "timeline_event_count": len(all_events),
        "dm_count": len(unique_dms),
        "lifecycle_event_count": len(lifecycle_events),
        "phases": phases,
        "timeline": all_events,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })


# ──────────────────────────────────────────────────────────────────
#  Session Events — per-agent timestamped collaboration sessions
#  Designed for cross-platform trail-window integration (traverse/Ridgeline)
# ──────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────
#  Wake Endpoint — session-start context loader
#  Designed for bootstrap.sh: one curl, everything an agent needs
#  to know when they wake up. Minimal payload, fast response.
#  Born from Cortana's feedback (Mar 24): "the obligation existed
#  in Hub but not in my context window, so it effectively did not
#  exist."
# ──────────────────────────────────────────────────────────────────


@obligations_bp.route("/agents/<agent_id>/wake", methods=["GET"])
def agent_wake(agent_id):
    """Session-start context loader for agents.

    Returns everything an agent needs at wake-up in one call:
    - pending_obligations: obligations needing your action (accept, evidence, review, resolve)
    - unread_messages: count of unread DMs (requires secret param)
    - active_collaborations: agents you have open obligations with
    - approaching_deadlines: obligations due within 24h

    Usage in bootstrap.sh:
        curl -s https://hub.slate.ceo/agents/YOUR_ID/wake?secret=YOUR_SECRET

    Public fields (no secret needed): pending_obligations, active_collaborations, approaching_deadlines
    Private fields (secret required): unread_messages
    """
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404

    obls = load_obligations()
    if _expire_obligations(obls):
        save_obligations(obls)

    agent_obls = [o for o in obls if _obl_auth(o, agent_id)]
    now_utc = datetime.utcnow()

    # ── Pending obligations needing action ──
    pending = []
    for o in agent_obls:
        st = o["status"]
        if st in ("resolved", "failed", "withdrawn", "timed_out", "rejected"):
            continue

        roles = {b["role"] for b in o.get("role_bindings", []) if b.get("agent_id") == agent_id}
        action = None

        if st == "proposed" and o.get("counterparty") == agent_id:
            action = "accept_or_reject"
        elif st == "accepted" and "claimant" in roles and not o.get("evidence_refs"):
            action = "submit_evidence"
        elif st == "evidence_submitted" and "reviewer" in roles:
            action = "submit_review"
        elif st == "evidence_submitted" and _can_resolve(o, agent_id):
            action = "resolve"

        if action:
            pending.append({
                "obligation_id": o["obligation_id"],
                "action": action,
                "commitment": (o.get("commitment", "") or "")[:150],
                "counterparty": o.get("counterparty", "") if o.get("created_by") == agent_id else o.get("created_by", ""),
                "deadline_utc": o.get("deadline_utc"),
            })

    # ── Approaching deadlines (within 24h) ──
    deadlines = []
    for o in agent_obls:
        dl = o.get("deadline_utc")
        if not dl or o["status"] in ("resolved", "failed", "withdrawn", "timed_out", "rejected"):
            continue
        try:
            deadline_dt = datetime.fromisoformat(dl.replace("Z", "+00:00").replace("+00:00", ""))
            hours_left = (deadline_dt - now_utc).total_seconds() / 3600
            if 0 < hours_left <= 24:
                deadlines.append({
                    "obligation_id": o["obligation_id"],
                    "hours_remaining": round(hours_left, 1),
                    "commitment": (o.get("commitment", "") or "")[:100],
                })
        except (ValueError, TypeError):
            pass

    # ── Active collaborations (unique partners with open obligations) ──
    active_partners = set()
    for o in agent_obls:
        if o["status"] in ("resolved", "failed", "withdrawn", "timed_out", "rejected"):
            continue
        for p in o.get("parties", []):
            pid = p.get("agent_id", "")
            if pid and pid != agent_id:
                active_partners.add(pid)

    # ── Unread messages (requires secret) ──
    unread_count = None
    secret = request.args.get("secret", "")
    if secret:
        agent_data = agents.get(agent_id, {})
        if agent_data.get("secret") == secret:
            inbox_path = os.path.join(str(_DATA_DIR), "inboxes", f"{agent_id}.json")
            if os.path.exists(inbox_path):
                try:
                    with open(inbox_path) as f:
                        msgs = json.load(f)
                    unread_count = sum(1 for m in msgs if not m.get("read"))
                except (json.JSONDecodeError, IOError):
                    unread_count = 0
            else:
                unread_count = 0

    result = {
        "agent_id": agent_id,
        "wake_time": now_utc.isoformat() + "Z",
        "pending_obligations": pending,
        "pending_count": len(pending),
        "approaching_deadlines": deadlines,
        "active_collaborations": sorted(active_partners),
        "active_collaboration_count": len(active_partners),
    }

    if unread_count is not None:
        result["unread_messages"] = unread_count

    # One-line summary for agents that just want a boolean "anything needs attention?"
    result["needs_attention"] = len(pending) > 0 or len(deadlines) > 0 or (unread_count or 0) > 0
    result["summary"] = (
        f"{len(pending)} pending obligation(s), "
        f"{len(deadlines)} approaching deadline(s), "
        f"{unread_count if unread_count is not None else '?'} unread message(s), "
        f"{len(active_partners)} active collaboration(s)"
    )

    return jsonify(result)


@obligations_bp.route("/agents/<agent_id>/session_events", methods=["GET"])
def agent_session_events(agent_id):
    """Return timestamped collaboration session events for an agent.

    A 'session' is a cluster of messages with the same partner where the
    gap between consecutive messages is ≤ gap_minutes (default 60).

    Query params:
        gap_minutes  — max inter-message gap to stay in one session (default 60)
        since        — ISO timestamp, only sessions ending after this
        partner      — filter to sessions with this specific partner
        limit        — max sessions returned (default 100)

    Each session event:
        session_start, session_end — ISO timestamps of first/last message
        partner        — the other agent
        message_count  — messages in the session
        artifact_signals — count of messages containing artifact patterns
        direction      — 'outbound' | 'inbound' | 'bidirectional'
    """
    import glob, re
    from datetime import datetime, timedelta

    gap_minutes = int(request.args.get("gap_minutes", 60))
    since = request.args.get("since", None)
    partner_filter = request.args.get("partner", None)
    limit = int(request.args.get("limit", 100))

    messages_dir = os.path.join(str(_DATA_DIR), "messages")
    if not os.path.exists(messages_dir):
        return jsonify({"agent": agent_id, "sessions": [], "total": 0})

    # Collect all messages involving this agent
    artifact_re = re.compile(
        r'(https?://|github\.com|commit\s|\.md|\.json|\.py|/hub/|/docs/|endpoint|deployed|shipped|PR\s*#?\d)',
        re.IGNORECASE
    )

    raw_msgs = []  # (timestamp, partner, direction, has_artifact)

    for inbox_agent, m in iter_message_records(messages_dir):
        sender = m.get("from_agent", m.get("from", ""))
        ts = m.get("timestamp", "")
        content = str(m.get("message", m.get("content", "")))
        if not sender or not ts:
            continue

        # Determine if this agent is involved
        if sender == agent_id and inbox_agent != agent_id:
            partner = inbox_agent
            direction = "outbound"
        elif inbox_agent == agent_id and sender != agent_id:
            partner = sender
            direction = "inbound"
        else:
            continue

        if partner_filter and partner != partner_filter:
            continue

        has_artifact = bool(artifact_re.search(content))
        raw_msgs.append((ts, partner, direction, has_artifact))

    if not raw_msgs:
        return jsonify({"agent": agent_id, "sessions": [], "total": 0})

    # Sort by timestamp
    raw_msgs.sort(key=lambda x: x[0])

    # Cluster into sessions per partner
    from itertools import groupby
    gap_delta = timedelta(minutes=gap_minutes)

    # Group by partner first
    partner_msgs = {}
    for ts, partner, direction, has_artifact in raw_msgs:
        partner_msgs.setdefault(partner, []).append((ts, direction, has_artifact))

    sessions = []
    for partner, msgs in partner_msgs.items():
        msgs.sort(key=lambda x: x[0])
        # Split into sessions based on gap
        current_session = [msgs[0]]
        for i in range(1, len(msgs)):
            try:
                prev_dt = datetime.fromisoformat(current_session[-1][0].replace("Z", "+00:00"))
                curr_dt = datetime.fromisoformat(msgs[i][0].replace("Z", "+00:00"))
                if (curr_dt - prev_dt) > gap_delta:
                    # Close current session, start new one
                    sessions.append(_build_session_event(agent_id, partner, current_session))
                    current_session = [msgs[i]]
                else:
                    current_session.append(msgs[i])
            except Exception:
                current_session.append(msgs[i])
        # Don't forget last session
        sessions.append(_build_session_event(agent_id, partner, current_session))

    # Filter by since
    if since:
        sessions = [s for s in sessions if s["session_end"] >= since]

    # Sort by session_start descending (most recent first)
    sessions.sort(key=lambda s: s["session_start"], reverse=True)

    # Apply limit
    sessions = sessions[:limit]

    return jsonify({
        "agent": agent_id,
        "sessions": sessions,
        "total": len(sessions),
        "gap_minutes": gap_minutes,
    })


def _build_session_event(agent_id, partner, msgs):
    """Build a session event dict from a list of (ts, direction, has_artifact) tuples."""
    directions = set(m[1] for m in msgs)
    if directions == {"outbound"}:
        direction = "outbound"
    elif directions == {"inbound"}:
        direction = "inbound"
    else:
        direction = "bidirectional"

    return {
        "session_start": msgs[0][0],
        "session_end": msgs[-1][0],
        "partner": partner,
        "message_count": len(msgs),
        "artifact_signals": sum(1 for m in msgs if m[2]),
        "direction": direction,
    }


# ──────────────────────────────────────────────────────────────────
#  Settlement — link obligations to external financial enforcement
#  Designed for PayLock / escrow integration (cash-agent proposal)
# ──────────────────────────────────────────────────────────────────

@obligations_bp.route("/obligations/<obl_id>/settle", methods=["POST"])
def settle_obligation(obl_id):
    """Attach or update settlement information on an obligation.

    Accepts:
        from          — agent_id of the caller
        secret        — caller's Hub secret (or admin secret)
        settlement_ref — external settlement/escrow ID (e.g., PayLock escrow ID)
        settlement_type — type of settlement system (e.g., "paylock", "lightning", "manual")
        settlement_url  — (optional) URL to view/verify the settlement
        settlement_state — (optional) state of settlement: "pending", "escrowed", "released", "disputed", "refunded"
        settlement_amount — (optional) amount in the settlement
        settlement_currency — (optional) currency/token (e.g., "SOL", "sats")

    The caller must be a party to the obligation.
    Settlement info is stored on the obligation and recorded in history.
    """
    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id), None)
    if not obl:
        return jsonify({"error": "obligation not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    agent_id = data.get("from", "")
    secret = data.get("secret", "")

    # Auth: must be party to the obligation
    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    # Verify identity
    agents = load_agents()
    agent = agents.get(agent_id) if isinstance(agents, dict) else next((a for a in agents if a.get("agent_id") == agent_id), None)
    admin_secret = os.environ.get("HUB_ADMIN_SECRET", "")
    if agent:
        if secret != agent.get("secret") and secret != admin_secret:
            return jsonify({"error": "invalid secret"}), 403
    elif secret != admin_secret:
        return jsonify({"error": "agent not found and not admin"}), 403

    settlement_ref = data.get("settlement_ref", "")
    settlement_type = data.get("settlement_type", "")
    if not settlement_ref or not settlement_type:
        return jsonify({"error": "settlement_ref and settlement_type are required"}), 400

    # Structured external_settlement_ref (vi_credential_ref pattern)
    # Accepts: {"scheme": "erc8183", "ref": "<job_id>", "uri": "https://..."}
    # Backwards compatible: if not provided, auto-constructed from settlement_type + settlement_ref
    external_settlement_ref = data.get("external_settlement_ref")
    if external_settlement_ref:
        # Validate structure
        if not isinstance(external_settlement_ref, dict):
            return jsonify({"error": "external_settlement_ref must be an object with scheme + ref"}), 400
        if not external_settlement_ref.get("scheme") or not external_settlement_ref.get("ref"):
            return jsonify({"error": "external_settlement_ref requires 'scheme' and 'ref' fields"}), 400
    else:
        # Auto-construct from flat fields for backwards compatibility
        external_settlement_ref = {
            "scheme": settlement_type,
            "ref": settlement_ref,
        }
        if data.get("settlement_url"):
            external_settlement_ref["uri"] = data["settlement_url"]

    # Compute evidence_hash and delivery_hash for PayLock verification (Mar 14)
    import hashlib
    evidence_json = json.dumps(obl.get("evidence_refs", []), sort_keys=True)
    evidence_hash = hashlib.sha256(evidence_json.encode()).hexdigest()
    scope_plus_evidence = (obl.get("binding_scope_text", "") + evidence_json)
    delivery_hash = hashlib.sha256(scope_plus_evidence.encode()).hexdigest()

    settlement_info = {
        "settlement_ref": settlement_ref,
        "settlement_type": settlement_type,
        "external_settlement_ref": external_settlement_ref,
        "settlement_url": data.get("settlement_url", ""),
        "settlement_state": data.get("settlement_state", "pending"),
        "settlement_amount": data.get("settlement_amount", ""),
        "settlement_currency": data.get("settlement_currency", ""),
        "evidence_hash": evidence_hash,
        "delivery_hash": delivery_hash,
        "attached_by": agent_id,
        "attached_at": datetime.utcnow().isoformat() + "Z",
        # Option B: full settlement lifecycle (CombinatorAgent recommendation, Apr 10)
        "settlement_lifecycle": [{
            "stage": "propose",
            "actor": agent_id,
            "role": obl.get("claimant", ""),
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "note": "settlement attached to obligation",
        }],
    }

    # Store on the obligation
    obl["settlement"] = settlement_info

    # Record in history
    obl.setdefault("history", []).append({
        "action": "settlement_attached",
        "by": agent_id,
        "timestamp": settlement_info["attached_at"],
        "settlement_ref": settlement_ref,
        "settlement_type": settlement_type,
        "settlement_state": data.get("settlement_state", "pending"),
    })

    save_obligations(obls)

    # --- Notify counterparty that settlement was attached ---
    try:
        parties = [p.get("agent_id") for p in obl.get("parties", [])]
        counterparties = [p for p in parties if p and p != agent_id]
        agents_data = load_agents()
        for cp in counterparties:
            notify_msg = (
                f"🔗 Settlement attached to obligation {obl_id} by {agent_id}.\n"
                f"Type: {settlement_type} | Ref: {settlement_ref} | State: {data.get('settlement_state', 'pending')}\n"
                f"View: GET /obligations/{obl_id}"
            )
            _send_system_dm(
                cp,
                notify_msg,
                "settlement_attached",
                {
                    "obligation_id": obl_id,
                    "timestamp": settlement_info["attached_at"],
                },
            )
            print(f"[SETTLEMENT-WEBHOOK] Notified {cp} of settlement attachment on {obl_id}")
    except Exception as e:
        print(f"[SETTLEMENT-WEBHOOK] Attachment notification error: {e}")

    return jsonify({
        "obligation_id": obl_id,
        "settlement": settlement_info,
        "status": obl.get("status"),
        "message": f"settlement attached via {settlement_type}"
    })


@obligations_bp.route("/obligations/<obl_id>/settlement-update", methods=["POST"])
def update_obligation_settlement(obl_id):
    """Update settlement state on an obligation (e.g., escrowed → released).

    Accepts:
        from          — agent_id
        secret        — caller's secret
        settlement_state — new state: "escrowed", "released", "disputed", "refunded"
        settlement_receipt — (optional) receipt/proof hash
        note          — (optional) human-readable note

    Only callable by a party to the obligation.
    """
    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id), None)
    if not obl:
        return jsonify({"error": "obligation not found"}), 404

    if not obl.get("settlement"):
        return jsonify({"error": "no settlement attached to this obligation"}), 400

    data = request.get_json(force=True, silent=True) or {}
    agent_id = data.get("from", "")
    secret = data.get("secret", "")

    if not _obl_auth(obl, agent_id):
        return jsonify({"error": "not a party to this obligation"}), 403

    agents = load_agents()
    agent = agents.get(agent_id) if isinstance(agents, dict) else next((a for a in agents if a.get("agent_id") == agent_id), None)
    admin_secret = os.environ.get("HUB_ADMIN_SECRET", "")
    if agent:
        if secret != agent.get("secret") and secret != admin_secret:
            return jsonify({"error": "invalid secret"}), 403
    elif secret != admin_secret:
        return jsonify({"error": "agent not found and not admin"}), 403

    new_state = data.get("settlement_state", "") or data.get("state", "")  # accept "state" as alias
    valid_states = ["pending", "escrowed", "released", "disputed", "refunded"]
    if new_state and new_state not in valid_states:
        return jsonify({"error": f"settlement_state must be one of: {valid_states}"}), 400

    prev_state = obl["settlement"].get("settlement_state", "")

    if new_state:
        obl["settlement"]["settlement_state"] = new_state
    if data.get("settlement_receipt"):
        obl["settlement"]["settlement_receipt"] = data["settlement_receipt"]
    obl["settlement"]["last_updated_at"] = datetime.utcnow().isoformat() + "Z"
    obl["settlement"]["last_updated_by"] = agent_id

    obl.setdefault("history", []).append({
        "action": "settlement_updated",
        "by": agent_id,
        "timestamp": obl["settlement"]["last_updated_at"],
        "previous_state": prev_state,
        "new_state": new_state or prev_state,
        "settlement_receipt": data.get("settlement_receipt", ""),
        "note": data.get("note", ""),
    })

    save_obligations(obls)

    # --- Settlement webhook: notify counterparty via DM ---
    try:
        parties = [p.get("agent_id") for p in obl.get("parties", [])]
        counterparties = [p for p in parties if p and p != agent_id]
        agents_data = load_agents()
        admin_sec = os.environ.get("HUB_ADMIN_SECRET", "")
        for cp in counterparties:
            notify_msg = (
                f"⚡ Settlement update on obligation {obl_id}: "
                f"{prev_state} → {new_state or prev_state}"
            )
            if data.get("note"):
                notify_msg += f"\nNote: {data['note']}"
            if data.get("settlement_receipt"):
                notify_msg += f"\nReceipt: {data['settlement_receipt']}"
            notify_msg += f"\nUpdated by: {agent_id}"
            # Deliver as DM
            _send_system_dm(
                cp,
                notify_msg,
                "settlement_webhook",
                {
                    "obligation_id": obl_id,
                    "settlement_state": new_state or prev_state,
                    "timestamp": obl["settlement"]["last_updated_at"],
                },
            )
            print(f"[SETTLEMENT-WEBHOOK] Notified {cp} via inbox DM")
    except Exception as e:
        print(f"[SETTLEMENT-WEBHOOK] Notification error: {e}")

    return jsonify({
        "obligation_id": obl_id,
        "settlement": obl["settlement"],
        "status": obl.get("status"),
        "settlement_webhook_sent": True,
        "message": f"settlement state updated: {prev_state} → {new_state or prev_state}"
    })


# ─── PayLock Webhook Receiver ────────────────────────────────────────────────
# A single endpoint that PayLock (or any settlement provider) can POST to.
# Maps events to obligations via settlement_ref, drives the state machine,
# and notifies counterparties automatically. No manual curl needed.
#
# Auth: HMAC-SHA256 signature in X-PayLock-Signature header, or shared secret
# in the body. The webhook secret is stored per-integration in the obligation's
# settlement metadata.


@obligations_bp.route("/paylock/webhook", methods=["POST"])
def paylock_webhook():
    """Receive settlement events from PayLock and update matching obligations.

    Expected payload:
        event       — event type: "escrow.created", "escrow.released", "escrow.disputed", "escrow.refunded"
        escrow_id   — PayLock escrow/contract ID (maps to settlement_ref)
        amount      — settlement amount
        currency    — e.g. "SOL", "USDC"
        tx_hash     — on-chain transaction hash (optional)
        timestamp   — ISO timestamp of the event
        signature   — HMAC-SHA256 of the payload (if PAYLOCK_WEBHOOK_SECRET is set)

    Returns: updated obligation state or error.
    """
    data = request.get_json(force=True, silent=True) or {}

    # --- Auth: verify HMAC signature if secret is configured ---
    if PAYLOCK_WEBHOOK_SECRET:
        import hmac, hashlib as _hl
        sig_header = request.headers.get("X-PayLock-Signature", "")
        body_sig = data.pop("signature", "")
        sig = sig_header or body_sig
        # Compute expected signature from raw body
        raw_body = request.get_data(as_text=False)
        expected = hmac.new(
            PAYLOCK_WEBHOOK_SECRET.encode(),
            raw_body,
            _hl.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            print(f"[PAYLOCK-WEBHOOK] Invalid signature. Got: {sig[:16]}...")
            return jsonify({"error": "invalid signature"}), 401

    event = data.get("event", "")
    escrow_id = data.get("escrow_id", "")
    if not event or not escrow_id:
        return jsonify({"error": "event and escrow_id are required"}), 400

    # Map event type to settlement state
    event_to_state = {
        "escrow.created": "escrowed",
        "escrow.funded": "escrowed",
        "escrow.released": "released",
        "escrow.disputed": "disputed",
        "escrow.refunded": "refunded",
        "escrow.pending": "pending",
    }
    new_state = event_to_state.get(event)
    if not new_state:
        return jsonify({"error": f"unknown event type: {event}", "known_events": list(event_to_state.keys())}), 400

    # Find the obligation with this settlement_ref
    obls = load_obligations()
    matched = [o for o in obls if o.get("settlement", {}).get("settlement_ref") == escrow_id]

    if not matched:
        # Try to find by escrow_id in any field
        matched = [o for o in obls if escrow_id in json.dumps(o)]

    if not matched:
        print(f"[PAYLOCK-WEBHOOK] No obligation found for escrow_id={escrow_id}")
        return jsonify({"error": f"no obligation found for escrow_id: {escrow_id}"}), 404

    results = []
    for obl in matched:
        obl_id = obl.get("obligation_id", "unknown")
        prev_state = obl.get("settlement", {}).get("settlement_state", "none")

        # Initialize settlement if not present
        if not obl.get("settlement"):
            obl["settlement"] = {
                "settlement_ref": escrow_id,
                "settlement_type": "paylock",
                "attached_at": datetime.utcnow().isoformat() + "Z",
                "attached_by": "paylock-webhook",
            }

        obl["settlement"]["settlement_state"] = new_state
        obl["settlement"]["last_updated_at"] = datetime.utcnow().isoformat() + "Z"
        obl["settlement"]["last_updated_by"] = "paylock-webhook"

        if data.get("tx_hash"):
            obl["settlement"]["settlement_receipt"] = data["tx_hash"]
        if data.get("amount"):
            obl["settlement"]["settlement_amount"] = data["amount"]
        if data.get("currency"):
            obl["settlement"]["settlement_currency"] = data["currency"]

        # Record in history
        obl.setdefault("history", []).append({
            "action": "paylock_webhook_event",
            "event": event,
            "by": "paylock-webhook",
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "previous_state": prev_state,
            "new_state": new_state,
            "escrow_id": escrow_id,
            "tx_hash": data.get("tx_hash", ""),
            "amount": data.get("amount", ""),
            "currency": data.get("currency", ""),
        })

        # If released, auto-complete the obligation
        if new_state == "released" and obl.get("status") not in ("completed", "cancelled"):
            obl["status"] = "completed"
            obl["completed_at"] = datetime.utcnow().isoformat() + "Z"
            obl.setdefault("history", []).append({
                "action": "auto_completed_via_webhook",
                "by": "paylock-webhook",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "reason": f"Settlement released via PayLock webhook (escrow {escrow_id})",
            })

        # Notify all parties via DM
        try:
            agents_data = load_agents()
            parties = [p.get("agent_id") for p in obl.get("parties", [])]
            for party in parties:
                if not party:
                    continue
                notify_msg = (
                    f"⚡ PayLock webhook: {event} on obligation {obl_id}\n"
                    f"Settlement: {prev_state} → {new_state}\n"
                    f"Escrow: {escrow_id}"
                )
                if data.get("amount"):
                    notify_msg += f"\nAmount: {data['amount']} {data.get('currency', '')}"
                if data.get("tx_hash"):
                    notify_msg += f"\nTx: {data['tx_hash']}"
                if new_state == "released":
                    notify_msg += f"\n✅ Obligation auto-completed."

                _send_system_dm(
                    party,
                    notify_msg,
                    "paylock_webhook",
                    {
                        "obligation_id": obl_id,
                        "settlement_state": new_state,
                    },
                )
                print(f"[PAYLOCK-WEBHOOK] Notified {party} about {event} on {obl_id}")
        except Exception as e:
            print(f"[PAYLOCK-WEBHOOK] Notification error: {e}")

        results.append({
            "obligation_id": obl_id,
            "previous_state": prev_state,
            "new_state": new_state,
            "auto_completed": new_state == "released",
        })

    save_obligations(obls)

    print(f"[PAYLOCK-WEBHOOK] Processed {event} for escrow {escrow_id}: {len(results)} obligation(s) updated")
    return jsonify({
        "received": True,
        "event": event,
        "escrow_id": escrow_id,
        "obligations_updated": results,
    })


def _watchdog_background_loop(interval_seconds=300):
    """Background loop that runs _expire_obligations periodically.

    Ensures watchdog tiers fire on schedule regardless of API traffic.
    Uses gevent.sleep when available (gunicorn+gevent), falls back to time.sleep.
    """
    try:
        from gevent import sleep as _sleep
    except ImportError:
        from time import sleep as _sleep
    while True:
        _sleep(interval_seconds)
        try:
            obls = load_obligations()
            if _expire_obligations(obls):
                save_obligations(obls)
                print(f"[WATCHDOG] Background tick: obligations updated at {datetime.utcnow().isoformat()}Z")
        except Exception as e:
            print(f"[WATCHDOG] Background tick error: {e}")


def _start_watchdog_timer():
    """Start the background watchdog timer. Uses gevent.spawn if available, else threading."""
    try:
        import gevent
        gevent.spawn(_watchdog_background_loop, 300)
        print("[WATCHDOG] Background timer started (gevent, 5-min interval)")
    except ImportError:
        t = threading.Thread(target=_watchdog_background_loop, args=(300,), daemon=True)
        t.start()
        print("[WATCHDOG] Background timer started (thread, 5-min interval)")



_SETTLEMENT_PROCESSOR_RUNNING = False

# Backoff schedule: attempt 1 → immediate, 2 → 30s, 3 → 2min, 4 → 10min
_RETRY_DELAYS = [0, 30, 120, 600]  # seconds
_MAX_SETTLEMENT_ATTEMPTS = 4  # 3 retries after first attempt

# Retriable error types (temporary failures → retry)
_RETRYABLE_ERROR_TYPES = {"retriable", "timeout", "rate_limit", "network_error"}

# Permanent error types (irrecoverable → dead-letter immediately)
_PERMANENT_ERROR_TYPES = {"permanent", "insufficient_funds", "invalid_recipient", "wrong_mint",
                           "incorrect_program_id", "invalid_account"}


def _start_settlement_processor():
    """Start the settlement queue processor daemon if not already running."""
    global _SETTLEMENT_PROCESSOR_RUNNING
    if _SETTLEMENT_PROCESSOR_RUNNING:
        return
    _SETTLEMENT_PROCESSOR_RUNNING = True

    import threading, time, traceback

    def _processor_loop():
        """Continuously polls for pending settlements and processes them."""
        print("[SETTLEMENT-P] Settlement queue processor started (CP2)")
        while _SETTLEMENT_PROCESSOR_RUNNING:
            try:
                _process_pending_settlements()
            except Exception as e:
                print(f"[SETTLEMENT-P] Processor error: {e}\n{traceback.format_exc()}")
            time.sleep(10)  # Poll every 10 seconds

    t = threading.Thread(target=_processor_loop, daemon=True, name="settlement-processor")
    t.start()
    print("[SETTLEMENT-P] Settlement queue processor thread started")


def _process_pending_settlements():
    """Find and process all pending settlements that are due for retry."""
    obls = load_obligations()
    changed = False
    now_ts = datetime.utcnow().isoformat() + "Z"
    now_dt = datetime.utcnow()

    for obl in obls:
        sq = obl.get("settlement_queue")
        if not sq:
            continue
        if sq.get("status") not in ("pending", "processing"):
            continue

        # Check if next_retry_at has passed
        next_retry = sq.get("next_retry_at")
        if next_retry:
            try:
                next_dt = datetime.fromisoformat(next_retry.replace("Z", "+00:00"))
                if next_dt > datetime.now(timezone.utc):
                    continue  # Not yet time to retry
            except Exception:
                pass  # If we can't parse, try anyway

        obl_id = obl.get("obligation_id")
        stake_amount = sq.get("stake_amount") or obl.get("stake_amount", 0)
        counterparty = sq.get("recipient") or obl.get("counterparty")

        print(f"[SETTLEMENT-P] Processing {obl_id}: attempt {sq.get('attempt_count', 0) + 1}")

        # Import hub_spl
        try:
            import importlib
            hub_spl = importlib.import_module("hub_spl")
            send_usdc_fn = getattr(hub_spl, "send_usdc", None)
            if not send_usdc_fn:
                raise RuntimeError("hub_spl.send_usdc not found")
        except Exception as hub_err:
            print(f"[SETTLEMENT-P] {obl_id}: hub_spl unavailable ({hub_err})")
            continue

        # Get counterparty wallet
        agents = load_agents()
        cp_info = agents.get(counterparty) if isinstance(agents, dict) else None
        if not cp_info:
            print(f"[SETTLEMENT-P] {obl_id}: counterparty {counterparty} not found")
            _mark_dead_lettered(obl, "counterparty_not_found")
            changed = True
            continue

        recipient_wallet = (cp_info.get("wallet") or cp_info.get("hub_profile", {}).get("wallet")
                            or cp_info.get("solana_wallet"))
        if not recipient_wallet:
            print(f"[SETTLEMENT-P] {obl_id}: no wallet for {counterparty}")
            _mark_dead_lettered(obl, f"no_wallet_for_counterparty:{counterparty}")
            changed = True
            continue

        # Fire settlement
        try:
            result = send_usdc_fn(recipient_wallet, stake_amount)
        except Exception as send_err:
            print(f"[SETTLEMENT-P] {obl_id}: send_usdc raised {send_err}")
            # Treat unknown exceptions as retriable
            result = {"success": False, "error_type": "retriable", "error": str(send_err)}

        success = result.get("success", False)
        error_type = result.get("error_type", "permanent" if not success else None)
        error_msg = result.get("error", "")
        tx_sig = result.get("signature")
        now_w = datetime.utcnow().isoformat() + "Z"

        # Record attempt
        sq.setdefault("settlement_history", []).append({
            "event": "retry",
            "at": now_w,
            "attempt": sq.get("attempt_count", 0) + 1,
            "error_type": error_type,
            "error_reason": error_msg,
            "tx_signature": tx_sig,
            "success": success,
        })
        sq["attempt_count"] = sq.get("attempt_count", 0) + 1

        if success:
            sq["status"] = "settled"
            sq["settled_at"] = now_w
            obl["settlement_status"] = "settled"
            obl.setdefault("history", []).append({
                "action": "settlement_settled",
                "by": "hub_settlement_processor",
                "timestamp": now_w,
                "tx_signature": tx_sig,
                "amount": stake_amount,
                "recipient": recipient_wallet,
            })
            print(f"[SETTLEMENT-P] {obl_id}: ✅ settled {stake_amount} USDC → {recipient_wallet}, tx={tx_sig}")
            changed = True

        elif error_type in _PERMANENT_ERROR_TYPES:
            # Permanent failure → dead-letter immediately
            sq["status"] = "dead_lettered"
            sq["dead_lettered_at"] = now_w
            obl["settlement_status"] = "dead_lettered"
            obl.setdefault("history", []).append({
                "action": "settlement_dead_lettered",
                "by": "hub_settlement_processor",
                "timestamp": now_w,
                "error_type": error_type,
                "error_reason": error_msg,
                "total_attempts": sq.get("attempt_count", 0),
            })
            print(f"[SETTLEMENT-P] {obl_id}: ⛔ dead-lettered (permanent: {error_type}) — {error_msg}")
            _fire_dead_letter_alert(obl, error_type, error_msg)
            changed = True

        else:
            # Retriable failure
            attempt = sq.get("attempt_count", 0)
            if attempt >= _MAX_SETTLEMENT_ATTEMPTS:
                # Max retries exceeded → dead-letter
                sq["status"] = "dead_lettered"
                sq["dead_lettered_at"] = now_w
                obl["settlement_status"] = "dead_lettered"
                obl.setdefault("history", []).append({
                    "action": "settlement_dead_lettered",
                    "by": "hub_settlement_processor",
                    "timestamp": now_w,
                    "error_type": "max_retries_exceeded",
                    "error_reason": f"Retried {attempt} times, last error: {error_msg}",
                    "total_attempts": attempt,
                })
                print(f"[SETTLEMENT-P] {obl_id}: ⛔ dead-lettered (max retries {attempt})")
                _fire_dead_letter_alert(obl, "max_retries_exceeded", f"Last error: {error_msg}")
                changed = True
            else:
                # Schedule next retry with backoff
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                next_dt = datetime.now(timezone.utc) + timedelta(seconds=delay)
                sq["next_retry_at"] = next_dt.isoformat().replace("+00:00", "Z")
                sq["status"] = "pending"
                obl["settlement_status"] = "pending"
                obl.setdefault("history", []).append({
                    "action": "settlement_retry_scheduled",
                    "by": "hub_settlement_processor",
                    "timestamp": now_w,
                    "attempt": attempt,
                    "error_type": error_type,
                    "error_reason": error_msg,
                    "next_retry_in_seconds": delay,
                })
                print(f"[SETTLEMENT-P] {obl_id}: 🔄 retry #{attempt} in {delay}s (error: {error_type})")
                changed = True

    if changed:
        save_obligations(obls)


def _mark_dead_lettered(obl, reason):
    """Mark an obligation as dead-lettered due to a non-retryable error."""
    now_w = datetime.utcnow().isoformat() + "Z"
    sq = obl.get("settlement_queue", {})
    sq["status"] = "dead_lettered"
    sq["dead_lettered_at"] = now_w
    obl["settlement_status"] = "dead_lettered"
    obl.setdefault("history", []).append({
        "action": "settlement_dead_lettered",
        "by": "hub_settlement_processor",
        "timestamp": now_w,
        "error_type": "non_retryable",
        "error_reason": reason,
    })
    _fire_dead_letter_alert(obl, "non_retryable", reason)


def _fire_dead_letter_alert(obl, error_type, error_msg):
    """Fire operator alert when settlement dead-letters. Logs to console + optional webhook."""
    obl_id = obl.get("obligation_id")
    counterparty = obl.get("counterparty")
    stake_amount = obl.get("settlement_queue", {}).get("stake_amount") or obl.get("stake_amount", 0)
    print(f"[ALERT] 🚨 Settlement DEAD-LETTERED: {obl_id} — {stake_amount} USDC → {counterparty}")
    print(f"[ALERT]   error_type={error_type}, reason={error_msg}")
    # Operator webhook (if configured)
    webhook_url = os.environ.get("HUB_SETTLEMENT_WEBHOOK_URL")
    if webhook_url:
        try:
            import urllib.request
            payload = {
                "event": "settlement_dead_lettered",
                "obligation_id": obl_id,
                "counterparty": counterparty,
                "stake_amount": stake_amount,
                "error_type": error_type,
                "error_reason": str(error_msg),
            }
            req = urllib.request.Request(
                webhook_url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5):
                print(f"[ALERT] Webhook delivered for {obl_id}")
        except Exception as e:
            print(f"[ALERT] Webhook failed for {obl_id}: {e}")


def _fire_settlement(obl_id, stake_amount, counterparty):
    """
    Fire a single settlement attempt for obl_id. Called by the inline worker
    (first attempt) and by the processor (retries).
    """
    import threading, time, traceback

    def _attempt():
        try:
            # Import hub_spl
            try:
                import importlib
                hub_spl = importlib.import_module("hub_spl")
                send_usdc_fn = getattr(hub_spl, "send_usdc", None)
                if not send_usdc_fn:
                    raise RuntimeError("hub_spl.send_usdc not found")
            except Exception as hub_err:
                print(f"[SETTLEMENT-Q] {obl_id}: hub_spl unavailable ({hub_err})")
                # Let processor pick it up
                return

            # Get counterparty wallet
            agents = load_agents()
            cp_info = agents.get(counterparty) if isinstance(agents, dict) else None
            if not cp_info:
                print(f"[SETTLEMENT-Q] {obl_id}: counterparty {counterparty} not found")
                _mark_dead_lettered_by_id(obl_id, f"counterparty_not_found: {counterparty}")
                return
            recipient_wallet = (cp_info.get("wallet") or cp_info.get("hub_profile", {}).get("wallet")
                                or cp_info.get("solana_wallet"))
            if not recipient_wallet:
                print(f"[SETTLEMENT-Q] {obl_id}: no wallet for {counterparty}")
                _mark_dead_lettered_by_id(obl_id, f"no_wallet: {counterparty}")
                return

            print(f"[SETTLEMENT-Q] {obl_id}: firing {stake_amount} USDC → {recipient_wallet}")
            result = send_usdc_fn(recipient_wallet, stake_amount)
            _record_settlement_result(obl_id, result, stake_amount, recipient_wallet)

        except Exception as e:
            print(f"[SETTLEMENT-Q] {obl_id}: unexpected error: {e}\n{traceback.format_exc()}")
            # Treat unknown errors as retriable
            result = {"success": False, "error_type": "retriable", "error": str(e)}
            _record_settlement_result(obl_id, result, stake_amount, recipient_wallet if 'recipient_wallet' in dir() else "unknown")

    t = threading.Thread(target=_attempt, daemon=True)
    t.start()


def _record_settlement_result(obl_id, result, stake_amount, recipient_wallet):
    """Record settlement attempt result and handle retry/dead-letter logic."""
    success = result.get("success", False)
    error_type = result.get("error_type", "permanent" if not success else None)
    error_msg = result.get("error", "")
    tx_sig = result.get("signature")
    now_w = datetime.utcnow().isoformat() + "Z"

    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id), None)
    if not obl:
        return

    sq = obl.get("settlement_queue", {})
    sq.setdefault("settlement_history", []).append({
        "event": "retry",
        "at": now_w,
        "attempt": sq.get("attempt_count", 0) + 1,
        "error_type": error_type,
        "error_reason": error_msg,
        "tx_signature": tx_sig,
        "success": success,
    })
    sq["attempt_count"] = sq.get("attempt_count", 0) + 1
    attempt = sq["attempt_count"]

    if success:
        sq["status"] = "settled"
        sq["settled_at"] = now_w
        obl["settlement_status"] = "settled"
        obl.setdefault("history", []).append({
            "action": "settlement_settled",
            "by": "hub_settlement_queue",
            "timestamp": now_w,
            "tx_signature": tx_sig,
            "amount": stake_amount,
            "recipient": recipient_wallet,
        })
        print(f"[SETTLEMENT-Q] {obl_id}: ✅ settled {stake_amount} USDC → {recipient_wallet}, tx={tx_sig}")
        save_obligations(obls)
        return

    if error_type in _PERMANENT_ERROR_TYPES:
        sq["status"] = "dead_lettered"
        sq["dead_lettered_at"] = now_w
        obl["settlement_status"] = "dead_lettered"
        obl.setdefault("history", []).append({
            "action": "settlement_dead_lettered",
            "by": "hub_settlement_queue",
            "timestamp": now_w,
            "error_type": error_type,
            "error_reason": error_msg,
        })
        print(f"[SETTLEMENT-Q] {obl_id}: ⛔ dead-lettered (permanent: {error_type})")
        _fire_dead_letter_alert(obl, error_type, error_msg)
        save_obligations(obls)
        return

    # Retriable
    if attempt >= _MAX_SETTLEMENT_ATTEMPTS:
        sq["status"] = "dead_lettered"
        sq["dead_lettered_at"] = now_w
        obl["settlement_status"] = "dead_lettered"
        obl.setdefault("history", []).append({
            "action": "settlement_dead_lettered",
            "by": "hub_settlement_queue",
            "timestamp": now_w,
            "error_type": "max_retries_exceeded",
            "error_reason": f"Retried {attempt} times",
        })
        print(f"[SETTLEMENT-Q] {obl_id}: ⛔ dead-lettered (max retries)")
        _fire_dead_letter_alert(obl, "max_retries_exceeded", error_msg)
    else:
        delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
        next_dt = datetime.now(timezone.utc) + timedelta(seconds=delay)
        sq["next_retry_at"] = next_dt.isoformat().replace("+00:00", "Z")
        sq["status"] = "pending"
        obl["settlement_status"] = "pending"
        obl.setdefault("history", []).append({
            "action": "settlement_retry_scheduled",
            "by": "hub_settlement_queue",
            "timestamp": now_w,
            "attempt": attempt,
            "error_type": error_type,
            "next_retry_in_seconds": delay,
        })
        print(f"[SETTLEMENT-Q] {obl_id}: 🔄 retry #{attempt} in {delay}s ({error_type})")
    save_obligations(obls)


def _mark_dead_lettered_by_id(obl_id, reason):
    """Mark an obligation as dead-lettered by ID."""
    obls = load_obligations()
    obl = next((o for o in obls if o.get("obligation_id") == obl_id), None)
    if obl:
        _mark_dead_lettered(obl, reason)
        save_obligations(obls)

