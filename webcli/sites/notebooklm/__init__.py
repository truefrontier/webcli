"""notebooklm.google.com (webcli notebooklm): notebooks, sources, notes, Studio outputs, ask with citations, and
generate / download for Studio outputs. A Google data site: runs as google:<account>, no login of its own.

Built on the RPCs documented by teng-lin/notebooklm-py (MIT), re-implemented in webcli's in-page, guarded style.
"""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.notebooklm.cli import group

    return group


ADAPTER = Site(
    name="notebooklm",
    kind="data",
    provider="google",
    summary="notebooklm.google.com: notebooks, sources, source text / guide, notes, ask (citations), Studio "
            "artifacts, generate (audio, video, reports, quiz, flashcards, mind map, infographic, slides, data "
            "table), download",
    cli=_cli,
    fetch="webcli.sites.notebooklm.fetch",
    ops=("status", "notebooks", "sources", "source", "notes", "artifacts", "artifact", "ask", "plan", "generate",
         "download"),
    verbs={"status": Strategy.COOKIE, "notebooks": Strategy.COOKIE, "sources": Strategy.COOKIE,
           "source": Strategy.COOKIE, "notes": Strategy.COOKIE, "artifacts": Strategy.COOKIE, "ask": Strategy.COOKIE,
           "generate": Strategy.COOKIE, "download": Strategy.COOKIE},
    status_op="status",
)
