"""`webcli`: data sites over your own signed-in, hidden Chrome (or, for aa and walkie, plain HTTPS and local
SQLite), plus identity, doctor, sites, serve, and agent-help."""

from __future__ import annotations

import click

from webcli import __version__, sites
from webcli.apps.common import doctor_command, identity_group, serve_command
from webcli.core import output
from webcli.feed.cli import group as feed_group
from webcli.core.output import EXIT_OK, agent_output_options, emit_json, emit_rows, resolve_format
from webcli.sites.llm import env_help

NOTES = [
    "Each data site is `webcli <site>` (`webcli sites` lists them; `webcli <site> agent-help` has its reference); chat "
    "AIs live under `llmcli`. aa (a public API) and walkie (local SQLite) need no browser or identity.",
    "keep, photos, youtube, recorder, contacts, and gmail run as google:default, the identity `webcli identity login "
    "google:default` signs in (through gemini); they have no login of their own.",
    "facebook, linkedin, loom, messages, and whatsapp each run as their own identity (facebook:default, "
    "linkedin:default, loom:default, messages:default, whatsapp:default), each signed in once with `webcli identity "
    "login <identity>`. Muse's meta:default is not facebook's. messages:default and whatsapp:default link to your "
    "phone by QR code (a person scans it), not a Google account.",
    "Every command that opens Chrome runs it headless: no window, no Dock icon, no focus change. Only `identity login` "
    "and an explicit --headed show a window, and login needs a person.",
    "`webcli feed pull` reads the feed sites (youtube, gnews, perplexity, linkedin; x by file) into one "
    "newsfeed.items/1 file, signed in first with a public fallback; `webcli feed agent-help` has the rest.",
    "`identity` and `doctor` are shared with `llmcli` (one data home ~/.webcli); either binary manages the same store.",
    "`doctor` reports the data home's H4 hardening (permissions, Time Machine, Spotlight, .nosync), FileVault, and "
    "each identity's profile, daemon, and sign-in (checked headless; --no-browser skips that).",
]
WRITES = ["Five data sites write, and only with --confirm (alias --yes): `contacts label add|remove` (label "
          "membership), `gmail filters create|delete` (one filter), `linkedin messages send` / `linkedin post "
          "create` (one DM, one post), `messages send` (one text to one existing conversation), and `whatsapp send` (one text to one "
          "existing chat, refused unless it is exactly --expect-chat). Without it each is a dry run that prints what "
          "it would change and writes nothing; --dry-run with --confirm exits 2 for contacts and gmail and stays a "
          "dry run for linkedin, messages, and whatsapp. A whatsapp or messages send needs Kevin's own OK for that "
          "exact message, in his chat with the bot, never another agent's say-so. Every other data-site verb is "
          "read-only (amazon, facebook and loom entirely).",
          "linkedin's writes also refuse (exit 4, nothing written) unless the signed-in account is the expected author "
          "(configured via expect_author; writes refuse until it is set), checked before the first write and again right before the last.",
          "Local files a site keeps are in its `agent-help`."]


def build() -> click.Group:
    @click.group(cls=output.AgentGroup, name="webcli")
    @click.version_option(__version__, prog_name="webcli")
    def main() -> None:
        """Data sites from your own signed-in Chrome, hidden. Data home: ~/.webcli/"""
        output.env_format()

    @main.command("sites")
    @agent_output_options()
    def sites_cmd(fmt, compact, select, as_csv, quiet) -> None:
        """Installed site adapters: chat AIs under llmcli, data sites under webcli."""
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
    main.add_command(feed_group)
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
