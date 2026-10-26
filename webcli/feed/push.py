"""`webcli feed push`: HTTP PUT one feed file (the latest by default) to a URL. https only (http only to localhost).
A bearer token, when the server wants one, comes from an environment variable named by --token-env and is never
printed. On a tailnet behind Tailscale Serve no token is needed: Serve tells the server who you are."""

from __future__ import annotations

import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 5 * 1024 * 1024


def check_url(url: str) -> str:
    parts = urlsplit(url)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError("--to must be an https URL (http only for localhost).")
    if parts.username or parts.password:
        raise ValueError("--to must not carry credentials; use --token-env.")
    return url


def push(path: Path, url: str, token_env: str | None = None, timeout: float = 60) -> dict:
    body = path.read_bytes()
    if len(body) > MAX_BYTES:
        raise ValueError(f"{path.name} is {len(body)} bytes; the limit is {MAX_BYTES}.")
    headers = {"Content-Type": "application/json"}
    if token_env:
        token = os.environ.get(token_env, "").strip()
        if not token:
            raise ValueError(f"${token_env} is empty or unset.")
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(check_url(url), data=body, method="PUT", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return {"pushed": True, "status": response.status, "file": str(path), "bytes": len(body), "to": url}
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url} answered HTTP {exc.code}.") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Couldn't reach {url}: {exc.reason}") from None
