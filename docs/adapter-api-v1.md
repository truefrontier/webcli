# Adapter API v1

A webcli tool imports from one module, `webcli.adapter`, and nothing else in webcli. Everything in that module stays put for as long as core supports API 1. Everything outside it can change in any release.

```python
from webcli.adapter import ADAPTER_API, Site, Strategy, Policy, host_matches, write_options, confirm_write
```

## What's in it

| Group | Names |
|---|---|
| Version | `ADAPTER_API` (`1`), `SUPPORTED_ADAPTER_APIS` |
| Declaring a tool | `Site`, `Strategy`, `NO_IDENTITY` |
| The request guard | `Policy`, `Guard`, `describe`, `host_matches`, `path_matches` |
| Errors | `AuthRequired`, `Challenged`, `ConfigError`, `Interrupted`, `NotFound`, `ProfileBusy`, `RateLimited`, `UsageError` |
| Output and exit codes | `EXIT_OK`, `EXIT_USAGE`, `EXIT_NOT_FOUND`, `EXIT_AUTH`, `EXIT_API`, `EXIT_RATE_LIMITED`, `EXIT_CONFIG`, `EXIT_SENT_UNVERIFIED`, `agent_output_options`, `emit_json`, `emit_rows`, `die`, `fail`, `note`, `warn` |
| Writes | `write_options` (`--dry-run` by default, `--confirm` alias `--yes`), `confirm_write` |
| Redaction | `redact_text`, `redact_value`, `redact_url`, `shape` |
| A browser tool | `site_group` (the tool's `webcli <tool>` group, with `--identity`), `call` (run one of its fetch ops in the identity's hidden Chrome), `site_page` (the page an op works in; `mode="visible", sign_in=True` for the sign-in window), `emit_record`, `login_hint`, `add_agent_help` |

Raise these errors rather than exiting with your own codes. That way every tool's exit codes mean the same thing.

## Declaring a tool

A tool's package exposes `ADAPTER = Site(...)`. API 1 adds five optional fields to `Site`. All of them are optional, and their defaults keep an older site working. Since 1.0.0rc2 the built-in sites that write set `writes` and `sends_to_people` too, in their own verb names (gsc's are `submit_sitemap` and `request_indexing`).

| Field | Default | Meaning |
|---|---|---|
| `adapter_api` | `1` | The API the tool was built for. |
| `hosts` | `()` | Every host the tool's pages reach, as in the manifest. |
| `version` | `""` | The tool's own version. Built-ins ship with webcli and leave it empty. |
| `writes` | `frozenset()` | Names of the commands that change something on the site. |
| `sends_to_people` | `frozenset()` | Of those, the ones that reach another person. |

```python
ADAPTER = Site(
    name="acmenotes", kind="data", provider="acmenotes", summary="Acme Notes: list and read your notes",
    cli=_cli, fetch="webcli_tool_acmenotes.fetch", ops=("status", "notes", "add_label"),
    verbs={"status": Strategy.COOKIE, "list": Strategy.COOKIE, "label add": Strategy.COOKIE},
    adapter_api=1, version="0.1.0", hosts=("notes.example.com", "*.cdn.example.com"),
    writes=frozenset({"label add"}),
)
```

A command's name is its path under the tool's group, with words split by one space: `webcli acmenotes label add` is `"label add"`. `verbs`, `writes`, and `sends_to_people` all use those names, and so does the manifest.

These facts appear twice: in code, and in the tool's `webcli-tool.toml` manifest ([webcli-tool-manifest.md](webcli-tool-manifest.md)). The manifest is the authority. A tool whose `Site` disagrees with its manifest isn't loaded.

`Site` has no `tier` field. Core decides `official` (built in) or `community` (installed) when it loads a tool, and a tool can't claim either.

`webcli sites --format json` now shows `adapterApi` for every site, and shows `version`, `hosts`, `writes`, and `sendsToPeople` for any site that sets them.

## Versions

- `ADAPTER_API` goes up only when a change here would break a tool: a name removed or renamed, or a signature or return shape changed. Adding a name doesn't change it.
- Core loads tools built for any version in `SUPPORTED_ADAPTER_APIS`: the current one and, once there is one, the version before it. It lists a tool built for any other version as needing an update and doesn't load it.
- A tool's own `version` is separate. The library index pins each tool version to a commit and a hash.

## Command effects (effectsApi 1, since 1.0.0rc2)

Every command in `agent-help --format json` says what it can do besides read. The `webcli` umbrella, each site's own `agent-help`, `llmcli`, and `chromecli` all report it. A program that decides what an agent may run, such as a policy wrapper, can read these facts instead of keeping its own list.

### What agent-help prints

The reference gets a top-level `"effectsApi": 1`. A webcli without it (0.18.x, 1.0.0rc1) publishes no effects, so a reader should fall back to its own rules.

Each command gets an `effects` block:

```json
{"name": "gsc inspect", "...": "...",
 "effects": {"writes": "with_flag", "writeFlags": ["--request-indexing", "--yes", "--confirm"],
             "sendsToPeople": false, "humanOnly": false, "opensWindow": false, "changesLocalSetup": false}}
```

