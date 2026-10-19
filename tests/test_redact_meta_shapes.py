"""Offline tests (from musecli): the Meta / Muse secrets survive no redaction path: fb_dtsg, lsd, jazoest, datr, the
*_sess session cookies, access tokens, x-fb-atod-token / x-hatch-gateway-token, and the VM gateway's query."""

from __future__ import annotations

import json

from conftest import FIXTURES
from webcli.core.redact import post_body_keys, redact_headers, redact_text, redact_url, redact_value, shape

# Synthetic values in the shapes Meta uses (tests/fixtures/muse). None of these is real.
DTSG = "NAfakeDtsg0123456789:42:1791990000"
LSD = "AVfakeLsd0123456789"
ACCESS = "EAAfakeAccessToken0123456789abcdefFAKE"
SESS = "fakeSessionKey0123456789abcdef"
DATR = "fAkEdAtR_0123456789xyz"
SECRETS = [DTSG, LSD, ACCESS, SESS, DATR, "25871"]
FORM = (FIXTURES / "muse" / "graphql_form.txt").read_text()


def leaked(text: str) -> list[str]:
    return [secret for secret in SECRETS if secret in text]


def test_text_drops_csrf_tokens_session_cookies_and_gateway_headers():
    line = (f"POST muse.ai/api/graphql body={FORM} cookie-ish abra_sess={SESS}; hatch_sess={SESS}; datr={DATR}\n"
            f"x-fb-atod-token: {ACCESS}\nx-hatch-gateway-token: {ACCESS}")
    out = redact_text(line)
    assert leaked(out) == []
    assert "fb_api_req_friendly_name=useHatchConversationsQuery" in out  # the operation name stays readable


def test_json_bodies_and_auth_check_answers():
    raw = (FIXTURES / "muse" / "auth_check.json").read_text()
    out = redact_text(raw)
    assert leaked(out) == [] and "Test Owner" in out
    body = json.dumps({"vmAddress": "10.20.30.40:443", "vmName": "vm-fake-0123", "nonce": "n-0123456789"})
    assert all(value not in redact_text(body) for value in ("10.20.30.40", "vm-fake-0123", "n-0123456789"))
    assert leaked(json.dumps(redact_value(json.loads(raw)))) == []


def test_the_gateway_socket_url_keeps_only_its_shape():
    url = f"wss://hatch.metaaivm.com/v1/noise?vm_id=1234567890&auth_token={ACCESS}"
    assert redact_url(url) == "wss://hatch.metaaivm.com/v1/noise?vm_id=<redacted>&auth_token=<redacted>"


def test_headers_and_shapes():
    out = redact_headers({"x-fb-friendly-name": "useHatchConversationsQuery", "x-fb-lsd": LSD,
                          "x-hatch-gateway-token": ACCESS, "x-fb-atod-token": ACCESS, "Cookie": f"datr={DATR}"})
    assert out["x-fb-friendly-name"] == "useHatchConversationsQuery"
    assert {out[k] for k in ("x-fb-lsd", "x-hatch-gateway-token", "x-fb-atod-token", "Cookie")} == {"<redacted>"}
    tree = shape({"fb_dtsg": DTSG, "lsd": LSD, "jazoest": "25871", "datr": DATR, "llm_sess": SESS,
                  "user": {"session_key": SESS, "name": "Test Owner"}})
    assert leaked(json.dumps(tree)) == []
    assert post_body_keys(FORM) == sorted(["av", "__user", "__a", "fb_dtsg", "jazoest", "lsd", "fb_api_caller_class",
                                           "fb_api_req_friendly_name", "variables", "server_timestamps", "doc_id"])
    assert leaked(redact_text(f"x-fb-lsd: {LSD}\nX-FB-DTSG: {DTSG}")) == []
