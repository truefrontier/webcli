"""chromecli 0.10.0, offline: pre-approval grants (CLI validation, previews, requests, the audit line with the grant id
and Kevin's quote, values never logged), plus the catalog-linter test-drive fixes on the CLI side (list --all,
wait --expect-*, default screenshot path, read header, preview probe, the chromecli exit-code table in agent-help).
The extension side is tested by node --test (grants.test.mjs, grants-worker.test.mjs, testdrive.test.mjs)."""

from __future__ import annotations

import base64
import json
import re

import pytest

from test_chromecli import err, ext, home, run  # noqa: F401  (fixtures)

FORM = "http://127.0.0.1:8765"
QUOTE = "yes go ahead and place the order, it's under $50"


def ops_sent(fake) -> list[dict]:
    return [m for m in fake.received if m.get("op")]


def grant_ext(req):
    op, args = req["op"], req["args"]
    if op == "grant.add":
        g = {"id": "g1a2b3", "op": args["op"], "origin": args["origin"], "target": args.get("target"),
             "quote": args["quote"], "uses": args["uses"], "usesLeft": args["uses"], "expiresInSec": args["ttlSec"]}
        return {"ok": True, "result": {"op": "grant.add", "added": True, "grant": g}}
    if op == "grant.list":
        return {"ok": True, "result": {"enabled": True, "grants": [{"id": "g1a2b3", "op": "click", "origin": FORM,
                "target": {"text": "Place order"}, "quote": QUOTE, "usesLeft": 1, "expiresInSec": 1700,
                "maxAmount": 49.99, "currency": "USD"}]}}
    if op == "grant.revoke":
        return {"ok": True, "result": {"op": "grant.revoke", "revoked": 1}}
    if op == "page.click":
        return {"ok": True, "result": {"tab": 7, "origin": FORM, "op": "click", "gated": True,
                "reasons": ["submit_button"], "confirmedInBrowser": False, "confirmedBy": "grant:g1a2b3",
                "grant": {"id": "g1a2b3", "quote": QUOTE, "usesLeft": 0, "amount": 44.99}, "dispatched": True,
                "openedTabs": [{"id": 120, "url": f"{FORM}/receipt", "share": "act"}]}}
    if op == "page.probe":
        return {"ok": True, "result": {"tab": 7, "origin": FORM, "op": args["op"], "found": True, "tabKind": "act",
                "element": {"tag": "button", "name": "Place order"},
                "gateEstimate": {"heuristic": ["submit_button"], "wouldPrompt": True, "grantSkippable": True}}}
    return {"ok": True, "result": {"echo": op, "args": args}}


ADD = ["grant", "add", "--op", "click", "--origin", f"{FORM}/checkout?x=1", "--target-text", "Place order",
       "--role", "button", "--max-amount", "49.99", "--quote", QUOTE]


def test_grant_add_previews_without_confirm_and_sends_nothing(ext):
    fake = ext(grant_ext).start()
    r = run(*ADD)
    assert r.exit_code == 0, r.output
    plan = json.loads(r.stdout)
    assert plan["dryRun"] is True and plan["op"] == "click" and plan["origin"] == FORM
    assert plan["target"] == {"text": "Place order", "role": "button"}
    assert plan["maxAmount"] == 49.99 and plan["currency"] == "USD"
    assert plan["uses"] == 1 and plan["ttlSec"] == 1800 and plan["quote"] == QUOTE
    assert "grants_disabled" in plan["requires"] and "own words" in plan["rule"]
    assert ops_sent(fake) == []


def test_grant_add_confirm_sends_grant_add_and_logs_id_and_quote(ext, home):
    fake = ext(grant_ext).start()
    r = run(*ADD, "--uses", "2", "--ttl", "1h30m", "--confirm")
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["grant"]["id"] == "g1a2b3"
    m = ops_sent(fake)[0]
    assert m["op"] == "grant.add" and m["confirm"] is True
    assert m["args"] == {"op": "click", "origin": FORM, "target": {"text": "Place order", "role": "button"},
                         "quote": QUOTE, "uses": 2, "ttlSec": 5400, "maxAmount": 49.99, "currency": "USD"}
    log = (home / "logs" / "chromecli.log").read_text()
    line = next(x for x in log.splitlines() if "op=grant.add" in x)
    assert "grant=g1a2b3" in line and json.dumps(QUOTE) in line and "uses=2 ttl_s=5400" in line


