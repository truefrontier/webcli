"""Required (6): identity resolution, the identity -> directory mapping, plus the data home's config, H4
hardening, `webcli doctor`, and `webcli sites`."""

from __future__ import annotations

import json
import stat

import pytest
from click.testing import CliRunner

from webcli import sites
from webcli.apps import llmcli, webcli as webcli_app
from webcli.core import config, hardening, identities
from webcli.core.errors import ConfigError
from webcli.core.identities import Identity, resolve

GEMINI, CHATGPT, MUSE = sites.get("gemini"), sites.get("chatgpt"), sites.get("muse")


def test_default_identities_map_to_their_dirs():
    google, chatgpt, meta = resolve(GEMINI), resolve(CHATGPT), resolve(MUSE)
    assert (google.id, chatgpt.id, meta.id) == ("google:default", "chatgpt:default", "meta:default")
    assert google.profile == config.home() / "identities" / "google-default" / "chrome-profile"
    assert chatgpt.profile == config.home() / "identities" / "chatgpt-default" / "chrome-profile"
    assert meta.profile == config.home() / "identities" / "meta-default" / "chrome-profile"
    assert meta.label == "com.truefrontier.webcli.meta-default"
    assert google.label == "com.truefrontier.webcli.google-default"
    assert google.run_file(".sock") == config.home() / "run" / "google-default.sock"
    assert identities.from_dir("google-my-work").id == "google:my-work"  # the mapping reverses


def test_explicit_beats_env_beats_config_beats_default(monkeypatch):
    config.save({"sites": {"gemini": {"identity": "google:cfg"}}})
    assert resolve(GEMINI).id == "google:cfg"
    monkeypatch.setenv("WEBCLI_IDENTITY", "google:env")
    assert resolve(GEMINI).id == "google:env"
    assert resolve(GEMINI, "google:flag").id == "google:flag"
    monkeypatch.delenv("WEBCLI_IDENTITY")
    assert resolve(CHATGPT).id == "chatgpt:default"  # another site's config doesn't leak


@pytest.mark.parametrize("bad", ["google", "Google:default", "google:", ":x", "google:../x", "google:a/b",
                                 "goo-gle:x", "google:a b"])
def test_bad_ids_are_config_errors(bad):
    with pytest.raises(ConfigError):
        Identity(bad)


def test_a_wrong_provider_is_a_config_error_wherever_it_comes_from(monkeypatch):
    with pytest.raises(ConfigError, match="a chatgpt identity"):
        resolve(GEMINI, "chatgpt:default")
    monkeypatch.setenv("WEBCLI_IDENTITY", "google:default")
    with pytest.raises(ConfigError, match=r"\$WEBCLI_IDENTITY is google:default"):
        resolve(CHATGPT)
    monkeypatch.delenv("WEBCLI_IDENTITY")
    config.save({"sites": {"chatgpt": {"identity": "google:default"}}})
    with pytest.raises(ConfigError, match="config.toml"):
        resolve(CHATGPT)


def test_the_cli_runs_as_the_resolved_identity():
    result = CliRunner().invoke(llmcli.build(), ["gemini", "--identity", "google:work", "status"])
    assert result.exit_code == 4 and json.loads(result.stdout)["identity"] == "google:work"
    assert json.loads(result.stderr)["hint"] == ("a person runs: llmcli gemini --identity google:work login "
                                                 "(or webcli identity login google:work)")
    result = CliRunner().invoke(llmcli.build(), ["gemini", "status"], env={"WEBCLI_IDENTITY": "google:env"})
    assert json.loads(result.stdout)["identity"] == "google:env"
    result = CliRunner().invoke(llmcli.build(), ["chatgpt", "status"])
    assert json.loads(result.stdout)["identity"] == "chatgpt:default"


