"""Twitch platform adapter — EventSub WebSocket in, Helix chat out.

Config model (all per-install, never hardcoded — see README):
* App credentials: client_id (plugin settings) + TWITCH_CLIENT_SECRET (.env)
* Channel:         settings.channel = {user_id, login}  (set when the broadcaster logs in)
* Bot account:     TWITCH_BOT_ACCESS_TOKEN / TWITCH_BOT_REFRESH_TOKEN + settings.accounts.bot
* Broadcaster:     TWITCH_BROADCASTER_ACCESS_TOKEN / TWITCH_BROADCASTER_REFRESH_TOKEN
                   + settings.accounts.broadcaster
* send_as:         "bot" (default) | "broadcaster"  — which identity the agent sends under
* response_mode:   "mention" (default, @<name> required) | "all"
* mention_names:   display logins to react to in mention mode
* Allowlist:       TWITCH_ALLOWED_USERS / TWITCH_ALLOW_ALL_USERS (gateway authz, env)

Why WebSocket and not webhook: the WebSocket transport is an OUTBOUND
connection to wss://eventsub.wss.twitch.tv/ws — no public URL, no callback
secret, no inbound port. It only accepts user access tokens, which is
exactly what the two device-flow logins produce.

Identity & echo model:
* The bot account's user id is always dropped (it is our own voice).
* The broadcaster IS the streamer — their messages are NOT dropped.
* Messages we just sent (as whichever identity) are dropped by their
  message id, so the send-as toggle can never create an echo loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret, send_error

try:
    import websockets  # noqa: F401
    WS_AVAILABLE = True
except ImportError:
    WS_AVAILABLE = False

try:
    import httpx  # noqa: F401
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

from . import client as _tc  # TwitchClient, scopes, constants

logger = logging.getLogger(__name__)

PLUGIN_ID = "twitch-platform"
MAX_MESSAGE_LENGTH = 500  # Twitch chat message cap
DEDUP_TTL_SECONDS = 300
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
WS_KEEPALIVE_TIMEOUT = 30  # seconds requested from Twitch (valid: 10–600)
TOKEN_REFRESH_MARGIN = 300  # refresh 5 min before expiry
SENT_ID_TTL = 120  # keep our sent message ids for echo protection

_ROLES = ("bot", "broadcaster")


# ──────────────────────────────────────────────────────────────────────
# Config bundle (settings + scoped secrets), live-re-read with a short
# cache so dashboard toggles (send_as, response_mode) apply without a
# gateway restart.
# ──────────────────────────────────────────────────────────────────────

_settings_cache: dict[str, Any] = {"at": 0.0, "value": {}}


def _secret(name: str) -> str:
    v = _get_scoped_secret(name, "")
    return v.strip() if isinstance(v, str) else ""


def _load_settings() -> dict[str, Any]:
    now = time.time()
    if now - _settings_cache["at"] < 2.0:
        return _settings_cache["value"]
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        cfg = {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    value = entry.get("settings") or {}
    _settings_cache.update(at=now, value=value)
    return value


def _bundle() -> dict[str, Any]:
    """The whole per-install state the adapter needs, fresh from disk (2s cache)."""
    s = _load_settings()
    accounts_raw = s.get("accounts") or {}
    accounts = {}
    for role in _ROLES:
        a = accounts_raw.get(role) or {}
        accounts[role] = {
            "access_token": _secret(f"TWITCH_{role.upper()}_ACCESS_TOKEN"),
            "refresh_token": _secret(f"TWITCH_{role.upper()}_REFRESH_TOKEN"),
            "user_id": str(a.get("user_id") or ""),
            "login": str(a.get("login") or ""),
            "display_name": str(a.get("display_name") or ""),
        }
    return {
        "client_id": str(s.get("client_id") or ""),
        "client_secret": _secret("TWITCH_CLIENT_SECRET"),
        "channel": s.get("channel") or {},
        "send_as": s.get("send_as") if s.get("send_as") in _ROLES else "bot",
        "response_mode": s.get("response_mode") if s.get("response_mode") in ("mention", "all") else "mention",
        "mention_names": [str(n) for n in (s.get("mention_names") or []) if str(n).strip()],
        "token_expires": s.get("token_expires") or {},
        "accounts": accounts,
    }


def _save_settings(key: str, value: Any) -> None:
    from hermes_cli.plugins_state import save_plugin_setting
    save_plugin_setting(PLUGIN_ID, (key,), value)
    _settings_cache["at"] = 0.0  # invalidate so the 2s cache picks it up


def _config_gaps(b: dict[str, Any]) -> list[str]:
    gaps = []
    if not (b["client_id"] and b["client_secret"]):
        gaps.append("app credentials (client id + secret)")
    if not str(b["channel"].get("user_id") or ""):
        gaps.append("channel (connect your broadcaster account)")
    if not b["accounts"]["bot"]["access_token"]:
        gaps.append("bot account")
    if not b["accounts"]["broadcaster"]["access_token"]:
        gaps.append("broadcaster account")
    return gaps


def _store_tokens(role: str, data: dict[str, Any]) -> None:
    """Persist a fresh access/refresh token pair (gateway-process safe)."""
    from hermes_cli.credential_lifecycle import save_provider_env_credential
    save_provider_env_credential(f"TWITCH_{role.upper()}_ACCESS_TOKEN", data["access_token"])
    save_provider_env_credential(f"TWITCH_{role.upper()}_REFRESH_TOKEN", data["refresh_token"])
    s = _load_settings()
    te = dict(s.get("token_expires") or {})
    te[role] = time.time() + float(data.get("expires_in") or 21600)
    _save_settings("token_expires", te)
    logger.info("[twitch] refreshed %s token (expires in %ss)", role, data.get("expires_in"))


def check_requirements() -> bool:
    """Passive probe: deps importable RIGHT NOW (never installs)."""
    return WS_AVAILABLE and HTTPX_AVAILABLE


def validate_config(config) -> bool:
    return not _config_gaps(_bundle())


def is_connected(config) -> bool:
    return validate_config(config)


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from the per-install bundle BEFORE adapter
    construction so `gateway status` reflects a dashboard-configured install."""
    b = _bundle()
    if _config_gaps(b):
        return None
    return {
        "client_id": b["client_id"],
        "channel_user_id": str(b["channel"].get("user_id") or ""),
        "channel_login": str(b["channel"].get("login") or ""),
        "send_as": b["send_as"],
        "response_mode": b["response_mode"],
        "mention_names": b["mention_names"],
    }


