"""`llmcli`: chat AIs over your own signed-in, hidden Chrome. `llmcli <site> -p PROMPT` works like `claude -p`."""

from __future__ import annotations

import click

from webcli import __version__, sites
from webcli.apps.common import doctor_command, identity_group, serve_command
from webcli.core import output
from webcli.sites.llm import env_help

NOTES = [
    "Sites: `llmcli <site> ...`; each has its own `agent-help`: gemini (identity google:default), chatgpt "
    "(identity chatgpt:default), muse (Meta's Muse at muse.ai; identity meta:default, a Meta account).",
    "Every command runs Chrome headless: no window, no Dock icon, no focus change. Only `identity login` / `<site> "
    "login` and an explicit --headed show a window, and login needs a person.",
    "Uniform ask JSON on every site: answer, chatId, requestedModel, servedModel, servedModelDisplay, fallback, "
    "images, citations, canvas (null/empty when a site lacks one). Same exit codes everywhere (see exitCodes).",
    "`identity` (list/login/status) and `doctor` are shared with `webcli` over the one data home ~/.webcli; "
    "either binary manages the same store.",
    "Data home ~/.webcli ($WEBCLI_HOME): identities/<provider>-<account>/chrome-profile, run/ (daemon sockets), "
    "logs/ (redacted), sites/<site>/ (last_chat).",
]
WRITES = ["Only `<site> ask` / `<site> -p` send anything (one prompt each); see each site's agent-help."]


def build() -> click.Group:
    @click.group(cls=output.AgentGroup, name="llmcli")
    @click.version_option(__version__, prog_name="llmcli")
    def main() -> None:
        """Chat AIs (Gemini, ChatGPT, Muse) from your own signed-in Chrome, hidden. Data home: ~/.webcli/"""
        output.env_format()

    for site in sites.of_kind("llm"):
        main.add_command(site.cli(), site.name)
    main.add_command(identity_group())
    main.add_command(doctor_command())
    main.add_command(serve_command())
    main.add_command(output.agent_help_command(main, version=__version__, env=env_help, notes=NOTES, writes=WRITES))
    return main


def main() -> None:
    output.APP, output.FORMAT_ENV = "llmcli", "LLMCLI_FORMAT"
    build().main(prog_name="llmcli")


if __name__ == "__main__":
    main()
