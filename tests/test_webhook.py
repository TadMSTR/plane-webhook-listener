"""
End-to-end verification tests for the Plane webhook listener (TQMCP-3).

Covers:
  - HMAC-SHA256 signature verification (accept / reject)
  - Non-issue events are silently dropped
  - issue/created fires a Matrix notification
  - issue/updated with state change fires a notification
  - issue/updated with no state change is silent
  - Dedup window suppresses duplicate notifications within N seconds
  - State name resolved from inline dict vs UUID cache fallback
  - Missing PLANE_WEBHOOK_SECRET skips verification (dev mode)
"""

import hashlib
import hmac
import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FAKE_SECRET = "test-secret-abc"
FAKE_PROJECT_ID = "proj-uuid-1234"
FAKE_PROJECT_IDENTIFIER = "TQMCP"
FAKE_WORK_ITEM_ID = "work-item-uuid-5678"
FAKE_STATE_ID_BACKLOG = "state-backlog-0000"
FAKE_STATE_ID_INPROG  = "state-inprog-1111"
FAKE_STATE_ID_DONE    = "state-done-2222"


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_client(env_overrides: dict | None = None):
    """Import app fresh with patched env vars."""
    env = {
        "PLANE_WEBHOOK_SECRET": FAKE_SECRET,
        "MATRIX_ACCESS_TOKEN": "mat-token",
        "MATRIX_ROOM_ID": "!room:helmforge.me",
        "MATRIX_HOMESERVER": "http://localhost:8008",
        "PLANE_API_TOKEN": "",   # no Plane API calls in unit tests
        "PLANE_WORKSPACE": "forge",
        "PLANE_BASE_URL": "https://plane.helmforge.me",
    }
    if env_overrides:
        env.update(env_overrides)

    import importlib
    import sys

    with patch.dict("os.environ", env, clear=False):
        # Force fresh import of the module so env vars are re-read
        if "main" in sys.modules:
            del sys.modules["main"]
        import main as m  # type: ignore

        # Pre-seed the project cache so identifier lookup works
        m.project_cache[FAKE_PROJECT_ID] = FAKE_PROJECT_IDENTIFIER
        m.state_name_cache[FAKE_STATE_ID_BACKLOG] = "Backlog"
        m.state_name_cache[FAKE_STATE_ID_INPROG]  = "In Progress"
        m.state_name_cache[FAKE_STATE_ID_DONE]    = "Done"

        # Reset dedup and state caches between tests
        m.state_cache.clear()
        m.last_notified.clear()

        return TestClient(m.app), m


def _issue_payload(
    action: str = "created",
    state_id: str = FAKE_STATE_ID_BACKLOG,
    work_item_id: str = FAKE_WORK_ITEM_ID,
    project_id: str = FAKE_PROJECT_ID,
    name: str = "Fix dispatcher routing loop",
    seq: int = 1,
    state_dict: dict | None = None,
) -> dict:
    state_field = state_dict if state_dict is not None else state_id
    return {
        "event": "issue",
        "action": action,
        "data": {
            "id": work_item_id,
            "project": project_id,
            "sequence_id": seq,
            "name": name,
            "priority": "high",
            "state": state_field,
            "created_by": "ted",
        },
    }


def _post(client, payload: dict, secret: str | None = FAKE_SECRET) -> tuple:
    body = json.dumps(payload).encode()
    sig = _sign(body, secret) if secret else ""
    resp = client.post(
        "/webhook/plane",
        content=body,
        headers={"Content-Type": "application/json", "x-plane-signature": sig},
    )
    return resp


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSignatureVerification:
    def test_valid_signature_accepted(self):
        client, _ = _make_client()
        resp = _post(client, _issue_payload())
        assert resp.status_code == 200

    def test_invalid_signature_rejected(self):
        client, _ = _make_client()
        payload = _issue_payload()
        body = json.dumps(payload).encode()
        resp = client.post(
            "/webhook/plane",
            content=body,
            headers={"Content-Type": "application/json", "x-plane-signature": "bad-sig"},
        )
        assert resp.status_code == 401

    def test_no_secret_configured_skips_verification(self):
        """With no secret set, any (or empty) signature is accepted."""
        client, _ = _make_client({"PLANE_WEBHOOK_SECRET": ""})
        resp = _post(client, _issue_payload(), secret=None)
        assert resp.status_code == 200


