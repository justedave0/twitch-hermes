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


def test_result_banner_is_rendered():
    # Errors must be visible where the user is acting, not buried in the
    # bottom Test card.
    assert "msg[0].text" in SRC
    assert "text-red-400" in SRC
