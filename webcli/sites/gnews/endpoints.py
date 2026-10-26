"""news.google.com (webcli gnews): what it touches. Recon 2026-10-26 on google:default, in a hidden Chrome with a
GET-only guard: /foryou answers 200 signed in, with no challenge, and renders ~30 stories server-side (./read/<token>
links, <time datetime>, publisher, "By" line). The page's one POST on load (batchexecute rpcids XaDMu) and its
logging are aborted and the stories still render, so webcli reads the rendered page and sends no RPC at all.

Sections are the page's own nav links (./topics/<id>), read off For you and opened with a GET.
"""

from __future__ import annotations

APP_HOST = "news.google.com"
ORIGIN = f"https://{APP_HOST}"
FORYOU_URL = f"{ORIGIN}/foryou"
PUBLIC_URL = f"{ORIGIN}/"  # signed out: the public home (top stories) stands in for For you
HOSTS = (APP_HOST,)

# --section -> the nav link's label on the page (foryou: the For you page itself).
SECTIONS = {"foryou": None, "us": "U.S.", "world": "World", "local": "Local", "business": "Business",
            "technology": "Technology", "entertainment": "Entertainment", "sports": "Sports", "science": "Science",
            "health": "Health"}

DEFAULT_LIMIT = 30
MAX_LIMIT = 60  # what one page renders without scrolling (scrolling would need the page's RPCs)
RENDER_WAIT_MS = 2500
