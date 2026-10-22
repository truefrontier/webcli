"""chromecli P2 (tab actions) and P3 (page actions), offline: preview shapes (nothing sent to the extension),
--dry-run beats --confirm, request shapes, CLI-side validation (missing_expect, bad_target, --find = P4), the exit
codes the extension's answers map to (declined / confirm_timeout 4, url_mismatch 5, eval_disabled 7, sent_unverified 8),
typed values absent from chromecli.log, and agent-help. The extension's own logic is tested by node --test
(extensions/chromecli/test/act.test.mjs, gate.test.mjs)."""

from __future__ import annotations

import json
import time

import pytest

from test_chromecli import FakeExtension, err, ext, home, run  # noqa: F401  (fixtures)

FORM = "http://127.0.0.1:8765"


def ops_sent(fake: FakeExtension) -> list[dict]:
    return [m for m in fake.received if m.get("op")]


def ok_ext(req):
    op, args = req["op"], req["args"]
    base = {"tab": args.get("tab", 101), "origin": FORM, "op": op.split(".", 1)[1], "gated": False, "reasons": [],
            "confirmedInBrowser": False, "dispatched": True, "verified": True}
    return {"ok": True, "result": base}


PREVIEWS = [
    ("open", [f"{FORM}/form.html"]),
    ("focus", ["7"]),
    ("close", ["7"]),
    ("navigate", ["7", f"{FORM}/nav.html"]),
    ("click", ["--tab", "7", "--text", "Next", "--role", "link", "--expect-origin", FORM]),
    ("fill", ["--tab", "7", "--selector", "#email", "--value", "kevin@example.com", "--expect-origin", FORM]),
    ("type", ["--tab", "7", "--ref", "e1", "--value", "abc", "--expect-url", f"{FORM}/form.html"]),
    ("select", ["--tab", "7", "--selector", "#country", "--value", "US", "--expect-origin", FORM]),
    ("press", ["Enter", "--tab", "7", "--selector", "#email", "--expect-origin", FORM]),
    ("eval", ["--tab", "7", "--code", "document.title", "--expect-origin", FORM]),
]


@pytest.mark.parametrize(("cmd", "args"), PREVIEWS)
def test_preview_is_dry_and_sends_nothing(ext, cmd, args):
    fake = ext(ok_ext).start()
    for extra in ([], ["--dry-run"], ["--dry-run", "--confirm"]):
        result = run(cmd, *args, *extra)
        assert result.exit_code == 0, (cmd, extra, result.output)
        plan = json.loads(result.stdout)
        assert plan["dryRun"] is True and plan["op"] == cmd and "Nothing was sent" in plan["hint"]
    assert ops_sent(fake) == []


def test_preview_shapes(ext):
    ext(ok_ext).start()
    plan = json.loads(run("open", "https://example.org/").stdout)
    assert plan["browserConfirm"] is True and plan["autoShare"] is None and plan["origin"] == "https://example.org"
    plan = json.loads(run("open", "https://bank.chase.com/").stdout)
    assert plan["denied"] == "deny: *.chase.com" and plan["browserConfirm"] is False
    plan = json.loads(run("fill", "--tab", "7", "--selector", "#email", "--value", "kevin@example.com",
                          "--expect-origin", FORM).stdout)
    assert plan["target"] == {"selector": "#email"} and plan["expect"] == {"origin": FORM}
    assert plan["value"].startswith("<17 chars sha256:") and "kevin@example.com" not in json.dumps(plan)
    plan = json.loads(run("eval", "--tab", "7", "--code", "1+1", "--expect-origin", FORM).stdout)
    assert plan["browserConfirm"] == "always" and "disabled" in plan["note"]


def test_preview_knows_the_allowlists(ext, home):
    from webcli.core import config

    config.ensure_home()
    (home / "config.toml").write_text(f'[chromecli]\nact_allow = ["{FORM}"]\nread_allow = ["https://docs.python.org"]\n')
    ext(ok_ext).start()
    assert json.loads(run("open", f"{FORM}/form.html").stdout)["autoShare"] == "act"
    plan = json.loads(run("open", "https://docs.python.org/3/").stdout)
    assert plan["autoShare"] == "read" and plan["browserConfirm"] is False


