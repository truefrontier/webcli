"""chromecli 0.11.4: an unknown [chromecli] key is a warning (host log + `status` configWarnings) and is ignored, never
fatal and never loosening; a known key with an invalid value stays fatal (exit 7). When the running host's version
differs from the installed CLI's or the extension's, `status` and every exit-7 error say to reload the extension."""
from __future__ import annotations

import json

import pytest

from test_chromecli import EXTENSION_ID, err, ext, fake_tabs, home, run  # noqa: F401  (fixtures)
from webcli import __version__
from webcli.chromecli import bridge, host, policy
from webcli.core import config
from webcli.core.errors import ConfigError

RELOAD = "reload the chromecli extension in chrome://extensions"


def same_version_tabs(req):
    reply = fake_tabs(req)
    if req["op"] == "status":
        reply["result"]["extVersion"] = __version__
    return reply


# parsing

def test_unknown_keys_are_warnings_and_ignored():
    pol, warnings = policy.parse_checked({"history": False, "future_key": ["x"], "another": 1})
    assert pol == policy.parse({"history": False})
    assert pol["hash"] == policy.parse({"history": False})["hash"]
    assert "future_key" not in pol and "another" not in pol
    assert len(warnings) == 2 and all("ignored" in w for w in warnings)
    assert "'another'" in warnings[0] and "'future_key'" in warnings[1]


def test_known_keys_have_no_warnings():
    assert policy.parse_checked(None) == (policy.default_policy(), [])
    assert policy.parse_checked({"deny_except": [], "frame_act_allow": []})[1] == []


@pytest.mark.parametrize("section", [
    {"act_allow_all": True, "Read_Allow": ["*.example.com"], "deny_remove": ["path:/checkout"]},
    {"implicit": "act", "allow": ["https://bank.example"], "history_on": True, "deny ": []},
])
def test_unknown_keys_never_loosen(section):
    pol, warnings = policy.parse_checked(section)
    assert len(warnings) == len(section)
    assert pol == policy.default_policy()
    assert policy.loosening(policy.default_policy(), pol) == []


def test_unknown_keys_never_show_values():
    _, warnings = policy.parse_checked({"api_token": "sk-live-SECRETVALUE123"})
    assert warnings and "SECRETVALUE" not in " ".join(warnings)


@pytest.mark.parametrize(("section", "word"), [
    ({"implicit_active": "act", "future_key": 1}, "implicit_active"),
    ({"history": "yes"}, "history"),
    ({"confirm_timeout": 5, "future_key": 1}, "confirm_timeout"),
    ({"deny_except": ["http://admin.shopify.com/x"]}, "deny_except"),
    ({"frame_act_allow": ["https://*.example.com"], "future_key": 1}, "frame_act_allow"),
    ({"read_allow": "example.com"}, "read_allow"),
])
def test_invalid_known_keys_stay_fatal(section, word):
    with pytest.raises(ConfigError) as exc:
        policy.parse_checked(section)
    assert word in str(exc.value)


def test_a_non_table_stays_fatal():
    with pytest.raises(ConfigError):
        policy.parse_checked(["not", "a", "table"])


# the host

def test_host_ignores_unknown_keys_logs_and_reports_them(ext, home):
    config.ensure_home()
    (home / "config.toml").write_text('[chromecli]\nhistory = false\nsome_future_key = ["https://x.example"]\n')
    fake = ext(same_version_tabs).start()
    listed = run("list")
    assert listed.exit_code == 0, listed.stderr
    st = json.loads(run("status", "--format", "json").stdout)
    assert st["host"]["configError"] is None
    assert len(st["configWarnings"]) == 1 and "some_future_key" in st["configWarnings"][0]
    assert st["host"]["configWarnings"] == st["configWarnings"]
    assert st["versionMismatch"] is False and "hint" not in st
    log = (home / "logs" / "chromecli.log").read_text()
    assert "event=config_warning" in log and "some_future_key" in log and "https://x.example" not in log
    pushed = [m for m in fake.received if m.get("event") == "policy"][-1]["policy"]
    assert "some_future_key" not in pushed and pushed["history"] is False


def test_status_has_no_warnings_without_unknown_keys(ext, home):
    ext(same_version_tabs).start()
    st = json.loads(run("status", "--format", "json").stdout)
    assert st["configWarnings"] == []
    assert st["versions"] == {"cli": __version__, "host": __version__, "extension": __version__}


# versions

def test_status_says_reload_when_the_extension_differs(ext):
    ext(fake_tabs).start()  # extVersion 0.7.0
    result = run("status", "--format", "json")
    assert result.exit_code == 0
    st = json.loads(result.stdout)
    assert st["versionMismatch"] is True and st["versions"]["extension"] == "0.7.0"
    assert RELOAD in st["hint"]


def test_status_says_reload_when_the_host_differs(ext, monkeypatch):
    monkeypatch.setattr(host, "__version__", "0.10.2")
    ext(same_version_tabs).start()
    st = json.loads(run("status", "--format", "json").stdout)
    assert st["versions"]["host"] == "0.10.2" and st["versionMismatch"] is True and RELOAD in st["hint"]
    table = run("status", "--format", "table")
    assert table.exit_code == 0 and RELOAD in table.stderr


def test_exit_7_says_reload_when_the_host_differs(ext, home, monkeypatch):
    monkeypatch.setattr(host, "__version__", "0.10.2")
    config.ensure_home()
    (home / "config.toml").write_text('[chromecli]\nimplicit_active = "act"\n')
    ext(same_version_tabs).start()
    result = run("list")
    assert result.exit_code == 7
    e = err(result)
    assert e["error"] == "config_error" and RELOAD in e["message"] and RELOAD in e["hint"]
    assert e["versions"]["host"] == "0.10.2" and e["versionMismatch"] is True
    assert "hostVersion" not in e and "implicit_active" in e["message"]


def test_exit_7_without_a_mismatch_says_only_the_config_problem(ext, home):
    config.ensure_home()
    (home / "config.toml").write_text('[chromecli]\nimplicit_active = "act"\n')
    ext(same_version_tabs).start()
    e = err(run("list"))
    assert e["error"] == "config_error" and RELOAD not in e["message"] and "versionMismatch" not in e


def test_exit_7_from_an_older_host_asks_status_for_its_version(monkeypatch):
    """A 0.10.2 host refuses new keys and doesn't put its version in the error; the CLI asks `status`."""
    calls = []

    def fake_call(op, args=None, *, timeout=None, confirm=False):
        calls.append(op)
        if op == "status":
            return {"ok": True, "result": {"extVersion": "0.10.2", "extensionId": EXTENSION_ID,
                                           "host": {"version": "0.10.2", "configError": "x"}}}
        return {"ok": False, "error": {"code": "config_error",
                                       "message": "[chromecli] has unknown keys: deny_except, frame_act_allow."}}

    monkeypatch.setattr(bridge, "call", fake_call)
    result = run("list")
    assert result.exit_code == 7 and calls == ["tabs.list", "status"]
    e = err(result)
    assert RELOAD in e["message"] and e["versions"] == {"cli": __version__, "host": "0.10.2", "extension": "0.10.2"}


def test_version_check():
    assert bridge.version_check(__version__, __version__)["versionMismatch"] is False
    assert bridge.version_check(__version__, None)["versionMismatch"] is False
    off = bridge.version_check("0.10.2", "0.11.4" if __version__ != "0.11.4" else "0.10.2")
    assert off["versionMismatch"] is True and RELOAD in off["reloadHint"]
