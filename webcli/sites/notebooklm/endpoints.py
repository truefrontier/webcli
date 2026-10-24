"""Every URL and rpcid webcli notebooklm touches, in one table. Fix this file first when a capture disagrees.

notebooklm.google.com (Google is moving it to notebook.google.com; both serve the same app and the same RPCs) is a
WIZ app that talks to two internal endpoints:

  batchexecute     POST /_/LabsTailwindUi/data/batchexecute?rpcids=<id>&source-path=/notebook/<id>&bl=..&f.sid=..
                   form body f.req=[[[rpcid, "<payload json>", null, "generic"]]] & at=<token>
  GenerateFreeFormStreamed
                   POST /_/LabsTailwindUi/data/google.internal.labs.tailwind.orchestration.v1.
                   LabsTailwindOrchestrationService/GenerateFreeFormStreamed
                   form body f.req=[null, "<params json>"] & at=<token>; the chat answer, streamed back in
                   batchexecute framing (one wrb.fr envelope per update, the last one marked final).

The `at` token (WIZ_global_data.SNlM0e), build label (cfb2h), and session id (FdrFJe) are read inside the page by
fetch.py's JS, exactly as gemini does; Python only ever sees response bodies. The guard reads each outgoing
batchexecute body in memory to check its rpcid, and never stores or prints it.

Source: the rpcids, payload shapes, and response index paths come from teng-lin/notebooklm-py (MIT; v0.8.5 on PyPI,
2026-10), the unofficial NotebookLM CLI whose `notebooklm login` signs in through a Playwright profile and then
calls these same RPCs. webcli re-implements them in its own style (in-page fetch, fail-closed guard); no code is
copied. See docs/notebooklm-site.md.
"""

from __future__ import annotations

import re

APP_HOSTS = ("notebooklm.google.com", "notebook.google.com")
APP_HOST = APP_HOSTS[0]
APP_URL = f"https://{APP_HOST}/"
NOTEBOOK_URL = f"https://{APP_HOST}/notebook/{{id}}"
RPC_PREFIX = "/_/LabsTailwindUi/data/"
BATCH_PATH = RPC_PREFIX + "batchexecute"
QUERY_PATH = (RPC_PREFIX + "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService/"
              "GenerateFreeFormStreamed")

# The rpcids webcli notebooklm itself calls, with the backend method each resolves to (notebooklm-py's registry).
RPC = {
    "notebooks": "wXbhsf",  # ListRecentlyViewedProjects [None, 1, None, [2]] -> [[notebook, ...]]
    "notebook": "rLM1Ne",  # GetProject [id, None, ctx, None, 0] -> [notebook] (title, sources, settings)
    "source": "hizoJc",  # LoadSource [[source_id], [2], [2]] -> source text blocks
    "source_guide": "tr032e",  # GenerateDocumentGuides [[[[source_id]]]] -> summary, keywords (response only)
    "notes": "cFji9",  # GetNotes [notebook_id] -> [[note, ...]] (mind maps are JSON-bodied notes)
    "artifacts": "gArtLc",  # ListArtifacts [[2], notebook_id, filter] -> [[artifact, ...]]
    "artifact": "v9rmvd",  # GetArtifact [artifact_id] -> [artifact] (quiz / flashcard HTML, mind-map tree)
    "create_artifact": "R7cb6c",  # CreateArtifact [ctx, notebook_id, descriptor] -> [artifact]
    "create_notebook": "CCqFvf",  # CreateProject [title, None, None, ctx] -> notebook row [title, _, id, ...]
    "add_source": "izAoDd",  # AddSources [[spec], notebook_id, ctx] -> [[[[id], title, meta, settings]]]
    "add_source_file": "o4cbdc",  # register a file source [[[filename]], notebook_id, ctx] -> its source id
}

