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

A tool's package exposes `ADAPTER = Site(...)`. API 1 adds five optional fields to `Site`. Built-in sites don't set them yet, and their defaults keep them working.

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
