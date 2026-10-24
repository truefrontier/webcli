# webcli notebooklm

`webcli notebooklm` reads your NotebookLM notebooks and creates Studio outputs, through the `google:default`
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
| `generate kinds` | Which kinds can be generated (faq and timeline report as unavailable) |
| `generate KIND NOTEBOOK ... --confirm [--wait]` | Create one Studio output |

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

Every other rpcid (source add/delete, notebook create/delete, artifact delete, note writes, sharing, ...) is blocked
and named in the error. Downloads go only to Google media hosts. Tokens (`SNlM0e`, `cfb2h`, `FdrFJe`) are read
inside the page and never reach Python, output, or logs.

## Verified

Live against Kevin's account on 2026-10-23: `status`, `notebooks`, `sources`, `source get` (text and `--guide`),
`artifacts`, `ask` (answer plus citations), `generate report --style study-guide --confirm --wait` (completed in
about 50 s), `download` of that report (markdown) and of an existing audio overview (m4a), and `status ARTIFACT -n`.

Tested against synthetic fixtures only: generating audio, video, quiz, flashcards, mind-map, infographic,
slide-deck, and data-table; downloading video, infographic, slides, quiz, flashcards, and mind map; `notes` with
notes present; and `status ARTIFACT` without `-n` (it searches every notebook).

## Proposed write commands (not built)

These would each need their own rpcid in a guard policy and a `--confirm` dry run:

- `notebook create|rename|delete|share` (`CCqFvf`, `s0tc2d`, `WWINqb`, `QDyure`)
- `source add|add-file|delete|refresh|rename` (`izAoDd`, `o4cbdc`, `tGMBJ`, `FLmJqe`, `b7Wfje`)
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
