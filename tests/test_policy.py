"""Adapter policy tests: mention gate, config gap detection, echo
protection, and the live config bundle.

The mention gate is the safety gate for a PUBLIC channel — these cases are
regression-locked: case-insensitivity, display-name matching (Twitch
autocomplete sends the display name), and prefix protection.
"""
from __future__ import annotations

import time

from conftest import Store


def _bundle(**over):
    base = {
        "client_id": "cid",
        "client_secret": "sec",
        "channel": {"user_id": "222", "login": "justedave"},
        "send_as": "bot",
        "response_mode": "mention",
        "mention_names": [],
        "token_expires": {},
        "accounts": {
            "bot": {"access_token": "t", "refresh_token": "r", "user_id": "111",
                    "login": "davegpt", "display_name": "DaveGPT"},
            "broadcaster": {"access_token": "t", "refresh_token": "r", "user_id": "222",
                            "login": "justedave", "display_name": "justeDave_"},
        },
    }
    base.update(over)
    return base


def _adapter(adapter_pkg):
    return object.__new__(adapter_pkg.TwitchAdapter)


class TestMentionGate:
    def test_case_insensitive(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle()
        for text in ("hello @davegpt", "hey @DaveGPT", "@DAVEGPT yo"):
            assert adapter_pkg.TwitchAdapter._mentions_us(fa, text, b) is True, text

    def test_display_name_also_matches(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle()
        # display_name "DaveGPT" differs from login only in case
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@DaveGPT hi", b) is True
        # a display name with a space (autocomplete sends this)
        b2 = _bundle(accounts={
            "bot": {"access_token": "t", "refresh_token": "r", "user_id": "111",
                    "login": "davegpt", "display_name": "Dave GPT"},
            "broadcaster": _bundle()["accounts"]["broadcaster"],
        })
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@Dave GPT hi", b2) is True

    def test_prefix_of_longer_word_does_not_match(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle()
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@davegpt2 something", b) is False

    def test_other_users_do_not_match(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle()
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "hello @other", b) is False
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "no mention here", b) is False
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "davegpt is cool (no @)", b) is False

    def test_explicit_mention_names_honored_plus_auto_login(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle(mention_names=["custom"])
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@custom hi", b) is True
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@davegpt hi", b) is True  # login auto-included

    def test_send_as_broadcaster_matches_broadcaster_names(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle(send_as="broadcaster")
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@justeDave_ hey", b) is True
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@justedave hey", b) is True
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@someone", b) is False

    def test_no_names_at_all_returns_false(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        b = _bundle(accounts={
            "bot": {"access_token": "t", "refresh_token": "r", "user_id": "111",
                    "login": "", "display_name": ""},
            "broadcaster": _bundle()["accounts"]["broadcaster"],
        })
        assert adapter_pkg.TwitchAdapter._mentions_us(fa, "@davegpt hi", b) is False


class TestConfigGaps:
    def test_complete_config_has_no_gaps(self, adapter_pkg):
        assert adapter_pkg._config_gaps(_bundle()) == []

    def test_empty_config_reports_everything(self, adapter_pkg):
        b = _bundle(
            client_id="", client_secret="", channel={},
            accounts={
                "bot": {"access_token": "", "refresh_token": "", "user_id": "",
                        "login": "", "display_name": ""},
                "broadcaster": {"access_token": "", "refresh_token": "", "user_id": "",
                                "login": "", "display_name": ""},
            })
        gaps = adapter_pkg._config_gaps(b)
        assert len(gaps) == 4  # app creds, channel, bot, broadcaster
        assert any("app credentials" in g for g in gaps)
        assert any("bot account" in g for g in gaps)
        assert any("broadcaster account" in g for g in gaps)

    def test_invalid_send_as_falls_back_to_bot(self, adapter_pkg, store):
        store.settings["send_as"] = "bogus"
        adapter_pkg._settings_cache["at"] = 0.0
        b = adapter_pkg._bundle()
        assert b["send_as"] == "bot"

    def test_invalid_response_mode_falls_back_to_mention(self, adapter_pkg, store):
        store.settings["response_mode"] = "maybe"
        adapter_pkg._settings_cache["at"] = 0.0
        b = adapter_pkg._bundle()
        assert b["response_mode"] == "mention"


class TestEchoBookkeeping:
    def test_sent_ids_tracked_and_expired(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        fa._sent_ids = {}
        adapter_pkg.TwitchAdapter._remember_sent(fa, "abc")
        assert "abc" in fa._sent_ids
        fa._sent_ids["old"] = time.time() - 9999
        adapter_pkg.TwitchAdapter._remember_sent(fa, "new")
        assert "old" not in fa._sent_ids
        assert "new" in fa._sent_ids

    def test_empty_id_is_ignored(self, adapter_pkg):
        fa = _adapter(adapter_pkg)
        fa._sent_ids = {}
        adapter_pkg.TwitchAdapter._remember_sent(fa, "")
        assert fa._sent_ids == {}


class TestBundle:
    def test_bundle_reads_store(self, adapter_pkg, store):
        store.settings["client_id"] = "cid-from-store"
        store.settings["channel"] = {"user_id": "222", "login": "justedave"}
        store.settings["send_as"] = "broadcaster"
        store.settings["response_mode"] = "all"
        store.settings["mention_names"] = ["a", "b"]
        store.settings["token_expires"] = {"bot": time.time() + 1000}
        store.settings["accounts"] = {
            "bot": {"user_id": "111", "login": "davegpt", "display_name": "DaveGPT"},
            "broadcaster": {"user_id": "222", "login": "justedave", "display_name": "justeDave_"},
        }
        store.env["TWITCH_CLIENT_SECRET"] = "sec-from-env"
        store.env["TWITCH_BOT_ACCESS_TOKEN"] = "tok-bot"
        store.env["TWITCH_BOT_REFRESH_TOKEN"] = "ref-bot"
        store.env["TWITCH_BROADCASTER_ACCESS_TOKEN"] = "tok-broad"
        adapter_pkg._settings_cache["at"] = 0.0
        b = adapter_pkg._bundle()
        assert b["client_id"] == "cid-from-store"
        assert b["client_secret"] == "sec-from-env"
        assert b["send_as"] == "broadcaster"
        assert b["response_mode"] == "all"
        assert b["mention_names"] == ["a", "b"]
        assert b["accounts"]["bot"]["access_token"] == "tok-bot"
        assert b["accounts"]["broadcaster"]["access_token"] == "tok-broad"
        assert b["channel"]["user_id"] == "222"

    def test_env_enablement_none_when_gaps(self, adapter_pkg, store):
        adapter_pkg._settings_cache["at"] = 0.0
        assert adapter_pkg._env_enablement() is None

    def test_env_enablement_seeds_extra_when_complete(self, adapter_pkg, store):
        store.settings["client_id"] = "cid"
        store.settings["channel"] = {"user_id": "222", "login": "justedave"}
        store.settings["accounts"] = {
            "bot": {"user_id": "111", "login": "davegpt", "display_name": "D"},
            "broadcaster": {"user_id": "222", "login": "justedave", "display_name": "J"},
        }
        store.env["TWITCH_CLIENT_SECRET"] = "sec"
        store.env["TWITCH_BOT_ACCESS_TOKEN"] = "t"
        store.env["TWITCH_BROADCASTER_ACCESS_TOKEN"] = "t"
        adapter_pkg._settings_cache["at"] = 0.0
        extra = adapter_pkg._env_enablement()
        assert extra is not None
        assert extra["channel_user_id"] == "222"
        assert extra["send_as"] == "bot"
        assert extra["response_mode"] == "mention"
