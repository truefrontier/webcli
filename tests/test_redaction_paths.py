"""Required (1): redaction really runs on every path that prints or logs, and no secret shape survives it."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from webcli.apps import llmcli
from webcli.core import capture, config, output
from webcli.core.errors import AuthRequired
from webcli.core.output import die
from webcli.core.redact import REDACTED, redact_headers, redact_text, redact_url, shape
from webcli.core.identities import Identity

# Synthetic values in the shapes the sites use. None of these is real.
JWT = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0LW93bmVyIn0.ZmFrZS1zaWduYXR1cmUtdGVzdA"
SESSION = "eyJhbGciOiJkaXIiLCJlbmMiOiJBMjU2R0NNIn0..ZmFrZWl2MTIz.ZmFrZWNpcGhlcnRleHQwMTIzNDU2Nzg5.ZmFrZXRhZzEy"
APIKEY = "sk-proj-FAKEfake0123456789abcdefABCDEF"
DEVICE = "0f8e7d6c-5b4a-4321-9876-0123456789ab"
CLEARANCE = "Xx9f.FakeClearance-1760800000-1.2.1.1-AbCdEf0123456789"
PSID = "g.a000testPSIDvalue0123456789abcdefFAKE0076"
SAS_SIG = "Zm9vYmFyRmFrZVNpZzAxMjM0NTY3ODk%3D"
SAS = (f"https://acct.blob.core.windows.net/files/x.png?se=2026-10-18T19%3A00%3A00Z&sp=r&sv=2024-08-04&sr=b"
       f"&skoid=abc123def456&sig={SAS_SIG}")
OAI_FILE = f"https://sdmntprwestus.oaiusercontent.com/files/00000000-aaaa/raw?se=x&sig={SAS_SIG}"
SECRETS = [JWT, SESSION, APIKEY, DEVICE, CLEARANCE, PSID, SAS_SIG, "skoid=abc123def456"]


def leaked(text: str) -> list[str]:
    return [secret for secret in SECRETS if secret in text]


@pytest.mark.parametrize("text", [
    f"Cookie: __Secure-next-auth.session-token={SESSION}; oai-did={DEVICE}; cf_clearance={CLEARANCE}",
    f"set-cookie: __Secure-1PSID={PSID}; Path=/",
    f"__Secure-next-auth.session-token.0={SESSION} and __Secure-1PSID={PSID}",
    f"Authorization: Bearer {JWT}",
    f"request had bearer {JWT} in it",
    f"a stray JWT {JWT} in prose",
    f"the key is {APIKEY}",
    f"oai-did={DEVICE}",
    f'{{"oai-did": "{DEVICE}", "accessToken": "{JWT}"}}',
    f"cf_clearance={CLEARANCE}",
    f"download {SAS} now",
    f"download {OAI_FILE} now",
])
def test_redact_text_drops_every_secret_shape(text):
    out = redact_text(text)
    assert leaked(out) == [], out
    assert REDACTED in out


def test_redact_url_headers_and_shape():
    assert redact_url(SAS) == "https://acct.blob.core.windows.net/<redacted>"
    assert redact_url(OAI_FILE) == "https://sdmntprwestus.oaiusercontent.com/<redacted>"
    headers = redact_headers({"Cookie": f"cf_clearance={CLEARANCE}", "Authorization": f"Bearer {JWT}",
                              "OAI-Device-Id": DEVICE, "X-Api-Key": APIKEY, "Accept": "text/html"})
    assert leaked(json.dumps(headers)) == [] and headers["Accept"] == "text/html"
    assert leaked(json.dumps(shape({"session": {"accessToken": JWT}, "oai-did": DEVICE, "url": SAS,
                                    "note": f"key {APIKEY}"}))) == []


def test_die_redacts_the_json_error_and_its_log_line(capsys):
    with pytest.raises(SystemExit) as code:
        die(f"Chrome said: Authorization: Bearer {JWT}", 5, hint=f"cookie oai-did={DEVICE}",
            detail=f"see {SAS}")
    assert code.value.code == 5
    err = capsys.readouterr().err
    assert leaked(err) == [] and REDACTED in err
    payload = json.loads(err)
    assert payload["code"] == 5 and payload["detail"].startswith("see https://acct.blob.core.windows.net/<redacted>")
    log = (config.home() / "logs" / "llmcli.log").read_text()
    assert "exit 5" in log and leaked(log) == [] and REDACTED in log


def test_die_redacts_human_errors_too(monkeypatch, capsys):
    monkeypatch.setattr(output, "json_errors", lambda: False)
    with pytest.raises(SystemExit):
        die(f"the key {APIKEY} leaked", 5, hint=f"Bearer {JWT}")
    err = capsys.readouterr().err
    assert leaked(err) == [] and err.count(REDACTED) == 2


def test_log_lines_are_redacted_and_private():
    output.log(f"daemon: Set-Cookie: __Secure-1PSID={PSID}; and {SAS}", name="probe")
    path = config.home() / "logs" / "probe.log"
    text = path.read_text()
    assert leaked(text) == [] and REDACTED in text
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_logged_traceback_is_redacted_whole():
    try:
        raise RuntimeError(f"Authorization: Bearer {JWT}")
    except RuntimeError:
        output.logger("trace").exception("command failed")
    text = (config.home() / "logs" / "trace.log").read_text()
    assert "Traceback" in text and leaked(text) == []


def test_a_command_error_with_a_token_is_redacted_end_to_end(monkeypatch):
    """The real path: a site error carrying a token, through the CLI's error mapping, to stderr and the log."""
    from webcli.sites.gemini import fetch

    Identity("google:default").profile.mkdir(parents=True)
    (Identity("google:default").profile / "Local State").write_text("{}")

    async def models(**_kwargs):
        raise AuthRequired(f"Gemini refused: Cookie: __Secure-1PSID={PSID}; Authorization: Bearer {JWT}")

    monkeypatch.setattr(fetch, "models", models)
    result = CliRunner().invoke(llmcli.build(), ["gemini", "models"])
    assert result.exit_code == 4
    assert leaked(result.stderr) == [] and REDACTED in result.stderr
    log = (config.home() / "logs" / "llmcli.log").read_text()
    assert leaked(log) == [] and "exit 4" in log


