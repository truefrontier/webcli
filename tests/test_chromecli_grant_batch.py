"""chromecli 0.11.5, offline: `grant add-batch` (one quote covering several grants) on the CLI side: --file / --grant
parsing, defaults, validation, the preview, the request, exit 9 with per-grant results, --partial, the audit line with
each grant's verdict and matched clause, --max-amount 0 and --ttl default. The clause logic itself (split, Jev mapping,
limits, all-or-nothing) is tested in node (extensions/chromecli/test/grant-batch.test.mjs) with a mocked Jev."""

from __future__ import annotations

import json

import pytest

from test_chromecli import err, ext, home, run  # noqa: F401  (fixtures)

ADMIN = "https://admin.shopify.com"
APP = "https://asdescribed.truefrontier.app"
QUOTE_B = ("Yes, pre-approve chromecli clicks on as-described-review for the next 45 minutes: Install once, Approve on "
           "the $0 Starter plan once, Scan products once, Use the title or Use the size once, Use the description for "
           "all 3 once, Apply to 3 products once, and Undo 3 times.")
CLAUSES = ["Install once", "Approve on the $0 Starter plan once", "Scan products once",
           "Use the title or Use the size once", "Use the description for all 3 once", "Apply to 3 products once",
           "Undo 3 times"]
GRANTS = [
    {"target-text": "Install", "role": "button"},
    {"target-text": "Approve", "role": "button", "max-amount": 0},
    {"target-text": "Scan products", "frame-origin": APP},
    {"target-text": "Use the title", "frame-origin": APP},
    {"target-text": "Use the description for all 3", "frame-origin": APP},
    {"target-text": "Apply to 3 products", "frame-origin": APP},
    {"target-text": "Undo", "frame-origin": APP, "uses": 3},
]


def ops_sent(fake) -> list[dict]:
    return [m for m in fake.received if m.get("op")]


def batch_ext(req):
    if req["op"] != "grant.add_batch":
        return {"ok": True, "result": {}}
    args = req["args"]
    results = []
    for i, g in enumerate(args["grants"], 1):
        cl = {"id": f"c{i}", "text": CLAUSES[i - 1], "uses": 3 if i == 7 else 1}
        results.append({"index": i, "op": g["op"], "target": g["target"]["text"], "verdict": "authorized",
                        "confidence": 0.93, "reason": "Jev: the quote's item authorizes this grant (0.93).", "clause": cl,
                        "stored": True, "grant": {"id": f"g{i:05d}", "op": g["op"], "origin": g["origin"],
                                                  "target": g["target"], "usesLeft": g["uses"], "clause": cl}})
    return {"ok": True, "result": {"op": "grant.add_batch", "added": len(results), "refused": 0,
                                   "preamble": QUOTE_B.split(":")[0], "windowSec": 2700, "results": results}}


def write_grants(tmp_path, grants=GRANTS):
    f = tmp_path / "grants.json"
    f.write_text(json.dumps(grants))
    return str(f)


def test_add_batch_preview_sends_nothing_and_shows_every_grant(ext, tmp_path):
    fake = ext(batch_ext).start()
    r = run("grant", "add-batch", "--origin", ADMIN, "--file", write_grants(tmp_path), "--quote", QUOTE_B, "--user-approved")
    assert r.exit_code == 0, r.output
    plan = json.loads(r.stdout)
    assert plan["dryRun"] is True and plan["op"] == "grant.add_batch" and plan["quote"] == QUOTE_B
    assert len(plan["grants"]) == 7 and plan["partial"] is False
    assert plan["grants"][1]["maxAmount"] == 0 and plan["grants"][1]["currency"] == "USD"
    assert plan["grants"][6] == {"op": "click", "origin": ADMIN, "target": {"text": "Undo"}, "uses": 3,
                                 "frameOrigin": APP, "ttl": plan["grants"][6]["ttl"]}
    assert "window" in plan["grants"][0]["ttl"]
    assert "All or nothing" in plan["requires"] and "own words" in plan["rule"]
    assert ops_sent(fake) == []


def test_add_batch_confirm_sends_one_request_and_logs_each_clause(ext, home, tmp_path):
    fake = ext(batch_ext).start()
    r = run("grant", "add-batch", "--origin", ADMIN, "--tab", "812", "--file", write_grants(tmp_path),
            "--quote", QUOTE_B, "--user-approved", "--confirm")
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["added"] == 7 and [x["clause"]["text"] for x in out["results"]] == CLAUSES
    [m] = ops_sent(fake)
    assert m["op"] == "grant.add_batch" and m["confirm"] is True
    a = m["args"]
    assert a["quote"] == QUOTE_B and a["userApproved"] is True and a["tab"] == 812 and "partial" not in a
    assert a["grants"][0] == {"op": "click", "origin": ADMIN, "target": {"text": "Install", "role": "button"}, "uses": 1}
    assert a["grants"][6] == {"op": "click", "origin": ADMIN, "target": {"text": "Undo"}, "uses": 3, "frameOrigin": APP}
    line = next(x for x in (home / "logs" / "chromecli.log").read_text().splitlines() if "op=grant.add_batch" in x)
    assert "grants=7 partial=False user_approved=True" in line and json.dumps(QUOTE_B) in line
    assert "added=7 refused=0" in line
    assert 'g7=authorized:0.93 grant7=g00007 clause7="Undo 3 times"' in line
    assert "[2] grant_op=click" in line and "max_amount=0.0 USD" in line and "ttl_s=default" in line


