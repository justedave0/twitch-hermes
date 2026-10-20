"""TwitchClient unit tests — the HTTP layer, mocked at ``_request``.

Covers the device-activation flow (the whole point of the plugin: no
redirect URI), token refresh, /oauth2/validate identity lookup, chat send
and EventSub subscription creation.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import fresh_state, make_fake_request


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def twitch(client_mod):
    state = fresh_state()
    client = client_mod.TwitchClient("cid-test", "sec-test")
    type(client)._request = make_fake_request(state)  # class-level: binds self  # type: ignore[assignment]
    return client, state, client_mod


def test_client_requires_client_id(client_mod):
    with pytest.raises(ValueError):
        client_mod.TwitchClient("")


def test_scopes_match_the_documented_requirements(client_mod):
    # bot: read + write chat + the user:bot scope needed for EventSub
    assert client_mod.SCOPES_BOT == ["user:read:chat", "user:write:chat", "user:bot"]
    # broadcaster: channel:bot unlocks chat events for that channel
    assert client_mod.SCOPES_BROADCASTER[0] == "channel:bot"
    assert "user:write:chat" in client_mod.SCOPES_BROADCASTER  # send-as-broadcaster needs it


def test_device_authorization_returns_code_and_no_redirect(twitch):
    client, state, mod = twitch
    dev = run(client.device_authorization(mod.SCOPES_BOT))
    assert dev["user_code"] == "WXYZ-1234"
    assert dev["verification_uri"] == "https://id.twitch.tv/activate"
    assert dev["device_code"]
    assert "interval" in dev and "expires_in" in dev
    # the flow encodes the role by scopes: bot scopes → bot token later
    assert state["device_role"] == "bot"


def test_device_flow_pending_then_authorized(twitch):
    client, state, mod = twitch
    dev = run(client.device_authorization(mod.SCOPES_BROADCASTER))
    assert state["device_role"] == "broadcaster"
    first = run(client.poll_device_token(dev["device_code"], mod.SCOPES_BROADCASTER))
    assert first["status"] == "pending"
    second = run(client.poll_device_token(dev["device_code"], mod.SCOPES_BROADCASTER))
    assert second["status"] == "authorized"
    assert second["access_token"] == "tok-broadcaster"
    assert second["refresh_token"] == "ref-broadcaster"


def test_device_flow_expired_maps_to_expired_status(client_mod):
    client = client_mod.TwitchClient("cid", "sec")

    async def fake(self, method, url, **kw):
        return 400, {"status": 400, "message": "expired_token"}  # Twitch's real wire shape

    type(client)._request = fake  # type: ignore[assignment]
    assert run(client.poll_device_token("dev-x", client_mod.SCOPES_BOT))["status"] == "expired"


def test_refresh_token_success_and_failure(twitch):
    client, state, mod = twitch
    data = run(client.refresh_token("ref-bot"))
    assert data["access_token"] == "tok-fresh-ref-bot"
    assert data["refresh_token"] == "ref-new"

    async def reject(self, method, url, **kw):
        return 400, {"error": "invalid_grant", "error_description": "refresh token used"}

    type(client)._request = reject  # type: ignore[assignment]
    with pytest.raises(mod.TwitchError):
        run(client.refresh_token("ref-bot"))


def test_validate_token_returns_identity(twitch):
    client, state, mod = twitch
    info = run(client.validate_token("tok-bot"))
    assert info["user_id"] == "111"
    assert info["login"] == "davegpt"
    info2 = run(client.validate_token("tok-broadcaster"))
    assert info2["user_id"] == "222"
    assert info2["login"] == "justedave"


def test_validate_bad_token_raises(twitch):
    client, state, mod = twitch
    with pytest.raises(mod.TwitchError):
        run(client.validate_token("tok-nobody"))


def test_send_chat_message_returns_id_and_posts_required_fields(twitch):
    client, state, mod = twitch
    mid = run(client.send_chat_message("tok-bot", broadcaster_id="222",
                                       sender_id="111", message="hello"))
    assert mid == "msg-1"
    body = state["sends"][0]
    assert body == {"broadcaster_id": "222", "sender_id": "111", "message": "hello"}


def test_send_chat_message_without_id_raises(twitch):
    client, state, mod = twitch

    async def no_id(self, method, url, **kw):
        return 200, {"data": []}

    type(client)._request = no_id  # type: ignore[assignment]
    with pytest.raises(mod.TwitchError):
        run(client.send_chat_message("tok-bot", broadcaster_id="222",
                                     sender_id="111", message="x"))


def test_send_chat_message_reads_real_twitch_wire_shape(twitch):
    """REGRESSION: POST /chat/messages returns {"data": [{"message_id",
    "is_sent", "drop_reason"}]} — the id key is "message_id", not "id".
    The old code read data[0]["id"], so EVERY successful send (the message
    visibly landed in chat) was reported as
    'HTTP 200: no message id in response' to the user."""
    client, state, mod = twitch

    async def real(self, method, url, **kw):
        return 200, {"data": [{"message_id": "fbb45d5a-dd6c-40c7-9c96-b5cb71f88adf",
                               "is_sent": True, "drop_reason": None}]}

    type(client)._request = real  # type: ignore[assignment]
    mid = run(client.send_chat_message("tok-bot", broadcaster_id="222",
                                       sender_id="111", message="hello"))
    assert mid == "fbb45d5a-dd6c-40c7-9c96-b5cb71f88adf"


def test_send_chat_message_dropped_raises(twitch):
    """is_sent=False means Twitch dropped the message (raid, rate limit, ...)
    — reporting its id as success would lie about what happened."""
    client, state, mod = twitch

    async def dropped(self, method, url, **kw):
        return 200, {"data": [{"message_id": "x", "is_sent": False,
                               "drop_reason": "raid"}]}

    type(client)._request = dropped  # type: ignore[assignment]
    with pytest.raises(mod.TwitchError, match="dropped"):
        run(client.send_chat_message("tok-bot", broadcaster_id="222",
                                     sender_id="111", message="x"))


def test_poll_pending_reads_twitch_message_field(twitch):
    """REGRESSION: Twitch reports device-flow errors in the "message" field
    ({"status": 400, "message": "authorization_pending"}), not the
    RFC-standard "error" field. A pending poll must map to "pending" —
    before the fix it fell through to "invalid" and the dashboard killed
    the pairing card on the first poll (~5 s after Connect)."""
    client, state, mod = twitch

    async def pending(self, method, url, **kw):
        return 400, {"status": 400, "message": "authorization_pending"}

    type(client)._request = pending  # type: ignore[assignment]
    assert run(client.poll_device_token("dev-x", mod.SCOPES_BOT))["status"] == "pending"


def test_poll_all_twitch_error_shapes_map_correctly(twitch):
    """Every documented device-flow error, in Twitch's real wire shape."""
    client, state, mod = twitch
    cases = {
        "slow_down": "slow_down",
        "expired_token": "expired",
        "authorization_declined": "denied",
        "invalid_request": "invalid",
        "invalid_grant": "invalid",
    }
    for wire_msg, want in cases.items():
        async def fake(self, method, url, _m=wire_msg, **kw):
            return 400, {"status": 400, "message": _m}

        type(client)._request = fake  # type: ignore[assignment]
        assert run(client.poll_device_token("dev-x", mod.SCOPES_BOT))["status"] == want, wire_msg


