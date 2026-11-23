"""Pure parsers: profile ids and URLs, the friends tab, About sections, friend search, and photos. No browser, no
network.

Input is the outerHTML of the page's [role="main"] region, grabbed after Facebook rendered it.
The parsers read it like innerText: block elements break lines, inline text joins. They key on
link targets and English label words ("Current city", "Works at"), never on Facebook's
generated class names. A Facebook UI in another language needs its own words here.

Stored URLs are always canonical: Facebook adds per-session tokens to links (__cft__[0], __tn__,
the h= of l.facebook.com/l.php), and none of those may reach the cache or an export.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Iterator
from urllib.parse import SplitResult, parse_qs, parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

ORIGIN = "https://www.facebook.com"
# Query fields that carry a secret or a per-session token on a Facebook link (fbc's list: CSRF tokens, session
# cookies, __cft__ / __tn__ / h / __hs / __spin_* / __dyn / __csr link and build tokens, the account id): never stored.
TOKEN_FIELD = re.compile(
    r"cookie|auth|key|token|secret|pass|session|csrf|xsrf|sig|otp|credential|bearer|dtsg|jazoest|lsd|"
    r"c_user|^xs$|datr|^sb$|^fr$|__cft__|__tn__|^h$|__hs|__spin|__dyn|__csr|__rev|__hsi|^av$|__user|encrypted",
    re.I,
)
FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com",
            "touch.facebook.com", "business.facebook.com"}
LINK_SHIMS = {"l.facebook.com", "lm.facebook.com"}
# First path segments that are Facebook features, never a person.
RESERVED = {
    "about", "ads", "ajax", "api", "bookmarks", "business", "careers", "checkpoint", "comment", "dialog", "directory",
    "events", "find-friends", "friends", "fundraisers", "gaming", "groups", "hashtag", "help", "home.php", "jobs",
    "l.php", "legal", "login", "login.php", "logout", "logout.php", "marketplace", "me", "media", "memories",
    "messages", "notes", "notifications", "pages", "permalink.php", "photo", "photo.php", "photos", "places",
    "plugins", "policies", "privacy", "public", "recover", "reel", "reels", "saved", "search", "settings",
    "share", "sharer", "sharer.php", "stories", "story.php", "terms", "two_step_verification", "watch", "video",
    "videos", "weather", "browse", "live", "offers", "posts", "profile", "people", "pg", "messenger_media",
}
_USERNAME = re.compile(r"^[A-Za-z0-9.]{2,50}$")
_NUMERIC = re.compile(r"^\d{5,20}$")
SOCIAL = {"linkedin.com": "linkedin", "instagram.com": "instagram", "twitter.com": "x", "x.com": "x",
          "youtube.com": "youtube", "tiktok.com": "tiktok", "github.com": "github", "threads.net": "threads",
          "pinterest.com": "pinterest"}
# Button and tab labels that can sit inside a friend card or the friends tab; never a subtitle.
UI_LABELS = {
    "add friend", "all friends", "birthdays", "cancel request", "college", "confirm", "current city", "delete",
    "edit profile", "find friends", "follow", "followers", "following", "friend requests", "friends", "high school",
    "hometown", "message", "more", "recently added", "remove", "search", "see all", "see more", "unfriend", "work",
}
_MUTUAL = re.compile(r"^\d[\d,.]*\s+mutual friends?$", re.I)
UNAVAILABLE = ("this content isn't available", "this page isn't available", "content isn't available right now",
               "isn't available at the moment")


# --- A small DOM ---------------------------------------------------------------------------------

BLOCK = {"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption", "figure",
         "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
         "pre", "section", "table", "td", "th", "tr", "ul"}
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "template", "noscript", "svg"}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def iter(self, tag: str | None = None) -> Iterator["Node"]:
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.iter(tag)

    def text(self) -> str:
        return " ".join(line.text for line in lines(self))

    def ancestors(self) -> list["Node"]:
        """Root first, self last."""
        chain, node = [], self
        while node is not None:
            chain.append(node)
            node = node.parent
        return chain[::-1]


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root")
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, parent=self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(Node(tag, {k: v or "" for k, v in attrs}, parent=self.cur))

    def handle_endtag(self, tag):
        node = self.cur
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:  # a stray end tag closes nothing
            self.cur = node.parent

    def handle_data(self, data):
        self.cur.children.append(data)


def build(html: str) -> Node:
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder.root


@dataclass
class Line:
    text: str
    links: list[str] = field(default_factory=list)


def _hidden(node: Node) -> bool:
    style = node.attrs.get("style", "").replace(" ", "").lower()
    return "hidden" in node.attrs or "display:none" in style


def lines(node: Node) -> list[Line]:
    """innerText-like lines: block elements break, inline text joins. Each line keeps its link hrefs."""
    out: list[Line] = []
    buf: list[str] = []
    links: list[str] = []

    def flush() -> None:
        text = " ".join("".join(buf).split())
        if text:
            out.append(Line(text, list(dict.fromkeys(links))))
        buf.clear()
        links.clear()

    def walk(n: Node, href: str | None) -> None:
        if n.tag in SKIP or _hidden(n):
            return
        if n.tag == "a" and n.attrs.get("href"):
            href = n.attrs["href"]
        block = n.tag in BLOCK
        if block:
            flush()
        for child in n.children:
            if isinstance(child, str):
                if child.strip() and href:
                    links.append(href)
                buf.append(child)
            else:
                walk(child, href)
        if block:
            flush()

    walk(node, None)
    flush()
    return out


def find_main(root: Node) -> Node:
    return next((n for n in root.iter() if n.attrs.get("role") == "main"), root)


# --- Profile ids and URLs ------------------------------------------------------------------------

@dataclass(frozen=True)
class Person:
    """A profile: key is the username when known, else the numeric id."""

    key: str
    username: str | None = None
    numeric_id: str | None = None

    @property
    def url(self) -> str:
        if self.username:
            return f"{ORIGIN}/{self.username}"
        return f"{ORIGIN}/profile.php?id={self.numeric_id}"


def _split(href: str, base: str = ORIGIN) -> SplitResult | None:
    """urlsplit of href against base, or None for a malformed URL (a page may hold any href)."""
    try:
        parts = urlsplit(urljoin(base + "/", href.strip()))
        parts.port  # noqa: B018 - raises ValueError for a bad port
        return parts
    except ValueError:
        return None


def person_from_url(href: str | None, base: str = ORIGIN) -> Person | None:
    """The profile a facebook.com link points to, or None for anything that isn't a person."""
    parts = _split(href) if href else None
    if parts is None:
        return None
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() not in FB_HOSTS:
        return None
    segs = [s for s in parts.path.split("/") if s]
    if not segs:
        return None
    first = segs[0]
    if first.lower() == "profile.php":
        ids = parse_qs(parts.query).get("id", [])
        return Person(ids[0], numeric_id=ids[0]) if len(ids) == 1 and _NUMERIC.match(ids[0]) else None
    if first.lower() == "people" and len(segs) >= 3 and _NUMERIC.match(segs[2]):
        return Person(segs[2], numeric_id=segs[2])  # /people/Jane-Doe/1000123/
    if _NUMERIC.match(first):
        return Person(first, numeric_id=first)
    if first.lower() in RESERVED or not _USERNAME.match(first) or not re.search(r"[A-Za-z]", first):
        return None
    if first.startswith(".") or first.endswith(".") or ".." in first or first.lower().endswith((".php", ".html")):
        return None
    return Person(first, username=first)