# File sources: register (o4cbdc), then a resumable upload of the bytes on the app host itself: POST UPLOAD_PATH
# (?authuser=) with x-goog-upload-command: start and a JSON body {PROJECT_ID, SOURCE_NAME, SOURCE_ID}; its answer's
# x-goog-upload-url header (the same path, ?upload_id=...) then takes the bytes with "upload, finalize".
UPLOAD_PATH = "/upload/_/"

# Fail closed: a batchexecute POST passes only when its one rpcid is listed for the running command's policy.
READ_RPCIDS = frozenset({RPC["notebooks"], RPC["notebook"], RPC["source"], RPC["source_guide"], RPC["notes"],
                         RPC["artifacts"], RPC["artifact"]})
# `generate --confirm` only: one CreateArtifact. Nothing else that writes is ever allowed.
GENERATE_RPCIDS = READ_RPCIDS | {RPC["create_artifact"]}
# The only writes a `create` / `source add --confirm` page may be opened for (guard.write_policy, with a per-rpcid
# budget). Rename, delete, share, source edits, and notes are not here, so no policy can ever allow them.
WRITE_RPCIDS = frozenset({RPC["create_notebook"], RPC["add_source"], RPC["add_source_file"]})
UPLOAD = "upload"  # write_policy's budget key for upload POSTs (two per file: start, then upload+finalize)

# Known writes, for tests and for reading capture output. Outside a write_policy the guard blocks every one; a
# write_policy opens only its own WRITE_RPCIDS.
MUTATING_RPCIDS = {
    "CCqFvf": "create notebook", "te3DCe": "copy notebook", "s0tc2d": "rename notebook / sharing settings",
    "WWINqb": "delete notebook", "izAoDd": "add source", "o4cbdc": "register uploaded file", "tGMBJ": "delete source",
    "FLmJqe": "refresh source", "b7Wfje": "rename source", "X1snv": "add sources async", "QsNTEd": "append source",
    "R27wvc": "copy sources", "agX4Bc": "create label", "le8sX": "update label", "GyzE7e": "delete labels",
    "V5N4be": "delete artifact", "rc3d8d": "rename artifact", "Krh3pd": "export artifact to Drive",
    "RGP97b": "share audio", "KmcKPe": "revise slide", "Rytqqe": "retry artifact", "mKDdke": "copy artifacts",
    "Ljjv0c": "fast research", "QA9ei": "deep research", "LBwxtb": "import research", "Zbrupe": "cancel research",
    "yyryJe": "generate note mind map", "CYK0Xb": "create note", "cYAfTb": "edit note", "AH0mwd": "delete note",
    "J7Gthc": "delete chat history", "XgrPMd": "cancel generation", "QDyure": "share notebook",
    "fejl7e": "remove from recently viewed", "hT54vc": "account settings", "ZwVcOc": "get-or-create account",
}

# Studio artifact type codes (ListArtifacts row [2]); type 4 is told apart by its variant at row [9][1][0].
TYPE_AUDIO, TYPE_REPORT, TYPE_VIDEO, TYPE_APP, TYPE_MIND_MAP = 1, 2, 3, 4, 5
TYPE_INFOGRAPHIC, TYPE_SLIDE_DECK, TYPE_DATA_TABLE = 7, 8, 9
VARIANT_FLASHCARDS, VARIANT_QUIZ, VARIANT_MIND_MAP = 1, 2, 4
STATUS = {0: "unknown", 1: "pending", 2: "in_progress", 3: "completed", 4: "failed", 5: "suggested"}

# Hosts an artifact's media (audio, video, infographic, slide PDF) downloads from, hop by hop. Fetched with
# page.request, which the route guard never sees, so fetch.py checks every hop against this.
MEDIA_HOSTS = re.compile(r"[a-z0-9-]+\.googleusercontent\.com|[a-z0-9-]+\.usercontent\.google\.com|"
                         r"lh\d+\.google\.com|[a-z0-9-]+\.googlevideo\.com")
