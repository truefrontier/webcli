"""Offline tests (from cgpt): nothing the merged redaction lets through contains a token, cookie, device id, or signed link."""

from __future__ import annotations

import json

from webcli.core.redact import post_body_keys, redact_headers, redact_text, redact_url, shape, tokenish

# Synthetic values in the shapes ChatGPT uses. None of these is real.
JWT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LW93bmVyIn0.ZmFrZS1zaWduYXR1cmUtdGVzdA"
JWE = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..ZmFrZWl2MTIz.ZmFrZWNpcGhlcnRleHQwMTIzNDU2Nzg5.ZmFrZXRhZzEy"
SENTINEL = "gAAAAABnFakeSentinelToken0123456789abcdefXYZ=="
PROOF = "gAAAAACWzI3LDAsIk1vbiBPY3QgMTggMjAyNiAxMzowMDowMCBHTVQtMDUwMA~S"
CF = "Xx9f.FakeClearance-1760800000-1.2.1.1-AbCdEf0123456789"
DEVICE = "0f8e7d6c-5b4a-4321-9876-0123456789ab"
SIG = "Zm9vYmFyRmFrZVNpZzAxMjM0NTY3ODk%3D"
APIKEY = "sk-proj-FAKEfake0123456789abcdefABCDEF"
SECRETS = [JWT, JWE, SENTINEL, PROOF, CF, DEVICE, SIG, APIKEY, "hunter2"]
SIGNED = (f"https://sdmntprwestus.oaiusercontent.com/files/00000000-aaaa-bbbb-cccc-000000000001/raw?se=2026-10-18T19%3A"
          f"00%3A00Z&sp=r&sv=2024-08-04&sr=b&scid=00000000-0000-0000-0000-000000000000&skoid=abc123def456&sktid=x"
          f"&skt=2026&ske=2026&sks=b&skv=2024&sig={SIG}")
ESTUARY = f"https://chatgpt.com/backend-api/estuary/content?id=file_00000000fake&ts=489&p=fs&cid=1&sig={SIG}"
CHAT = "https://chatgpt.com/c/68f3c1a2-1234-8000-9abc-0123456789ab"


def test_tokenish():
    assert [tokenish(s) for s in (JWT, JWE, SENTINEL, CF, APIKEY)] == [True] * 5
    # Chat and message ids are UUIDs and stay readable; model slugs and prose are not tokens.
    assert [tokenish(s) for s in ("68f3c1a2-1234-8000-9abc-0123456789ab", "gpt-5-instant", "What is 2+2?",
                                  "application/json")] == [False] * 4


def test_headers_drop_auth_sentinel_device_and_cookie_values():
    out = redact_headers({
        "Authorization": f"Bearer {JWT}",
        "Cookie": f"__Secure-next-auth.session-token.0={JWE}; oai-did={DEVICE}; cf_clearance={CF}",
        "OpenAI-Sentinel-Chat-Requirements-Token": SENTINEL,
        "OpenAI-Sentinel-Proof-Token": PROOF,
        "OAI-Device-Id": DEVICE,
        "ChatGPT-Account-Id": DEVICE,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Referer": CHAT,
    })
    assert out == {
        "Accept": "text/event-stream",
        "Authorization": "<redacted>",
        "ChatGPT-Account-Id": "<redacted>",
        "Content-Type": "application/json",
        "Cookie": "<redacted>",
        "OAI-Device-Id": "<redacted>",
        "OpenAI-Sentinel-Chat-Requirements-Token": "<redacted>",
        "OpenAI-Sentinel-Proof-Token": "<redacted>",
        "Referer": CHAT,
    }


def test_signed_links_keep_only_their_host_or_lose_every_query_value():
    assert redact_url(SIGNED) == "https://sdmntprwestus.oaiusercontent.com/<redacted>"
    assert redact_url("https://files.oaiusercontent.com/file-FAKE123?se=x&sig=y") == \
        "https://files.oaiusercontent.com/<redacted>"
    out = redact_url(ESTUARY)
    assert SIG not in out and "file_00000000fake" not in out and out.startswith(
        "https://chatgpt.com/backend-api/estuary/content?id=<redacted>&ts=<redacted>")
    # An ordinary chatgpt.com URL keeps its UUID path and harmless params.
    assert redact_url(CHAT + "?model=gpt-5") == CHAT + "?model=gpt-5"