class TestNonIssueEvents:
    def test_cycle_event_ignored(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        payload = {"event": "cycle", "action": "created", "data": {}}
        resp = _post(client, payload)
        assert resp.status_code == 200
        assert len(messages_sent) == 0

    def test_module_event_ignored(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        payload = {"event": "module", "action": "updated", "data": {}}
        resp = _post(client, payload)
        assert resp.status_code == 200
        assert len(messages_sent) == 0


class TestIssueCreated:
    def test_created_fires_matrix_notification(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        resp = _post(client, _issue_payload(action="created", seq=1))
        assert resp.status_code == 200
        assert len(messages_sent) == 1
        msg = messages_sent[0]
        assert "TQMCP-1" in msg
        assert "Fix dispatcher routing loop" in msg
        assert "created" in msg.lower() or "State:" in msg or "Priority:" in msg

    def test_created_resolves_state_name_from_cache(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        resp = _post(client, _issue_payload(action="created", state_id=FAKE_STATE_ID_BACKLOG))
        assert resp.status_code == 200
        assert "Backlog" in messages_sent[0]

    def test_created_resolves_state_name_from_inline_dict(self):
        """state field may arrive as {id: ..., name: ...} dict instead of bare UUID."""
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        state_dict = {"id": "some-unknown-uuid", "name": "Triage"}
        resp = _post(client, _issue_payload(
            action="created", state_dict=state_dict,
        ))
        assert resp.status_code == 200
        assert "Triage" in messages_sent[0]

    def test_created_includes_plane_url(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        resp = _post(client, _issue_payload(action="created", seq=42))
        assert resp.status_code == 200
        assert "TQMCP-42" in messages_sent[0]
        assert "plane.helmforge.me" in messages_sent[0]


class TestIssueUpdated:
    def test_state_change_fires_notification(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        # Seed the old state in the cache
        m.state_cache[FAKE_WORK_ITEM_ID] = FAKE_STATE_ID_BACKLOG

        resp = _post(client, _issue_payload(
            action="updated", state_id=FAKE_STATE_ID_INPROG,
        ))
        assert resp.status_code == 200
        assert len(messages_sent) == 1
        msg = messages_sent[0]
        assert "Backlog" in msg
        assert "In Progress" in msg
        assert "→" in msg

    def test_no_state_change_silent(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        # Old and new state are the same
        m.state_cache[FAKE_WORK_ITEM_ID] = FAKE_STATE_ID_INPROG

        resp = _post(client, _issue_payload(
            action="updated", state_id=FAKE_STATE_ID_INPROG,
        ))
        assert resp.status_code == 200
        assert len(messages_sent) == 0

    def test_done_state_includes_checkmark(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        m.state_cache[FAKE_WORK_ITEM_ID] = FAKE_STATE_ID_INPROG

        resp = _post(client, _issue_payload(
            action="updated", state_id=FAKE_STATE_ID_DONE,
        ))
        assert resp.status_code == 200
        assert "✓" in messages_sent[0]

    def test_unknown_project_shows_unknown_identifier(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        m.state_cache["wid-unknown"] = FAKE_STATE_ID_BACKLOG

        payload = _issue_payload(
            action="updated",
            state_id=FAKE_STATE_ID_INPROG,
            work_item_id="wid-unknown",
            project_id="unknown-proj-uuid",
        )
        resp = _post(client, payload)
        assert resp.status_code == 200
        # Should still send something, identifier falls back to "UNKNOWN"
        assert "UNKNOWN" in messages_sent[0]


class TestDeduplication:
    def test_duplicate_within_window_suppressed(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        payload = _issue_payload(action="created")
        _post(client, payload)
        assert len(messages_sent) == 1

        # Second identical event immediately after
        _post(client, payload)
        assert len(messages_sent) == 1, "Duplicate within dedup window should be suppressed"

    def test_duplicate_after_window_fires(self):
        client, m = _make_client()
        messages_sent = []
        m.send_matrix_message = lambda msg: messages_sent.append(msg)

        payload = _issue_payload(action="created")
        _post(client, payload)
        assert len(messages_sent) == 1

        # Manually expire the dedup window
        m.last_notified[FAKE_WORK_ITEM_ID] = 0.0

        _post(client, payload)
        assert len(messages_sent) == 2, "Event after dedup window should fire"


class TestHealthEndpoint:
    def test_health_returns_ok(self):
        client, m = _make_client()
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "projects_cached" in data
