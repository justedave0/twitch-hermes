"""Frontend regression guards (source-level).

The live tab broke in ways unit tests on the backend cannot see: a missing
Content-Type header made every mutating call 422, the secret input was a dead
controlled component, and the result banner was global instead of per-card.
These guards lock the current architecture down: independent cards that share
one postJSON / useSaver / Note, auto-save with no save buttons, and per-card
inline errors (no global banner).
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "dashboard" / "dist" / "index.js").read_text()


def test_every_mutating_call_goes_through_postjson_with_content_type():
    # The helper exists and sets the header the SDK omits.
    assert "Content-Type" in SRC and "application/json" in SRC
    # Exactly ONE stringified body in the whole file: inside postJSON.
    # Any other raw body would be sent as text/plain and FastAPI would 422.
    assert SRC.count("body: JSON.stringify(") == 1, (
        "a raw JSON body outside postJSON() would ship without "
        "Content-Type: application/json and 422 in FastAPI")


def test_mutating_endpoints_call_postjson_not_raw_fetch():
    for endpoint in ('BASE + "/app"', 'BASE + "/connect"', 'BASE + "/account"',
                     'BASE + "/settings"', 'BASE + "/test"'):
        # each mutating endpoint must appear in a postJSON call
        assert re.search(rf'postJSON\(\s*{re.escape(endpoint)}', SRC), (
            f"{endpoint} must be called via postJSON()")
    # the flow poll is a plain GET — must stay fetchJSON (no body involved)
    assert 'SDK.fetchJSON(BASE + "/connect/"' in SRC


def test_shared_saver_and_note_reused():
    # Reuse over re-write: ONE useSaver hook and ONE Note component power every
    # card. No card reimplements its own save/result state.
    assert "function useSaver" in SRC, "the shared save/result hook must exist"
    assert "function Note" in SRC, "the shared inline status component must exist"
    assert SRC.count("useSaver()") >= 3, "app/accounts/behavior cards must share useSaver"
    assert SRC.count("h(Note, { note:") >= 4, "every card must render its own <Note/>"


def test_secret_input_is_state_bound_not_dead():
    # The password field must be bound to live local state so real keystrokes
    # survive re-renders (the old bug: value: "" wiped every keystroke).
    assert 'value: form[0].client_secret' in SRC, "secret field must be bound to state"
    assert 'value: "", type: "password"' not in SRC, "dead controlled secret input regressed"


def test_app_state_seeded_from_status():
    # The Client ID field displays the stored value. Without seeding the form
    # from /status, saving after only editing the secret POSTs an empty
    # client_id and 400s. Seeding happens exactly once (a ref) so the 10s
    # refresh never clobbers an in-progress edit.
    assert "client_id: props.app.client_id" in SRC, "app form must seed from /status"
    assert "seeded.current" in SRC, "seed must run once (no clobber on refresh)"
    assert "value: form[0].client_id" in SRC, "Client ID field must bind to the seeded state"


def test_secret_not_erased_on_failed_save():
    # The pasted secret is cleared ONLY on a successful save (inside the
    # out.ok branch) — never unconditionally or in a .finally, so a failed
    # save leaves the secret visible for re-reading and retry.
    assert "if (out.ok) {" in SRC, "success path must be explicit"
    assert 'client_secret: ""' in SRC, "secret is cleared on success"
    assert not re.search(r'finally\([^)]*\)\s*\{[^}]*client_secret\s*:\s*""', SRC), (
        "secret must not be wiped in a .finally")


def test_errors_are_per_card_not_global():
    # No global result banner. Every card owns a <Note/> that shows its own
    # success/error inline (red for errors), so a failure appears in the card
    # that caused it — not in a shared banner the user isn't looking at.
    assert "text-red-400" in SRC, "error notes must render red"
    assert 'msg[0].text' not in SRC, "the global msg banner must be gone"
    assert "h(Note, { note: saver.note })" in SRC, "cards wire their own note into Note"


def test_no_save_buttons_autosave():
    # No "Save ..." buttons anywhere. Each change auto-saves: toggles/checkbox
    # save on click, text fields save on blur — through the shared commit path.
    assert "Save app credentials" not in SRC, "app save button must be gone"
    assert "Save behavior" not in SRC, "behavior save button must be gone"
    assert "Saving\u2026" not in SRC, "no 'Saving…' button state"
    assert "function commit" in SRC, "app card auto-saves on blur via commit()"
    assert "saveField" in SRC, "behavior card auto-saves each changed field"


def test_settings_autosave_is_partial():
    # /settings is a partial update: auto-save posts ONLY the changed field, so
    # rapid changes never clobber each other. The body must be a single-key
    # object, not the whole settings object.
    assert "body[field] = value" in SRC, "auto-save must post a single-field body"


def test_transient_poll_error_keeps_pairing_alive():
    # A failed poll (network blip, 5xx) must NOT kill the device-code card:
    # only a terminal "unknown or expired flow" (404) may. The pairing
    # window is the one thing the user cannot retry cheaply.
    assert 'emsg.indexOf("unknown or expired flow")' in SRC, (
        "poll catch must distinguish terminal 404s from transient errors")
    assert "retrying" in SRC, "transient poll errors should say they retry"


def test_pairing_shows_full_url_not_a_code_box():
    # The code is already inside the activation URL. A separate code box made
    # users copy a redundant string and its background flickered + its text was
    # unselectable. The pairing card must render the FULL verification_uri as
    # the link text, with no <code> element for user_code.
    assert "}, flow.verification_uri)," in SRC, (
        "the link text must be the full verification_uri (code included)")
    assert "Click this link on any device" in SRC
    assert "Enter the code" not in SRC, "must not ask the user to type the code"
    assert "flow.user_code || " not in SRC, (
        "the code box (h('code', ... flow.user_code)) must be gone")


def test_pairing_link_is_underlined_and_selectable():
    assert "underline" in SRC, "the activation link must be underlined like a link"
    assert "userSelect" in SRC, "the URL text must be selectable/copyable"


def test_no_token_countdown_implies_manual_action():
    # "token valid ~N min" read as a deadline the user must act on. The adapter
    # auto-refreshes 5 min before expiry; the view must say so.
    assert "valid ~" not in SRC and " min\"" not in SRC, (
        "do not show a 'token valid ~N min' countdown")
    assert "tokens auto-refresh" in SRC


def test_view_auto_refreshes_status():
    # The tab used to load /status once and go stale until a manual reload. It
    # must re-poll on an interval, updating only the status (not the forms).
    assert "function refreshStatus" in SRC
    assert "setInterval(refreshStatus" in SRC
    assert "clearInterval(statusTimer)" in SRC


def test_poll_renders_only_on_status_change():
    # Re-rendering the pairing card on every 5s poll (even when the status was
    # unchanged) flickered the card and killed text selection. The flow state
    # must only update when the status actually changes.
    assert "var lastStatus = flowInfo.status;" in SRC
    assert "} else if (d.status !== lastStatus) {" in SRC
    assert "lastStatus = d.status;" in SRC
