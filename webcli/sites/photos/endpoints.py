"""Every URL and rpcid webcli photos touches. Fix this file first when a capture disagrees.

photos.google.com is a Google WIZ app: it renders the library grid, albums, search results, and a photo's page from
batchexecute RPCs (POST /_/PhotosUi/data/batchexecute?rpcids=<id>, form body f.req=[[[rpcid, "<payload>", null,
"generic"]]] & at=<token>). webcli photos never calls them itself: it reads what the page renders (UI), the way
google-photos-cli did. The guard lets a batchexecute through only when every rpcid in it is a read listed here.

`download` gets the original the way google-photos-cli did: the photo's page loads its media from
photos.fife.usercontent.google.com/pw/<base>; <base>=dv (video original) or <base>=d (still original) answers the
file. Those GETs go through page.request, which the route guard never sees, so guard.download_url_allowed checks
every hop.

Status: the rpcids come from Google Photos Toolkit (xob0t/Google-Photos-Toolkit, src/api/api.js), not verified live
with webcli. Hosts were seen in the old google-photos-cli profile's network state (2026-10-19). Run
`python -m webcli.core.capture photos` after `webcli identity login google:default` to see what the guard blocked,
and add an rpcid to READ_RPCIDS only once you know it is a read.
"""

from __future__ import annotations

import re

ORIGIN = "https://photos.google.com"
APP_HOST = "photos.google.com"
APP_URL = ORIGIN + "/"
ALBUMS_URL = ORIGIN + "/albums"
ALBUM_URL = ORIGIN + "/album/{id}"
PHOTO_URL = ORIGIN + "/photo/{id}"
SEARCH_URL = ORIGIN + "/search/{query}"
MEDIA_HOST = "photos.fife.usercontent.google.com"
HOSTS = (APP_HOST, MEDIA_HOST)
BATCH_PATH = re.compile(r"/_/[A-Za-z]+/data/batchexecute")
MEDIA_BASE = re.compile(r"https://photos\.fife\.usercontent\.google\.com/pw/[^?=]+")
# Where an original may come from, hop by hop: the media host, or a googleusercontent.com host it redirects to.
DOWNLOAD_HOSTS = re.compile(r"photos\.fife\.usercontent\.google\.com|[a-z0-9-]+\.googleusercontent\.com")

# Fail closed: a batchexecute POST passes only when EVERY rpcid in it is listed here.
READ_RPCIDS = frozenset({
    "lcxiM",  # library items by taken date: the main grid
    "EzkLib",  # items by upload date, favorites, and search results
    "Z5xsfc",  # albums
    "snAcKc",  # an album's items
    "VrseUb",  # one item's info
    "fDcn4b",  # one item's extended info
    "EWgK9e",  # several items' info
    "EzwWhf",  # storage quota
    "F2A0H",  # shared links
    "swbisb",  # items matching a file hash
})

# Known writes, for tests and for reading captures. The guard blocks them because they are not in READ_RPCIDS.
MUTATING_RPCIDS = {
    "XwAOJf": "move to trash / restore",
    "ycV3Nd": "remove from album",
    "OXvT9d": "create album",
    "E1Cajb": "add to album",
    "laUYf": "add to shared album",
    "QD9nKf": "reorder album",
    "Ftfh0": "favorite",
    "w7TP3c": "archive",
    "StLnCe": "move to locked folder",
    "Pp2Xxe": "remove from locked folder",
    "LjmOue": "remove from shared album",
    "V8RKJ": "save shared media",
    "Es7fke": "save partner media",
    "EtUHOe": "set or delete location",
    "DaSgWe": "set date",
    "AQNOFd": "set description",
    "pLFTfd": "create a download link",
    "yCLA7": "create a download token",
}

RENDER_MS = 5_000  # after load: the grid's first tiles
INFO_MS = 4_000  # a photo's page
SCROLL_PX, SCROLL_MS = 2_400, 800
MAX_SCROLLS = 60
STALL_SCROLLS = 3  # scrolls without a new tile: the end of the grid
PLAY_MS = 1_500  # after Space on a video: its media URL loads