def test_confirm_sends_the_request(ext):
    fake = ext(ok_ext).start()
    t0 = time.time()
    r = run("open", f"{FORM}/form.html", "--confirm")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["dispatched"] is True
    r = run("click", "--tab", "7", "--text", "Place order", "--role", "button", "--expect-origin", FORM,
            "--verify-text", "submitted", "--confirm")
    assert r.exit_code == 0, r.output
    r = run("press", "Escape", "--tab", "7", "--expect-url", f"{FORM}/form.html", "--yes")
    assert r.exit_code == 0, r.output
    sent = ops_sent(fake)
    assert [m["op"] for m in sent] == ["tab.open", "page.click", "page.press"]
    assert all(m["confirm"] is True for m in sent)
    assert sent[0]["args"] == {"url": f"{FORM}/form.html", "active": False, "window": None}
    assert sent[1]["args"] == {"tab": 7, "target": {"text": "Place order", "role": "button"}, "expect": {"origin": FORM},
                               "verify": {"text": "submitted"}}
    assert sent[2]["args"]["target"] is None and sent[2]["args"]["key"] == "Escape"
    # long enough for the in-browser confirm (confirm_timeout 60 + post-checks)
    assert sent[1]["deadlineMs"] / 1000 - t0 >= 85


def test_focus_close_navigate_requests(ext):
    fake = ext(ok_ext).start()
    assert run("focus", "7", "--confirm").exit_code == 0
    assert run("close", "7", "--confirm").exit_code == 0
    assert run("navigate", "7", f"{FORM}/nav.html", "--expect-origin", FORM, "--confirm").exit_code == 0
    sent = ops_sent(fake)
    assert [(m["op"], m["args"]) for m in sent] == [
        ("tab.focus", {"tab": 7}), ("tab.close", {"tab": 7}),
        ("tab.navigate", {"tab": 7, "url": f"{FORM}/nav.html", "expect": {"origin": FORM}})]


def test_missing_expect_is_exit_2_and_sends_nothing(ext):
    fake = ext(ok_ext).start()
    for cmd, args in [("click", ["--tab", "7", "--selector", "#place"]),
                      ("fill", ["--tab", "7", "--selector", "#email", "--value", "x"]),
                      ("press", ["Enter", "--tab", "7"]), ("eval", ["--tab", "7", "--code", "1"])]:
        for extra in ([], ["--confirm"]):
            r = run(cmd, *args, *extra)
            assert r.exit_code == 2, (cmd, r.output)
            assert err(r)["error"] == "missing_expect"
    assert ops_sent(fake) == []


def test_bad_targets_are_exit_2(ext):
    fake = ext(ok_ext).start()
    e = ["--expect-origin", FORM, "--confirm"]
    assert err(run("click", "--tab", "7", *e))["error"] == "bad_target"
    assert err(run("click", "--tab", "7", "--selector", "a", "--ref", "e1", *e))["error"] == "bad_target"
    assert err(run("click", "--tab", "7", "--selector", "a", "--role", "button", *e))["error"] == "bad_target"
    r = run("click", "--tab", "7", "--find", "the next link", *e)
    assert r.exit_code == 2 and err(r)["error"] == "bad_target" and "P4" in err(r)["message"]
    assert run("open", "chrome://settings", "--confirm").exit_code == 2
    assert run("fill", "--tab", "7", "--selector", "#email", *e).exit_code == 2  # no value
    assert ops_sent(fake) == []


@pytest.mark.parametrize(("code", "exit_"), [("declined", 4), ("confirm_timeout", 4), ("paused", 4),
                                             ("not_act_shared", 4), ("origin_blocked", 4), ("url_mismatch", 5),
                                             ("eval_disabled", 7), ("element_not_found", 3),
                                             ("ambiguous_target", 3)])
def test_extension_errors_map_to_exit_codes(ext, code, exit_):
    ext(lambda req: {"ok": False, "error": {"code": code, "message": f"m {code}", "tab": 7}}).start()
    r = run("click", "--tab", "7", "--selector", "#place", "--expect-origin", FORM, "--confirm")
    assert r.exit_code == exit_, r.output
    assert err(r)["error"] == code


