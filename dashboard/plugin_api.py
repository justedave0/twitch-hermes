"""Twitch dashboard plugin — backend API routes.

Mounted at /api/plugins/twitch-platform/ by the dashboard plugin system.

Storage (same conventions as the Bambu Lab plugin):
* non-secret settings → plugins.entries.twitch-platform.settings (config.yaml)
    client_id, channel, accounts (id/login only), send_as, response_mode,
    mention_names, token_expires
* secrets             → .env via the credential lifecycle, read through
  agent.secret_scope, NEVER returned to clients (only has_* flags):
    TWITCH_CLIENT_SECRET, TWITCH_BOT_ACCESS_TOKEN, TWITCH_BOT_REFRESH_TOKEN,
    TWITCH_BROADCASTER_ACCESS_TOKEN, TWITCH_BROADCASTER_REFRESH_TOKEN
* authz gates (env, applied by the gateway at startup):
    TWITCH_ALLOWED_USERS, TWITCH_ALLOW_ALL_USERS
* cron delivery target (env): TWITCH_HOME_CHANNEL (broadcaster channel id)

Device-activation flow (no OAuth redirect URI):
  POST /connect {role}            → start flow, return user_code + twitch.tv/activate link
  GET  /connect/{flow_id}         → poll; on success the token pair is stored
"""
from __future__ import annotations

import asyncio
import importlib.util
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

PLUGIN_ID = "twitch-platform"
ROLES = ("bot", "broadcaster")
ROLES_SCOPES = {
    "bot": ["user:read:chat", "user:write:chat", "user:bot"],
    "broadcaster": ["channel:bot", "user:read:chat", "user:write:chat"],
}
ROLES_LABELS = {
    "bot": "Bot account (the account the agent chats as)",
    "broadcaster": "Broadcaster account (the channel owner — also unlocks the channel)",
}

_client_lock = asyncio.Lock()
_client_mod: Optional[Any] = None

# Active device-activation flows: flow_id → {device_code, role, expires_at, interval}
_flows: dict[str, dict[str, Any]] = {}


def _get_client() -> Any:
    """Load the plugin's client.py standalone (the dashboard loads this file
    by path, so relative package imports are not guaranteed)."""
    global _client_mod
    if _client_mod is None:
        path = Path(__file__).resolve().parents[1] / "client.py"
        spec = importlib.util.spec_from_file_location("twitch_client_standalone", str(path))
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _client_mod = mod
    return _client_mod


# ──────────────────────────────────────────────────────────────────────
# Storage helpers
# ──────────────────────────────────────────────────────────────────────