# ──────────────────────────────────────────────────────────────────────
# Adapter
# ──────────────────────────────────────────────────────────────────────


class TwitchAdapter(BasePlatformAdapter):
    """EventSub WebSocket listener in, Helix chat out, one channel."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH

    def __init__(self, config: PlatformConfig):
        super().__init__(config=config, platform=Platform("twitch"))
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE, ttl_seconds=DEDUP_TTL_SECONDS)
        self._sent_ids: dict[str, float] = {}  # message id → ts (echo protection)
        self._ws_task: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._reconnect_url: Optional[str] = None
        self._sub_id: Optional[str] = None
        self._refresh_lock = asyncio.Lock()
        self._client: Optional[_tc.TwitchClient] = None
        self._last_event_at: Optional[float] = None

    # ── lifecycle ────────────────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not WS_AVAILABLE or not HTTPX_AVAILABLE:
            missing = "websockets" if not WS_AVAILABLE else "httpx"
            logger.error("[%s] dependency missing: %s (pip install %s)", self.name, missing, missing)
            self._set_fatal_error("twitch_deps_missing", f"Missing Python dependency: {missing}", retryable=False)
            return False
        b = _bundle()
        gaps = _config_gaps(b)
        if gaps:
            self._set_fatal_error(
                "twitch_not_configured",
                "Twitch is not fully configured: " + ", ".join(gaps) +
                ". Complete setup in the dashboard (Twitch tab) — app credentials, "
                "broadcaster login, bot login.",
                retryable=False)
            return False
        try:
            self._client = _tc.TwitchClient(b["client_id"], b["client_secret"])
            self._ws_task = asyncio.create_task(self._run_ws())
            self._mark_connected()
            self._wire_plugin_handlers(None)
            logger.info("[%s] up — listening on channel %s (send_as=%s, mode=%s)",
                        self.name, b["channel"].get("login") or b["channel"].get("user_id"),
                        b["send_as"], b["response_mode"])
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("[%s] failed to connect: %s", self.name, e)
            return False

    async def disconnect(self) -> None:
        self._running = False
        self._mark_disconnected()
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
            self._ws_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._client and self._sub_id:
            try:
                await self._client.delete_subscription(
                    _bundle()["accounts"]["bot"]["access_token"], self._sub_id)
            except Exception:  # noqa: BLE001
                logger.debug("[%s] stale subscription cleanup failed", self.name)
        self._sub_id = None
        self._dedup.clear()
        logger.info("[%s] disconnected", self.name)

    # ── websocket session loop ───────────────────────────────────────────

    async def _run_ws(self) -> None:
        if not WS_AVAILABLE:  # connect() guards this; belt and braces for the task
            logger.error("[%s] websockets library not available", self.name)
            return
        backoff = 0
        while self._running:
            b = _bundle()
            if _config_gaps(b):
                logger.warning("[%s] config lost mid-run — stopping (reconfigure in dashboard)", self.name)
                self._set_fatal_error("twitch_not_configured", "Twitch config is incomplete.", retryable=False)
                return
            url = self._reconnect_url or f"{_tc.WS_URL}?keepalive_timeout_seconds={WS_KEEPALIVE_TIMEOUT}"
            migrating = self._reconnect_url is not None
            self._reconnect_url = None
            try:
                await self._session(url, b)
                backoff = 0
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                if not self._running:
                    return
                logger.warning("[%s] EventSub WebSocket error: %s", self.name, e)
            if not self._running:
                return
            if migrating:
                continue  # Twitch handed us a fresh URL — hop immediately, no backoff
            delay = RECONNECT_BACKOFF[min(backoff, len(RECONNECT_BACKOFF) - 1)]
            backoff += 1
            logger.info("[%s] reconnecting to EventSub in %ds", self.name, delay)
            await asyncio.sleep(delay)

    async def _session(self, url: str, b: dict[str, Any]) -> None:
        bot_token = await self._fresh_token("bot")
        assert self._client is not None
        async with websockets.connect(
            url, open_timeout=20, ping_interval=20, ping_timeout=20, close_timeout=5,
        ) as ws:
            self._ws = ws
            welcome_raw = await asyncio.wait_for(ws.recv(), timeout=30)
            welcome = json.loads(welcome_raw)
            sess = (welcome.get("payload") or {}).get("session") or {}
            session_id = sess.get("id")
            keepalive = float(sess.get("keepalive_timeout_seconds") or WS_KEEPALIVE_TIMEOUT)
            if not session_id:
                raise RuntimeError(f"no session id in welcome: {welcome_raw[:200]}")
            # Subscribe within the welcome window (default 10s at Twitch).
            await self._subscribe(bot_token, session_id, b)
            logger.info("[%s] EventSub session %s active (keepalive %.0fs)",
                        self.name, session_id[:12], keepalive)
            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=keepalive * 2)
                except asyncio.TimeoutError:
                    logger.warning("[%s] keepalive timeout — reconnecting", self.name)
                    return
                try:
                    frame = json.loads(raw)
                except (ValueError, json.JSONDecodeError):
                    continue
                await self._handle_frame(frame, keepalive)
            self._ws = None

    async def _handle_frame(self, frame: dict[str, Any], keepalive: float) -> None:
        meta = frame.get("metadata") or {}
        mtype = meta.get("message_type")
        if mtype == "session_keepalive":
            return
        if mtype == "session_reconnect":
            sess = (frame.get("payload") or {}).get("session") or {}
            new_url = sess.get("reconnect_url")
            if new_url:
                logger.info("[%s] Twitch requests edge swap — migrating to new URL", self.name)
                self._reconnect_url = new_url
                self._sub_id = None  # subscription moves with the new session; we re-subscribe
                return
        elif mtype == "notification":
            payload = frame.get("payload") or {}
            event = payload.get("event") or {}
            if not event:
                return
            self._last_event_at = time.time()
            sub = payload.get("subscription") or {}
            sub_type = meta.get("subscription_type") or sub.get("type")
            if sub_type == "channel.chat.message" or "message" in event:
                asyncio.create_task(self._on_chat_message(event))
        elif mtype == "revocation":
            sub = (frame.get("payload") or {}).get("subscription") or {}
            logger.error("[%s] Twitch revoked our subscription (status=%s)", self.name, sub.get("status"))
            self._sub_id = None

    async def _subscribe(self, token: str, session_id: str, b: dict[str, Any]) -> None:
        chan = b["channel"]
        bot = b["accounts"]["bot"]
        assert self._client is not None
        # Best-effort: drop stale websocket subscriptions for this channel
        # (a crashed previous session may have left one behind).
        try:
            for sub in await self._client.get_subscriptions(token):
                cond = sub.get("condition") or {}
                transport = sub.get("transport") or {}
                if (transport.get("method") == "websocket"
                        and cond.get("broadcaster_user_id") == str(chan.get("user_id"))):
                    await self._client.delete_subscription(token, str(sub["id"]))
                    logger.info("[%s] removed stale subscription %s", self.name, sub["id"])
        except Exception as e:  # noqa: BLE001
            logger.debug("[%s] stale subscription scan: %s", self.name, e)
        sub = await self._client.create_chat_subscription(
            token, session_id=session_id,
            broadcaster_user_id=str(chan["user_id"]), user_id=str(bot["user_id"]))
        self._sub_id = str(sub.get("id") or "")
        logger.info("[%s] subscribed to channel.chat.message for channel %s (sub %s)",
                    self.name, chan.get("login") or chan.get("user_id"), self._sub_id)

    # ── token freshness (single-flight refresh) ──────────────────────────

    async def _fresh_token(self, role: str) -> str:
        b = _bundle()
        acc = b["accounts"][role]
        token, expires_at = acc["access_token"], float(b["token_expires"].get(role) or 0)
        if token and time.time() < expires_at - TOKEN_REFRESH_MARGIN:
            return token
        async with self._refresh_lock:
            b = _bundle()
            acc = b["accounts"][role]
            token, expires_at = acc["access_token"], float(b["token_expires"].get(role) or 0)
            if token and time.time() < expires_at - TOKEN_REFRESH_MARGIN:
                return token
            if not acc["refresh_token"]:
                raise RuntimeError(
                    f"Twitch {role} token expired and no refresh token is stored — "
                    f"reconnect the {role} account in the dashboard.")
            assert self._client is not None
            data = await self._client.refresh_token(acc["refresh_token"])
            _store_tokens(role, data)
            return str(data["access_token"])

    # ── inbound ──────────────────────────────────────────────────────────

    def _remember_sent(self, message_id: str) -> None:
        if not message_id:
            return
        self._sent_ids[message_id] = time.time()
        now = time.time()
        for mid in [m for m, ts in self._sent_ids.items() if now - ts > SENT_ID_TTL]:
            self._sent_ids.pop(mid, None)

    def _mentions_us(self, text: str, b: dict[str, Any]) -> bool:
        # Case-insensitive: both sides are lowercased. Besides the configured
        # mention names we always match the active account's login AND display
        # name (Twitch tab-autocomplete sends the display name, which may
        # differ in case/spacing, e.g. "Dave GPT" for login "davegpt").
        names = [n.lower() for n in b["mention_names"] if str(n).strip()]
        acc = b["accounts"][b["send_as"]]
        for extra in (acc.get("login"), acc.get("display_name")):
            extra = str(extra or "").lower()
            if extra and extra not in names:
                names.append(extra)
        names = [n for n in names if n]
        if not names:
            return False
        low = text.lower()
        for n in names:
            if re.search(r"@" + re.escape(n) + r"(?![a-z0-9_])", low):
                return True
        return False

    async def _on_chat_message(self, event: dict[str, Any]) -> None:
        b = _bundle()
        msg_id = str(event.get("message_id") or uuid.uuid4().hex)
        if self._dedup.is_duplicate(msg_id):
            return
        text = (event.get("message") or "").strip()
        if not text:
            return
        sender_id = str(event.get("user_id") or "")
        # Our own voice: the bot account is always ours; a message we just
        # sent (possibly as the broadcaster) is ours by id.
        if sender_id and sender_id == b["accounts"]["bot"]["user_id"]:
            return
        if msg_id in self._sent_ids:
            return
        if b["response_mode"] == "mention" and not self._mentions_us(text, b):
            logger.debug("[%s] mention mode — %s did not mention us, dropping",
                         self.name, event.get("user_login"))
            return
        chan = b["channel"]
        source = self.build_source(
            chat_id=str(chan.get("user_id") or ""),
            chat_name=str(chan.get("login") or chan.get("user_id") or ""),
            chat_type="channel",
            user_id=sender_id or str(event.get("user_login") or ""),
            user_name=str(event.get("user_name") or event.get("user_login") or ""),
            message_id=msg_id,
        )
        message_event = MessageEvent(
            text=text, message_type=MessageType.TEXT, source=source,
            message_id=msg_id, raw_message=event,
        )
        logger.debug("[%s] %s: %.80s", self.name, event.get("user_login"), text)
        await self.handle_message(message_event)

    # ── outbound ─────────────────────────────────────────────────────────

    async def send(
        self, chat_id: str, content: str,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        b = _bundle()
        chan = b["channel"]
        if not str(chan.get("user_id") or ""):
            return SendResult(success=False, error="Twitch: no channel configured")
        role = b["send_as"]
        acc = b["accounts"][role]
        if not acc["access_token"]:
            return SendResult(success=False, error=f"Twitch: no {role} account connected")
        if not acc["user_id"]:
            return SendResult(success=False, error=f"Twitch: {role} account has no user id — reconnect it")
        if not self._client:
            self._client = _tc.TwitchClient(b["client_id"], b["client_secret"])
        try:
            token = await self._fresh_token(role)
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=f"Twitch token refresh failed: {e}", retryable=True)
        try:
            message_id = await self._client.send_chat_message(
                token, broadcaster_id=str(chan["user_id"]),
                sender_id=str(acc["user_id"]), message=content[:MAX_MESSAGE_LENGTH])
        except Exception as e:  # noqa: BLE001
            return SendResult(success=False, error=str(e)[:300], retryable=True)
        self._remember_sent(message_id)
        logger.info("[%s] sent as %s (@%s): %.80s", self.name, role, acc["login"], content)
        return SendResult(success=True, message_id=message_id)

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        b = _bundle()
        chan = b["channel"]
        return {"name": str(chan.get("login") or chan.get("user_id") or chat_id), "type": "channel"}

    # ── status (used by dashboard / debugging) ───────────────────────────

    def runtime_status(self) -> dict[str, Any]:
        b = _bundle()
        gaps = _config_gaps(b)
        return {
            "connected": bool(self._running),
            "gaps": gaps,
            "channel": b["channel"].get("login") or str(b["channel"].get("user_id") or ""),
            "send_as": b["send_as"],
            "response_mode": b["response_mode"],
            "subscription_id": self._sub_id,
            "last_event_seconds_ago": (
                round(time.time() - self._last_event_at, 1) if self._last_event_at else None),
        }


# ──────────────────────────────────────────────────────────────────────
# Out-of-process sender (cron deliver=twitch / send_message tool)
# ──────────────────────────────────────────────────────────────────────


async def _standalone_send(
    pconfig, chat_id: str, message: str, *,
    thread_id: Optional[str] = None, media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Send one chat message without a live gateway (cron jobs)."""
    b = _bundle()
    gaps = _config_gaps(b)
    if gaps:
        return send_error("twitch: not fully configured — " + ", ".join(gaps))
    role = b["send_as"]
    acc = b["accounts"][role]
    chan = b["channel"]
    client = _tc.TwitchClient(b["client_id"], b["client_secret"])
    try:
        token = acc["access_token"]
        expires_at = float(b["token_expires"].get(role) or 0)
        if not token or time.time() >= expires_at - TOKEN_REFRESH_MARGIN:
            if not acc["refresh_token"]:
                return send_error(f"twitch: {role} token expired and no refresh token stored")
            data = await client.refresh_token(acc["refresh_token"])
            _store_tokens(role, data)
            token = data["access_token"]
        target = chat_id or str(chan.get("user_id"))
        message_id = await client.send_chat_message(
            token, broadcaster_id=str(target), sender_id=str(acc["user_id"]),
            message=message[:MAX_MESSAGE_LENGTH])
        return {"success": True, "platform": "twitch", "chat_id": str(target),
                "message_id": message_id, "as": role}
    except Exception as e:  # noqa: BLE001
        return send_error(f"twitch standalone send failed: {e}")


# ──────────────────────────────────────────────────────────────────────
# Plugin registration
# ──────────────────────────────────────────────────────────────────────


def register(ctx) -> None:
    """Plugin entry point — registers the twitch gateway platform."""
    ctx.register_platform(
        name="twitch", label="Twitch",
        adapter_factory=lambda cfg: TwitchAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=[],  # all configuration is dashboard-driven (see README)
        install_hint="pip install websockets httpx   # both are already Hermes dependencies",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="TWITCH_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="TWITCH_ALLOWED_USERS",
        allow_all_env="TWITCH_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH,
        emoji="💬",
        pii_safe=True,
        allow_update_command=True,
        platform_hint=(
            "You are on Twitch chat, in a PUBLIC channel. Plain text only — "
            "markdown does not render, and images/files cannot be sent. "
            "Messages are capped at 500 characters; keep replies short and punchy. "
            "Every reply is visible to the whole channel, so be considerate and "
            "don't dump long output. In mention mode you are addressed as @<name>; "
            "in all mode any chat message can reach you, so reply only when it adds value."
        ),
    )
