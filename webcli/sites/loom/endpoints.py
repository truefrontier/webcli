"""Everything webcli loom knows about www.loom.com, and the guard's allowlists. Fix this file first when Loom moves
something.

Status tags:
  LOOM-CLI  - what loom-cli used and read live (its library list, video card, and share-page transcript).
  GUESS     - UNVERIFIED. Every guess fails closed: a wrong path is blocked and named in the error.
"""

from __future__ import annotations

from pathlib import Path

ORIGIN = "https://www.loom.com"
APP_HOST = "www.loom.com"
APP_URL = ORIGIN + "/looms/videos"  # LOOM-CLI: the library; signed out it redirects to /login
LOGIN_URL = ORIGIN + "/login"  # LOOM-CLI
SHARE_URL = ORIGIN + "/share/{id}"  # LOOM-CLI: the share page requests the official transcript itself
GRAPHQL_URL = ORIGIN + "/graphql"  # LOOM-CLI: POST {operationName, query, variables}
GRAPHQL_PATHS = frozenset({"/graphql", "/graphql/"})
QUERIES = Path(__file__).with_name("queries")  # LOOM-CLI: GetLoomsForLibrary, GetVideoCardDetails, as captured
# LOOM-CLI: the headers Loom's own web client sends with a GraphQL call.
GRAPHQL_HEADERS = {"content-type": "application/json", "accept": "application/json",
                   "apollographql-client-name": "web", "x-loom-request-source": "loom_web"}

# Hosts every command may GET: Loom's app, API, and CDN (cdn.loom.com serves the transcript file). GUESS for any
# Loom host past www.loom.com; an off-list host is blocked and named.
HOSTS = ("loom.com", "*.loom.com")
# GETs with side effects (GUESS): sign-out. Aborted even on a listed host.
SIDE_EFFECT_GETS = (("www.loom.com", "/logout"), ("www.loom.com", "/api/logout"))
# Sign-in: a landing URL holding one of these means signed out (LOOM-CLI).
SIGNED_OUT_MARKS = ("login", "signup")
# A tab past sign-in shows one of these (LOOM-CLI, minus /share/: a public share page loads signed out). Login also
# waits for the library query to answer as signed in: a tab can get here before Loom has finished signing in.
SIGNED_IN_PATHS = ("/looms", "/library", "/settings")

TRANSCRIPT_MARK = "mediametadata/transcription/"  # LOOM-CLI: the phrase file's URL holds this and the video id
TRANSCRIPT_SECONDS = 25  # LOOM-CLI
