"""The Artificial Analysis Free Data API (https://artificialanalysis.ai/api/v2) over plain HTTPS: GET with an
x-api-key header, no browser. Responses are cached 6h in ~/.webcli/sites/aa/cache (--refresh skips the cache).

The key: $ARTIFICIAL_ANALYSIS_API_KEY, else ~/.webcli/sites/aa/api_key (0600, `webcli aa config set-key`). It only
ever travels in the request header: never in a URL, an error, or a log line.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from webcli import __version__
from webcli.core import config
from webcli.core.errors import ConfigError, RateLimited
from webcli.core.output import write_atomic

BASE_URL = "https://artificialanalysis.ai/api/v2"
LLM_ENDPOINT = "/language/models/free"
TTS_ENDPOINT = "/data/media/text-to-speech"
KEY_ENV = "ARTIFICIAL_ANALYSIS_API_KEY"
CACHE_TTL = 6 * 3600
TIMEOUT = 30
MAX_PAGES = 50  # ponytail: a runaway has_more stops here; the free API returns one page today
RESET_TZ = ZoneInfo("America/Chicago")  # resets_at, like every site's
SET_KEY_HINT = f"Set ${KEY_ENV}, or run `webcli aa config set-key` (a free key: https://artificialanalysis.ai/)."


class APIError(RuntimeError):
    """The API replied with an error, or the network failed (exit 5)."""


def key_path() -> Path:
    return config.home() / "sites" / "aa" / "api_key"


def api_key() -> str:
    """The configured key; a ConfigError (exit 7) when there is none."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key and key_path().is_file():
        key = key_path().read_text(encoding="utf-8").strip()
    if not key:
        raise ConfigError("No Artificial Analysis API key configured.", hint=SET_KEY_HINT)
    return key


def save_key(key: str) -> Path:
    key = key.strip()
    if not key:
        raise ConfigError("The API key is empty.", hint=SET_KEY_HINT)
    path = config.site_dir("aa") / "api_key"
    write_atomic(path, key + "\n")  # 0600 via mkstemp, renamed into place
    return path


def resets_at(retry_after: str | None, now: datetime | None = None) -> str | None:
    """A Retry-After header (seconds, or an HTTP date) as ISO 8601 in America/Chicago; None when absent or odd."""
    value = (retry_after or "").strip()
    try:
        when = ((now or datetime.now(RESET_TZ)) + timedelta(seconds=int(value)) if value.isdigit()
                else parsedate_to_datetime(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return when.astimezone(RESET_TZ).isoformat(timespec="seconds")


def _cache_file(endpoint: str, params: dict[str, Any]) -> Path:
    name = endpoint.strip("/").replace("/", "_") + "".join(f"_{k}-{v}" for k, v in sorted(params.items()))
    return config.home() / "sites" / "aa" / "cache" / f"{name}.json"


def get(endpoint: str, params: dict[str, Any] | None = None, *, refresh: bool = False) -> dict:
    """GET endpoint?params as JSON, from the cache when it is fresh and not `refresh`."""
    params = params or {}
    cache = _cache_file(endpoint, params)
    if not refresh and cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_TTL:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass  # a torn or corrupt cache file: fetch again
    url = BASE_URL + endpoint + (f"?{urlencode(params)}" if params else "")
    request = urllib.request.Request(url, headers={"x-api-key": api_key(), "Accept": "application/json",
                                                   "User-Agent": f"webcli/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ConfigError(f"Artificial Analysis rejected the API key (HTTP {exc.code}).",
                              hint=SET_KEY_HINT) from None
        if exc.code == 429:
            raise RateLimited("Artificial Analysis rate limit reached (HTTP 429).", sent=False,
                              resets_at=resets_at(exc.headers.get("Retry-After") if exc.headers else None)) from None
        raise APIError(f"Artificial Analysis replied HTTP {exc.code} for {endpoint}.") from None
    except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts, resets, a cut-off body
        raise APIError(f"Couldn't reach artificialanalysis.ai: {getattr(exc, 'reason', exc)}") from None
    try:
        data = json.loads(body)
    except ValueError:
        raise APIError(f"Artificial Analysis sent a reply that isn't JSON for {endpoint}.") from None
    if not isinstance(data, dict):
        raise APIError(f"Artificial Analysis sent an unexpected reply for {endpoint}.")
    config.private_dir(cache.parent)
    write_atomic(cache, json.dumps(data))
    return data


def llm_models(*, refresh: bool = False) -> list[dict]:
    """Every LLM row across pages (the API has used both top-level and meta.pagination)."""
    models: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        response = get(LLM_ENDPOINT, {"page": page}, refresh=refresh)
        rows = response.get("data") or []
        if not rows:
            break
        models.extend(rows)
        pagination = response.get("pagination") or (response.get("meta") or {}).get("pagination") or {}
        if not (pagination.get("has_more") or pagination.get("has_next")):
            break
    return models


def tts_models(*, refresh: bool = False) -> list[dict]:
    return get(TTS_ENDPOINT, refresh=refresh).get("data") or []