| Key | Values | Meaning |
|---|---|---|
| `writes` | `"never"`, `"with_flag"`, `"always"` | `never`: it only reads. `with_flag`: a dry run unless one of `writeFlags` is given. `always`: it can change something with no flag at all, for example after a prompt a person answers. |
| `writeFlags` | list of flags | Only for `with_flag`: every spelling, aliases included (`--confirm`, `--yes`, `--send`). Treat any one of them on the command line as a write. Some commands need two of them to actually write (`gsc inspect` needs `--request-indexing` and `--yes`), so the list is a conservative trigger, not a recipe. Empty for `never` and `always`. |
| `sendsToPeople` | bool | It can reach another person: a message, a post, a pull request, or an agent that acts on its own (Muse). |
| `humanOnly` | bool | Only a person should run it: it signs in, opens a window for a person, stores a secret, changes which tools or identities webcli trusts, runs a daemon, or runs code nobody reviewed (`dev`, `test`, `lint`). |
| `opensWindow` | bool | It can show a visible Chrome window or bring one to the front (`login`, `--headed`, `new --capture`, chromecli `open` and `focus`). |
| `changesLocalSetup` | bool | Its writes change webcli's setup on this computer, not a site: tools, grants, launchd agents, the data home's permissions, a stored key, a sign-in profile. |

`writes` counts changes on a site or account and changes to webcli's own setup. It doesn't count the output you asked for (an export, a download, a feed file), webcli's caches and logs, or the ordinary state a page keeps when you look at it. For example, opening a chat can mark it read, as it does in a browser.

### Where the facts come from

- **Built-ins** declare them next to each click command with `@effects(...)` from `webcli.core.output`, for example `@effects("with_flag", via=("confirm",), sends=True)` above `@group.command("send")`. `via` names the click options that turn the write on, and agent-help prints all of their spellings.
- **Community and dev tools** get them from the manifest, never from the tool's code. Core replaces whatever the code set when it loads the tool. A command the manifest marks `writes = true` (which core gates) is `with_flag` with the gate's `--confirm`/`--yes`, and `sendsToPeople` comes from `sends_to_people`. A manifest read is `never`. Core's own `agent-help` is `never`.
- **Anything else** is undeclared and reports the worst case: `{"writes": "always", "writeFlags": [], "sendsToPeople": true, "humanOnly": true, "opensWindow": true, "changesLocalSetup": true, "undeclared": true}`. That covers a built-in command someone forgot to declare, a tool command its manifest doesn't name, a broken tool's placeholder command, and a declaration that doesn't add up (a `via` that isn't an on/off flag of the command). A command never reads as `never` by accident.
- A `via` flag that can be on without being typed (a default, an env var, an `auto_envvar_prefix` or `default_map` in the command's or a tool group's context settings) makes the command `always`.

`tests/test_effects.py` keeps this true. Every command of all three binaries must declare effects, and the reviewed table of every non-read command is checked exactly. Every write-looking option (`--confirm`, `--yes`, `-y`, `--send`, `--request-indexing`, `--new-ok`, `--fix`, `--install-launchd`) must be declared, and every `--headed`, `--capture`, and `login` must say it opens a window. A new command can't ship without a declaration.

### `webcli sites --format json`: where each site came from

Every site entry gains:

| Key | Built-in | Community tool |
|---|---|---|
| `source` | `"builtin"` | `"installed"` (from `webcli tools install`) or `"dev"` (from `webcli new`, listed in dev.toml), from the folder core actually loaded |
| `verified` | `null` | `true` only for an installed tool whose pinned version, commit, and sha256 (`.installed.json`) match a cached library index entry marked verified, and whose files on disk still hash to that sha256. A dev tool is never verified. |
| `commit`, `sha256` | `null` | What install pinned; `null` for a dev tool |
| `grants` | `[]` | The identities `webcli tools grant` gave it (grants.toml), the same list the daemon checks |
| `revoked` | `false` | `false`, or the library's reason |
| `disabled` | `false` | `false` for a loaded tool |

A top-level `skipped` list adds the community tools core didn't load, with the same keys plus `name` and `reason`: revoked, disabled, a name clash (a dev tool shadowed by an installed one), or broken. Another package's entry point shows as `source: "entrypoint"`. This list is read from manifests, install records, the cached index, grants.toml, and the revoked lists only. A skipped tool's code is never imported. An unreadable grants.toml makes `webcli sites` exit 7, as the daemon does: no grant is assumed.

### Limits

- **A tool's code runs in webcli's process.** Effects for a community tool are what its manifest promises and core's gate enforces. Core gates the leaf command a manifest names. Code a tool runs elsewhere, such as at import, in a group callback, or in an option callback, isn't gated, and a tool can patch anything in the process. The library's review, the pinned commit, and the sha256 are what make a tool trustworthy. `verified` reports them, and nothing here sandboxes code.
- **Listing commands imports enabled tools.** `agent-help` and `sites` build the command tree, so each enabled community tool's import-time code runs before a reader sees `verified`. A reader that allows only official sites can ask with `WEBCLI_NO_TOOLS=1`, which loads no community tool at all.
- **Bytecode isn't hashed.** `verified` hashes the tool's files as installed. It leaves out `.installed.json`, `.disabled`, and the `__pycache__/` folders Python writes, so a replaced `.pyc` isn't detected. Someone who can write to `~/.webcli/tools/` as you can also change webcli itself.
- **The cached index decides `verified`.** `webcli sites` never goes online. Until `webcli tools search`, `info`, `install`, or `update` refreshes the cache, a newly verified tool reads `false` and the old revoked list applies.
