"""Plugin registration contract: register(ctx) must declare the platform
with the exact wiring the gateway needs (authz env, cron delivery,
out-of-process sender, message cap, hint)."""
from __future__ import annotations


class FakeCtx:
    def __init__(self):
        self.platform = None
        self.tools = []

    def register_platform(self, **kw):
        self.platform = kw

    def register_tool(self, **kw):
        self.tools.append(kw)


def test_register_declares_twitch_platform(adapter_pkg):
    ctx = FakeCtx()
    adapter_pkg.register(ctx)
    assert ctx.platform is not None, "register() must call ctx.register_platform"
    p = ctx.platform
    assert p["name"] == "twitch"
    assert p["label"] == "Twitch"
    assert p["allowed_users_env"] == "TWITCH_ALLOWED_USERS"
    assert p["allow_all_env"] == "TWITCH_ALLOW_ALL_USERS"
    assert p["cron_deliver_env_var"] == "TWITCH_HOME_CHANNEL"
    assert p["max_message_length"] == 500
    assert callable(p["standalone_sender_fn"])
    assert callable(p["check_fn"])
    assert callable(p["env_enablement_fn"])
    assert p["platform_hint"], "the agent must be told it is in a public channel"
    assert "500" in p["platform_hint"] or "500 characters" in p["platform_hint"]


def test_adapter_factory_builds_adapter(adapter_pkg):
    ctx = FakeCtx()
    adapter_pkg.register(ctx)
    from gateway.config import PlatformConfig

    adapter = ctx.platform["adapter_factory"](PlatformConfig(enabled=True))
    assert isinstance(adapter, adapter_pkg.TwitchAdapter)
    assert adapter.name == "twitch"
    assert adapter.MAX_MESSAGE_LENGTH == 500


def test_check_requirements_passes_when_deps_present(adapter_pkg):
    assert adapter_pkg.check_requirements() is True