def test_device_flow_uses_plural_scopes_param_in_both_requests(twitch):
    """REGRESSION: Twitch's device flow takes ``scopes`` (PLURAL,
    space-delimited) in BOTH the /oauth2/device request and the
    /oauth2/token poll request. The singular RFC ``scope`` (or an omitted
    param) silently yields a token with an EMPTY scope grant — it validates
    fine but every chat send 401s. This is the costliest lesson in this
    project: it survived a full pairing and only surfaced on the first send.
    """
    client, state, mod = twitch
    captured = {}

    async def spy(self, method, url, **kw):
        if url.endswith("/oauth2/device") or url.endswith("/oauth2/token"):
            captured[url] = dict(kw.get("data") or {})
        return await make_fake_request(state)(self, method, url, **kw)

    type(client)._request = spy  # type: ignore[assignment]
    dev = run(client.device_authorization(mod.SCOPES_BOT))
    run(client.poll_device_token(dev["device_code"], mod.SCOPES_BOT))

    dev_req = captured[f"{mod.ID_URL}/oauth2/device"]
    tok_req = captured[f"{mod.ID_URL}/oauth2/token"]
    assert dev_req.get("scopes") == " ".join(mod.SCOPES_BOT), dev_req
    assert "scope" not in dev_req, "singular 'scope' produces an empty grant"
    assert tok_req.get("scopes") == " ".join(mod.SCOPES_BOT), tok_req
    assert "scope" not in tok_req, "the token request MUST carry 'scopes' too"


def test_create_chat_subscription_uses_websocket_transport(twitch):
    client, state, mod = twitch

    captured = {}

    async def spy(self, method, url, **kw):
        if url.endswith("/eventsub/subscriptions") and method == "POST":
            captured.update(kw.get("json_body") or {})
        return await make_fake_request(state)(self, method, url, **kw)

    type(client)._request = spy  # type: ignore[assignment]
    sub = run(client.create_chat_subscription("tok-bot", session_id="sess-1",
                                              broadcaster_user_id="222", user_id="111"))
    assert sub["id"] == "sub-1"
    assert captured["type"] == "channel.chat.message"
    assert captured["transport"] == {"method": "websocket", "session_id": "sess-1"}
    assert captured["condition"] == {"broadcaster_user_id": "222", "user_id": "111"}