def test_a_usage_error_echoing_a_token_is_redacted():
    """click echoes a bad argument back; when it is a token, that echo is redacted too (JSON and human)."""
    result = CliRunner().invoke(llmcli.build(), ["chatgpt", "chats", "show", JWT])
    assert result.exit_code == 2 and JWT not in result.stderr and REDACTED in result.stderr
    runner = CliRunner()
    result = runner.invoke(llmcli.build(), ["gemini", "chats", "show", f"c_{APIKEY}"], env={"LLMCLI_FORMAT": "text"})
    assert result.exit_code == 2 and APIKEY not in result.output


def test_notes_and_warnings_are_redacted(monkeypatch, capsys):
    monkeypatch.setattr(output, "json_errors", lambda: False)
    output.note(f"chat at {OAI_FILE}", quiet=False)
    output.warn(f"warning: Bearer {JWT}")
    err = capsys.readouterr().err
    assert leaked(err) == [] and "sdmntprwestus.oaiusercontent.com/<redacted>" in err and "Bearer <redacted>" in err


def test_capture_lines_keep_names_and_drop_values():
    line = capture.request_line("POST", f"https://chatgpt.com/backend-api/x?token={JWT}&oai-did={DEVICE}",
                                json.dumps({"accessToken": JWT, "model": "gpt-6"}))
    assert leaked(line) == [] and "keys=['accessToken', 'model']" in line and "?oai-did,token" in line
    path = capture.write("chatgpt", {"traffic": [f"GET {SAS}"], "title": f"Bearer {JWT}"})
    text = path.read_text()
    assert leaked(text) == [] and path.stat().st_mode & 0o777 == 0o600
