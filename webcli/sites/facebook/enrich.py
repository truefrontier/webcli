"""Enrichment: role, company, location, and public links, each as {value, source, confidence} or null.

Sources, and nothing else:
  facebook_about         the person's About tab, as the signed-in account sees it (cached by `about`)
  facebook_friends_list  the work/city line under their name on the account's Friends tab
  company_site           the homepage of a website the person lists on Facebook
  web_search             a public LinkedIn profile URL found with the `tvly` CLI
Never a paid people-data broker. Unknown is null; nothing is guessed. A LinkedIn result counts only
when the name matches AND the person's company or city appears in it, and only when exactly one
profile qualifies.

Confidence: high = stated by the person (About tab) or both company and city corroborate a LinkedIn
result; medium = a Friends-tab subtitle, or name + company on LinkedIn; low = name + city only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import unicodedata
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

from webcli import __version__
from webcli.sites.facebook import parse
from webcli.sites.facebook.guard import fb_host
from webcli.sites.facebook.store import now

TVLY_ENV = "WEBCLI_TVLY"
TVLY_FALLBACK = Path.home() / ".local" / "bin" / "tvly"
SITE_TIMEOUT = 8
SITE_MAX_BYTES = 300_000
SITE_MAX_REDIRECTS = 3
USER_AGENT = f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) webcli/{__version__} (+company site check)"
COMPANY_NOISE = {"the", "inc", "llc", "ltd", "co", "corp", "corporation", "company", "pllc", "pc", "lp", "llp",
                 "plc", "gmbh", "and", "of"}
SUBTITLE_WORK = re.compile(r"^(?:Works at (?P<employer>.+)|(?P<role>[^·]+?) at (?P<employer2>[^·]+))$")
SUBTITLE_CITY = re.compile(r"^Lives in (?P<city>.+)$")


def field(value: str | None, source: str, confidence: str) -> dict | None:
    return {"value": value, "source": source, "confidence": confidence} if value else None


def words(text: str | None) -> list[str]:
    """Lowercase ASCII words: accents folded, punctuation dropped."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", text.lower())


def company_words(name: str | None) -> list[str]:
    return [w for w in words(name) if w not in COMPANY_NOISE]


def contains(haystack: str | None, needle: list[str]) -> bool:
    """Every needle word appears as a whole word in haystack (and there is at least one)."""
    hay = set(words(haystack))
    return bool(needle) and all(w in hay for w in needle)


def city_words(city: str | None) -> list[str]:
    """"Springfield, Missouri" -> ["springfield"]: the city itself, not the state."""
    return words((city or "").split(",")[0])


# --- Web adapters (replaced by fakes in tests) -----------------------------------------------------

def tvly_path() -> str | None:
    """The tvly binary, or None when it's missing or WEBCLI_TVLY=off: tvly is optional."""
    wanted = os.environ.get(TVLY_ENV, "").strip()
    if wanted.lower() in ("off", "0", "false", "no", "none"):
        return None
    if wanted:
        return shutil.which(wanted)
    return shutil.which("tvly") or (str(TVLY_FALLBACK) if os.access(TVLY_FALLBACK, os.X_OK) else None)


def tvly_search(query: str, *, max_results: int = 8) -> list[dict] | None:
    """[{title, url, content}] from `tvly search --json`, restricted to linkedin.com. None if unavailable or failed."""
    exe = tvly_path()
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "search", query, "--json", "--max-results", str(max_results), "--include-domains", "linkedin.com"],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        )
        if done.returncode != 0:
            return None
        results = json.loads(done.stdout).get("results")
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None
    if not isinstance(results, list):
        return None
    return [{"title": str(r.get("title") or ""), "url": str(r.get("url") or ""), "content": str(r.get("content") or "")}
            for r in results if isinstance(r, dict)]


