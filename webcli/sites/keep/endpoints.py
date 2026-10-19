"""Every URL webcli keep touches. Fix this file first when a capture disagrees.

keep.google.com syncs notes with the same JSON API Keep's apps use: POST .../notes/v1/changes, body {nodes: [local
edits to upload], targetVersion, clientTimestamp, requestHeader, userInfo?: {labels}}, answer {nodes: [...],
toVersion, truncated}. webcli keep reads the page's own answers (INTERCEPT) and never calls it itself. A changes POST
also UPLOADS edits (nodes) and label changes (userInfo), so the guard lets one through only when it carries
neither: a read-only sync.

Hosts: keep.google.com, and the API on www.googleapis.com or notes-pa.clients6.google.com (both seen in the old
keep-cli profile's network state, 2026-10-19; which one carries changes is not verified live with webcli). Run
`python -m webcli.core.capture keep` after `webcli identity login google:default` to see what the guard blocked.
"""

from __future__ import annotations

APP_HOST = "keep.google.com"
APP_URL = "https://keep.google.com/u/0/"
HOSTS = (APP_HOST,)
CHANGES_HOSTS = frozenset({"www.googleapis.com", "notes-pa.clients6.google.com"})
CHANGES_SUFFIX = "/notes/v1/changes"
SYNC_SECONDS = 25  # Keep's first sync burst ends with truncated: false
SETTLE_MS = 3_000  # then trailing pages
