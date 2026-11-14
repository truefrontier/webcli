# The library index: `index.json`

The webcli-tools library (`truefrontier/webcli-tools`) lists its tools in one file at the top of the repo, `index.json`. `webcli tools` reads it to search, install, and update tools. Core reads its `revoked` list to switch bad tools off.

The index is what you trust. It pins each community tool to one commit and to a hash of the tool's files, so a tool installs exactly as it was reviewed.

It also catalogs webcli's official built-in data sites, like `loom` and `gmail`, so `webcli tools search` finds them next to the community tools. A built-in ships inside webcli: its entry has no commit or hash, and nothing ever downloads it.

## Format

```json
{
  "tools": [
    {
      "name": "examplehttpbin",
      "version": "0.1.0",
      "summary": "httpbin.org: read a few public endpoints (read only)",
      "commit": "3f1c9e0d2b7a4c5e6f8091a2b3c4d5e6f7a8b9c0",
      "sha256": "9b2d…64 hex digits…",
      "path": "tools/examplehttpbin",
      "tier": "community",
      "verified": false
    },
    {
      "name": "loom",
      "version": "1.0.0rc1",
      "summary": "Loom, read-only: list, info, transcript (official transcripts), status (loom-cli parity)",
      "tier": "official",
      "builtin": true,
      "verified": true,
      "requires_webcli": ">=1.0.0rc1,<2",
      "source": "core",
      "identity": "loom",
      "hosts": [],
      "commands": [{"name": "list", "strategy": "cookie", "writes": false}],
      "docs": "https://github.com/truefrontier/webcli/blob/main/docs/social-data-sites.md"
    }
  ],
  "revoked": [
    "sometool",
    {"name": "othertool", "versions": ["0.1.0"], "reason": "posted without asking"}
  ]
}
```

| Field | Required | Rule |
|---|---|---|
| `tools` | yes | A list, one entry per tool. Each name appears once. An entry is a community tool, or an official built-in when it has `"builtin": true`. |
| `tools[].name` | yes | The tool's name, the same as in its `webcli-tool.toml`. 2–32 lowercase letters and digits, with no hyphens. |
| `tools[].version` | yes | The same as the manifest's `version`. Install refuses a tool whose manifest says otherwise. |
| `tools[].summary` | no | One line. `webcli tools search` matches on it. |
| `tools[].commit` | yes | The full 40-character commit SHA. Never a branch or tag. |
| `tools[].sha256` | yes | The tree hash of the tool's folder at that commit (see below). 64 lowercase hex digits. |
| `tools[].path` | no | The tool's folder in the repo. Defaults to `tools/<name>`. No `..` and no leading `/`. |
| `tools[].tier` | no | `community`. Any other value is refused unless the entry is a built-in. |
| `tools[].verified` | no | `true` only if a maintainer has verified the tool. Defaults to `false`. |
| `revoked` | yes | The kill switch, even when it's empty (`[]`). Each entry is a tool name, which revokes every version, or an object with `name`, an optional `versions` list of exact version strings, and an optional `reason`. |

Any other key is ignored. `revoked` is required so that an index cut off partway can't quietly drop a revocation.

### An official built-in

| Field | Required | Rule |
|---|---|---|
| `name` | yes | The site's name in webcli. |
| `version` | yes | The webcli release that ships it. |
| `summary` | no | The site's `summary` in that release. |
| `builtin` | yes | `true`. |
| `tier` | yes | `official`. |
| `identity` | yes | The provider it signs in with (`google`), or `none`. |
| `hosts` | yes | A list of host strings. Built-ins keep their own host policy, so today it's `[]`. |
| `commands` | yes | A list of `{"name", "strategy", "writes"}`. `writes` is a boolean. |
| `requires_webcli` | no | The webcli versions that ship it. |
| `docs` | no | A link to the site's page in webcli's `docs/`. |
| `verified` | no | A boolean. The library sets `true`. |
| `commit`, `sha256`, `path` | never | A built-in with any of them is refused. |

The library writes these entries with `python scripts/library.py builtins`, which reads them from webcli's own registry. It lists only `kind = "data"` sites: chatgpt, gemini, and muse are llmcli's.

webcli 1.0.0rc1 predates built-in entries and refuses any index that has them. It keeps using its cached copy, with a warning.

## The hash

`sha256` is a hash of the tool's files, not of a tarball, because GitHub doesn't promise its tarballs stay byte-for-byte the same. To compute it:

1. List every file under the tool's folder by its path relative to that folder, with `/` between parts.
2. Sort the paths.
3. For each file, write the line `<path>\0<sha256 of the file's bytes, in hex>\n`.
4. Take the sha256 of all those lines together, in hex.

A tool holds regular files only. A symlink or other special file fails the install.

A tool holds source only, too. A `__pycache__/` folder, or any `.pyc`, `.pyo`, `.pyd`, or `.so` file, fails the install. Python runs those without the source a reviewer reads, and an unchecked-hash `.pyc` even runs in place of the `.py` file next to it. The `sha256` pin covers them, so the pin alone wouldn't catch one.

webcli computes the hash for you:

```
python -m webcli.core.index_client sha256 <folder>
```

Hash what `git archive` exports at the pinned commit, not your working copy. That's what `webcli tools install` downloads, so `.gitattributes` rules like `export-ignore`, `export-subst`, and `eol` can't make the two differ. Better still, don't use those rules under `tools/` at all. For example:

```
mkdir -p /tmp/export && git archive <commit> tools/<name> | tar -x -C /tmp/export
python -m webcli.core.index_client sha256 /tmp/export/tools/<name>
```

## What `webcli tools` does with it

- **Fetch.** It reads `https://raw.githubusercontent.com/truefrontier/webcli-tools/main/index.json` and saves it as `~/.webcli/tools/index-cache.json`. An index with the wrong shape is refused, and the old cache stays. Offline, `search`, `info`, and `install` use the cache and warn you. `update` needs a fresh index.
- **Download.** It fetches the repo tarball at the pinned commit (`https://codeload.github.com/truefrontier/webcli-tools/tar.gz/<commit>`) and takes only the tool's folder: regular files only, and no compiled code. It unpacks into a dot-named staging folder that core's loader skips.
- **Check.** The files must match `sha256`. The manifest must be valid, and its name and version must match the index. Its `requires_webcli` range must include the running webcli. The tool can't be revoked, and its name can't belong to a built-in.
- **Built-ins.** `search` lists them, marked installed `builtin` when this webcli ships them. `info` shows the catalog entry and downloads nothing. `install` on one does nothing and exits 0; if this webcli doesn't ship it yet, it exits 2 and says to update webcli. `update`, `remove`, `disable`, and `enable` on one exit 2. A community entry that uses a built-in's name is refused.
- **Ask.** It shows the manifest: hosts, identity, and every command that writes or sends to people. Then it asks before it installs, unless you pass `--yes`.
- **Install.** It moves the folder to `~/.webcli/tools/<name>/` and records the pin in `<name>/.installed.json`.
- **Update.** It does the same check and swap. It asks first if the new version wants a host, a writing or sending command, or an identity that the installed one didn't. Otherwise it updates without asking and returns a link to the diff.

## Testing against another index

Set `WEBCLI_TOOLS_INDEX_URL` to another `index.json`. Set `WEBCLI_TOOLS_ARCHIVE_URL` to another tarball URL, with `{commit}` where the SHA goes. Any URL Python's `urllib` opens works, `file://` included. That covers a fork, a local copy, or a test's fake library. `tests/test_tools_cmd.py` builds one.
