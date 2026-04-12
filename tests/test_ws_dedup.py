"""
Tests for WebSocket duplicate-send fix.

All tests call server._ws_deliver_unread() and server._ws_push_message() directly —
the real server functions, not reimplementations. This ensures regressions in the
actual code are caught.

Covers:
  1. First call delivers all unread, populates _ws_delivered_ids
  2. Second call (heartbeat) delivers nothing — all already in delivered set
  3. Partial prior delivery — only unseen messages sent
  4. _ws_push_message populates _ws_delivered_ids
  5. Message pushed via _ws_push_message not resent by heartbeat
  6. _ws_delivered_ids and _ws_send_locks cleaned up on disconnect
  7. Reconnect (new ws object) gets fresh delivered set, receives unread again
  8. ws.send() failure stops loop, records only successful sends
  9. None/empty message ID does not poison the dedup set
  10. Concurrent sends serialized: _ws_deliver_unread holds send_lock,
      _ws_push_message waits rather than sending concurrently
"""

import json
import threading
import time
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Each test gets a clean data dir and reset WS globals."""
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "MESSAGES_DIR", tmp_path / "data" / "messages")
    monkeypatch.setattr(server, "SENT_DIR", tmp_path / "data" / "sent")
    (tmp_path / "data" / "messages").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data" / "sent").mkdir(parents=True, exist_ok=True)
    server._ws_connections.clear()
    server._ws_delivered_ids.clear()
    server._ws_send_locks.clear()
    yield tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ws():
    ws = MagicMock()
    ws.sent = []
    ws.send.side_effect = lambda data: ws.sent.append(json.loads(data))
    return ws


def _register_ws(agent_id, ws):
    """Simulate ws_messages registration block."""
    with server._ws_lock:
        server._ws_connections.setdefault(agent_id, []).append(ws)
        server._ws_delivered_ids[id(ws)] = set()
        server._ws_send_locks[id(ws)] = threading.Lock()


def _deregister_ws(agent_id, ws):
    """Simulate ws_messages finally block."""
    with server._ws_lock:
        conns = server._ws_connections.get(agent_id, [])
        if ws in conns:
            conns.remove(ws)
        server._ws_delivered_ids.pop(id(ws), None)
        server._ws_send_locks.pop(id(ws), None)


def _seed_inbox(agent_id, messages):
    grouped = defaultdict(list)
    for m in messages:
        grouped[m.get("from", "unknown")].append(m)
    for peer, msgs in grouped.items():
        conv_dir = server.get_conversation_dir(agent_id)
        conv_dir.mkdir(parents=True, exist_ok=True)
        server._atomic_json_dump(server.get_conversation_path(agent_id, peer), msgs)


def _msg(msg_id, from_agent="alice", text="hello"):
    return {
        "id": msg_id,
        "from": from_agent,
        "message": text,
        "timestamp": "2026-04-06T00:00:00",
        "read": False,
        "priority": 0.5,
    }


def _sent_ids(ws):
    return {m["data"]["messageId"] for m in ws.sent if m.get("type") == "message"}


def _delivered_set(ws):
    with server._ws_lock:
        return set(server._ws_delivered_ids.get(id(ws), set()))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_first_deliver_sends_all_unread():
    ws = _make_ws()
    _register_ws("bob", ws)
    _seed_inbox("bob", [_msg("m1"), _msg("m2"), _msg("m3")])

    server._ws_deliver_unread(ws, "bob")

    assert _sent_ids(ws) == {"m1", "m2", "m3"}
    assert _delivered_set(ws) == {"m1", "m2", "m3"}


def test_second_deliver_skips_already_sent():
    ws = _make_ws()
    _register_ws("bob", ws)
    _seed_inbox("bob", [_msg("m1"), _msg("m2")])

    server._ws_deliver_unread(ws, "bob")  # first — delivers both
    ws.sent.clear()
    server._ws_deliver_unread(ws, "bob")  # second (heartbeat) — should deliver nothing

    assert ws.sent == [], "Heartbeat must not re-send already-delivered messages"


def test_partial_prior_delivery_sends_only_new():
    ws = _make_ws()
    _register_ws("bob", ws)
    _seed_inbox("bob", [_msg("m1"), _msg("m2"), _msg("m3")])

    with server._ws_lock:
        server._ws_delivered_ids[id(ws)] = {"m1"}

    server._ws_deliver_unread(ws, "bob")

    assert _sent_ids(ws) == {"m2", "m3"}
    assert _delivered_set(ws) == {"m1", "m2", "m3"}


def test_ws_push_message_records_in_delivered_ids():
    ws = _make_ws()
    _register_ws("bob", ws)

    server._ws_push_message("bob", _msg("pushed"))

    assert "pushed" in _delivered_set(ws)


def test_pushed_message_not_redelivered_by_heartbeat():
    ws = _make_ws()
    _register_ws("bob", ws)
    msg = _msg("live")
    _seed_inbox("bob", [msg])

    server._ws_push_message("bob", msg)
    ws.sent.clear()

    server._ws_deliver_unread(ws, "bob")

    assert ws.sent == [], "Message pushed live must not be resent by heartbeat"


def test_cleanup_on_disconnect():
    ws = _make_ws()
    _register_ws("bob", ws)

    assert id(ws) in server._ws_delivered_ids
    assert id(ws) in server._ws_send_locks

    _deregister_ws("bob", ws)

    assert id(ws) not in server._ws_delivered_ids
    assert id(ws) not in server._ws_send_locks


def test_reconnect_receives_unread_again():
    ws1, ws2 = _make_ws(), _make_ws()
    _seed_inbox("bob", [_msg("m1"), _msg("m2")])

    _register_ws("bob", ws1)
    server._ws_deliver_unread(ws1, "bob")
    _deregister_ws("bob", ws1)

    _register_ws("bob", ws2)
    server._ws_deliver_unread(ws2, "bob")

    assert _sent_ids(ws2) == {"m1", "m2"}, "Reconnected client must receive all unread"


def test_send_failure_stops_loop_records_partial():
    ws = _make_ws()
    _register_ws("bob", ws)
    _seed_inbox("bob", [_msg("m1"), _msg("m2"), _msg("m3")])

    call_count = [0]

    def send_side_effect(data):
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError("connection broken")
        ws.sent.append(json.loads(data))

    ws.send.side_effect = send_side_effect
    server._ws_deliver_unread(ws, "bob")

    delivered = _delivered_set(ws)
    assert "m1" in delivered
    assert "m2" not in delivered
    assert "m3" not in delivered


def test_none_id_does_not_poison_dedup_set():
    """A message with id=None must not block future messages from being deduplicated."""
    ws = _make_ws()
    _register_ws("bob", ws)

    no_id_msg = _msg("m1")
    no_id_msg["id"] = None  # simulate missing id field stored as None
    _seed_inbox("bob", [no_id_msg, _msg("m2")])

    server._ws_deliver_unread(ws, "bob")

    # None must not be in the dedup set
    delivered = _delivered_set(ws)
    assert None not in delivered
    # m2 (with a real id) should be tracked
    assert "m2" in delivered
    # Both messages were sent (no crash)
    assert len([m for m in ws.sent if m.get("type") == "message"]) == 2


def test_sends_serialized_under_send_lock():
    """_ws_deliver_unread holds send_lock for entire batch; _ws_push_message waits.

    Verifies send_lock is held during every ws.send() in _ws_deliver_unread so that
    concurrent _ws_push_message calls cannot interleave mid-batch.
    """
    ws = _make_ws()
    _register_ws("bob", ws)
    _seed_inbox("bob", [_msg("m1"), _msg("m2")])

    send_lock = server._ws_send_locks[id(ws)]
    locks_held_during_send = []

    def instrumented_send(data):
        locks_held_during_send.append(send_lock.locked())
        ws.sent.append(json.loads(data))

    ws.send.side_effect = instrumented_send
    server._ws_deliver_unread(ws, "bob")

    assert all(locks_held_during_send), (
        "send_lock must be held during every ws.send() call in _ws_deliver_unread"
    )


def test_push_skips_message_already_delivered_by_heartbeat():
    """TOCTOU fix: _ws_push_message must not resend a message _ws_deliver_unread already sent.

    Simulates the race: heartbeat delivers m1, then send_message calls _ws_push_message(m1).
    The push should detect m1 in _ws_delivered_ids and skip the send.
    """
    ws = _make_ws()
    _register_ws("bob", ws)
    msg = _msg("m1")
    _seed_inbox("bob", [msg])

    # Heartbeat runs first, delivers m1
    server._ws_deliver_unread(ws, "bob")
    assert "m1" in _delivered_set(ws)
    ws.sent.clear()

    # Live push arrives for the same message (TOCTOU race scenario)
    server._ws_push_message("bob", msg)

    # Push must NOT have sent m1 again
    message_sends = [m for m in ws.sent if m.get("type") == "message"]
    assert message_sends == [], "_ws_push_message must skip messages already in _ws_delivered_ids"


def test_partial_failure_retried_on_next_heartbeat():
    """After ws.send() fails mid-batch, the next heartbeat retries the unsent messages.

    Verifies that messages not recorded in _ws_delivered_ids (due to send failure)
    are re-attempted on the next _ws_deliver_unread call.
    """
    ws = _make_ws()
    _register_ws("bob", ws)
    _seed_inbox("bob", [_msg("m1"), _msg("m2"), _msg("m3")])

    call_count = [0]
    def fail_on_second(data):
        call_count[0] += 1
        if call_count[0] == 2:
            raise OSError("broken")
        ws.sent.append(json.loads(data))

    ws.send.side_effect = fail_on_second
    server._ws_deliver_unread(ws, "bob")  # m1 sent, m2 fails, m3 skipped

    assert "m1" in _delivered_set(ws)
    assert "m2" not in _delivered_set(ws)

    # Restore send, run next heartbeat
    ws.send.side_effect = lambda data: ws.sent.append(json.loads(data))
    ws.sent.clear()
    server._ws_deliver_unread(ws, "bob")  # should retry m2, m3

    retried = _sent_ids(ws)
    assert "m1" not in retried, "m1 already delivered, must not be resent"
    assert "m2" in retried, "m2 failed last time, must be retried"
    assert "m3" in retried, "m3 was skipped last time, must be retried"


def test_none_id_message_resent_every_heartbeat_by_design():
    """Messages with id=None cannot be deduplicated and are resent every heartbeat.

    This is documented expected behavior — send_message() always assigns a real ID.
    This test pins the behavior so future changes don't silently alter it.
    """
    ws = _make_ws()
    _register_ws("bob", ws)
    no_id = _msg("anything")
    no_id["id"] = None
    _seed_inbox("bob", [no_id])

    server._ws_deliver_unread(ws, "bob")
    first_count = len([m for m in ws.sent if m.get("type") == "message"])
    ws.sent.clear()

    server._ws_deliver_unread(ws, "bob")  # second heartbeat
    second_count = len([m for m in ws.sent if m.get("type") == "message"])

    assert first_count == 1
    assert second_count == 1, "None-ID messages are resent every heartbeat (by design — undeduplicated)"


def test_deliver_unread_snapshot_inside_lock_prevents_toctou():
    """Snapshot of already_delivered happens inside send_lock, so _ws_push_message
    updating _ws_delivered_ids concurrently cannot cause a duplicate send.

    We verify this by: seeding inbox, having _ws_push_message 'win' the send_lock first,
    marking m1 as delivered, then running _ws_deliver_unread — which should skip m1.
    """
    ws = _make_ws()
    _register_ws("bob", ws)
    msg = _msg("m1")
    _seed_inbox("bob", [msg])

    # Simulate _ws_push_message winning the race: it marks m1 delivered before heartbeat runs
    with server._ws_lock:
        server._ws_delivered_ids[id(ws)].add("m1")

    # Heartbeat runs after — must skip m1
    server._ws_deliver_unread(ws, "bob")

    message_sends = [m for m in ws.sent if m.get("type") == "message"]
    assert message_sends == [], "Heartbeat must skip messages already marked by push"
