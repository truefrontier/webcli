# Google data sites: keep, photos, youtube, recorder, contacts, gmail

All six sites are built, tested offline, and registered. None has run against live Google yet: the build was not allowed to sign in. The first live run is the real test of each request guard's allowlist.

Four are read-only. Two can write, and only with `--confirm`: `contacts label add|remove` and `gmail filters create|delete`. See [Writes](#writes-contacts-and-gmail).

## What ships

| Site | Verbs | Source | How it reads |
|---|---|---|---|
| `webcli keep` | `list`, `get`, `info`, `search`, `status` | google-keep-cli | Reads the page's own `notes/v1/changes` sync |
| `webcli photos` | `list`, `search`, `info`, `albums`, `download`, `status` | google-photos-cli | Reads the rendered page; `download` fetches the `=dv` / `=d` original |
| `webcli youtube` | `history`, `status` | youtube-cli (ytcli) | youtubei `browse` inside the page; My Activity for exact times |
| `webcli recorder` | `list`, `info`, `search`, `transcript`, `download`, `sync`, `status` | google-recorder-cli | Reads the page's own `GetRecordingList` / `GetTranscription` (after `ListLabels` / `GetRecordingInfo`) |
| `webcli contacts` | `list`, `get`, `search`, `other`, `labels`, `export`, `status`; writes: `label add`, `label remove` | google-contacts-cli (gcontacts) | Calls the app's own batchexecute RPCs from inside the page |
| `webcli gmail` | `filters list`, `status`; writes: `filters create`, `filters delete` | google-gmail-cli | Reads the Settings > Filters table; writes drive its dialogs |

All six:

- run as `google:default`, which gemini's login signs in (`login=None`, no second Google sign-in flow);
- are read-only on the site, except the four write verbs above, which are dry runs unless `--confirm`;
- are headless only: no `--headed` flag, and an op called with `headed=True` still opens headless Chrome;
- add a `status` verb beyond the source verb lists, because each adapter declares `status_op="status"`.

## What was left out, and why

- **`recorder mcp` and `recorder-mcp`:** dropped (no MCP anywhere).
- **`recorder skill`:** dropped. The mechanism is local (it copies a bundled SKILL.md into `~/.claude/skills`), but the text documents the old `recorder` binary, its `~/.recorder-cli` profile, a `pipx install git+https://...` step, and the `recorder-mcp` server. So it is not MCP-free. `webcli recorder agent-help` is the agent reference instead.
- **Per-site `login`:** dropped. Use `webcli identity login google:default`.
- **ytcli's My Activity cookie file and `login --myactivity`:** dropped. webcli never reads cookie values, and the "verify it's you" check needs a window. `--source auto` falls back to YouTube's day-precision history behind that check.
- **gcontacts' `login`, `--headed`, and its own profile:** dropped, as for the other sites. `status` reports `{signedIn, email, contactsApi}`; gcontacts' `name` field is gone.
- **gcontacts' exit codes:** mapped to webcli's table. Not signed in is now 4 (was 3), and not found is now 3 (was 4).
- **gmail `filters create --add-label`, `--forward`, and `--no-has-attachment`:** dropped. They never worked: `--add-label` ticked "Apply the label" without choosing a label, `--forward` filled in nothing, and `--no-has-attachment` did nothing.
- **gmail `filters delete` by a fragment of an id:** dropped. google-gmail-cli matched any id that contained or ended with the argument and clicked the first hit, which could delete the wrong filter. webcli matches the exact id `filters list` prints.

## Writes: contacts and gmail

Every write verb is a dry run by default. Without `--confirm` (alias `--yes`), it prints what it would change and exits 0. `--dry-run` with `--confirm` exits 2.

**`webcli contacts label add|remove LABEL`** keeps gcontacts' batch semantics exactly:

- exactly one selector: `--from-label NAME|ID`, `--query Q` (saved contacts only), or `--ids ID[,ID...]` (repeatable);
- contacts already in the wanted state are skipped, so a rerun is safe;
- 200 contacts per call, a random 1 to 2.5 second pause between calls, then a read-back: `accepted` counts what Google acknowledged, and `changed` counts only contacts that read back in the new state;
- a rate limit or lost session mid-run prints the result so far with `stopped: {code, error}`, then exits with that code (6, 4, or 5);
- `--create-missing` (add only) creates the label first; system groups are never a target (exit 2); a name two labels share exits 2 and lists both ids.

The dry run does every read (labels, the selection, each contact's labels) on a page that cannot write. Only `--confirm` opens the page with write permission. Even then, the guard checks each write's payload field by field: one label id, one direction, only the selected contacts' keys, and every other field empty. It allows one label create, under exactly the name given.

**`webcli gmail filters create|delete`:**

- `create` needs at least one criterion (`--from`, `--to`, `--subject`, `--query`, `--negated-query`, `--has-attachment`) and one action (`--skip-inbox`, `--mark-as-read`, `--star`, `--delete-it`, `--never-spam`). Its dry run is offline: it checks the plan and prints it.
- `delete FILTER_ID`'s dry run reads the filter list and prints the filter it would delete. An unknown id exits 3.
- google-gmail-cli had no dry run: create and delete wrote at once. webcli adds the dry-run default and `--confirm`, as the build spec requires.

## Guards: what is allowed, and the known limits

Every guard starts from `webcli/sites/google.py`:

- GET/HEAD only over https on the site's hosts plus Google's sign-in and static hosts;
- no sign-out GETs;
- Google's `RotateCookies` POST, which gemini also allows.

Each site then adds its own API reads:

- **keep:** a POST to exactly `notes-pa.clients6.google.com/notes/v1/changes` passes only when it uploads nothing (no `nodes`, no `userInfo`). Keep's web app sends it from gapi's proxy iframe, so a GET to exactly `notes-pa.clients6.google.com/static/proxy.html` passes too. Both were captured live on 2026-10-19; the web app's sync body carries `nodes` (empty), `targetVersion`, `clientTimestamp`, and `requestHeader`. Everything else the page calls stays blocked, `drive.google.com/auth_warmup` and `getFamilyInfo` included: the sync doesn't need them.
- **photos:** a batchexecute POST passes only when every rpcid is in `READ_RPCIDS`. Those ids come from Google Photos Toolkit and are unverified with webcli.
- **recorder:** a POST (and its OPTIONS preflight) passes only to exactly `ListLabels`, `GetRecordingList`, `GetRecordingInfo`, and `GetTranscription`. Captured live on 2026-10-19: the app asks for `ListLabels` before the list, and a recording's page asks for `GetRecordingInfo` before the transcript. With either blocked, the data call never comes. `GetShareList`, `GetAudioTag`, `GetWaveform`, `GetGlobalSearchReadiness`, and peoplestack-pa's contact autocomplete stay blocked.
- **youtube:** POSTs are limited to ytcli's three youtubei reads and My Activity's batchexecute. The My Activity rule allows any rpcid, as ytcli's did, because its read ids aren't mapped.
- **contacts:** a batchexecute POST passes only when its URL rpcids and its f.req body agree (counted) and every rpcid is one of gcontacts' five reads. gcontacts saw these live on 2026-10-18. A label write (`ls96Jc`, `ouU0xd`) passes only on a `--confirm` page, inside that run's scope. Every other known write (label delete and rename, contact edits, stars, merges, the page's prefs write on load) is blocked, and tests check each one.
- **gmail:** reads allow GET/HEAD only, and never with an `act`, `at`, or `logout` parameter. Every POST is blocked, including Gmail's sync, telemetry, and attestation. It is unverified whether the filters table renders with every POST blocked. If `filters list` times out, run `python -m webcli.core.capture gmail --url https://mail.google.com/mail/u/0/#settings/filters`. A `--confirm` run also allows ONE POST to `/mail/u/0/`. Its `act` must be that command's, and its query or form must carry the filter id (delete) or each typed criterion (create) as a whole parameter value. A create is not bound to its actions or to "has attachment", because their wire names are unverified. Only webcli's own clicks choose them. **The act values (`cf` create, `df` delete) are guesses:** google-gmail-cli's capture kept parameter names, not values. A wrong guess fails closed. The guard blocks the write, nothing changes, and the error names the blocked act, for example `POST mail.google.com/mail/u/0/ act=xf`. Add the real value to `webcli/sites/gmail/endpoints.py` only after a capture shows it is that filter write.

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
webcli contacts status
webcli contacts labels
webcli contacts label add <a label> --ids <one id from contacts list>   # dry run only: writes nothing
webcli gmail status
webcli gmail filters list
webcli gmail filters delete <an id from the list>                     # dry run only: writes nothing
```

Run the writes with `--confirm` only when you mean to change that label or filter.

If a command comes back empty or times out, its error names what the guard blocked. To see everything a page asked for, run:

```sh
python -m webcli.core.capture <site>
```

Capture runs headless and pauses the identity's daemon while it runs, as `identity login` does, so it doesn't wait on the profile.

Add an rpcid, method, or host to that site's `endpoints.py` only once you know it is a read.

The version went from 0.1.0 to 0.2.0 for the first four sites, then to 0.3.0 for contacts and gmail. A daemon running an older version restarts on the first new command, which makes it load the new sites.