def test_grant_value_never_on_the_wire_back_or_in_the_log(ext, home, tmp_path):
    fake = ext(grant_ext).start()
    f = tmp_path / "v.txt"
    f.write_text("as-described-review\n")
    r = run("grant", "add", "--op", "fill", "--origin", FORM, "--target-text", "Store name", "--value-file", str(f),
            "--quote", "type as-described-review to confirm the deletion")
    plan = json.loads(r.stdout)
    assert plan["value"].startswith("<19 chars sha256:") and "as-described-review" not in plan["value"]
    r = run("grant", "add", "--op", "fill", "--origin", FORM, "--target-text", "Store name", "--value-file", str(f),
            "--quote", "type it", "--confirm")
    assert r.exit_code == 0, r.output
    assert ops_sent(fake)[0]["args"]["value"] == "as-described-review"
    log = (home / "logs" / "chromecli.log").read_text()
    assert "value=<19 chars sha256:" in log
    assert "as-described-review\"" not in log.replace('"type it"', "")


@pytest.mark.parametrize(("extra", "msg"), [
    (["--quote", "   "], "quote is required"),
    (["--quote", "x" * 501], "over 500"),
    (["--ttl", "25h"], "between 1m and 24h"),
    (["--ttl", "30"], "between 1m and 24h"),
    (["--ttl", "soon"], "isn't a duration"),
    (["--currency", "EUR"], "need --max-amount"),
])
def test_grant_add_validation(ext, extra, msg):
    ext(grant_ext).start()
    base = ["grant", "add", "--op", "click", "--origin", FORM, "--target-text", "Pay", "--quote", QUOTE]
    r = run(*base, *extra)
    assert r.exit_code == 2, r.output
    assert msg in err(r)["message"]


def test_grant_add_target_rules(ext):
    ext(grant_ext).start()
    q = ["--quote", QUOTE]
    assert err(run("grant", "add", "--op", "click", "--origin", FORM, *q))["error"] == "bad_target"
    assert err(run("grant", "add", "--op", "click", "--origin", FORM, "--target-text", "a", "--selector", "#a", *q))["error"] == "bad_target"
    assert err(run("grant", "add", "--op", "navigate", "--origin", FORM, "--target-text", "a", *q))["error"] == "bad_target"
    assert err(run("grant", "add", "--op", "click", "--origin", "chrome://settings", "--target-text", "a", *q))["error"] == "bad_request"
    assert run("grant", "add", "--op", "eval", "--origin", FORM, *q).exit_code == 2  # not a grant op
    assert run("grant", "add", "--op", "click", "--origin", FORM, "--target-text", "a").exit_code == 2  # --quote required
    plan = json.loads(run("grant", "add", "--op", "open", "--origin", FORM, "--url-prefix", f"{FORM}/admin", *q).stdout)
    assert plan["target"] is None and plan["urlPrefix"] == f"{FORM}/admin"


def test_grants_disabled_is_exit_4_with_a_hint(ext):
    ext(lambda req: {"ok": False, "error": {"code": "grants_disabled", "message": "Pre-approvals are off."}}).start()
    r = run(*ADD, "--confirm")
    assert r.exit_code == 4
    e = err(r)
    assert e["error"] == "grants_disabled" and "popup" in e["hint"]


def test_grant_list_and_revoke(ext):
    fake = ext(grant_ext).start()
    res = json.loads(run("grant", "list", "--format", "json").stdout)
    assert res["enabled"] is True and res["grants"][0]["quote"] == QUOTE
    text = run("grant", "list", "--format", "table").stdout
    assert "g1a2b3" in text and QUOTE in text and "<= 49.99 USD" in text
    assert json.loads(run("grant", "revoke", "g1a2b3").stdout)["dryRun"] is True
    assert run("grant", "revoke", "g1a2b3", "--confirm").exit_code == 0
    assert run("grant", "revoke", "--all", "--confirm").exit_code == 0
    assert run("grant", "revoke").exit_code == 2
    assert run("grant", "revoke", "g1", "--all").exit_code == 2
    sent = [(m["op"], m["args"], m["confirm"]) for m in ops_sent(fake)]
    assert sent == [("grant.list", {}, False), ("grant.list", {}, False),
                    ("grant.revoke", {"id": "g1a2b3"}, True), ("grant.revoke", {"all": True}, True)]