def _settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly() or {}
    except Exception:
        return {}
    entry = ((cfg.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    return entry.get("settings") or {}


def _save(key: str, value: Any) -> None:
    from hermes_cli.plugins_state import save_plugin_setting
    save_plugin_setting(PLUGIN_ID, (key,), value)


def _has_secret(name: str) -> bool:
    try:
        from agent.secret_scope import get_secret
        return bool(get_secret(name))
    except Exception:
        return False


def _secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret
        v = get_secret(name)
        return v.strip() if isinstance(v, str) else ""
    except Exception:
        return ""


def _store_secret(name: str, value: str) -> None:
    from hermes_cli.credential_lifecycle import save_provider_env_credential
    save_provider_env_credential(name, value)


def _clear_secret(name: str) -> None:
    try:
        from hermes_cli.config import remove_env_value
        remove_env_value(name)
    except Exception:
        pass


def _client_pair() -> tuple[str, str]:
    s = _settings()
    return str(s.get("client_id") or "").strip(), _secret("TWITCH_CLIENT_SECRET")


def _account(role: str) -> dict[str, Any]:
    s = _settings()
    a = (s.get("accounts") or {}).get(role) or {}
    return {
        "user_id": str(a.get("user_id") or ""),
        "login": str(a.get("login") or ""),
        "display_name": str(a.get("display_name") or ""),
        "has_token": bool(_secret(f"TWITCH_{role.upper()}_ACCESS_TOKEN")),
        "expires_in": None,
    }


def _account_expires_in(role: str) -> Optional[float]:
    s = _settings()
    expires_at = (s.get("token_expires") or {}).get(role)
    if not expires_at:
        return None
    remain = float(expires_at) - time.time()
    return round(remain, 1) if remain > 0 else 0


def _allowed_users() -> str:
    return _secret("TWITCH_ALLOWED_USERS")


def _allow_all_users() -> bool:
    return _secret("TWITCH_ALLOW_ALL_USERS").lower() in ("1", "true", "yes")


# ──────────────────────────────────────────────────────────────────────
# Models
# ──────────────────────────────────────────────────────────────────────


class AppIn(BaseModel):
    client_id: str = ""
    client_secret: Optional[str] = None  # blank = keep existing


class ConnectIn(BaseModel):
    role: str  # "bot" | "broadcaster"


class TestIn(BaseModel):
    text: str = ""


class SettingsIn(BaseModel):
    send_as: Optional[str] = None
    response_mode: Optional[str] = None
    mention_names: Optional[list[str]] = None
    allowed_users: Optional[str] = None
    allow_all_users: Optional[bool] = None


class RoleIn(BaseModel):
    role: str


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────


@router.get("/status")
async def get_status() -> dict[str, Any]:
    """Config + token presence. Secrets are NEVER returned — only flags."""
    s = _settings()
    out_accounts = {}
    for role in ROLES:
        acc = _account(role)
        acc["expires_in"] = _account_expires_in(role)
        out_accounts[role] = acc
    chan = s.get("channel") or {}
    return {
        "app": {"client_id": str(s.get("client_id") or ""),
                "has_secret": _has_secret("TWITCH_CLIENT_SECRET")},
        "channel": {"user_id": str(chan.get("user_id") or ""),
                    "login": str(chan.get("login") or "")} if chan.get("user_id") else None,
        "accounts": out_accounts,
        "settings": {
            "send_as": s.get("send_as") if s.get("send_as") in ROLES else "bot",
            "response_mode": s.get("response_mode") if s.get("response_mode") in ("mention", "all") else "mention",
            "mention_names": [str(n) for n in (s.get("mention_names") or [])],
            "allowed_users": _allowed_users(),
            "allow_all_users": _allow_all_users(),
        },
        "version": "1.0.0",
    }


@router.post("/app")
async def save_app(body: AppIn) -> dict[str, Any]:
    """Save app credentials. Blank client_secret keeps the existing one."""
    client_id = body.client_id.strip()
    if not client_id:
        raise HTTPException(status_code=400, detail="client id is required")
    _save("client_id", client_id)
    if body.client_secret is not None and body.client_secret.strip():
        _store_secret("TWITCH_CLIENT_SECRET", body.client_secret.strip())
    return {"ok": True, "has_secret": _has_secret("TWITCH_CLIENT_SECRET")}


@router.post("/connect")
async def start_connect(body: ConnectIn) -> dict[str, Any]:
    """Start a device-activation flow for one role (no redirect URI)."""
    role = body.role
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {ROLES}")
    client_id, client_secret = _client_pair()
    if not (client_id and client_secret):
        raise HTTPException(status_code=400, detail="save your app credentials first (client id + secret)")
    tc = _get_client()
    try:
        dev = await tc.TwitchClient(client_id, client_secret).device_authorization(ROLES_SCOPES[role])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Twitch device flow failed: {e}")
    flow_id = uuid.uuid4().hex
    _flows[flow_id] = {
        "device_code": dev["device_code"],
        "role": role,
        "expires_at": time.time() + float(dev.get("expires_in") or 900),
        "interval": float(dev.get("interval") or 5),
        "client_id": client_id,
        "client_secret": client_secret,
    }
    return {
        "flow_id": flow_id,
        "user_code": dev.get("user_code", ""),
        "verification_uri": dev.get("verification_uri") or "https://id.twitch.tv/activate",
        "interval": float(dev.get("interval") or 5),
        "expires_in": float(dev.get("expires_in") or 900),
        "role": role,
    }


@router.get("/connect/{flow_id}")
async def poll_connect(flow_id: str) -> dict[str, Any]:
    """Poll a device-activation flow. On success the token pair is stored
    and (for the broadcaster) the channel is set from that account."""
    flow = _flows.get(flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="unknown or expired flow — press Connect again")
    if time.time() > flow["expires_at"]:
        _flows.pop(flow_id, None)
        return {"status": "expired"}
    tc = _get_client()
    client = tc.TwitchClient(flow["client_id"], flow["client_secret"])
    try:
        data = await client.poll_device_token(flow["device_code"])
    except Exception as e:  # noqa: BLE001
        return {"status": "pending", "error": str(e)[:200]}
    status = data.get("status")
    if status != "authorized":
        return {"status": status}  # pending | slow_down | expired | denied | invalid
    role = flow["role"]
    # Store tokens + expiry
    _store_secret(f"TWITCH_{role.upper()}_ACCESS_TOKEN", data["access_token"])
    _store_secret(f"TWITCH_{role.upper()}_REFRESH_TOKEN", data["refresh_token"])
    s = _settings()
    te = dict(s.get("token_expires") or {})
    te[role] = time.time() + float(data.get("expires_in") or 21600)
    _save("token_expires", te)
    # Resolve the account identity
    try:
        user = await client.get_user(data["access_token"])
    except Exception:  # noqa: BLE001
        user = None
    accounts = dict(s.get("accounts") or {})
    accounts[role] = {
        "user_id": str((user or {}).get("id") or ""),
        "login": str((user or {}).get("login") or ""),
        "display_name": str((user or {}).get("display_name") or ""),
    }
    _save("accounts", accounts)
    result: dict[str, Any] = {
        "status": "authorized",
        "login": accounts[role]["login"],
        "user_id": accounts[role]["user_id"],
        "display_name": accounts[role]["display_name"],
    }
    if role == "broadcaster" and accounts[role]["user_id"]:
        # The broadcaster login defines the channel this install lives in.
        _save("channel", {"user_id": accounts[role]["user_id"], "login": accounts[role]["login"]})
        _store_secret("TWITCH_HOME_CHANNEL", accounts[role]["user_id"])
        result["channel_set"] = True
    _flows.pop(flow_id, None)
    return result


@router.delete("/account")
async def delete_account(body: RoleIn) -> dict[str, Any]:
    """Disconnect one role: drop its tokens (+ channel if broadcaster)."""
    role = body.role
    if role not in ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {ROLES}")
    _clear_secret(f"TWITCH_{role.upper()}_ACCESS_TOKEN")
    _clear_secret(f"TWITCH_{role.upper()}_REFRESH_TOKEN")
    s = _settings()
    te = dict(s.get("token_expires") or {})
    te.pop(role, None)
    _save("token_expires", te)
    if role == "broadcaster":
        _save("channel", {})
        _clear_secret("TWITCH_HOME_CHANNEL")
    return {"ok": True}


@router.post("/test")
async def test_message(body: TestIn) -> dict[str, Any]:
    """Send one message to the channel as the active identity (send_as)."""
    s = _settings()
    chan = s.get("channel") or {}
    send_as = s.get("send_as")
    role = send_as if isinstance(send_as, str) and send_as in ROLES else "bot"
    token = _secret(f"TWITCH_{role.upper()}_ACCESS_TOKEN")
    if not str(chan.get("user_id") or ""):
        return {"ok": False, "error": "no channel configured — connect your broadcaster account first"}
    if not token:
        return {"ok": False, "error": f"no {role} account connected"}
    client_id, client_secret = _client_pair()
    if not client_id:
        return {"ok": False, "error": "save your app credentials first"}
    acc = (s.get("accounts") or {}).get(role) or {}
    if not str(acc.get("user_id") or ""):
        return {"ok": False, "error": f"{role} account has no user id — reconnect it"}
    tc = _get_client()
    client = tc.TwitchClient(client_id, client_secret)
    try:
        expires_at = float((s.get("token_expires") or {}).get(role) or 0)
        if time.time() >= expires_at - 300:
            refresh = _secret(f"TWITCH_{role.upper()}_REFRESH_TOKEN")
            if not refresh:
                return {"ok": False, "error": f"{role} token expired — reconnect the {role} account"}
            data = await client.refresh_token(refresh)
            _store_secret(f"TWITCH_{role.upper()}_ACCESS_TOKEN", data["access_token"])
            _store_secret(f"TWITCH_{role.upper()}_REFRESH_TOKEN", data["refresh_token"])
            te = dict(_settings().get("token_expires") or {})
            te[role] = time.time() + float(data.get("expires_in") or 21600)
            _save("token_expires", te)
            token = data["access_token"]
        message_id = await client.send_chat_message(
            token, broadcaster_id=str(chan["user_id"]), sender_id=str(acc["user_id"]),
            message=(body.text.strip() or "👋 test message from your Hermes agent")[:500])
        return {"ok": True, "message_id": message_id, "as": role,
                "channel": chan.get("login") or str(chan.get("user_id"))}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:300]}


