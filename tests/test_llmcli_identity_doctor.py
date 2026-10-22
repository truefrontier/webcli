"""llmcli exposes the same shared identity + doctor as webcli, backed by one data home."""

from __future__ import annotations

import json

from click.testing import CliRunner

from webcli.apps import llmcli, webcli as webcli_app


def run(app, *args):
    return CliRunner().invoke(app.build(), list(args))


def test_llmcli_has_identity_and_doctor():
    cmds = set(llmcli.build().commands)
    assert {"identity", "doctor", "serve", "gemini", "chatgpt"} <= cmds


def test_llmcli_identity_has_login_list_status():
    identity = llmcli.build().commands["identity"]
    assert {"login", "list", "status"} <= set(identity.commands)


def test_llmcli_identity_login_help_mentions_chatgpt_default():
    # The command Kevin expected: `llmcli identity login chatgpt:default`
    r = run(llmcli, "identity", "login", "--help")
    assert r.exit_code == 0 and "PROVIDER:ACCOUNT" in r.output


def test_llmcli_doctor_reports_same_shape_as_webcli():
    a = run(llmcli, "doctor", "--no-browser")
    b = run(webcli_app, "doctor", "--no-browser")
    assert a.exit_code == 0 and b.exit_code == 0
    da, db = json.loads(a.output), json.loads(b.output)
    assert set(da) == set(db) == {"version", "home", "chrome", "identities", "sites", "chromecli", "ok"}
    # one shared store: identical identities + sites regardless of which binary asked
    assert da["sites"] == db["sites"]
    assert {i["id"] for i in da["identities"]} == {i["id"] for i in db["identities"]}
    assert set(da["sites"]["llmcli"]) >= {"gemini", "chatgpt"}


def test_llmcli_doctor_identity_status_hint_is_app_aware():
    r = run(llmcli, "identity", "status", "google:default", "--no-browser")
    assert r.exit_code == 4
    assert "llmcli identity login google:default" in r.output


def test_llmcli_agent_help_lists_identity_and_doctor():
    r = run(llmcli, "agent-help", "--format", "json")
    assert r.exit_code == 0
    names = {c["name"] for c in json.loads(r.output)["commands"]}
    assert any(n.startswith("identity") for n in names)
    assert "doctor" in names