def person_from_arg(value: str) -> Person:
    """A CLI argument: a numeric id, a username, or a profile URL. ValueError for anything else."""
    value = (value or "").strip()
    if _NUMERIC.match(value):
        return Person(value, numeric_id=value)
    if "/" in value or value.lower() in FB_HOSTS:  # a URL; "m.jones" is a username, not m.facebook.com
        url = value if re.match(r"^https?://", value, re.I) else "https://" + value.lstrip("/")
        person = person_from_url(url)
        if person:
            return person
    elif _USERNAME.match(value) and value.lower() not in RESERVED and re.search(r"[A-Za-z]", value):
        return Person(value, username=value)
    raise ValueError(f"Not a Facebook profile id, username, or profile URL: {value!r}")


def about_url(person: Person, section: str) -> str:
    if person.username:
        return f"{ORIGIN}/{person.username}/{section}"
    return f"{ORIGIN}/profile.php?{urlencode({'id': person.numeric_id, 'sk': section})}"


def fb_page_url(href: str | None) -> str | None:
    """A facebook.com page link (an employer, a school) without its tracking query."""
    parts = _split(href) if href else None
    if parts is None or (parts.hostname or "").lower() not in FB_HOSTS or not parts.path.strip("/"):
        return None
    if parts.path.lower().startswith("/profile.php"):
        ids = parse_qs(parts.query).get("id", [])
        return f"{ORIGIN}/profile.php?id={ids[0]}" if len(ids) == 1 and _NUMERIC.match(ids[0]) else None
    return ORIGIN + parts.path.rstrip("/")


def external_url(href: str | None) -> str | None:
    """The real target of an outbound link: unwraps l.facebook.com/l.php?u=..., drops h=, fbclid,
    utm_*, user:password@, and any token-looking query field. None for facebook.com links and
    anything that isn't http(s)."""
    parts = _split(href) if href else None
    if parts is None:
        return None
    host = (parts.hostname or "").lower()
    if host in LINK_SHIMS:
        targets = parse_qs(parts.query).get("u", [])
        parts = _split(targets[0]) if len(targets) == 1 else None
        if parts is None:
            return None
        host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host or host in FB_HOSTS or host.endswith(".facebook.com"):
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() != "fbclid" and not k.lower().startswith("utm_") and not TOKEN_FIELD.search(k)])
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, query, ""))