@router.put("/settings")
async def save_settings(body: SettingsIn) -> dict[str, Any]:
    """Behavior settings. send_as / response_mode / mention_names apply
    live (the adapter re-reads settings); allowlist changes need a gateway
    restart (gateway authz)."""
    s = _settings()
    if body.send_as is not None:
        if body.send_as not in ROLES:
            raise HTTPException(status_code=400, detail="send_as must be 'bot' or 'broadcaster'")
        _save("send_as", body.send_as)
    if body.response_mode is not None:
        if body.response_mode not in ("mention", "all"):
            raise HTTPException(status_code=400, detail="response_mode must be 'mention' or 'all'")
        _save("response_mode", body.response_mode)
    if body.mention_names is not None:
        names = [str(n).strip().lower() for n in body.mention_names if str(n).strip()]
        _save("mention_names", names)
    restart = False
    if body.allowed_users is not None:
        v = body.allowed_users.strip()
        if v:
            _store_secret("TWITCH_ALLOWED_USERS", v)
        else:
            _clear_secret("TWITCH_ALLOWED_USERS")
        restart = True
    if body.allow_all_users is not None:
        if body.allow_all_users:
            _store_secret("TWITCH_ALLOW_ALL_USERS", "true")
        else:
            _clear_secret("TWITCH_ALLOW_ALL_USERS")
        restart = True
    return {"ok": True, "restart_required": restart}