def test_add_batch_repeated_grant_specs_and_shorthand(ext, home):
    fake = ext(batch_ext).start()
    r = run("grant", "add-batch", "--origin", ADMIN, "--frame-origin", APP, "--quote", QUOTE_B, "--user-approved",
            "--confirm", "--partial", "--grant", "Scan products", "--grant", '{"target-text":"Undo","uses":3,"ttl":"30m"}',
            "--grant", json.dumps({"target": {"text": "Install", "role": "button"}, "frame-origin": None}))
    assert r.exit_code == 0, r.output
    a = ops_sent(fake)[0]["args"]
    assert a["partial"] is True
    assert a["grants"][0] == {"op": "click", "origin": ADMIN, "target": {"text": "Scan products"}, "uses": 1, "frameOrigin": APP}
    assert a["grants"][1]["ttlSec"] == 1800 and a["grants"][1]["uses"] == 3
    assert a["grants"][2] == {"op": "click", "origin": ADMIN, "target": {"text": "Install", "role": "button"}, "uses": 1}


@pytest.mark.parametrize(("argv", "msg"), [
    ([], "Give the grants"),
    (["--grant", '{"target-text": "Undo", "times": 3}'], "unknown key 'times'"),
    (["--grant", '{"target-text": "Undo", "uses": 21}'], "grant 1: --uses must be 1-20"),
    (["--grant", '{"target-text": "Approve", "max-amount": -1}'], "grant 1: --max-amount must be 0 or more"),
    (["--grant", '{"target-text": "Undo", "frame-origin": "http://x.example"}'], "must be an exact https origin"),
    (["--grant", '{"op": "navigate", "target-text": "Undo"}'], "match on --origin"),
    (["--grant", '{"op": "eval", "target-text": "x"}'], "op must be one of"),
    (["--grant", '{"target-text": "Undo", "ttl": "25h"}'], "between 1m and 24h"),
    (["--grant", "{not json"], "isn't valid JSON"),
    (["--grant", "Undo", "--quote", "x" * 501], "over 500"),
    (["--grant", "Undo"] * 21, "at most 20"),
])
def test_add_batch_validation(ext, argv, msg):
    fake = ext(batch_ext).start()
    base = ["grant", "add-batch", "--origin", ADMIN, "--quote", QUOTE_B, "--user-approved"]
    r = run(*base, *argv)
    assert r.exit_code == 2, r.output
    assert msg in err(r)["message"]
    assert ops_sent(fake) == []


def test_add_batch_needs_user_approved_and_an_origin(ext):
    fake = ext(batch_ext).start()
    r = run("grant", "add-batch", "--origin", ADMIN, "--grant", "Undo", "--quote", QUOTE_B, "--confirm")
    assert r.exit_code == 2 and err(r)["error"] == "missing_user_approval"
    r = run("grant", "add-batch", "--grant", "Undo", "--quote", QUOTE_B, "--user-approved")
    assert r.exit_code == 2 and "not an http(s) origin" in err(r)["message"]
    assert ops_sent(fake) == []


def test_add_batch_refusal_is_exit_9_with_per_grant_results_and_logged(ext, home, tmp_path):
    results = [{"index": i, "verdict": "authorized", "confidence": 0.93, "clause": {"id": f"c{i}", "text": CLAUSES[i - 1]},
                "stored": False} for i in range(1, 8)]
    results[6] = {"index": 7, "verdict": "overreach", "confidence": None, "stored": False,
                  "reason": '"Undo 3 times" approves 3 uses; this grant asks for 4.', "clause": {"id": "c7", "text": "Undo 3 times"}}
    ext(lambda req: {"ok": False, "error": {"code": "grant_overreach", "message": "1 of 7 grants refused; nothing was granted.",
                                            "results": results, "hint": "Nothing was granted. Ask Kevin."}}).start()
    grants = [dict(g) for g in GRANTS]
    grants[6]["uses"] = 4
    r = run("grant", "add-batch", "--origin", ADMIN, "--file", write_grants(tmp_path, grants), "--quote", QUOTE_B,
            "--user-approved", "--confirm")
    assert r.exit_code == 9, r.output
    e = err(r)
    assert e["error"] == "grant_overreach" and e["results"][6]["verdict"] == "overreach"
    line = next(x for x in (home / "logs" / "chromecli.log").read_text().splitlines() if "op=grant.add_batch" in x)
    assert "outcome=grant_overreach exit=9" in line and 'g7=overreach:None grant7=- clause7="Undo 3 times"' in line


def test_grant_add_accepts_max_amount_zero_tab_and_no_ttl(ext):
    fake = ext(lambda req: {"ok": True, "result": {"op": "grant.add", "added": True, "grant": {"id": "g1"}}}).start()
    r = run("grant", "add", "--op", "click", "--origin", ADMIN, "--target-text", "Approve", "--max-amount", "0",
            "--tab", "812", "--quote", QUOTE_B, "--user-approved", "--confirm")
    assert r.exit_code == 0, r.output
    a = ops_sent(fake)[0]["args"]
    assert a["maxAmount"] == 0 and a["tab"] == 812 and "ttlSec" not in a


def test_agent_help_shows_add_batch_with_quote_b():
    ref = json.loads(run("agent-help", "--format", "json").stdout)
    notes = " ".join(ref["notes"])
    assert "grant add-batch" in notes and "Undo 3 times" in notes and "--partial" in notes
    assert "as-described-review for the next 45 minutes" in notes
    names = {c["name"] for c in ref["commands"]}
    assert "grant add-batch" in names
