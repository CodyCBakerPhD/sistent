"""Find images (and among them badges) in Markdown, HTML and reStructuredText.

Recognised syntaxes, all of which may occur several times on one line:

* ``[![alt](img)](href)`` and ``![alt](img)`` (inline destinations, optional ``"title"``),
* ``[![alt][ref]][ref2]`` and ``![alt][ref]`` (reference style, resolved through the ``links`` mapping; an
  unresolved reference keeps its label as the image URL),
* ``<img src="...">`` optionally wrapped in ``<a href="...">`` (attributes in any order, single or double quotes,
  tags may span lines),
* ``.. image:: url`` with an optional ``:target:`` / ``:alt:`` option line, including the ``.. |name| image::``
  substitution form.

Images inside fenced code blocks (``\\`\\`\\``` and ``~~~``) and HTML comments are ignored (an unclosed ``<!--``
hides the rest of the text, as it does when rendered). Whether an image is a *badge* is decided by :func:`is_badge`
from its URL alone.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

BadgeSyntax = Literal["md-linked", "md-image", "md-ref", "html", "rst"]


@dataclass(frozen=True)
class Badge:
    """One image found in a document (``badge`` says whether its URL looks like a status badge)."""

    alt: str
    """Alternative text (``""`` when absent)."""
    img: str
    """Image URL, whitespace removed, reference labels resolved when possible."""
    href: str | None
    """Link target when the image is wrapped in a link, else ``None``."""
    line: int
    """1-based line number of the image element itself (the ``![``, ``<img`` or ``.. image::``)."""
    syntax: BadgeSyntax
    badge: bool
    """``True`` when :func:`is_badge` accepts :attr:`img`."""


BADGE_HOSTS: frozenset[str] = frozenset(
    {
        "img.shields.io",
        "shields.io",
        "badge.fury.io",
        "codecov.io",
        "coveralls.io",
        "readthedocs.org",
        "app.readthedocs.org",
        "readthedocs.io",
        "zenodo.org",
        "results.pre-commit.ci",
        "mybinder.org",
        "pepy.tech",
        "static.pepy.tech",
        "anaconda.org",
        "img.badgesize.io",
        "badgen.net",
        "snyk.io",
        "bestpractices.coreinfrastructure.org",
        "api.codacy.com",
        "github.com",
    }
)
"""Hosts whose images are badges. ``github.com`` only counts with an Actions workflow path or a ``badge.svg`` file."""

_GITHUB_HOST = "github.com"


def _host_matches(host: str, listed: str) -> bool:
    return host == listed or host.endswith("." + listed)


def is_badge(url: str) -> bool:
    """Whether ``url`` points at a status badge.

    True when the host is (a subdomain of) one of :data:`BADGE_HOSTS` — for ``github.com`` only when the path
    contains ``/actions/workflows/`` or ends with ``badge.svg`` — or when the URL path contains ``badge``
    (case-insensitive). Relative paths and unparseable URLs are never badges unless their path says ``badge``.
    """
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    path = parts.path
    if _host_matches(host, _GITHUB_HOST):
        return "/actions/workflows/" in path or path.endswith("badge.svg")
    if any(_host_matches(host, listed) for listed in BADGE_HOSTS if listed != _GITHUB_HOST):
        return True
    return "badge" in path.lower()


# ----- patterns ----------------------------------------------------------------------------------------------------

_DEST = r"(?:\((?P<{0}>[^)]*)\)|\[(?P<{0}ref>[^\]]*)\])"
_RE_MD_LINKED = re.compile(r"\[!\[(?P<alt>[^\]]*)\]" + _DEST.format("img") + r"\]" + _DEST.format("href"))
_RE_MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]" + _DEST.format("img"))
_RE_HTML = re.compile(r"(?:<a\b(?P<a_attrs>[^>]*)>\s*)?<img\b(?P<img_attrs>[^>]*)>", re.I)
_RE_ATTR = re.compile(
    r"""\b(?P<name>src|alt|href)\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'>]+))""",
    re.I,
)
_RE_RST = re.compile(
    r"^[ \t]*\.\.[ \t]+(?:\|(?P<sub>[^|\n]*)\|[ \t]+)?image::[ \t]*(?P<url>[^\n]*?)[ \t]*$"
    r"(?P<opts>(?:\n[ \t]+:[A-Za-z-]+:[^\n]*)*)",
    re.M,
)
_RE_RST_OPTION = re.compile(r"^[ \t]+:(?P<name>[A-Za-z-]+):[ \t]*(?P<value>.*?)[ \t]*$", re.M)
_RE_FENCE = re.compile(r"^[ \t]*(?P<fence>`{3,}|~{3,})")
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_TITLE_SPLIT = re.compile(r"""\s+(?=["'(])""")


