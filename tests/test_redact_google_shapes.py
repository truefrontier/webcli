"""Offline tests (from gweb): the merged redaction never lets a Google cookie, token, or session id through."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from webcli.core.redact import post_body_keys, redact_headers, redact_text, redact_url, shape, tokenish

AT = "AJvLN6Mx4nqzQ0aTestTokenOnlyAAAA:1760450400123"  # shape of WIZ_global_data.SNlM0e
SECRETS = [
    AT,
    "g.a000testPSIDvalue0123456789abcdefFAKE0076",  # shape of __Secure-1PSID
    "sidts-CjEBTestRotatingValue0123456789AAAA",  # shape of __Secure-1PSIDTS
    "-4271880281862545069",  # f.sid (all digits, so only the name gives it away)
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.c2lnbmF0dXJl",
    "hunter2",
]


def test_tokenish():
    assert [tokenish(s) for s in SECRETS[:5]] == [True] * 5  # including f.sid, all digits
    assert [tokenish(s) for s in ("What is 2+2?", "c_1a2b3c4d5e6f7a8b", "application/json", "MaZiqc")] == [False] * 4


def test_headers_keep_names_and_drop_secret_values():
    assert redact_headers({
        "Cookie": f"__Secure-1PSID={SECRETS[1]}",
        "Authorization": "SAPISIDHASH 1760450400_abc",
        "X-Same-Domain": "1",
        "X-Goog-Ext-525001261-Jspb": '[1,null,null,null,"9ec249fc9ad08861"]',
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Referer": f"https://gemini.google.com/app?at={SECRETS[0]}&hl=en",
    }) == {
        "Authorization": "<redacted>",
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Cookie": "<redacted>",
        "Referer": "https://gemini.google.com/app?at=<redacted>&hl=en",
        "X-Goog-Ext-525001261-Jspb": '[1,null,null,null,"9ec249fc9ad08861"]',
        "X-Same-Domain": "1",
    }


def test_batchexecute_url_keeps_rpcids_and_drops_session():
    url = "https://gemini.google.com/_/BardChatUi/data/batchexecute?" + urlencode({
        "rpcids": "MaZiqc", "source-path": "/app", "bl": "boq_assistant-bard-web-server_20261012.08_p0",
        "f.sid": SECRETS[3], "hl": "en", "_reqid": "123456", "rt": "c",
    })
    assert redact_url(url) == (
        "https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=MaZiqc&source-path=%2Fapp"
        "&bl=<redacted>&f.sid=<redacted>&hl=en&_reqid=123456&rt=c"  # the build label looks like a token: fine
    )


def test_shape_redacts_sensitive_keys_and_tokens():
    assert shape({"SNlM0e": AT, "FdrFJe": SECRETS[3], "title": "Trip ideas", "at": "x"}) == {
        "SNlM0e": "str: <redacted>",
        "FdrFJe": "str: <redacted>",
        "title": "str: 'Trip ideas'",
        "at": "str: <redacted>",
    }
    assert shape(["c_1a2b3c4d5e6f7a8b", SECRETS[1], [1760450400, 123000000]]) == [
        "<list of 3>", "str: 'c_1a2b3c4d5e6f7a8b'", "str: <redacted>"]


def test_no_secret_survives_a_capture():
    capture = json.dumps({
        "shape": shape({"token": {"value": SECRETS[2]}, "user": {"name": "Test Owner", "jwt": SECRETS[4]},
                        "chats": [["c_1a2b3c4d5e6f7a8b", "Trip ideas", SECRETS[1]]]}),
        "headers": redact_headers({"cookie": f"SID={SECRETS[1]}", "x-goog-authuser": "0",
                                   "referer": f"https://gemini.google.com/app?f.sid={SECRETS[3]}"}),
        "url": redact_url(f"https://gemini.google.com/_/BardChatUi/data/batchexecute?f.sid={SECRETS[3]}&at={AT}"),
        "postKeys": post_body_keys(urlencode({"f.req": "[]", "at": AT, "pass": SECRETS[5]})),
    })
    assert [s for s in SECRETS if s in capture] == []
    assert "Test Owner" in capture and "Trip ideas" in capture  # ordinary samples survive


def test_post_body_keys_never_values():
    assert post_body_keys(urlencode({"f.req": "[[]]", "at": AT})) == ["at", "f.req"]
    assert post_body_keys(json.dumps({"key": "hunter2", "name": "x"})) == ["key", "name"]
    assert post_body_keys(None) == []


def test_redact_text_masks_the_at_form_field_and_snlm0e():
    bare = AT.split(":")[0]
    body = urlencode({"f.req": '[[["MaZiqc","[34]",null,"generic"]]]', "at": AT})
    raw = f"f.req=%5B%5D&at={AT}&"
    page = (f'window.WIZ_global_data = {{"SNlM0e":"{AT}","FdrFJe":"{SECRETS[3]}","cfb2h":"boq_web_20261015.08_p0"}};'
            f" {{'SNlM0e': '{AT}'}}")
    out = redact_text("\n".join([body, raw, page]))
    assert bare not in out and SECRETS[3] not in out and "boq_web" not in out
    assert out.count("at=<redacted>") == 2
    assert '"SNlM0e":"<redacted>"' in out and "'SNlM0e': \"<redacted>\"" in out


def test_redact_text_masks_cookies_headers_session_ids_and_signed_urls():
    text = "\n".join([
        f"Cookie: SID={SECRETS[1]}; __Secure-1PSIDTS={SECRETS[2]}",
        f"authorization: SAPISIDHASH 1760450400_{SECRETS[1]}",
        f"set SAPISID={SECRETS[1]} and __Secure-3PAPISID={SECRETS[2]} then SAPISID1PHASH 1760_{SECRETS[2]}",
        f"GET https://gemini.google.com/_/BardChatUi/data/batchexecute?rpcids=hNvQHb&f.sid={SECRETS[3]}"
        f"&bl=boq_web_20261015.08_p0&hl=en",
        f"img https://lh3.googleusercontent.com/gg-dl/AOI_d_{SECRETS[2]}=s1024-rj?alr=yes and "
        f"https://work.fife.usercontent.google.com/rd-gg-dl/{SECRETS[1]}",
        f'{{"token": "{SECRETS[2]}", "jwt": "{SECRETS[4]}"}} stray {SECRETS[1]}',
        "What is 2+2? Trip ideas c_1a2b3c4d5e6f7a8b https://gemini.google.com/app/1a2b3c4d5e6f7a8b",
    ])
    out = redact_text(text)
    assert [s for s in SECRETS[:5] if s in out] == [], out
    assert "boq_web" not in out and "AOI_d_" not in out and "gg-dl" not in out
    assert "https://lh3.googleusercontent.com/<redacted>" in out
    assert "rpcids=hNvQHb" in out and "&hl=en" in out
    assert "What is 2+2? Trip ideas c_1a2b3c4d5e6f7a8b https://gemini.google.com/app/1a2b3c4d5e6f7a8b" in out
    assert redact_text(None) == ""