def test_identity_list_and_login_site():
    Identity("google:work").profile.mkdir(parents=True)
    (Identity("google:work").profile / "Local State").write_text("{}")
    rows = json.loads(CliRunner().invoke(webcli_app.build(), ["identity", "list"]).stdout)
    assert {r["id"]: r["profileExists"] for r in rows} == {"chatgpt:default": False, "google:default": False,
                                                           "google:work": True, "meta:default": False}
    all_sites = list(sites.registry().values())
    assert identities.login_site(Identity("google:work"), all_sites).name == "gemini"
    assert identities.login_site(Identity("chatgpt:default"), all_sites).name == "chatgpt"
    assert identities.login_site(Identity("meta:default"), all_sites).name == "muse"
    with pytest.raises(ConfigError):
        identities.login_site(Identity("linkedin:default"), all_sites)


def test_config_round_trips_and_is_private():
    data = {"core": {"lock_wait": 30.0, "daemon": True}, "sites": {"gemini": {"identity": "google:default"}},
            "identities": {"google:work": {"provider": "google"}}}
    path = config.save(data)
    assert config.load() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_new_home_gets_the_h4_hardening(tool_calls):
    root = config.ensure_home()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert (root / ".metadata_never_index").exists() and (root / ".nosync").exists()
    assert ["tmutil", "addexclusion", str(root)] in tool_calls
    tool_calls.clear()
    config.ensure_home()
    assert ["tmutil", "addexclusion", str(root)] not in tool_calls  # only when first created


def test_hardening_check_reports_each_item(monkeypatch):
    root = config.ensure_home()
    assert hardening.check(root)["ok"] is True
    root.chmod(0o755)
    (root / ".nosync").unlink()
    monkeypatch.setattr(hardening, "filevault", lambda: "off")
    report = hardening.check(root)
    assert (report["private"], report["nosync"], report["fileVault"], report["ok"]) == (False, False, "off", False)
    assert hardening.apply(root)["private"] is True


def test_doctor_reports_home_chrome_and_identities_without_a_browser():
    result = CliRunner().invoke(webcli_app.build(), ["doctor"])
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["home"]["timeMachineExcluded"] is True and report["home"]["spotlightExcluded"] is True
    assert report["home"]["fileVault"] == "on" and report["home"]["private"] is True
    assert {i["id"]: i["signedIn"] for i in report["identities"]} == {"chatgpt:default": False,
                                                                      "google:default": False,
                                                                      "meta:default": False}
    assert report["chrome"]["ignoredDefaultArgs"] == ["--enable-automation", "--use-mock-keychain"]
    assert report["sites"] == {"llmcli": ["chatgpt", "gemini", "muse"],
                               "webcli": ["aa", "keep", "walkie"]}


def test_sites_lists_every_llm_site_and_the_data_sites():
    result = CliRunner().invoke(webcli_app.build(), ["sites"])
    data = json.loads(result.stdout)
    assert [s["name"] for s in data["llmcli"]] == ["chatgpt", "gemini", "muse"]
    assert [s["name"] for s in data["webcli"]] == ["aa", "keep", "walkie"]
    for site in data["webcli"]:  # aa, walkie: no sign-in, so no identity (none in `identity list` / `doctor` either)
        no_sign_in = site["name"] in ("aa", "walkie")
        assert (site["kind"], site["binary"], site["provider"], site["defaultIdentity"]) == (
            ("data", "webcli", "none", None) if no_sign_in else ("data", "webcli", "google", "google:default"))
    assert data["webcli"][0]["verbs"] == {"models": "public", "digest": "public", "tts": "public"}
    assert {s["defaultIdentity"] for s in data["llmcli"]} == {"chatgpt:default", "google:default", "meta:default"}
    muse = next(s for s in data["llmcli"] if s["name"] == "muse")
    assert (muse["kind"], muse["provider"], muse["binary"]) == ("llm", "meta", "llmcli")


def test_every_dir_the_code_creates_under_the_home_is_0700():
    config.private_dir(Identity("google:default").path)
    config.private_dir(config.site_dir("gemini") / "google-default")
    for path in config.home().rglob("*"):
        if path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_a_home_first_created_by_a_command_still_gets_the_hardening(tool_calls):
    import asyncio

    from webcli.core import browser

    async def lock():
        async with browser.profile_lock(Identity("google:default"), wait=0):
            pass

    asyncio.run(lock())  # the first thing to touch the home: a command taking its profile lock
    root = config.home()
    assert ["tmutil", "addexclusion", str(root)] in tool_calls
    assert (root / ".metadata_never_index").exists()