def test_failed_read_back_is_exit_8_sent_unverified(ext):
    ext(lambda req: {"ok": False, "error": {"code": "sent_unverified", "message": "read-back differs", "tab": 7,
                                            "dispatched": True, "verified": False, "failed": ["read_back"]}}).start()
    r = run("fill", "--tab", "7", "--selector", "#email", "--value", "x@y.z", "--expect-origin", FORM, "--confirm")
    assert r.exit_code == 8
    e = err(r)
    assert e["error"] == "sent_unverified" and e["failed"] == ["read_back"] and e["dispatched"] is True


def test_typed_values_and_eval_code_are_absent_from_the_log(ext, home, tmp_path):
    ext(ok_ext).start()
    secret = "correct-horse-battery-staple-91"
    vf = tmp_path / "v.txt"
    vf.write_text(secret + "\n")
    assert run("fill", "--tab", "7", "--selector", "#password", "--value-file", str(vf), "--expect-origin", FORM,
               "--confirm").exit_code == 0
    assert run("type", "--tab", "7", "--selector", "#email", "--value", "typed-words-xyz", "--expect-origin", FORM,
               "--confirm").exit_code == 0
    assert run("click", "--tab", "7", "--text", "Secret Button Label", "--expect-origin", FORM,
               "--confirm").exit_code == 0
    run("eval", "--tab", "7", "--code", "alert('eval-code-abc')", "--expect-origin", FORM, "--confirm")
    log = (home / "logs" / "chromecli.log").read_text()
    for s in (secret, "typed-words-xyz", "eval-code-abc", "Secret Button Label"):
        assert s not in log
    assert "op=page.fill" in log and f"value=<{len(secret)} chars sha256:" in log
    assert "op=page.type" in log and "value=<15 chars sha256:" in log
    assert "code=<" in log and "target=text:<19 chars sha256:" in log


def test_value_file_from_stdin(ext):
    fake = ext(ok_ext).start()
    from click.testing import CliRunner
    from webcli.chromecli.cli import group

    r = CliRunner().invoke(group, ["fill", "--tab", "7", "--selector", "#email", "--value-file", "-",
                                   "--expect-origin", FORM, "--confirm"], input="from-stdin\n")
    assert r.exit_code == 0, r.output
    assert ops_sent(fake)[0]["args"]["value"] == "from-stdin"


def test_gated_audit_line(ext, home):
    ext(lambda req: {"ok": False, "error": {"code": "declined", "message": "Kevin denied", "tab": 7, "origin": FORM,
                                            "reasons": ["submit_button", "submit_word:order"]}}).start()
    assert run("click", "--tab", "7", "--selector", "#place", "--expect-origin", FORM, "--confirm").exit_code == 4
    log = (home / "logs" / "chromecli.log").read_text()
    assert "gated=submit_button,submit_word:order" in log and "confirm=declined" in log and "exit=4" in log


def test_wait_is_read_only(ext):
    fake = ext(lambda req: {"ok": True, "result": {"tab": 7, "found": True, "waitedMs": 5}}).start()
    r = run("wait", "--tab", "7", "--text", "submitted", "--timeout", "3")
    assert r.exit_code == 0 and json.loads(r.stdout)["found"] is True
    m = ops_sent(fake)[0]
    assert m["op"] == "page.wait" and m["confirm"] is False and m["args"] == {"tab": 7, "timeoutMs": 3000, "text": "submitted"}
    assert run("wait", "--tab", "7").exit_code == 2
    assert run("wait", "--tab", "7", "--text", "a", "--selector", "b").exit_code == 2


def test_agent_help_lists_the_write_commands():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    names = {c["name"] for c in ref["commands"]}
    assert {"open", "focus", "close", "navigate", "click", "type", "fill", "select", "press", "wait", "eval"} <= names
    writes = " ".join(ref["writes"])
    for w in ("open URL --confirm", "close TAB --confirm", "navigate TAB URL", "`click`", "eval", "dryRun"):
        assert w in writes, w
    notes = " ".join(ref["notes"])
    assert "missing_expect" in notes and "sent_unverified" in notes and "declined" in notes


def test_protocol_ops():
    from webcli.chromecli import protocol

    for op in ("tab.open", "tab.focus", "tab.close", "tab.navigate", "page.click", "page.type", "page.fill",
               "page.select", "page.press", "page.wait", "page.eval"):
        assert protocol.validate_request({"v": 1, "op": op})["op"] == op
    assert protocol.exit_code("eval_disabled") == 7
