"""Line-based Markdown structure parser (no third-party library).

:func:`parse_document` splits a document into a flat list of :class:`Section` objects (the first one is always the
*preamble*, the content before the first heading) and, per section, extracts unwrapped paragraphs, lists (with
nesting, continuation lines and task boxes), code blocks, tables and image URLs. Reference link definitions and all
images/badges of the document are collected on the :class:`Document`.

The parser is deliberately pragmatic (CommonMark-ish, line oriented) and never raises on odd input; a
:class:`ParseError` is only raised for absurd input (text above :data:`MAX_CHARS`, NUL bytes). All line numbers are
1-based.

Notable choices: HTML comments are removed before block parsing and, as in HTML/CommonMark, an *unclosed* ``<!--``
hides everything up to the end of the document; a setext underline promotes only the paragraph line directly above
it; paragraphs are hard-wrapped lines joined with single spaces, with block boundaries decided by this scanner.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sistent.parsers import ParseError
from sistent.parsers.badges import Badge, parse_badges

MAX_CHARS = 5_000_000
"""Texts longer than this many characters are refused with :class:`ParseError`."""


# ----- data --------------------------------------------------------------------------------------------------------


@dataclass
class Item:
    """One list item."""

    text: str
    """Marker and task box stripped; continuation lines joined with a single space; nested items excluded."""
    raw: str
    """The first line as written (marker included), surrounding whitespace stripped."""
    line: int
    """1-based line number of the first line."""
    ordered: bool
    checkbox: bool | None
    """``None`` when the item has no task box, otherwise whether it is checked."""
    children: list[Item] = field(default_factory=list)


@dataclass
class ListBlock:
    """A top-level list of a section body (nested lists live in :attr:`Item.children`)."""

    ordered: bool
    items: list[Item]
    line: int
    """1-based line number of the first item."""


@dataclass
class CodeBlock:
    """A fenced (``fenced=True``) or indented code block."""

    info: str
    """Info string of a fenced block (``"python"``), ``""`` otherwise."""
    lines: list[str]
    """Content lines with the fence/indentation removed."""
    line: int
    """1-based line number of the opening fence (or first code line)."""
    fenced: bool


@dataclass
class Section:
    """A heading and its *own* body (up to the next heading of any level)."""

    level: int
    """``0`` for the preamble, ``1``..``6`` otherwise."""
    heading_raw: str
    """Heading text as written (ATX markers / setext underline and closing ``#`` removed), ``""`` for the preamble."""
    line: int
    """1-based line number of the heading (``0`` for the preamble)."""
    body_lines: list[str] = field(default_factory=list)
    """Raw lines after the heading up to (not including) the next heading; HTML comments already removed."""
    paragraphs: list[str] = field(default_factory=list)
    """Unwrapped prose paragraphs (no list items, code, tables, comments, image/tag-only lines); ``>`` stripped."""
    lists: list[ListBlock] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    tables: list[list[str]] = field(default_factory=list)
    """Each table as its raw row lines."""
    images: list[str] = field(default_factory=list)
    """Image URLs found in the own body (Markdown and HTML), in document order."""


@dataclass
class SectionNode:
    """A section with the sections nested under it (by heading level)."""

    section: Section
    children: list[SectionNode] = field(default_factory=list)


@dataclass
class Document:
    """A parsed Markdown document."""

    source: str
    lines: list[str]
    """The original lines (line endings normalised, front matter and comments still present)."""
    front_matter: str | None
    """Raw text between the ``---`` / ``+++`` fences at line 1, ``None`` when there is none."""
    sections: list[Section]
    """Flat, in document order; ``sections[0]`` is always the (possibly empty) preamble."""
    links: dict[str, str]
    """Reference link definitions: casefolded label -> URL."""
    badges: list[Badge]
    """Every image of the document (see :func:`sistent.parsers.badges.parse_badges`), ``badge`` flag included."""

    @property
    def title(self) -> str | None:
        """``heading_raw`` of the first heading when it is the document's only level-1 heading, else ``None``."""
        headings = self.sections[1:]
        if not headings or headings[0].level != 1:
            return None
        if sum(1 for section in headings if section.level == 1) != 1:
            return None
        return headings[0].heading_raw

    def tree(self) -> list[SectionNode]:
        """Sections nested by level (a section is a child of the closest preceding shallower one).

        The preamble is not part of the tree; it stays at ``sections[0]``.
        """
        roots: list[SectionNode] = []
        stack: list[SectionNode] = []
        for section in self.sections[1:]:
            node = SectionNode(section)
            while stack and stack[-1].section.level >= section.level:
                stack.pop()
            (stack[-1].children if stack else roots).append(node)
            stack.append(node)
        return roots


# ----- line classification -----------------------------------------------------------------------------------------

_RE_ATX = re.compile(r"^ {0,3}(?P<marks>#{1,6})(?:[ \t]+(?P<text>.*?))?[ \t]*$")
_RE_ATX_CLOSE = re.compile(r"(?:^|[ \t]+)#+[ \t]*$")
_RE_SETEXT = re.compile(r"^ {0,3}(?P<marks>=+|-+)[ \t]*$")
_RE_THEMATIC = re.compile(r"^ {0,3}(?P<char>[-*_])(?:[ \t]*(?P=char)){2,}[ \t]*$")
_RE_FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_RE_LIST_ITEM = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-*+]|\d{1,9}[.)])(?:(?P<space>[ \t]+)(?P<text>.*)|[ \t]*$)")
_RE_TASK = re.compile(r"^\[(?P<state>[ xX])\](?:[ \t]+|$)")
_RE_BLOCKQUOTE = re.compile(r"^ {0,3}> ?")
_RE_REF_DEF = re.compile(
    r"""^ {0,3}\[(?P<label>[^\]]+)\]:[ \t]*(?:<(?P<angle>[^>]*)>|(?P<url>\S+))"""
    r"""(?:[ \t]+(?:"[^"]*"|'[^']*'|\([^)]*\)))?[ \t]*$"""
)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_HTML_START = re.compile(r"^[ \t]*<[A-Za-z/]")

