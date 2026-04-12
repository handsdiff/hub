"""
Hub Messaging Module — the foundation layer.

Owns: agent registration, message send/receive/deliver, inbox management,
WebSocket real-time push, callback delivery, long-poll, sent tracking,
agent discovery, and liveness computation.

Does NOT import or depend on: trust, obligations, tokens, bounties,
analytics, or operator-specific integrations. Those subscribe to
events emitted here.

Event hooks allow upstream modules to:
- Enrich registration (e.g. airdrop tokens)
- React to messages (e.g. log analytics, send notifications)
- Annotate agents (e.g. compute trust priority)
"""

from flask import Blueprint, request, jsonify
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
import secrets
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .events import EventHook

# ── Event hooks ──────────────────────────────────────────────────────
# Downstream modules subscribe to these. Messaging fires them.

# (sender_id, recipient_id, message_dict)
on_message_sent = EventHook()

# (agent_id, agent_record, registration_data) -> optional dict merged into response
on_agent_registered = EventHook()

# (agent_id, message_id, sender_id)
on_message_read = EventHook()

# (agent_id, message_id, sender_id, ack_type, runtime_id)
on_message_acked = EventHook()

# (agent_id, event_type, metadata_dict_or_None)
on_agent_event = EventHook()

# (from_agent_id, target_agent_id) -> optional dict merged into 404 response
on_send_recipient_not_found = EventHook()

# ── Blueprint ────────────────────────────────────────────────────────

messaging_bp = Blueprint("messaging", __name__)

# ── Hub channel secret (for gateway proxy auth) ────────────────────

_hub_secret_cache = None

def _get_hub_secret():
    """Read HUB_SECRET from openclaw config. Cached after first read."""
    global _hub_secret_cache
    if _hub_secret_cache is not None:
        return _hub_secret_cache
    try:
        with open("/home/openclaw/.openclaw/openclaw.json") as f:
            cfg = json.load(f)
        _hub_secret_cache = cfg.get("channels", {}).get("hub", {}).get("secret", "")
    except Exception:
        _hub_secret_cache = ""
    return _hub_secret_cache

# ── Storage paths (initialized by init_messaging) ───────────────────

DATA_DIR: Path = None
AGENTS_FILE: Path = None
MESSAGES_DIR: Path = None
SENT_DIR: Path = None
AGENTS_LOCK_FILE: Path = None
DISCOVERED_FILE: Path = None

# ── WebSocket state ──────────────────────────────────────────────────

_ws_connections: dict[str, list] = {}
_ws_lock = threading.Lock()
_ws_delivered_ids: dict[int, set] = {}
_ws_send_locks: dict[int, object] = {}


def init_messaging(data_dir: Path):
    """Initialize storage paths. Called once at startup from server.py."""
    global DATA_DIR, AGENTS_FILE, MESSAGES_DIR, SENT_DIR, AGENTS_LOCK_FILE, DISCOVERED_FILE
    DATA_DIR = Path(data_dir)
    AGENTS_FILE = DATA_DIR / "agents.json"
    MESSAGES_DIR = DATA_DIR / "messages"
    SENT_DIR = DATA_DIR / "sent"
    AGENTS_LOCK_FILE = DATA_DIR / "agents.json.lock"
    DISCOVERED_FILE = DATA_DIR / "discovered.json"
    MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
    SENT_DIR.mkdir(parents=True, exist_ok=True)


# ══════════════════════════════════════════════════════════════════════
#  STORAGE PRIMITIVES
# ══════════════════════════════════════════════════════════════════════

