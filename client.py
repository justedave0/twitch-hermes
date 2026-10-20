"""Twitch API client — device-activation flow, token refresh, Helix chat + EventSub.

Pure stdlib + httpx (already a Hermes dependency). No account, app or
channel is hardcoded: the client id/secret and tokens are injected per
install (see ``adapter._bundle`` / the dashboard).

Endpoints used (all documented at dev.twitch.tv):
* POST https://id.twitch.tv/oauth2/device     — start device-activation flow
* POST https://id.twitch.tv/oauth2/token      — poll / refresh (device_code, refresh_token)
* GET  https://api.twitch.tv/helix/users      — resolve login/ID
* GET  https://api.twitch.tv/helix/streams    — is the channel live?
* POST https://api.twitch.tv/helix/chat/messages       — send a chat message
* POST/GET/DELETE https://api.twitch.tv/helix/eventsub/subscriptions — EventSub (websocket transport)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

ID_URL = "https://id.twitch.tv"
HELIX_URL = "https://api.twitch.tv/helix"
WS_URL = "wss://eventsub.wss.twitch.tv/ws"
ACTIVATE_URL = "https://id.twitch.tv/activate"

# Scopes each role login must carry (device flow requests exactly these).
SCOPES_BOT = ["user:read:chat", "user:write:chat", "user:bot"]
SCOPES_BROADCASTER = ["channel:bot", "user:read:chat", "user:write:chat"]


class TwitchError(Exception):
    """Twitch API error with the HTTP status and the error detail."""

    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class TwitchClient:
    def __init__(self, client_id: str, client_secret: str = "", *, timeout: float = 15.0):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self._timeout = timeout
        if not self.client_id:
            raise ValueError("Twitch client_id is required")

    # ── low-level ────────────────────────────────────────────────────────

    async def _request(
        self, method: str, url: str, *, headers: Optional[dict] = None,
        data: Optional[dict] = None, params: Optional[dict] = None,
        json_body: Optional[dict] = None,
    ) -> tuple[int, dict]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(method, url, headers=headers, data=data,
                                        params=params, json=json_body)
        try:
            payload = resp.json()
        except (ValueError, json.JSONDecodeError):  # pragma: no cover - Twitch always JSON
            payload = {"raw": resp.text[:300]}
        if not isinstance(payload, dict):
            payload = {"data": payload}
        return resp.status_code, payload

    # ── device-activation flow (no redirect URI) ─────────────────────────

    async def device_authorization(self, scopes: list[str]) -> dict:
        """POST /oauth2/device → {device_code, user_code, verification_uri, interval, expires_in}."""
        code, p = await self._request(
            "POST", f"{ID_URL}/oauth2/device",
            data={"client_id": self.client_id, "scope": " ".join(scopes)},
        )
        if code != 200:
            raise TwitchError(code, f"device authorization failed: {p}")
        return p

    async def poll_device_token(self, device_code: str) -> dict:
        """Poll /oauth2/token for the device grant.

        Returns {"status": "authorized", **token} on success, or
        {"status": "pending"|"slow_down"|"expired"|"denied"|"invalid"}.
        """
        code, p = await self._request(
            "POST", f"{ID_URL}/oauth2/token",
            data={
                "client_id": self.client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        if code == 200 and p.get("access_token"):
            p["status"] = "authorized"
            return p
        err = str(p.get("error") or f"http_{code}")
        status = {
            "authorization_pending": "pending",
            "slow_down": "slow_down",
            "expired_token": "expired",
            "authorization_declined": "denied",
            "invalid_request": "invalid",
            "invalid_grant": "invalid",
        }.get(err, "invalid")
        logger.debug("device poll: %s (%s)", err, code)
        return {"status": status}

    async def refresh_token(self, refresh_token: str) -> dict:
        """POST /oauth2/token (refresh_token grant) → {access_token, refresh_token, expires_in, ...}.

        Twitch refresh tokens are single-use; the caller must serialize
        refreshes (the adapter uses an asyncio lock).
        """
        code, p = await self._request(
            "POST", f"{ID_URL}/oauth2/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if code != 200:
            raise TwitchError(code, f"token refresh failed: {p.get('error_description') or p}")
        return p

    # ── Helix (user-token) ───────────────────────────────────────────────

    async def _helix(self, method: str, path: str, token: str, *,
                     params: Optional[dict] = None, json_body: Optional[dict] = None) -> dict:
        headers = {"Authorization": f"Bearer {token}", "Client-Id": self.client_id}
        code, p = await self._request(method, f"{HELIX_URL}{path}", headers=headers,
                                      params=params, json_body=json_body)
        if code >= 300:
            raise TwitchError(code, f"{method} {path}: {p.get('message') or p}")
        return p

    async def get_user(self, token: str, *, user_id: Optional[str] = None,
                       login: Optional[str] = None) -> Optional[dict]:
        """GET /users → first row ({id, login, display_name, ...}) or None."""
        params = {}
        if user_id:
            params["id"] = str(user_id)
        elif login:
            params["login"] = login
        else:
            return None
        p = await self._helix("GET", "/users", token, params=params)
        data = p.get("data") or []
        return data[0] if data else None

    async def get_stream(self, token: str, broadcaster_id: str) -> Optional[dict]:
        """GET /streams?user_id= → live stream object or None."""
        p = await self._helix("GET", "/streams", token, params={"user_id": str(broadcaster_id)})
        data = p.get("data") or []
        return data[0] if data else None

    async def send_chat_message(self, token: str, *, broadcaster_id: str, sender_id: str,
                                message: str) -> str:
        """POST /chat/messages → the new message's id."""
        p = await self._helix("POST", "/chat/messages", token, json_body={
            "broadcaster_id": str(broadcaster_id),
            "sender_id": str(sender_id),
            "message": message,
        })
        data = p.get("data") or []
        if not data or not data[0].get("id"):
            raise TwitchError(200, f"no message id in response: {p}")
        return str(data[0]["id"])

    # ── EventSub (websocket transport — user token only) ─────────────────

    async def create_chat_subscription(self, token: str, *, session_id: str,
                                       broadcaster_user_id: str, user_id: str) -> dict:
        """POST /eventsub/subscriptions for channel.chat.message over this websocket session."""
        p = await self._helix("POST", "/eventsub/subscriptions", token, json_body={
            "type": "channel.chat.message",
            "version": "1",
            "condition": {
                "broadcaster_user_id": str(broadcaster_user_id),
                "user_id": str(user_id),
            },
            "transport": {"method": "websocket", "session_id": session_id},
        })
        data = p.get("data") or []
        if not data:
            raise TwitchError(200, f"no subscription created: {p}")
        return data[0]

    async def get_subscriptions(self, token: str) -> list[dict]:
        p = await self._helix("GET", "/eventsub/subscriptions", token)
        return p.get("data") or []

    async def delete_subscription(self, token: str, subscription_id: str) -> None:
        await self._helix("DELETE", f"/eventsub/subscriptions/{subscription_id}", token)
