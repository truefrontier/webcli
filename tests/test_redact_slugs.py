"""0.16.4: a readable article slug in a URL path is not a secret; real tokens in paths still go."""

import pytest

from webcli.core.output import redact_value
from webcli.core.redact import REDACTED, redact_text, redact_url, slugish

SLUG_URL = "https://news.example.com/2025/08/18/study-finds-95-percent-of-ai-pilots-at-companies-failing-cfo"


@pytest.mark.parametrize("url", [
    SLUG_URL,
    "https://blog.example.org/posts/how-to-build-a-cli-in-2026.html",
    "https://www.example.com/business/2026/q3-results-beat-forecast-by-12-percent",
    "https://docs.example.dev/guides/getting-started-with-the-v2-api",
])
def test_article_slugs_stay(url):
    assert redact_url(url) == url
    assert redact_value({"failed": [url]}) == {"failed": [url]}
    assert redact_text(f"1 of 2 sources failed: {url}.") == f"1 of 2 sources failed: {url}."


@pytest.mark.parametrize("url", [
    "https://example.com/files/a1b2c3d4-e5f6a7b8-c9d0e1f2-a3b4c5d6",   # dashed hex id
    "https://example.com/x/k3j4-h5g6-f7d8-s9a0-q1w2",                   # letter-digit chunks
    "https://example.com/dl/AbCdEfGh1234567890XyZ_-abc",                # opaque, mixed case
    "https://example.com/s/ghp-abcdefghijklmnopqrstuvwxyz0123456789",   # one long part
    "https://example.com/r/reset-token-9f8e7d6c5b4a39281706",           # long trailing secret part
])
def test_tokens_in_paths_still_go(url):
    assert redact_url(url).endswith(f"/{REDACTED}")


def test_slugish_rules():
    assert slugish("study-finds-95-percent-of-ai-pilots-failing")
    assert slugish("study-finds-95-percent-failing.")       # sentence punctuation after a URL
    assert not slugish("two-parts")                          # too few parts to matter (and short anyway)
    assert not slugish("Study-Finds-95-Percent-Failing")     # not lowercase
    assert not slugish("a1-b2-c3-d4-e5")                     # mixed letter-digit parts
    assert not slugish("1234-5678-9012-3456")                # numbers, not words (a card-like number)


def test_query_values_and_signed_hosts_unchanged():
    assert REDACTED in redact_url("https://example.com/a?token=study-finds-95-percent-of-ai-pilots")
    assert redact_url("https://lh3.googleusercontent.com/gg/some-long-readable-looking-slug-here") == \
        f"https://lh3.googleusercontent.com/{REDACTED}"
