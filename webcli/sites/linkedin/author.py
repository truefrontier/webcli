"""The author check every LinkedIn write passes (limsg's, kept exactly): createMessage and normShares name no author,
so a write goes out as whichever account the Chrome profile is signed in to. A write happens only when that account's
handle (linkedin.com/in/<handle>) equals the expected author, ignoring case, checked before the first write and again
right before the last one (same handle, profile urn, and member urn).

The expected author: --expect-author, then $WEBCLI_LINKEDIN_EXPECT_AUTHOR, then expect_author in config.toml's
[sites.linkedin], then kevnk.
"""

from __future__ import annotations

import os
from typing import Any

from webcli.core import config
from webcli.sites.linkedin.endpoints import DEFAULT_EXPECT_AUTHOR, EXPECT_AUTHOR_ENV


def expected(flag: str | None = None) -> str:
    """The handle writes must come from. ValueError for an empty or non-string value (exit 2)."""
    value: Any = flag if flag is not None else os.environ.get(EXPECT_AUTHOR_ENV)
    if value is None:
        sites = config.load().get("sites")
        site = sites.get("linkedin") if isinstance(sites, dict) else None
        value = site.get("expect_author") if isinstance(site, dict) else None
    if value is None:
        value = DEFAULT_EXPECT_AUTHOR
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"The expected author must be a LinkedIn handle (the part after linkedin.com/in/), got "
                         f"{value!r}.")
    return value.strip()


def matches(author: dict[str, Any], expect: str) -> bool:
    return str(author.get("publicIdentifier") or "").casefold() == expect.casefold()


def same_account(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """The same signed-in account: handle (ignoring case), profile urn, and member urn all equal."""
    def key(author: dict[str, Any]) -> tuple:
        return str(author.get("publicIdentifier") or "").casefold(), author.get("profileUrn"), author.get("memberUrn")

    return key(a) == key(b)


def who(author: dict[str, Any]) -> str:
    return f"{author.get('name') or '(no name)'} ({author.get('publicIdentifier')}, {author.get('memberUrn')})"


def mismatch(author: dict[str, Any], expect: str) -> str:
    return (f"Signed in as {who(author)}, but the expected author is {expect}. A person signs in as {expect}: "
            "webcli identity login linkedin:default.")
