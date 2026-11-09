"""The headless Chromium the page-script and DOM tests run in: Playwright's own, else any cached headless shell.

Never the user's Chrome by default. Set WEBCLI_TEST_CHROME_CHANNEL to a Playwright channel (`chrome`, `chrome-beta`,
`msedge`) to fall back to that installed browser: Playwright starts it headless with a throwaway profile, and the tests
only load local fixture pages. Without one of these, those tests skip.
"""

from __future__ import annotations

import os
from pathlib import Path


def launch_chromium(p):
    """The first browser that starts under sync Playwright `p`, or None (the caller skips)."""
    shells = sorted(Path.home().glob("Library/Caches/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    shells += sorted(Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/*/chrome-headless-shell"))
    options = [{}, *({"executable_path": str(exe)} for exe in reversed(shells))]
    if channel := os.environ.get("WEBCLI_TEST_CHROME_CHANNEL"):
        options.append({"channel": channel})
    for option in options:
        try:
            return p.chromium.launch(**option)
        except Exception:
            continue
    return None
