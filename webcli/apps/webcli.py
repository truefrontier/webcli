"""`webcli`: data sites over your own signed-in, hidden Chrome. Phase 1 ships no data-site adapters yet: identity,
doctor, sites, serve, and agent-help."""

from __future__ import annotations

import click

from webcli import __version__, sites
from webcli.apps.common import doctor_command, identity_group, serve_command
from webcli.core import output
from webcli.core.output import EXIT_OK, agent_output_options, emit_json, emit_rows, resolve_format
from webcli.sites.llm import env_help

NOTES = [
    "Phase 1 has no data-site adapters yet (`webcli sites` lists them as they land); chat AIs live under `llmcli`.",
    "Every command runs Chrome headless: no window, no Dock icon, no focus change. Only `identity login` and an "
    "explicit --headed show a window, and login needs a person.",
    "`identity` and `doctor` are shared with `llmcli` (one data home ~/.webcli); either binary manages the same store.",
    "`doctor` reports the data home's H4 hardening (permissions, Time Machine, Spotlight, .nosync), FileVault, and "
    "each identity's profile, daemon, and sign-in (checked headless; --no-browser skips that).",
]
WRITES = ["Nothing yet: data-site write verbs will be dry-run by default (--dry-run / --confirm)."]


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

    for site in sites.of_kind("data"):
        main.add_command(site.cli(), site.name)
    main.add_command(identity_group())
    main.add_command(doctor_command())
    main.add_command(serve_command())
    main.add_command(output.agent_help_command(main, version=__version__, env=env_help, notes=NOTES, writes=WRITES))
    return main


def main() -> None:
    output.APP, output.FORMAT_ENV = "webcli", "WEBCLI_FORMAT"
    build().main(prog_name="webcli")


if __name__ == "__main__":
    main()
