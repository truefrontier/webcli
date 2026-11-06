# The library index: `index.json`

The webcli-tools library (`truefrontier/webcli-tools`) lists its tools in one file at the top of the repo, `index.json`. `webcli tools` reads it to search, install, and update tools. Core reads its `revoked` list to switch bad tools off.

The index is what you trust. It pins each tool to one commit and to a hash of the tool's files, so a tool installs exactly as it was reviewed.

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
      "verified": false
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
| `tools` | yes | A list, one entry per tool. Each name appears once. |
| `tools[].name` | yes | The tool's name, the same as in its `webcli-tool.toml`. 2–32 lowercase letters and digits, with no hyphens. |
| `tools[].version` | yes | The same as the manifest's `version`. Install refuses a tool whose manifest says otherwise. |
| `tools[].summary` | no | One line. `webcli tools search` matches on it. |
| `tools[].commit` | yes | The full 40-character commit SHA. Never a branch or tag. |
| `tools[].sha256` | yes | The tree hash of the tool's folder at that commit (see below). 64 lowercase hex digits. |
| `tools[].path` | no | The tool's folder in the repo. Defaults to `tools/<name>`. No `..` and no leading `/`. |
| `tools[].verified` | no | `true` only if a maintainer has verified the tool. Defaults to `false`. |
| `revoked` | yes | The kill switch, even when it's empty (`[]`). Each entry is a tool name, which revokes every version, or an object with `name`, an optional `versions` list of exact version strings, and an optional `reason`. |

Any other key is ignored. `revoked` is required so that an index cut off partway can't quietly drop a revocation.

## The hash

`sha256` is a hash of the tool's files, not of a tarball, because GitHub doesn't promise its tarballs stay byte-for-byte the same. To compute it:

1. List every file under the tool's folder by its path relative to that folder, with `/` between parts.
2. Sort the paths.
3. For each file, write the line `<path>\0<sha256 of the file's bytes, in hex>\n`.
4. Take the sha256 of all those lines together, in hex.

A tool holds regular files only. A symlink or other special file fails the install.

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
- **Download.** It fetches the repo tarball at the pinned commit (`https://codeload.github.com/truefrontier/webcli-tools/tar.gz/<commit>`) and takes only the tool's folder, regular files only. It unpacks into a dot-named staging folder that core's loader skips.
- **Check.** The files must match `sha256`. The manifest must be valid, and its name and version must match the index. Its `requires_webcli` range must include the running webcli. The tool can't be revoked, and its name can't belong to a built-in.
- **Ask.** It shows the manifest: hosts, identity, and every command that writes or sends to people. Then it asks before it installs, unless you pass `--yes`.
- **Install.** It moves the folder to `~/.webcli/tools/<name>/` and records the pin in `<name>/.installed.json`.
- **Update.** It does the same check and swap. It asks first if the new version wants a host, a writing or sending command, or an identity that the installed one didn't. Otherwise it updates without asking and returns a link to the diff.

## Testing against another index

Set `WEBCLI_TOOLS_INDEX_URL` to another `index.json`. Set `WEBCLI_TOOLS_ARCHIVE_URL` to another tarball URL, with `{commit}` where the SHA goes. Any URL Python's `urllib` opens works, `file://` included. That covers a fork, a local copy, or a test's fake library. `tests/test_tools_cmd.py` builds one.
