"""A small DOM and CSS-selector subset for parsing WhatsApp Web's rendered panes offline (no browser).

The live commands take a pane's outerHTML from the page and parse it here, so the saved fixture HTML in the tests
goes through exactly the code the live site does. Selectors: tag, *, #id, .class, [attr], [attr="v"], [attr^="v"],
[attr*="v"], [attr$="v"], the descendant (space) and child (>) combinators, and comma lists. `first` tries a comma
list's alternatives in order (a priority list); `select` returns every match in document order, like
querySelectorAll.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser
from typing import Iterator

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
SKIP = {"script", "style", "template", "noscript", "svg"}
BLOCK = {"div", "p", "li", "br", "tr", "section", "article", "header", "footer"}
BIDI = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    def iter(self) -> Iterator["Node"]:
        """Every descendant element, in document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def contains(self, other: "Node") -> bool:
        node = other.parent
        while node is not None:
            if node is self:
                return True
            node = node.parent
        return False


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


def build(html: str | None) -> Node:
    builder = _Builder()
    builder.feed(html or "")
    builder.close()
    return builder.root


def clean(text: str | None) -> str:
    """Whitespace collapsed, bidi marks (WhatsApp wraps phone numbers in them) removed."""
    return " ".join(BIDI.sub("", text or "").split())


def text(node: Node | None, *, lines: bool = False, skip: "set[int] | None" = None) -> str:
    """innerText-like text: emoji <img>s read as their alt; with lines, <br> and block elements break lines. `skip`:
    ids of nodes left out (a quoted block inside a message)."""
    if node is None:
        return ""
    parts: list[str] = []

    def walk(n: Node) -> None:
        if n.tag in SKIP or (skip and id(n) in skip):
            return
        if n.tag == "img":
            alt = n.attrs.get("alt", "")
            if alt and "emoji" in n.attrs.get("class", "") or (alt and len(alt) <= 4 and not alt.isascii()):
                parts.append(alt)
            return
        if n.tag == "br":
            parts.append("\n")
            return
        block = lines and n.tag in BLOCK
        if block:
            parts.append("\n")
        for child in n.children:
            if isinstance(child, str):
                parts.append(child)
            else:
                walk(child)
        if block:
            parts.append("\n")

    walk(node)
    raw = "".join(parts)
    if not lines:
        return clean(raw)
    out = [clean(line) for line in raw.split("\n")]
    return "\n".join(line for line in out if line)


# --- Selectors ----------------------------------------------------------------------------------------------------

_ATTR = re.compile(r'\[\s*([\w:-]+)\s*(?:([~^$*|]?=)\s*(?:"([^"]*)"|\'([^\']*)\'|([^\]\s]+)))?\s*\]')
_SIMPLE = re.compile(r"(#[\w-]+|\.[\w-]+)")


@dataclass(frozen=True)
class Compound:
    tag: str | None
    ids: tuple[str, ...]
    classes: tuple[str, ...]
    attrs: tuple[tuple[str, str | None, str | None], ...]


def _split(selector: str, sep: str) -> list[str]:
    """Split on `sep` outside brackets and quotes."""
    out, buf, depth, quote = [], [], 0, None
    for ch in selector:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    out.append("".join(buf))
    return [part.strip() for part in out if part.strip()]


def _compound(token: str) -> Compound:
    attrs = tuple((m[1], m[2], next((g for g in (m[3], m[4], m[5]) if g is not None), None))
                  for m in _ATTR.finditer(token))
    rest = _ATTR.sub("", token)
    tag_match = re.match(r"^([a-zA-Z][\w-]*|\*)", rest)
    tag = tag_match[1].lower() if tag_match and tag_match[1] != "*" else None
    rest = rest[tag_match.end():] if tag_match else rest
    simple = _SIMPLE.findall(rest)
    if "".join(simple) != rest:
        raise ValueError(f"unsupported selector {token!r}")
    return Compound(tag, tuple(s[1:] for s in simple if s[0] == "#"), tuple(s[1:] for s in simple if s[0] == "."),
                    attrs)


@lru_cache(maxsize=256)
def compile_selector(selector: str) -> tuple[tuple[tuple[str, Compound], ...], ...]:
    """Alternatives, each a chain of (combinator to the previous: " " or ">" or "", compound)."""
    alternatives = []
    for alt in _split(selector, ","):
        spaced = re.sub(r"\s*>\s*", " > ", alt)
        chain, combinator = [], ""
        for token in _split(spaced, " "):
            if token == ">":
                combinator = ">"
                continue
            chain.append((combinator, _compound(token)))
            combinator = " "
        alternatives.append(tuple(chain))
    return tuple(alternatives)


def _attr_ok(node: Node, name: str, op: str | None, value: str | None) -> bool:
    if name not in node.attrs:
        return False
    have = node.attrs[name]
    if op is None:
        return True
    value = value or ""
    if op == "=":
        return have == value
    if op == "^=":
        return bool(value) and have.startswith(value)
    if op == "$=":
        return bool(value) and have.endswith(value)
    if op == "*=":
        return bool(value) and value in have
    if op == "~=":
        return value in have.split()
    if op == "|=":
        return have == value or have.startswith(value + "-")
    return False


def _matches_compound(node: Node, c: Compound) -> bool:
    if node.tag == "#root" or (c.tag and node.tag != c.tag):
        return False
    if any(node.attrs.get("id") != i for i in c.ids):
        return False
    if c.classes and not set(c.classes) <= node.classes():
        return False
    return all(_attr_ok(node, *a) for a in c.attrs)


def _matches_chain(node: Node, chain: tuple[tuple[str, Compound], ...], scope: Node) -> bool:
    combinator, last = chain[-1]
    if not _matches_compound(node, last):
        return False
    if len(chain) == 1:
        return True
    rest = chain[:-1]
    parent = node.parent
    if combinator == ">":
        return parent is not None and _matches_chain(parent, rest, scope)
    while parent is not None and parent.tag != "#root":
        if _matches_chain(parent, rest, scope):
            return True
        parent = parent.parent
    return False


def matches(node: Node, selector: str) -> bool:
    return any(_matches_chain(node, chain, node) for chain in compile_selector(selector))


def select(root: Node, selector: str) -> list[Node]:
    """Every descendant of root matching any alternative, in document order (querySelectorAll; ancestors outside
    root count, as in the browser)."""
    alternatives = compile_selector(selector)
    return [n for n in root.iter() if any(_matches_chain(n, chain, root) for chain in alternatives)]


def first(root: Node, selector: str) -> Node | None:
    """The first match of the first alternative that matches anything (a priority list)."""
    for alt in _split(selector, ","):
        found = select(root, alt)
        if found:
            return found[0]
    return None


def outermost(nodes: list[Node]) -> list[Node]:
    """Nodes not inside another of `nodes`, in order."""
    keep = set(map(id, nodes))
    out = []
    for node in nodes:
        parent, inside = node.parent, False
        while parent is not None:
            if id(parent) in keep:
                inside = True
                break
            parent = parent.parent
        if not inside:
            out.append(node)
    return out


def subtree_ids(nodes: list[Node]) -> set[int]:
    out = set()
    for node in nodes:
        out.add(id(node))
        out.update(id(n) for n in node.iter())
    return out


def alternatives(selector: str) -> list[str]:
    """A comma list's selectors, in order."""
    return _split(selector, ",")
