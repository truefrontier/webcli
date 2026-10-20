"""www.linkedin.com (webcli linkedin): limsg (linkedin-messages-cli) ported onto the shared core. Identity provider
"linkedin" (linkedin:default, its own sign-in: a person runs `webcli identity login linkedin:default`).

Read-only except `messages send` and `post create` (`feed` and `posts search|from|read` are reads): both are dry runs by default, and both refuse to write (exit 4)
unless the signed-in account's handle is the expected author (configured via expect_author; writes refuse until it is set), checked before the first
write and again right before the last one.
"""

from __future__ import annotations

from webcli.sites.base import Site, Strategy


def _cli():
    from webcli.sites.linkedin.cli import group

    return group


async def _login(timeout: int, on_state=lambda _state: None) -> dict:
    from webcli.sites.linkedin import fetch

    return await fetch.login(timeout, on_state)


ADAPTER = Site(
    name="linkedin",
    kind="data",
    provider="linkedin",
    summary="LinkedIn: messages list/history/read, activity, feed, posts search/from/read, boost stats, status; "
            "messages send and post create (dry run unless --confirm, and only as the expected author) (limsg parity)",
    cli=_cli,
    fetch="webcli.sites.linkedin.fetch",
    ops=("status", "conversations", "messages", "send", "activity", "export", "boosts", "boost_stats", "post", "feed",
         "posts_search", "posts_from", "post_read"),
    verbs={"messages list": Strategy.COOKIE, "messages history": Strategy.COOKIE, "messages read": Strategy.COOKIE,
           "messages send": Strategy.COOKIE, "activity comments": Strategy.UI, "activity reactions": Strategy.UI,
           "activity posts": Strategy.UI, "activity export": Strategy.UI, "boost list": Strategy.COOKIE,
           "boost stats": Strategy.COOKIE, "post create": Strategy.COOKIE, "status": Strategy.COOKIE,
           "feed": Strategy.COOKIE, "posts search": Strategy.UI, "posts from": Strategy.COOKIE,
           "posts read": Strategy.COOKIE},
    login=_login,
    status_op="status",
)
