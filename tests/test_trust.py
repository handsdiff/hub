"""
Tests for the trust module.

Covers: trust profile (GET /trust/<id>), attestations, trust signals,
graceful degradation on corrupted data, and helper functions.
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
sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def trust_app():
    """Create a Flask app with messaging + trust + supporting Blueprints and temp data dir."""
    from flask import Flask
    from hub.messaging import messaging_bp, init_messaging, on_message_sent, on_agent_registered
    from hub.trust import trust_bp, init_trust
    from hub.bounties import bounties_bp, init_bounties
    from hub.analytics import analytics_bp, init_analytics
    from hub.obligations import obligations_bp, init_obligations

    tmpdir = tempfile.mkdtemp(prefix="hub-trust-test-")

    app = Flask(__name__)
    app.config["TESTING"] = True

    on_message_sent.clear()
    on_agent_registered.clear()

    init_messaging(Path(tmpdir))
    init_trust(tmpdir)
    init_bounties(tmpdir)
    init_analytics(tmpdir)
    init_obligations(tmpdir)
    app.register_blueprint(messaging_bp)
    app.register_blueprint(trust_bp)
    app.register_blueprint(bounties_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(obligations_bp)

    yield app, tmpdir

    on_message_sent.clear()
    on_agent_registered.clear()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _register_agent(client, agent_id):
    """Helper: register an agent and return its secret."""
    resp = client.post("/agents/register", json={
        "agent_id": agent_id,
        "description": f"Test agent {agent_id}",
        "capabilities": ["testing"],
    })
    return resp.get_json()["secret"]


# ══════════════════════════════════════════════════════════════════════
#  GET /trust/<agent_id> — trust profile
# ══════════════════════════════════════════════════════════════════════

class TestGetTrustProfile:
    def test_trust_profile_registered_agent(self, trust_app):
        """Happy path: registered agent returns 200 with STS v1 structure."""
        app, tmpdir = trust_app
        with app.test_client() as c:
            _register_agent(c, "test-trust-agent")
            resp = c.get("/trust/test-trust-agent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["version"] == "1.0.0"
            assert data["agent_identity"]["agent_name"] == "test-trust-agent"
            assert "behavioral_trust" in data
            assert "operational_state" in data
            assert "discovery_layer" in data
            assert "summary" in data

    def test_trust_profile_nonexistent_agent(self, trust_app):
        """Nonexistent agent should return 200 with empty/default profile, not 500."""
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.get("/trust/nonexistent-agent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["version"] == "1.0.0"
            # Should have empty behavioral data
            assert data["behavioral_trust"]["economic_trust"]["successful_deliveries"] == 0
            assert data["behavioral_trust"]["trust_quality"]["attester_count"] == 0

    def test_trust_profile_corrupted_health_history(self, trust_app):
        """Corrupted health_history.json should degrade gracefully, not crash."""
        app, tmpdir = trust_app
        with open(os.path.join(tmpdir, "health_history.json"), "w") as f:
            f.write("{corrupted")
        # Re-init trust to pick up the file
        from hub.trust import init_trust
        init_trust(tmpdir)
        with app.test_client() as c:
            resp = c.get("/trust/some-agent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["operational_state"]["uptime_percentage"] == 0

    def test_trust_profile_corrupted_attestations(self, trust_app):
        """Corrupted attestations.json should degrade gracefully."""
        app, tmpdir = trust_app
        with open(os.path.join(tmpdir, "attestations.json"), "w") as f:
            f.write("not-json-at-all")
        from hub.trust import init_trust
        init_trust(tmpdir)
        with app.test_client() as c:
            resp = c.get("/trust/some-agent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["behavioral_trust"]["trust_quality"]["attester_count"] == 0

    def test_trust_profile_corrupted_trust_signals(self, trust_app):
        """Corrupted trust_signals.json should degrade gracefully."""
        app, tmpdir = trust_app
        with open(os.path.join(tmpdir, "trust_signals.json"), "w") as f:
            f.write("[invalid")
        from hub.trust import init_trust
        init_trust(tmpdir)
        with app.test_client() as c:
            resp = c.get("/trust/some-agent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["behavioral_trust"]["social_attestations"] == []

    def test_trust_profile_bounty_missing_requester(self, trust_app):
        """Bounty records with missing 'requester' field should not crash."""
        app, tmpdir = trust_app
        with open(os.path.join(tmpdir, "bounties.json"), "w") as f:
            json.dump([{"id": "b1", "status": "completed", "claimed_by": "test-agent"}], f)
        with app.test_client() as c:
            resp = c.get("/trust/test-agent")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["behavioral_trust"]["economic_trust"]["successful_deliveries"] == 1


# ══════════════════════════════════════════════════════════════════════
#  GET /trust — list all trust profiles
# ══════════════════════════════════════════════════════════════════════

class TestListTrust:
    def test_list_trust_empty(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.get("/trust")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["count"] == 0
            assert data["agents"] == []

    def test_list_trust_with_health_data(self, trust_app):
        app, tmpdir = trust_app
        with open(os.path.join(tmpdir, "health_history.json"), "w") as f:
            json.dump({"agent-a": {"stats": {"uptime_pct": 99}, "checks": []}}, f)
        from hub.trust import init_trust
        init_trust(tmpdir)
        with app.test_client() as c:
            resp = c.get("/trust")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["count"] == 1


# ══════════════════════════════════════════════════════════════════════
#  POST /trust/attest — submit attestation
# ══════════════════════════════════════════════════════════════════════

class TestAttestation:
    def test_attest_happy_path(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            s1 = _register_agent(c, "attester-1")
            _register_agent(c, "subject-1")
            resp = c.post("/trust/attest", json={
                "from": "attester-1",
                "secret": s1,
                "agent_id": "subject-1",
                "category": "reliability",
                "score": 0.9,
                "evidence": "Delivered 3 obligations on time",
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["total_attestations"] == 1
            assert data["attestation"]["category"] == "reliability"
            assert data["attestation"]["score"] == 0.9

    def test_attest_bad_secret(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            _register_agent(c, "attester-2")
            _register_agent(c, "subject-2")
            resp = c.post("/trust/attest", json={
                "from": "attester-2",
                "secret": "wrong-secret",
                "agent_id": "subject-2",
                "score": 0.5,
            })
            assert resp.status_code == 403

    def test_attest_missing_fields(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.post("/trust/attest", json={"from": "x"})
            assert resp.status_code == 400

    def test_attest_self(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            s = _register_agent(c, "narcissist")
            resp = c.post("/trust/attest", json={
                "from": "narcissist",
                "secret": s,
                "agent_id": "narcissist",
                "score": 1.0,
            })
            assert resp.status_code == 400

    def test_attest_invalid_score(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            s = _register_agent(c, "att-score")
            _register_agent(c, "sub-score")
            resp = c.post("/trust/attest", json={
                "from": "att-score",
                "secret": s,
                "agent_id": "sub-score",
                "score": 5.0,
            })
            assert resp.status_code == 400

    def test_attest_shows_in_profile(self, trust_app):
        """Attestation should appear in trust profile's trust_quality."""
        app, tmpdir = trust_app
        with app.test_client() as c:
            s = _register_agent(c, "a-prof")
            _register_agent(c, "b-prof")
            c.post("/trust/attest", json={
                "from": "a-prof", "secret": s, "agent_id": "b-prof",
                "score": 0.8, "evidence": "Good work",
            })
            resp = c.get("/trust/b-prof")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["behavioral_trust"]["trust_quality"]["attester_count"] == 1