def link_type(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for domain, kind in SOCIAL.items():
        if host == domain or host.endswith("." + domain):
            return kind
    return "website"


def linkedin_profile(url: str | None) -> str | None:
    """https://www.linkedin.com/in/<slug> for a LinkedIn member URL, else None (company pages, posts...)."""
    parts = _split(url) if url else None
    if parts is None:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return None
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 2 or segs[0].lower() != "in" or not re.match(r"^[A-Za-z0-9\-_%]{2,100}$", segs[1]):
        return None
    return f"https://www.linkedin.com/in/{segs[1]}"


# --- Page checks ---------------------------------------------------------------------------------

def unavailable(html: str) -> bool:
    text = find_main(build(html)).text().lower().replace("’", "'")
    return any(phrase in text for phrase in UNAVAILABLE)


def _title_name(title: str | None) -> str | None:
    name = re.sub(r"^\(\d+\+?\)\s*", "", (title or "").strip())  # "(3) Jane Doe | Facebook"
    name = re.sub(r"\s*\|\s*Facebook\s*$", "", name).strip()
    return name if name and name.lower() not in ("facebook", "log in or sign up", "log into facebook", "log in to facebook") else None


def profile_name(html: str, title: str | None = None) -> str | None:
    main = find_main(build(html))
    for h1 in main.iter("h1"):
        if text := h1.text().strip():
            return text
    return _title_name(title)


# --- Friends tab ---------------------------------------------------------------------------------

def _lca(nodes: list[Node]) -> Node:
    chains = [n.ancestors() for n in nodes]
    common = chains[0][0]
    for level in zip(*chains):
        if all(n is level[0] for n in level):
            common = level[0]
        else:
            break
    return common


def _keys_under(root: Node) -> dict[int, set[str]]:
    """id(node) -> the person keys linked anywhere inside it, in one pass (cards are checked often)."""
    memo: dict[int, set[str]] = {}

    def walk(node: Node) -> set[str]:
        keys: set[str] = set()
        if node.tag == "a" and (person := person_from_url(node.attrs.get("href"))):
            keys.add(person.key)
        for child in node.children:
            if isinstance(child, Node):
                keys |= walk(child)
        memo[id(node)] = keys
        return keys

    walk(root)
    return memo


def _subtitle_lines(card: Node, name: str) -> list[str]:
    out = []
    for line in lines(card):
        text = line.text.strip(" ·")
        if text.startswith(name + " "):  # the name link and the next item rendered on one line
            text = text[len(name):].strip(" ·")
        if not text or text == name or text.lower() in UI_LABELS or _MUTUAL.match(text):
            continue
        if text not in out:
            out.append(text)
    return out


def friends(html: str, page_url: str) -> list[dict]:
    """Friend cards on a friends tab: {key, name, profileUrl, numericId, username, subtitle}, page order.

    A card is the lowest element holding every link to one person (avatar and name), bounded so it
    never takes in another person's link. The account's own profile (the page owner) is left out.
    """
    main = find_main(build(html))
    me = person_from_url(page_url)
    found: dict[str, list[tuple[Node, Person]]] = {}
    for a in main.iter("a"):
        person = person_from_url(a.attrs.get("href"))
        if person and (me is None or person.key != me.key):
            found.setdefault(person.key, []).append((a, person))
    under = _keys_under(main)

    def others(node: Node, mine: str) -> bool:
        return bool(under.get(id(node), set()) - {mine, me.key if me else mine})

    rows = []
    for key, items in found.items():
        anchors = [a for a, _ in items]
        name = next((t for a in anchors if (t := a.text().strip()) and t.lower() not in UI_LABELS
                     and not _MUTUAL.match(t)), None)
        name = name or next((a.attrs["aria-label"].strip() for a in anchors if a.attrs.get("aria-label")), None)
        if not name:
            continue
        card = _lca(anchors)
        if others(card, key):  # the same person linked from two places: start from the name link
            card = next((a for a in anchors if a.text().strip() == name), anchors[0])
        # Only the name so far: widen one level at a time, never into another card or a big section.
        while not _subtitle_lines(card, name) and card is not main and card.parent is not None:
            parent = card.parent
            if others(parent, key) or len(lines(parent)) > 4:
                break
            card = parent
        sub = _subtitle_lines(card, name)[:3]
        username = next((p.username for _, p in items if p.username), None)
        numeric = next((p.numeric_id for _, p in items if p.numeric_id), None)
        person = Person(key, username, numeric)
        rows.append({"key": key, "name": name, "profileUrl": person.url, "numericId": numeric,
                     "username": username, "subtitle": " · ".join(sub) or None})
    return rows


# --- About sections ------------------------------------------------------------------------------

_EDU = re.compile(r"^(?:Studie[sd]|Went to|Goes to|Attend(?:s|ed))\b", re.I)
_STUDIED_AT = re.compile(r"^Studie[sd] (?!at )(?P<field>.+?) at (?P<school>.+)$", re.I)  # not "Studied at X at Y"
_SCHOOL = re.compile(r"^(?:Studie[sd] at|Went to|Goes to|Attend(?:s|ed)) (?P<school>.+)$", re.I)
_WORKS_AT = re.compile(r"^(?P<verb>Works|Worked) at (?P<employer>.+)$", re.I)
_FORMER = re.compile(r"^(?:Former|Previously) (?P<role>.+?) at (?P<employer>.+)$", re.I)
_ROLE_AT = re.compile(r"^(?!I\b|I['’]m\b|We\b)(?P<role>[^.!?]{1,80}?) at (?P<employer>.+)$")
_LIVES = re.compile(r"^Lives in (?P<city>.+)$", re.I)
_FROM = re.compile(r"^From (?P<city>.+)$", re.I)
_DATES = re.compile(r"\b(?:19|20)\d{2}\b|\bPresent\b", re.I)
_NOT_WORK = re.compile(r"^(?:Lives in|From|Joined|Born|Married|In a relationship|Engaged|Followed by|Moved to)\b",
                       re.I)
CITY_LABELS = {"current city": "currentCity", "current town/city": "currentCity", "hometown": "hometown"}
LINK_LABELS = {"website", "websites", "linkedin", "instagram", "twitter", "x", "youtube", "tiktok", "github",
               "threads", "pinterest", "social link", "social links"}


def _employer_page(line: Line) -> str | None:
    return next((u for u in map(fb_page_url, line.links) if u and not u.endswith("/about")), None)


def _dates_line(text: str) -> bool:
    """A job's own dates ("2012 - 2016"), not the next item that happens to hold a year ("Married ... since 2010")."""
    return bool(_DATES.search(text)) and not (_NOT_WORK.match(text) or _EDU.match(text) or " at " in text)


def _work(line: Line, nxt: str | None) -> dict | None:
    text = line.text
    if len(text) > 160 or _EDU.match(text) or _NOT_WORK.match(text):
        return None
    ended = bool(nxt and _dates_line(nxt) and "present" not in nxt.lower())
    if m := _WORKS_AT.match(text):
        role, employer, current = None, m["employer"], m["verb"].lower() == "works" and not ended
    elif m := _FORMER.match(text):
        role, employer, current = m["role"], m["employer"], False
    elif m := _ROLE_AT.match(text):
        role, employer, current = m["role"], m["employer"], not ended
    else:
        return None
    return {"role": role.strip() if role else None, "employer": employer.strip(), "employerPage": _employer_page(line),
            "current": current, "raw": text}


def _school(line: Line) -> dict | None:
    if m := _STUDIED_AT.match(line.text):
        return {"school": m["school"].strip(), "field": m["field"].strip(), "raw": line.text}
    if m := _SCHOOL.match(line.text):
        return {"school": m["school"].strip(), "field": None, "raw": line.text}
    return None


def _add_school(education: list[dict], school: dict) -> None:
    """Add a school, merging it with the same school seen in another section: the overview can say
    "Studied at MSU", the education tab "Studied Computer Science at MSU". The entry with a field wins."""
    for i, seen in enumerate(education):
        if seen["school"].lower() == school["school"].lower():
            if school["field"] and not seen["field"]:
                education[i] = school
            return
    education.append(school)


def _add_job(work: list[dict], job: dict) -> None:
    """Add a job, merging it with the same employer seen in another section: the overview says
    "Works at Acme", the work section "Owner at Acme" with dates. The entry with a role wins."""
    for i, seen in enumerate(work):
        if seen["employer"].lower() != job["employer"].lower():
            continue
        if seen["role"] == job["role"]:
            return
        if seen["role"] is None or job["role"] is None:
            detailed, other = (job, seen) if job["role"] else (seen, job)
            work[i] = {**detailed, "employerPage": detailed["employerPage"] or other["employerPage"]}
            return
    work.append(job)


def about(sections: dict[str, str | None], page_url: str, title: str | None = None) -> dict:
    """One profile's About tab as the signed-in account sees it, from the HTML of each section that loaded.

    sections maps a section name to its [role=main] HTML, or None when it didn't load. Each section
    is reported as "ok" (text found) or "missing", so an empty field never reads as "has none".
    """
    work: list[dict] = []
    education: list[dict] = []
    links: list[dict] = []
    places = {"currentCity": None, "hometown": None}
    status = {}
    name = None
    for section, html in sections.items():
        main = find_main(build(html)) if html else None
        found = lines(main) if main is not None else []
        status[section] = "ok" if found else "missing"
        if html and not name:
            name = profile_name(html)
        for i, line in enumerate(found):
            nxt = found[i + 1].text if i + 1 < len(found) else None
            label = (nxt or "").strip().lower()
            if label in CITY_LABELS and not places[CITY_LABELS[label]]:
                places[CITY_LABELS[label]] = line.text
                continue
            if m := _LIVES.match(line.text):
                places["currentCity"] = places["currentCity"] or m["city"].strip()
                continue
            if m := _FROM.match(line.text):
                places["hometown"] = places["hometown"] or m["city"].strip()
                continue
            if school := _school(line):
                _add_school(education, school)
                continue
            for url in filter(None, map(external_url, line.links)):
                if url not in {link["url"] for link in links}:
                    links.append({"type": link_type(url), "url": url})
            if job := _work(line, nxt):
                _add_job(work, job)
    person = person_from_url(page_url)
    return {
        "key": person.key if person else None,
        "username": person.username if person else None,
        "numericId": person.numeric_id if person else None,
        "profileUrl": person.url if person else None,
        "name": name or _title_name(title),
        "work": work,
        "education": education,
        "currentCity": places["currentCity"],
        "hometown": places["hometown"],
        "websites": [link["url"] for link in links if link["type"] == "website"],
        "links": links,
        "sections": status,
    }


# --- Photos of a person, and friend search -------------------------------------------------------
# Facebook's photos tab and people search answer with the same Relay JSON the page renders: a GraphQL
# response, or that JSON embedded in the HTML. These walk that JSON. A photo link in the DOM is only a
# fallback, and it has no caption, owner, time, or tags.

FRIEND_STATUSES = frozenset({"ARE_FRIENDS", "FRIENDS", "IS_FRIEND"})
_NOT_FRIEND = re.compile(r"\bFriends\b")
_MUTUAL_FRIENDS = re.compile(r"mutual friends?", re.I)
_PHOTO_MARK = re.compile(r'"__typename"\s*:\s*"Photo"|"__isMedia"\s*:\s*"Photo"')
_USER_ID_MARK = re.compile(r'"userI[Dd]"\s*:\s*"(\d{5,20})"')
_FBID = re.compile(r"^[0-9]{5,20}$")
_COOKIE_QUERY = re.compile(r"^(c_user|xs|datr|sb|fr|presence)$", re.I)
_SKIP_IMAGE = frozenset({
    "tags", "taggee", "owner", "actors", "feedback", "comments", "message", "privacy_scope",
    "creation_story", "story", "feedback_context", "profile_picture", "profile_picture_depth_0",
})
_IMAGE_RANK = {"full_image": 5, "image": 4, "photo_image": 3, "viewer_image": 2, "preview": 1, "thumbnail": 0}


def parse_since(value: str) -> datetime:
    """--since: a date or an ISO-8601 time, as UTC. A bare date starts at 00:00:00Z."""
    raw = (value or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid --since {value!r}. Use a date (2024-01-01) or ISO-8601 "
                         "(2024-01-01T00:00:00Z).") from exc
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        return when.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"Invalid --since {value!r}: outside the years 1 to 9999 in UTC.") from exc


