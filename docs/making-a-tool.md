# Making a webcli tool

`webcli new` makes a read-only tool for a site you use. You run it with `webcli dev`, check it with `webcli lint` and `webcli test`, and share it with `webcli submit`. None of this needs the library or the network, except running the tool itself.

## The short version

```
webcli new acmenotes --url https://notes.example.com/
webcli lint acmenotes
webcli test acmenotes
webcli dev acmenotes -- page
webcli identity login acmenotes:default   # once, if the site needs sign-in (a person does this)
webcli submit acmenotes                   # when it works
```

## `webcli new NAME`

It makes `./webcli-tool-NAME/` and lists it in `~/.webcli/tools/dev.toml`, so the other commands find it by name.

| Option | What it does |
|---|---|
| `--url URL` | The `https://` page the tool starts from. Its host becomes the tool's only host. Without it, the tool points at `www.example.com`, and `lint` warns until you change it. |
| `--identity ID` | The provider the tool signs in as. It defaults to the tool's own name, so the tool gets its own identity, `NAME:default`. Naming another provider, like `google`, prints a warning: webcli won't let the tool use `google:default` until you run `webcli tools grant NAME google:default`. |
| `--capture` | Before writing anything, opens a visible Chrome on the URL for you to click around in. See [Guided capture](#guided-capture). |

The name is 2 to 32 lowercase letters and digits, starting with a letter. It can't have hyphens, because it's also the identity's name. `new` refuses a built-in site's name (`gmail`), a built-in provider's (`google`), a `webcli` command's (`lint`), and a name the library already has (when `webcli tools` has cached its index). It never overwrites a folder.

The tool it makes reads one page: `page` lists the page's headings and links, and `status` says whether the page shows a sign-in form. It has its own guard: GET and HEAD only, to the tool's hosts only. Its tests run offline against `tests/fixtures/`.

| File | What's in it |
|---|---|
| `webcli-tool.toml` | The manifest: what the tool is, its hosts, its identity, and its commands ([rules](webcli-tool-manifest.md)) |
| `webcli_tool_NAME/__init__.py` | `ADAPTER = Site(...)`: the same facts, in code |
| `webcli_tool_NAME/endpoints.py` | The URL and the hosts |
| `webcli_tool_NAME/guard.py` | Which requests the page may make |
| `webcli_tool_NAME/fetch.py` | What the tool does in Chrome (its daemon ops), and its sign-in window |
| `webcli_tool_NAME/parse.py` | Turning what the page shows into rows. Pure, so tests can check it |
| `webcli_tool_NAME/cli.py` | The commands people type |
| `tests/` | Offline tests and recorded pages |

A tool imports from `webcli.adapter` and its own package only ([adapter API](adapter-api-v1.md)).

## Guided capture

`webcli new NAME --url URL --capture` opens a visible Chrome on `URL`, signed in as the tool's identity. You go to the pages you want commands for, then close the window.

- **Only reads go out.** The window allows GET, HEAD, and OPTIONS. Every POST, PUT, PATCH, and DELETE is blocked and listed, and so is every WebSocket. A GraphQL query sent as a POST is blocked too; the list names it (`op=ListNotes (query)`) so you can add it by hand. A site that changes something on a GET can still do that, so click carefully.
- **Hosts.** The tool gets the URL's host plus the hosts under it that the page read from (for `www.example.com`, also `api.example.com`). Other hosts are listed but not added.
- **Reads.** The read requests it saw are printed as candidates for each command's `requests`.
- **Fixture.** The last page you were on is saved to `tests/fixtures/capture-<time>.json`. Tokens, session IDs, and signed links are redacted, and email addresses become `user@example.com`. Names and other page text stay, so read the file before you share the tool.
- **The full record** goes to `~/.webcli/sites/NAME/capture/` (mode 0600). It stays on your machine.

Capture can't sign in for you, since sign-in needs a POST. If the site needs it, make the tool first, sign in, then capture again:

```
webcli new acmenotes --url https://notes.example.com/
webcli identity login acmenotes:default
webcli new acmenotes --capture
```

On a tool that already exists, `--capture` adds a recorded page and suggests any new hosts. It doesn't change the tool's code or manifest.

## `webcli dev NAME -- ARGS`

It runs the tool as `webcli NAME ARGS` would: `webcli dev acmenotes -- page --format json`. Core loads it from `dev.toml` the way it loads an installed tool, with the same trust rules ([tool-trust.md](tool-trust.md)). Chrome runs in this command, not in the identity's daemon, so each run picks up your latest edits. If core skipped the tool (a broken manifest, a name clash), `dev` says why and stops.

## `webcli test NAME`

It runs each `test_*` function in `tests/test_*.py` with the network blocked, the way the library's CI does. Tests are plain functions that take no arguments. Plain `pytest` runs the same files. It exits 0 when all pass, 7 when one fails or the tool doesn't load, and 3 when there are no tests.

## `webcli lint NAME`

It runs the library's checks. It exits 0 when there are no errors (warnings are fine), and 7 otherwise.

| Check | Error when |
|---|---|
| `manifest` | `webcli-tool.toml` breaks a rule |
| `adapter` | `ADAPTER` doesn't load, or disagrees with the manifest |
| `commands` | A command isn't in the manifest, or the manifest lists one the tool doesn't have |
| `writes` | A command takes `--confirm` but isn't marked `writes = true`. A marked write is a warning: the library reviews each one |
| `guard` | The tool's `POLICY` lets the page reach a host outside `hosts`, or lets a PUT, PATCH, or DELETE through when no command writes |
| `imports` | The tool imports anything from webcli besides `webcli.adapter` |
| `tests` | There's no `tests/test_*.py` |
| `secrets` | Any file has a private key, an API key or token, or a Cookie or Authorization value. Fixtures also can't have anything webcli's redaction would hide, or an email address |

It warns about the placeholder host and maintainer, too.

`test` and `lint` also take a tool folder's path instead of a name: `webcli lint ./tools/acmenotes`. `test` and `lint` import the tool's code, so run them on code you trust.
