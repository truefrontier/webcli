"""chromecli 0.11.3: deny_except (narrow deny exceptions) and frame_act_allow (acting in listed iframes), Python side:
the shared cases with the extension (validation, matching), config parsing, the audit-log fields and grant add
--frame-origin. Pending-until-Accept and the gates are the extension's (test/frame-act.test.mjs, policy.test.mjs)."""
from __future__ import annotations

import json
import re

import pytest

from test_chromecli import CASES, ext, home, run  # noqa: F401  (fixtures)
from webcli.chromecli import policy
from webcli.core.errors import ConfigError

FORM = "http://127.0.0.1:8765"
APP = "https://asdescribed.truefrontier.app"
EXC = "https://admin.shopify.com/store/*/oauth/install"
QUOTE = "approved to click Scan, Review fixes, Apply and Undo in As Described on the as-described-review store"


@pytest.mark.parametrize(("pattern", "ok"), CASES["except_valid"])
def test_except_validation(pattern, ok):
    assert (policy.except_error(pattern) is None) is ok, policy.except_error(pattern)


@pytest.mark.parametrize(("origin", "ok"), CASES["frame_valid"])
def test_frame_origin_validation(origin, ok):
    assert (policy.frame_origin_error(origin) is None) is ok


def _except_policy():
    c = CASES["except_denied"]
    return policy.parse({"deny": [*policy.DEFAULT_DENY, *c["deny_add"]], "deny_except": c["deny_except"]})


@pytest.mark.parametrize(("url", "denied", "exc"), CASES["except_denied"]["urls"])
def test_except_matching(url, denied, exc):
    p = _except_policy()
    assert bool(policy.denied(url, p)) is denied
    assert policy.deny_exception(url, p) == exc


def test_config_parsing_and_hash():
    p = policy.parse({"deny_except": ["HTTPS://Admin.Shopify.com/store/*/oauth/install"],
                      "frame_act_allow": ["https://AsDescribed.TrueFrontier.app"]})
    assert p["deny_except"] == [EXC] and p["frame_act_allow"] == [APP]
    assert p["hash"] != policy.default_policy()["hash"]
    for bad in ({"deny_except": EXC}, {"deny_except": ["https://admin.shopify.com"]},
                {"deny_except": ["http://admin.shopify.com/x"]}, {"deny_except": ["https://*.shopify.com/x"]},
                {"deny_except": ["https://chromewebstore.google.com/detail/*"]},
                {"frame_act_allow": ["https://*.truefrontier.app"]}, {"frame_act_allow": ["http://a.com"]},
                {"frame_act_allow": [f"{APP}/path"]}, {"frame_act_allow": APP}):
        with pytest.raises(ConfigError):
            policy.parse(bad)


def test_adding_is_loosening_removing_is_not():
    old, new = policy.parse({}), policy.parse({"deny_except": [EXC], "frame_act_allow": [APP]})
    kinds = sorted(x["kind"] for x in policy.loosening(old, new))
    assert kinds == ["deny_except_added", "frame_act_allow_added"]
    assert policy.loosening(new, old) == []
    t = policy.tighten(old, new)
    assert t["deny_except"] == [] and t["frame_act_allow"] == []


def frame_ext(req):
    op, args = req["op"], req["args"]
    if op == "page.click":
        return {"ok": True, "result": {"tab": 7, "origin": FORM, "op": "click", "gated": False, "reasons": [],
                "confirmedInBrowser": False, "dispatched": True, "frameId": 5, "frameOrigin": APP,
                "denyExcept": None}}
    if op == "grant.add":
        g = {"id": "g9", "op": args["op"], "origin": args["origin"], "frameOrigin": args.get("frameOrigin"),
             "target": args.get("target"), "quote": args["quote"], "uses": args["uses"], "usesLeft": args["uses"],
             "expiresInSec": args["ttlSec"]}
        return {"ok": True, "result": {"op": "grant.add", "added": True, "grant": g,
                "jevCheck": {"verdict": "authorized", "confidence": 0.9, "reason": "ok", "narrowed": []}}}
    return {"ok": True, "result": {}}


def test_frame_action_logs_frame_and_origin(ext, home):
    ext(frame_ext).start()
    r = run("click", "--tab", "7", "--ref", "f5:e1", "--expect-origin", FORM, "--confirm")
    assert r.exit_code == 0, r.output
    log = (home / "logs" / "chromecli.log").read_text()
    line = next(x for x in log.splitlines() if "op=page.click" in x)
    assert re.search(rf"frame=5 frame_origin={re.escape(APP)}", line), line


def test_grant_add_frame_origin(ext, home):
    fake = ext(frame_ext).start()
    base = ["grant", "add", "--op", "click", "--origin", FORM, "--target-text", "Apply", "--quote", QUOTE,
            "--user-approved", "--uses", "4"]
    r = run(*base, "--frame-origin", APP, "--confirm")
    assert r.exit_code == 0, r.output
    m = [x for x in fake.received if x.get("op")][0]
    assert m["args"]["frameOrigin"] == APP and m["args"]["uses"] == 4
    log = (home / "logs" / "chromecli.log").read_text()
    assert f"frame_origin={APP}" in next(x for x in log.splitlines() if "op=grant.add" in x)
    for bad in ("https://*.truefrontier.app", "http://asdescribed.truefrontier.app", f"{APP}/x"):
        assert run(*base, "--frame-origin", bad, "--confirm").exit_code != 0
    nav = run("grant", "add", "--op", "navigate", "--origin", FORM, "--quote", QUOTE, "--user-approved",
              "--frame-origin", APP)
    assert nav.exit_code != 0


def test_agent_help_mentions_the_new_keys():
    out = run("agent-help").output
    for s in ("deny_except", "frame_act_allow", "--frame-origin", "--uses"):
        assert s in out, s
    assert json.dumps  # keep import used
