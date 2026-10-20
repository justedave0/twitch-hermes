"""Dashboard API integration tests.

Runs the REAL ``dashboard/plugin_api.py`` router through a real FastAPI app
(TestClient), against the in-memory hermes storage twins and the fake Twitch
HTTP backend. This is the surface a user drives from the browser: save app
credentials, start device flows for both roles, poll them to completion,
verify the channel gets set, toggle behavior, send a test message.

These tests exist because the live-tab flow broke three ways before it was
ever exercised: a missing Content-Type header (FastAPI 422s text/plain
bodies), a dead controlled secret input, and a connect flow that stored an
empty identity.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from conftest import fresh_state, make_fake_request


@pytest.fixture
def app_and_state(api_mod):
    state = fresh_state()
    # Patch the standalone client copy that plugin_api loads for itself.
    standalone = api_mod._get_client()
    standalone.TwitchClient._request = make_fake_request(state)  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(api_mod.router)
    return TestClient(app), state


def test_status_initial_is_empty(app_and_state, store):
    client, state = app_and_state
    r = client.get("/status")
    assert r.status_code == 200
    d = r.json()
    assert d["app"]["client_id"] == ""
    assert d["app"]["has_secret"] is False
    assert d["channel"] is None
    for role in ("bot", "broadcaster"):
        assert d["accounts"][role]["has_token"] is False
        assert d["accounts"][role]["user_id"] == ""


def test_app_save_persists_secret_and_never_echoes_it(app_and_state, store):
    client, state = app_and_state
    r = client.post("/app", json={"client_id": "cid-test", "client_secret": "sec-test"})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "has_secret": True}
    assert store.env["TWITCH_CLIENT_SECRET"] == "sec-test"
    assert store.settings["client_id"] == "cid-test"

    # The secret must never appear in any response body.
    status = client.get("/status").json()
    assert "sec-test" not in json.dumps(status)
    assert status["app"]["has_secret"] is True


def test_app_save_requires_client_id(app_and_state):
    client, state = app_and_state
    r = client.post("/app", json={"client_id": "  ", "client_secret": "x"})
    assert r.status_code == 400


def test_connect_requires_app_credentials(app_and_state):
    client, state = app_and_state
    r = client.post("/connect", json={"role": "bot"})
    assert r.status_code == 400
    assert "credentials" in r.json()["detail"]


def test_connect_rejects_unknown_role(app_and_state, store):
    client, state = app_and_state
    store.settings["client_id"] = "cid"
    store.env["TWITCH_CLIENT_SECRET"] = "sec"
    r = client.post("/connect", json={"role": "moderator"})
    assert r.status_code == 400


def test_connect_broadcaster_full_flow_sets_channel(app_and_state, store):
    client, state = app_and_state
    store.settings["client_id"] = "cid"
    store.env["TWITCH_CLIENT_SECRET"] = "sec"

    r = client.post("/connect", json={"role": "broadcaster"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["role"] == "broadcaster"
    assert d["user_code"] == "WXYZ-1234"
    assert d["verification_uri"] == "https://id.twitch.tv/activate"
    flow_id = d["flow_id"]

    # First poll: pending (the fake needs one pending poll).
    p1 = client.get(f"/connect/{flow_id}").json()
    assert p1["status"] == "pending"

    # Second poll: authorized — identity must come from /oauth2/validate.
    p2 = client.get(f"/connect/{flow_id}").json()
    assert p2["status"] == "authorized"
    assert p2["login"] == "justedave"
    assert p2["user_id"] == "222"
    assert p2.get("channel_set") is True

    # Stored state: token, identity, channel, and the cron home channel.
    assert store.env["TWITCH_BROADCASTER_ACCESS_TOKEN"] == "tok-broadcaster"
    assert store.env["TWITCH_BROADCASTER_REFRESH_TOKEN"] == "ref-broadcaster"
    assert store.settings["accounts"]["broadcaster"]["user_id"] == "222"
    assert store.settings["channel"] == {"user_id": "222", "login": "justedave"}
    assert store.env["TWITCH_HOME_CHANNEL"] == "222"

    # The flow is single-use.
    p3 = client.get(f"/connect/{flow_id}")
    assert p3.status_code == 404


def test_connect_bot_flow_stores_bot_identity(app_and_state, store):
    client, state = app_and_state
    store.settings["client_id"] = "cid"
    store.env["TWITCH_CLIENT_SECRET"] = "sec"

    d = client.post("/connect", json={"role": "bot"}).json()
    flow_id = d["flow_id"]
    client.get(f"/connect/{flow_id}")  # pending
    p2 = client.get(f"/connect/{flow_id}").json()
    assert p2["status"] == "authorized"
    assert p2["login"] == "davegpt"
    assert p2["user_id"] == "111"
    assert "channel_set" not in p2  # the bot login must NOT set the channel
    assert store.settings.get("channel") in (None, {})


def test_unknown_flow_404s(app_and_state):
    client, state = app_and_state
    r = client.get("/connect/not-a-real-flow")
    assert r.status_code == 404


def test_settings_roundtrip_and_env_gates(app_and_state, store):
    client, state = app_and_state
    r = client.put("/settings", json={
        "send_as": "broadcaster", "response_mode": "all",
        "allowed_users": "111, dave",
        "allow_all_users": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["restart_required"] is True  # allowlist is gateway-side

    status = client.get("/status").json()["settings"]
    assert status["send_as"] == "broadcaster"
    assert status["response_mode"] == "all"
    assert store.env["TWITCH_ALLOWED_USERS"] == "111, dave"
    assert store.env["TWITCH_ALLOW_ALL_USERS"] == "true"

    # Invalid values are rejected.
    assert client.put("/settings", json={"send_as": "mod"}).status_code == 400
    assert client.put("/settings", json={"response_mode": "maybe"}).status_code == 400


def test_send_as_toggle_applies_live_to_bundle(adapter_pkg, store):
    """The adapter re-reads settings (2s cache) — a dashboard toggle of
    send_as must flip the identity the adapter will send under, no restart."""
    store.settings["send_as"] = "bot"
    b = adapter_pkg._bundle()
    assert b["send_as"] == "bot"
    store.settings["send_as"] = "broadcaster"
    adapter_pkg._settings_cache["at"] = 0.0  # force a re-read
    b = adapter_pkg._bundle()
    assert b["send_as"] == "broadcaster"


def test_test_message_sends_as_active_identity(app_and_state, store):
    client, state = app_and_state
    store.settings["client_id"] = "cid"
    store.env["TWITCH_CLIENT_SECRET"] = "sec"
    # Connect both roles (broadcaster sets the channel).
    for role in ("broadcaster", "bot"):
        d = client.post("/connect", json={"role": role}).json()
        client.get(f"/connect/{d['flow_id']}")  # pending
        client.get(f"/connect/{d['flow_id']}")  # authorized

    r = client.post("/test", json={"text": "hello from the test suite"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] is True
    assert d["as"] == "bot"  # default send-as
    assert d["channel"] == "justedave"
    assert state["sends"], "a chat message must have been sent"
    assert state["sends"][0]["message"] == "hello from the test suite"
    assert state["sends"][0]["broadcaster_id"] == "222"
    assert state["sends"][0]["sender_id"] == "111"


def test_test_message_reports_missing_channel(app_and_state, store):
    client, state = app_and_state
    r = client.post("/test", json={"text": "hi"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert "broadcaster" in r.json()["error"]


def test_delete_account_clears_tokens_and_channel(app_and_state, store):
    client, state = app_and_state
    store.settings["client_id"] = "cid"
    store.env["TWITCH_CLIENT_SECRET"] = "sec"
    d = client.post("/connect", json={"role": "broadcaster"}).json()
    client.get(f"/connect/{d['flow_id']}")
    client.get(f"/connect/{d['flow_id']}")
    assert store.env["TWITCH_BROADCASTER_ACCESS_TOKEN"] == "tok-broadcaster"

    r = client.request("DELETE", "/account", json={"role": "broadcaster"})
    assert r.status_code == 200, r.text
    assert "TWITCH_BROADCASTER_ACCESS_TOKEN" not in store.env
    assert "TWITCH_BROADCASTER_REFRESH_TOKEN" not in store.env
    assert store.settings.get("channel") in (None, {})
    assert "TWITCH_HOME_CHANNEL" not in store.env


def test_status_never_leaks_secrets(app_and_state, store):
    client, state = app_and_state
    store.settings["client_id"] = "cid"
    store.env["TWITCH_CLIENT_SECRET"] = "top-secret-xyz"
    d = client.post("/connect", json={"role": "bot"}).json()
    client.get(f"/connect/{d['flow_id']}")
    client.get(f"/connect/{d['flow_id']}")
    blob = json.dumps(client.get("/status").json())
    assert "top-secret-xyz" not in blob
    assert "tok-bot" not in blob
    assert "ref-bot" not in blob
