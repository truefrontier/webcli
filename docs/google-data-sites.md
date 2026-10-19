# Google data sites: keep, photos, youtube, recorder

All four sites are built, tested offline, and registered. None has run against live Google yet: the build was not allowed to sign in. The first live run is the real test of each request guard's allowlist.

## What ships

| Site | Verbs | Source | How it reads |
|---|---|---|---|
| `webcli keep` | `list`, `get`, `info`, `search`, `status` | google-keep-cli | Reads the page's own `notes/v1/changes` sync |
| `webcli photos` | `list`, `search`, `info`, `albums`, `download`, `status` | google-photos-cli | Reads the rendered page; `download` fetches the `=dv` / `=d` original |
| `webcli youtube` | `history`, `status` | youtube-cli (ytcli) | youtubei `browse` inside the page; My Activity for exact times |
| `webcli recorder` | `list`, `info`, `search`, `transcript`, `download`, `sync`, `status` | google-recorder-cli | Reads the page's own `GetRecordingList` / `GetTranscription` |

All four:

- run as `google:default`, which gemini's login signs in (`login=None`, no second Google sign-in flow);
- are read-only on the site: no write verbs, and each guard blocks known writes;
- are headless only: no `--headed` flag, and an op called with `headed=True` still opens headless Chrome;
- add a `status` verb beyond the source verb lists, because each adapter declares `status_op="status"`.

## What was left out, and why

- **`recorder mcp` and `recorder-mcp`:** dropped (no MCP anywhere).
- **`recorder skill`:** dropped. The mechanism is local (it copies a bundled SKILL.md into `~/.claude/skills`), but the text documents the old `recorder` binary, its `~/.recorder-cli` profile, a `pipx install git+https://...` step, and the `recorder-mcp` server. So it is not MCP-free. `webcli recorder agent-help` is the agent reference instead.
- **Per-site `login`:** dropped. Use `webcli identity login google:default`.
- **ytcli's My Activity cookie file and `login --myactivity`:** dropped. webcli never reads cookie values, and the "verify it's you" check needs a window. `--source auto` falls back to YouTube's day-precision history behind that check.

## Guards: what is allowed, and the known limits

Every guard starts from `webcli/sites/google.py`:

- GET/HEAD only over https on the site's hosts plus Google's sign-in and static hosts;
- no sign-out GETs;
- Google's `RotateCookies` POST, which gemini also allows.

Each site then adds its own API reads:

- **keep:** a POST to `notes/v1/changes` passes only when it uploads nothing (no `nodes`, no `userInfo`). That body format comes from Keep's Android API; the web app's format is unverified.
- **photos:** a batchexecute POST passes only when every rpcid is in `READ_RPCIDS`. Those ids come from Google Photos Toolkit and are unverified with webcli.
- **recorder:** a POST (and its OPTIONS preflight) passes only to exactly `GetRecordingList` and `GetTranscription`.
- **youtube:** POSTs are limited to ytcli's three youtubei reads and My Activity's batchexecute. The My Activity rule allows any rpcid, as ytcli's did, because its read ids aren't mapped.

Downloads (photos originals, recorder audio) go through `page.request`, which the route guard never sees. So each redirect hop is checked against the site's download hosts before it is fetched. Files are written through an exclusive temp file and renamed into place, so they end up mode 0600.

Playwright routes only the first URL of a redirect chain. A redirect the server chooses is followed unchecked, as in the other sites.

## Live check for Kevin (after `webcli identity login google:default`)

Run each, and confirm no window appears:

```sh
python -m webcli.scripts.window_check -- webcli keep status
webcli keep list --limit 5
webcli photos list --limit 5
webcli photos download <id from the list> -o /tmp/photos-check
webcli youtube status
webcli youtube history --limit 5
webcli recorder list
webcli recorder transcript <id> -o -
```

If a command comes back empty or times out, its error names what the guard blocked. To see everything a page asked for, run:

```sh
python -m webcli.core.capture <site>
```

Add an rpcid, method, or host to that site's `endpoints.py` only once you know it is a read.

The version went from 0.1.0 to 0.2.0. A daemon still running 0.1.0 restarts on the first new command, which makes it load the new sites.