def since_iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def photos_url(person: Person, mode: str) -> str:
    """The profile tab: photos_of (Photos of <Name>) or photos_by (Uploads)."""
    section = "photos_of" if mode == "of" else "photos_by"
    return about_url(person, section)


def friends_search_url(name: str) -> str:
    """People search restricted to the signed-in account's friends."""
    filters = {"friends": json.dumps({"name": "friends", "args": ""}, separators=(",", ":"))}
    token = base64.b64encode(json.dumps(filters, separators=(",", ":")).encode()).decode()
    return f"{ORIGIN}/search/people/?{urlencode({'q': name, 'filters': token})}"


def clean_image_url(url: str | None) -> str | None:
    """An image URL safe to print: https only, and never a session-cookie query (c_user, xs, ...)."""
    parts = _split(url) if url else None
    if parts is None or parts.scheme != "https" or not parts.hostname:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _COOKIE_QUERY.match(k)])
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, query, ""))


def _iso_time(value) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10**12:  # milliseconds
            stamp /= 1000
        if stamp < 10**9:
            return None
        try:
            return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _iso_time(int(text))
        try:
            return since_iso(parse_since(text))
        except ValueError:
            return None
    return None


def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_dicts(value)


def _json_values(text: str) -> list:
    """JSON values in a GraphQL body or an HTML page: the body itself, then each JSON <script>."""
    raw = text.strip()
    if raw.startswith("for (;;);"):
        raw = raw[9:].strip()
    try:
        return [json.loads(raw)]
    except json.JSONDecodeError:
        pass
    found = []
    for match in re.finditer(r"<script\b[^>]*>(.*?)</script>", text, re.I | re.S):
        body = match.group(1).strip()
        if body.startswith("for (;;);"):
            body = body[9:].strip()
        if not body or body[0] not in "{[":
            found.extend(_photo_objects_in_js(body))
            continue
        try:
            found.append(json.loads(body))
        except json.JSONDecodeError:
            found.extend(_photo_objects_in_js(body))
    if not found:
        found.extend(_photo_objects_in_js(text))
    return found