_MD_IMAGE = r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])"
_RE_MEDIA_NOISE = re.compile(
    "|".join(
        (
            r"\[" + _MD_IMAGE + r"\](?:\([^)]*\)|\[[^\]]*\])",  # [![alt](img)](href) and reference variants
            _MD_IMAGE,
            r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>",  # any HTML tag (not autolinks such as <https://...>)
            r"&nbsp;",
        )
    )
)


def _indent_width(line: str) -> int:
    """Columns of leading whitespace (tabs advance to the next multiple of 4)."""
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _column(line: str, index: int) -> int:
    """Column of ``line[index]`` with tabs expanded."""
    width = 0
    for ch in line[:index]:
        width = width + (4 - width % 4) if ch == "\t" else width + 1
    return width


def _dedent(line: str, cols: int) -> str:
    """Remove up to ``cols`` columns of leading whitespace."""
    width = 0
    k = 0
    while k < len(line) and width < cols:
        ch = line[k]
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
        k += 1
    return line[k:]


def _fence_open(line: str) -> re.Match[str] | None:
    match = _RE_FENCE_OPEN.match(line)
    if match is None:
        return None
    if match.group("fence")[0] == "`" and "`" in match.group("info"):
        return None  # a backtick fence's info string may not contain backticks (that is an inline code span)
    return match


def _closes_fence(line: str, char: str, length: int, max_indent: int) -> bool:
    stripped = line.strip()
    return len(stripped) >= length and stripped == char * len(stripped) and _indent_width(line) <= max_indent


def _is_media_only(line: str) -> bool:
    """Whether the line consists solely of images, linked images and HTML tags (no prose)."""
    return not _RE_MEDIA_NOISE.sub("", line).strip()


def _strip_atx_close(text: str) -> str:
    return _RE_ATX_CLOSE.sub("", text).strip()


def _label(label: str) -> str:
    return " ".join(label.split()).casefold()


def _strip_blockquote(line: str) -> str:
    while True:
        match = _RE_BLOCKQUOTE.match(line)
        if match is None or match.end() == 0:
            return line
        line = line[match.end() :]


