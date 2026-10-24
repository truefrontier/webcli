# webcli notebooklm

`webcli notebooklm` reads your NotebookLM notebooks, creates notebooks and adds sources, and creates Studio outputs, through the `google:default`
identity's hidden Chrome. Same sign-in as Gemini, Keep, Photos, and the other Google sites:
`webcli identity login google:default`.

## Credit

The wire format (batchexecute RPC ids, request parameter layouts, artifact type codes, and the streamed chat
endpoint) was learned from [teng-lin/notebooklm-py](https://github.com/teng-lin/notebooklm-py) (v0.8.5, MIT
License, Copyright (c) Teng Lin), the unofficial NotebookLM CLI that drives the same signed-in web session through
Playwright. webcli re-implements it on webcli's core (identities, the session lock, the fail-closed request guard,
the identity daemon, exit codes); no code was copied.

## Commands

All of them take `--json` (same as `--format json`) and webcli's usual `--format/--quiet/--compact/--select/--csv`.
`NOTEBOOK`, `SOURCE`, and `ARTIFACT` accept an id or a notebooklm.google.com URL.

| Command | What it does |
|---|---|
| `status [ARTIFACT] [-n NOTEBOOK]` | Signed-in account and API check; with an id, that artifact's status |
| `notebooks` | Every notebook: id, title, emoji, source count, updated |
| `sources NOTEBOOK` | A notebook's sources: id, title, type, status |
| `source get SOURCE [-n NOTEBOOK] [--guide] [-o PATH]` | A source's full indexed text, or (`--guide`) its summary and keywords |
| `notes NOTEBOOK` | Saved notes |
| `artifacts NOTEBOOK` | Studio outputs, newest first: id, kind, title, status, created |
| `ask NOTEBOOK "question" [--source S]` | NotebookLM's answer, with citations (source id, title, quoted passage). NOTEBOOK may also be the notebook's exact title |
| `download ARTIFACT -o PATH [-n NOTEBOOK]` | Save an output: audio `.m4a`, video `.mp4`, infographic `.png`, slides `.pdf`, report `.md`, quiz/flashcards/mind map/data table `.json`. A path ending in `/` is a directory |
| `create TITLE [SOURCES] --confirm [--wait]` | Create a notebook (optionally adding sources); prints its id and URL. Dry run (exit 2) without `--confirm` |
| `source add NOTEBOOK SOURCES --confirm [--wait]` | Add files, URLs, or pasted text; prints each source's id and status. Dry run (exit 2) without `--confirm` |
| `generate kinds` | Which kinds can be generated (faq and timeline report as unavailable) |
| `generate KIND NOTEBOOK ... --confirm [--wait]` | Create one Studio output |

### create and source add

```bash
webcli notebooklm create "Q3 research"                                   # dry run: prints the plan, exits 2
webcli notebooklm create "Q3 research" --confirm --json                  # {"notebook_id", "title", "url", "sources": []}
webcli notebooklm create "Q3 research" --file notes.md --url https://example.com/a --confirm --wait
webcli notebooklm source add NOTEBOOK --file a.md --file b.txt --file paper.pdf --confirm --wait --json
webcli notebooklm source add NOTEBOOK --url https://example.com/a --url https://youtu.be/VIDEO --confirm
webcli notebooklm source add NOTEBOOK --urls-file reading-list.txt --confirm   # one URL per line; # comments
pbpaste | webcli notebooklm source add NOTEBOOK --text - --title "Meeting notes" --confirm
```

SOURCES are any mix of `--file PATH` (repeatable; `.md`, `.markdown`, `.txt`, `.pdf`, 1 byte to 50 MB),
`--url URL` (repeatable; web pages, and YouTube links as video sources), `--urls-file FILE` (`-` reads stdin), and
`--text TEXT --title T` (`--text -` reads stdin). Everything is checked before a browser opens (missing file,
unsupported type, non-http URL, `--text` without `--title`: exit 2); duplicate URLs are dropped. Each source is
sent once, in the order files, URLs, text; one that fails is reported with its error and the rest still go.

