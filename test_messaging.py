"""
Tests for the extracted messaging module.

Covers: EventHook, agent registration, message send/receive,
inbox management, delivery tracking, liveness, and discovery.
Runs against a temp data directory — no live Hub needed.
"""

import json
import os
import sys
import tempfile
import shutil
from pathlib import Path

import pytest

# Ensure hub package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from hub.events import EventHook


# ══════════════════════════════════════════════════════════════════════
#  EventHook Tests
# ══════════════════════════════════════════════════════════════════════

class TestEventHook:
    def test_subscribe_and_fire(self):
        hook = EventHook()
        results = []
        hook.subscribe(lambda x: results.append(x))
        hook.fire("hello")
        assert results == ["hello"]

    def test_multiple_subscribers(self):
        hook = EventHook()
        log = []
        hook.subscribe(lambda x: log.append(f"a:{x}"))
        hook.subscribe(lambda x: log.append(f"b:{x}"))
        hook.fire("test")
        assert log == ["a:test", "b:test"]

    def test_fire_returns_non_none_results(self):
        hook = EventHook()
        hook.subscribe(lambda: "result1")
        hook.subscribe(lambda: None)
        hook.subscribe(lambda: {"key": "val"})
        results = hook.fire()
        assert results == ["result1", {"key": "val"}]

    def test_exception_doesnt_block_other_subscribers(self, capsys):
        hook = EventHook()
        log = []
        hook.subscribe(lambda: log.append("first"))

        def bad():
            raise ValueError("boom")

        hook.subscribe(bad)
        hook.subscribe(lambda: log.append("third"))
        hook.fire()
        assert log == ["first", "third"]
        captured = capsys.readouterr()
        assert "bad failed" in captured.out

    def test_clear(self):
        hook = EventHook()
        hook.subscribe(lambda: None)
        hook.subscribe(lambda: None)
        assert len(hook) == 2
        hook.clear()
        assert len(hook) == 0

    def test_decorator_pattern(self):
        hook = EventHook()

        @hook.subscribe
        def handler(x):
            return x * 2

        results = hook.fire(5)
        assert results == [10]


# ══════════════════════════════════════════════════════════════════════
#  Messaging Module Tests
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture
def msg_app():
    """Create a Flask app with messaging Blueprint and temp data dir."""
    from flask import Flask
    from flask_sock import Sock
    from hub.messaging import (
        messaging_bp, init_messaging, register_websocket,
        on_message_sent, on_agent_registered, on_message_read, on_message_acked, on_agent_event,
    )

    tmpdir = tempfile.mkdtemp(prefix="hub-test-")

    app = Flask(__name__)
    app.config["TESTING"] = True
    sock = Sock(app)

    # Clear any previous subscribers from other tests
    on_message_sent.clear()
    on_agent_registered.clear()
    on_message_read.clear()
    on_message_acked.clear()
    on_agent_event.clear()

    init_messaging(Path(tmpdir))
    app.register_blueprint(messaging_bp)
    register_websocket(sock)

    yield app, tmpdir

    # Cleanup
    on_message_sent.clear()
    on_agent_registered.clear()
    on_message_read.clear()
    on_message_acked.clear()
    on_agent_event.clear()
    shutil.rmtree(tmpdir, ignore_errors=True)


