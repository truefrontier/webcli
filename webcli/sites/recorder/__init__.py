"""recorder.google.com (webcli recorder): google-recorder-cli's data verbs ported onto the shared core. Identity
provider "google" (the google:default profile gemini's login signs in). Read-only. Its MCP server (`recorder mcp`,
`recorder-mcp`) and `recorder skill` are not ported."""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.recorder.cli import group

    return group


ADAPTER = Site(
    name="recorder",
    kind="data",
    provider="google",
    summary="Google Recorder (Pixel) recordings, read-only: list, info, search, transcript, download (audio), sync, "
            "status (google-recorder-cli parity, no MCP)",
    cli=_cli,
    fetch="webcli.sites.recorder.fetch",
    ops=("status", "recordings", "transcript", "download", "sync"),
    verbs={"list": Strategy.INTERCEPT, "info": Strategy.INTERCEPT, "search": Strategy.INTERCEPT,
           "transcript": Strategy.INTERCEPT, "download": Strategy.COOKIE, "sync": Strategy.INTERCEPT,
           "status": Strategy.COOKIE},
    status_op="status",
)