Without `--confirm` (or with `--dry-run`) nothing is written and the command exits 2: `create` prints the title and
the planned calls without opening a browser; `source add` also checks the notebook (read-only) and shows its title
and source count. With `--confirm`, the output has `notebook_id`, `url`, and per source `kind`, `input`, `id`,
`title`, `type`, `status` (`processing`, `preparing`, `ready`, `error`, or `failed` with `error` when the add itself
failed). `--wait` polls the notebook's sources every `--interval` seconds (default 5) until each is `ready` or
`error`, up to `--timeout` (default 600). Exit 0 when everything was added (and, with `--wait`, is ready); exit 5
when any source failed, errored, or the wait timed out.

Files upload the way the NotebookLM web app does: `o4cbdc` registers the file name and returns a source id, then
two POSTs to `notebooklm.google.com/upload/_/` (a resumable start, then upload-and-finalize) carry the bytes. Both
run inside the page with the page's cookies; the one-time upload URL is checked (same origin, same path, one
`upload_id`) and never leaves the page.

### generate

Every kind takes `--source` (repeatable), `--language` (default `en`), `--instructions`, `--wait`,
`--timeout` (1800 s), `--interval` (15 s), and `--confirm`/`--yes`. Without `--confirm` it is a dry run: it checks
the notebook, prints `{"dryRun": true, ...}` with the exact plan, creates nothing, and exits 2. `--wait` polls until
the output is completed (exit 0) or failed/timed out (exit 5).

| Kind | Options |
|---|---|
| `audio` | `--style deep-dive\|brief\|critique\|debate`, `--length short\|default\|long` |
| `video` | `--style explainer\|brief\|cinematic\|short`, `--visual auto\|custom\|classic\|whiteboard\|heritage\|paper-craft\|watercolor\|anime\|retro-print\|kawaii`, `--visual-prompt` |
| `report` | `--style briefing-doc\|study-guide\|blog-post\|custom` |
| `quiz`, `flashcards` | `--quantity fewer\|standard\|more`, `--difficulty easy\|medium\|hard` |
| `mind-map` | none |
| `infographic` | `--orientation landscape\|portrait\|square`, `--detail concise\|standard\|detailed`, `--visual auto\|sketch-note\|professional\|bento-grid\|editorial\|instructional\|bricks\|clay\|anime\|kawaii\|scientific` |
| `slide-deck` | `--style detailed\|presenter`, `--length default\|short` |
| `data-table` | none |

FAQ and timeline are not offered: NotebookLM's current Studio has no RPC for them.

## Guard

The request guard fails closed with three policies, all named `notebooklm`:

- **READ** (every read): batchexecute calls only, each carrying exactly one read rpcid that matches its URL label
  (`wXbhsf` notebooks, `rLM1Ne` notebook, `hizoJc` source, `tr032e` source guide, `cFji9` notes, `gArtLc`
  artifacts, `v9rmvd` artifact).
- **ASK**: READ plus the `GenerateFreeFormStreamed` chat endpoint.
- **GENERATE**: READ plus `R7cb6c` (CreateArtifact), used only by `generate --confirm`, once per call.
- **Write** (`create --confirm`, `source add --confirm`): READ plus a budget counted from the command line: one
  `CCqFvf` (CreateProject) for `create`, one `izAoDd` (AddSources) per URL or text, one `o4cbdc` (register file)
  per file, and two POSTs to `/upload/_/` on the app host per file. A call past its budget is blocked, so a second
  notebook, a stray add, or an extra upload can't happen.

Every other rpcid (notebook rename/delete/share, source delete/rename/refresh, artifact delete, note writes, sharing, ...) is blocked
and named in the error. Downloads go only to Google media hosts. Tokens (`SNlM0e`, `cfb2h`, `FdrFJe`) are read
inside the page and never reach Python, output, or logs.

## Verified

