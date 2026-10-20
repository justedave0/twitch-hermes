"""Shared fixtures: in-memory twins of the hermes storage layer, minimal
fakes of the gateway adapter contract (so the adapter imports outside a full
Hermes install), a fake Twitch HTTP backend, and loaders for the plugin
modules.

Nothing here needs a running gateway, dashboard or real Hermes home — the
suite runs in a bare venv with pytest + fastapi + httpx + websockets.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]

_STORE_HOLDER = {}


def _set_store(store):
    _STORE_HOLDER["store"] = store


def _store():
    return _STORE_HOLDER["store"]


class Store:
    """In-memory twin of the hermes storage layer.

    * ``env``      → the profile ``.env`` (secrets live here)
    * ``settings`` → ``plugins.entries.twitch-platform.settings`` (config.yaml)
    """

    PLUGIN_ID = "twitch-platform"

    def __init__(self):
        self.env: dict[str, str] = {}
        self.settings: dict = {}

    # -- config.yaml side -------------------------------------------------

    def config_dict(self) -> dict:
        return {"plugins": {"entries": {self.PLUGIN_ID: {"settings": self.settings}}}}

    def save_setting(self, key: str, value) -> None:
        self.settings[key] = value


# ── fake hermes storage modules (installed into sys.modules by a fixture) ─

def _make_hermes_fakes():
    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []  # namespace package

    cfg = types.ModuleType("hermes_cli.config")

    def load_config_readonly():
        return _store().config_dict()

    def remove_env_value(name):
        _store().env.pop(name, None)

    cfg.load_config_readonly = load_config_readonly
    cfg.remove_env_value = remove_env_value

    pstate = types.ModuleType("hermes_cli.plugins_state")

    def save_plugin_setting(plugin_id, keys, value):
        assert plugin_id == Store.PLUGIN_ID, f"unexpected plugin id {plugin_id!r}"
        for k in keys:
            _store().save_setting(k, value)

    pstate.save_plugin_setting = save_plugin_setting

    cred = types.ModuleType("hermes_cli.credential_lifecycle")

    def save_provider_env_credential(name, value):
        _store().env[name] = value

    cred.save_provider_env_credential = save_provider_env_credential

    agent = types.ModuleType("agent")
    agent.__path__ = []
    secret_scope = types.ModuleType("agent.secret_scope")

    def get_secret(name, default=None):
        return _store().env.get(name, default)

    secret_scope.get_secret = get_secret

    return {
        "hermes_cli": hermes_cli,
        "hermes_cli.config": cfg,
        "hermes_cli.plugins_state": pstate,
        "hermes_cli.credential_lifecycle": cred,
        "agent": agent,
        "agent.secret_scope": secret_scope,
    }


# ── fake gateway modules (adapter contract, just enough to import/run) ────

def _make_gateway_fakes():
    gateway = types.ModuleType("gateway")
    gateway.__path__ = []

    gcfg = types.ModuleType("gateway.config")

    class _PlatformMember:
        def __init__(self, value):
            self.value = value

        def __repr__(self):
            return f"Platform({self.value!r})"

    def Platform(value):  # noqa: N802 — mirrors the enum's callable form
        return _PlatformMember(str(value).strip().lower())

    class PlatformConfig:
        def __init__(self, enabled=False, extra=None, token=None, api_key=None):
            self.enabled = enabled
            self.extra = extra or {}
            self.token = token
            self.api_key = api_key

    gcfg.Platform = Platform
    gcfg.PlatformConfig = PlatformConfig

    platforms = types.ModuleType("gateway.platforms")
    platforms.__path__ = []

    base = types.ModuleType("gateway.platforms.base")

    class SendResult:
        def __init__(self, success, message_id=None, error=None,
                     retryable=False, raw_response=None):
            self.success = success
            self.message_id = message_id
            self.error = error
            self.retryable = retryable
            self.raw_response = raw_response

    class BasePlatformAdapter:
        def __init__(self, config, platform):
            self.config = config
            self.platform = platform
            self.name = getattr(platform, "value", str(platform))
            self._running = False
            self._fatal_error = None

        def _mark_connected(self, **kw):
            self._running = True

        def _mark_disconnected(self):
            self._running = False

        def _set_fatal_error(self, code, message, *, retryable):
            self._fatal_error = (code, message, retryable)
            self._running = False

        def _wire_plugin_handlers(self, native=None):
            pass

        def build_source(self, **kw):
            return SimpleNamespace(**kw)

        async def send_typing(self, chat_id, metadata=None):
            pass

        async def handle_message(self, event):
            pass

    base.SendResult = SendResult
    base.BasePlatformAdapter = BasePlatformAdapter

    event = types.ModuleType("gateway.platforms.event")

    class MessageType:
        TEXT = "text"

    class MessageEvent:
        def __init__(self, text, message_type=MessageType.TEXT, source=None,
                     raw_message=None, message_id=None, timestamp=None):
            self.text = text
            self.message_type = message_type
            self.source = source
            self.raw_message = raw_message
            self.message_id = message_id
            self.timestamp = timestamp

    event.MessageType = MessageType
    event.MessageEvent = MessageEvent

    helpers = types.ModuleType("gateway.platforms.helpers")

    class MessageDeduplicator:
        def __init__(self, max_size=1000, ttl_seconds=300):
            self._items: dict[str, float] = {}
            self._ttl = ttl_seconds

        def is_duplicate(self, key):
            now = time.time()
            self._items = {k: t for k, t in self._items.items() if now - t < self._ttl}
            if key in self._items:
                return True
            self._items[key] = now
            return False

        def clear(self):
            self._items = {}

    helpers.MessageDeduplicator = MessageDeduplicator

    shared = types.ModuleType("gateway.platforms._shared")

    def get_scoped_secret(name, default=None, **kw):
        return _store().env.get(name, default)

    def send_error(message):
        return {"error": str(message)}

    shared.get_scoped_secret = get_scoped_secret
    shared.send_error = send_error

    return {
        "gateway": gateway,
        "gateway.config": gcfg,
        "gateway.platforms": platforms,
        "gateway.platforms.base": base,
        "gateway.platforms.event": event,
        "gateway.platforms.helpers": helpers,
        "gateway.platforms._shared": shared,
    }


# ── fixtures ---------------------------------------------------------------

def pytest_configure(config):
    """Keep the plugin root's ``__init__.py`` from breaking test setup.

    The Hermes loader requires an ``__init__.py`` at the repo root (it does
    ``from .adapter import register``). pytest >= 8 turns any collected
    directory containing one into a ``Package`` collector whose ``setup``
    imports that file — as a nameless top-level module, where the relative
    import fails and every test errors at setup. The file is fine when the
    plugin loader imports it (always as a proper package), so swallow the
    standalone-import failure only.
    """
    import _pytest.nodes as _pytest_nodes
    import _pytest.python as _pytest_python

    orig_setup = _pytest_python.Package.setup

    def setup(self):
        try:
            orig_setup(self)
        except ImportError:
            pass  # the plugin root __init__.py is not standalone-importable
        except _pytest_nodes.Collector.CollectError:
            pass  # same failure, wrapped by pytest's importtestmodule

    _pytest_python.Package.setup = setup


@pytest.fixture
def store():
    s = Store()
    _set_store(s)
    return s


@pytest.fixture
def fakes(store):
    """Install the fake hermes + gateway modules; restore on teardown."""
    installed = {**_make_hermes_fakes(), **_make_gateway_fakes()}
    saved = {n: sys.modules.get(n) for n in installed}
    for name, mod in installed.items():
        sys.modules[name] = mod
    yield
    for name in installed:
        if saved[name] is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved[name]


def _load_module(name: str, path: Path) -> types.ModuleType:
    if name in sys.modules:
        sys.modules.pop(name)
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def client_mod(fakes):
    """client.py standalone (no relative imports)."""
    return _load_module("twh_client_ut", REPO / "client.py")


@pytest.fixture
def adapter_pkg(fakes):
    """The adapter module loaded as a package (adapter.py does ``from . import client``).

    Mirrors how the plugin loader imports directory plugins: a parent
    namespace package in sys.modules + the submodule under a dotted name,
    with __package__ set so relative imports resolve."""
    pkg = "twh_pkg_ut"
    for name in (f"{pkg}.client", f"{pkg}.adapter", pkg):
        sys.modules.pop(name, None)
    ns = types.ModuleType(pkg)
    ns.__path__ = [str(REPO)]
    ns.__package__ = pkg
    sys.modules[pkg] = ns
    spec = importlib.util.spec_from_file_location(
        f"{pkg}.adapter", str(REPO / "adapter.py"), submodule_search_locations=[str(REPO)])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = f"{pkg}.adapter"
    mod.__path__ = [str(REPO)]
    sys.modules[f"{pkg}.adapter"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def api_mod(fakes):
    """dashboard/plugin_api.py (loads its own standalone client.py copy)."""
    return _load_module("twh_api_ut", REPO / "dashboard" / "plugin_api.py")


# ── fake Twitch HTTP backend -----------------------------------------------

def fresh_state() -> dict:
    return {
        "polls": 0,
        "pending_polls": 1,  # first poll → authorization_pending
        "device_role": None,
        "sends": [],
    }


def make_fake_request(state: dict):
    """A drop-in ``TwitchClient._request`` implementing the endpoints under test."""

    async def fake_request(self, method, url, *, headers=None, data=None,
                           params=None, json_body=None):
        headers = headers or {}
        data = data or {}
        if url.endswith("/oauth2/device"):
            state["device_role"] = "broadcaster" if "channel:bot" in data.get("scope", "") else "bot"
            return 200, {"device_code": "dev-123", "user_code": "WXYZ-1234",
                         "verification_uri": "https://id.twitch.tv/activate",
                         "interval": 5, "expires_in": 900}
        if url.endswith("/oauth2/token"):
            if data.get("grant_type") == "urn:ietf:params:oauth:grant-type:device_code":
                state["polls"] += 1
                if state["polls"] <= state["pending_polls"]:
                    return 400, {"error": "authorization_pending"}
                role = state["device_role"]
                return 200, {"access_token": f"tok-{role}", "refresh_token": f"ref-{role}",
                             "token_type": "Bearer", "expires_in": 21600, "scope": ""}
            if data.get("grant_type") == "refresh_token":
                return 200, {"access_token": f"tok-fresh-{data['refresh_token']}",
                             "refresh_token": "ref-new", "token_type": "Bearer",
                             "expires_in": 21600, "scope": ""}
            return 400, {"error": "unsupported_grant_type"}
        if url.endswith("/oauth2/validate"):
            auth = headers.get("Authorization", "")
            token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
            role = token.split("-")[-1] if token.startswith("tok-") else None
            ident = {"bot": ("davegpt", "111"), "broadcaster": ("justedave", "222")}.get(role)
            if not ident:
                return 401, {"error": "bad_token"}
            return 200, {"access_token": token, "client_id": self.client_id,
                         "login": ident[0], "user_id": ident[1],
                         "expires_in": 21600, "scope": ""}
        if url.endswith("/helix/chat/messages"):
            state["sends"].append(json_body or {})
            return 200, {"data": [{"id": f"msg-{len(state['sends'])}", **(json_body or {})}]}
        if url.endswith("/eventsub/subscriptions"):
            if method == "POST":
                return 200, {"data": [{"id": "sub-1", "status": "enabled"}]}
            return 200, {"data": []}
        if url.endswith("/helix/users"):
            return 200, {"data": []}
        if url.endswith("/helix/streams"):
            return 200, {"data": []}
        return 404, {"error": f"unmocked endpoint {method} {url}"}

    return fake_request