def _is_delimiter_row(line: str) -> bool:
    stripped = line.strip()
    return "|" in stripped and "-" in stripped and re.fullmatch(r"[\s|:-]+", stripped) is not None


def _table_end(lines: list[str], i: int) -> int:
    """End index (exclusive) of the table starting at ``i``, or ``0`` when there is none."""
    n = len(lines)
    first = lines[i].strip()
    if first.startswith("|"):
        j = i
        while j < n and lines[j].strip().startswith("|"):
            j += 1
        return j
    if "|" in first and i + 1 < n and _is_delimiter_row(lines[i + 1]):
        j = i + 2
        while j < n and lines[j].strip() and "|" in lines[j]:
            j += 1
        return j
    return 0


# ----- block readers -----------------------------------------------------------------------------------------------


def _read_fenced(lines: list[str], i: int, match: re.Match[str], *, max_close_indent: int) -> tuple[CodeBlock, int]:
    fence = match.group("fence")
    char, length = fence[0], len(fence)
    indent = _indent_width(match.group("indent"))
    body: list[str] = []
    j = i + 1
    while j < len(lines):
        if _closes_fence(lines[j], char, length, max_close_indent):
            j += 1
            break
        body.append(_dedent(lines[j], indent))
        j += 1
    return CodeBlock(info=match.group("info").strip(), lines=body, line=i + 1, fenced=True), j


def _read_indented(lines: list[str], i: int, cols: int) -> tuple[CodeBlock, int]:
    body: list[str] = []
    j = i
    while j < len(lines) and (not lines[j].strip() or _indent_width(lines[j]) >= cols):
        body.append(_dedent(lines[j], cols))
        j += 1
    while body and not body[-1].strip():
        body.pop()
        j -= 1
    return CodeBlock(info="", lines=body, line=i + 1, fenced=False), j


@dataclass
class _Open:
    item: Item
    marker_col: int
    content_col: int


def _append_text(item: Item, part: str) -> None:
    item.text = f"{item.text} {part}" if item.text else part