def _photo_objects_in_js(text: str) -> list[dict]:
    """Photo objects embedded in a script that isn't itself JSON. Bounded so a huge page stays cheap."""
    decoder = json.JSONDecoder()
    found, seen = [], 0
    for mark in _PHOTO_MARK.finditer(text):
        if seen >= 40:
            break
        seen += 1
        window = text[max(0, mark.start() - 100_000):mark.start() + 100_000]
        rel = mark.start() - max(0, mark.start() - 100_000)
        starts = [m.start() for m in re.finditer(r"\{", window[:rel + 1])]
        for pos in reversed(starts[-30:]):
            try:
                obj, _end = decoder.raw_decode(window[pos:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and _is_photo(obj):
                found.append(obj)
                break
    return found


def _is_photo(node: dict) -> bool:
    kind = node.get("__typename") or node.get("__isMedia") or node.get("__isNode")
    return kind == "Photo" and bool(_FBID.match(str(node.get("id") or "")))


def _caption(node: dict) -> str | None:
    message = node.get("message")
    if isinstance(message, dict):
        text = message.get("text")
    elif isinstance(message, str):
        text = message
    else:
        text = None
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def _owner(node: dict) -> dict | None:
    owner = node.get("owner")
    if not isinstance(owner, dict):
        return None
    name = owner.get("name") if isinstance(owner.get("name"), str) else None
    oid = str(owner["id"]) if owner.get("id") else None
    if not name and not oid:
        return None
    url = owner.get("url") if isinstance(owner.get("url"), str) else None
    person = person_from_url(url) if url else None
    return {"id": oid, "name": name, "url": person.url if person else (clean_image_url(url) if url else None)}


def _add_tag(people: list[dict], seen: set, taggee: dict) -> None:
    name = taggee.get("name") if isinstance(taggee.get("name"), str) else None
    tid = str(taggee["id"]) if taggee.get("id") else None
    if not name and not tid:
        return
    key = tid or name
    if key in seen:
        return
    seen.add(key)
    url = taggee.get("url") if isinstance(taggee.get("url"), str) else None
    person = person_from_url(url) if url else None
    tag = {"id": tid, "name": name}
    if person and person.username:
        tag["username"] = person.username
    people.append(tag)


def _tags(node: dict) -> tuple[list[dict], int]:
    raw = next((node[key] for key in ("tags", "with_tags", "facebox_tags")
                if isinstance(node.get(key), (dict, list))), None)
    if raw is None:
        return [], 0
    declared = raw.get("count") if isinstance(raw, dict) and isinstance(raw.get("count"), int) else None
    people: list[dict] = []
    seen: set = set()

    def walk(obj) -> None:
        if isinstance(obj, dict):
            taggee = obj.get("taggee")
            if isinstance(taggee, dict):
                _add_tag(people, seen, taggee)
            elif obj.get("__typename") in ("User", "Page") and obj.get("name") and obj.get("id"):
                _add_tag(people, seen, obj)
                return  # a tagged person; don't treat their nested profiles as more tags
            for key, value in obj.items():
                if key != "taggee":
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(raw)
    return people, max(len(people), declared or 0)


def _images(node: dict) -> list[tuple]:
    found = []

    def walk(obj, hint: str | None) -> None:
        if isinstance(obj, dict):
            uri = obj.get("uri")
            if isinstance(uri, str) and uri.startswith("https://"):
                width = obj.get("width") if isinstance(obj.get("width"), (int, float)) else 0
                height = obj.get("height") if isinstance(obj.get("height"), (int, float)) else 0
                found.append((hint, int(width), int(height), uri))
            for key, value in obj.items():
                if key in _SKIP_IMAGE:
                    continue
                walk(value, key if key in _IMAGE_RANK else hint)
        elif isinstance(obj, list):
            for value in obj:
                walk(value, hint)

    walk(node, None)
    return found


def best_image(candidates: list[tuple]) -> str | None:
    """The largest rendition. Area wins; a full `image` beats a thumbnail of unknown size."""
    usable = []
    for hint, width, height, uri in candidates:
        cleaned = clean_image_url(uri)
        host = (urlsplit(cleaned).hostname or "") if cleaned else ""
        if cleaned and (host == "fbcdn.net" or host.endswith(".fbcdn.net")):
            usable.append((hint, width, height, cleaned))
    if not usable:
        return None

    def rank(item) -> tuple:
        hint, width, height, uri = item
        return ((width or 0) * (height or 0), _IMAGE_RANK.get(hint, 2), len(uri))

    return max(usable, key=rank)[3]


def _photo_page_url(node: dict, photo_id: str) -> str:
    url = node.get("url") if isinstance(node.get("url"), str) else ""
    parts = _split(url) if url else None
    if parts is not None and (parts.hostname or "").lower() in FB_HOSTS:
        fbid = (parse_qs(parts.query).get("fbid") or [None])[0]
        if fbid and _FBID.match(fbid):
            return f"{ORIGIN}/photo/?fbid={fbid}"
        if "/photo" in parts.path.lower():
            return f"{ORIGIN}/photo/?fbid={photo_id}"
    return f"{ORIGIN}/photo/?fbid={photo_id}"


def photo_record(node: dict) -> dict | None:
    """One photo, or None when `node` isn't a Photo. Tags are whoever Facebook tagged, not a guess."""
    if not _is_photo(node):
        return None
    photo_id = str(node["id"])
    tags, tag_count = _tags(node)
    created = None
    for key in ("created_time", "createdTime", "publish_time"):
        created = _iso_time(node.get(key))
        if created:
            break
    return {
        "id": photo_id,
        "imageUrl": best_image(_images(node)),
        "caption": _caption(node),
        "createdTime": created,
        "owner": _owner(node),
        "photoUrl": _photo_page_url(node, photo_id),
        "tags": tags,
        "tagCount": tag_count,
        "faceCount": None,
    }


def _fbid(href: str | None) -> str | None:
    parts = _split(href) if href else None
    if parts is None:
        return None
    for key in ("fbid", "photo_id"):
        values = parse_qs(parts.query).get(key) or []
        if len(values) == 1 and _FBID.match(values[0]):
            return values[0]
    return None


def photos_from_dom(html: str) -> list[dict]:
    """Photo links in the rendered tab, used only when the page carried no photo JSON. No tags, no caption."""
    main = find_main(build(html))
    found, seen = [], set()
    for link in main.iter("a"):
        photo_id = _fbid(link.attrs.get("href"))
        if not photo_id or photo_id in seen:
            continue
        seen.add(photo_id)
        image = None
        for img in link.iter("img"):
            src = clean_image_url(img.attrs.get("src"))
            if src and "fbcdn.net" in (urlsplit(src).hostname or ""):
                image = src
                break
        found.append({
            "id": photo_id, "imageUrl": image, "caption": None, "createdTime": None, "owner": None,
            "photoUrl": f"{ORIGIN}/photo/?fbid={photo_id}", "tags": [], "tagCount": 0, "faceCount": None,
        })
    return found


def photos_in(payload) -> list[dict]:
    """Photos in one GraphQL body or HTML document, first-seen order, deduped by id."""
    nodes: list = []
    if isinstance(payload, str):
        nodes = _json_values(payload)
    elif isinstance(payload, (dict, list)):
        nodes = [payload]
    found: dict[str, dict] = {}
    for node in nodes:
        for obj in _walk_dicts(node):
            photo = photo_record(obj)
            if photo and photo["id"] not in found:
                found[photo["id"]] = photo
    if found:
        return list(found.values())
    if isinstance(payload, str):
        return photos_from_dom(payload)
    return []


def prefer_photo(old: dict, new: dict) -> dict:
    """Keep the record that has more of what the tab's JSON knows (tags, time, caption, owner, image)."""

    def score(photo: dict) -> tuple:
        return (1 if photo.get("tags") else 0, 1 if photo.get("createdTime") else 0,
                1 if photo.get("caption") else 0, 1 if photo.get("owner") else 0,
                1 if photo.get("imageUrl") else 0)

    return new if score(new) > score(old) else old


def photos_from(payloads: list) -> list[dict]:
    found: dict[str, dict] = {}
    for payload in payloads:
        if isinstance(payload, str) and not payload.strip():
            continue
        for photo in photos_in(payload):
            found[photo["id"]] = prefer_photo(found[photo["id"]], photo) if photo["id"] in found else photo
    if found:
        return list(found.values())
    for payload in payloads:
        if isinstance(payload, str):
            for photo in photos_from_dom(payload):
                found.setdefault(photo["id"], photo)
    return list(found.values())


def best_image_for(payloads: list, photo_id: str, fallback: str | None = None) -> str | None:
    """The largest image on a photo page for `photo_id`, else the largest image there, else `fallback`."""
    ranked: list[tuple] = []
    loose: list[tuple] = []
    for payload in payloads:
        nodes = _json_values(payload) if isinstance(payload, str) else [payload]
        for node in nodes:
            for obj in _walk_dicts(node):
                if not isinstance(obj, dict):
                    continue
                images = _images(obj)
                if _is_photo(obj) and str(obj.get("id")) == photo_id:
                    ranked.extend(images)
                elif obj.get("__typename") == "Photo" or "image" in obj:
                    loose.extend(images)
    return best_image(ranked) or best_image(loose) or clean_image_url(fallback)


def _collect_user_ids(node, add, depth: int = 0) -> None:
    if depth > 30:
        return
    if isinstance(node, dict):
        for key in ("userID", "userId"):
            if key in node:
                add(node[key])
        for value in node.values():
            _collect_user_ids(value, add, depth + 1)
    elif isinstance(node, list):
        for value in node[:400]:
            _collect_user_ids(value, add, depth + 1)


def _clean_profile_name(name: str | None) -> str | None:
    if not name:
        return None
    name = re.sub(r"^photos of\s+", "", name, flags=re.I).strip()
    if name.casefold() in {"photos", "uploads", "facebook", "log in or sign up"}:
        return None
    return name


def photo_subject(who: Person, page_url: str, payloads: list, html: str | None) -> dict:
    """Who the photos tab is about: the id we were given, else the one userID the page names, plus the h1 name."""
    seen = person_from_url(page_url)
    found: list[str] = []

    def add(value) -> None:
        text = str(value or "")
        if _FBID.match(text) and text not in found:
            found.append(text)

    if who.numeric_id:
        add(who.numeric_id)
    if seen and seen.numeric_id:
        add(seen.numeric_id)
    if html:
        for match in _USER_ID_MARK.finditer(html):
            add(match.group(1))
    for payload in payloads:
        if isinstance(payload, str):
            for match in _USER_ID_MARK.finditer(payload):
                add(match.group(1))
            for value in _json_values(payload):
                _collect_user_ids(value, add)
        else:
            _collect_user_ids(payload, add)
    if who.numeric_id:
        match_ids = [who.numeric_id]
    elif seen and seen.numeric_id:
        match_ids = [seen.numeric_id]
    else:
        match_ids = found if len(found) == 1 else []
    name = _clean_profile_name(profile_name(html, None) if html else None)
    username = who.username or (seen.username if seen else None)
    numeric = match_ids[0] if match_ids else None
    url_person = Person(username or numeric or who.key, username, numeric)
    return {
        "id": numeric,
        "ids": match_ids,
        "name": name,
        "names": [name] if name else [],
        "username": username,
        "profileUrl": url_person.url if (username or numeric) else who.url,
        "key": username or numeric or who.key,
    }


def _tag_is_subject(tag: dict, subject: dict) -> bool:
    tid = tag.get("id")
    ids = subject.get("ids") or []
    if tid and ids:
        return tid in ids
    if tid and subject.get("id"):
        return tid == subject["id"]
    username = tag.get("username")
    if username and subject.get("username") and username == subject["username"]:
        return True
    name = (tag.get("name") or "").casefold()
    names = {(item or "").casefold() for item in subject.get("names") or [] if item}
    return bool(name and name in names)


def is_solo(photo: dict, subject: dict) -> bool:
    """True only when Facebook's tag list has exactly the subject. Missing tags are not solo."""
    tags = photo.get("tags") or []
    if photo.get("tagCount") != 1 or len(tags) != 1:
        return False
    return _tag_is_subject(tags[0], subject)


def on_or_after(photo: dict, since: str) -> bool:
    """A photo with no created time is not inside --since (the time is unknown, not guessed)."""
    created = photo.get("createdTime")
    return bool(created) and created >= since


def _words(text: str) -> set[str]:
    return {word.casefold() for word in re.findall(r"[^\W_]+", text, re.UNICODE)}


def _snippet(node: dict) -> str | None:
    for key in ("primary_snippet_text", "snippet"):
        if isinstance(node.get(key), str):
            return node[key]
    entities = node.get("primary_snippet_text_with_entities")
    if isinstance(entities, dict) and isinstance(entities.get("text"), str):
        return entities["text"]
    return None


def _snippet_says_friend(snippet: str) -> bool:
    return bool(_NOT_FRIEND.search(_MUTUAL_FRIENDS.sub("", snippet)))


def _friend_hit(view: dict, profile: dict, tokens: set[str]) -> dict | None:
    if profile.get("__typename") not in (None, "User"):
        return None
    name = profile.get("name")
    if not isinstance(name, str) or not tokens <= _words(name):
        return None
    status = profile.get("friendship_status") or view.get("friendship_status")
    snippet = _snippet(view)
    if isinstance(status, str) and status not in FRIEND_STATUSES:
        return None
    if not (isinstance(status, str) and status in FRIEND_STATUSES) and not _snippet_says_friend(snippet or ""):
        return None
    url = profile.get("url") if isinstance(profile.get("url"), str) else None
    person = person_from_url(url) if url else None
    pid = str(profile.get("id") or "")
    if not _FBID.match(pid):
        pid = person.numeric_id if person and person.numeric_id else ""
    if not pid and person is None:
        return None
    return {
        "id": pid or None,
        "name": name,
        "username": person.username if person else None,
        "profileUrl": person.url if person else url,
        "snippet": snippet,
    }


def friend_hits(payloads: list, query: str) -> list[dict]:
    """Friends whose name contains every word of `query`, in page order. Not-friends are left out."""
    tokens = _words(query)
    if not tokens:
        return []
    hits, seen = [], set()
    for payload in payloads:
        nodes = _json_values(payload) if isinstance(payload, str) else [payload]
        for node in nodes:
            for view in _walk_dicts(node):
                profile = view.get("profile")
                if not isinstance(profile, dict):
                    continue
                hit = _friend_hit(view, profile, tokens)
                key = (hit["id"] or hit["profileUrl"]) if hit else None
                if hit and key and key not in seen:
                    seen.add(key)
                    hits.append(hit)
    return hits