# ══════════════════════════════════════════════════════════════════════
#  POST /trust/signal — submit trust signal
# ══════════════════════════════════════════════════════════════════════

class TestTrustSignal:
    def test_signal_happy_path(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.post("/trust/signal", json={
                "from": "agent-a",
                "about": "agent-b",
                "channel": "routing",
                "strength": 0.7,
                "evidence": "Routed 5 messages successfully",
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["ok"] is True
            assert data["channel"] == "routing"
            assert data["reinforced"] is False

    def test_signal_missing_fields(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.post("/trust/signal", json={"from": "x"})
            assert resp.status_code == 400

    def test_signal_invalid_channel(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.post("/trust/signal", json={
                "from": "a", "about": "b", "channel": "made-up-channel",
            })
            assert resp.status_code == 400

    def test_signal_reinforcement(self, trust_app):
        """Same from+about+channel should reinforce, not duplicate."""
        app, tmpdir = trust_app
        with app.test_client() as c:
            c.post("/trust/signal", json={
                "from": "a", "about": "b", "channel": "routing", "strength": 0.5,
            })
            resp = c.post("/trust/signal", json={
                "from": "a", "about": "b", "channel": "routing", "strength_delta": 0.2,
            })
            data = resp.get_json()
            assert data["reinforced"] is True

    def test_signal_shows_in_agent_signals(self, trust_app):
        """GET /trust/<id>/signals returns MVA behavioral trust signals."""
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.get("/trust/some-agent/signals")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "signals" in data
            signals = data["signals"]
            assert "delivery_rate" in signals
            assert "settlement_rate" in signals
            assert "ewma_trajectory" in signals
            assert "role_fit_trust" in signals


# ══════════════════════════════════════════════════════════════════════
#  GET /trust/signals — list all signals
# ══════════════════════════════════════════════════════════════════════

class TestTrustSignalsList:
    def test_signals_empty(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.get("/trust/signals")
            assert resp.status_code == 200

    def test_signals_with_data(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            c.post("/trust/signal", json={
                "from": "a", "about": "b", "channel": "routing",
            })
            resp = c.get("/trust/signals")
            assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
#  Trust helper functions
# ══════════════════════════════════════════════════════════════════════

class TestTrustHelpers:
    def test_load_health_history_missing_file(self, trust_app):
        """Missing file returns empty dict."""
        app, tmpdir = trust_app
        from hub.trust import load_health_history
        assert load_health_history() == {}

    def test_load_attestations_missing_file(self, trust_app):
        app, tmpdir = trust_app
        from hub.trust import load_attestations
        assert load_attestations() == {}

    def test_load_trust_signals_missing_file(self, trust_app):
        app, tmpdir = trust_app
        from hub.trust import load_trust_signals
        assert load_trust_signals() == {}

    def test_load_health_history_valid(self, trust_app):
        app, tmpdir = trust_app
        from hub.trust import load_health_history, init_trust
        with open(os.path.join(tmpdir, "health_history.json"), "w") as f:
            json.dump({"agent-x": {"stats": {"uptime_pct": 95}}}, f)
        init_trust(tmpdir)
        result = load_health_history()
        assert result["agent-x"]["stats"]["uptime_pct"] == 95

    def test_trust_multiplier_from_decay(self, trust_app):
        from hub.trust import _trust_multiplier_from_decay
        assert _trust_multiplier_from_decay(0.9) == 1.0
        assert _trust_multiplier_from_decay(0.6) == 0.75
        assert _trust_multiplier_from_decay(0.4) == 0.5
        assert _trust_multiplier_from_decay(0.2) == 0.25
        assert _trust_multiplier_from_decay(0.05) == 0.0
        # Edge: non-numeric input
        assert _trust_multiplier_from_decay("invalid") == 1.0
        assert _trust_multiplier_from_decay(None) == 1.0


# ══════════════════════════════════════════════════════════════════════
#  GET /trust/schema — schema endpoint
# ══════════════════════════════════════════════════════════════════════

class TestTrustSchema:
    def test_schema_returns_200(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.get("/trust/schema")
            assert resp.status_code == 200


# ══════════════════════════════════════════════════════════════════════
#  GET /trust/signal/channels — list valid channels
# ══════════════════════════════════════════════════════════════════════

class TestTrustChannels:
    def test_channels_returns_200(self, trust_app):
        app, tmpdir = trust_app
        with app.test_client() as c:
            resp = c.get("/trust/signal/channels")
            assert resp.status_code == 200
            data = resp.get_json()
            assert "routing" in str(data)
