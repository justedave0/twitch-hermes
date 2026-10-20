"""Frontend regression guards (source-level).

The live tab broke in ways unit tests on the backend cannot see: a missing
Content-Type header made every mutating call 422, and the secret input was a
dead controlled component. These guards lock both down.
"""
from __future__ import annotations

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
        import re
        assert re.search(rf'postJSON\(\s*{re.escape(endpoint)}', SRC), (
            f"{endpoint} must be called via postJSON()")
    # the flow poll is a plain GET — must stay fetchJSON (no body involved)
    assert 'SDK.fetchJSON(BASE + "/connect/"' in SRC


def test_secret_input_is_state_bound_not_dead():
    # The bug: value: "" hardcoded on the password field — real keystrokes
    # erased on every re-render, so users could not paste the secret.
    assert 'value: app[0].client_secret' in SRC, "secret field must be bound to state"
    assert 'value: "", type: "password"' not in SRC, "dead controlled secret input regressed"


def test_app_state_seeded_from_status():
    # The Client ID field displays the stored value; saveApp() posts the app
    # state. Without seeding the state from /status, saving after only editing
    # the secret POSTs an empty client_id and 400s.
    assert "client_id: d.app.client_id" in SRC, "load() must seed the app form state from /status"
    assert 'value: app[0].client_id' in SRC, "Client ID field must bind to the seeded state"


def test_secret_not_erased_on_failed_save():
    # .finally must not wipe the pasted secret when the save fails — the user
    # needs to re-read and retry it.
    assert '.finally(function () { setSavingApp(false); });' in SRC, (
        "secret field is cleared on failure — only .then may clear it")


def test_result_banner_is_rendered():
    # Errors must be visible where the user is acting, not buried in the
    # bottom Test card.
    assert "msg[0].text" in SRC
    assert "text-red-400" in SRC


def test_transient_poll_error_keeps_pairing_alive():
    # A failed poll (network blip, 5xx) must NOT kill the device-code card:
    # only a terminal "unknown or expired flow" (404) may. The pairing
    # window is the one thing the user cannot retry cheaply.
    assert 'emsg.indexOf("unknown or expired flow")' in SRC, (
        "poll catch must distinguish terminal 404s from transient errors")
    assert "retrying" in SRC, "transient poll errors should say they retry"


def test_pairing_shows_full_url_not_a_code_box():
    # The code is already inside the activation URL. A separate code box
    # made users copy a string that was redundant, and the box's
    # background flickered + its text was unselectable. The pairing card
    # must render the FULL verification_uri as the link text, with no
    # <code> element for user_code.
    assert "}, flow.verification_uri)," in SRC, (
        "the link text must be the full verification_uri (code included)")
    # The instruction is to click the link, not to type a code.
    assert "Click this link on any device" in SRC
    assert "Enter the code" not in SRC, "must not ask the user to type the code"
    # No <code> element rendering the user_code anywhere in the flow card.
    assert "flow.user_code || " not in SRC, (
        "the code box (h('code', ... flow.user_code)) must be gone")


def test_pairing_link_is_underlined_and_selectable():
    assert "underline" in SRC, "the activation link must be underlined like a link"
    assert "userSelect" in SRC, "the URL text must be selectable/copyable"


def test_no_token_countdown_implies_manual_action():
    # "token valid ~N min" read as a deadline the user must act on. The
    # adapter auto-refreshes 5 min before expiry; the view must say so.
    assert "valid ~" not in SRC and " min\"" not in SRC, (
        "do not show a 'token valid ~N min' countdown")
    assert "tokens auto-refresh" in SRC


def test_view_auto_refreshes_status():
    # The tab used to load /status once and go stale until a manual reload.
    # It must re-poll on an interval, updating only `st` (not the forms).
    assert "function refreshStatus" in SRC
    assert "setInterval(refreshStatus" in SRC
    assert "clearInterval(statusTimer)" in SRC


def test_poll_renders_only_on_status_change():
    # Re-rendering the pairing card on every 5s poll (even when the status
    # was unchanged) flickered the card and killed text selection. The flow
    # state must only update when the status actually changes.
    assert "var lastStatus = flowInfo.status;" in SRC
    assert "} else if (d.status !== lastStatus) {" in SRC
    assert "lastStatus = d.status;" in SRC