Live against Kevin's account on 2026-10-23: `status`, `notebooks`, `sources`, `source get` (text and `--guide`),
`artifacts`, `ask` (answer plus citations), `generate report --style study-guide --confirm --wait` (completed in
about 50 s), `download` of that report (markdown) and of an existing audio overview (m4a), and `status ARTIFACT -n`.

Live on 2026-10-24 (0.16.0): `create "webcli test" --confirm --json` with no sources made one empty notebook
(`CCqFvf` reply read correctly; `notebooks` lists it, `sources` shows none), and `source add` dry runs for
`--file`, `--url`, and `--text` (plan printed, exit 2, nothing added). Not yet run live: `source add --confirm`
(URL, text, or file upload) and `create` with sources; see "To confirm live" below.

Tested against synthetic fixtures only: generating audio, video, quiz, flashcards, mind-map, infographic,
slide-deck, and data-table; downloading video, infographic, slides, quiz, flashcards, and mind map; `notes` with
notes present; and `status ARTIFACT` without `-n` (it searches every notebook).

### To confirm live (create / source add)

- `authuser` on the upload POSTs: webcli sends the page's `authuser` if the URL has one, else the signed-in email
  (as notebooklm-py does), else `0`.
- That Chrome lets the in-page fetch set the `x-goog-upload-*` headers and read the `x-goog-upload-url` response
  header (same origin, so it should).
- Response nesting of `izAoDd` (source row, wrapped once or twice),
  and `o4cbdc` (exactly one source id); the readers accept each variant.
- That `text/markdown` is accepted as the upload content type for `.md` (notebooklm-py sends it).
- What a quota limit looks like on `create` (probably an RPC error code). (`CCqFvf` itself is verified.)

## Proposed write commands (not built)

These would each need their own rpcid in a guard policy and a `--confirm` dry run:

- `notebook rename|delete|share` (`s0tc2d`, `WWINqb`, `QDyure`)
- `source delete|refresh|rename` (`tGMBJ`, `FLmJqe`, `b7Wfje`)
- `note create|edit|delete` (`CYK0Xb`, `cYAfTb`, `AH0mwd`)
- `artifact delete|rename|export|retry|revise-slide` (`V5N4be`, `rc3d8d`, `Krh3pd`, `Rytqqe`, `KmcKPe`)
- `research` (fast/deep web research into sources: `Ljjv0c`, `QA9ei`, `LBwxtb`)
- `chat delete` (`J7Gthc`)

## llmcli notebooklm (the ask, as an llmcli site)

`llmcli notebooklm` is the same site's chat surface, shaped like `llmcli gemini` / `chatgpt` / `muse`, so agents and
tmuxlet (`--target notebooklm --notebook NB`) can use it as a chat backend:

```sh
llmcli notebooklm ask -n NOTEBOOK "question" [--source S]... [--chat ID] [--json]
llmcli notebooklm -p -n NOTEBOOK [PROMPT...] [-c | --chat ID] [--prompt-file F] [--output-file F] [--output-format json]
llmcli notebooklm notebooks | status | models      # models is always []
```

- `-n/--notebook`: an id, a notebook URL, or the exact title (case-insensitive when nothing matches exactly; two
  notebooks with the title exit 2 naming their ids). Env `LLMCLI_NOTEBOOKLM_NOTEBOOK` is the default.
- The JSON is the uniform llm ask object (`answer`, `chatId` = the conversation id, null model fields, `images: []`,
  `canvas: null`) with NotebookLM's citations (`number`, `title`, `url: null`, `source_id`, `source_title`,
  `excerpt`) and `notebook_id`, `notebook`, `question`, `conversation_id`.
- Conversations: a question continues the notebook's current conversation, as in the app. `--chat ID` sends the
  conversation id (GenerateFreeFormStreamed param 4) explicitly; `-p -c` reuses the last ask's notebook and
  conversation (`~/.webcli/sites/notebooklm/<identity>/last_chat`).
- Scope: it runs only the `status`, `notebooks`, and `ask` fetch ops (`chat.OPS`, checked before every call), and
  `ask` runs under the ASK guard policy (read rpcids plus the chat endpoint). No generate, download, or edits.