def _mask(text: str) -> str:
    """Blank out fenced code blocks and HTML comments, keeping every newline so offsets map to the same lines."""
    out: list[str] = []
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if fence_char:
            out.append("")
            if len(stripped) >= fence_len and stripped == fence_char * len(stripped):
                fence_char = ""
            continue
        match = _RE_FENCE.match(line)
        if match:
            fence = match.group("fence")
            fence_char, fence_len = fence[0], len(fence)
            out.append("")
            continue
        out.append(line)
    masked = _RE_COMMENT.sub(lambda m: "\n" * m.group().count("\n"), "\n".join(out))
    unclosed = masked.find("<!--")
    if unclosed >= 0:  # an unclosed comment hides the rest of the document, as it does when rendered
        masked = masked[:unclosed] + "\n" * masked.count("\n", unclosed)
    return masked


def _destination(raw: str) -> str:
    """URL part of a Markdown link destination: ``<...>`` unwrapped, optional title dropped, whitespace removed."""
    s = raw.strip()
    if s.startswith("<") and ">" in s:
        s = s[1 : s.index(">")]
    else:
        s = _RE_TITLE_SPLIT.split(s, maxsplit=1)[0]
    return "".join(s.split())


def _label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _attr(attrs: str, name: str) -> str | None:
    for match in _RE_ATTR.finditer(attrs):
        if match.group("name").lower() == name:
            value = match.group("dq") or match.group("sq") or match.group("bare") or ""
            return " ".join(value.split()) if name == "alt" else "".join(value.split())
    return None


def _line_of(newlines: list[int], pos: int) -> int:
    return bisect.bisect_right(newlines, pos) + 1


def parse_badges(text: str, *, links: Mapping[str, str] | None = None) -> list[Badge]:
    """All images of ``text`` in document order, each flagged with :func:`is_badge`.

    ``links`` maps casefolded reference labels to URLs (as collected from ``[label]: url`` definitions) and resolves
    reference-style images; without it (or for unknown labels) the label itself is kept as the URL.
    """
    resolved = {_label(k): v for k, v in (links or {}).items()}
    masked = _mask(text)
    newlines = [i for i, ch in enumerate(masked) if ch == "\n"]
    found: list[tuple[int, int, Badge]] = []

    def resolve(inline: str | None, ref: str | None, alt: str) -> tuple[str, bool]:
        if inline is not None:
            return _destination(inline), False
        label = _label(ref or "") or _label(alt)
        return "".join(resolved.get(label, ref or alt).split()), True

    for match in _RE_MD_LINKED.finditer(masked):
        alt = match.group("alt")
        img, img_ref = resolve(match.group("img"), match.group("imgref"), alt)
        link, href_ref = resolve(match.group("href"), match.group("hrefref"), alt)
        href: str | None = link or None
        syntax: BadgeSyntax = "md-ref" if (img_ref or href_ref) else "md-linked"
        badge = Badge(alt, img, href, _line_of(newlines, match.start()), syntax, is_badge(img))
        found.append((match.start(), match.end(), badge))

    for match in _RE_MD_IMAGE.finditer(masked):
        alt = match.group("alt")
        img, img_ref = resolve(match.group("img"), match.group("imgref"), alt)
        syntax = "md-ref" if img_ref else "md-image"
        badge = Badge(alt, img, None, _line_of(newlines, match.start()), syntax, is_badge(img))
        found.append((match.start(), match.end(), badge))

    for match in _RE_HTML.finditer(masked):
        img_attrs = match.group("img_attrs")
        img = _attr(img_attrs, "src") or ""
        alt = _attr(img_attrs, "alt") or ""
        a_attrs = match.group("a_attrs")
        href = _attr(a_attrs, "href") if a_attrs is not None else None
        img_pos = match.start("img_attrs") - len("<img")
        badge = Badge(alt, img, href or None, _line_of(newlines, img_pos), "html", is_badge(img))
        found.append((match.start(), match.end(), badge))

    for match in _RE_RST.finditer(masked):
        img = "".join(match.group("url").split())
        options = {m.group("name").lower(): m.group("value") for m in _RE_RST_OPTION.finditer(match.group("opts"))}
        alt = options.get("alt") or (match.group("sub") or "").strip()
        href = "".join(options.get("target", "").split()) or None
        badge = Badge(alt, img, href, _line_of(newlines, match.start()), "rst", is_badge(img))
        found.append((match.start(), match.end(), badge))

    found.sort(key=lambda entry: (entry[0], -entry[1]))
    out: list[Badge] = []
    consumed_until = -1
    for start, end, badge in found:
        if start < consumed_until:
            continue  # nested inside an already accepted match (e.g. the ![..] inside [![..](..)](..))
        out.append(badge)
        consumed_until = end
    return out
