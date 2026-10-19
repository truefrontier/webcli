"""Pure parsers (fbc's, unchanged): profile ids and URLs, the friends tab, and About sections. No browser, no
network.

Input is the outerHTML of the page's [role="main"] region, grabbed after Facebook rendered it.
The parsers read it like innerText: block elements break lines, inline text joins. They key on
link targets and English label words ("Current city", "Works at"), never on Facebook's
generated class names. A Facebook UI in another language needs its own words here.

Stored URLs are always canonical: Facebook adds per-session tokens to links (__cft__[0], __tn__,
the h= of l.facebook.com/l.php), and none of those may reach the cache or an export.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    if "/" in value or "." in value and value.lower().startswith(("facebook.", "www.", "m.")):
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
_STUDIED_AT = re.compile(r"^Studie[sd] (?P<field>.+?) at (?P<school>.+)$", re.I)
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


def _work(line: Line, nxt: str | None) -> dict | None:
    text = line.text
    if len(text) > 160 or _EDU.match(text) or _NOT_WORK.match(text):
        return None
    ended = bool(nxt and _DATES.search(nxt) and "present" not in nxt.lower())
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
                if school["school"] not in {e["school"] for e in education}:
                    education.append(school)
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
