"""Redaction: no access token, cookie, session id, sentinel token, device id, or signed download URL survives.

The superset of cgpt's and gweb's redact.py. Every log line, every error webcli/llmcli prints, and every capture or
probe output goes through redact_text / redact_url / redact_headers / shape. Over-matching is fine here ("author",
"keywords", "inside"): a lost sample beats a leaked secret.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "<redacted>"
# ChatGPT: the NextAuth session cookie (__Secure-next-auth.session-token[.0/.1]), the bearer accessToken from
# /api/auth/session, oai-did (device id), cf_clearance/__cf_bm (Cloudflare), _puid, the sentinel/proof/turnstile
# tokens (openai-sentinel-*), and Azure SAS parameters on signed download links (se, sp, sv, sr, st, skoid, sig...).
# Google: `at` is the XSRF token (WIZ_global_data.SNlM0e), f.sid the session id (FdrFJe), and SID/HSID/SAPISID/
# __Secure-1PSID(TS) are the session cookies.
SENSITIVE_NAME = re.compile(
    r"cookie|auth|key|token|secret|pass|session|csrf|xsrf|sig|otp|credential|bearer|sentinel|proof|turnstile"
    r"|clearance|device|did$|^did|puid|^__cf|cfuv|account.?id|^s[eptvr]$|^sk[a-z]{1,4}$|^rsc[a-z]$|^ts$|^p$"
    r"|sid|snlm0e|fdrfje|^at$",
    re.I,
)
# Query parameters that make a link a signed grant (Azure SAS, estuary): when one is present, every value goes.
SIGNED_PARAMS = frozenset({"sig", "se", "skoid", "sktid", "skt", "ske", "sks", "skv", "sp", "sv", "sr", "st"})
_JWT = re.compile(r"^eyJ[\w-]+\.[\w-]*(?:\.[\w-]*)+$")  # JWS (3 parts) and JWE (5 parts, the session cookie)
_HEX = re.compile(r"^[A-Fa-f0-9]{24,}$")
_LONG_NUMBER = re.compile(r"^-?\d{16,}$")  # f.sid (FdrFJe) is a signed 19-digit number
_OPAQUE = re.compile(r"^[A-Za-z0-9_\-+/=.:%~]{20,}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_ISO_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
SAMPLE_CHARS = 40


def urlish(value: str) -> bool:
    """Absolute (any scheme, any case), protocol-relative, or root-relative URL."""
    return bool(_SCHEME.match(value)) or value.startswith("/")


def tokenish(value: str) -> bool:
    """JWTs, long hex, long numbers, and long opaque strings that mix letters and digits.

    A bare UUID is an id (chat, message), not a secret, so it stays: device and account ids are UUIDs too, but
    they only travel under names (oai-did, oai-device-id, chatgpt-account-id) that SENSITIVE_NAME covers. An ISO 8601
    time (resets_at, a log line's stamp) is no secret either."""
    value = value.strip()
    if _UUID.match(value) or _ISO_TIME.match(value):
        return False
    if _JWT.match(value) or _HEX.match(value) or _LONG_NUMBER.match(value):
        return True
    return bool(_OPAQUE.match(value)) and any(c.isdigit() for c in value) and any(c.isalpha() for c in value)


# Hosts whose links are signed grants in the path or query: keep only the host. ChatGPT's file CDN and Azure blobs;
# Google's signed image links (lh3 /gg/, /gg-dl/, /rd-gg-dl/...).
_SIGNED_HOST = re.compile(
    r"(?:[\w-]+\.)*(?:oaiusercontent\.com|blob\.core\.windows\.net|googleusercontent\.com|usercontent\.google\.com"
    r"|ggpht\.com)|lh\d+\.google\.com",
    re.I,
)


def redact_url(url: str) -> str:
    """Keep scheme/host/path shape; redact sensitive or token-looking query values and path segments. A signed
    link (a SIGNED_PARAMS parameter, or a host in _SIGNED_HOST) keeps no query values; the latter only its host."""
    parts = urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    if _SIGNED_HOST.fullmatch(parts.hostname or ""):
        return f"{parts.scheme}://{host}/{REDACTED}" if parts.scheme else f"//{host}/{REDACTED}"
    netloc = f"{REDACTED}@{host}" if "@" in parts.netloc else parts.netloc
    path = "/".join(REDACTED if tokenish(seg) or seg.lower().startswith("eyj") else seg
                    for seg in parts.path.split("/"))
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    signed = any(k.lower() in SIGNED_PARAMS for k, _ in pairs)
    query = urlencode(
        [(k, REDACTED if signed or SENSITIVE_NAME.search(k) or tokenish(v) else v) for k, v in pairs],
        safe="<>",
    )
    return urlunsplit((parts.scheme, netloc, path, query, ""))


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Sensitive names lose their value; URL values (Referer, Origin, Location) go through redact_url."""

    def value_of(name: str, value: str) -> str:
        if SENSITIVE_NAME.search(name) or name.lower().startswith(("openai-", "oai-", "x-openai-", "chatgpt-")):
            return REDACTED
        if urlish(value) or "?" in value:
            return redact_url(value)
        return REDACTED if tokenish(value) else redact_text(value[:200])

    return {name: value_of(name, value) for name, value in sorted(headers.items())}


def _scalar(value: Any, samples: bool) -> str:
    kind = "null" if value is None else type(value).__name__
    if not samples or value is None:
        return kind
    if isinstance(value, str):
        if urlish(value):
            text = redact_url(value)
        elif tokenish(value):
            return f"str: {REDACTED}"
        else:
            text = redact_text(value)
        text = text if len(text) <= SAMPLE_CHARS else text[: SAMPLE_CHARS - 1] + "…"
        return f"str: {text!r}"
    return f"{kind}: {value!r}"


def shape(node: Any, *, samples: bool = True, key: str = "") -> Any:
    """Keys and types, with truncated samples. Under a sensitive key, only types survive."""
    if key and SENSITIVE_NAME.search(key):
        if isinstance(node, (dict, list)):
            return shape(node, samples=False)
        return f"{'null' if node is None else type(node).__name__}: {REDACTED}"
    if isinstance(node, dict):
        return {(REDACTED if tokenish(str(k)) else k): shape(v, samples=samples, key=str(k)) for k, v in node.items()}
    if isinstance(node, list):
        return [f"<list of {len(node)}>", *[shape(item, samples=samples) for item in node[:2]]]
    return _scalar(node, samples)


# redact_text: every secret that can turn up in free text (a log line, an error, page text, a request or
# response body). Field names are matched case-insensitively and over-match on purpose.
_FIELD = (r"__(?:secure|host)-[\w.-]+|oai-[\w-]+|openai-sentinel-[\w-]+|cf_clearance|__cf[\w-]*|_cfuvid|_puid"
          r"|[\w-]*device[\w-]*|did|chatgpt-account-id|account_?id|sig|se|sp|sv|sr|st|skoid|sktid|skt|ske|sks|skv"
          r"|at|f\.sid|bl|snlm0e|fdrfje|cfb2h|[\w-]*sapisid[\w-]*|[a-z]{0,2}sid(?:ts|cc)?|nid"
          r"|[\w.-]*(?:token|secret|passw(?:or)?d|cookie|auth|session|csrf|xsrf|api[_-]?key|sentinel|proof"
          r"|turnstile|sig)[\w.-]*")
_PAIR = re.compile(r"(?<![\w.-])(" + _FIELD + r")(\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^&\s;,\"'<>]+)", re.I)
_JSON_PAIR = re.compile(r"([\"'])(" + _FIELD + r")\1(\s*:\s*)(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|[^,}\]\s]+)",
                        re.I)
_HEADER = re.compile(r"\b((?:proxy-)?authorization|set-cookie|cookie|openai-sentinel-[\w-]+|oai-[\w-]+"
                     r"|chatgpt-account-id|x-openai-[\w-]+|x-goog-[\w-]*auth[\w-]*)(\s*:\s*)[^\r\n]*", re.I)
_SCHEME_TOKEN = re.compile(r"\b(sapisid\w*hash|bearer|basic)\s+[^\s\"']+", re.I)
_JWT_ANYWHERE = re.compile(r"\beyJ[\w-]{6,}(?:\.[\w-]*)+")
_API_KEY = re.compile(r"\bsk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{16,}")
_URL_OR_TOKEN = re.compile(r"(https?://[^\s\"'<>()\[\]{}\\]+)|([A-Za-z0-9_\-+/=.:%~]{20,})")
_HOST_PATH = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+/", re.I)  # "chatgpt.com/backend-api/..." without a scheme


def _url_or_token(match: re.Match) -> str:
    url, run = match.groups()
    if url:
        return redact_url(url)
    if _HOST_PATH.match(run):
        return redact_url("https://" + run).split("://", 1)[1]
    return REDACTED if tokenish(run) else run


def redact_text(text: Any) -> str:
    """`text` safe to print: Authorization/Cookie/Set-Cookie/openai-sentinel-*/oai-*/x-goog-*auth* header values,
    bearer/basic/SAPISIDHASH tokens, JWTs, sk- keys, the values of secret-looking fields (accessToken, session-token,
    oai-did, cf_clearance, at=, f.sid=, SAPISID, sig=, se=, skoid=...), signed download links, and any bare
    token-looking string become <redacted>."""
    text = "" if text is None else str(text)
    text = _HEADER.sub(lambda m: f"{m[1]}{m[2]}{REDACTED}", text)
    text = _SCHEME_TOKEN.sub(lambda m: f"{m[1]} {REDACTED}", text)
    text = _JWT_ANYWHERE.sub(REDACTED, text)
    text = _API_KEY.sub(REDACTED, text)
    text = _JSON_PAIR.sub(lambda m: f'{m[1]}{m[2]}{m[1]}{m[3]}"{REDACTED}"', text)
    text = _PAIR.sub(lambda m: f"{m[1]}{m[2]}{REDACTED}", text)
    return _URL_OR_TOKEN.sub(_url_or_token, text)


def redact_value(value: Any) -> Any:
    """redact_text on every string inside a JSON-able value (error extras, capture records); keys stay."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    return value


def post_body_keys(body: str | None) -> list[str]:
    """Field names of a blocked POST body (JSON or form). Never the values."""
    if not body:
        return []
    try:
        data = json.loads(body)
        return sorted(data) if isinstance(data, dict) else [f"<{type(data).__name__}>"]
    except ValueError:
        return sorted({k for k, _ in parse_qsl(body, keep_blank_values=True)})
