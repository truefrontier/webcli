# How core loads community tools, and what it holds them to

A community tool is Python code that runs next to your signed-in sessions. Core can't make that safe, but it can keep a tool to what its manifest says, keep it off your existing logins, and switch a bad one off. This page lists what core does and where it keeps each file. `webcli new`, `webcli tools`, and the library's `index.json` write these files; core reads them.

## The files

Everything lives in `~/.webcli/tools/` (or `$WEBCLI_HOME/tools/`), mode 0700.

| Path | Written by | What it holds |
|---|---|---|
| `<name>/` | `webcli tools install` | One installed tool: its `webcli-tool.toml` and its package, `webcli_tool_<name>/`, whose `__init__.py` sets `ADAPTER = Site(...)`. |
| `<name>/.disabled` | `webcli tools disable` | If present, the tool is off. Core never imports its code. |
| `<name>/.installed.json` | `webcli tools install` | What the install pinned: version, commit, sha256, and the index it came from. Core doesn't read it. |
| `.<anything>/` | `webcli tools install` | Skipped. Use a dot name for an install in progress (`webcli tools` uses `.stage-<name>-…` and `.old-<name>-…`). |
| `dev.toml` | `webcli new` | Tools you're still writing. One table per tool: `[toyhttp]` then `path = "/Users/you/webcli-tool-toyhttp"`. |
| `grants.toml` | `webcli tools grant` | Which of your identities each tool may use besides its own. Mode 0600. One line per tool: `toyhttp = ["google:default"]`. |
| `index-cache.json` | `webcli tools` | The library's `index.json`, cached. Core reads only its `revoked` list. Its format: [webcli-tools-index.md](webcli-tools-index.md). |
| `revoked.json` | anyone | More revocations, in the same shape. It may be the bare list. |

Core's code for each: `webcli/core/tools_registry.py` (loading), `webcli/core/grants.py`, `webcli/core/revoked.py`, and `webcli/sites/__init__.py` (the registry).

## Loading

Core loads the built-in sites first and marks them `official`. Then it loads the installed tools by name, then the `dev.toml` tools, and marks each one `community`. A tool never sets its own tier. `webcli sites` shows each site's tier.

Before core runs any of a tool's code, it skips the tool if:

- its name is a built-in site (`gmail`), a built-in site's provider (`google`, `meta`), a `webcli` command, or a name an earlier tool already took;
- it is disabled;
- its `webcli-tool.toml` is missing or invalid, or its `name` isn't the folder's (or the `dev.toml` key's);
- it is revoked.

Then core imports the tool's package from its folder. It doesn't add the folder to `sys.path`. Core skips the tool if:

- the import fails in any way;
- `ADAPTER` isn't a `Site` with the tool's name;
- the tool isn't a `webcli` data site (`kind = "data"`, no `llm_cli`);
- its `fetch` module is outside its own package;
- its `ADAPTER` disagrees with its manifest (`manifest_schema.mismatches`).

A skipped tool prints one warning on stderr and shows up in `webcli doctor` under `tools.skipped`, with the reason. A disabled tool is skipped without a warning. The other commands keep working. If a tool's commands fail to load later, it becomes one command that explains the problem and exits 7.

Another Python package's `webcli.sites` entry point isn't loaded. Community tools come with a manifest, through `webcli tools install` or `webcli new`.

To turn every community tool off at once, set `WEBCLI_NO_TOOLS=1`. That's the way out if a tool hangs while it loads.

## Identities

A community tool may always use its own identities, `<tool>:<account>`. A tool can't take a built-in site's or provider's name, so its own identities are never ones you made for something else.

Any other identity needs a grant: `webcli tools grant <tool> <identity>`. A tool whose manifest says `identity = "google"` can't use `google:default` until you grant it. Each grant covers one identity. If `grants.toml` can't be read, core treats it as granting nothing.

`webcli identity login` uses an official site's sign-in whenever one exists. A community tool signs in only its own identities.

## Hosts

While a community tool runs, its pages can reach only the hosts in its manifest (`Site.hosts`), whatever its own guard allows. This applies to its daemon ops and to its sign-in, and it covers requests and WebSockets alike. A tool with no hosts can reach nothing. Official sites keep their own guard only.

## Writes

For a community tool, each command its manifest marks `writes = true` is a dry run unless you pass `--confirm` (or `--yes`). Without `--confirm`, core prints `{"dryRun": true, ...}` and the tool's code for that command doesn't run.

This is best effort. It covers the commands the manifest names, not code the tool runs somewhere else. A write the manifest doesn't declare isn't gated. Reviewers read the code.

## Revocation

The library's `index.json` has a `revoked` list. Each entry is a tool name, or an object like this:

```json
{"name": "sometool", "versions": ["0.1.0"], "reason": "posted without asking"}
```

If an entry has no `versions`, every version is revoked. Versions are matched exactly; ranges aren't supported. Core won't load a revoked tool. The daemon also re-reads the list before every op of a community tool, so a daemon that is already running honors a new revocation.

If `index-cache.json` or `revoked.json` exists but can't be read, no community tool runs until you fix or remove the file. A damaged cache never switches a revoked tool back on.

## What core can't do

A tool's Python can still do anything your user account can: read files, start processes, or skip these checks. The checks catch honest mistakes and hold well-behaved tools to their manifest. They don't make a stranger's code safe. A tool updated while its daemon runs keeps its old code until the daemon restarts.

## Who can reach the sockets

The daemon (`run/<identity>.sock`) and chromecli-host (`run/chromecli.sock`) listen on unix sockets in `~/.webcli/run/`, which is mode 0700. The sockets are 0600. No other account on the Mac can open them. Any program running as you can.

A request on the socket names a site, an op, and its arguments, and the daemon runs it. A write op's `confirm` is one of those arguments, so a client that opens the socket sets it itself, the same way `--confirm` would. The dry-run default lives in the CLI, not behind the socket. chromecli-host is the same, except the extension still asks in the browser before a risky action.

So a policy that decides which webcli commands an agent may run, from `agent-help` effects or a wrapper like webcli-guard, holds only if that agent also can't open `~/.webcli/run/`. Run the agent under a sandbox profile that denies that folder, or as a separate macOS user.
