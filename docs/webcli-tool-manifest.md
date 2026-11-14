# The tool manifest: `webcli-tool.toml`

Every webcli tool outside core has a `webcli-tool.toml` file at the top of its folder. It says, in one place, what the tool is, which sites it talks to, which identity it signs in as, and which of its commands write. A person reads it before installing the tool, and core holds the tool to it.

One validator checks it everywhere: `webcli new`, `webcli lint`, `webcli tools install`, `webcli submit`, core's tool loader, and the library's CI. It lives in `webcli/core/manifest_schema.py`.

## Example

```toml
name = "acmenotes"
version = "0.1.0"
adapter_api = 1
requires_webcli = ">=1.0.0rc1,<2"
summary = "Acme Notes: list and read your notes; add a label (with --confirm)"
identity = "acmenotes"
hosts = ["notes.example.com", "*.cdn.example.com"]
maintainers = ["github:someone"]
license = "MIT"

[[commands]]
name = "status"
strategy = "cookie"
requests = ["GET notes.example.com/api/me"]

[[commands]]
name = "list"
strategy = "cookie"
writes = false
requests = ["POST notes.example.com/graphql op=ListNotes (query)"]

[[commands]]
name = "label add"
strategy = "cookie"
writes = true
requests = ["POST notes.example.com/graphql op=AddLabel (mutation)"]
```

## Top-level fields

| Field | Required | Rule |
|---|---|---|
| `name` | yes | 2–32 lowercase letters and digits, starting with a letter. No hyphens: the name is also the tool's identity, `<name>:default`. It can't be a built-in site (`gmail`, `loom`, ...), the provider a built-in signs in as (`google`, `meta`), `none`, or a `webcli` command (`sites`, `feed`, `identity`, `doctor`, `serve`, `agent-help`, `new`, `dev`, `test`, `lint`, `tools`, `submit`). webcli won't load a tool by any of these names, so `webcli lint`, `webcli new`, and `webcli tools install` refuse them too. |
| `version` | yes | `X.Y.Z`, like `0.1.0`. A pre-release adds `a`, `b`, or `rc` and a number: `0.2.0rc1`. |
| `adapter_api` | yes | The adapter API the tool was built for. This webcli supports `1`. |
| `requires_webcli` | no | A version range for webcli, like `">=1.0.0rc1,<2"`. Note that `>=1.0` leaves out `1.0.0rc1`, because a release candidate comes before its release. |
| `summary` | yes | One line: what the tool does, and whether it writes. |
| `identity` | no | The identity provider the tool signs in as. It defaults to `name`, so the tool signs in as `<name>:default`. Use `"none"` for a tool that never signs in. Give the provider only (`"loom"`), not a full identity (`"loom:default"`). Naming another provider, like `"google"`, is allowed, but the tool still can't use your `google:default` session until you grant it. |
| `hosts` | yes | Every host the tool's pages reach, at least one. Lowercase host names, like `www.example.com`. A leading `*.` covers subdomains: `*.example.com` matches `cdn.example.com` but not `example.com`. No scheme, port, or path, and no repeats. |
| `maintainers` | yes | At least one, each `github:<login>`. |
| `license` | yes | The tool's license, like `MIT`. |
| `[[commands]]` | yes | One table per command, at least one. |

Any other key is an error. That catches typos: a `write = true` that was silently ignored would make a write command look read-only.

## Commands

| Field | Required | Rule |
|---|---|---|
| `name` | yes | The command's path under the tool's group: one or two lowercase words, like `list` or `label add` (`webcli acmenotes label add`). Each name appears once. |
| `strategy` | yes | How the command gets its data: `public` (plain HTTP, no sign-in), `cookie` (the site's own API from your signed-in page), `intercept` (reads the page's own requests), or `ui` (drives the page). |
| `writes` | no | `true` if the command changes anything on the site. Defaults to `false`. |
| `sends_to_people` | no | `true` if the command reaches another person: a message, a post, an invite. Defaults to `false`. Needs `writes = true`, because sending is a write. |
| `requests` | no | The requests the command makes, one string each (see below). |

The tool's code uses the same command names. `Site.verbs`, `Site.writes`, and `Site.sends_to_people` in the tool's `ADAPTER` name commands exactly the way the manifest does.

### Requests

Each request is a method, then host and path, then any notes:

```
GET notes.example.com/api/me
POST notes.example.com/graphql op=ListNotes (query)
```

- The method is one of `GET`, `HEAD`, `OPTIONS`, `POST`, `PUT`, `PATCH`, or `DELETE`.
- The host must match one of `hosts`.
- For GraphQL, end the line with `(query)` or `(mutation)`.
- A read command (`writes = false`) can't list a `PUT`, `PATCH`, or `DELETE`, or a `(mutation)`.

These checks catch honest mistakes. They don't prove what the code does. A `POST` that deletes something passes them if nobody marks it. Reviewers read the code, and core enforces the rules below while the tool runs.

## How core uses the manifest

- **The manifest is the authority.** It's what a person agreed to when they installed the tool. If the tool's `ADAPTER` disagrees with it on name, identity, adapter API, version, hosts, commands, writes, or sends, the tool isn't loaded. `manifest_schema.mismatches(manifest, site)` lists the differences.
- **Hosts.** For a community tool, core blocks any request to a host not in `hosts`, whatever the tool's own guard says. A tool's guard can narrow that list, never widen it.
- **Identity.** A tool signs in as `<identity>:default`. Using an identity you already have, like `google:default`, needs `webcli tools grant <tool> <identity>`.
- **Writes.** A command marked `writes = true` is a dry run unless you pass `--confirm` (or `--yes`).
- **Tier.** Core marks each tool `official` (built in) or `community` (installed). A manifest can't set its tier.
- **`requires_webcli`.** The validator checks only its form. Whether the running webcli fits the range is checked when a tool is installed or loaded.

## Checking a manifest

From a shell, for CI:

```
python -m webcli.core.manifest_schema path/to/webcli-tool.toml
```

It prints `ok` for each valid file and exits 0. Otherwise it lists every broken rule and exits 7.

From Python:

```python
from webcli.core.manifest_schema import ManifestError, load

try:
    manifest = load("webcli-tool-acmenotes")  # a folder, or the .toml file itself
except ManifestError as exc:
    print(exc.problems)  # one line per broken rule
```

`load` returns a `Manifest`. Its `writes` and `sends_to_people` give the command names that write or send, and `default_identity` gives `<identity>:default`, or `None` for `identity = "none"`. `ManifestError` is a `ConfigError`, so the CLI exits 7 on it.

## Versions

The manifest has no version number of its own. Its `adapter_api` says which adapter API it was written for. A change to these rules that would turn a valid manifest invalid comes with a new adapter API version. See [adapter-api-v1.md](adapter-api-v1.md).
