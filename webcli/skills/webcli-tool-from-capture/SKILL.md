---
name: webcli-tool-from-capture
description: Turn a webcli guided-capture record into draft read-only commands for a webcli community tool (manifest [[commands]], guard, fetch ops, parse.py, made-up fixtures, offline tests) without seeing the person's data. Use after `webcli new NAME --url URL --capture`, or when asked to "draft commands from the capture", "build the tool from what I clicked", or "turn the capture into a tool".
---

# Draft a webcli tool from a capture

A person ran `webcli new NAME --url URL --capture` and clicked around the site. The capture record lists the requests the pages made, and the **shape** of every first-party JSON response: its keys and types, never its values. You turn the reads the person wants into working commands, written against those shapes and tested offline. Then the person checks them on the real site.

You never see the person's data, and you never use their signed-in session. Keep it that way. One caveat: a shape keeps an object's keys, and webcli collapses the keys that look like data (ids, emails, titles, numbers) but can miss a short one, like a first name used as a map key. Treat a key that reads like a name or a word from someone's content as data: don't copy it into code, fixtures, or messages.

## Ground rules

- **Read only the safe parts of the record:** `proposals`, `shapes`, `shapesSkipped`, `tool`, `identity`, `url`. Never read its `snapshot`, and never open `tests/fixtures/capture-*.json`. Those hold text from the person's own pages.
- **Don't run anything on the real site.** That means no `webcli dev`, `webcli NAME …`, `webcli identity login`, or `webcli new --capture`. They use the person's sign-in, so the person runs them. When they do, ask for the exit code and stderr only, never stdout (stdout is their data).
- **Read-only commands only.** Leave `writes` out of every command. Use GET. A POST is allowed only for a GraphQL `(query)` the person names, and you add it by hand (see below).
- **Make up every fixture value.** Use plain placeholders like `"n1"`, `"Example note"`, `"2026-01-01T00:00:00Z"`. No email addresses (lint rejects them), no names, and nothing token-like: no long hex, no long digit strings, no JWT-looking strings. Lint rejects anything webcli's redaction would hide.
- **The manifest is the authority.** `webcli-tool.toml`, `ADAPTER` in `__init__.py`, and `endpoints.HOSTS` must agree. `webcli lint` checks this.

## 1. Read the record

The newest record is in `~/.webcli/sites/NAME/capture/`, or `$WEBCLI_HOME/sites/NAME/capture/` when that variable is set. The `webcli new` report also prints its path. Print only the safe parts:

```sh
python3 - "$(ls -t ~/.webcli/sites/NAME/capture/*.json | head -1)" <<'EOF'
import json, sys
record = json.load(open(sys.argv[1]))
print(json.dumps({k: record.get(k) for k in ("tool", "identity", "url", "proposals", "shapes", "shapesSkipped")},
                 indent=1))
EOF
```

How to read it:

- `proposals.reads`: first-party GET request lines, like `GET api.example.com/v1/notes ?cursor,limit`. The path comes first, then the query's parameter **names** (never their values).
- `shapes[line]`: up to 3 distinct shapes seen for that line.
  - Scalars are `"str"`, `"int"`, `"float"`, `"bool"`, `"null"`.
  - `["<list>", …]` is a list followed by its item shapes.
  - `{"<key>": …}` is an object whose keys are data (ids, emails, titles), so the keys aren't shown.
  - `["<one of>", …]` means several shapes were seen there.
  - `"<deep>"` means the value was nested too deep to record.
  - `{"dropped": "oversize"}` means the shape was too big to keep.
- `proposals.blocked`: requests the capture stopped. A `op=Name (query)` line is a GraphQL read the person may want. Its response wasn't recorded, so it has no shape.
- `proposals.hosts`: first-party hosts the pages read from. They aren't necessarily in the tool yet (a tool made before its capture has only the start host). Check the manifest's `hosts`. Add any host a command needs to both `hosts` and `endpoints.HOSTS`.
- A read with no shape returned no JSON (HTML, an image), wasn't a 200, or answered with a non-JSON prefix. Skip it unless the person needs it.

