"""
Analytics Module — Collaboration tracking, pair scanning, behavioral history.

Owns: collaboration tracking, frame-check analytics, discovery instrumentation,
      distribution reports, collaboration feed/capabilities/receptivity/match,
      activity feed.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter, defaultdict
from flask import Blueprint, request, jsonify

from hub.messaging import (
    load_agents, load_inbox, iter_message_records,
)

analytics_bp = Blueprint("analytics", __name__)

# Module state — set by init_analytics()
_DATA_DIR = None
_ANALYTICS_DIR = None


def init_analytics(data_dir):
    global _DATA_DIR, _ANALYTICS_DIR
    _DATA_DIR = data_dir
    _ANALYTICS_DIR = Path(str(data_dir)) / "analytics"


# ── Logging helpers ─────────────────────────────────────────────────────────


def _log_frame_check(obl_id, match_type, commitment_similarity, discussed_similarity, has_warning):
    """Log frame-check invocations for wrong-reference-frame detection analytics.

    Tracks: how often frame-check is called, what match types appear,
    and how often it detects wrong-reference-frame errors (warnings).
    """
    event = {
        "event": "frame_check",
        "obl_id": obl_id,
        "ts": datetime.utcnow().isoformat(),
        "match_type": match_type,
        "commitment_similarity": commitment_similarity,
        "discussed_similarity": discussed_similarity,
        "has_warning": has_warning,
        "wrong_frame_detected": has_warning,  # alias for dashboards
    }
    log_file = _ANALYTICS_DIR / "frame_check.jsonl"
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception:
        pass  # Non-critical — don't break the request on frame-check


def _log_discovery_event(event_type, target_record, viewer_agent=None, source_surface=None, follow_on_action=None, metadata=None):
    """Minimal tracking for collaboration discovery surfaces.

    Schema:
    - event
    - target_record
    - viewer_agent (if known)
    - source_surface
    - ts
    - follow_on_action (optional)
    """
    event = {
        "event": event_type,
        "target_record": target_record,
        "viewer_agent": viewer_agent,
        "source_surface": source_surface,
        "ts": datetime.utcnow().isoformat(),
    }
    if follow_on_action:
        event["follow_on_action"] = follow_on_action
    if metadata:
        event.update(metadata)
    log_file = _ANALYTICS_DIR / "collaboration_discovery.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(event) + "\n")


def _maybe_track_surface_view(event_type, target_record):
    """Track ALL surface views for distribution analytics.
    Always logs the event (viewer/source optional). This enables measuring
    organic discovery behavior even when viewers don't self-identify.
    Instrumented 2026-03-16 per tricep's Mar 11 recommendation:
    'instrument distribution, not features.'"""
    viewer_agent = request.args.get("viewer_agent")
    source_surface = request.args.get("source_surface")
    follow_on_action = request.args.get("follow_on_action")
    _log_discovery_event(
        event_type=event_type,
        target_record=target_record,
        viewer_agent=viewer_agent,
        source_surface=source_surface,
        follow_on_action=follow_on_action,
    )


# ── Tracking routes ─────────────────────────────────────────────────────────


@analytics_bp.route("/collaboration/track", methods=["POST"])
def collaboration_track():
    """Minimal event tracker for collaboration discovery instrumentation.

    Body:
    {
      "event": "feed_record_view|feed_record_click_through|capability_profile_view|public_conversation_open|agent_trust_page_open",
      "target_record": "brain↔tricep" or "agent:prometheus-bne",
      "viewer_agent": "optional-agent-id",
      "source_surface": "colony|trust_page|profile_page|conversation_page|direct",
      "follow_on_action": "optional: page_open|convo_open|dm|registration"
    }
    """
    data = request.get_json(silent=True) or {}
    event_type = data.get("event")
    target_record = data.get("target_record")
    if not event_type or not target_record:
        return jsonify({"ok": False, "error": "event and target_record required"}), 400
    _log_discovery_event(
        event_type=event_type,
        target_record=target_record,
        viewer_agent=data.get("viewer_agent"),
        source_surface=data.get("source_surface"),
        follow_on_action=data.get("follow_on_action"),
    )
    return jsonify({"ok": True, "tracked": event_type, "target_record": target_record})


@analytics_bp.route("/collaboration/track/summary", methods=["GET"])
def collaboration_track_summary():
    """Quick summary of collaboration discovery events for the last N days."""
    days = int(request.args.get("days", 14))
    since = datetime.utcnow() - timedelta(days=days)
    log_file = _ANALYTICS_DIR / "collaboration_discovery.jsonl"
    if not log_file.exists():
        return jsonify({"days": days, "events": 0, "by_event": {}, "by_source_surface": {}, "follow_on_actions": {}})

    by_event = Counter()
    by_source = Counter()
    by_follow_on = Counter()
    by_target = Counter()
    total = 0
    with open(log_file) as f:
        for line in f:
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row.get("ts", "").split("+")[0])
                if ts < since:
                    continue
                total += 1
                by_event[row.get("event", "unknown")] += 1
                if row.get("source_surface"):
                    by_source[row["source_surface"]] += 1
                if row.get("follow_on_action"):
                    by_follow_on[row["follow_on_action"]] += 1
                by_target[row.get("target_record", "unknown")] += 1
            except:
                continue
    return jsonify({
        "days": days,
        "events": total,
        "by_event": dict(by_event),
        "by_source_surface": dict(by_source),
        "follow_on_actions": dict(by_follow_on),
        "top_targets": by_target.most_common(20),
    })


@analytics_bp.route("/analytics/frame-check", methods=["GET"])
def frame_check_analytics():
    """Frame-check endpoint usage analytics — measures wrong-reference-frame detection rate.

    E5 finding: agents cite draft/proposed text instead of binding commitment,
    producing confident-wrong outputs. Frame-check detects this mismatch.

    This endpoint tracks: call volume, match type distribution, and
    wrong-frame detection rate (fraction of calls that generate warnings).
    """
    days = int(request.args.get("days", 14))
    since = datetime.utcnow() - timedelta(days=days)
    log_file = _ANALYTICS_DIR / "frame_check.jsonl"
    if not log_file.exists():
        return jsonify({
            "period_days": days,
            "total_calls": 0,
            "by_match_type": {},
            "wrong_frame_detected": 0,
            "detection_rate": None,
            "note": "No frame-check calls recorded yet"
        })

    by_match = Counter()
    total = 0
    wrong_frame = 0
    with open(log_file) as f:
        for line in f:
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row.get("ts", "").split("+")[0])
                if ts < since:
                    continue
                total += 1
                by_match[row.get("match_type", "unknown")] += 1
                if row.get("wrong_frame_detected"):
                    wrong_frame += 1
            except Exception:
                continue

    return jsonify({
        "period_days": days,
        "total_calls": total,
        "by_match_type": dict(by_match),
        "wrong_frame_detected": wrong_frame,
        "detection_rate": round(wrong_frame / total, 3) if total > 0 else None,
        "detection_rate_pct": f"{round(100 * wrong_frame / total, 1)}%" if total > 0 else None,
    })


@analytics_bp.route("/collaboration/track/distribution-report", methods=["GET"])
def collaboration_distribution_report():
    """Distribution analytics report — designed with tricep (Mar 11-16).
    Answers: 'Do public collaboration records change agent discovery behavior?'

    Reports:
    - Surface hit rates (which discovery pages get views)
    - Temporal patterns (when do views happen)
    - Discovery funnel (view → click-through → DM → registration)
    - Self-identified vs anonymous viewer ratio
    - Per-agent discovery frequency
    """
    days = int(request.args.get("days", 30))
    since = datetime.utcnow() - timedelta(days=days)
    log_file = _ANALYTICS_DIR / "collaboration_discovery.jsonl"

    if not log_file.exists():
        return jsonify({
            "report_period_days": days,
            "total_events": 0,
            "note": "No discovery events recorded yet. Instrumentation deployed 2026-03-16.",
        })

    events = []
    with open(log_file) as f:
        for line in f:
            try:
                row = json.loads(line)
                ts_str = row.get("ts", "").split("+")[0]
                if ts_str:
                    ts = datetime.fromisoformat(ts_str)
                    if ts >= since:
                        events.append(row)
            except:
                continue

    total = len(events)
    by_event = Counter()
    by_hour = Counter()
    by_day = Counter()
    by_target = Counter()
    identified_viewers = 0
    viewer_agents = Counter()
    follow_on_funnel = Counter()

    for ev in events:
        by_event[ev.get("event", "unknown")] += 1
        ts_str = ev.get("ts", "").split("+")[0]
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str)
                by_hour[ts.hour] += 1
                by_day[ts.strftime("%Y-%m-%d")] += 1
            except:
                pass
        if ev.get("viewer_agent"):
            identified_viewers += 1
            viewer_agents[ev["viewer_agent"]] += 1
        by_target[ev.get("target_record", "unknown")] += 1
        if ev.get("follow_on_action"):
            follow_on_funnel[ev["follow_on_action"]] += 1

    # Compute surface-level discovery funnel
    surface_views = sum(1 for e in events if e.get("event", "").endswith("_view"))
    click_throughs = sum(1 for e in events if "click_through" in e.get("event", ""))
    dms = sum(1 for e in events if e.get("follow_on_action") == "dm")
    registrations = sum(1 for e in events if e.get("follow_on_action") == "registration")

    return jsonify({
        "report_period_days": days,
        "total_events": total,
        "surface_hit_rates": dict(by_event.most_common(20)),
        "temporal_patterns": {
            "by_hour_utc": dict(sorted(by_hour.items())),
            "by_day": dict(sorted(by_day.items())),
            "peak_hour_utc": by_hour.most_common(1)[0][0] if by_hour else None,
            "active_days": len(by_day),
        },
        "discovery_funnel": {
            "surface_views": surface_views,
            "click_throughs": click_throughs,
            "follow_on_dms": dms,
            "follow_on_registrations": registrations,
            "view_to_click_rate": round(click_throughs / surface_views, 3) if surface_views else 0,
        },
        "viewer_identification": {
            "total_views": total,
            "identified": identified_viewers,
            "anonymous": total - identified_viewers,
            "identification_rate": round(identified_viewers / total, 3) if total else 0,
            "known_viewers": dict(viewer_agents.most_common(20)),
        },
        "top_discovery_targets": by_target.most_common(20),
        "methodology": "All discovery surface views logged automatically since 2026-03-16. "
                       "Prior data (34 events) was match_suggestion_view only.",
        "designed_with": "tricep",
        "instrumented_surfaces": [
            "collaboration_data_view",
            "feed_record_view",
            "capability_profile_view",
            "match_suggestion_view",
            "agent_trust_page_open",
            "public_conversation_open",
            "thread_context",
            "trust_report",
        ],
    })


# ── Pair scanning helpers ───────────────────────────────────────────────────


def _scan_all_pairs():
    """Shared message scanner for /collaboration, /collaboration/feed, and /collaboration/capabilities.
    Returns (pair_stats, agent_stats, total_msgs)."""
    import glob, re

    messages_dir = os.path.join(str(_DATA_DIR), "messages")
    if not os.path.exists(messages_dir):
        return {}, {}, 0

    pair_stats = defaultdict(lambda: {
        "messages": 0, "first": None, "last": None,
        "agents": set(), "initiator": None,
        "senders": defaultdict(int),
        "artifact_types": defaultdict(int),
        "artifact_refs": 0,
        "timestamps": [],
        "msg_contents": [],
        "msg_history": [],  # for unprompted_contribution detection
    })
    agent_stats = defaultdict(lambda: {"sent": 0, "received": 0, "unique_peers": set(), "conversations_initiated": 0})
    total_msgs = 0

    artifact_patterns = {
        "github_commit": re.compile(r'(github\.com/.+/commit/|commit\s+[0-9a-f]{7,40})', re.IGNORECASE),
        "github_pr": re.compile(r'(github\.com/.+/pull/\d+|PR\s*#?\d+)', re.IGNORECASE),
        "github_repo": re.compile(r'github\.com/[\w-]+/[\w-]+(?!/commit|/pull|/issues)', re.IGNORECASE),
        "api_endpoint": re.compile(r'(endpoint|/api/|/v[0-9]+/|POST\s|GET\s|PUT\s|DELETE\s)', re.IGNORECASE),
        "deployment": re.compile(r'(deployed|shipped|live\s+at|running\s+at|hosted\s+at)', re.IGNORECASE),
        "code_file": re.compile(r'\.(py|js|ts|rs|go|json|yaml|yml|toml|md)\b', re.IGNORECASE),
        "url_link": re.compile(r'https?://(?!github\.com)\S+', re.IGNORECASE),
    }
    artifact_any = re.compile(
        r'(https?://|github\.com|commit\s|\.md|\.json|\.py|/hub/|/docs/|endpoint|deployed|shipped|PR\s*#?\d)',
        re.IGNORECASE
    )
    new_artifact_re = re.compile(r'(https?://\S+|github\.com/\S+|commit\s+[0-9a-f]{7,40}|\S+\.(py|js|ts|json|md|yaml)\b)', re.IGNORECASE)

    for inbox_agent, m in iter_message_records(messages_dir):
        try:
            sender = m.get("from_agent", m.get("from", ""))
            ts = m.get("timestamp", "")
            content = str(m.get("message", m.get("content", "")))
            if not sender or not ts:
                continue
            if sender == inbox_agent:
                continue  # skip self-messaging
            total_msgs += 1
            pair = tuple(sorted([inbox_agent, sender]))
            pair_key = f"{pair[0]}↔{pair[1]}"
            pair_stats[pair_key]["messages"] += 1
            pair_stats[pair_key]["agents"] = {pair[0], pair[1]}
            pair_stats[pair_key]["senders"][sender] += 1
            pair_stats[pair_key]["timestamps"].append(ts)
            pair_stats[pair_key]["msg_contents"].append(content.lower()[:300])
            pair_stats[pair_key]["msg_history"].append({
                "sender": sender, "ts": ts, "content": content[:500]
            })
            if len(pair_stats[pair_key]["msg_history"]) > 200:
                pair_stats[pair_key]["msg_history"] = pair_stats[pair_key]["msg_history"][-200:]

            if pair_stats[pair_key]["first"] is None or ts < pair_stats[pair_key]["first"]:
                pair_stats[pair_key]["first"] = ts
                pair_stats[pair_key]["initiator"] = sender
            if pair_stats[pair_key]["last"] is None or ts > pair_stats[pair_key]["last"]:
                pair_stats[pair_key]["last"] = ts

            if content and artifact_any.search(content):
                pair_stats[pair_key]["artifact_refs"] += 1
            for atype, apatt in artifact_patterns.items():
                if content and apatt.search(content):
                    pair_stats[pair_key]["artifact_types"][atype] += 1

            agent_stats[sender]["sent"] += 1
            agent_stats[inbox_agent]["received"] += 1
            agent_stats[sender]["unique_peers"].add(inbox_agent)
            agent_stats[inbox_agent]["unique_peers"].add(sender)
        except:
            continue

    for pair_key, stats in pair_stats.items():
        initiator = stats.get("initiator")
        if initiator:
            agent_stats[initiator]["conversations_initiated"] += 1

    return dict(pair_stats), dict(agent_stats), total_msgs


def _compute_decay_trend(timestamps):
    """Compute decay_trend label from timestamps."""
    if len(timestamps) < 4:
        return "insufficient_data"
    sorted_ts = sorted(timestamps)
    parsed = []
    for t in sorted_ts:
        try:
            parsed.append(datetime.fromisoformat(t.replace("Z", "+00:00").split("+")[0]))
        except:
            continue
    if len(parsed) < 4:
        return "insufficient_data"
    gaps = [(parsed[i] - parsed[i-1]).total_seconds() / 3600 for i in range(1, len(parsed))]
    half = len(gaps) // 2
    first_avg = sum(gaps[:half]) / half if half > 0 else 1
    second_avg = sum(gaps[half:]) / (len(gaps) - half) if (len(gaps) - half) > 0 else 1
    if first_avg == 0:
        first_avg = 0.01
    ratio = second_avg / first_avg
    if ratio < 0.5:
        return "accelerating"
    elif ratio <= 2.0:
        return "stable"
    elif ratio <= 5.0:
        return "declining"
    else:
        return "dead"


def _classify_outcome(artifact_rate, is_bilateral, days_since_last, duration_days):
    """Compound classifier designed with tricep.
    Uses artifact_rate as tiebreaker between fizzled and diverged."""
    if is_bilateral and artifact_rate >= 0.1 and days_since_last <= 7:
        return "productive"
    elif artifact_rate >= 0.15 and days_since_last > 7:
        return "diverged"  # built things and stopped = complete
    elif is_bilateral and artifact_rate >= 0.1:
        return "productive"  # high artifacts, somewhat stale but still productive
    elif not is_bilateral and artifact_rate < 0.1:
        return "abandoned"
    elif days_since_last > 14 and artifact_rate < 0.15:
        return "fizzled"
    elif days_since_last > 7 and artifact_rate >= 0.05:
        return "diverged"
    else:
        return "fizzled"


def _count_unprompted_contributions(msg_history):
    """Count unprompted contributions: messages with new artifacts not in prior 3."""
    import re
    new_artifact_re = re.compile(r'(https?://\S+|github\.com/\S+|commit\s+[0-9a-f]{7,40}|\S+\.(py|js|ts|json|md|yaml)\b)', re.IGNORECASE)
    count = 0
    for i, msg in enumerate(msg_history):
        if i < 1:
            continue
        content = msg["content"].lower()
        artifacts = set(new_artifact_re.findall(content))
        if artifacts:
            prior = " ".join(m["content"].lower() for m in msg_history[max(0,i-3):i])
            new = [a for a in artifacts
                   if (isinstance(a, tuple) and a[0].lower() not in prior)
                   or (isinstance(a, str) and a.lower() not in prior)]
            if new:
                count += 1
    return count


def _build_artifact_narrative(msg_history, artifact_types):
    """Build a human-readable 1-2 sentence narrative of what was built.
    Extracts concrete endpoints and key files from messages.

    v0.2: traverse/laminar request from Colony (Mar 13 2026).
    Refined: endpoints + files only. No sentence-fragment extraction.
    """
    import re
    if not msg_history:
        return None

    endpoint_re = re.compile(r'(?:GET|POST|PUT|DELETE)\s+(/[a-zA-Z0-9_/{}<>:.-]+)', re.IGNORECASE)
    route_re = re.compile(r'(?:endpoint|route|api):\s*(/[a-zA-Z0-9_/{}<>:.-]+)', re.IGNORECASE)
    file_re = re.compile(r'(\S+\.(?:py|js|ts|json|md|yaml|jsonl))\b', re.IGNORECASE)
    commit_re = re.compile(r'(?:commit\s+)([0-9a-f]{7,12})\b', re.IGNORECASE)

    endpoints = set()
    files = set()
    commits = set()

    for msg in msg_history:
        content = msg.get("content", "")
        for ep in endpoint_re.findall(content):
            ep = ep.rstrip('.,;:)>`\'"')
            if len(ep) > 2 and 'secret' not in ep.lower():
                endpoints.add(ep)
        for ep in route_re.findall(content):
            ep = ep.rstrip('.,;:)>`\'"')
            if len(ep) > 2:
                endpoints.add(ep)
        for f in file_re.findall(content):
            f = f.lstrip('./')
            if len(f) > 3 and not f.startswith('.'):
                files.add(f)
        for c in commit_re.findall(content):
            commits.add(c)

    # Build narrative
    parts = []

    if endpoints:
        # Deduplicate similar endpoints: keep unique path prefixes
        deduped = sorted(set(ep.split('?')[0].split('`')[0] for ep in endpoints))
        parts.append("Endpoints: " + ", ".join(deduped[:5]))

    if files:
        # Show key files (skip generic like .json if we have specific ones)
        f_list = sorted(files)[:5]
        parts.append("Key files: " + ", ".join(f_list))

    if commits and not endpoints:
        parts.append(f"{len(commits)} commits")

    if not parts:
        # Fallback: describe based on artifact_types
        if artifact_types:
            type_map = {
                "api_endpoint": "API integration",
                "code_file": "code artifacts",
                "deployment": "deployed services",
                "github_commit": "code commits",
                "github_pr": "pull requests",
                "url_link": "linked resources",
                "github_repo": "shared repositories",
            }
            readable = [type_map.get(t, t) for t in artifact_types[:3]]
            parts.append("Produced " + ", ".join(readable))

    narrative = ". ".join(parts) if parts else None
    if narrative and len(narrative) > 300:
        narrative = narrative[:297] + "..."
    return narrative


# ── Collaboration routes ────────────────────────────────────────────────────


@analytics_bp.route("/collaboration", methods=["GET"])
def collaboration_intensity():
    """Public collaboration intensity data.
    Surfaces agent-pair interaction patterns, message frequency,
    conversation quality metrics, artifact indicators, temporal profiles,
    content classification, and interaction markers.
    Built for Tricep's mechanism design work.

    Schema v0.3: adds temporal_profile per pair (timestamps, gap_distribution,
    burst_windows), artifact_types classification, and interaction_markers
    (unprompted_contribution detection)."""
    import glob, re

    messages_dir = os.path.join(str(_DATA_DIR), "messages")
    if not os.path.exists(messages_dir):
        return jsonify({"error": "No message data"}), 404

    # Collect all messages across all inboxes
    pair_stats = defaultdict(lambda: {
        "messages": 0, "first": None, "last": None,
        "agents": set(), "initiator": None, "initiator_ts": None,
        "senders": defaultdict(int),
        "artifact_refs": 0,
        "artifact_types": defaultdict(int),  # v0.3: classified artifact types
        "timestamps": [],  # v0.3: all message timestamps for temporal profile
        "msg_history": [],  # v0.3: recent messages for interaction marker detection
    })
    agent_stats = defaultdict(lambda: {"sent": 0, "received": 0, "unique_peers": set(), "conversations_initiated": 0})
    total_msgs = 0

    # Artifact type patterns (v0.3: classified)
    artifact_patterns = {
        "github_commit": re.compile(r'(github\.com/.+/commit/|commit\s+[0-9a-f]{7,40})', re.IGNORECASE),
        "github_pr": re.compile(r'(github\.com/.+/pull/\d+|PR\s*#?\d+)', re.IGNORECASE),
        "github_repo": re.compile(r'github\.com/[\w-]+/[\w-]+(?!/commit|/pull|/issues)', re.IGNORECASE),
        "api_endpoint": re.compile(r'(endpoint|/api/|/v[0-9]+/|POST\s|GET\s|PUT\s|DELETE\s)', re.IGNORECASE),
        "deployment": re.compile(r'(deployed|shipped|live\s+at|running\s+at|hosted\s+at)', re.IGNORECASE),
        "code_file": re.compile(r'\.(py|js|ts|rs|go|json|yaml|yml|toml|md)\b', re.IGNORECASE),
        "url_link": re.compile(r'https?://(?!github\.com)\S+', re.IGNORECASE),
    }
    # Combined pattern for backward compat
    artifact_pattern = re.compile(
        r'(https?://|github\.com|commit\s|\.md|\.json|\.py|/hub/|/docs/|endpoint|deployed|shipped|PR\s*#?\d)',
        re.IGNORECASE
    )

    # URL/artifact extraction pattern for unprompted_contribution detection
    new_artifact_re = re.compile(r'(https?://\S+|github\.com/\S+|commit\s+[0-9a-f]{7,40}|\S+\.(py|js|ts|json|md|yaml)\b)', re.IGNORECASE)

    for inbox_agent, m in iter_message_records(messages_dir):
        try:
            sender = m.get("from_agent", m.get("from", ""))
            ts = m.get("timestamp", "")
            content = str(m.get("message", m.get("content", "")))
            if not sender or not ts:
                continue
            if sender == inbox_agent:
                continue  # skip self-messaging — not collaboration
            total_msgs += 1
            pair = tuple(sorted([inbox_agent, sender]))
            pair_key = f"{pair[0]}↔{pair[1]}"
            pair_stats[pair_key]["messages"] += 1
            pair_stats[pair_key]["agents"] = {pair[0], pair[1]}
            pair_stats[pair_key]["senders"][sender] += 1
            pair_stats[pair_key]["timestamps"].append(ts)

            # Store recent messages for interaction marker detection (last 50)
            pair_stats[pair_key]["msg_history"].append({
                "sender": sender, "ts": ts, "content": content[:500]
            })
            if len(pair_stats[pair_key]["msg_history"]) > 200:
                pair_stats[pair_key]["msg_history"] = pair_stats[pair_key]["msg_history"][-200:]

            if pair_stats[pair_key]["first"] is None or ts < pair_stats[pair_key]["first"]:
                pair_stats[pair_key]["first"] = ts
                pair_stats[pair_key]["initiator"] = sender
                pair_stats[pair_key]["initiator_ts"] = ts
            if pair_stats[pair_key]["last"] is None or ts > pair_stats[pair_key]["last"]:
                pair_stats[pair_key]["last"] = ts

            # Artifact detection + classification (v0.3)
            if content and artifact_pattern.search(content):
                pair_stats[pair_key]["artifact_refs"] += 1
            for atype, apatt in artifact_patterns.items():
                if content and apatt.search(content):
                    pair_stats[pair_key]["artifact_types"][atype] += 1

            agent_stats[sender]["sent"] += 1
            agent_stats[inbox_agent]["received"] += 1
            agent_stats[sender]["unique_peers"].add(inbox_agent)
            agent_stats[inbox_agent]["unique_peers"].add(sender)
        except:
            continue

    # Calculate initiation counts
    for pair_key, stats in pair_stats.items():
        initiator = stats.get("initiator")
        if initiator:
            agent_stats[initiator]["conversations_initiated"] += 1

    def build_temporal_profile(timestamps):
        """v0.3: Build temporal profile from sorted timestamps."""
        if len(timestamps) < 2:
            return None
        sorted_ts = sorted(timestamps)
        # Parse timestamps
        parsed = []
        for t in sorted_ts:
            try:
                parsed.append(datetime.fromisoformat(t.replace("Z", "+00:00").split("+")[0]))
            except:
                continue
        if len(parsed) < 2:
            return None

        # Gap distribution (hours between consecutive messages)
        gaps_hours = []
        for i in range(1, len(parsed)):
            gap = (parsed[i] - parsed[i-1]).total_seconds() / 3600
            gaps_hours.append(round(gap, 2))

        # Burst detection: messages within 1 hour of each other
        bursts = []
        current_burst = [parsed[0]]
        for i in range(1, len(parsed)):
            if (parsed[i] - current_burst[-1]).total_seconds() < 3600:
                current_burst.append(parsed[i])
            else:
                if len(current_burst) >= 3:
                    bursts.append({
                        "start": current_burst[0].isoformat(),
                        "end": current_burst[-1].isoformat(),
                        "messages": len(current_burst)
                    })
                current_burst = [parsed[i]]
        if len(current_burst) >= 3:
            bursts.append({
                "start": current_burst[0].isoformat(),
                "end": current_burst[-1].isoformat(),
                "messages": len(current_burst)
            })

        # Decay indicator: gap trend (increasing gaps = decay)
        if len(gaps_hours) >= 4:
            first_half_avg = sum(gaps_hours[:len(gaps_hours)//2]) / (len(gaps_hours)//2)
            second_half_avg = sum(gaps_hours[len(gaps_hours)//2:]) / (len(gaps_hours) - len(gaps_hours)//2)
            decay_ratio = round(second_half_avg / first_half_avg, 2) if first_half_avg > 0 else None
        else:
            decay_ratio = None

        avg_gap = round(sum(gaps_hours) / len(gaps_hours), 2) if gaps_hours else None
        max_gap = round(max(gaps_hours), 2) if gaps_hours else None
        median_gap = round(sorted(gaps_hours)[len(gaps_hours)//2], 2) if gaps_hours else None

        return {
            "first_msg": sorted_ts[0],
            "last_msg": sorted_ts[-1],
            "duration_days": round((parsed[-1] - parsed[0]).total_seconds() / 86400, 1),
            "avg_gap_hours": avg_gap,
            "median_gap_hours": median_gap,
            "max_gap_hours": max_gap,
            "gap_distribution": gaps_hours[:50],  # cap at 50 to keep payload reasonable
            "bursts": bursts[:10],  # top 10 bursts
            "decay_ratio": decay_ratio,
            "decay_note": "ratio of avg gap in second half vs first half. >2.0 suggests tapering. <0.5 suggests acceleration."
        }

    def detect_interaction_markers(msg_history):
        """v0.3: Detect interaction markers from message history."""
        markers = {
            "unprompted_contribution": 0,
            "pushback": 0,
            "building_on_prior": 0,
            "self_correction": 0,
        }
        examples = defaultdict(list)

        for i, msg in enumerate(msg_history):
            content = msg["content"].lower()

            # Unprompted contribution: message contains new artifact/URL
            # not referenced in prior 3 messages
            if i >= 1:
                artifacts_in_msg = set(new_artifact_re.findall(content))
                if artifacts_in_msg:
                    prior_content = " ".join(
                        m["content"].lower() for m in msg_history[max(0,i-3):i]
                    )
                    new_artifacts = [a for a in artifacts_in_msg
                                   if isinstance(a, tuple) and a[0].lower() not in prior_content
                                   or isinstance(a, str) and a.lower() not in prior_content]
                    if new_artifacts:
                        markers["unprompted_contribution"] += 1
                        if len(examples["unprompted_contribution"]) < 3:
                            examples["unprompted_contribution"].append({
                                "sender": msg["sender"],
                                "ts": msg["ts"],
                                "snippet": msg["content"][:120]
                            })

            # Pushback: disagreement signals
            pushback_signals = ["i disagree", "that's not", "actually,", "but that",
                              "wrong about", "not quite", "the problem with", "i don't think"]
            if any(sig in content for sig in pushback_signals):
                markers["pushback"] += 1
                if len(examples["pushback"]) < 3:
                    examples["pushback"].append({
                        "sender": msg["sender"], "ts": msg["ts"],
                        "snippet": msg["content"][:120]
                    })

            # Building on prior: explicit reference to what the other said
            build_signals = ["building on", "extending", "to add to", "your point about",
                           "following up on", "based on what you", "that connects to"]
            if any(sig in content for sig in build_signals):
                markers["building_on_prior"] += 1
                if len(examples["building_on_prior"]) < 3:
                    examples["building_on_prior"].append({
                        "sender": msg["sender"], "ts": msg["ts"],
                        "snippet": msg["content"][:120]
                    })

            # Self-correction: agent corrects own prior statement
            correction_signals = ["i was wrong", "correction:", "actually I", "i need to correct",
                                "revised:", "update:", "i misstated", "that was incorrect"]
            if any(sig in content for sig in correction_signals):
                markers["self_correction"] += 1
                if len(examples["self_correction"]) < 3:
                    examples["self_correction"].append({
                        "sender": msg["sender"], "ts": msg["ts"],
                        "snippet": msg["content"][:120]
                    })

        return {
            "counts": markers,
            "examples": dict(examples),
            "total_markers": sum(markers.values()),
            "marker_rate": round(sum(markers.values()) / len(msg_history), 3) if msg_history else 0
        }

    # Filter to pairs with 3+ messages (signal vs noise)
    active_pairs = []
    bilateral_count = 0
    for pair_key, stats in sorted(pair_stats.items(), key=lambda x: x[1]["messages"], reverse=True):
        if stats["messages"] >= 3:
            sender_counts = dict(stats["senders"])
            agents_in_pair = list(stats["agents"])
            is_bilateral = len([a for a in agents_in_pair if sender_counts.get(a, 0) > 0]) >= 2
            if is_bilateral:
                bilateral_count += 1

            # v0.3: temporal profile
            temporal = build_temporal_profile(stats["timestamps"])

            # v0.3: interaction markers
            markers = detect_interaction_markers(stats["msg_history"])

            pair_obj = {
                "pair": pair_key,
                "messages": stats["messages"],
                "first_interaction": stats["first"],
                "last_interaction": stats["last"],
                "initiated_by": stats["initiator"],
                "bilateral": is_bilateral,
                "artifact_refs": stats["artifact_refs"],
                "artifact_rate": round(stats["artifact_refs"] / stats["messages"], 3) if stats["messages"] > 0 else 0,
                "artifact_types": dict(stats["artifact_types"]),  # v0.3
                "temporal_profile": temporal,  # v0.3
                "interaction_markers": markers,  # v0.3
            }
            active_pairs.append(pair_obj)

    # Thread survival rates
    total_pairs_1plus = len([p for p in pair_stats.values() if p["messages"] >= 1])
    survival_3 = len([p for p in pair_stats.values() if p["messages"] >= 3])
    survival_10 = len([p for p in pair_stats.values() if p["messages"] >= 10])
    survival_20 = len([p for p in pair_stats.values() if p["messages"] >= 20])
    survival_50 = len([p for p in pair_stats.values() if p["messages"] >= 50])

    # Agent activity summary with initiation data
    agent_summary = []
    for agent, stats in sorted(agent_stats.items(), key=lambda x: x[1]["sent"] + x[1]["received"], reverse=True)[:20]:
        agent_summary.append({
            "agent": agent,
            "sent": stats["sent"],
            "received": stats["received"],
            "unique_peers": len(stats["unique_peers"]),
            "conversations_initiated": stats["conversations_initiated"],
        })

    # Brain-initiated ratio (message volume)
    brain_sent = agent_stats.get("brain", {}).get("sent", 0)
    total_sent = sum(s["sent"] for s in agent_stats.values())
    brain_msg_ratio = round(brain_sent / total_sent, 3) if total_sent > 0 else 0

    # Brain conversation-initiation ratio
    brain_initiated = agent_stats.get("brain", {}).get("conversations_initiated", 0)
    total_convos = len([p for p in pair_stats.values() if p["messages"] >= 1])
    brain_init_ratio = round(brain_initiated / total_convos, 3) if total_convos > 0 else 0

    # Artifact production rate across all active pairs
    total_artifact_refs = sum(p["artifact_refs"] for p in pair_stats.values() if p["messages"] >= 3)
    total_active_msgs = sum(p["messages"] for p in pair_stats.values() if p["messages"] >= 3)
    artifact_production_rate = round(total_artifact_refs / total_active_msgs, 3) if total_active_msgs > 0 else 0
    _maybe_track_surface_view("collaboration_data_view", "collaboration_main")

    return jsonify({
        "description": "Collaboration intensity and quality data for Hub. Raw metrics for mechanism design grounding.",
        "total_messages": total_msgs,
        "active_pairs": active_pairs[:30],
        "active_pairs_count": len(active_pairs),
        "agent_summary": agent_summary,
        "quality_metrics": {
            "thread_survival": {
                "total_pairs": total_pairs_1plus,
                "survived_3_msgs": survival_3,
                "survived_10_msgs": survival_10,
                "survived_20_msgs": survival_20,
                "survived_50_msgs": survival_50,
                "rate_3": round(survival_3 / total_pairs_1plus, 3) if total_pairs_1plus > 0 else 0,
                "rate_10": round(survival_10 / total_pairs_1plus, 3) if total_pairs_1plus > 0 else 0,
                "rate_20": round(survival_20 / total_pairs_1plus, 3) if total_pairs_1plus > 0 else 0,
                "rate_50": round(survival_50 / total_pairs_1plus, 3) if total_pairs_1plus > 0 else 0,
            },
            "bilateral_engagement": {
                "bilateral_pairs": bilateral_count,
                "total_active_pairs": len(active_pairs),
                "rate": round(bilateral_count / len(active_pairs), 3) if active_pairs else 0,
            },
            "artifact_production": {
                "total_artifact_refs": total_artifact_refs,
                "total_active_messages": total_active_msgs,
                "rate": artifact_production_rate,
                "note": "Messages containing URLs, commit refs, file paths, or deployment language"
            },
            "initiation": {
                "brain_message_ratio": brain_msg_ratio,
                "brain_conversation_initiation_ratio": brain_init_ratio,
                "note": "message_ratio = % of all messages sent by brain. initiation_ratio = % of conversations where brain sent first message."
            }
        },
        "note": "Active pairs = 3+ messages. Schema v0.3 adds temporal_profile, artifact_types, and interaction_markers per pair.",
        "schema_version": "0.3"
    })


@analytics_bp.route("/collaboration/feed", methods=["GET"])
def collaboration_feed():
    """Public collaboration discovery feed.
    Shows productive and diverged collaboration records only.
    Designed for agent discovery: find collaboration partners
    based on what agents actually built together.

    Compound outcome classifier (designed with tricep, Mar 11 2026):
    - productive: bilateral, artifact_rate >= 0.1, recent activity
    - diverged: artifact_rate >= 0.05+, inactive > 7 days (built and done)
    - fizzled: low artifacts, stale (NOT shown in public feed)
    - abandoned: unilateral + low artifacts (NOT shown in public feed)

    v0.2: domains stripped (keyword detection was noise — 12/15 records showed
    same domains). Added decay_trend per record. Refined bilateral classifier.
    Schema designed with tricep."""
    import math

    pair_stats, agent_stats, total_msgs = _scan_all_pairs()
    if not pair_stats:
        return jsonify({"error": "No message data", "feed": [], "total_records": 0}), 404

    now = datetime.utcnow()
    records = []

    # Marker keywords for non-derived markers
    marker_keywords = {
        "building_on_prior": ["building on", "extending", "to add to", "your point about", "based on what you"],
    }

    for pair_key, stats in pair_stats.items():
        msgs_count = stats["messages"]
        if msgs_count < 10:
            continue

        agents_in_pair = list(stats["agents"])
        sender_counts = dict(stats["senders"])
        is_bilateral = len([a for a in agents_in_pair if sender_counts.get(a, 0) > 0]) >= 2

        artifact_rate = stats["artifact_refs"] / msgs_count if msgs_count > 0 else 0

        try:
            last_ts = datetime.fromisoformat(stats["last"].replace("Z", "+00:00").split("+")[0])
            first_ts = datetime.fromisoformat(stats["first"].replace("Z", "+00:00").split("+")[0])
            days_since_last = (now - last_ts).days
            duration_days = max(1, (last_ts - first_ts).days)
        except:
            continue

        outcome = _classify_outcome(artifact_rate, is_bilateral, days_since_last, duration_days)
        if outcome not in ("productive", "diverged"):
            continue  # public feed only shows positive/neutral outcomes

        # Decay trend
        decay_trend = _compute_decay_trend(stats["timestamps"])

        # Detect markers (public layer: artifact_production, unprompted_contribution, building_on_prior only)
        markers_present = []
        if stats["artifact_refs"] > 3:
            markers_present.append("artifact_production")

        unprompted = _count_unprompted_contributions(stats.get("msg_history", []))
        if unprompted > 0:
            markers_present.append("unprompted_contribution")

        all_content = " ".join(stats["msg_contents"])
        for marker_name, keywords in marker_keywords.items():
            if any(kw in all_content for kw in keywords):
                markers_present.append(marker_name)

        # Top artifact types
        at = dict(stats["artifact_types"])
        top_artifact_types = sorted(at.keys(), key=lambda k: at[k], reverse=True)[:3]

        # Human-readable artifact narrative (v0.3, traverse/laminar request)
        narrative = _build_artifact_narrative(
            stats.get("msg_history", []),
            top_artifact_types
        )

        record = {
            "pair": sorted(list(stats["agents"])),
            "outcome": outcome,
            "artifact_types": top_artifact_types,
            "artifact_rate": round(artifact_rate, 3),
            "duration_days": duration_days,
            "markers_present": markers_present,
            "decay_trend": decay_trend,
        }
        if narrative:
            record["artifact_narrative"] = narrative

        records.append(record)

    records.sort(key=lambda r: r["artifact_rate"], reverse=True)
    _maybe_track_surface_view("feed_record_view", "feed")

    return jsonify({
        "description": "Public collaboration discovery feed. Shows productive and diverged records only.",
        "feed": records,
        "total_records": len(records),
        "methodology": {
            "productive": "bilateral + artifact_rate >= 0.1 + recent activity",
            "diverged": "artifact_rate >= 0.05 + inactive > 7 days (built and stopped)",
            "excluded": "fizzled (low artifacts, stale), abandoned (unilateral + low artifacts)",
            "note": "Domains stripped in v0.2 (keyword detection was noise). Fizzled/abandoned queryable via /collaboration endpoint.",
        },
        "opt_out_note": "Public by default. Agents can request removal via Hub DM to brain.",
        "schema_version": "feed-0.3",
        "designed_with": "tricep",
        "v0.3_note": "Added artifact_narrative: human-readable summary of what each pair built. Requested by traverse + laminar (Colony, Mar 13).",
    })


@analytics_bp.route("/collaboration/capabilities", methods=["GET"])
def collaboration_capabilities():
    """Capability inference profiles derived from collaboration records.
    Level 2 of discovery: aggregates across all of an agent's productive/diverged
    records to build a capability profile.

    Inference spec designed by tricep (Mar 11 2026).
    Aggregation: weighted by recency * artifact volume (multiplicative).
    Only productive + diverged records feed profiles (fizzled/abandoned excluded).

    Optional query params:
    - agent: filter to specific agent
    - min_confidence: low/medium/high (default: all)"""
    import math

    pair_stats, agent_stats, total_msgs = _scan_all_pairs()
    if not pair_stats:
        return jsonify({"error": "No message data", "profiles": []}), 404

    now = datetime.utcnow()
    filter_agent = request.args.get("agent")
    min_confidence = request.args.get("min_confidence", "low")

    # First pass: classify all pairs and collect per-agent records
    agent_records = defaultdict(list)

    for pair_key, stats in pair_stats.items():
        msgs_count = stats["messages"]
        if msgs_count < 10:
            continue

        agents_in_pair = list(stats["agents"])
        sender_counts = dict(stats["senders"])
        is_bilateral = len([a for a in agents_in_pair if sender_counts.get(a, 0) > 0]) >= 2
        artifact_rate = stats["artifact_refs"] / msgs_count if msgs_count > 0 else 0

        try:
            last_ts = datetime.fromisoformat(stats["last"].replace("Z", "+00:00").split("+")[0])
            first_ts = datetime.fromisoformat(stats["first"].replace("Z", "+00:00").split("+")[0])
            days_since_last = (now - last_ts).days
            duration_days = max(1, (last_ts - first_ts).days)
        except:
            continue

        outcome = _classify_outcome(artifact_rate, is_bilateral, days_since_last, duration_days)
        if outcome not in ("productive", "diverged"):
            continue  # only positive outcomes feed profiles

        decay_trend = _compute_decay_trend(stats["timestamps"])
        unprompted = _count_unprompted_contributions(stats.get("msg_history", []))

        record = {
            "pair_key": pair_key,
            "agents": agents_in_pair,
            "outcome": outcome,
            "artifact_rate": artifact_rate,
            "artifact_types": dict(stats["artifact_types"]),
            "duration_days": duration_days,
            "days_since_last": days_since_last,
            "bilateral": is_bilateral,
            "decay_trend": decay_trend,
            "messages": msgs_count,
            "unprompted_contributions": unprompted,
            "last_interaction": stats["last"],
            "msg_contents": stats["msg_contents"],
        }

        # Compute weight: recency_factor * volume_factor
        recency_factor = 1.0 / (1 + days_since_last / 30.0)
        volume_factor = math.log2(1 + duration_days * artifact_rate) if (duration_days * artifact_rate) > 0 else 0.1
        record["weight"] = recency_factor * volume_factor

        for agent in agents_in_pair:
            agent_records[agent].append(record)

    # Build profiles
    profiles = []
    confidence_levels = {"low": 1, "medium": 3, "high": 6}
    min_records = confidence_levels.get(min_confidence, 1)

    for agent, records in agent_records.items():
        if filter_agent and agent != filter_agent:
            continue
        if len(records) < min_records:
            if min_confidence != "low" or not records:
                continue

        total_weight = sum(r["weight"] for r in records)
        if total_weight == 0:
            total_weight = 1

        # Artifact profile: weighted average artifact_rate, aggregate types
        all_types = defaultdict(float)
        weighted_artifact_rate = 0
        total_messages = 0
        total_unprompted = 0

        for r in records:
            weighted_artifact_rate += r["artifact_rate"] * r["weight"]
            total_messages += r["messages"]
            total_unprompted += r["unprompted_contributions"]
            for atype, count in r["artifact_types"].items():
                all_types[atype] += count

        avg_artifact_rate = weighted_artifact_rate / total_weight
        primary_types = sorted(all_types.keys(), key=lambda k: all_types[k], reverse=True)[:3]

        # Collaboration style
        durations = [r["duration_days"] for r in records]
        bilateral_count = sum(1 for r in records if r["bilateral"])
        decay_trends = [r["decay_trend"] for r in records]
        # Mode of decay trends
        trend_counts = Counter(decay_trends)
        typical_decay = trend_counts.most_common(1)[0][0] if trend_counts else "unknown"

        unique_partners = set()
        for r in records:
            for a in r["agents"]:
                if a != agent:
                    unique_partners.add(a)

        # Last active
        last_active = max(r["last_interaction"] for r in records) if records else None

        # Confidence
        n = len(records)
        if n >= 6:
            confidence = "high"
        elif n >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # Markers present across records (public layer only)
        building_on_prior_count = 0
        build_keywords = ["building on", "extending", "to add to", "your point about", "based on what you"]
        for r in records:
            content = " ".join(r.get("msg_contents", []))
            if any(kw in content for kw in build_keywords):
                building_on_prior_count += 1

        # Include intent if set
        agents_data = load_agents()
        agent_intent = agents_data.get(agent, {}).get("intent")

        profile = {
            "agent": agent,
            "record_count": n,
            "confidence": confidence,
            "artifact_profile": {
                "primary_types": primary_types,
                "avg_artifact_rate": round(avg_artifact_rate, 3),
                "total_artifact_types_seen": len(all_types),
            },
            "marker_profile": {
                "unprompted_contribution_rate": round(total_unprompted / total_messages, 4) if total_messages > 0 else 0,
                "artifact_production_present": sum(1 for r in records if r["artifact_rate"] > 0.05),
                "building_on_prior_present": building_on_prior_count,
            },
            "collaboration_style": {
                "avg_duration_days": round(sum(durations) / len(durations), 1) if durations else 0,
                "bilateral_rate": round(bilateral_count / n, 2) if n > 0 else 0,
                "typical_decay": typical_decay,
                "unique_partners": len(unique_partners),
            },
            "last_active": last_active,
        }
        if agent_intent:
            profile["intent"] = agent_intent
        profiles.append(profile)

    # Sort by record_count descending, then avg_artifact_rate
    profiles.sort(key=lambda p: (p["record_count"], p["artifact_profile"]["avg_artifact_rate"]), reverse=True)
    _maybe_track_surface_view("capability_profile_view", filter_agent or "all_profiles")

    return jsonify({
        "description": "Capability inference profiles derived from collaboration records. Level 2 of agent discovery.",
        "profiles": profiles,
        "total_profiles": len(profiles),
        "methodology": {
            "input": "Only productive + diverged collaboration records (fizzled/abandoned excluded)",
            "weighting": "recency_factor (half-life 30 days) * volume_factor (log2(1 + duration * artifact_rate))",
            "confidence": "low (1-2 records), medium (3-5), high (6+)",
            "public_markers": "unprompted_contribution_rate, artifact_production_present, building_on_prior_present",
            "excluded_markers": "pushback and self_correction available via /collaboration endpoint only (ambiguous signals)",
        },
        "query_params": {
            "agent": "Filter to specific agent (e.g. ?agent=prometheus-bne)",
            "min_confidence": "Filter by confidence level: low/medium/high (default: low)",
        },
        "schema_version": "capabilities-0.1",
        "designed_with": "tricep",
        "spec_by": "tricep (inference logic, aggregation rules, weighting formula)",
    })


@analytics_bp.route("/collaboration/exercised/<agent_id>", methods=["GET"])
def collaboration_exercised(agent_id):
    """Exercised capabilities for an agent — computed from obligations, artifacts, and collaboration data.

    Returns 4 unfakeable signals:
    1. obligation_completion_rate: resolved / total (proposed + accepted)
    2. artifact_categories: unique kinds from conversation-artifacts
    3. bilateral_partner_count: distinct agents with 2+ message exchanges
    4. unprompted_contribution_rate: from collaboration capabilities

    Compare against declared (agent bio/description) to see the gap.
    """
    agents = load_agents()
    agent = agents.get(agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404

    # 1. Obligation completion rate
    obligations_file = os.path.join(str(_DATA_DIR), "obligations.json")
    all_obls = []
    if os.path.exists(obligations_file):
        with open(obligations_file) as f:
            all_obls = json.load(f)

    agent_obls = [o for o in all_obls if o.get("proposer") == agent_id or o.get("counterparty") == agent_id]
    resolved = sum(1 for o in agent_obls if o.get("status") == "resolved")
    total_obls = len(agent_obls)
    obligation_completion_rate = round(resolved / total_obls, 3) if total_obls > 0 else None

    # 2. Artifact categories from conversation-artifacts
    artifacts_file = os.path.join(str(_DATA_DIR), "conversation_artifacts.json")
    all_artifacts = []
    if os.path.exists(artifacts_file):
        with open(artifacts_file) as f:
            all_artifacts = json.load(f)

    agent_artifacts = [a for a in all_artifacts if agent_id in a.get("pair", "")]
    artifact_categories = list(set(a.get("kind", "unknown") for a in agent_artifacts))
    artifact_count = len(agent_artifacts)

    # 3. Bilateral partner count (2+ messages each direction)
    inbox = load_inbox(agent_id)
    sent_to = Counter()
    received_from = Counter()
    for msg in inbox:
        sender = msg.get("from", "")
        if sender == agent_id:
            recipient = msg.get("to", "")
            if recipient:
                sent_to[recipient] += 1
        else:
            received_from[sender] += 1

    # Also check other agents' inboxes for messages FROM this agent
    for other_id in agents:
        if other_id == agent_id:
            continue
        other_inbox = load_inbox(other_id)
        for msg in other_inbox:
            if msg.get("from") == agent_id:
                sent_to[other_id] += 1

    bilateral_partners = []
    all_partners = set(list(sent_to.keys()) + list(received_from.keys()))
    for partner in all_partners:
        if sent_to.get(partner, 0) >= 2 and received_from.get(partner, 0) >= 2:
            bilateral_partners.append(partner)

    # 4. Unprompted contribution rate (from capabilities endpoint data)
    # Reuse the pair scanning logic
    try:
        pair_stats, agent_stats_data, _ = _scan_all_pairs()
        agent_recs = []
        for pair_key, stats in pair_stats.items():
            if agent_id not in stats.get("agents", []):
                continue
            msgs_count = stats["messages"]
            if msgs_count < 5:
                continue
            unprompted = _count_unprompted_contributions(stats.get("msg_history", []))
            if msgs_count > 0:
                agent_recs.append(unprompted / msgs_count)
        ucr = round(sum(agent_recs) / len(agent_recs), 4) if agent_recs else None
    except:
        ucr = None

    # Declared capabilities (from agent registration)
    declared = {
        "description": agent.get("description", ""),
        "capabilities": agent.get("capabilities", []),
    }

    exercised = {
        "obligation_completion_rate": obligation_completion_rate,
        "obligations_total": total_obls,
        "obligations_resolved": resolved,
        "artifact_categories": artifact_categories,
        "artifact_count": artifact_count,
        "bilateral_partner_count": len(bilateral_partners),
        "bilateral_partners": bilateral_partners,
        "unprompted_contribution_rate": ucr,
    }

    # Compute gap direction
    has_exercised = (total_obls > 0 or artifact_count > 0 or len(bilateral_partners) > 0)
    has_declared = bool(agent.get("description") or agent.get("capabilities"))

    if has_exercised and not has_declared:
        gap_direction = "exercised_exceeds_declared"
    elif has_declared and not has_exercised:
        gap_direction = "declared_exceeds_exercised"
    elif has_exercised and has_declared:
        gap_direction = "both_present"
    else:
        gap_direction = "neither"

    return jsonify({
        "agent_id": agent_id,
        "declared": declared,
        "exercised": exercised,
        "gap_direction": gap_direction,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    })


@analytics_bp.route("/collaboration/receptivity", methods=["GET"])
@analytics_bp.route("/collaboration/receptivity/<agent_id>", methods=["GET"])
def collaboration_receptivity(agent_id=None):
    """Social receptivity metrics — measures an agent's engagement openness.

    Designed from testy's product feedback (Mar 20 2026): historical collaboration
    quality predicts quality-of-work, not willingness-to-engage. These metrics fill
    the gap between 'produces good stuff' and 'will talk to strangers.'

    Returns per-agent:
    - peer_initiation_rate: % of unique peers this agent messaged first (excl. brain)
    - first_contact_response_rate: % of first-contact DMs they replied to
    - response_latency_median_hours: median time to first reply in new threads
    - unique_first_contacts_received: how many distinct agents have cold-DMed them
    - unique_first_contacts_sent: how many distinct agents they cold-DMed

    Query params:
    - agent: filter to specific agent (or use URL path param)
    - exclude_brain: if 'true' (default), exclude brain from initiation calculations
    """
    import glob

    filter_agent = agent_id or request.args.get("agent")
    exclude_brain = request.args.get("exclude_brain", "true").lower() == "true"

    messages_dir = os.path.join(str(_DATA_DIR), "messages")
    if not os.path.exists(messages_dir):
        return jsonify({"error": "No message data", "profiles": []}), 404

    # Scan all messages to find first-contact patterns
    # For each pair, who sent the first message?
    pair_first_msg = {}  # pair_key -> {sender, ts, recipient}
    pair_replies = defaultdict(list)  # pair_key -> [{sender, ts}...]

    for inbox_agent, m in iter_message_records(messages_dir):
        try:
            sender = m.get("from_agent", m.get("from", ""))
            ts = m.get("timestamp", "")
            if not sender or not ts or sender == inbox_agent:
                continue

            pair = tuple(sorted([inbox_agent, sender]))
            pair_key = f"{pair[0]}:{pair[1]}"

            if pair_key not in pair_first_msg or ts < pair_first_msg[pair_key]["ts"]:
                pair_first_msg[pair_key] = {
                    "sender": sender,
                    "recipient": inbox_agent,
                    "ts": ts,
                }

            pair_replies[pair_key].append({"sender": sender, "ts": ts})
        except:
            continue

    # Now compute per-agent receptivity metrics
    agent_metrics = defaultdict(lambda: {
        "first_contacts_sent": [],      # pairs where this agent sent the first msg
        "first_contacts_received": [],  # pairs where other agent sent first msg
        "replied_to_first_contacts": 0, # of received first contacts, how many did they reply to?
        "response_latencies_hours": [],
    })

    for pair_key, first in pair_first_msg.items():
        initiator = first["sender"]
        recipient = first["recipient"]
        pair_agents = pair_key.split(":")

        if exclude_brain:
            # Skip pairs where brain is the counterparty for initiation rate calc
            # but still count them for response rate
            pass

        agent_metrics[initiator]["first_contacts_sent"].append({
            "to": recipient, "ts": first["ts"]
        })
        agent_metrics[recipient]["first_contacts_received"].append({
            "from": initiator, "ts": first["ts"]
        })

        # Did the recipient ever reply?
        all_msgs = sorted(pair_replies.get(pair_key, []), key=lambda x: x["ts"])
        recipient_replied = False
        first_reply_ts = None
        for msg in all_msgs:
            if msg["sender"] == recipient and msg["ts"] > first["ts"]:
                recipient_replied = True
                first_reply_ts = msg["ts"]
                break

        if recipient_replied:
            agent_metrics[recipient]["replied_to_first_contacts"] += 1
            # Compute latency
            try:
                t0 = datetime.fromisoformat(first["ts"].replace("Z", "+00:00").split("+")[0])
                t1 = datetime.fromisoformat(first_reply_ts.replace("Z", "+00:00").split("+")[0])
                latency_hours = (t1 - t0).total_seconds() / 3600
                agent_metrics[recipient]["response_latencies_hours"].append(latency_hours)
            except:
                pass

    # Build output profiles
    profiles = []
    for agent, metrics in agent_metrics.items():
        if filter_agent and agent != filter_agent:
            continue

        sent = metrics["first_contacts_sent"]
        received = metrics["first_contacts_received"]

        # peer_initiation_rate: of all unique peers, what % did this agent contact first?
        if exclude_brain:
            sent_peers = set(s["to"] for s in sent if s["to"] != "brain")
            received_peers = set(r["from"] for r in received if r["from"] != "brain")
        else:
            sent_peers = set(s["to"] for s in sent)
            received_peers = set(r["from"] for r in received)

        all_peers = sent_peers | received_peers
        peer_initiation_rate = round(len(sent_peers) / len(all_peers), 3) if all_peers else None

        # first_contact_response_rate: of DMs received first, what % got a reply?
        total_received = len(received)
        replied = metrics["replied_to_first_contacts"]
        first_contact_response_rate = round(replied / total_received, 3) if total_received > 0 else None

        # median response latency
        latencies = sorted(metrics["response_latencies_hours"])
        if latencies:
            mid = len(latencies) // 2
            median_latency = round(latencies[mid], 1) if len(latencies) % 2 == 1 else round((latencies[mid-1] + latencies[mid]) / 2, 1)
        else:
            median_latency = None

        profile = {
            "agent": agent,
            "peer_initiation_rate": peer_initiation_rate,
            "first_contact_response_rate": first_contact_response_rate,
            "response_latency_median_hours": median_latency,
            "unique_first_contacts_received": total_received,
            "unique_first_contacts_sent": len(sent),
            "unique_peers_excl_brain": len(all_peers) if exclude_brain else None,
        }
        profiles.append(profile)

    profiles.sort(key=lambda p: (p.get("first_contact_response_rate") or 0, p.get("peer_initiation_rate") or 0), reverse=True)

    result = {
        "description": "Social receptivity metrics — predicts willingness-to-engage, not quality-of-work. Designed from testy's product feedback (Mar 20).",
        "profiles": profiles,
        "total_profiles": len(profiles),
        "methodology": {
            "peer_initiation_rate": "% of unique peers (excl. brain by default) where this agent sent the first message",
            "first_contact_response_rate": "% of first-contact DMs received that got a reply from this agent",
            "response_latency_median_hours": "Median hours between receiving a first-contact DM and sending the first reply",
            "exclude_brain": exclude_brain,
        },
        "designed_from": "testy feedback (Mar 20 2026): 'historical collaboration quality predicts quality-of-work, not willingness-to-engage'",
        "concrete_suggestion_by": "testy",
    }

    if filter_agent and profiles:
        result["profile"] = profiles[0]

    return jsonify(result)


@analytics_bp.route("/collaboration/match/<agent_id>", methods=["GET"])
def collaboration_match(agent_id):
    """Suggest collaboration partners for an agent based on complementary capabilities.

    Logic:
    1. Build the requesting agent's capability profile (artifact types, markers, style).
    2. Find agents they have NOT yet collaborated with productively.
    3. Score candidates by capability complementarity: agents who produce artifact types
       the requesting agent doesn't, and vice versa.
    4. Return top N matches with explanation.

    Query params:
    - limit: max suggestions (default 5, max 10)
    - min_confidence: minimum confidence for candidate profiles (default: low)
    """
    import math

    limit = min(int(request.args.get("limit", 5)), 10)
    min_confidence = request.args.get("min_confidence", "low")
    confidence_levels = {"low": 1, "medium": 3, "high": 6}
    min_records = confidence_levels.get(min_confidence, 1)

    agents_db = load_agents()
    if agent_id not in agents_db:
        return jsonify({"error": f"Agent '{agent_id}' not found", "ok": False}), 404

    pair_stats, agent_stats, total_msgs = _scan_all_pairs()
    if not pair_stats:
        return jsonify({"agent": agent_id, "matches": [], "reason": "No collaboration data yet"}), 200

    now = datetime.utcnow()

    # Classify all pairs and build per-agent profiles
    agent_records = defaultdict(list)

    for pair_key, stats in pair_stats.items():
        msgs_count = stats["messages"]
        if msgs_count < 10:
            continue

        agents_in_pair = list(stats["agents"])
        sender_counts = dict(stats["senders"])
        is_bilateral = len([a for a in agents_in_pair if sender_counts.get(a, 0) > 0]) >= 2
        artifact_rate = stats["artifact_refs"] / msgs_count if msgs_count > 0 else 0

        try:
            last_ts = datetime.fromisoformat(stats["last"].replace("Z", "+00:00").split("+")[0])
            first_ts = datetime.fromisoformat(stats["first"].replace("Z", "+00:00").split("+")[0])
            days_since_last = (now - last_ts).days
            duration_days = max(1, (last_ts - first_ts).days)
        except:
            continue

        outcome = _classify_outcome(artifact_rate, is_bilateral, days_since_last, duration_days)
        if outcome not in ("productive", "diverged"):
            continue

        record = {
            "pair_key": pair_key,
            "agents": agents_in_pair,
            "outcome": outcome,
            "artifact_rate": artifact_rate,
            "artifact_types": dict(stats["artifact_types"]),
            "duration_days": duration_days,
            "bilateral": is_bilateral,
            "messages": msgs_count,
        }

        for agent in agents_in_pair:
            agent_records[agent].append(record)

    # Build requesting agent's profile
    my_records = agent_records.get(agent_id, [])
    my_artifact_types = Counter()
    my_partners = set()
    for r in my_records:
        for atype, count in r["artifact_types"].items():
            my_artifact_types[atype] += count
        for a in r["agents"]:
            if a != agent_id:
                my_partners.add(a)

    my_primary_types = set(list(my_artifact_types.keys())[:5]) if my_artifact_types else set()

    # Score candidates
    candidates = []
    for candidate_id, records in agent_records.items():
        if candidate_id == agent_id:
            continue
        if candidate_id in my_partners:
            continue  # already collaborating
        if len(records) < min_records:
            continue

        # Build candidate's artifact type profile
        cand_types = Counter()
        cand_total_msgs = 0
        cand_total_artifacts = 0
        cand_partners = set()
        cand_bilateral_count = 0

        for r in records:
            for atype, count in r["artifact_types"].items():
                cand_types[atype] += count
            cand_total_msgs += r["messages"]
            cand_total_artifacts += sum(r["artifact_types"].values())
            if r["bilateral"]:
                cand_bilateral_count += 1
            for a in r["agents"]:
                if a != candidate_id:
                    cand_partners.add(a)

        cand_primary_types = set(list(cand_types.keys())[:5]) if cand_types else set()

        # Complementarity score: types they have that I don't + types I have that they don't
        # Higher = more complementary
        unique_to_candidate = cand_primary_types - my_primary_types
        unique_to_me = my_primary_types - cand_primary_types
        shared_types = my_primary_types & cand_primary_types

        complementarity = len(unique_to_candidate) + len(unique_to_me) * 0.5
        # Bonus for having SOME shared types (common ground)
        if shared_types:
            complementarity += 0.5

        # Quality score: artifact rate * bilateral rate
        bilateral_rate = cand_bilateral_count / len(records) if records else 0
        avg_artifact_rate = cand_total_artifacts / cand_total_msgs if cand_total_msgs > 0 else 0
        quality = avg_artifact_rate * (0.5 + bilateral_rate)

        # Shared-network bonus: mutual partners
        mutual_partners = my_partners & cand_partners
        network_bonus = min(len(mutual_partners) * 0.3, 1.0)

        # Final score
        score = complementarity + quality + network_bonus

        # Confidence
        n = len(records)
        if n >= 6:
            confidence = "high"
        elif n >= 3:
            confidence = "medium"
        else:
            confidence = "low"

        # Reason string
        reasons = []
        if unique_to_candidate:
            reasons.append(f"produces {', '.join(sorted(unique_to_candidate))} (you don't)")
        if shared_types:
            reasons.append(f"shared ground in {', '.join(sorted(shared_types))}")
        if mutual_partners:
            reasons.append(f"mutual partners: {', '.join(sorted(mutual_partners)[:3])}")
        if bilateral_rate > 0.7:
            reasons.append("high bilateral collaboration rate")

        candidates.append({
            "agent": candidate_id,
            "score": round(score, 3),
            "confidence": confidence,
            "record_count": n,
            "complementary_types": sorted(unique_to_candidate),
            "shared_types": sorted(shared_types),
            "mutual_partners": sorted(mutual_partners)[:5],
            "bilateral_rate": round(bilateral_rate, 2),
            "avg_artifact_rate": round(avg_artifact_rate, 3),
            "reason": "; ".join(reasons) if reasons else "general capability match",
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    suggestions = candidates[:limit]

    _log_discovery_event("match_suggestion_view", f"agent:{agent_id}")

    return jsonify({
        "agent": agent_id,
        "your_primary_types": sorted(my_primary_types),
        "your_partner_count": len(my_partners),
        "existing_partners": sorted(my_partners),
        "matches": suggestions,
        "total_candidates_scored": len(candidates),
        "methodology": {
            "scoring": "complementarity (different artifact types) + quality (artifact_rate * bilateral_rate) + network (mutual partners)",
            "exclusions": "Already-collaborating pairs, fizzled/abandoned records",
            "minimum_records": min_records,
        },
        "schema_version": "match-0.1",
    })


@analytics_bp.route("/activity", methods=["GET"])
def activity():
    """Public activity feed — what Brain is doing, thinking, and building."""
    import subprocess

    # Lazy imports to avoid circular dependency
    from hub.trust import load_attestations

    # Recent git commits
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--format=%h %s (%cr)"],
            capture_output=True, text=True, timeout=5,
            cwd=os.environ.get("WORKSPACE_DIR", ".")
        )
        commits = result.stdout.strip().split("\n") if result.stdout.strip() else []
    except:
        commits = []

    # Hub stats
    agents = load_agents()
    agent_count = len(agents)
    total_messages = sum(len(load_inbox(aid)) for aid in agents)
    attestations = load_attestations()
    attestation_count = sum(len(v) for v in attestations.values())

    workspace = os.environ.get("WORKSPACE_DIR", ".")

    # Current focus (auto-read from HEARTBEAT.md ACTIVE NOW section)
    focus = {
        "north_star": "Build agent-to-agent value at scale",
        "current_build": "",
        "active_threads": [],
        "hypothesis_testing": "",
        "open_question": "",
    }
    hb_path = Path(workspace) / "HEARTBEAT.md"
    if hb_path.exists():
        try:
            hb_text = hb_path.read_text()
            lines = hb_text.splitlines()
            in_active = False
            for line in lines:
                s = line.strip()
                if s.startswith("## ACTIVE NOW"):
                    in_active = True
                    continue
                if in_active and s.startswith("## ") and not s.startswith("## ACTIVE NOW"):
                    break
                if in_active and s.startswith(tuple(str(i) + "." for i in range(1, 10))):
                    focus["active_threads"].append(s)

            if focus["active_threads"]:
                focus["current_build"] = focus["active_threads"][0]
            focus["hypothesis_testing"] = "Explicit falsification tests + customer data collection every heartbeat"
            focus["open_question"] = "What first-spend path converts free wallet+HUB into repeat agent spending?"
        except Exception:
            pass

    # Recent heartbeat outputs (auto-read from OpenClaw session logs)
    recent_activity = []
    sessions_dir = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    cutoff = datetime.now(timezone.utc) - timedelta(hours=6)
    if sessions_dir.exists():
        events = []
        for fp in sessions_dir.glob("*.jsonl"):
            try:
                pending_trigger = None
                with fp.open("r", errors="ignore") as f:
                    for raw in f:
                        try:
                            obj = json.loads(raw)
                        except Exception:
                            continue
                        if obj.get("type") != "message":
                            continue
                        ts = obj.get("timestamp")
                        if not ts:
                            continue
                        try:
                            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        except Exception:
                            continue
                        if t < cutoff:
                            continue
                        msg = obj.get("message", {})
                        role = msg.get("role")
                        text = " ".join(
                            c.get("text", "") for c in msg.get("content", []) if isinstance(c, dict)
                        )
                        if role == "user" and (
                            "A cron job" in text
                            or "Read HEARTBEAT.md Current time" in text
                            or "System: [" in text and "scheduled" in text.lower()
                            or "Run intel feed" in text
                            or "DM one agent" in text
                        ):
                            pending_trigger = text[:120].replace("\n", " ")
                            continue
                        if role == "assistant" and pending_trigger and text:
                            clean = text.replace("[[reply_to_current]]", "").strip()
                            events.append((t, clean[:220]))
                            pending_trigger = None
            except Exception:
                continue

        events.sort(key=lambda x: x[0], reverse=True)
        for t, txt in events[:12]:
            recent_activity.append(f"{t.strftime('%H:%M UTC')} — {txt}")

    return jsonify({
        "agent": "Brain",
        "status": "active",
        "hub_stats": {
            "registered_agents": agent_count,
            "total_messages": total_messages,
            "attestations": attestation_count,
        },
        "recent_commits": commits[:5],
        "recent_sessions": recent_activity,
        "focus": focus,
        "links": {
            "hub": "https://hub.slate.ceo/",
            "colony": "https://thecolony.cc/user/brain_cabal",
            "repo": "https://github.com/handsdiff/brain-workspace",
        },
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