def test_a_grant_answered_click_logs_confirmed_by_and_quote(ext, home):
    ext(grant_ext).start()
    r = run("click", "--tab", "7", "--text", "Place order", "--expect-origin", FORM, "--confirm")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["confirmedBy"] == "grant:g1a2b3" and out["openedTabs"][0]["share"] == "act"
    line = next(x for x in (home / "logs" / "chromecli.log").read_text().splitlines() if "op=page.click" in x)
    assert "grant_used=g1a2b3" in line and json.dumps(QUOTE) in line and "grant_amount=44.99" in line
    assert "opened=120:act" in line


def test_page_preview_carries_a_read_only_probe(ext):
    fake = ext(grant_ext).start()
    plan = json.loads(run("click", "--tab", "7", "--text", "Place order", "--expect-origin", FORM).stdout)
    assert plan["probe"]["found"] is True and plan["probe"]["gateEstimate"]["wouldPrompt"] is True
    assert "read-only probe" in plan["hint"]
    m = ops_sent(fake)[0]
    assert m["op"] == "page.probe" and m["confirm"] is False and m["args"]["op"] == "click"
    # no bridge: the preview still prints, with probe.error
    fake.stop()


def test_preview_without_a_bridge_still_prints(home):
    plan = json.loads(run("fill", "--tab", "7", "--selector", "#e", "--value", "x", "--expect-origin", FORM).stdout)
    assert plan["dryRun"] is True and plan["probe"]["error"] == "bridge_down"


def test_list_all_wait_expect_and_screenshot_default_path(ext, home):
    shots = []

    def handler(req):
        if req["op"] == "tabs.screenshot":
            shots.append(req)
            return {"ok": True, "result": {"tab": 7, "format": "png", "data": base64.b64encode(b"\x89PNG x").decode()}}
        return {"ok": True, "result": []}

    fake = ext(handler).start()
    assert run("list", "--all", "--format", "json").exit_code == 0
    assert run("wait", "--tab", "7", "--text", "ok", "--expect-origin", FORM, "--expect-url", f"{FORM}/a").exit_code == 0
    r = run("screenshot", "7")
    assert r.exit_code == 0, r.output
    path = json.loads(r.stdout)["path"]
    assert re.search(r"/chromecli/screenshots/tab7-\d{8}-\d{6}\.png$", path)
    assert path.startswith(str(home.resolve())) and open(path, "rb").read() == b"\x89PNG x"
    sent = {m["op"]: m["args"] for m in ops_sent(fake)}
    assert sent["tabs.list"] == {"active": False, "all": True}
    assert sent["page.wait"]["expect"] == {"origin": FORM, "urlPrefix": f"{FORM}/a"}


def test_agent_help_has_chromecli_exit_codes_and_the_grant_rule():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    codes = ref["exitCodes"]
    assert set(codes) == {"0", "2", "3", "4", "5", "7", "8"}
    assert "eval_disabled" in codes["7"] and "grants_disabled" in codes["4"] and "sent_unverified" in codes["8"]
    assert "sign-in" not in json.dumps(codes) and "rate limited" not in json.dumps(codes)  # not webcli's table
    notes = " ".join(ref["notes"])
    assert "ONLY create a grant that quotes Kevin's own words from the current conversation" in notes
    assert "openedTabs" in notes and "list --all" in notes and "shadow" in notes.lower()
    names = {c["name"] for c in ref["commands"]}
    assert {"grant add", "grant list", "grant revoke"} <= names
    ev = next(c for c in ref["commands"] if c["name"] == "eval")
    assert "DISABLED" in ev["summary"] and "browser-confirmed" not in ev["summary"]
    add = next(c for c in ref["commands"] if c["name"] == "grant add")
    assert any("--quote" in o["flags"] and "EXACT words" in o["help"] for o in add["options"])