def _interrupts_lazy(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith(("|", ">")) or _RE_REF_DEF.match(line) is not None or _is_media_only(line)


def _can_start_list(match: re.Match[str], *, in_paragraph: bool) -> bool:
    """CommonMark: only bullets and ``1.`` items with text may interrupt a paragraph."""
    if not in_paragraph:
        return True
    marker = match.group("marker")
    if not (match.group("text") or "").strip():
        return False
    return not marker[0].isdigit() or marker[:-1] == "1"


def _parse_list(lines: list[str], start: int) -> tuple[list[ListBlock | CodeBlock], int]:
    """Consume the list starting at ``start``; returns its blocks (lists, embedded code) and the next line index."""
    n = len(lines)
    blocks: list[ListBlock | CodeBlock] = []
    stack: list[_Open] = []
    current: ListBlock | None = None
    blank = False
    i = start
    while i < n:
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            blank = True
            i += 1
            continue
        indent = _indent_width(line)
        if _RE_THEMATIC.match(line) or _RE_ATX.match(line):
            break
        target = next((entry for entry in reversed(stack) if entry.marker_col + 2 <= indent), None)
        item_match = _RE_LIST_ITEM.match(line)
        code_like = blank and target is not None and indent >= target.content_col + 4
        if item_match is not None and not code_like:
            while stack and indent < stack[-1].marker_col + 2:
                stack.pop()
            marker = item_match.group("marker")
            ordered = marker[0].isdigit()
            text = (item_match.group("text") or "").strip()
            checkbox: bool | None = None
            task = _RE_TASK.match(text)
            if task is not None:
                checkbox = task.group("state") != " "
                text = text[task.end() :].strip()
            if item_match.group("text") is not None:
                content_col = _column(line, item_match.start("text"))
                if content_col - indent - len(marker) > 4:
                    content_col = indent + len(marker) + 1
            else:
                content_col = indent + len(marker) + 1
            item = Item(text=text, raw=stripped, line=i + 1, ordered=ordered, checkbox=checkbox)
            if stack:
                stack[-1].item.children.append(item)
            else:
                if current is None or current.ordered != ordered:
                    current = ListBlock(ordered=ordered, items=[], line=i + 1)
                    blocks.append(current)
                current.items.append(item)
            stack.append(_Open(item, indent, content_col))
            blank = False
            i += 1
            continue

        fence = _fence_open(line)
        if fence is not None:
            if indent < 2:
                break  # an unindented fence ends the list; the caller reads it
            block, i = _read_fenced(lines, i, fence, max_close_indent=indent + 3)
            blocks.append(block)
            blank = False
            continue

        if blank:
            if target is None:
                break  # blank line followed by an unindented non-item line: the list is over
            while stack and stack[-1] is not target:
                stack.pop()
            if indent >= target.content_col + 4:
                block, i = _read_indented(lines, i, target.content_col + 4)
                blocks.append(block)
            else:
                _append_text(target.item, stripped)
                i += 1
            blank = False
            continue

        if indent < 2 and _interrupts_lazy(line):
            break
        _append_text(stack[-1].item, stripped)
        i += 1
    return blocks, i


# ----- document scanner --------------------------------------------------------------------------------------------


class _Scanner:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.n = len(lines)
        self.sections: list[Section] = [Section(level=0, heading_raw="", line=0)]
        self.spans: list[list[int]] = [[0, self.n]]  # 0-based [body start, body end) per section
        self.links: dict[str, str] = {}
        self.para: list[str] = []
        self.para_quoted = False
        self.in_html = False

    @property
    def section(self) -> Section:
        return self.sections[-1]

    def run(self) -> None:
        i = 0
        while i < self.n:
            i = self.step(i)
        self.flush()
        for section, (start, end) in zip(self.sections, self.spans, strict=True):
            section.body_lines = self.lines[start:end]

    def flush(self) -> None:
        if self.para:
            self.section.paragraphs.append(" ".join(self.para))
            self.para = []
        self.para_quoted = False

    def open_section(self, level: int, raw: str, *, heading_index: int, body_start: int) -> None:
        self.flush()
        self.spans[-1][1] = heading_index
        self.sections.append(Section(level=level, heading_raw=raw, line=heading_index + 1))
        self.spans.append([body_start, self.n])
        self.in_html = False

    def add(self, block: ListBlock | CodeBlock) -> None:
        if isinstance(block, ListBlock):
            self.section.lists.append(block)
        else:
            self.section.code_blocks.append(block)

    def step(self, i: int) -> int:
        line = self.lines[i]
        stripped = line.strip()
        if not stripped:
            self.flush()
            self.in_html = False
            return i + 1
        indent = _indent_width(line)

        fence = _fence_open(line)
        if fence is not None and indent <= 3:
            self.flush()
            block, end = _read_fenced(self.lines, i, fence, max_close_indent=3)
            self.section.code_blocks.append(block)
            return end

        atx = _RE_ATX.match(line)
        if atx is not None:
            heading = _strip_atx_close(atx.group("text") or "")
            self.open_section(len(atx.group("marks")), heading, heading_index=i, body_start=i + 1)
            return i + 1

        if self.para and not self.para_quoted and _RE_SETEXT.match(line):
            setext = self.para.pop()
            level = 1 if stripped.startswith("=") else 2
            self.open_section(level, setext, heading_index=i - 1, body_start=i + 1)
            return i + 1

        if _RE_THEMATIC.match(line):
            self.flush()
            return i + 1

        if indent >= 4 and not self.in_html:
            if self.para:
                self.para.append(stripped)  # lazy continuation of the paragraph
                return i + 1
            block, end = _read_indented(self.lines, i, 4)
            self.section.code_blocks.append(block)
            return end

        item = _RE_LIST_ITEM.match(line)
        if item is not None and indent <= 3 and _can_start_list(item, in_paragraph=bool(self.para)):
            self.flush()
            blocks, end = _parse_list(self.lines, i)
            for list_or_code in blocks:
                self.add(list_or_code)
            return end

        table_end = _table_end(self.lines, i)
        if table_end:
            self.flush()
            self.section.tables.append(self.lines[i:table_end])
            return table_end

        ref = _RE_REF_DEF.match(line)
        if ref is not None:
            self.flush()
            url = ref.group("angle") if ref.group("angle") is not None else ref.group("url")
            self.links.setdefault(_label(ref.group("label")), url)
            return i + 1

        if _RE_HTML_START.match(line):
            self.in_html = True
        if _is_media_only(line):
            self.flush()
            return i + 1

        quoted = _RE_BLOCKQUOTE.match(line) is not None
        text = _strip_blockquote(line).strip() if quoted else stripped
        if not text:
            self.flush()  # a bare ">" is a blank line inside the quote
            return i + 1
        self.para.append(text)
        self.para_quoted = quoted
        return i + 1


# ----- preprocessing -----------------------------------------------------------------------------------------------


def _extract_front_matter(work: list[str]) -> str | None:
    """Blank the front matter lines in ``work`` (keeping line numbers) and return their raw content."""
    if not work:
        return None
    fence = work[0].rstrip()
    if fence not in ("---", "+++"):
        return None
    closers = ("---", "...") if fence == "---" else ("+++",)
    for j in range(1, len(work)):
        if work[j].rstrip() in closers:
            content = "\n".join(work[1:j])
            for k in range(j + 1):
                work[k] = ""
            return content
    return None


def _fenced_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        match = _fence_open(lines[i])
        if match is None:
            i += 1
            continue
        fence = match.group("fence")
        j = i + 1
        while j < len(lines) and not _closes_fence(lines[j], fence[0], len(fence), _indent_width(lines[i]) + 3):
            j += 1
        ranges.append((i, min(j + 1, len(lines))))
        i = j + 1
    return ranges


def _strip_comments(work: list[str]) -> None:
    """Remove ``<!-- ... -->`` (possibly spanning lines) outside fenced code, preserving line numbers.

    An unclosed ``<!--`` hides the rest of the document, as it does when rendered.
    """

    def strip_run(start: int, end: int) -> bool:
        """Strip one run of non-fenced lines; ``True`` when it ended inside an unclosed comment."""
        if start >= end:
            return False
        joined = "\n".join(work[start:end])
        if "<!--" not in joined:
            return False
        cleaned = _RE_COMMENT.sub(lambda m: "\n" * m.group().count("\n"), joined)
        unclosed = cleaned.find("<!--")
        if unclosed >= 0:
            cleaned = cleaned[:unclosed] + "\n" * cleaned.count("\n", unclosed)
        work[start:end] = cleaned.split("\n")
        return unclosed >= 0

    runs: list[tuple[int, int]] = []
    position = 0
    for fence_start, fence_end in _fenced_ranges(work):
        runs.append((position, fence_start))
        position = fence_end
    runs.append((position, len(work)))
    for start, end in runs:
        if strip_run(start, end):
            for k in range(end, len(work)):
                work[k] = ""
            return


def parse_document(text: str, *, source: str = "") -> Document:
    """Parse Markdown ``text`` into a :class:`Document`.

    ``source`` labels the text (a file path) in error messages and on the result. Raises :class:`ParseError` only
    for absurd input (longer than :data:`MAX_CHARS` characters or containing NUL bytes).
    """
    if len(text) > MAX_CHARS:
        raise ParseError(source, f"text too large: {len(text):,} characters (limit {MAX_CHARS:,})")
    if "\x00" in text:
        raise ParseError(source, "binary content (NUL byte)")
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    work = list(lines)
    front_matter = _extract_front_matter(work)
    _strip_comments(work)

    scanner = _Scanner(work)
    scanner.run()
    badges = parse_badges("\n".join(work), links=scanner.links)
    for section, (start, end) in zip(scanner.sections, scanner.spans, strict=True):
        section.images = [badge.img for badge in badges if start <= badge.line - 1 < end]

    return Document(
        source=source,
        lines=lines,
        front_matter=front_matter,
        sections=scanner.sections,
        links=scanner.links,
        badges=badges,
    )
