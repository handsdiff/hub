import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from hub.messaging import (  # noqa: E402
    init_messaging,
    messaging_bp,
    on_agent_registered,
    on_message_sent,
)
from hub.obligations import (  # noqa: E402
    COUNTERPARTY_ACCEPTS_DEFAULT_DEADLINE_DAYS,
    EVIDENCE_SELF_RESOLVE_HOURS,
    _can_resolve,
    _expire_obligations,
    init_obligations,
    obligations_bp,
)


def _iso_hours_ago(hours):
    return (datetime.utcnow() - timedelta(hours=hours)).replace(microsecond=0).isoformat() + "Z"


def _iso_hours_from_now(hours):
    return (datetime.utcnow() + timedelta(hours=hours)).replace(microsecond=0).isoformat() + "Z"


def _parse_utc(iso_ts):
    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).astimezone(timezone.utc)


@pytest.fixture
def obligation_app():
    tmpdir = tempfile.mkdtemp(prefix="hub-obligation-test-")
    app = Flask(__name__)
    app.config["TESTING"] = True

    on_message_sent.clear()
    on_agent_registered.clear()
    init_messaging(Path(tmpdir))
    init_obligations(tmpdir)
    app.register_blueprint(messaging_bp)
    app.register_blueprint(obligations_bp)

    yield app

    on_message_sent.clear()
    on_agent_registered.clear()
    shutil.rmtree(tmpdir, ignore_errors=True)


def _register(client, agent_id):
    resp = client.post("/agents/register", json={"agent_id": agent_id})
    assert resp.status_code in (200, 201)
    return resp.get_json()["secret"]


def _base_counterparty_obligation(status="accepted", **extra):
    now = _iso_hours_ago(1)
    obl = {
        "obligation_id": "obl-test",
        "created_at": now,
        "created_by": "alice",
        "counterparty": "bob",
        "parties": [{"agent_id": "alice"}, {"agent_id": "bob"}],
        "role_bindings": [
            {"role": "claimant", "agent_id": "alice"},
            {"role": "counterparty", "agent_id": "bob"},
        ],
        "status": status,
        "commitment": "deliver the artifact",
        "closure_policy": "counterparty_accepts",
        "timeout_policy": "claimant_self_resolve",
        "evidence_refs": [],
        "history": [
            {"status": "proposed", "at": now, "by": "alice"},
            {"status": status, "at": now, "by": "bob"},
        ],
    }
    obl.update(extra)
    return obl


def test_counterparty_accepts_uses_14_day_deadline_default(obligation_app):
    with obligation_app.test_client() as client:
        alice_secret = _register(client, "alice")
        _register(client, "bob")

        resp = client.post("/obligations", json={
            "from": "alice",
            "secret": alice_secret,
            "counterparty": "bob",
            "commitment": "deliver a closure-gap patch",
            "closure_policy": "counterparty_accepts",
        })

    assert resp.status_code in (200, 201)
    obligation = resp.get_json()["obligation"]
    assert obligation["deadline_utc"]

    deadline_delta = _parse_utc(obligation["deadline_utc"]) - _parse_utc(obligation["created_at"])
    assert timedelta(days=14, seconds=-1) <= deadline_delta <= timedelta(days=14, minutes=1)


def test_backfills_missing_counterparty_deadline_and_expires(obligation_app):
    created_at = _iso_hours_ago((COUNTERPARTY_ACCEPTS_DEFAULT_DEADLINE_DAYS * 24) + 2)
    obl = _base_counterparty_obligation(created_at=created_at, history=[
        {"status": "proposed", "at": created_at, "by": "alice"},
        {"status": "accepted", "at": created_at, "by": "bob"},
    ])

    changed = _expire_obligations([obl])

    assert changed is True
    assert obl["deadline_utc"] == (
        _parse_utc(created_at) + timedelta(days=COUNTERPARTY_ACCEPTS_DEFAULT_DEADLINE_DAYS)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    assert obl["status"] == "deadline_elapsed"
    assert obl["timeout_elapsed"] is True


def test_claimant_self_resolve_opens_after_unanswered_evidence(obligation_app):
    submitted_at = _iso_hours_ago(EVIDENCE_SELF_RESOLVE_HOURS + 1)
    obl = _base_counterparty_obligation(
        status="evidence_submitted",
        deadline_utc=_iso_hours_from_now(24 * 7),
        evidence_refs=[{"submitted_at": submitted_at, "by": "alice", "url": "https://example.com/proof"}],
        history=[
            {"status": "proposed", "at": _iso_hours_ago(100), "by": "alice"},
            {"status": "accepted", "at": _iso_hours_ago(96), "by": "bob"},
            {"status": "evidence_submitted", "at": submitted_at, "by": "alice"},
        ],
    )

    changed = _expire_obligations([obl])

    assert changed is True
    assert obl["status"] == "deadline_elapsed"
    assert obl["timeout_elapsed"] is True
    assert obl["evidence_self_resolve_elapsed"] is True
    assert _can_resolve(obl, "alice") is True


def test_counterparty_activity_blocks_evidence_self_resolve(obligation_app):
    submitted_at = _iso_hours_ago(EVIDENCE_SELF_RESOLVE_HOURS + 1)
    obl = _base_counterparty_obligation(
        status="evidence_submitted",
        deadline_utc=_iso_hours_from_now(24 * 7),
        evidence_refs=[{"submitted_at": submitted_at, "by": "alice", "url": "https://example.com/proof"}],
        history=[
            {"status": "proposed", "at": _iso_hours_ago(100), "by": "alice"},
            {"status": "accepted", "at": _iso_hours_ago(96), "by": "bob"},
            {"status": "evidence_submitted", "at": submitted_at, "by": "alice"},
            {"event": "counterparty_review", "at": _iso_hours_ago(1), "by": "bob"},
        ],
    )

    changed = _expire_obligations([obl])

    assert changed is False
    assert obl["status"] == "evidence_submitted"
    assert not obl.get("timeout_elapsed")