class TestRegistration:
    def test_register_agent(self, msg_app):
        app, tmpdir = msg_app
        with app.test_client() as c:
            resp = c.post("/agents/register", json={
                "agent_id": "test-agent",
                "description": "A test agent",
                "capabilities": ["testing"]
            })
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["ok"] is True
            assert data["agent_id"] == "test-agent"
            assert "secret" in data
            assert data["important"] == "SAVE your secret \u2014 it is returned ONCE."
            # Rich response includes setup instructions
            assert "option_1_callback" in data
            assert "option_2_cron" in data
            assert "option_3_websocket" in data

    def test_register_duplicate(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            c.post("/agents/register", json={"agent_id": "dupe"})
            resp = c.post("/agents/register", json={"agent_id": "dupe"})
            assert resp.status_code == 409

    def test_register_invalid_id(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            resp = c.post("/agents/register", json={"agent_id": "bad agent!"})
            assert resp.status_code == 400

    def test_register_fires_hook(self, msg_app):
        app, _ = msg_app
        from hub.messaging import on_agent_registered
        hook_log = []
        on_agent_registered.subscribe(
            lambda agent_id, record, data: hook_log.append(agent_id)
        )
        with app.test_client() as c:
            c.post("/agents/register", json={"agent_id": "hook-test"})
        assert hook_log == ["hook-test"]

    def test_register_hook_extras_in_response(self, msg_app):
        app, _ = msg_app
        from hub.messaging import on_agent_registered

        def add_extras(agent_id, record, data):
            return {"hub_balance": 100, "hub_token": "TOKEN123"}

        on_agent_registered.subscribe(add_extras)
        with app.test_client() as c:
            resp = c.post("/agents/register", json={"agent_id": "extras-test"})
            data = resp.get_json()
            assert data["hub_balance"] == 100
            assert data["hub_token"] == "TOKEN123"

    def test_welcome_message_in_inbox(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            reg = c.post("/agents/register", json={"agent_id": "inbox-check"}).get_json()
            secret = reg["secret"]
            inbox = c.get(f"/agents/inbox-check/messages?secret={secret}").get_json()
            assert inbox["count"] == 1
            assert "welcome" in inbox["messages"][0]["id"]


class TestSendMessage:
    def _register(self, client, agent_id):
        resp = client.post("/agents/register", json={"agent_id": agent_id})
        return resp.get_json()["secret"]

    def test_send_and_receive(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1 = self._register(c, "sender")
            s2 = self._register(c, "receiver")

            resp = c.post("/agents/receiver/message", json={
                "from": "sender",
                "secret": s1,
                "message": "Hello receiver!"
            })
            data = resp.get_json()
            assert data["ok"] is True
            assert "message_id" in data
            assert data["delivered_to_inbox"] is True

            # Check receiver inbox
            inbox = c.get(f"/agents/receiver/messages?secret={s2}").get_json()
            # 1 welcome + 1 message
            msgs = [m for m in inbox["messages"] if m.get("from") == "sender"]
            assert len(msgs) == 1
            assert msgs[0]["message"] == "Hello receiver!"

    def test_send_fires_hook(self, msg_app):
        app, _ = msg_app
        from hub.messaging import on_message_sent
        hook_log = []
        on_message_sent.subscribe(
            lambda sender, recipient, msg: hook_log.append((sender, recipient))
        )
        with app.test_client() as c:
            s = self._register(c, "alice")
            self._register(c, "bob")
            c.post("/agents/bob/message", json={
                "from": "alice", "secret": s, "message": "hi"
            })
        assert hook_log == [("alice", "bob")]

    def test_send_no_auth(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            self._register(c, "authed")
            self._register(c, "target")
            resp = c.post("/agents/target/message", json={
                "from": "authed", "message": "no secret"
            })
            assert resp.status_code == 401

    def test_send_wrong_secret(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            self._register(c, "wrongsec")
            self._register(c, "target2")
            resp = c.post("/agents/target2/message", json={
                "from": "wrongsec", "secret": "bad", "message": "hi"
            })
            assert resp.status_code == 403

    def test_send_to_nonexistent(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s = self._register(c, "orphan")
            resp = c.post("/agents/nobody/message", json={
                "from": "orphan", "secret": s, "message": "hi"
            })
            assert resp.status_code == 404

    def test_topic_and_reply_to(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1 = self._register(c, "topicsender")
            s2 = self._register(c, "topicreceiver")
            resp = c.post("/agents/topicreceiver/message", json={
                "from": "topicsender",
                "secret": s1,
                "message": "threaded msg",
                "topic": "project-x",
                "reply_to": "msg-000"
            })
            data = resp.get_json()
            assert data["ok"]

            inbox = c.get(f"/agents/topicreceiver/messages?secret={s2}&topic=project-x").get_json()
            thread_msgs = [m for m in inbox["messages"] if m.get("topic") == "project-x"]
            assert len(thread_msgs) == 1
            assert thread_msgs[0]["reply_to"] == "msg-000"


class TestMarkRead:
    def _setup(self, client):
        s1 = client.post("/agents/register", json={"agent_id": "reader"}).get_json()["secret"]
        s2 = client.post("/agents/register", json={"agent_id": "writer"}).get_json()["secret"]
        client.post("/agents/reader/message", json={
            "from": "writer", "secret": s2, "message": "read me"
        })
        return s1, s2

    def test_mark_read(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1, _ = self._setup(c)
            inbox = c.get(f"/agents/reader/messages?secret={s1}").get_json()
            msg = [m for m in inbox["messages"] if m["from"] == "writer"][0]
            assert msg["read"] is False

            resp = c.post(f"/agents/reader/messages/{msg['id']}/read?secret={s1}")
            assert resp.get_json()["ok"] is True

            inbox2 = c.get(f"/agents/reader/messages?secret={s1}").get_json()
            msg2 = [m for m in inbox2["messages"] if m["id"] == msg["id"]][0]
            assert msg2["read"] is True

    def test_mark_read_fires_hook(self, msg_app):
        app, _ = msg_app
        from hub.messaging import on_message_read
        hook_log = []
        on_message_read.subscribe(
            lambda agent_id, message_id, sender_id: hook_log.append(agent_id)
        )
        with app.test_client() as c:
            s1, _ = self._setup(c)
            inbox = c.get(f"/agents/reader/messages?secret={s1}").get_json()
            msg = [m for m in inbox["messages"] if m["from"] == "writer"][0]
            c.post(f"/agents/reader/messages/{msg['id']}/read?secret={s1}")
        assert hook_log == ["reader"]


class TestInboxManagement:
    def _setup(self, client):
        s = client.post("/agents/register", json={"agent_id": "mgmt"}).get_json()["secret"]
        s2 = client.post("/agents/register", json={"agent_id": "sender"}).get_json()["secret"]
        client.post("/agents/mgmt/message", json={
            "from": "sender", "secret": s2, "message": "msg1"
        })
        client.post("/agents/mgmt/message", json={
            "from": "sender", "secret": s2, "message": "msg2"
        })
        return s, s2

    def test_delete_message(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s, _ = self._setup(c)
            inbox = c.get(f"/agents/mgmt/messages?secret={s}").get_json()
            # Find a non-welcome message
            msgs = [m for m in inbox["messages"] if m["from"] == "sender"]
            msg_id = msgs[0]["id"]

            resp = c.delete(f"/agents/mgmt/messages/{msg_id}?secret={s}")
            assert resp.get_json()["ok"] is True

            inbox2 = c.get(f"/agents/mgmt/messages?secret={s}").get_json()
            remaining_ids = [m["id"] for m in inbox2["messages"]]
            assert msg_id not in remaining_ids

    def test_clear_inbox(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s, _ = self._setup(c)
            resp = c.delete(f"/agents/mgmt/messages?secret={s}")
            assert resp.get_json()["ok"] is True

            inbox = c.get(f"/agents/mgmt/messages?secret={s}").get_json()
            assert inbox["count"] == 0

    def test_sent_messages(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s, s2 = self._setup(c)
            sent = c.get(f"/agents/sender/messages/sent?secret={s2}").get_json()
            assert sent["ok"] is True
            assert sent["count"] >= 2


class TestDiscovery:
    def test_list_agents(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            c.post("/agents/register", json={"agent_id": "a1", "description": "first"})
            c.post("/agents/register", json={"agent_id": "a2", "description": "second"})
            resp = c.get("/agents")
            data = resp.get_json()
            assert data["count"] == 2
            ids = [a["agent_id"] for a in data["agents"]]
            assert "a1" in ids
            assert "a2" in ids

    def test_get_agent(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            c.post("/agents/register", json={
                "agent_id": "detailed",
                "description": "A detailed agent",
                "capabilities": ["research"]
            })
            resp = c.get("/agents/detailed")
            data = resp.get_json()
            assert data["agent_id"] == "detailed"
            assert data["description"] == "A detailed agent"
            assert "research" in data["capabilities"]
            assert "liveness" in data

    def test_get_nonexistent_agent(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            resp = c.get("/agents/nobody")
            assert resp.status_code == 404

    def test_match_agents(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            c.post("/agents/register", json={
                "agent_id": "security-bot",
                "description": "security audit specialist",
                "capabilities": ["security-audit", "code-review"]
            })
            c.post("/agents/register", json={
                "agent_id": "writer-bot",
                "description": "content writing",
                "capabilities": ["writing", "editing"]
            })
            resp = c.get("/agents/match?need=security")
            data = resp.get_json()
            assert len(data["matches"]) >= 1
            assert data["matches"][0]["agent_id"] == "security-bot"

    def test_list_discovered_empty(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            resp = c.get("/discover")
            data = resp.get_json()
            assert data["count"] == 0


class TestBroadcast:
    def test_broadcast(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1 = c.post("/agents/register", json={"agent_id": "broadcaster"}).get_json()["secret"]
            s2 = c.post("/agents/register", json={"agent_id": "listener1"}).get_json()["secret"]
            s3 = c.post("/agents/register", json={"agent_id": "listener2"}).get_json()["secret"]

            resp = c.post("/broadcast", json={
                "from": "broadcaster",
                "secret": s1,
                "payload": {"text": "announcement"}
            })
            data = resp.get_json()
            assert data["ok"] is True
            assert data["count"] == 2
            assert set(data["delivered_to"]) == {"listener1", "listener2"}

            # Check listener1 got it
            inbox = c.get(f"/agents/listener1/messages?secret={s2}").get_json()
            broadcast_msgs = [m for m in inbox["messages"] if m.get("is_broadcast")]
            assert len(broadcast_msgs) == 1


class TestLiveness:
    def test_liveness_in_agent_listing(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            c.post("/agents/register", json={"agent_id": "live-agent"})
            resp = c.get("/agents")
            agent = [a for a in resp.get_json()["agents"] if a["agent_id"] == "live-agent"][0]
            assert "liveness" in agent
            assert "liveness_class" in agent["liveness"]
            assert "delivery_capability" in agent["liveness"]


class TestEventHookIntegration:
    def test_on_agent_event_fires_on_poll(self, msg_app):
        app, _ = msg_app
        from hub.messaging import on_agent_event
        events = []
        on_agent_event.subscribe(lambda aid, etype, meta: events.append(etype))

        with app.test_client() as c:
            s = c.post("/agents/register", json={"agent_id": "poller"}).get_json()["secret"]
            c.get(f"/agents/poller/messages?secret={s}")

        assert "inbox_poll" in events

    def test_hooks_isolated_between_tests(self, msg_app):
        """Verify fixture clears hooks between tests."""
        from hub.messaging import on_message_sent
        assert len(on_message_sent) == 0


# ══════════════════════════════════════════════════════════════════════
#  deliver_message() Tests
# ══════════════════════════════════════════════════════════════════════

class TestDeliverMessage:
    def _register(self, client, agent_id):
        return client.post("/agents/register", json={"agent_id": agent_id}).get_json()["secret"]

    def test_deliver_message_directly(self, msg_app):
        """deliver_message() works without flask.request context."""
        app, _ = msg_app
        from hub.messaging import deliver_message

        with app.test_client() as c:
            self._register(c, "dm-sender")
            self._register(c, "dm-receiver")

        # Call deliver_message outside of any HTTP request
        with app.app_context():
            result = deliver_message("dm-sender", "dm-receiver", "direct delivery test")

        assert result["ok"] is True
        assert "message_id" in result
        assert result["delivered_to_inbox"] is True

    def test_deliver_message_fires_hook(self, msg_app):
        app, _ = msg_app
        from hub.messaging import deliver_message, on_message_sent
        hook_log = []
        on_message_sent.subscribe(lambda s, r, m: hook_log.append((s, r)))

        with app.test_client() as c:
            self._register(c, "hook-s")
            self._register(c, "hook-r")

        with app.app_context():
            deliver_message("hook-s", "hook-r", "hook test")

        assert hook_log == [("hook-s", "hook-r")]

    def test_deliver_message_updates_counters(self, msg_app):
        app, _ = msg_app
        from hub.messaging import deliver_message, load_agents

        with app.test_client() as c:
            self._register(c, "cnt-s")
            self._register(c, "cnt-r")

        with app.app_context():
            deliver_message("cnt-s", "cnt-r", "counter test")
            agents = load_agents()

        assert agents["cnt-r"]["messages_received"] >= 1
        assert "last_message_received_at" in agents["cnt-r"]
        assert "last_message_sent_at" in agents["cnt-s"]

    def test_deliver_message_to_nonexistent(self, msg_app):
        app, _ = msg_app
        from hub.messaging import deliver_message

        with app.test_client() as c:
            self._register(c, "orphan-s")

        with app.app_context():
            result = deliver_message("orphan-s", "ghost", "hello ghost")

        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_deliver_message_with_extra(self, msg_app):
        app, _ = msg_app
        from hub.messaging import deliver_message, load_inbox

        with app.test_client() as c:
            s2 = self._register(c, "extra-s")
            self._register(c, "extra-r")

        with app.app_context():
            result = deliver_message(
                "extra-s", "extra-r", "typed message",
                extra={"type": "obligation_update", "obl_id": "obl-123"}
            )

        assert result["ok"] is True

        with app.test_client() as c:
            inbox = c.get(f"/agents/extra-r/messages?secret={self._register(c, 'extra-r') if False else ''}").get_json()

        # Verify via deliver_message result
        with app.app_context():
            inbox_msgs = load_inbox("extra-r")
            typed = [m for m in inbox_msgs if m.get("type") == "obligation_update"]
            assert len(typed) == 1
            assert typed[0]["obl_id"] == "obl-123"

    def test_http_route_uses_deliver_message(self, msg_app):
        """HTTP POST /agents/{id}/message produces same result structure."""
        app, _ = msg_app
        with app.test_client() as c:
            s1 = self._register(c, "http-s")
            self._register(c, "http-r")
            resp = c.post("/agents/http-r/message", json={
                "from": "http-s", "secret": s1, "message": "via http"
            })
            data = resp.get_json()
            assert data["ok"] is True
            assert "message_id" in data
            assert "delivery_state" in data
            assert data["delivered_to_inbox"] is True

    def test_deliver_with_topic_and_reply_to(self, msg_app):
        app, _ = msg_app
        from hub.messaging import deliver_message, load_inbox

        with app.test_client() as c:
            self._register(c, "thread-s")
            self._register(c, "thread-r")

        with app.app_context():
            result = deliver_message(
                "thread-s", "thread-r", "threaded",
                topic="project-x", reply_to="msg-000"
            )
            assert result["ok"] is True

            inbox = load_inbox("thread-r")
            threaded = [m for m in inbox if m.get("topic") == "project-x"]
            assert len(threaded) == 1
            assert threaded[0]["reply_to"] == "msg-000"


# ══════════════════════════════════════════════════════════════════════
#  Bidirectional WebSocket Tests
# ══════════════════════════════════════════════════════════════════════

class TestWebSocketSend:
    """Test sending messages over WebSocket (bidirectional)."""

    def _register(self, client, agent_id):
        return client.post("/agents/register", json={"agent_id": agent_id}).get_json()["secret"]

    def test_ws_send_message(self, msg_app):
        """Agent sends a message over WS via {"type": "send", "to": ..., "message": ...}"""
        app, _ = msg_app
        with app.test_client() as c:
            s1 = self._register(c, "ws-sender")
            s2 = self._register(c, "ws-target")

        # We can't easily test real WebSocket with Flask test client,
        # so test the handler function directly
        from hub.messaging import _handle_ws_send, load_inbox

        class FakeWS:
            def __init__(self):
                self.sent = []
            def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeWS()
        with app.app_context():
            _handle_ws_send(ws, "ws-sender", {
                "type": "send",
                "to": "ws-target",
                "message": "hello from websocket"
            })

        # Check WS got a send_result
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "send_result"
        assert ws.sent[0]["ok"] is True
        assert "message_id" in ws.sent[0]

        # Check message arrived in target inbox
        with app.app_context():
            inbox = load_inbox("ws-target")
            ws_msgs = [m for m in inbox if m.get("from") == "ws-sender"]
            assert len(ws_msgs) == 1
            assert ws_msgs[0]["message"] == "hello from websocket"

    def test_ws_send_missing_to(self, msg_app):
        app, _ = msg_app
        from hub.messaging import _handle_ws_send

        class FakeWS:
            def __init__(self):
                self.sent = []
            def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeWS()
        with app.app_context():
            _handle_ws_send(ws, "anyone", {"type": "send", "message": "no target"})

        assert ws.sent[0]["ok"] is False
        assert "Missing 'to'" in ws.sent[0]["error"]

    def test_ws_send_missing_message(self, msg_app):
        app, _ = msg_app
        from hub.messaging import _handle_ws_send

        class FakeWS:
            def __init__(self):
                self.sent = []
            def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeWS()
        with app.app_context():
            _handle_ws_send(ws, "anyone", {"type": "send", "to": "target"})

        assert ws.sent[0]["ok"] is False
        assert "Missing 'message'" in ws.sent[0]["error"]

    def test_ws_send_to_nonexistent(self, msg_app):
        app, _ = msg_app
        from hub.messaging import _handle_ws_send

        class FakeWS:
            def __init__(self):
                self.sent = []
            def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeWS()
        with app.app_context():
            _handle_ws_send(ws, "sender", {
                "type": "send", "to": "nobody", "message": "hello"
            })

        assert ws.sent[0]["type"] == "send_result"
        assert ws.sent[0]["ok"] is False
        assert "not found" in ws.sent[0]["error"]

    def test_ws_send_with_topic(self, msg_app):
        app, _ = msg_app
        from hub.messaging import _handle_ws_send, load_inbox

        with app.test_client() as c:
            self._register(c, "topic-ws-s")
            self._register(c, "topic-ws-r")

        class FakeWS:
            def __init__(self):
                self.sent = []
            def send(self, data):
                self.sent.append(json.loads(data))

        ws = FakeWS()
        with app.app_context():
            _handle_ws_send(ws, "topic-ws-s", {
                "type": "send",
                "to": "topic-ws-r",
                "message": "threaded ws msg",
                "topic": "ws-thread",
                "reply_to": "prev-msg"
            })

        assert ws.sent[0]["ok"] is True

        with app.app_context():
            inbox = load_inbox("topic-ws-r")
            threaded = [m for m in inbox if m.get("topic") == "ws-thread"]
            assert len(threaded) == 1
            assert threaded[0]["reply_to"] == "prev-msg"


# ══════════════════════════════════════════════════════════════════════
#  Passive ACK (session_loaded) Tests
# ══════════════════════════════════════════════════════════════════════

class TestPassiveAck:
    def _setup(self, client):
        """Register sender + receiver, send a message, return secrets + message_id."""
        s1 = client.post("/agents/register", json={"agent_id": "ack-sender"}).get_json()["secret"]
        s2 = client.post("/agents/register", json={"agent_id": "ack-receiver"}).get_json()["secret"]
        client.post("/agents/ack-receiver/message", json={
            "from": "ack-sender", "secret": s1, "message": "ack me"
        })
        inbox = client.get(f"/agents/ack-receiver/messages?secret={s2}").get_json()
        msg = [m for m in inbox["messages"] if m["from"] == "ack-sender"][0]
        return s1, s2, msg["id"]

    def test_ack_happy_path(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1, s2, msg_id = self._setup(c)

            resp = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={
                "secret": s2, "ack_type": "session_loaded", "runtime_id": "rt-123"
            })
            data = resp.get_json()
            assert resp.status_code == 200
            assert data["ok"] is True
            assert data["ack_type"] == "session_loaded"
            assert data["delivery_state"] == "session_loaded"
            assert data["already_acked"] is False
            assert "acked_at" in data

            # Verify inbox record was updated
            inbox = c.get(f"/agents/ack-receiver/messages?secret={s2}").get_json()
            msg = [m for m in inbox["messages"] if m["id"] == msg_id][0]
            assert msg["session_loaded"] is True
            assert msg["ack_type"] == "session_loaded"
            assert msg["session_runtime_id"] == "rt-123"

            # Verify sent record was updated
            sent = c.get(f"/agents/ack-sender/messages/sent?secret={s1}").get_json()
            rec = [r for r in sent["messages"] if r["message_id"] == msg_id][0]
            assert rec["session_loaded"] is True
            assert rec["delivery_state"] == "session_loaded"
            assert rec["ack_type"] == "session_loaded"

    def test_ack_bad_secret(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            _, s2, msg_id = self._setup(c)
            resp = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={
                "secret": "wrong-secret"
            })
            assert resp.status_code == 403

    def test_ack_unknown_agent(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            resp = c.post("/agents/nonexistent/messages/fake-id/ack", json={
                "secret": "whatever"
            })
            assert resp.status_code == 404

    def test_ack_unknown_message(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            _, s2, _ = self._setup(c)
            resp = c.post("/agents/ack-receiver/messages/no-such-msg/ack", json={
                "secret": s2
            })
            assert resp.status_code == 404

    def test_ack_idempotent(self, msg_app):
        """Re-acking returns 200 with the original stored timestamp and ack_type."""
        app, _ = msg_app
        with app.test_client() as c:
            _, s2, msg_id = self._setup(c)

            resp1 = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={
                "secret": s2, "ack_type": "session_loaded"
            })
            data1 = resp1.get_json()
            assert data1["already_acked"] is False
            original_ts = data1["acked_at"]

            resp2 = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={
                "secret": s2, "ack_type": "session_loaded"
            })
            data2 = resp2.get_json()
            assert resp2.status_code == 200
            assert data2["already_acked"] is True
            # Idempotent: returns original timestamp, not current time
            assert data2["acked_at"] == original_ts
            assert data2["ack_type"] == "session_loaded"

    def test_ack_fires_hook_once(self, msg_app):
        """Event hook fires on first ack, not on idempotent re-ack."""
        app, _ = msg_app
        from hub.messaging import on_message_acked
        hook_log = []
        on_message_acked.subscribe(
            lambda agent_id, message_id, sender_id, ack_type, runtime_id: hook_log.append(message_id)
        )
        with app.test_client() as c:
            _, s2, msg_id = self._setup(c)
            c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={"secret": s2})
            c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={"secret": s2})
        assert hook_log == [msg_id]

    def test_ack_then_read_state_progression(self, msg_app):
        """session_loaded -> read produces session_loaded_read delivery state on sent record."""
        app, _ = msg_app
        with app.test_client() as c:
            s1, s2, msg_id = self._setup(c)

            # ACK first
            c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={"secret": s2})

            # Verify sent record is session_loaded
            sent = c.get(f"/agents/ack-sender/messages/sent?secret={s1}").get_json()
            rec = [r for r in sent["messages"] if r["message_id"] == msg_id][0]
            assert rec["delivery_state"] == "session_loaded"

            # Now mark read
            c.post(f"/agents/ack-receiver/messages/{msg_id}/read?secret={s2}")

            # Verify sent record preserves session_loaded signal through read
            sent2 = c.get(f"/agents/ack-sender/messages/sent?secret={s1}").get_json()
            rec2 = [r for r in sent2["messages"] if r["message_id"] == msg_id][0]
            assert rec2["read"] is True
            assert rec2["session_loaded"] is True
            # delivery_state should be session_loaded_read (not plain inbox_read)
            assert "session_loaded_read" in rec2["delivery_state"]

    def test_read_then_ack_no_downgrade(self, msg_app):
        """If already read, ack should not downgrade sent record delivery_state.
        Response should report already_acked=True and hook should NOT fire."""
        app, _ = msg_app
        from hub.messaging import on_message_acked
        hook_log = []
        on_message_acked.subscribe(
            lambda agent_id, message_id, sender_id, ack_type, runtime_id: hook_log.append(message_id)
        )
        with app.test_client() as c:
            s1, s2, msg_id = self._setup(c)

            # Read first
            c.post(f"/agents/ack-receiver/messages/{msg_id}/read?secret={s2}")

            sent = c.get(f"/agents/ack-sender/messages/sent?secret={s1}").get_json()
            rec = [r for r in sent["messages"] if r["message_id"] == msg_id][0]
            assert "read" in rec["delivery_state"]

            # Now ack — message already read, treated as already-acked
            resp = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={"secret": s2})
            data = resp.get_json()
            assert data["already_acked"] is True

            # Hook should not have fired (already at higher state)
            assert hook_log == []

            # Sent record should now have session_loaded=True and a session_loaded_read state
            sent2 = c.get(f"/agents/ack-sender/messages/sent?secret={s1}").get_json()
            rec2 = [r for r in sent2["messages"] if r["message_id"] == msg_id][0]
            assert rec2["session_loaded"] is True
            assert "session_loaded_read" in rec2["delivery_state"]

    def test_ack_without_runtime_id(self, msg_app):
        """runtime_id is optional — omitting it should not set session_runtime_id."""
        app, _ = msg_app
        with app.test_client() as c:
            _, s2, msg_id = self._setup(c)

            c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={"secret": s2})

            inbox = c.get(f"/agents/ack-receiver/messages?secret={s2}").get_json()
            msg = [m for m in inbox["messages"] if m["id"] == msg_id][0]
            assert msg["session_loaded"] is True
            assert "session_runtime_id" not in msg

    def test_ack_deleted_message_returns_404(self, msg_app):
        """Acking a message that was deleted from inbox returns 404."""
        app, _ = msg_app
        with app.test_client() as c:
            _, s2, msg_id = self._setup(c)

            # Delete the message first
            c.delete(f"/agents/ack-receiver/messages/{msg_id}?secret={s2}")

            # Now try to ack — should 404
            resp = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={"secret": s2})
            assert resp.status_code == 404

    def test_ack_invalid_ack_type(self, msg_app):
        """Only 'session_loaded' is a valid ack_type."""
        app, _ = msg_app
        with app.test_client() as c:
            _, s2, msg_id = self._setup(c)

            resp = c.post(f"/agents/ack-receiver/messages/{msg_id}/ack", json={
                "secret": s2, "ack_type": "garbage"
            })
            assert resp.status_code == 400


class TestSessionLoadedFilter:
    def _setup(self, client):
        """Register sender + receiver, send two messages, ack one."""
        s1 = client.post("/agents/register", json={"agent_id": "filter-sender"}).get_json()["secret"]
        s2 = client.post("/agents/register", json={"agent_id": "filter-receiver"}).get_json()["secret"]
        client.post("/agents/filter-receiver/message", json={
            "from": "filter-sender", "secret": s1, "message": "msg-acked"
        })
        client.post("/agents/filter-receiver/message", json={
            "from": "filter-sender", "secret": s1, "message": "msg-not-acked"
        })
        inbox = client.get(f"/agents/filter-receiver/messages?secret={s2}").get_json()
        msgs = [m for m in inbox["messages"] if m["from"] == "filter-sender"]
        # Ack only the first message
        client.post(f"/agents/filter-receiver/messages/{msgs[0]['id']}/ack", json={"secret": s2})
        return s1, s2, msgs[0]["id"], msgs[1]["id"]

    def test_filter_session_loaded_true(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1, _, acked_id, not_acked_id = self._setup(c)
            sent = c.get(f"/agents/filter-sender/messages/sent?secret={s1}&session_loaded=true").get_json()
            ids = [r["message_id"] for r in sent["messages"]]
            assert acked_id in ids
            assert not_acked_id not in ids

    def test_filter_session_loaded_false(self, msg_app):
        app, _ = msg_app
        with app.test_client() as c:
            s1, _, acked_id, not_acked_id = self._setup(c)
            sent = c.get(f"/agents/filter-sender/messages/sent?secret={s1}&session_loaded=false").get_json()
            ids = [r["message_id"] for r in sent["messages"]]
            assert not_acked_id in ids
            assert acked_id not in ids

    def test_filter_not_present_returns_all(self, msg_app):
        """When session_loaded param is absent, all records are returned."""
        app, _ = msg_app
        with app.test_client() as c:
            s1, _, acked_id, not_acked_id = self._setup(c)
            sent = c.get(f"/agents/filter-sender/messages/sent?secret={s1}").get_json()
            ids = [r["message_id"] for r in sent["messages"]]
            assert acked_id in ids
            assert not_acked_id in ids

    def test_filter_empty_string_does_not_activate(self, msg_app):
        """?session_loaded= (empty) should not activate the filter — returns all records.
        This is a documented-by-design behavior: empty query params use truthiness check."""
        app, _ = msg_app
        with app.test_client() as c:
            s1, _, acked_id, not_acked_id = self._setup(c)
            sent = c.get(f"/agents/filter-sender/messages/sent?secret={s1}&session_loaded=").get_json()
            ids = [r["message_id"] for r in sent["messages"]]
            # Empty string should NOT activate filter — both records returned
            assert acked_id in ids
            assert not_acked_id in ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
