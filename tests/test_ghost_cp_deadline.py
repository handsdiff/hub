import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def obligation_app():
    from flask import Flask
    from hub.messaging import messaging_bp, init_messaging, on_message_sent, on_agent_registered
    from hub.obligations import obligations_bp, init_obligations

    tmpdir = tempfile.mkdtemp(prefix="hub-ghost-cp-test-")
    app = Flask(__name__)
    app.config["TESTING"] = True

    on_message_sent.clear()
    on_agent_registered.clear()

    init_messaging(Path(tmpdir))
    init_obligations(tmpdir)
    app.register_blueprint(messaging_bp)
    app.register_blueprint(obligations_bp)

    yield app, tmpdir

    on_message_sent.clear()
    on_agent_registered.clear()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _register(client, agent_id):
    resp = client.post("/agents/register", json={
        "agent_id": agent_id,
        "description": f"Test agent {agent_id}",
        "capabilities": ["testing"],
    })
    assert resp.status_code in (200, 201)
    return resp.get_json()["secret"]


def test_counterparty_accepts_defaults_deadline(obligation_app):
    app, _tmpdir = obligation_app
    with app.test_client() as client:
        claimant_secret = _register(client, "claimant")
        _register(client, "counterparty")

        resp = client.post("/obligations", json={
            "from": "claimant",
            "secret": claimant_secret,
            "counterparty": "counterparty",
            "commitment": "deliver proof",
            "closure_policy": "counterparty_accepts",
        })

        assert resp.status_code == 201
        obl = resp.get_json()["obligation"]
        assert obl["deadline_defaulted"] is True

        created = datetime.fromisoformat(obl["created_at"].replace("Z", "+00:00"))
        deadline = datetime.fromisoformat(obl["deadline_utc"].replace("Z", "+00:00"))
        assert abs((deadline - created) - timedelta(days=14)) < timedelta(seconds=1)


def test_counterparty_accepts_migration_sets_deadline(obligation_app):
    _app, _tmpdir = obligation_app
    from hub.obligations import load_obligations, save_obligations, _expire_obligations

    created_at = "2026-05-01T00:00:00Z"
    obligation = {
        "obligation_id": "obl-missing-deadline",
        "created_at": created_at,
        "created_by": "claimant",
        "counterparty": "counterparty",
        "status": "accepted",
        "closure_policy": "counterparty_accepts",
        "timeout_policy": "claimant_self_resolve",
        "history": [{"status": "accepted", "at": created_at, "by": "counterparty"}],
    }
    save_obligations([obligation])

    obls = load_obligations()
    assert _expire_obligations(obls) is True
    assert obls[0]["deadline_utc"] == "2026-05-15T00:00:00Z"
    assert obls[0]["deadline_defaulted"] is True
    assert any(h.get("event") == "deadline_defaulted" for h in obls[0]["history"])


def test_claimant_self_resolve_from_evidence_after_48h_only():
    from hub.obligations import _can_resolve

    submitted_at = (datetime.now(timezone.utc) - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ")
    obligation = {
        "status": "evidence_submitted",
        "created_by": "claimant",
        "counterparty": "counterparty",
        "closure_policy": "counterparty_accepts",
        "timeout_policy": "claimant_self_resolve",
        "role_bindings": [
            {"role": "claimant", "agent_id": "claimant"},
            {"role": "counterparty", "agent_id": "counterparty"},
        ],
        "evidence_refs": [{"submitted_at": submitted_at, "uri": "https://example.test/proof"}],
    }

    assert _can_resolve(obligation, "claimant") is True

    obligation["timeout_policy"] = "auto_expire"
    assert _can_resolve(obligation, "claimant") is False

    obligation["timeout_policy"] = "claimant_self_resolve"
    obligation["evidence_refs"][0]["submitted_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=47)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert _can_resolve(obligation, "claimant") is False