## 2. Pick the commands with the person

1. List the reads that have shapes, skipping assets, telemetry, and feature flags.
2. For each one, give a one-line guess at what it is (from the path and the shape's keys) and a command name: one or two lowercase words, like `notes` or `note show`.
3. Ask the person which to build. If you can't ask, build the ones that look like the person's own content (lists of items with titles, dates, ids) and say which you skipped.

Keep the scaffold's `status` command: sign-in uses it, because `fetch.login()` calls `status()` after the window closes. If there's a read like `/me` or `/user`, move `status` onto it instead of the password-field guess:

- `fetch.status()` uses `API_POLICY`, `_open()`, and `_get()` (below), not `_body()`. A signed-out answer (401 or 403) must return `{"signedIn": False, ...}`, not raise: `status` is a report (its command exits 4 itself when `signedIn` is false), and `login()` reads `signedIn` to say whether sign-in worked.
- Rewrite `parse.status_info()` in place to take that `{status, body}` result instead of a page snapshot: signed in means HTTP 200 with the user object the shape shows. Don't keep the old version beside it.
- Replace the scaffold's `test_a_password_field_means_signed_out` with a test of the new rule, and update the `status` wording in `cli.py` (its docstring and `NOTES` say "sign-in form").

Keep or remove `page`, whichever the person prefers. It stays on `POLICY`.

## 3. Write each command

Work in `./webcli-tool-NAME/`. Edit the files in this order so they stay in step.

**`webcli-tool.toml`:** one `[[commands]]` per command:

```toml
[[commands]]
name = "notes"
strategy = "cookie"
requests = ["GET www.example.com/", "GET api.example.com/v1/notes ?cursor,limit"]
```

List every request the command makes, including the start page the fetch runs from. Update `summary` here and in `ADAPTER`.

**`webcli_tool_NAME/__init__.py`:** add each op to `ops`, and each command to `verbs` as `Strategy.COOKIE`.

**`endpoints.py`:** one constant per API URL, and the query parameter names it uses.

**`guard.py`:** keep `POLICY` for the scaffold's `page` command (it needs the page's own scripts). Add a narrow `API_POLICY` for the new commands, and for `status` if it moves to an API read. It allows GET only, over https, to exact host and path pairs: the start page and each API path. Compare whole paths. Don't use `path_matches` here: it treats any path ending in `/` as a prefix, so `"/"` would allow the whole host.

```python
API_REQUESTS = {  # (host, path), exactly as in the manifest's requests
    ("www.example.com", "/"),
    ("api.example.com", "/v1/notes"),
}

def api_allowed(method: str, url: str) -> bool:
    parts = urlsplit(url)
    return method.upper() == "GET" and on_hosts(url) and (parts.hostname, parts.path) in API_REQUESTS

API_POLICY = Policy("NAME", lambda request, _state: api_allowed(request.method, request.url), _describe)
```

The start page's scripts get blocked under this policy. That's fine: the fetch below runs in the page anyway. A path with an id in it (`/v1/notes/<id>`) needs a pattern you write by hand: match the fixed part exactly and check that the id is one segment.

**`fetch.py`:** one async op per command. Each op takes `headed: bool = False` as a keyword and runs the site's own API from the signed-in page with `page.evaluate`. Never use `page.request` or `page.context.request`: they skip the guard.

```python
GET_JSON = """async (url) => {
  const res = await fetch(url, {credentials: 'include', headers: {accept: 'application/json'}});
  let body = null;
  try { body = await res.json(); } catch (e) {}
  return {status: res.status, body};
}"""

async def _open(page) -> None:
    """Load the signed-in start page once per op: the fetches run from it."""
    await page.goto(APP_URL, wait_until="domcontentloaded", timeout=60_000)

async def _get(page, url: str) -> dict:
    """{status, body} of a GET from the open start page."""
    return await page.evaluate(GET_JSON, url)

def _body(result: dict):
    if result["status"] in (401, 403):
        raise AuthRequired("NAME's session is missing or expired.")
    if result["status"] == 429:
        raise RateLimited("NAME is rate limiting this profile.")
    if result["status"] >= 400 or result["body"] is None:
        raise RuntimeError(f"NAME's API answered HTTP {result['status']}.")
    return result["body"]

async def notes(limit: int = 50, *, headed: bool = False) -> list[dict]:
    async with site_page(API_POLICY) as page:
        await _open(page)
        return parse.notes(_body(await _get(page, NOTES_URL)), limit)
```