def public_http_url(url: str) -> bool:
    """http(s), not a Facebook host (Facebook is only ever read through the guarded browser), and
    every address the host resolves to is public (no localhost, LAN, or link-local)."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:  # bad port or IPv6 literal
        return False
    if parts.scheme not in ("http", "https") or not host or fb_host(host):
        return False
    try:
        infos = socket.getaddrinfo(host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except (OSError, UnicodeError, ValueError):
        return False
    # ponytail: checked before connect, so DNS rebinding between check and fetch is not covered.
    return bool(infos) and all(ipaddress.ip_address(info[4][0].split("%")[0]).is_global for info in infos)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # site_name follows redirects itself, re-checking each hop


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_title = False
        self.title = ""
        self.site_name = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "meta" and (attrs.get("property") or attrs.get("name")) in ("og:site_name", "og:title"):
            self.site_name = self.site_name or (attrs.get("content") or "")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data


def site_name(url: str) -> str | None:
    """The homepage's <title> and og:site_name, joined. None when it can't be read safely."""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(SITE_MAX_REDIRECTS + 1):
        if not public_http_url(url):
            return None
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with opener.open(request, timeout=SITE_TIMEOUT) as response:
                if "html" not in (response.headers.get("Content-Type") or "html"):
                    return None
                raw = response.read(SITE_MAX_BYTES)
                charset = response.headers.get_content_charset() or "utf-8"
                break
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                url = urljoin(url, exc.headers["Location"])
                continue
            return None
        except (OSError, ValueError):
            return None
    else:
        return None
    parser = _TitleParser()
    try:
        parser.feed(raw.decode(charset, errors="replace"))
    except Exception:
        return None
    text = " ".join(f"{parser.title} {parser.site_name}".split())
    return text or None


# --- Matching --------------------------------------------------------------------------------------

def _name_part(title: str) -> str:
    """LinkedIn result titles read "Jane Doe - Owner - Acme | LinkedIn": the name is the first part."""
    return re.split(r"\s+[-–—|]\s+|\s*\|\s*", title, maxsplit=1)[0]


def linkedin_match(name: str | None, company: str | None, city: str | None,
                   results: list[dict]) -> dict | None:
    """The one LinkedIn profile whose title names this person and whose text names their company
    or city. None when there's no such profile, or more than one.

    Every word of the Facebook name (initials aside) must be in the result's name part, so
    "Jane Alice Doe" never matches "Jane Bob Doe". Extra words on LinkedIn (a middle name) are fine.
    """
    tokens = [t for t in words(name) if len(t) > 1]
    if len(tokens) < 2 or not results:
        return None
    hits: dict[str, tuple[str, str]] = {}
    for result in results:
        url = parse.linkedin_profile(result.get("url"))
        if not url:
            continue
        who = set(words(_name_part(result.get("title") or "")))
        if not all(t in who for t in tokens):
            continue
        text = f"{result.get('title', '')} {result.get('content', '')}"
        by_company = contains(text, company_words(company))
        by_city = contains(text, city_words(city))
        if by_company or by_city:
            level = "high" if by_company and by_city else "medium" if by_company else "low"
            hits.setdefault(url.lower(), (url, level))
    if len(hits) != 1:
        return None
    url, level = next(iter(hits.values()))
    return field(url, "web_search", level)


# --- Build -----------------------------------------------------------------------------------------

def _subtitle(person: dict) -> tuple[str | None, str | None, str | None]:
    """(role, employer, city) from the Friends-tab subtitle, one " · " part at a time."""
    role = employer = city = None
    for part in (person.get("subtitle") or "").split(" · "):
        part = part.strip()
        if m := SUBTITLE_CITY.match(part):
            city = city or m["city"]
        elif (m := SUBTITLE_WORK.match(part)) and not parse._EDU.match(part):
            employer = employer or (m["employer"] or m["employer2"]).strip()
            role = role or (m["role"].strip() if m["role"] else None)
    return role, employer, city


def build(person: dict, about: dict | None, *, web: bool = True,
          search: Callable[[str], list[dict] | None] = tvly_search,
          site: Callable[[str], str | None] = site_name) -> dict:
    """The enrichment record for one cached person. `about` is their cached About record or None."""
    about = about or {}
    job = next((w for w in about.get("work") or [] if w.get("current") and w.get("employer")), None)
    sub_role, sub_employer, sub_city = _subtitle(person)

    if job:
        role = field(job.get("role"), "facebook_about", "high")
        company = field(job["employer"], "facebook_about", "high")
    else:
        role = field(sub_role, "facebook_friends_list", "medium")
        company = field(sub_employer, "facebook_friends_list", "medium")
    location = (field(about.get("currentCity"), "facebook_about", "high")
                or field(sub_city, "facebook_friends_list", "medium"))

    links = about.get("links") or []
    website_url = next((l["url"] for l in links if l.get("type") == "website"), None)
    website = field(website_url, "facebook_about", "high")
    linkedin = field(next((parse.linkedin_profile(l["url"]) for l in links
                           if parse.linkedin_profile(l.get("url"))), None), "facebook_about", "high")

    company_page = None
    company_name = company["value"] if company else None
    if web and website_url and company_name:
        try:
            title = site(website_url)
        except Exception:  # a web check never sinks the record; the website stays unverified
            title = None
        if title and contains(title, company_words(company_name)):
            company_page = field(website_url, "company_site", "high")
    if not company_page and job and job.get("employerPage"):
        company_page = field(job["employerPage"], "facebook_about", "high")

    if linkedin:
        status = "not needed"
    elif not web:
        status = "skipped"
    elif not (company_name or location):
        status = "not enough to corroborate"
    else:
        name = person.get("name") or about.get("name")
        city = location["value"] if location else None
        query = " ".join(f'"{x}"' for x in (name, company_name or city) if x) + " linkedin"
        results = search(query)
        if results is None:
            status = "unavailable"
        else:
            status = "ok"
            linkedin = linkedin_match(name, company_name, city, results)

    return {
        "key": person["key"],
        "name": person.get("name") or about.get("name"),
        "role": role,
        "company": company,
        "location": location,
        "publicLinks": {"linkedin": linkedin, "website": website, "companyPage": company_page},
        "webSearch": status,
        "enrichedAt": now(),
    }
