"""`webcli`: data sites over your own signed-in, hidden Chrome. Phase 1 ships no data-site adapters yet: identity,
doctor, sites, serve, and agent-help."""

from __future__ import annotations

import click

from webcli import __version__, sites
from webcli.apps.common import identity_group, identity_report, serve_command
from webcli.core import browser, config, hardening, identities, output
from webcli.core.output import EXIT_OK, agent_output_options, emit_json, emit_rows, resolve_format
from webcli.sites.llm import env_help

NOTES = [
    "Phase 1 has no data-site adapters yet (`webcli sites` lists them as they land); chat AIs live under `llmcli`.",
    "Every command runs Chrome headless: no window, no Dock icon, no focus change. Only `identity login` and an "
    "explicit --headed show a window, and login needs a person.",
    "`doctor` reports the data home's H4 hardening (permissions, Time Machine, Spotlight, .nosync), FileVault, and "
    "each identity's profile, daemon, and sign-in (checked headless; --no-browser skips that).",
]
WRITES = ["Nothing yet: data-site write verbs will be dry-run by default (--dry-run / --confirm)."]


def chrome_info() -> dict:
    import plistlib

    try:
        version = plistlib.loads(browser.CHROME_PLIST.read_bytes())["CFBundleShortVersionString"]
    except (OSError, KeyError, ValueError, plistlib.InvalidFileException):
        version = None
    return {"installed": version is not None, "version": version, "channel": "chrome", "headlessByDefault": True,
            "ignoredDefaultArgs": browser.IGNORED_DEFAULT_ARGS}


def build() -> click.Group:
    @click.group(cls=output.AgentGroup, name="webcli")
    @click.version_option(__version__, prog_name="webcli")
    def main() -> None:
        """Data sites from your own signed-in Chrome, hidden. Data home: ~/.webcli/"""
        output.env_format()

    @main.command("sites")
    @agent_output_options()
    def sites_cmd(fmt, compact, select, as_csv, quiet) -> None:
        """Installed site adapters: chat AIs under llmcli, data sites under webcli (none yet)."""
        rows = [site.describe() for site in sites.registry().values()]
        out = "csv" if as_csv else resolve_format(fmt)
        if out == "json" and not (select or compact):
            emit_json({"llmcli": [r for r in rows if r["kind"] == "llm"],
                       "webcli": [r for r in rows if r["kind"] == "data"]})
        else:
            emit_rows(rows, title="Sites", fields=["name", "binary", "provider", "defaultIdentity", "summary"],
                      compact_fields=("name", "binary"), columns=["name", "binary", "provider", "defaultIdentity"],
                      noun="sites", fmt=fmt, compact=compact, select=select, quiet=quiet, as_csv=as_csv)
        output.note("webcli data sites: none installed yet.", quiet=quiet or bool(sites.of_kind("data")))
        raise SystemExit(EXIT_OK)

    @main.command("doctor")
    @click.option("--fix", is_flag=True, help="Re-apply the H4 hardening to the data home first.")
    @click.option("--no-browser", is_flag=True, help="Don't check sign-in with a (headless) Chrome.")
    def doctor_cmd(fix: bool, no_browser: bool) -> None:
        """Health report (JSON): data home hardening, FileVault, Chrome, every identity's profile, daemon, and
        sign-in. Never shows a window."""
        root = config.ensure_home()
        home = hardening.apply(root) if fix else hardening.check(root)
        known = identities.known(list(sites.registry().values()))
        report = {
            "version": __version__,
            "home": home,
            "chrome": chrome_info(),
            "identities": [identity_report(i, browser_check=not no_browser) for i in known],
            "sites": {"llmcli": [s.name for s in sites.of_kind("llm")], "webcli": [s.name for s in sites.of_kind("data")]},
        }
        report["ok"] = bool(home["ok"] and report["chrome"]["installed"])
        emit_json(report)
        raise SystemExit(EXIT_OK)

    for site in sites.of_kind("data"):
        main.add_command(site.cli(), site.name)
    main.add_command(identity_group())
    main.add_command(serve_command())
    main.add_command(output.agent_help_command(main, version=__version__, env=env_help, notes=NOTES, writes=WRITES))
    return main


def main() -> None:
    output.APP, output.FORMAT_ENV = "webcli", "WEBCLI_FORMAT"
    build().main(prog_name="webcli")


if __name__ == "__main__":
    main()