Import `RateLimited` from `webcli.adapter`. Build query strings with `urllib.parse.urlencode`. If the shape shows a cursor (`nextCursor`, `next`, `cursor`), follow it until `limit`, and stop when a cursor repeats. Open the start page once, before the loop:

```python
rows, cursor, seen = [], None, set()
while len(rows) < limit and cursor not in seen:
    seen.add(cursor)
    query = urlencode({"limit": 50, **({"cursor": cursor} if cursor else {})})
    body = _body(await _get(page, f"{NOTES_URL}?{query}"))
    rows += parse.notes(body, limit - len(rows))
    cursor = body.get("nextCursor")
    if not cursor:
        break
```

Adding a host to `endpoints.HOSTS` also opens it to `POLICY` (GET and HEAD only), which the `page` command uses. That's expected: `page` stays read-only.

**`parse.py`:** pure functions from a body to rows. No browser, no network. Write them against the shape:

- Use `.get()` everywhere. Treat every field as possibly missing or `null`, even when the shape shows one type.
- Handle each `["<one of>", …]` variant. Under `{"<key>": …}`, iterate `.values()` (or `.items()` when the key is the id).
- Return flat rows of plain strings, numbers, and bools, with the fields an agent would want first.

**`cli.py`:** one click command per command, modeled on the scaffold's `page`:

- Use `@agent_output_options()`, `call(SITE, "op", …)`, and `emit_rows(…)`. Pass `emit_rows` the same arguments the scaffold's `page` does: `title`, `fields` (every row key, in order), `compact_fields` (the few that fit a narrow table), `columns`, `noun`, and the output options.
- Exit 3 (`die(…, EXIT_NOT_FOUND)`) when there are no rows.
- Add a line to `NOTES` describing the rows.

**`tests/`:** plain functions with no arguments. `webcli test` runs them with the network blocked.

- Put each command's fixture in `tests/fixtures/api/<command>.json`, not in `tests/fixtures/` itself: the scaffold's test feeds every `tests/fixtures/*.json` to the page parser. Build it from the shape with made-up values. Include one item with its optional fields missing or `null`.
- Assert the parser's exact output against a literal list of rows.
- Assert `api_allowed` allows each `(host, path)` in `API_REQUESTS`, and refuses a POST, a path you skipped (a read you didn't build), the same path on another host, and `http://`. Lint doesn't check this, so the test is what keeps the guard narrow.

## 4. Check, then hand off

Run both checks. They import only this folder, which you just wrote, and run offline:

```sh
webcli lint NAME
webcli test NAME
```

Fix until both pass with no errors. Then hand off to the person. Tell them the draft is waiting for a live check, and give them exactly what to run:

```sh
webcli identity login NAME:default     # once, if the site needs sign-in
webcli dev NAME -- status
webcli dev NAME -- notes --limit 3
```

Ask for each command's exit code and stderr, never its output. Then fix from what they report:

| They see | Likely cause |
|---|---|
| Exit 4 on `status` | Not signed in, or `status` checks the wrong thing |
| Exit 4 on a command, `status` passes | The API wants a header the site's scripts add (often a CSRF token). Ask which header, by name only |
| Exit 5 with "HTTP 404" | The path needs an id or a query value the capture only showed by name |
| Exit 5 with "Failed to fetch" | The guard blocked the request: add its host and path to `API_REQUESTS` and to the command's `requests`. Or the API is on another host that refuses the page's origin |
| Exit 3 | `parse.py` expects a key the real response doesn't have. Compare the shape again |

When their live check passes, `webcli submit NAME` shows what sharing would send (a dry run). The person decides whether to add `--confirm`.