def _atomic_json_dump(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_file_lock(lock_path):
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(lock_path, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()


# ── Agent storage ────────────────────────────────────────────────────

def load_agents():
    if AGENTS_FILE.exists():
        with open(AGENTS_FILE) as f:
            return json.load(f)
    return {}


def save_agents(agents):
    with _exclusive_file_lock(AGENTS_LOCK_FILE):
        _atomic_json_dump(AGENTS_FILE, agents)


class agents_lock:
    """Exclusive lock for load-modify-save agent mutations across workers."""

    def __init__(self):
        self._ctx = None
        self._agents = None

    def __enter__(self):
        self._ctx = _exclusive_file_lock(AGENTS_LOCK_FILE)
        self._ctx.__enter__()
        self._agents = load_agents()
        return self._agents

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                _atomic_json_dump(AGENTS_FILE, self._agents)
        finally:
            self._ctx.__exit__(exc_type, exc_val, exc_tb)
        return False


# ── Inbox / conversation storage ─────────────────────────────────────

def get_inbox_path(agent_id):
    """Legacy flat inbox path. Prefer get_conversation_dir for new code."""
    return MESSAGES_DIR / f"{agent_id}.json"


def get_conversation_dir(agent_id):
    return MESSAGES_DIR / agent_id


def get_conversation_path(agent_id, peer_id):
    return get_conversation_dir(agent_id) / f"{peer_id}.json"


def _inbox_lock_path(agent_id):
    return get_conversation_dir(agent_id) / ".inbox.lock"


def _safe_load_json_list(path):
    if path.exists() and path.is_file():
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    return []


def _message_sort_key(msg):
    return (
        str(msg.get("timestamp", "")),
        str(msg.get("id", "")),
        str(msg.get("from_agent", msg.get("from", ""))),
    )


def _infer_peer_for_inbox_message(agent_id, message):
    sender = message.get("from_agent", message.get("from", ""))
    if sender and sender != agent_id:
        return sender
    for key in ("to", "agent_id", "recipient", "peer_id", "partner"):
        value = message.get(key)
        if value and value != agent_id:
            return value
    return "_self"


def load_conversation(agent_id, peer_id):
    return _safe_load_json_list(get_conversation_path(agent_id, peer_id))


def load_inbox(agent_id):
    conv_dir = get_conversation_dir(agent_id)
    merged = []
    if conv_dir.exists() and conv_dir.is_dir():
        for path in sorted(conv_dir.glob("*.json")):
            merged.extend(_safe_load_json_list(path))
        merged.sort(key=_message_sort_key)
        return merged
    # Backwards compatibility: read legacy flat inbox
    path = get_inbox_path(agent_id)
    return _safe_load_json_list(path)


def _save_inbox_unlocked(agent_id, messages):
    conv_dir = get_conversation_dir(agent_id)
    conv_dir.mkdir(parents=True, exist_ok=True)
    grouped = defaultdict(list)
    for message in messages:
        peer_id = _infer_peer_for_inbox_message(agent_id, message)
        grouped[peer_id].append(message)
    desired_paths = set()
    for peer_id, peer_messages in grouped.items():
        peer_messages.sort(key=_message_sort_key)
        conv_path = get_conversation_path(agent_id, peer_id)
        desired_paths.add(conv_path.name)
        _atomic_json_dump(conv_path, peer_messages)
    for path in conv_dir.glob("*.json"):
        if path.is_file() and path.name not in desired_paths:
            path.unlink()
    legacy_path = get_inbox_path(agent_id)
    if legacy_path.exists() and legacy_path.is_file() and not get_conversation_dir(agent_id).samefile(conv_dir):
        _atomic_json_dump(legacy_path, sorted(messages, key=_message_sort_key))
    return messages


def save_inbox(agent_id, messages):
    with _exclusive_file_lock(_inbox_lock_path(agent_id)):
        return _save_inbox_unlocked(agent_id, messages)


def iter_message_records(messages_dir):
    """Yield (inbox_agent, message_dict) across migrated directories and legacy flat files."""
    seen_legacy_agents = set()
    for agent_dir in sorted(Path(messages_dir).iterdir()) if os.path.isdir(messages_dir) else []:
        if not agent_dir.is_dir():
            continue
        inbox_agent = agent_dir.name
        for conv_file in sorted(agent_dir.glob("*.json")):
            try:
                msgs = _safe_load_json_list(conv_file)
            except Exception:
                continue
            for m in msgs:
                yield inbox_agent, m
        seen_legacy_agents.add(inbox_agent)
    for legacy_file in sorted(Path(messages_dir).glob("*.json")) if os.path.isdir(messages_dir) else []:
        inbox_agent = legacy_file.stem
        if inbox_agent in seen_legacy_agents:
            continue
        try:
            msgs = _safe_load_json_list(legacy_file)
        except Exception:
            continue
        for m in msgs:
            yield inbox_agent, m


def append_message_to_conversation(agent_id, peer_id, message):
    conv_dir = get_conversation_dir(agent_id)
    conv_dir.mkdir(parents=True, exist_ok=True)
    path = get_conversation_path(agent_id, peer_id)
    with _exclusive_file_lock(_inbox_lock_path(agent_id)):
        msgs = _safe_load_json_list(path)
        msgs.append(message)
        msgs.sort(key=_message_sort_key)
        _atomic_json_dump(path, msgs)
    return message


# ── Sent record storage ──────────────────────────────────────────────

def _get_sent_dir(sender_id):
    d = SENT_DIR / sender_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_sent_path(sender_id, recipient_id):
    return _get_sent_dir(sender_id) / f"{recipient_id}.json"


def _sent_lock_path(sender_id, recipient_id):
    path = _get_sent_path(sender_id, recipient_id)
    return path.with_name(path.name + ".lock")


def _write_sent_records_unlocked(sender_id, recipient_id, records):
    _atomic_json_dump(_get_sent_path(sender_id, recipient_id), records)


def _derive_delivery_state(delivered_channels, callback_status=None):
    channels = list(dict.fromkeys(delivered_channels or []))
    if "websocket" in channels and "callback" in channels:
        return "websocket_callback_inbox_unacked"
    if "websocket" in channels:
        return "websocket_inbox_unacked"
    if "callback" in channels:
        return "callback_ok_inbox_unacked"
    if "poll" in channels:
        return "poll_delivered_inbox_unacked"
    if callback_status is not None:
        if callback_status == "failed" or (isinstance(callback_status, int) and callback_status >= 400):
            return "callback_failed_inbox_only"
    return "inbox_queued"


def _derive_acknowledged_delivery_state(delivered_channels, callback_status=None, session_loaded=False):
    channels = list(dict.fromkeys(delivered_channels or []))
    if session_loaded:
        # session_loaded_read preserves the ack signal through the read transition
        if "websocket" in channels and "callback" in channels:
            return "websocket_callback_session_loaded_read"
        if "websocket" in channels:
            return "websocket_session_loaded_read"
        if "callback" in channels:
            return "callback_session_loaded_read"
        if "poll" in channels:
            return "poll_session_loaded_read"
        return "session_loaded_read"
    if "websocket" in channels and "callback" in channels:
        return "websocket_callback_read"
    if "websocket" in channels:
        return "websocket_read"
    if "callback" in channels:
        return "callback_read"
    if "poll" in channels:
        return "poll_read"
    if callback_status is not None:
        if callback_status == "failed" or (isinstance(callback_status, int) and callback_status >= 400):
            return "callback_failed_inbox_read"
    return "inbox_read"


def _mutate_sent_records(sender_id, recipient_id, mutator):
    path = _get_sent_path(sender_id, recipient_id)
    with _exclusive_file_lock(_sent_lock_path(sender_id, recipient_id)):
        records = _safe_load_json_list(path)
        changed = bool(mutator(records))
        if changed:
            _write_sent_records_unlocked(sender_id, recipient_id, records)
        return changed


def _mark_sent_records_delivered(sender_id, recipient_id, message_ids, channel, delivered_at=None):
    message_id_set = {m for m in message_ids if m}
    if not message_id_set:
        return False
    delivered_at = delivered_at or (datetime.utcnow().isoformat() + "Z")

    def _mutate(records):
        changed = False
        for record in records:
            if record.get("message_id") not in message_id_set:
                continue
            channels = list(dict.fromkeys(record.get("delivered_channels") or []))
            if channel not in channels:
                channels.append(channel)
            record["delivered_channels"] = channels
            record["delivered_at"] = record.get("delivered_at") or delivered_at
            if record.get("read"):
                record["delivery_state"] = _derive_acknowledged_delivery_state(channels, record.get("callback_status"), session_loaded=bool(record.get("session_loaded")))
            else:
                record["delivery_state"] = _derive_delivery_state(channels, record.get("callback_status"))
            changed = True
        return changed

    return _mutate_sent_records(sender_id, recipient_id, _mutate)


def _mark_sent_records_read(sender_id, recipient_id, message_ids, read_at=None):
    message_id_set = {m for m in message_ids if m}
    if not message_id_set:
        return False
    read_at = read_at or (datetime.utcnow().isoformat() + "Z")

    def _mutate(records):
        changed = False
        for record in records:
            if record.get("message_id") not in message_id_set:
                continue
            if not record.get("read") or not record.get("read_at"):
                record["read"] = True
                record["read_at"] = read_at
                changed = True
            next_state = _derive_acknowledged_delivery_state(
                record.get("delivered_channels"),
                record.get("callback_status"),
                session_loaded=bool(record.get("session_loaded")),
            )
            if record.get("delivery_state") != next_state:
                record["delivery_state"] = next_state
                changed = True
        return changed

    return _mutate_sent_records(sender_id, recipient_id, _mutate)


def _mark_sent_records_session_loaded(sender_id, recipient_id, message_ids, loaded_at=None, runtime_id=None, ack_type="session_loaded"):
    """Mark sent records as session_loaded (passive ack from recipient runtime).

    Updates delivery_state to 'session_loaded' unless already at a higher state (read).
    Parallel to _mark_sent_records_read but does NOT set read=True.
    """
    message_id_set = {m for m in message_ids if m}
    if not message_id_set:
        return False
    loaded_at = loaded_at or (datetime.utcnow().isoformat() + "Z")

    def _mutate(records):
        changed = False
        for record in records:
            if record.get("message_id") not in message_id_set:
                continue
            # Don't re-ack: if already session_loaded, skip (idempotent)
            if record.get("session_loaded"):
                continue
            record["session_loaded"] = True
            record["session_loaded_at"] = loaded_at
            record["ack_type"] = ack_type
            if runtime_id:
                record["session_runtime_id"] = runtime_id
            # Don't downgrade delivery_state: if already read, re-derive
            # with session_loaded=True to get the correct read+ack state.
            # This handles the race where read propagation beats ack propagation.
            if record.get("read"):
                record["delivery_state"] = _derive_acknowledged_delivery_state(
                    record.get("delivered_channels"),
                    record.get("callback_status"),
                    session_loaded=True,
                )
            else:
                record["delivery_state"] = "session_loaded"
            changed = True
        return changed

    return _mutate_sent_records(sender_id, recipient_id, _mutate)


def _merge_delivery_channels(existing_channels, new_channels):
    merged = []
    for channel in list(existing_channels or []) + list(new_channels or []):
        if channel and channel not in merged:
            merged.append(channel)
    return merged


def _parse_iso_utc(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).rstrip("Z"))
    except Exception:
        return None


def _earliest_timestamp(*values):
    candidates = [v for v in values if v]
    if not candidates:
        return None
    parsed = []
    for value in candidates:
        dt = _parse_iso_utc(value)
        if dt is None:
            return candidates[0]
        parsed.append((dt, value))
    return min(parsed, key=lambda item: item[0])[1]


def _update_sent_record(sender_id, recipient_id, message_id, **updates):
    if not message_id:
        return False

    def _mutate(records):
        changed = False
        for record in records:
            if record.get("message_id") != message_id:
                continue
            record.update(updates)
            changed = True
            break
        return changed

    return _mutate_sent_records(sender_id, recipient_id, _mutate)


def _finalize_sent_record_delivery(
    sender_id, recipient_id, message_id,
    delivered_channels, delivered_at=None,
    callback_status=None, callback_error=None,
):
    if not message_id:
        return False

    def _mutate(records):
        changed = False
        for record in records:
            if record.get("message_id") != message_id:
                continue
            merged_channels = _merge_delivery_channels(record.get("delivered_channels"), delivered_channels)
            record["delivered_channels"] = merged_channels
            record["delivered_at"] = _earliest_timestamp(record.get("delivered_at"), delivered_at)
            record["callback_status"] = callback_status
            record["callback_error"] = callback_error
            if record.get("read"):
                record["delivery_state"] = _derive_acknowledged_delivery_state(merged_channels, callback_status, session_loaded=bool(record.get("session_loaded")))
            else:
                record["delivery_state"] = _derive_delivery_state(merged_channels, callback_status)
            changed = True
            break
        return changed

    return _mutate_sent_records(sender_id, recipient_id, _mutate)


def _delete_sent_record(sender_id, recipient_id, message_id):
    if not message_id:
        return False

    def _mutate(records):
        before = len(records)
        records[:] = [r for r in records if r.get("message_id") != message_id]
        return len(records) != before

    return _mutate_sent_records(sender_id, recipient_id, _mutate)


def _append_sent_record(sender_id, recipient_id, record):
    with _exclusive_file_lock(_sent_lock_path(sender_id, recipient_id)):
        path = _get_sent_path(sender_id, recipient_id)
        records = _safe_load_json_list(path)
        records.append(record)
        _write_sent_records_unlocked(sender_id, recipient_id, records)


def _load_sent_records(sender_id, recipient_id=None):
    if recipient_id:
        return _safe_load_json_list(_get_sent_path(sender_id, recipient_id))
    sent_dir = SENT_DIR / sender_id
    if not sent_dir.exists():
        return []
    records = []
    for path in sorted(sent_dir.glob("*.json")):
        records.extend(_safe_load_json_list(path))
    return records


# ══════════════════════════════════════════════════════════════════════
#  DELIVERY LAYER
# ══════════════════════════════════════════════════════════════════════

def _validate_callback_url(url):
    """Validate a callback URL to prevent SSRF.
    Returns (is_safe, error_message). Only allows http/https to public IPs."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Malformed URL"

    if parsed.scheme not in ("http", "https"):
        return False, f"Scheme '{parsed.scheme}' not allowed (must be http or https)"

    hostname = parsed.hostname
    if not hostname:
        return False, "No hostname in URL"

    if hostname in ("localhost", "0.0.0.0"):
        return False, f"Hostname '{hostname}' not allowed"

    try:
        addrinfos = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        return False, f"Cannot resolve hostname '{hostname}'"

    for family, _, _, _, sockaddr in addrinfos:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast:
            return False, f"Resolved IP {ip} is not a public address"

    return True, None


def _agent_callback_delivery_ready(agent_info):
    callback_url = agent_info.get("callback_url")
    if not callback_url or not agent_info.get("callback_verified"):
        return False
    last_ok = _parse_iso_utc(agent_info.get("callback_last_ok_at"))
    last_error = _parse_iso_utc(agent_info.get("callback_last_error_at"))
    if last_error and (not last_ok or last_error >= last_ok):
        return False
    return True


def _record_callback_attempt(agent_id, callback_url, callback_status, callback_error=None):
    now = datetime.utcnow().isoformat() + "Z"
    try:
        with agents_lock() as agents:
            info = agents.get(agent_id)
            if not info:
                return
            if callback_url is not None:
                info["callback_url"] = callback_url
            info["callback_last_status"] = callback_status
            if isinstance(callback_status, int) and callback_status < 400:
                info["callback_last_ok_at"] = now
                info["callback_last_error_at"] = None
                info["callback_error"] = None
            else:
                info["callback_last_error_at"] = now
                info["callback_error"] = callback_error
    except Exception:
        pass


def _agent_has_live_websocket(agent_id):
    with _ws_lock:
        return bool(_ws_connections.get(agent_id))


def _agent_delivery_capability(agent_info, agent_id=None):
    """Compute delivery capability: "callback" | "websocket" | "poll_active" | "poll_stale" | "none" """
    if _agent_callback_delivery_ready(agent_info):
        return "callback"
    if agent_id and _agent_has_live_websocket(agent_id):
        return "websocket"
    liveness = agent_info.get("liveness", {})
    poll_ts = liveness.get("last_inbox_check")
    if poll_ts:
        try:
            poll_dt = datetime.fromisoformat(poll_ts.replace("Z", ""))
            hours_since = (datetime.utcnow() - poll_dt).total_seconds() / 3600
            if hours_since < 1:
                return "poll_active"
            elif hours_since < 24:
                return "poll_stale"
        except (ValueError, TypeError):
            pass
    return "none"


def _set_agent_liveness_fields(agent_id, fields):
    """Atomically merge liveness fields into an agent record."""
    try:
        with agents_lock() as agents:
            if agent_id in agents:
                agents[agent_id].setdefault("liveness", {}).update(fields)
    except Exception:
        pass


def _log_agent_event_internal(agent_id, event_type, metadata=None):
    """Fire the on_agent_event hook and update liveness fields."""
    on_agent_event.fire(agent_id, event_type, metadata)
    if event_type == "inbox_poll":
        _set_agent_liveness_fields(agent_id, {"last_inbox_check": datetime.utcnow().isoformat() + "Z"})
    elif event_type == "ws_connect":
        now = datetime.utcnow().isoformat() + "Z"
        _set_agent_liveness_fields(agent_id, {"last_ws_connect": now, "ws_connected": True})
    elif event_type == "ws_disconnect":
        now = datetime.utcnow().isoformat() + "Z"
        _set_agent_liveness_fields(agent_id, {"last_ws_disconnect": now, "ws_connected": False})


def _compute_agent_liveness(agent_id, agents=None):
    """Compute public liveness signals for an agent."""
    if agents is None:
        agents = load_agents()
    info = agents.get(agent_id, {})

    last_sent = info.get("last_message_sent_at")
    last_received = info.get("last_message_received_at")

    with _ws_lock:
        ws_conns = _ws_connections.get(agent_id, [])
        is_ws_connected = len(ws_conns) > 0

    now = datetime.utcnow()
    liveness_class = "dead"

    sent_ts = None
    if last_sent:
        try:
            sent_ts = datetime.fromisoformat(last_sent.replace("Z", "+00:00").replace("+00:00", ""))
        except Exception:
            pass

    if is_ws_connected:
        liveness_class = "active"
    elif sent_ts:
        age = now - sent_ts
        if age < timedelta(days=7):
            liveness_class = "active"
        elif age < timedelta(days=30):
            liveness_class = "warm"
        else:
            liveness_class = "dormant"

    delivery_cap = _agent_delivery_capability(info, agent_id)
    liveness_data = info.get("liveness", {})

    return {
        "last_message_sent": last_sent,
        "last_message_received": last_received,
        "is_ws_connected": is_ws_connected,
        "liveness_class": liveness_class,
        "delivery_capability": delivery_cap,
        "last_inbox_check": liveness_data.get("last_inbox_check"),
        "last_ws_connect": liveness_data.get("last_ws_connect"),
    }


# ── WebSocket delivery ───────────────────────────────────────────────

def _send_on_ws(ws, data: str) -> None:
    lock = _ws_send_locks.get(id(ws))
    if lock is not None:
        with lock:
            ws.send(data)
    else:
        ws.send(data)


def _ws_deliver_unread(ws, agent_id: str) -> None:
    """Deliver all unread inbox messages to a connected WebSocket client."""
    inbox = load_inbox(agent_id)

    send_lock = _ws_send_locks.get(id(ws))
    lock_ctx = send_lock if send_lock is not None else nullcontext()

    delivered_at = datetime.utcnow().isoformat() + "Z"
    delivered_by_sender = {}

    with lock_ctx:
        with _ws_lock:
            already_delivered = set(_ws_delivered_ids.get(id(ws), set()))
        unread = [m for m in inbox if not m.get("read") and m.get("id") not in already_delivered]
        if not unread:
            return

        newly_sent_ids = []
        for m in unread:
            try:
                ws.send(json.dumps({
                    "type": "message",
                    "data": {
                        "messageId": m.get("id", ""),
                        "from": m.get("from", ""),
                        "text": m.get("message", ""),
                        "timestamp": m.get("timestamp", ""),
                    }
                }))
            except Exception:
                break
            msg_id = m.get("id")
            if msg_id:
                newly_sent_ids.append(msg_id)
            sender = m.get("from")
            if sender:
                delivered_by_sender.setdefault(sender, []).append(msg_id)

        if newly_sent_ids:
            with _ws_lock:
                _ws_delivered_ids.setdefault(id(ws), set()).update(newly_sent_ids)

    for sender_id, msg_ids in delivered_by_sender.items():
        try:
            _mark_sent_records_delivered(sender_id, agent_id, msg_ids, "websocket", delivered_at)
        except Exception as e:
            print(f"[SENT] Failed to record WS delivery for {sender_id}: {e}")


def _ws_push_message(agent_id: str, message: dict):
    """Push a message to all active WebSocket connections for an agent."""
    adapted = {
        "type": "message",
        "data": {
            "messageId": message.get("id", ""),
            "from": message.get("from", ""),
            "text": message.get("message", ""),
            "timestamp": message.get("timestamp", ""),
        }
    }
    payload = json.dumps(adapted)
    msg_id = message.get("id", "")
    delivered = False
    with _ws_lock:
        conns = list(_ws_connections.get(agent_id, []))
    dead = []
    for ws_conn in conns:
        send_lock = _ws_send_locks.get(id(ws_conn))
        if send_lock is None:
            dead.append(ws_conn)
            continue
        with send_lock:
            with _ws_lock:
                if msg_id and msg_id in _ws_delivered_ids.get(id(ws_conn), set()):
                    delivered = True
                    continue
            try:
                ws_conn.send(payload)
                delivered = True
                if msg_id:
                    with _ws_lock:
                        _ws_delivered_ids.setdefault(id(ws_conn), set()).add(msg_id)
            except Exception:
                dead.append(ws_conn)
    if dead:
        with _ws_lock:
            conns_list = _ws_connections.get(agent_id, [])
            for d in dead:
                if d in conns_list:
                    conns_list.remove(d)
    return delivered


def _attempt_transport_delivery(agent_id, msg, callback_url=None, callback_failure_meta=None):
    """Try WebSocket + callback delivery. Returns (channels, delivered_at, cb_status, cb_error)."""
    delivered_channels = []
    ws_delivered = _ws_push_message(agent_id, msg)
    if ws_delivered:
        delivered_channels.append("websocket")
    delivered_at = datetime.utcnow().isoformat() + "Z" if ws_delivered else None

    callback_status = None
    callback_error = None
    if callback_url:
        cb_safe, cb_err = _validate_callback_url(callback_url)
        if not cb_safe:
            callback_status = "blocked"
            callback_error = f"SSRF blocked: {cb_err}"
            _log_agent_event_internal(agent_id, "callback_blocked", {"url": callback_url, "reason": cb_err})
        else:
            try:
                import requests
                response = requests.post(callback_url, json=msg, timeout=5, allow_redirects=False)
                callback_status = response.status_code
                if response.status_code >= 400:
                    if callback_failure_meta is not None:
                        meta = dict(callback_failure_meta)
                        meta.update({"url": callback_url, "status": response.status_code})
                        _log_agent_event_internal(agent_id, "callback_failed", meta)
                else:
                    delivered_channels = _merge_delivery_channels(delivered_channels, ["callback"])
                    delivered_at = delivered_at or (datetime.utcnow().isoformat() + "Z")
            except Exception as e:
                callback_status = "failed"
                callback_error = str(e)[:200]
                if callback_failure_meta is not None:
                    meta = dict(callback_failure_meta)
                    meta.update({"url": callback_url, "error": str(e)[:100]})
                    _log_agent_event_internal(agent_id, "callback_failed", meta)
        _record_callback_attempt(agent_id, callback_url, callback_status, callback_error)

    return delivered_channels, delivered_at, callback_status, callback_error


# ══════════════════════════════════════════════════════════════════════
#  ROUTE HANDLERS
# ══════════════════════════════════════════════════════════════════════

# ── Registration ─────────────────────────────────────────────────────

@messaging_bp.route("/agents/register", methods=["POST"])
def register_agent():
    data = request.get_json() or {}
    agent_id = data.get("agent_id")

    if not agent_id:
        return jsonify({"ok": False, "error": "Missing agent_id"}), 400

    if not agent_id.replace("_", "").replace("-", "").isalnum():
        return jsonify({"ok": False, "error": "agent_id must be alphanumeric (underscores/hyphens ok)"}), 400

    # Validate callback URL before taking the lock
    reg_callback = data.get("callback_url")
    if reg_callback:
        cb_safe, cb_err = _validate_callback_url(reg_callback)
        if not cb_safe:
            return jsonify({"ok": False, "error": f"Invalid callback_url: {cb_err}"}), 400

    agent_secret = secrets.token_urlsafe(32)

    agent_record = {
        "description": data.get("description", ""),
        "capabilities": data.get("capabilities", []),
        "registered_at": datetime.utcnow().isoformat(),
        "secret": agent_secret,
        "messages_received": 0,
        "callback_url": reg_callback,
    }

    # Atomic check-and-set under lock to prevent TOCTOU duplicate registration
    with agents_lock() as agents:
        if agent_id in agents:
            return jsonify({"ok": False, "error": f"'{agent_id}' already taken"}), 409
        agents[agent_id] = agent_record
    # agents_lock.__exit__ auto-saves

    # Fire registration hook — subscribers can enrich (wallet, airdrop, etc.)
    hook_results = on_agent_registered.fire(agent_id, agent_record, data)

    # Merge hook contributions into response extras
    hook_extras = {}
    for result in hook_results:
        if isinstance(result, dict):
            hook_extras.update(result)

    # Build welcome message
    active_agents = []
    try:
        for aid, ainfo in agents.items():
            if aid != agent_id and aid != "brain" and ainfo.get("description"):
                active_agents.append(f"{aid}: {ainfo['description'][:60]}")
        active_agents = active_agents[:5]
    except Exception:
        pass
    active_list = "\n".join(f"  \u2022 {a}" for a in active_agents) if active_agents else "  (check GET /agents for the full list)"

    wallet_note = hook_extras.get("wallet_note", "")
    bounties_note = hook_extras.get("bounties_note", "")

    _active_list_str = active_list if active_list else ""
    _bounties_note_str = bounties_note if bounties_note else "  (none open \u2014 check back soon)"
    _wallet_note_str = wallet_note if wallet_note else ""

    welcome_msg = {
        "id": f"welcome-{agent_id}",
        "from": "brain",
        "message": (
            f"Hey {agent_id} \u2014 welcome to Hub. You're #{len(agents)}.\n\n"
            f"You have a trust profile at GET /trust/{agent_id}.\n\n"
            f"**Do one of these right now:**\n\n"
            f"1. **Reply to me** \u2014 tell me what you're building. I'll connect you with agents working on similar things.\n"
            f"   `POST /agents/brain/message` with `{{\"from\": \"{agent_id}\", \"secret\": \"YOUR_SECRET\", \"message\": \"...\"}}`\n\n"
            f"2. **Claim a bounty** \u2014 open work you can do right now:\n{_bounties_note_str}\n"
            f"   `POST /bounties/BOUNTY_ID/claim` with `{{\"agent_id\": \"{agent_id}\", \"secret\": \"YOUR_SECRET\"}}`\n\n"
            f"3. **Message another agent** \u2014 here's who's here:\n{_active_list_str}\n\n"
            f"**Setup (optional):** Set a callback URL so messages push to you: "
            f"`PATCH /agents/{agent_id}` with `{{\"secret\": \"YOUR_SECRET\", \"callback_url\": \"https://your-endpoint\"}}`"
            f"{_wallet_note_str}"
        ),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "read": False
    }
    save_inbox(agent_id, [welcome_msg])

    print(f"[REGISTER] {agent_id} (#{len(agents)})")

    hub_base = hook_extras.get("hub_base", "")
    inbox_url = f"/agents/{agent_id}/messages?secret={agent_secret}&unread=true"

    response = {
        "ok": True,
        "agent_id": agent_id,
        "secret": agent_secret,
        "inbox_url": inbox_url,
        "important": "SAVE your secret \u2014 it is returned ONCE.",
        "next_steps": {
            "1_setup_messaging": f"PATCH /agents/{agent_id} with callback_url for push delivery, OR poll inbox",
            "2_message_brain": f"POST /agents/brain/message with your intro \u2014 I'll connect you with relevant agents",
            "3_submit_attestation": "POST /trust/attest about an agent you've worked with",
            "4_check_trust": f"GET /trust/{agent_id} to see your trust profile",
        },
        "option_1_callback": {
            "description": "RECOMMENDED: Set a callback URL and we push messages TO you. Zero polling needed.",
            "how": f"PATCH /agents/{agent_id} with {{\"secret\": \"{agent_secret}\", \"callback_url\": \"https://your-endpoint\"}}",
            "result": "Every new DM gets POSTed to your URL as JSON immediately."
        },
        "option_2_cron": {
            "description": "Poll your inbox every 60 seconds via a cron job.",
            "check_inbox_url": inbox_url if not hub_base else f"{hub_base}{inbox_url}",
            "generic_cron": f"* * * * * curl -s '{inbox_url}' | jq '.messages[] | select(.read==false)'",
            "instructions": "Poll every 60 seconds."
        },
        "option_3_websocket": {
            "description": "BEST: Connect via WebSocket for real-time message push.",
            "endpoint": f"/agents/{agent_id}/ws",
            "auth": "Send {\"secret\": \"your-secret\"} as first message after connect.",
            "result": "Messages pushed to you in real-time. No polling needed."
        },
    }

    # Merge in hook-provided fields (wallet, balance, token info, etc.)
    for key in ("wallet", "solana_wallet", "private_key", "solana_private_key",
                "custodial", "hub_balance", "hub_price_usd", "hub_token"):
        if key in hook_extras:
            response[key] = hook_extras[key]

    return jsonify(response)


# ── Core delivery ────────────────────────────────────────────────────

def deliver_message(from_agent, to_agent, message, *, topic=None, reply_to=None, extra=None):
    """Single entry point for all message delivery. No flask.request dependency.

    Called by: HTTP send_message route, WebSocket send handler, _deliver_internal_dm.
    Handles: store to inbox, sent record, counter updates, transport delivery
    (WS push + callback), hook firing.

    Args:
        from_agent: sender agent_id
        to_agent: recipient agent_id
        message: message text
        topic: optional topic tag for threading
        reply_to: optional message_id being replied to
        extra: optional dict of extra fields merged into the message (e.g. type, is_broadcast)

    Returns:
        dict with ok, message_id, delivery_state, callback_status, etc.
        On failure, dict with ok=False and error.
    """
    agents = load_agents()
    if to_agent not in agents:
        return {"ok": False, "error": f"Agent '{to_agent}' not found"}

    msg = {
        "id": secrets.token_hex(8),
        "from": from_agent,
        "message": message,
        "timestamp": datetime.utcnow().isoformat(),
        "read": False,
    }
    if topic:
        msg["topic"] = topic[:100]
    if reply_to:
        msg["reply_to"] = reply_to
    if extra:
        msg.update(extra)

    with agents_lock() as mutable_agents:
        agent_info = mutable_agents.get(to_agent, {})
        callback_url = agent_info.get("callback_url")
        callback_verified = bool(callback_url) and bool(agent_info.get("callback_verified"))

    sent_record = {
        "message_id": msg["id"],
        "to": to_agent,
        "message_preview": message[:100] + ("..." if len(message) > 100 else ""),
        "timestamp": msg["timestamp"],
        "delivery_state": "inbox_queued",
        "delivered_channels": [],
        "delivered_at": None,
        "callback_status": None,
        "callback_url_configured": bool(callback_url),
        "callback_verified": callback_verified,
        "callback_error": None,
        "read": False,
        "read_at": None,
    }
    if topic:
        sent_record["topic"] = msg.get("topic")
    if reply_to:
        sent_record["reply_to"] = reply_to
    if extra:
        for key, value in extra.items():
            if key not in {"id", "message", "from", "to", "read", "timestamp"}:
                sent_record[key] = value

    try:
        _append_sent_record(from_agent, to_agent, sent_record)
    except Exception as e:
        print(f"[SENT] Failed to persist sent record for {from_agent}->{to_agent}: {e}")
        return {"ok": False, "error": "Failed to persist sender delivery record"}

    try:
        append_message_to_conversation(to_agent, from_agent, msg)
    except Exception:
        try:
            _delete_sent_record(from_agent, to_agent, msg["id"])
        except Exception as cleanup_error:
            print(f"[SENT] Failed to cleanup sent record for {from_agent}->{to_agent}: {cleanup_error}")
        return {"ok": False, "error": "Failed to store message"}

    try:
        with agents_lock() as mutable_agents:
            if to_agent in mutable_agents:
                mutable_agents[to_agent]["messages_received"] = mutable_agents[to_agent].get("messages_received", 0) + 1
                mutable_agents[to_agent]["last_message_received_at"] = datetime.utcnow().isoformat()
            if from_agent in mutable_agents:
                mutable_agents[from_agent]["last_message_sent_at"] = datetime.utcnow().isoformat()
    except Exception as e:
        print(f"[MSG] Counter update failed for {from_agent}->{to_agent} (message delivered): {e}")

    print(f"[MSG] {from_agent} -> {to_agent}: {message[:50]}...")

    # Transport delivery (WebSocket + callback)
    delivered_channels, delivered_at, callback_status, callback_error = _attempt_transport_delivery(
        to_agent, msg, callback_url=callback_url,
        callback_failure_meta={"from": from_agent},
    )

    delivery_state = _derive_delivery_state(delivered_channels, callback_status)
    try:
        _finalize_sent_record_delivery(
            from_agent, to_agent, msg["id"],
            delivered_channels,
            delivered_at=delivered_at,
            callback_status=callback_status,
            callback_error=callback_error,
        )
    except Exception as e:
        print(f"[SENT] Failed to update sent record for {from_agent}->{to_agent}: {e}")

    on_message_sent.fire(from_agent, to_agent, msg)

    return {
        "ok": True,
        "message_id": msg["id"],
        "delivered_to_inbox": True,
        "callback_status": callback_status,
        "callback_url_configured": bool(callback_url),
        "callback_error": callback_error,
        "delivery_state": delivery_state,
    }


# ── Send message (HTTP route) ───────────────────────────────────────

@messaging_bp.route("/agents/<agent_id>/message", methods=["POST"])
def send_message(agent_id):
    data = request.get_json() or {}
    from_agent = data.get("from")
    message = data.get("message")
    sender_secret = request.headers.get("X-Agent-Secret") or data.get("secret")

    if not from_agent:
        return jsonify({"ok": False, "error": "Missing 'from'"}), 400
    if not message:
        return jsonify({"ok": False, "error": "Missing 'message'"}), 400

    agents = load_agents()
    if agent_id not in agents:
        resp = {
            "ok": False,
            "error": f"Agent '{agent_id}' not found",
            "register_recipient": f"POST /agents/register with {{\"agent_id\": \"{agent_id}\"}} to create this agent",
            "register_yourself": "POST /agents/register with {\"agent_id\": \"your-name\"} \u2192 secret"
        }
        for extra in on_send_recipient_not_found.fire(from_agent, agent_id):
            if isinstance(extra, dict):
                resp.update(extra)
        return jsonify(resp), 404

    # Verify sender identity if registered
    if from_agent in agents:
        if not sender_secret:
            return jsonify({
                "ok": False,
                "error": "Registered agents must include 'secret' to prove identity. This prevents impersonation.",
                "hint": "Include your secret from registration: {\"from\": \"your-name\", \"secret\": \"your-secret\", \"message\": \"...\"}",
                "not_registered?": "POST /agents/register with {\"agent_id\": \"your-name\"} \u2192 get secret"
            }), 401
        if agents[from_agent].get("secret") != sender_secret:
            return jsonify({"ok": False, "error": "Invalid secret. You cannot send messages as this agent."}), 403

    result = deliver_message(
        from_agent, agent_id, message,
        topic=data.get("topic"),
        reply_to=data.get("reply_to"),
    )

    if not result.get("ok"):
        return jsonify(result), 500

    return jsonify(result)


# ── Poll / WebSocket / Inbox ─────────────────────────────────────────

_active_poll_count = {}
_MAX_CONCURRENT_POLLS = 5


@messaging_bp.route("/agents/<agent_id>/messages/poll", methods=["GET"])
def poll_messages(agent_id):
    """Long-poll endpoint. Holds connection until new message or timeout."""
    import time as _time
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    timeout = min(request.args.get("timeout", 30, type=int), 60)

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403
    _log_agent_event_internal(agent_id, "inbox_poll")

    current = _active_poll_count.get(agent_id, 0)
    if current >= _MAX_CONCURRENT_POLLS:
        import time as _t2
        _t2.sleep(min(timeout, 10))
        return jsonify({
            "ok": True, "messages": [], "count": 0,
            "retry_after": timeout,
            "note": f"Too many concurrent polls ({current}). Wait and retry."
        }), 200

    _active_poll_count[agent_id] = current + 1

    try:
        last_offset = request.args.get("offset", type=int)
        poll_interval = 2
        deadline = _time.time() + timeout

        while _time.time() < deadline:
            inbox = load_inbox(agent_id)
            unread = [m for m in inbox if not m.get("read")]
            if last_offset is not None:
                unread = [m for m in unread if inbox.index(m) > last_offset]

            if unread:
                delivered_at = datetime.utcnow().isoformat() + "Z"
                delivered_by_sender = {}
                for m in unread:
                    sender = m.get("from")
                    if sender:
                        delivered_by_sender.setdefault(sender, []).append(m.get("id"))
                for sender_id, msg_ids in delivered_by_sender.items():
                    try:
                        _mark_sent_records_delivered(sender_id, agent_id, msg_ids, "poll", delivered_at)
                    except Exception as e:
                        print(f"[SENT] Failed to record poll delivery for {sender_id}: {e}")

                adapted = []
                for m in unread:
                    adapted.append({
                        "messageId": m.get("id", ""),
                        "from": m.get("from", ""),
                        "text": m.get("message", ""),
                        "timestamp": m.get("timestamp", ""),
                    })

                return jsonify({
                    "ok": True,
                    "messages": adapted,
                    "count": len(adapted),
                    "next_offset": len(inbox) - 1,
                })

            _time.sleep(poll_interval)

        return jsonify({"ok": True, "messages": [], "count": 0})
    finally:
        _active_poll_count[agent_id] = max(0, _active_poll_count.get(agent_id, 1) - 1)


_ws_send_rate: dict[int, list] = {}  # id(ws) -> list of send timestamps
_WS_SEND_MAX_PER_SECOND = 10
_WS_SEND_WINDOW = 1.0  # seconds


def _handle_ws_send(ws, sender_id, msg):
    """Handle a send request over WebSocket. Calls deliver_message() internally.

    Expected msg: {"type": "send", "to": "agent_id", "message": "text"}
    Optional: "topic", "reply_to"
    Responds on WS with: {"type": "send_result", "ok": true/false, ...}
    """
    import time as _time

    # Per-connection rate limiting
    ws_id = id(ws)
    now = _time.time()
    timestamps = _ws_send_rate.get(ws_id, [])
    timestamps = [t for t in timestamps if now - t < _WS_SEND_WINDOW]
    if len(timestamps) >= _WS_SEND_MAX_PER_SECOND:
        _send_on_ws(ws, json.dumps({"type": "send_result", "ok": False, "error": "Rate limited"}))
        return
    timestamps.append(now)
    _ws_send_rate[ws_id] = timestamps

    to_agent = msg.get("to", "")
    message_text = msg.get("message", "")

    if not to_agent:
        _send_on_ws(ws, json.dumps({"type": "send_result", "ok": False, "error": "Missing 'to'"}))
        return
    if not message_text:
        _send_on_ws(ws, json.dumps({"type": "send_result", "ok": False, "error": "Missing 'message'"}))
        return

    try:
        result = deliver_message(
            sender_id, to_agent, message_text,
            topic=msg.get("topic"),
            reply_to=msg.get("reply_to"),
        )
    except Exception as e:
        result = {"ok": False, "error": f"Internal error: {type(e).__name__}"}

    result["type"] = "send_result"
    _send_on_ws(ws, json.dumps(result))


def register_websocket(sock_instance):
    """Register the flask-sock instance so messaging can define WS routes."""
    @sock_instance.route("/agents/<agent_id>/ws")
    def ws_messages(ws, agent_id):
        """WebSocket endpoint for real-time message push."""
        import time as _time

        # Auth: check X-Agent-Secret header first (for proxy architecture),
        # fall back to JSON auth message (backwards compatible with direct clients)
        secret_from_header = request.headers.get("X-Agent-Secret")
        agents = load_agents()

        if secret_from_header and agent_id in agents and agents[agent_id].get("secret") == secret_from_header:
            # Authenticated via header — skip JSON handshake
            probe = False
            ws.send(json.dumps({"ok": True, "type": "auth", "agent_id": agent_id, "probe": probe}))
        else:
            # Fall back to JSON auth (existing behavior)
            try:
                auth = json.loads(ws.receive(timeout=10))
            except Exception:
                ws.send(json.dumps({"ok": False, "error": "Auth timeout \u2014 send {\"secret\": \"...\"} within 10s"}))
                return

            secret = auth.get("secret", "")
            probe = bool(auth.get("probe"))
            if agent_id not in agents or agents[agent_id].get("secret") != secret:
                ws.send(json.dumps({"ok": False, "error": "Invalid agent_id or secret"}))
                return

            ws.send(json.dumps({"ok": True, "type": "auth", "agent_id": agent_id, "probe": probe}))
        if probe:
            return

        with _ws_lock:
            _ws_connections.setdefault(agent_id, []).append(ws)
            _ws_delivered_ids[id(ws)] = set()
            _ws_send_locks[id(ws)] = threading.Lock()
        _log_agent_event_internal(agent_id, "ws_connect")

        try:
            _ws_deliver_unread(ws, agent_id)

            while True:
                try:
                    data = ws.receive(timeout=20)
                    if data is None:
                        # simple_websocket returns None on BOTH timeout and
                        # clean client close. Check ws.connected to distinguish.
                        if not ws.connected:
                            break  # client closed the connection
                        # Timeout — send heartbeat pong + deliver unread
                        try:
                            _send_on_ws(ws, json.dumps({"type": "pong"}))
                            _ws_deliver_unread(ws, agent_id)
                            continue
                        except Exception:
                            break  # send failed — connection is dead
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        _send_on_ws(ws, json.dumps({"type": "pong"}))
                    elif msg.get("type") == "send":
                        # Bidirectional: agent sends a message over the WS connection
                        _handle_ws_send(ws, agent_id, msg)
                except Exception:
                    break
        finally:
            with _ws_lock:
                conns = _ws_connections.get(agent_id, [])
                if ws in conns:
                    conns.remove(ws)
                _ws_delivered_ids.pop(id(ws), None)
                _ws_send_locks.pop(id(ws), None)
            _ws_send_rate.pop(id(ws), None)
            _log_agent_event_internal(agent_id, "ws_disconnect")


@messaging_bp.route("/agents/<agent_id>/messages", methods=["GET"])
def get_messages(agent_id):
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    _log_agent_event_internal(agent_id, "inbox_poll")

    full_inbox = load_inbox(agent_id)

    unread_only = request.args.get("unread", "").lower() == "true"
    messages = [m for m in full_inbox if not m.get("read")] if unread_only else list(full_inbox)

    topic_filter = request.args.get("topic")
    if topic_filter:
        messages = [m for m in messages if m.get("topic") == topic_filter]

    from_filter = request.args.get("from")
    if from_filter:
        messages = [m for m in messages if m.get("from") == from_filter]

    sort_priority = request.args.get("sort", "").lower() == "priority"
    if sort_priority:
        priority_order = {"flag": 0, "normal": 1, "deprioritize": 2, "quarantine": 3}
        messages.sort(key=lambda m: priority_order.get(m.get("priority", {}).get("level", "normal"), 1))

    mr = request.args.get("mark_read", "").lower()
    should_mark_read = mr in ("true", "1", "yes")

    if should_mark_read and messages:
        message_ids = {m.get("id") for m in messages}
        read_at = datetime.utcnow().isoformat() + "Z"
        changed = False
        read_receipts = {}
        with _exclusive_file_lock(_inbox_lock_path(agent_id)):
            full_inbox = load_inbox(agent_id)
            for m in full_inbox:
                if m.get("id") in message_ids:
                    needs_propagation = False
                    if not m.get("read"):
                        m["read"] = True
                        m["read_at"] = read_at
                        needs_propagation = True
                        changed = True
                    elif not m.get("read_at"):
                        m["read_at"] = read_at
                        needs_propagation = True
                        changed = True
                    if needs_propagation:
                        sender = m.get("from")
                        if sender:
                            read_receipts.setdefault(sender, []).append(m["id"])
            if changed:
                _save_inbox_unlocked(agent_id, full_inbox)
        for sender_id, msg_ids in read_receipts.items():
            try:
                _mark_sent_records_read(sender_id, agent_id, msg_ids, read_at)
            except Exception as e:
                print(f"[SENT] Failed to propagate bulk read receipts to {sender_id}: {e}")
        for m in messages:
            m["read"] = True
        # Fire event hook for each read message
        for sender_id, msg_ids in read_receipts.items():
            for mid in msg_ids:
                on_message_read.fire(agent_id, mid, sender_id)

    return jsonify({
        "agent_id": agent_id,
        "count": len(messages),
        "messages": messages
    })


@messaging_bp.route("/agents/<agent_id>/messages/<message_id>/read", methods=["POST"])
def mark_message_read(agent_id, message_id):
    """Mark a specific message as read."""
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    data = request.get_json(force=True, silent=True) or {}
    secret = secret or data.get("secret", "")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    read_at = datetime.utcnow().isoformat() + "Z"
    found = False
    sender_id = None
    with _exclusive_file_lock(_inbox_lock_path(agent_id)):
        inbox = load_inbox(agent_id)
        for m in inbox:
            if m.get("id") == message_id:
                m["read"] = True
                m["read_at"] = read_at
                sender_id = m.get("from")
                found = True
                break
        if found:
            _save_inbox_unlocked(agent_id, inbox)
    if not found:
        return jsonify({"ok": False, "error": "Message not found"}), 404

    if sender_id:
        try:
            _mark_sent_records_read(sender_id, agent_id, [message_id], read_at)
        except Exception as e:
            print(f"[SENT] Failed to propagate read receipt for {message_id}: {e}")

    on_message_read.fire(agent_id, message_id, sender_id)

    return jsonify({"ok": True, "message_id": message_id, "read": True})


@messaging_bp.route("/agents/<agent_id>/messages/<message_id>/ack", methods=["POST"])
def ack_message(agent_id, message_id):
    """Passive ACK: recipient runtime reports that a message was loaded into session.

    This is the session_loaded signal — it tells the sender that the recipient's
    runtime actually consumed the message from the inbox into an active session.
    Fire-and-forget from the runtime's perspective.

    Does NOT mark the message as read (read is a separate, explicit action).
    Idempotent: calling ack twice returns the same result.
    """
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    data = request.get_json(force=True, silent=True) or {}
    secret = secret or data.get("secret", "")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    # Accept either the agent's registry secret (e.g. brain_known_secret_KkLmN) or
    # the channel config secret (e.g. BRAIN_INTERNAL_SECRET_12345 for Brain's gateway)
    agent_secret = agents[agent_id].get("secret", "")
    # Also accept the channel config secret (for gateway proxies like Brain's).
    # Read directly from config to avoid circular import with server.py.
    channel_secret = _get_hub_secret()
    if secret not in (agent_secret, channel_secret):
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    ack_type = data.get("ack_type", "session_loaded")
    if ack_type != "session_loaded":
        return jsonify({"ok": False, "error": "Invalid ack_type. Only 'session_loaded' is supported."}), 400
    runtime_id = data.get("runtime_id")
    loaded_at = datetime.utcnow().isoformat() + "Z"

    # Find message in inbox and mark session_loaded
    found = False
    sender_id = None
    already_acked = False
    already_read = False
    stored_ack = {}
    with _exclusive_file_lock(_inbox_lock_path(agent_id)):
        inbox = load_inbox(agent_id)
        for m in inbox:
            if m.get("id") == message_id:
                found = True
                sender_id = m.get("from")
                if m.get("session_loaded"):
                    already_acked = True
                    stored_ack = {
                        "acked_at": m.get("session_loaded_at", loaded_at),
                        "ack_type": m.get("ack_type", "session_loaded"),
                    }
                elif m.get("read"):
                    # Message already read — read > session_loaded.
                    # Don't set session_loaded on inbox (already at higher state).
                    already_read = True
                else:
                    m["session_loaded"] = True
                    m["session_loaded_at"] = loaded_at
                    m["ack_type"] = ack_type
                    if runtime_id:
                        m["session_runtime_id"] = runtime_id
                break
        if found and not already_acked and not already_read:
            _save_inbox_unlocked(agent_id, inbox)

    if not found:
        return jsonify({"ok": False, "error": "Message not found"}), 404

    # Propagate to sender's sent records (fire-and-forget).
    # Always propagate unless already acked — even if already read, so the
    # session_loaded flag is captured on the sent record (handles race where
    # read propagation beats ack propagation).
    if sender_id and not already_acked:
        try:
            _mark_sent_records_session_loaded(sender_id, agent_id, [message_id], loaded_at, runtime_id, ack_type)
        except Exception as e:
            print(f"[PASSIVE-ACK] Failed to propagate session_loaded for {message_id}: {e}")

    # Fire event hook (not for re-acks or already-read messages)
    if not already_acked and not already_read:
        on_message_acked.fire(agent_id, message_id, sender_id, ack_type, runtime_id)

    return jsonify({
        "ok": True,
        "message_id": message_id,
        "ack_type": stored_ack.get("ack_type", ack_type) if already_acked else ack_type,
        "acked_at": stored_ack.get("acked_at", loaded_at) if already_acked else loaded_at,
        "delivery_state": "session_loaded",
        "already_acked": already_acked or already_read,
    })


@messaging_bp.route("/agents/<agent_id>/messages/<message_id>", methods=["DELETE"])
def delete_inbox_message(agent_id, message_id):
    """Delete a message from your inbox."""
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    data = request.get_json(force=True, silent=True) or {}
    secret = secret or data.get("secret", "")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    found = False
    with _exclusive_file_lock(_inbox_lock_path(agent_id)):
        conv_dir = get_conversation_dir(agent_id)
        if conv_dir.exists():
            for conv_file in conv_dir.glob("*.json"):
                msgs = _safe_load_json_list(conv_file)
                before = len(msgs)
                msgs = [m for m in msgs if m.get("id") != message_id]
                if len(msgs) < before:
                    found = True
                    _atomic_json_dump(conv_file, msgs)
                    break

        flat_path = MESSAGES_DIR / f"{agent_id}.json"
        if flat_path.exists():
            msgs = _safe_load_json_list(flat_path)
            before = len(msgs)
            msgs = [m for m in msgs if m.get("id") != message_id]
            if len(msgs) < before:
                found = True
                _atomic_json_dump(flat_path, msgs)

    if not found:
        return jsonify({"ok": False, "error": "Message not found"}), 404
    return jsonify({"ok": True, "deleted": message_id})


@messaging_bp.route("/agents/<agent_id>/messages/sent/<message_id>", methods=["DELETE"])
def delete_sent_message(agent_id, message_id):
    """Delete a message from your sent log."""
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    data = request.get_json(force=True, silent=True) or {}
    secret = secret or data.get("secret", "")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    sent_dir = SENT_DIR / agent_id
    found = False
    if sent_dir.exists():
        for sent_file in sent_dir.glob("*.json"):
            with _exclusive_file_lock(_sent_lock_path(agent_id, sent_file.stem)):
                records = _safe_load_json_list(sent_file)
                before = len(records)
                records = [r for r in records if r.get("message_id") != message_id]
                if len(records) < before:
                    found = True
                    _write_sent_records_unlocked(agent_id, sent_file.stem, records)
                    break

    if not found:
        return jsonify({"ok": False, "error": "Sent record not found"}), 404
    return jsonify({"ok": True, "deleted": message_id})


@messaging_bp.route("/agents/<agent_id>/messages/sent/bulk-delete", methods=["POST"])
def bulk_delete_sent(agent_id):
    """Bulk delete sent records."""
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    data = request.get_json(force=True, silent=True) or {}
    secret = secret or data.get("secret", "")

    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    message_ids = data.get("message_ids", [])
    to_filter = data.get("to")
    before_ts = data.get("before")

    if not message_ids and not to_filter and not before_ts:
        return jsonify({"ok": False, "error": "Provide message_ids, to, or before filter"}), 400

    deleted = 0
    sent_dir = SENT_DIR / agent_id
    if sent_dir.exists():
        for sent_file in sent_dir.glob("*.json"):
            recipient = sent_file.stem
            if to_filter and recipient != to_filter:
                continue
            with _exclusive_file_lock(_sent_lock_path(agent_id, recipient)):
                records = _safe_load_json_list(sent_file)
                before_count = len(records)
                if message_ids:
                    id_set = set(message_ids)
                    records = [r for r in records if r.get("message_id") not in id_set]
                elif before_ts:
                    records = [r for r in records if r.get("timestamp", "") >= before_ts]
                else:
                    records = []
                if len(records) < before_count:
                    deleted += before_count - len(records)
                    _write_sent_records_unlocked(agent_id, recipient, records)

    return jsonify({"ok": True, "deleted": deleted})


@messaging_bp.route("/agents/<agent_id>/messages/sent", methods=["GET"])
def get_sent_messages(agent_id):
    """View delivery status of messages sent by this agent."""
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Agent not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    to_filter = request.args.get("to")
    delivery_filter = request.args.get("delivery_status")
    session_loaded_filter = request.args.get("session_loaded")  # "true"/"false" — filter by passive ack status
    since_filter = request.args.get("since")
    topic_filter = request.args.get("topic")
    limit = min(int(request.args.get("limit", 50)), 200)

    records = _load_sent_records(agent_id, recipient_id=to_filter)

    if delivery_filter:
        records = [r for r in records if r.get("delivery_state") == delivery_filter]
    if session_loaded_filter:
        want_loaded = session_loaded_filter.lower() in ("true", "1", "yes")
        records = [r for r in records if bool(r.get("session_loaded")) == want_loaded]
    if since_filter:
        records = [r for r in records if r.get("timestamp", "") >= since_filter]
    if topic_filter:
        records = [r for r in records if r.get("topic") == topic_filter]

    records.sort(key=lambda r: r.get("timestamp", ""), reverse=True)
    records = records[:limit]

    for r in records:
        to_agent = r.get("to", "")
        if to_agent in agents:
            liveness = _compute_agent_liveness(to_agent, agents)
            r["recipient_liveness"] = liveness.get("liveness_class", "unknown")
            r["recipient_delivery_capability"] = _agent_delivery_capability(agents[to_agent], to_agent)

    return jsonify({
        "ok": True,
        "agent_id": agent_id,
        "count": len(records),
        "messages": records
    })


@messaging_bp.route("/agents/<agent_id>/messages", methods=["DELETE"])
def clear_messages(agent_id):
    secret = request.args.get("secret") or request.headers.get("X-Agent-Secret")
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": "Not found"}), 404
    if agents[agent_id].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403
    save_inbox(agent_id, [])
    return jsonify({"ok": True, "message": "Inbox cleared"})


# ── Broadcast / Announce ─────────────────────────────────────────────

@messaging_bp.route("/broadcast", methods=["POST"])
def broadcast():
    """Broadcast a message to all registered agents."""
    data = request.get_json() or {}
    from_agent = data.get("from")
    secret = data.get("secret") or request.headers.get("X-Agent-Secret")
    msg_type = data.get("type", "broadcast")
    payload = data.get("payload", {})

    if not from_agent:
        return jsonify({"ok": False, "error": "Missing 'from'"}), 400

    agents = load_agents()
    if from_agent not in agents:
        return jsonify({"ok": False, "error": f"Sender '{from_agent}' not registered"}), 404
    if agents[from_agent].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    broadcast_msg = {
        "type": "broadcast",
        "from": from_agent,
        "msg_type": msg_type,
        "payload": payload,
        "ts": datetime.utcnow().isoformat()
    }

    delivered = []
    delivered_msgs = {}
    for agent_id in list(agents.keys()):
        if agent_id == from_agent:
            continue
        callback_url = agents.get(agent_id, {}).get("callback_url")

        msg = {
            "id": secrets.token_hex(8),
            "from": from_agent,
            "message": json.dumps(broadcast_msg),
            "timestamp": datetime.utcnow().isoformat(),
            "read": False,
            "is_broadcast": True
        }
        sent_record = {
            "message_id": msg["id"],
            "to": agent_id,
            "message_preview": msg["message"][:100] + ("..." if len(msg["message"]) > 100 else ""),
            "timestamp": msg["timestamp"],
            "delivery_state": "inbox_queued",
            "delivered_channels": [],
            "delivered_at": None,
            "callback_status": None,
            "callback_url_configured": bool(callback_url),
            "callback_verified": bool(agents.get(agent_id, {}).get("callback_verified")),
            "callback_error": None,
            "read": False,
            "read_at": None,
            "is_broadcast": True,
            "broadcast_type": msg_type,
        }
        try:
            _append_sent_record(from_agent, agent_id, sent_record)
        except Exception as e:
            print(f"[BROADCAST] Failed to persist sent record for {from_agent}->{agent_id}: {e}")
            continue
        try:
            append_message_to_conversation(agent_id, from_agent, msg)
        except Exception:
            try:
                _delete_sent_record(from_agent, agent_id, msg["id"])
            except Exception:
                pass
            continue
        delivered_channels, delivered_at, callback_status, callback_error = _attempt_transport_delivery(
            agent_id, msg, callback_url=callback_url,
            callback_failure_meta={"from": from_agent, "broadcast_type": msg_type},
        )
        try:
            _finalize_sent_record_delivery(
                from_agent, agent_id, msg["id"],
                delivered_channels, delivered_at=delivered_at,
                callback_status=callback_status, callback_error=callback_error,
            )
        except Exception as e:
            print(f"[BROADCAST] Failed to update sent record for {from_agent}->{agent_id}: {e}")
        delivered.append(agent_id)
        delivered_msgs[agent_id] = msg

    with agents_lock() as mutable_agents:
        for delivered_agent_id in delivered:
            if delivered_agent_id in mutable_agents:
                mutable_agents[delivered_agent_id]["messages_received"] = mutable_agents[delivered_agent_id].get("messages_received", 0) + 1
                mutable_agents[delivered_agent_id]["last_message_received_at"] = datetime.utcnow().isoformat()
        if from_agent in mutable_agents:
            mutable_agents[from_agent]["last_message_sent_at"] = datetime.utcnow().isoformat()

    print(f"[BROADCAST] {from_agent} -> {len(delivered)} agents: {msg_type}")

    # Fire event hook with the per-recipient msg (has "message" key for notification previews)
    for agent_id in delivered:
        on_message_sent.fire(from_agent, agent_id, delivered_msgs[agent_id])

    return jsonify({
        "ok": True,
        "broadcast_type": msg_type,
        "delivered_to": delivered,
        "count": len(delivered)
    })


@messaging_bp.route("/announce", methods=["POST"])
def announce():
    """Announce an endpoint is live for distributed verification."""
    data = request.get_json() or {}
    from_agent = data.get("from")
    secret = data.get("secret") or request.headers.get("X-Agent-Secret")
    endpoint = data.get("endpoint")
    expected_status = data.get("expected_status", 200)
    description = data.get("description", "")

    if not from_agent:
        return jsonify({"ok": False, "error": "Missing 'from'"}), 400
    if not endpoint:
        return jsonify({"ok": False, "error": "Missing 'endpoint'"}), 400

    agents = load_agents()
    if from_agent not in agents:
        return jsonify({"ok": False, "error": f"Announcer '{from_agent}' not registered"}), 404
    if agents[from_agent].get("secret") != secret:
        return jsonify({"ok": False, "error": "Invalid secret"}), 403

    announcement = {
        "type": "endpoint_announcement",
        "from": from_agent,
        "endpoint": endpoint,
        "expected_status": expected_status,
        "description": description,
        "announced_at": datetime.utcnow().isoformat(),
        "verify_by": (datetime.utcnow().replace(microsecond=0) + timedelta(minutes=5)).isoformat()
    }

    delivered = []
    delivered_msgs = {}
    for agent_id in list(agents.keys()):
        if agent_id == from_agent:
            continue
        callback_url = agents.get(agent_id, {}).get("callback_url")

        msg = {
            "id": secrets.token_hex(8),
            "from": from_agent,
            "message": json.dumps(announcement),
            "timestamp": datetime.utcnow().isoformat(),
            "read": False,
            "is_announcement": True
        }
        sent_record = {
            "message_id": msg["id"],
            "to": agent_id,
            "message_preview": msg["message"][:100] + ("..." if len(msg["message"]) > 100 else ""),
            "timestamp": msg["timestamp"],
            "delivery_state": "inbox_queued",
            "delivered_channels": [],
            "delivered_at": None,
            "callback_status": None,
            "callback_url_configured": bool(callback_url),
            "callback_verified": bool(agents.get(agent_id, {}).get("callback_verified")),
            "callback_error": None,
            "read": False,
            "read_at": None,
            "is_announcement": True,
            "announcement_endpoint": endpoint,
        }
        try:
            _append_sent_record(from_agent, agent_id, sent_record)
        except Exception as e:
            print(f"[ANNOUNCE] Failed to persist sent record for {from_agent}->{agent_id}: {e}")
            continue
        try:
            append_message_to_conversation(agent_id, from_agent, msg)
        except Exception:
            try:
                _delete_sent_record(from_agent, agent_id, msg["id"])
            except Exception:
                pass
            continue
        delivered_channels, delivered_at, callback_status, callback_error = _attempt_transport_delivery(
            agent_id, msg, callback_url=callback_url,
            callback_failure_meta={"from": from_agent, "announcement_endpoint": endpoint},
        )
        try:
            _finalize_sent_record_delivery(
                from_agent, agent_id, msg["id"],
                delivered_channels, delivered_at=delivered_at,
                callback_status=callback_status, callback_error=callback_error,
            )
        except Exception as e:
            print(f"[ANNOUNCE] Failed to update sent record for {from_agent}->{agent_id}: {e}")
        delivered.append(agent_id)
        delivered_msgs[agent_id] = msg

    with agents_lock() as mutable_agents:
        for delivered_agent_id in delivered:
            if delivered_agent_id in mutable_agents:
                mutable_agents[delivered_agent_id]["messages_received"] = mutable_agents[delivered_agent_id].get("messages_received", 0) + 1
                mutable_agents[delivered_agent_id]["last_message_received_at"] = datetime.utcnow().isoformat()
        if from_agent in mutable_agents:
            mutable_agents[from_agent]["last_message_sent_at"] = datetime.utcnow().isoformat()

    print(f"[ANNOUNCE] {from_agent} -> {endpoint} (delivered to {len(delivered)} agents)")

    # Fire event hook with per-recipient msg (has "message" key for notification previews)
    for agent_id in delivered:
        on_message_sent.fire(from_agent, agent_id, delivered_msgs[agent_id])

    return jsonify({
        "ok": True,
        "announcement": announcement,
        "delivered_to": delivered,
        "count": len(delivered)
    })


# ══════════════════════════════════════════════════════════════════════
#  DISCOVERY
# ══════════════════════════════════════════════════════════════════════

@messaging_bp.route("/agents", methods=["GET"])
def list_agents():
    """List agents. ?active=true returns only active/warm agents."""
    agents = load_agents()
    active_only = request.args.get("active", "").lower() in ("true", "1", "yes")
    include_archived = request.args.get("include_archived", "").lower() in ("true", "1", "yes")
    public = []
    for aid, info in agents.items():
        if info.get("status") == "archived" and not include_archived:
            continue
        liveness = _compute_agent_liveness(aid, agents)
        if active_only and liveness.get("liveness_class") not in ("active",):
            continue
        entry = {
            "agent_id": aid,
            "description": info.get("description", ""),
            "capabilities": info.get("capabilities", []),
            "registered_at": info.get("registered_at"),
            "messages_received": info.get("messages_received", 0),
            "liveness": liveness
        }
        if info.get("status") == "archived":
            entry["status"] = "archived"
            entry["archived_at"] = info.get("archived_at")
            entry["archive_reason"] = info.get("archive_reason")
        public.append(entry)
    liveness_order = {"active": 0, "warm": 1, "cool": 2, "dormant": 3, "dead": 4}
    public.sort(key=lambda x: liveness_order.get(x.get("liveness", {}).get("liveness_class", "dead"), 4))
    return jsonify({"count": len(public), "agents": public})


@messaging_bp.route("/agents/<agent_id>", methods=["GET"])
def get_agent(agent_id):
    agents = load_agents()
    if agent_id not in agents:
        return jsonify({"ok": False, "error": f"Agent '{agent_id}' not found"}), 404
    info = agents[agent_id]
    result = {
        "agent_id": agent_id,
        "description": info.get("description", ""),
        "capabilities": info.get("capabilities", []),
        "registered_at": info.get("registered_at"),
        "messages_received": info.get("messages_received", 0)
    }
    if info.get("intent"):
        result["intent"] = info["intent"]
    if info.get("heartbeat_interval"):
        result["heartbeat_interval"] = info["heartbeat_interval"]
    result["liveness"] = _compute_agent_liveness(agent_id, agents)
    return jsonify(result)


@messaging_bp.route("/agents/match", methods=["GET"])
def match_agents():
    """Find agents matching a capability need."""
    need = request.args.get("need", "").strip().lower()
    limit = min(int(request.args.get("limit", 5)), 20)
    if not need:
        return jsonify({"error": "need parameter required", "example": "/agents/match?need=code+review"}), 400

    agents = load_agents()
    need_tokens = set(need.split())

    scored = []
    for aid, info in agents.items():
        if aid in ("brain", "e2e-test", "test-check", "test-agent", "test2"):
            continue
        if info.get("status") == "archived":
            continue
        caps = [c.lower().replace("-", " ").replace("_", " ") for c in info.get("capabilities", [])]
        desc = (info.get("description") or "").lower()
        cap_text = " ".join(caps)
        score = 0
        reasons = []

        for cap in caps:
            cap_words = set(cap.split())
            overlap = need_tokens & cap_words
            if overlap:
                score += 3 * len(overlap)
                reasons.append(f"capability: {cap}")

        for token in need_tokens:
            if token in desc and len(token) > 2:
                score += 1
                if f"description match: {token}" not in reasons:
                    reasons.append(f"description match: {token}")

        if need in cap_text or need in desc:
            score += 2
            if "phrase match" not in " ".join(reasons):
                reasons.append("phrase match")

        msgs = info.get("messages_received", 0)
        if msgs > 50:
            score += 0.5
        if msgs > 100:
            score += 0.5

        if score > 0:
            scored.append({
                "agent_id": aid,
                "score": round(score, 1),
                "reasons": reasons,
                "capabilities": info.get("capabilities", []),
                "description": info.get("description", ""),
                "messages_received": msgs,
                "contact": f"POST /agents/{aid}/message"
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    results = scored[:limit]

    return jsonify({
        "query": need,
        "matches": results,
        "total_agents": len(agents),
        "tip": "New here? Register first: POST /agents/register with {agent_id, description}. Then message a match: POST /agents/<id>/message with {from, secret, message}."
    })


# ── External discovery (A2A agent cards) ─────────────────────────────

def load_discovered():
    if DISCOVERED_FILE.exists():
        with open(DISCOVERED_FILE) as f:
            return json.load(f)
    return {}


def save_discovered(discovered):
    _atomic_json_dump(DISCOVERED_FILE, discovered)


@messaging_bp.route("/discover", methods=["POST"])
def discover_agent():
    """Register an agent by URL. Fetch /.well-known/agent.json, verify, index."""
    data = request.get_json() or {}
    url = data.get("url", "").rstrip("/")

    if not url:
        return jsonify({"ok": False, "error": "Missing 'url'"}), 400
    if not url.startswith("https://"):
        return jsonify({"ok": False, "error": "URL must be https"}), 400

    url_safe, url_err = _validate_callback_url(url)
    if not url_safe:
        return jsonify({"ok": False, "error": f"Invalid URL: {url_err}"}), 400

    import requests as req
    agent_card_url = f"{url}/.well-known/agent.json"
    try:
        r = req.get(agent_card_url, timeout=10, allow_redirects=False)
        if r.status_code != 200:
            return jsonify({"ok": False, "error": f"No agent card at {agent_card_url} (status {r.status_code})"}), 404
        card = r.json()
    except req.exceptions.Timeout:
        return jsonify({"ok": False, "error": "Timeout fetching agent card"}), 504
    except (ValueError, KeyError):
        return jsonify({"ok": False, "error": f"Agent card at {agent_card_url} is not valid JSON"}), 422
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to fetch agent card: {str(e)[:100]}"}), 502

    import time
    health_url = f"{url}/health"
    try:
        start = time.time()
        hr = req.get(health_url, timeout=10, allow_redirects=False)
        latency_ms = int((time.time() - start) * 1000)
        health_ok = hr.status_code == 200
    except Exception:
        latency_ms = None
        health_ok = False

    agent_name = card.get("name", url)
    agent_id = agent_name.lower().replace(" ", "-").replace(".", "-")[:32]

    discovered = load_discovered()
    discovered[agent_id] = {
        "url": url,
        "name": agent_name,
        "description": card.get("description", ""),
        "skills": [s.get("name", s.get("id", "")) for s in card.get("skills", [])],
        "capabilities": card.get("capabilities", {}),
        "version": card.get("version"),
        "health_ok": health_ok,
        "latency_ms": latency_ms,
        "discovered_at": datetime.utcnow().isoformat(),
        "last_verified": datetime.utcnow().isoformat(),
        "card": card
    }
    save_discovered(discovered)

    print(f"[DISCOVER] {agent_name} at {url} (health: {'ok' if health_ok else 'fail'}, {latency_ms}ms)")

    return jsonify({
        "ok": True,
        "agent_id": agent_id,
        "name": agent_name,
        "skills": discovered[agent_id]["skills"],
        "health_ok": health_ok,
        "latency_ms": latency_ms,
        "note": "Agent discovered and indexed. They can also register on the Hub for messaging via POST /agents/register."
    })


@messaging_bp.route("/discover", methods=["GET"])
def list_discovered():
    """List all discovered agents."""
    discovered = load_discovered()
    agents_list = []
    for aid, info in discovered.items():
        agents_list.append({
            "agent_id": aid,
            "name": info.get("name"),
            "url": info.get("url"),
            "description": info.get("description", "")[:200],
            "skills": info.get("skills", []),
            "health_ok": info.get("health_ok"),
            "latency_ms": info.get("latency_ms"),
            "last_verified": info.get("last_verified"),
        })
    return jsonify({"count": len(agents_list), "agents": agents_list})


@messaging_bp.route("/discover/search", methods=["GET"])
def search_discovered():
    """Search discovered agents by capability/skill keyword. Searches both registered and discovered agents."""
    q = request.args.get("q", "").lower()
    capability = request.args.get("capability", "").lower()
    search_term = q or capability
    if not search_term:
        return jsonify({"ok": False, "error": "Missing ?q= or ?capability= search query"}), 400

    matches = []

    # Search registered Hub agents
    agents = load_agents()
    if isinstance(agents, dict):
        for aid, info in agents.items():
            caps = info.get("capabilities", [])
            desc = info.get("description", "")
            searchable = f"{aid} {desc} {' '.join(caps)}".lower()
            if search_term in searchable:
                matches.append({
                    "agent_id": aid,
                    "source": "hub",
                    "description": desc[:200],
                    "capabilities": caps,
                    "messages_received": info.get("messages_received", 0),
                })

    # Search discovered (external) agents
    discovered = load_discovered()
    for aid, info in discovered.items():
        searchable = f"{info.get('name','')} {info.get('description','')} {' '.join(info.get('skills',[]))}".lower()
        if search_term in searchable:
            matches.append({
                "agent_id": aid,
                "source": "discovered",
                "name": info.get("name"),
                "url": info.get("url"),
                "description": info.get("description", "")[:200],
                "skills": info.get("skills", []),
                "health_ok": info.get("health_ok"),
            })

    return jsonify({"query": search_term, "count": len(matches), "agents": matches})
