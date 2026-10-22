"""chromecli P4 (Jev, BYOK), offline: `find` and `--find` / `--jev` request shapes, Jev exit codes (jev_not_configured 7,
jev_error 5), the auto-fallback warning, intents absent from chromecli.log, agent-help. Jev itself (request bodies,
parsers, gating, the key never in a port message) is tested by node --test (extensions/chromecli/test/jev.test.mjs)."""

from __future__ import annotations

import json

import pytest

from test_chromecli import FakeExtension, err, ext, home, run  # noqa: F401  (fixtures)
from test_chromecli_act import FORM, ok_ext, ops_sent

ROWS = [{"ref": "e1", "score": 0.98, "source": "jev", "tag": "input", "role": "textbox", "name": "Email"},
        {"ref": "e2", "score": 0.04, "source": "jev", "tag": "input", "role": "textbox", "name": "Password"}]


def find_ext(req):
    if req["op"] == "page.find":
        return {"ok": True, "result": {"tab": 7, "origin": FORM, "results": ROWS, "pick": "e1",
                                       "jev": {"mode": req["args"].get("jev"), "used": True}}}
    return ok_ext(req)


def test_find_request_and_rows(ext):
    fake = ext(find_ext).start()
    r = run("find", "--tab", "7", "the email field", "--limit", "3", "--format", "json")
    assert r.exit_code == 0, r.output
    assert [x["ref"] for x in json.loads(r.stdout)] == ["e1", "e2"]
    m = ops_sent(fake)[0]
    assert m["op"] == "page.find" and m["confirm"] is False
    assert m["args"] == {"tab": 7, "intent": "the email field", "jev": "auto", "limit": 3}
    r = run("find", "--tab", "7", "the email field", "--jev", "off", "--format", "json")
    assert ops_sent(fake)[1]["args"]["jev"] == "off"


def test_find_fallback_warns_on_stderr(ext):
    def fell_back(req):
        rows = [dict(x, source="heuristic") for x in ROWS]
        return {"ok": True, "result": {"tab": 7, "origin": FORM, "results": rows, "pick": "e1",
                                       "jev": {"mode": "auto", "used": False, "error": "Jev returned HTTP 401."}}}
    ext(fell_back).start()
    r = run("find", "--tab", "7", "the email field", "--format", "json")
    assert r.exit_code == 0
    assert json.loads(r.stdout)[0]["source"] == "heuristic"
    assert "jev_unavailable" in r.stderr


@pytest.mark.parametrize(("code", "exit_"), [("jev_not_configured", 7), ("jev_error", 5), ("ambiguous_target", 3)])
def test_jev_errors_map_to_exit_codes(ext, code, exit_):
    ext(lambda req: {"ok": False, "error": {"code": code, "message": f"m {code}", "tab": 7}}).start()
    r = run("find", "--tab", "7", "x", "--jev", "on")
    assert r.exit_code == exit_ and err(r)["error"] == code
    r = run("click", "--tab", "7", "--find", "the delete account button", "--jev", "on", "--expect-origin", FORM,
            "--confirm")
    assert r.exit_code == exit_ and err(r)["error"] == code


def test_find_target_and_jev_flag_on_page_actions(ext):
    fake = ext(ok_ext).start()
    r = run("click", "--tab", "7", "--find", "the delete account button", "--expect-origin", FORM)
    assert r.exit_code == 0 and json.loads(r.stdout)["target"] == {"find": "the delete account button"}
    assert ops_sent(fake) == []  # preview
    assert run("fill", "--tab", "7", "--find", "the email field", "--value", "a@b.c", "--jev", "off",
               "--expect-origin", FORM, "--confirm").exit_code == 0
    m = ops_sent(fake)[0]
    assert m["op"] == "page.fill" and m["args"]["target"] == {"find": "the email field"} and m["args"]["jev"] == "off"
    r = run("click", "--tab", "7", "--find", "x", "--jev", "maybe", "--expect-origin", FORM, "--confirm")
    assert r.exit_code == 2


def test_intents_are_absent_from_the_log(ext, home):
    ext(find_ext).start()
    assert run("find", "--tab", "7", "the secret intent words", "--format", "json").exit_code == 0
    assert run("click", "--tab", "7", "--find", "another secret intent", "--expect-origin", FORM,
               "--confirm").exit_code == 0
    log = (home / "logs" / "chromecli.log").read_text()
    assert "secret intent" not in log
    assert "intent=<23 chars sha256:" in log and "target=find:<21 chars sha256:" in log
    assert "jev=auto" in log


def test_agent_help_documents_jev():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    assert "find" in {c["name"] for c in ref["commands"]}
    text = " ".join(ref["notes"]) + " ".join(ref["writes"])
    for w in ("--find INTENT", "jev_not_configured", "jev_error", "never removes", "jevConfigured"):
        assert w in text, w


def test_protocol_find_is_read_only():
    from webcli.chromecli import protocol

    assert protocol.validate_request({"v": 1, "op": "page.find"})["op"] == "page.find"
    assert "page.find" not in protocol.MUTATING_OPS
    assert protocol.exit_code("jev_not_configured") == 7 and protocol.exit_code("jev_error") == 5


def test_091_navigate_preview_and_notes(ext):
    ext(ok_ext).start()
    r = run("navigate", "7", "https://example.org/x")
    plan = json.loads(r.stdout)
    assert plan["browserConfirm"] is True and "same-origin move in a tab Kevin Share-act'd" in plan["note"]
    notes = " ".join(json.loads(run("agent-help", "--format", "json").stdout)["notes"])
    for w in ("act-tab safe floor", "tabKind", "jevInfo", "bot tabs gate more strictly", "same-origin navigate"):
        assert w in notes, w


def test_091_audit_line_has_tab_kind_and_jev_info(ext, home):
    def res(req):
        out = ok_ext(req)
        out["result"].update(tabKind="act", jevInfo={"choice": "safe", "confidence": 0.72, "gated": False})
        return out
    ext(res).start()
    assert run("click", "--tab", "7", "--selector", "#plain", "--expect-origin", FORM, "--confirm").exit_code == 0
    log = (home / "logs" / "chromecli.log").read_text()
    assert "tab_kind=act" in log and "jev_info=safe:info" in log
