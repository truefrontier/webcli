"""Every URL webcli keep touches. Fix this file first when a capture disagrees.

keep.google.com syncs notes with the same JSON API Keep's apps use: POST .../notes/v1/changes, body {nodes: [local
edits to upload], targetVersion, clientTimestamp, requestHeader, userInfo?: {labels}}, answer {nodes: [...],
toVersion, truncated}. webcli keep reads the page's own answers (INTERCEPT) and never calls it itself. A changes POST
also UPLOADS edits (nodes) and label changes (userInfo), so the guard lets one through only when it carries
neither: a read-only sync.

Hosts (captured live, 2026-10-19): keep.google.com, and the API on notes-pa.clients6.google.com. Keep's gapi client
sends the changes POST from its proxy iframe, GET notes-pa.clients6.google.com/static/proxy.html; with that GET
blocked, no sync ever fires. Everything else the page asks for (getFamilyInfo, people-pa's proxy, drive.google.com/auth_warmup,
waa-pa, ogads-pa, signaler-pa, telemetry, CSP reports) stays blocked; the sync doesn't need it. Run
`python -m webcli.core.capture keep` to see what the guard blocked.
"""

from __future__ import annotations

APP_HOST = "keep.google.com"
APP_URL = "https://keep.google.com/u/0/"
HOSTS = (APP_HOST,)
API_HOST = "notes-pa.clients6.google.com"
CHANGES_PATH = "/notes/v1/changes"
PROXY_PATH = "/static/proxy.html"  # gapi's iframe on API_HOST, which sends the changes POST
SYNC_SECONDS = 25  # Keep's first sync burst ends with truncated: false
SETTLE_MS = 3_000  # then trailing pages
