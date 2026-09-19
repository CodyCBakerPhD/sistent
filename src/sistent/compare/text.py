"""Text normalisation, similarity and identity substitution.

Rules of thumb encoded here (measured on real READMEs / AGENTS.md files):

* never run character-level ``SequenceMatcher`` on section bodies: with ``autojunk`` it collapses on anything longer
  than ~200 characters, and even without it punishes pure additions. Bodies are compared with word-shingle Jaccard
  (order-insensitive "is this the same content?") plus line-level opcodes (which side added/removed lines).
* character-level :func:`ratio` (``autojunk=False``) is only for short strings: rules, headings, badge parameters.
* every string is passed through a :class:`Substituter` before it is stored in a snapshot, so templated sentences that
  embed the package/org/branch name are not reported as drift.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Sequence
from typing import Literal

from sistent.model import Identity, StaleHit

NAME = "{{name}}"
ORG = "{{org}}"
BRANCH = "{{branch}}"

_QUOTES = str.maketrans(
    {
        "\u2018": "'",  # left single quotation mark
        "\u2019": "'",  # right single quotation mark
        "\u201a": "'",  # single low-9 quotation mark
        "\u201b": "'",  # single high-reversed-9 quotation mark
        "\u201c": '"',  # left double quotation mark
        "\u201d": '"',  # right double quotation mark
        "\u201e": '"',  # double low-9 quotation mark
        "\u201f": '"',  # double high-reversed-9 quotation mark
        "\u00b4": "'",  # acute accent
        "\u2032": "'",  # prime
        "\u2033": '"',  # double prime
    }
)

_DROP_CATEGORIES = {"So", "Sk", "Cf"}
_DROP_CHARS = {"️", "‍"}  # variation selector 16, zero width joiner

# ----- inline markup -----------------------------------------------------------------------------------------------

_RE_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_RE_IMAGE_REF = re.compile(r"!\[[^\]]*\]\[[^\]]*\]")
_RE_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_RE_LINK_REF = re.compile(r"\[([^\]]*)\]\[[^\]]*\]")
_RE_FOOTNOTE = re.compile(r"\[\^[^\]]+\]")
_RE_AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
_RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
_RE_CODE_SPAN = re.compile(r"(`{1,3})(.*?)\1")
_RE_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_RE_EM_STAR = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_RE_EM_UNDERSCORE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_RE_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def strip_inline_markup(text: str) -> str:
    """Remove inline Markdown/HTML markup, keeping the human-readable content.

    Links become their text, images and footnote references are dropped, emphasis/code/strike markers are removed
    (the wrapped content stays), HTML tags and comments are removed.
    """
    s = _RE_HTML_COMMENT.sub("", text)
    s = _RE_IMAGE.sub("", s)
    s = _RE_IMAGE_REF.sub("", s)
    s = _RE_LINK.sub(r"\1", s)
    s = _RE_LINK_REF.sub(r"\1", s)
    s = _RE_FOOTNOTE.sub("", s)
    s = _RE_AUTOLINK.sub(r"\1", s)
    s = _RE_HTML_TAG.sub("", s)
    s = _RE_CODE_SPAN.sub(r"\2", s)
    s = _RE_STRONG.sub(r"\2", s)
    s = _RE_EM_STAR.sub(r"\1", s)
    s = _RE_EM_UNDERSCORE.sub(r"\1", s)
    return _RE_STRIKE.sub(r"\1", s)


def _drop_symbols(text: str) -> str:
    return "".join(ch for ch in text if ch not in _DROP_CHARS and unicodedata.category(ch) not in _DROP_CATEGORIES)


def _finish(text: str) -> str:
    s = unicodedata.normalize("NFKC", text)
    s = s.translate(_QUOTES)
    s = s.casefold()
    s = " ".join(s.split())
    return s.rstrip(".:;").strip()


def normalize_text(text: str) -> str:
    """Canonical form of a rule / paragraph for comparison.

    Inline markup stripped, NFKC, straight quotes, casefold, whitespace collapsed, trailing ``.:;`` removed.
    """
    return _finish(strip_inline_markup(text))


_RE_ANCHOR_ID = re.compile(r"\s*\{#[^}]*\}\s*$")
_RE_TRAILING_HASHES = re.compile(r"\s+#+\s*$")
_RE_NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*[.):]?|[ivxlc]+[.):])\s+", re.I)
_RE_STEP = re.compile(r"^\s*step\s+\d+\s*[:.\-\u2013\u2014]\s*", re.I)
_RE_SHORTCODE = re.compile(r":[a-z0-9_+-]+:")


def normalize_heading(text: str) -> str:
    """Canonical form of a heading used as section identity.

    HTML/anchors removed, ``{#id}`` and trailing ``#`` stripped, inline markup stripped, leading numbering
    (``1``, ``2.3``, ``IV.``, ``Step 3:``) removed, emoji/shortcodes/symbols dropped, then the same finishing as
    :func:`normalize_text`. The empty string means "unmatchable" (caller decides).
    """
    s = _RE_HTML_COMMENT.sub("", text)
    s = _RE_HTML_TAG.sub("", s)
    s = _RE_ANCHOR_ID.sub("", s)
    s = _RE_TRAILING_HASHES.sub("", s)
    s = strip_inline_markup(s)
    s = _RE_NUMBERING.sub("", s)
    s = _RE_STEP.sub("", s)
    s = _RE_SHORTCODE.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    s = _drop_symbols(s)
    return _finish(s)


# ----- paragraphs and shingles -------------------------------------------------------------------------------------

_RE_BLOCK_START = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s|```|~~~|\||>)")


def unwrap(lines: Sequence[str]) -> list[str]:
    """Join hard-wrapped lines into paragraphs.

    A paragraph ends at a blank line or where a new block (list item, heading, fence, table row, block quote)
    starts. Leading/trailing whitespace of every line is dropped.
    """
    paragraphs: list[str] = []
    buf: list[str] = []
    for line in lines:
        if not line.strip():
            if buf:
                paragraphs.append(" ".join(buf))
                buf = []
            continue
        if buf and _RE_BLOCK_START.match(line):
            paragraphs.append(" ".join(buf))
            buf = []
        buf.append(line.strip())
    if buf:
        paragraphs.append(" ".join(buf))
    return paragraphs


def shingles(text: str, n: int = 3) -> frozenset[tuple[str, ...]]:
    """Word ``n``-grams of ``text``; texts shorter than ``n`` words yield their words as 1-grams."""
    words = text.split()
    if len(words) < n:
        return frozenset((w,) for w in words)
    return frozenset(tuple(words[i : i + n]) for i in range(len(words) - n + 1))


def jaccard(a: frozenset[tuple[str, ...]], b: frozenset[tuple[str, ...]]) -> float:
    """Jaccard similarity; two empty sets are identical (1.0)."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def similarity(a: str, b: str, n: int = 3) -> float:
    """Shingle Jaccard of two texts (order-insensitive, O(n))."""
    return jaccard(shingles(a, n), shingles(b, n))


LineClass = Literal["equal", "insert", "delete", "mixed"]


def classify_lines(main: Sequence[str], other: Sequence[str]) -> LineClass:
    """Which side changed, at line granularity.

    ``insert`` = ``other`` only adds lines (upstream candidate); ``delete`` = ``other`` only drops lines (downstream
    drift); ``mixed`` = both / replaced lines; ``equal`` = identical sequences.
    """
    matcher = difflib.SequenceMatcher(None, list(main), list(other), autojunk=False)
    tags = {tag for tag, *_ in matcher.get_opcodes() if tag != "equal"}
    if not tags:
        return "equal"
    if tags == {"insert"}:
        return "insert"
    if tags == {"delete"}:
        return "delete"
    return "mixed"


def line_counts(main: Sequence[str], other: Sequence[str]) -> tuple[int, int]:
    """``(added, removed)`` line counts of ``other`` relative to ``main``."""
    matcher = difflib.SequenceMatcher(None, list(main), list(other), autojunk=False)
    added = removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            added += j2 - j1
        if tag in ("delete", "replace"):
            removed += i2 - i1
    return added, removed


def ratio(a: str, b: str, *, cutoff: float = 0.0) -> float:
    """Character-level similarity for *short* strings (rules, headings); ``autojunk`` disabled.

    With ``cutoff`` the cheap upper bounds are checked first and ``0.0`` is returned early when they fall below it.
    """
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    if cutoff > 0.0 and (matcher.real_quick_ratio() < cutoff or matcher.quick_ratio() < cutoff):
        return 0.0
    return matcher.ratio()


def unified_diff(
    a: Sequence[str],
    b: Sequence[str],
    *,
    fromfile: str,
    tofile: str,
    context: int = 3,
    max_lines: int = 200,
) -> str:
    """Unified diff of two line sequences as one string, truncated to ``max_lines``."""
    lines = list(difflib.unified_diff(list(a), list(b), fromfile=fromfile, tofile=tofile, n=context, lineterm=""))
    if len(lines) > max_lines:
        rest = len(lines) - max_lines
        lines = [*lines[:max_lines], f"... (truncated, {rest} more lines)"]
    return "\n".join(lines)


def excerpt(text: str, start: int, end: int, *, width: int = 40) -> str:
    """A one-line window of ``text`` around ``[start, end)``."""
    lo = max(0, start - width)
    hi = min(len(text), end + width)
    snippet = " ".join(text[lo:hi].split())
    prefix = "..." if lo > 0 else ""
    suffix = "..." if hi < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


# ----- identity substitution ---------------------------------------------------------------------------------------

_WORD = "[A-Za-z0-9]"


def _bounded(alternation: str) -> str:
    return rf"(?<!{_WORD})(?:{alternation})(?!{_WORD})"


def _alternation(values: Sequence[str]) -> str:
    return "|".join(re.escape(v) for v in sorted(set(values), key=lambda v: (-len(v), v)))


class Substituter:
    """Replace a repository's own identity with placeholders; record mentions of other repositories.

    Own aliases -> ``{{name}}``, org -> ``{{org}}``, ``vars`` extras -> ``{{key}}``, and the default branch ->
    ``{{branch}}`` (only in URL-like contexts such as ``/blob/<b>/`` or ``branch=<b>`` so the word "main" in prose is
    untouched). Boundaries are ``[^A-Za-z0-9]`` so ``pkg[extra]``, ``pkg.git`` and ``pkg_logo.png`` all match.

    After own substitution, every *foreign* identity's aliases (>= 4 characters, not also an own alias) are searched;
    a match is recorded in :attr:`hits` as a :class:`StaleHit` and the text is left unchanged.

    One instance is meant to serve one ``(repo, aspect)`` extraction; it is not thread-safe.
    """

    def __init__(
        self,
        own: Identity,
        foreign: Sequence[Identity] = (),
        *,
        enabled: bool = True,
        stale: bool = True,
    ) -> None:
        self.own = own
        self.enabled = enabled
        self.stale = stale
        self.hits: list[StaleHit] = []
        self.applied: set[str] = set()

        aliases = [a for a in own.aliases if a]
        alt = _alternation(aliases)
        self._name_re = re.compile(_bounded(alt), re.IGNORECASE) if alt else None
        org = own.org or ""
        self._org_re = re.compile(_bounded(re.escape(org)), re.IGNORECASE) if org else None
        self._slug_re = (
            re.compile(rf"(?<!{_WORD})(?:{re.escape(org)})/(?:{alt})(?!{_WORD})", re.IGNORECASE)
            if org and alt
            else None
        )
        self._extra = [
            (key, re.compile(_bounded(re.escape(value)), re.IGNORECASE))
            for key, value in own.extra.items()
            if value and key not in ("name", "org", "branch")
        ]
        branch = own.branch or ""
        self._branch_re = (
            re.compile(
                r"(?P<pre>/blob/|/tree/|/raw/|/en/|branch=|version=|ref=|\.git@|(?<![A-Za-z0-9])@)"
                rf"(?:{re.escape(branch)})(?![A-Za-z0-9._-])",
                re.IGNORECASE,
            )
            if branch
            else None
        )
        own_folded = {a.casefold() for a in aliases}
        self._foreign: list[tuple[str, re.Pattern[str]]] = []
        for other in foreign:
            if other.name == own.name:
                continue
            candidates = [a for a in other.aliases if len(a) >= 4 and a.casefold() not in own_folded]
            if candidates:
                self._foreign.append((other.name, re.compile(_bounded(_alternation(candidates)), re.IGNORECASE)))

    def __call__(self, text: str, *, where: str = "") -> str:
        if not text:
            return text
        if self.enabled:
            text = self._substitute(text)
        if self.stale:
            self._scan_foreign(text, where)
        return text

    def _substitute(self, text: str) -> str:
        def replace_name(match: re.Match[str]) -> str:
            self.applied.add(match.group(0))
            return NAME

        def replace_slug(match: re.Match[str]) -> str:
            self.applied.add(match.group(0))
            return f"{ORG}/{NAME}"

        if self._slug_re is not None:
            text = self._slug_re.sub(replace_slug, text)
        if self._name_re is not None:
            text = self._name_re.sub(replace_name, text)
        if self._org_re is not None:
            text = self._org_re.sub(ORG, text)
        for key, pattern in self._extra:
            text = pattern.sub("{{" + key + "}}", text)
        if self._branch_re is not None:
            text = self._branch_re.sub(lambda m: m.group("pre") + BRANCH, text)
        return text

    def _scan_foreign(self, text: str, where: str) -> None:
        for other_repo, pattern in self._foreign:
            for match in pattern.finditer(text):
                self.hits.append(
                    StaleHit(
                        where=where,
                        alias=match.group(0),
                        other_repo=other_repo,
                        excerpt=excerpt(text, match.start(), match.end()),
                    )
                )

    def many(self, values: Sequence[str], *, where: str = "") -> list[str]:
        return [self(v, where=where) for v in values]


class NullSubstituter(Substituter):
    """A substituter that changes nothing (used for tests and ``substitute = false``)."""

    def __init__(self) -> None:
        super().__init__(Identity(name="", aliases=()), enabled=False, stale=False)