def test_redact_text_masks_every_secret_shape():
    text = "\n".join([
        f"authorization: Bearer {JWT}",
        f"Set-Cookie: __Secure-next-auth.session-token={JWE}; Path=/; HttpOnly",
        f"cookie names: __Secure-next-auth.session-token.0={JWE} oai-did={DEVICE} cf_clearance={CF} __cf_bm={CF}",
        f"openai-sentinel-chat-requirements-token: {SENTINEL}",
        f'{{"accessToken": "{JWT}", "sessionToken": "{JWE}", "user": {{"name": "Test Owner"}}, '
        f'"expires": "2026-11-17T00:00:00.000Z", "oai_device_id": "{DEVICE}", "proofofwork": {{"seed": "0.42"}}}}',
        f'{{"token": "{SENTINEL}", "turnstile": {{"dx": "{PROOF}"}}, "download_url": "{SIGNED}"}}',
        f"img {SIGNED} and {ESTUARY}",
        f"key {APIKEY} and a stray {JWT} in prose",
        f"open {CHAT} model gpt-5-instant: What is 2+2? Test Owner",
    ])
    out = redact_text(text)
    assert [s for s in SECRETS if s in out] == [], out
    assert "skoid=abc123" not in out and "sdmntprwestus.oaiusercontent.com/<redacted>" in out
    assert f"open {CHAT} model gpt-5-instant: What is 2+2? Test Owner" in out  # ordinary text survives
    assert '"user": {"name": "Test Owner"}' in out
    assert redact_text(None) == ""


def test_shape_redacts_sensitive_keys_and_tokens():
    assert shape({"accessToken": JWT, "user": {"email": "owner@example.test"}, "title": "Trip ideas",
                  "oai-did": DEVICE, "download_url": SIGNED}) == {
        "accessToken": "str: <redacted>",
        "user": {"email": "str: 'owner@example.test'"},
        "title": "str: 'Trip ideas'",
        "oai-did": "str: <redacted>",
        "download_url": "str: 'https://sdmntprwestus.oaiusercontent.co…'",
    }
    assert shape([SENTINEL, "gpt-5"]) == ["<list of 2>", "str: <redacted>", "str: 'gpt-5'"]


def test_no_secret_survives_a_capture():
    capture = json.dumps({
        "shape": shape({"session": {"accessToken": JWT}, "token": SENTINEL, "chats": [["Trip ideas", CF]]}),
        "headers": redact_headers({"authorization": f"Bearer {JWT}", "oai-language": "en-US"}),
        "url": redact_url(SIGNED),
        "postKeys": post_body_keys(json.dumps({"action": "next", "messages": [], "model": "gpt-5"})),
    })
    assert [s for s in SECRETS if s in capture] == []
    assert "Trip ideas" in capture and "gpt-5" not in json.loads(capture)["postKeys"]


def test_post_body_keys_never_values():
    assert post_body_keys(json.dumps({"action": "next", "model": "gpt-5", "token": SENTINEL})) == \
        ["action", "model", "token"]
    assert post_body_keys("a=1&b=2") == ["a", "b"]
    assert post_body_keys(None) == []


def test_redact_text_keeps_a_schemeless_host_and_path_but_not_its_tokens():
    assert redact_text("GET chatgpt.com/backend-api/conversation/68f3c1a2-1234-8000-9abc-0123456789ab/v1") == \
        "GET chatgpt.com/backend-api/conversation/68f3c1a2-1234-8000-9abc-0123456789ab/v1"
    out = redact_text(f"GET sdmntprwestus.oaiusercontent.com/files/x?sig={SIG}")
    assert SIG not in out and out == "GET sdmntprwestus.oaiusercontent.com/<redacted>?sig=<redacted>"
    assert JWT not in redact_text(f"GET chatgpt.com/x/{JWT}/y")
