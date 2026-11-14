"""`webcli new`: make a read-only tool for a site you use, in ./webcli-tool-NAME/, listed in ~/.webcli/tools/dev.toml
so `webcli dev`, `test`, and `lint` find it. With --capture, a person first clicks around the site in a visible
Chrome that lets only reads out, and the tool starts from what that showed (core/capture.py). webcli.py adds every
command in COMMANDS."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from webcli import scaffold
from webcli.core import browser, capture, identities, manifest_schema, output
from webcli.core.errors import UsageError
from webcli.core.guard import host_matches
from webcli.core.output import EXIT_API, EXIT_OK, EXIT_USAGE, agent_output_options, die, effects, emit_json, warn
from webcli.sites.base import NO_IDENTITY

CAPTURE_TIMEOUT = 600


def check_name(name: str) -> None:
    if not manifest_schema._NAME.fullmatch(name):
        die(f"{name!r} can't be a tool name: use 2-32 lowercase letters and digits, starting with a letter, and no "
            "hyphens (the name is also the tool's identity, NAME:default).", EXIT_USAGE)
    if name in manifest_schema._reserved():
        die(f"{name!r} is a built-in site, provider, or webcli command; a tool can't take its name.", EXIT_USAGE,
            hint="Pick another name. `webcli sites` lists the built-ins.")
    if name in scaffold.library_names():
        die(f"The webcli-tools library already has a tool named {name!r}.", EXIT_USAGE,
            hint=f"Install it with `webcli tools install {name}`, or pick another name.")


def provider_for(name: str, identity: str | None) -> str:
    """The provider the tool signs in as: --identity's provider part, else the tool's own name."""
    if not identity:
        return name
    provider, _, account = identity.partition(":")
    if not manifest_schema._PROVIDER.fullmatch(provider) or provider == NO_IDENTITY:
        die(f"--identity {identity!r}: give a provider like {name} or {name}:default. A scaffolded tool reads the "
            "site in Chrome, so it always has an identity.", EXIT_USAGE)
    if account and account != "default":
        warn(f"webcli: the tool signs in as {provider}:default; pick {provider}:{account} when you run it, with "
             f"`webcli {name} --identity {provider}:{account} ...`.")
    if provider != name:
        warn(f"webcli: {name} will sign in as {provider}:default, an identity other webcli sites may already use. "
             f"webcli won't let a community tool use it until you run `webcli tools grant {name} "
             f"{provider}:default`. Leave --identity off to give the tool its own ({name}:default).")
    return provider


def run_capture(name: str, provider: str, url: str, timeout: int) -> dict:
    """Guided capture in a visible Chrome on <provider>:default: {proposals, record (its private file), fixture}."""
    browser.identity = identities.parse(f"{provider}:default")
    if provider != name:
        warn(f"webcli: capture uses your existing {provider}:default profile (reads only).")
    warn(f"Opening a visible Chrome on {url} as {provider}:default. Go to the pages you want commands for; only "
         f"reads go out (every POST, PUT, PATCH, and DELETE is blocked). Close the window when you're done (it "
         f"stops by itself after {timeout}s).")
    try:
        data = capture.guided(name, url, timeout)
    except Exception as exc:
        die(f"Capture failed: {type(exc).__name__}: {exc}", EXIT_API,
            hint=f"Try again, or make the tool without --capture. If the site needs sign-in: `webcli new {name}` "
                 f"first, then `webcli identity login {provider}:default`, then `webcli new {name} --capture`.")
    proposals = capture.propose(url, data)
    record = capture.write(name, {"tool": name, "identity": f"{provider}:default", **data, "proposals": proposals})
    return {"proposals": proposals, "record": str(record), "fixture": capture.fixture(data.get("snapshot"))}


def save_fixture(folder: Path, fixture: dict | None) -> str | None:
    if fixture is None:
        return None
    rel = f"tests/fixtures/capture-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    (folder / rel).write_text(json.dumps(fixture, indent=1) + "\n", encoding="utf-8")
    return rel


def next_steps(name: str, provider: str) -> list[str]:
    steps = [f"webcli lint {name}", f"webcli test {name}", f"webcli dev {name} -- page"]
    if provider == name:
        steps.append(f"webcli identity login {name}:default   (once, if the site needs sign-in; a person does it)")
    return [*steps, f"webcli submit {name}   (when it works: share it with the webcli-tools library)"]


def report(result: dict, fmt: str | None) -> None:
    if output.resolve_format(fmt) == "json":
        emit_json(result)
        return
    click.echo(f"{'Updated' if result.get('recaptured') else 'Made'} {result['folder']} "
               f"(identity {result['identity']}, hosts {', '.join(result['hosts'])}).")
    found = (result.get("capture") or {}).get("proposals")
    if found:
        click.echo(f"Capture: {result['capture']['record']}")
        for label, key in (("Hosts it read from", "hosts"), ("Other hosts (not added)", "otherHosts"),
                           ("Reads (candidate requests for [[commands]])", "reads"),
                           ("Blocked (not reads; a GraphQL \"(query)\" may be a read to add by hand)", "blocked")):
            if found.get(key):
                click.echo(f"{label}:\n" + "\n".join(f"  {line}" for line in found[key]))
    if result.get("missingHosts"):
        click.echo("Add these to hosts in webcli-tool.toml and endpoints.py if the tool needs them: "
                   + ", ".join(result["missingHosts"]))
    click.echo("Next:\n" + "\n".join(f"  {step}" for step in result["next"]))


@effects("always", human=True, window=True, local_setup=True)
@click.command("new", options_metavar="[--url URL] [--identity ID] [--capture]",
               short_help="Make a new tool for a site you use.")
@click.argument("name")
@click.option("--url", default=None, metavar="URL",
              help=f"The https page the tool starts from (default: the placeholder {scaffold.PLACEHOLDER_URL}).")
@click.option("--identity", "identity", metavar="ID", default=None,
              help="The provider it signs in as (default: NAME, so NAME:default). Another provider needs "
                   "`webcli tools grant` before the tool may use it.")
@click.option("--capture", is_flag=True,
              help="First click around the site in a visible Chrome that lets only reads out; the tool starts "
                   "from what it saw. Needs a person. Again on an existing tool: records another page.")
@click.option("--timeout", default=CAPTURE_TIMEOUT, show_default=True, type=click.IntRange(10, 3600),
              help="Seconds --capture waits for you to close the window.")
@agent_output_options(rows=False)
def new_cmd(name, url, identity, capture, timeout, fmt, quiet) -> None:
    """Make a read-only tool for a site you use: ./webcli-tool-NAME/, with its manifest, code, and offline tests,
    listed in ~/.webcli/tools/dev.toml. NAME is lowercase letters and digits (no hyphens). Then `webcli dev NAME --
    page` runs it, `webcli lint NAME` and `webcli test NAME` check it, and `webcli submit NAME` shares it."""
    folder = Path.cwd() / scaffold.folder_name(name)
    if capture and (folder / manifest_schema.MANIFEST_NAME).is_file():
        recapture(name, folder, url, timeout, fmt)
    check_name(name)
    provider = provider_for(name, identity)
    url = url or scaffold.PLACEHOLDER_URL
    try:
        host, _ = scaffold.parse_url(url)
    except UsageError as exc:
        die(str(exc), EXIT_USAGE)
    if folder.exists():
        die(f"{folder} already exists.", EXIT_USAGE, hint="Pick another name, or move it out of the way.")
    elsewhere = scaffold.dev_tools().get(name)
    if elsewhere is not None and elsewhere.resolve() != folder.resolve() and elsewhere.is_dir():
        die(f"You're already writing a tool named {name!r}, at {elsewhere}.", EXIT_USAGE,
            hint=f"Work on that one (`webcli dev {name}`), or pick another name.")
    captured = run_capture(name, provider, url, timeout) if capture else None
    hosts = captured["proposals"]["hosts"] if captured else [host]
    files = scaffold.render(name=name, provider=provider, url=url, hosts=hosts)
    manifest_schema.loads(files[manifest_schema.MANIFEST_NAME], str(folder / manifest_schema.MANIFEST_NAME))
    scaffold.write(folder, files)
    result = {"name": name, "folder": str(folder), "identity": f"{provider}:default", "hosts": hosts,
              "devToml": str(scaffold.register(name, folder)), "next": next_steps(name, provider)}
    if captured:
        result["capture"] = {"proposals": captured["proposals"], "record": captured["record"],
                             "fixture": save_fixture(folder, captured["fixture"])}
    report(result, fmt)
    raise SystemExit(EXIT_OK)


def recapture(name: str, folder: Path, url: str | None, timeout: int, fmt: str | None) -> None:
    """`webcli new NAME --capture` on a tool that exists: record the site again (say, after `identity login`) and add
    the page to tests/fixtures/. The tool's code and manifest stay as they are; new hosts are only suggested."""
    manifest = manifest_schema.load(folder)
    if manifest.name != name:
        die(f"{folder} holds the tool {manifest.name!r}, not {name!r}.", EXIT_USAGE)
    url = url or next((f"https://{h}/" for h in manifest.hosts if not h.startswith("*.")), None)
    if url is None:
        die("Its hosts are all wildcards: pass --url.", EXIT_USAGE)
    try:
        scaffold.parse_url(url)
    except UsageError as exc:
        die(str(exc), EXIT_USAGE)
    captured = run_capture(name, manifest.identity, url, timeout)
    found = captured["proposals"]
    result = {"name": name, "folder": str(folder), "identity": f"{manifest.identity}:default",
              "hosts": list(manifest.hosts), "recaptured": True,
              "devToml": str(scaffold.register(name, folder)), "next": next_steps(name, manifest.identity),
              "missingHosts": [h for h in found["hosts"] if not any(host_matches(p, h) for p in manifest.hosts)],
              "capture": {"proposals": found, "record": captured["record"],
                          "fixture": save_fixture(folder, captured["fixture"])}}
    report(result, fmt)
    raise SystemExit(EXIT_OK)


COMMANDS = [new_cmd]
