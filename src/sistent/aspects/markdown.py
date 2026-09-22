"""Markdown section trees, rules and prose: the ``markdown`` aspect type.

One type serves both ``agent_instructions`` (AGENTS.md and relatives, optionally merged into one logical document)
and ``readme`` (README section layout). Extraction turns every content file into a flat list of *section nodes*, each
carrying its normalised heading path, the list items of its own body as *rules*, its paragraphs as *prose* lines and,
optionally, its code blocks. Comparison matches sections between main and a satellite (exact path, then a path suffix,
then the leaf heading gated on body similarity) and compares every matched pair according to its *mode*:

* ``presence`` - the section must exist;
* ``identical`` - the normalised own-body lines must be equal (main is authoritative);
* ``similar`` - word-shingle Jaccard plus line-level opcodes on the own-body text;
* ``rules`` - list items compared as sets (unordered lists) and sequences (ordered lists), prose ignored;
* ``full`` - ``rules`` on the list items plus ``similar`` on the remaining prose.

All noise removal (identity substitution, heading normalisation, unwrapping, title extraction, level rebasing) happens
at extraction; :meth:`MarkdownAspect.compare` is a pure function of two snapshots.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sistent.aspects.base import Aspect, UnparseableFile
from sistent.compare.sequences import close_matches, diff_sequences, match_by_keys
from sistent.compare.sets import diff_sets
from sistent.compare.text import (
    classify_lines,
    jaccard,
    line_counts,
    normalize_heading,
    normalize_text,
    ratio,
    shingles,
    strip_inline_markup,
    unified_diff,
)
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject, locator
from sistent.options import BaseOptions, OptionsError
from sistent.parsers import ParseError
from sistent.parsers.markdown import Document, Item, Section, parse_document
from sistent.repository import RepoContext

MODES: tuple[str, ...] = ("presence", "identical", "similar", "rules", "full")
"""Valid values of ``default_mode`` and of the ``sections`` table."""

PREAMBLE = "(preamble)"
"""Display name of the root pseudo-section (text before the first heading)."""
TITLE = "(title)"
"""Locator segment of title findings (``AGENTS.md#(title)``)."""
ORDER = "(order)"
"""Locator segment of heading-order findings (``README.md#(order)``)."""
TRUNCATED = "(truncated)"
"""Last prose line of a section whose body exceeded ``max_chars``."""
UNTITLED = "(untitled)"

LEAF_BODY_JACCARD = 0.3
"""Body similarity that lets two sections with the same leaf heading but different paths match."""
LEAF_PATH_RATIO = 0.85
"""Alternatively, the character similarity of the two joined paths."""
PROSE_EQUAL_JACCARD = 0.95
"""Prose bodies at least this similar are considered equal regardless of line changes."""
RST_MESSAGE = "reStructuredText is not supported in v1"

_RE_IMPORT = re.compile(r"^\s*@(\S+)\s*$")
_RE_ANCHOR_ID = re.compile(r"\s*\{#[^}]*\}\s*$")
_RE_TRAILING_HASHES = re.compile(r"\s+#+\s*$")
_RE_DUPLICATE = re.compile(r"@(\d+)$")
_RE_PROMPT = re.compile(r"^(?:\$|>>>|\.\.\.)\s+")
_RE_LIST_MARKER = re.compile(r"^(?:[-*+]|\d{1,9}[.)])\s+(?:\[[ xX]\]\s+)?")
_GLOB_CHARS = frozenset("*?[")
_QUOTE_WIDTH = 80


# ----- options -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class MarkdownOptions(BaseOptions):
    """Options of the ``markdown`` aspect type."""

    files: list[str] = field(
        metadata={
            "help": "Candidate files in priority order; globs are allowed at the repository root and inside a "
            "sub-directory such as .github/. '*.local.md' files are never included."
        }
    )
    merge: bool = field(
        default=False,
        metadata={
            "help": "Fold every content file into one logical document (sections keyed by file and path). "
            "False compares only the first present content file."
        },
    )
    ignore_sections: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Regexes (full match, case-insensitive) tried against the normalised heading and against the "
            "' > '-joined normalised path; matching sections and their subtrees are dropped."
        },
    )
    default_mode: str = field(
        default="full",
        metadata={"help": "Comparison mode for sections not listed in 'sections': " + " | ".join(MODES) + "."},
    )
    sections: dict[str, str] = field(
        default_factory=dict,
        metadata={"help": "Mode per section, keyed by normalised heading or by a glob on the ' > '-joined path."},
    )
    heading_aliases: dict[str, list[str]] = field(
        default_factory=dict,
        metadata={"help": "Canonical normalised heading -> synonyms folded into it before matching."},
    )
    similarity_threshold: float = field(
        default=0.6,
        metadata={
            "help": "Word-shingle Jaccard below which the prose of a 'similar'/'full' section that changed on both "
            "sides is reported as differing."
        },
    )
    rule_match_cutoff: float = field(
        default=0.75,
        metadata={"help": "difflib ratio at or above which an unmatched main rule and repo rule count as reworded."},
    )
    paragraph_rules: bool = field(
        default=False,
        metadata={"help": "Treat every paragraph as a rule too (prose-style instruction files); prose is then empty."},
    )
    compare_code: bool = field(
        default=False,
        metadata={"help": "Include code blocks (normalised, prompts stripped) in the comparison under subject 'code'."},
    )
    heading_order: bool = field(
        default=False,
        metadata={"help": "Report when the shared top-level sections appear in a different order."},
    )
    title: bool = field(
        default=True,
        metadata={"help": "Compare the document titles (the single H1) after identity substitution."},
    )
    max_chars: int = field(
        default=20000,
        metadata={"help": "Prose characters kept per section; longer bodies are truncated."},
    )

    def __post_init__(self) -> None:
        valid = ", ".join(MODES)
        if self.default_mode not in MODES:
            raise OptionsError(f"default_mode: expected one of {valid}; got {self.default_mode!r}")
        for key, mode in self.sections.items():
            if mode not in MODES:
                raise OptionsError(f"sections.{key}: expected one of {valid}; got {mode!r}")
        for name in ("similarity_threshold", "rule_match_cutoff"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise OptionsError(f"{name}: expected a number between 0 and 1; got {value!r}")
        if self.max_chars < 1:
            raise OptionsError(f"max_chars: expected a positive integer; got {self.max_chars!r}")
        for pattern in self.ignore_sections:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise OptionsError(f"ignore_sections: invalid regex {pattern!r}: {exc}") from None
        if not self.files:
            raise OptionsError("files: at least one candidate file is required")


# ----- snapshot nodes ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rule:
    """One list item (or, with ``paragraph_rules``, one paragraph) of a section's own body."""

    text: str
    """Normalised, substituted text: the rule's identity."""
    raw: str
    """First line as written (marker included), substituted."""
    line: int
    parent: str | None
    """Normalised text of the enclosing item for nested items, else ``None``."""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "raw": self.raw, "line": self.line, "parent": self.parent}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> _Rule:
        return cls(text=data["text"], raw=data["raw"], line=data["line"], parent=data.get("parent"))

    @property
    def quoted(self) -> str:
        """The item text without its list marker, shortened for messages."""
        text = _RE_LIST_MARKER.sub("", self.raw).strip() or self.text
        if len(text) > _QUOTE_WIDTH:
            text = text[: _QUOTE_WIDTH - 1] + "…"
        return text


@dataclass(eq=False)
class _Node:
    """One section of the flat section list (compared by identity, never by value)."""

    source: str
    path: tuple[str, ...]
    display: tuple[str, ...]
    level: int
    line: int
    mode: str
    rules: list[_Rule]
    ordered: list[list[_Rule]]
    prose: list[str]
    code: list[str]
    unmatchable: bool
    children: int = 0

    @property
    def locator(self) -> str:
        return locator(self.source, *(self.display or (PREAMBLE,)))

    @property
    def leaf(self) -> str:
        return self.path[-1] if self.path else ""

    @property
    def joined(self) -> str:
        return " > ".join(self.path)

    @property
    def is_preamble(self) -> bool:
        return not self.path

    @property
    def role(self) -> str:
        return _basename(self.source)

    def rule_texts(self) -> list[str]:
        return [r.text for r in self.rules] + [r.text for seq in self.ordered for r in seq]

    def body_lines(self, *, rules: bool, code: bool) -> list[str]:
        """Normalised own-body lines: prose, then (optionally) rules and code."""
        lines = list(self.prose)
        if rules:
            lines.extend(self.rule_texts())
        if code:
            lines.extend(self.code)
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "path": list(self.path),
            "display": list(self.display),
            "level": self.level,
            "line": self.line,
            "mode": self.mode,
            "rules": [r.to_dict() for r in self.rules],
            "ordered_lists": [[r.to_dict() for r in seq] for seq in self.ordered],
            "prose": list(self.prose),
            "code": list(self.code),
            "children": self.children,
            "unmatchable": self.unmatchable,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> _Node:
        return cls(
            source=data["source"],
            path=tuple(data["path"]),
            display=tuple(data["display"]),
            level=data["level"],
            line=data["line"],
            mode=data["mode"],
            rules=[_Rule.from_dict(r) for r in data["rules"]],
            ordered=[[_Rule.from_dict(r) for r in seq] for seq in data["ordered_lists"]],
            prose=list(data["prose"]),
            code=list(data["code"]),
            unmatchable=bool(data["unmatchable"]),
            children=int(data.get("children", 0)),
        )


@dataclass
class _Open:
    """An entry of the heading stack during tree construction."""

    level: int
    path: tuple[str, ...]
    display: tuple[str, ...]
    ignored: bool


# ----- small helpers -----------------------------------------------------------------------------------------------


def _basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _display_heading(raw: str) -> str:
    """Heading text for locators: markup stripped, ``{#id}``/closing hashes removed, casing kept."""
    text = _RE_ANCHOR_ID.sub("", raw)
    text = _RE_TRAILING_HASHES.sub("", text)
    return " ".join(strip_inline_markup(text).split())


def _canonical(text: str) -> str:
    """Whitespace-insensitive form of a file used for duplicate detection."""
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return "\n".join(lines).strip("\n")


def _import_target(text: str) -> str | None:
    """The ``@path`` a file consisting solely of Claude import lines points at, else ``None``."""
    targets: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _RE_IMPORT.match(line)
        if match is None:
            return None
        targets.append(match.group(1))
    if not targets:
        return None
    return targets[0].removeprefix("./")


def _cap(lines: Sequence[str], max_chars: int) -> list[str]:
    """Keep leading ``lines`` up to ``max_chars`` characters in total, marking truncation."""
    out: list[str] = []
    total = 0
    for line in lines:
        total += len(line)
        if total > max_chars:
            out.append(TRUNCATED)
            break
        out.append(line)
    return out


def _changed_lines(main: Sequence[str], other: Sequence[str]) -> tuple[list[str], list[str]]:
    """``(inserted, deleted)`` lines of ``other`` relative to ``main`` (line-level, never character-level)."""
    matcher = difflib.SequenceMatcher(None, list(main), list(other), autojunk=False)
    inserted: list[str] = []
    deleted: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            inserted.extend(other[j1:j2])
        if tag in ("delete", "replace"):
            deleted.extend(main[i1:i2])
    return inserted, deleted


def _describe_files(files: Mapping[str, Any]) -> str:
    """``AGENTS.md (+CLAUDE.md alias)`` style summary of a snapshot's ``files`` record."""
    content: list[str] = list(files.get("content", []))
    aliases: dict[str, str] = dict(files.get("aliases", {}))
    if not content:
        return "no file"
    text = ", ".join(content)
    if aliases:
        text += " (+" + ", ".join(f"{name} alias" for name in aliases) + ")"
    return text


def _resolve_role(files: Mapping[str, Any], rel: str) -> str:
    """Basename of ``rel`` after following alias links (a bounded number of hops)."""
    aliases: dict[str, str] = dict(files.get("aliases", {}))
    seen: set[str] = set()
    while rel in aliases and rel not in seen:
        seen.add(rel)
        rel = aliases[rel]
    return _basename(rel)


def _path_for_role(files: Mapping[str, Any], role: str) -> str:
    for rel in files.get("present", []):
        if _resolve_role(files, rel) == role:
            return str(rel)
    return role


def _suffix_key(length: int) -> Callable[[_Node], Hashable | None]:
    def key(node: _Node) -> Hashable | None:
        if node.unmatchable or len(node.path) < length:
            return None
        return node.path[-length:]

    return key


def _leaf_key(node: _Node) -> Hashable | None:
    if node.unmatchable or not node.path:
        return None
    return node.leaf


def _leaf_gate(main: _Node, other: _Node) -> bool:
    """Whether two sections sharing a leaf heading are the same section (body or path similarity)."""
    main_body = " ".join(main.body_lines(rules=True, code=False))
    other_body = " ".join(other.body_lines(rules=True, code=False))
    if jaccard(shingles(main_body), shingles(other_body)) >= LEAF_BODY_JACCARD:
        return True
    return ratio(main.joined, other.joined) >= LEAF_PATH_RATIO


def _numbered(items: Sequence[_Rule]) -> str:
    return "\n".join(f"  {i}. {r.quoted}" for i, r in enumerate(items, start=1)) or "  (empty)"


# ----- the aspect --------------------------------------------------------------------------------------------------


class MarkdownAspect(Aspect):
    """Section trees, rules and prose of Markdown files (AGENTS.md, README.md, CHANGELOG.md, ...)."""

    type_name = "markdown"
    options_cls = MarkdownOptions
    schema_version = 1
    description = (
        "Section trees of Markdown files: headings matched by normalised path, list items compared as rules, "
        "prose by similarity; serves agent-instruction files and README layouts."
    )

    def __init__(self, name: str, options: BaseOptions) -> None:
        super().__init__(name, options)
        if not isinstance(options, MarkdownOptions):
            raise TypeError(f"{type(self).__name__} needs MarkdownOptions, got {type(options).__name__}")
        self.opts: MarkdownOptions = options
        self._ignore_patterns = [re.compile(p, re.IGNORECASE) for p in options.ignore_sections]
        self._alias_map: dict[str, str] = {}
        for canonical, synonyms in options.heading_aliases.items():
            target = " ".join(canonical.split()).casefold()
            for synonym in synonyms:
                self._alias_map[" ".join(synonym.split()).casefold()] = target
        self._section_modes: list[tuple[str, str]] = [
            (" ".join(key.split()).casefold(), mode) for key, mode in options.sections.items()
        ]

    # ----- extraction -----------------------------------------------------------------------------------------

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """Classify the candidate files, parse the content ones and build the section list."""
        present: list[str] = []
        content: list[str] = []
        aliases: dict[str, str] = {}
        unsupported: list[str] = []
        canonical_texts: dict[str, str] = {}
        for rel in self._candidates(ctx):
            present.append(rel)
            if rel.casefold().endswith(".rst"):
                unsupported.append(rel)
                continue
            link = self._symlink_target(ctx, rel)
            if link is not None:
                aliases[rel] = link
                continue
            text = ctx.read_text(rel)
            imported = _import_target(text)
            if imported is not None:
                aliases[rel] = imported
                continue
            canonical = _canonical(text)
            original = canonical_texts.get(canonical)
            if original is not None:
                aliases[rel] = original
                continue
            canonical_texts[canonical] = rel
            if content and not self.opts.merge:
                ctx.ignored.append(f"{rel} (merge = false: only {content[0]} is compared)")
                continue
            content.append(rel)
        roles = sorted({_resolve_role({"aliases": aliases}, rel) for rel in present})

        title: dict[str, Any] | None = None
        nodes: list[_Node] = []
        top_order: list[str] = []
        front_matter: dict[str, str] = {}
        for rel in content:
            try:
                doc = parse_document(ctx.read_text(rel), source=rel)
            except ParseError as exc:
                raise UnparseableFile(rel, exc.reason) from exc
            if doc.front_matter is not None:
                front_matter[rel] = ctx.substitute(doc.front_matter, where=locator(rel))
            doc_title, doc_nodes, doc_order = self._extract_document(ctx, rel, doc)
            if title is None:
                title = doc_title
            nodes.extend(doc_nodes)
            top_order.extend(doc_order)
        all_rules: list[str] = list(dict.fromkeys(text for node in nodes for text in node.rule_texts()))
        return {
            "files": {
                "present": present,
                "content": content,
                "aliases": aliases,
                "roles": roles,
                "unsupported": unsupported,
            },
            "front_matter": front_matter,
            "title": title,
            "sections": [node.to_dict() for node in nodes],
            "all_rules": all_rules,
            "top_order": top_order,
        }

    def _candidates(self, ctx: RepoContext) -> list[str]:
        """Present candidate files in ``files`` order (globs expanded, ``*.local.md`` and directories dropped)."""
        out: list[str] = []
        for entry in self.opts.files:
            entry = entry.strip().removeprefix("./")
            if not entry:
                continue
            directory, _, name = entry.rpartition("/")
            if _GLOB_CHARS & set(name):
                matches = ctx.glob(name, dirs=[directory or "."])
            else:
                matches = [entry] if ctx.exists(entry) else []
            for rel in matches:
                if rel in out or rel.casefold().endswith(".local.md") or ctx.repo.is_dir(rel):
                    continue
                out.append(rel)
        return out

    @staticmethod
    def _symlink_target(ctx: RepoContext, rel: str) -> str | None:
        """Repo-relative target of a symlinked candidate, or ``None`` for regular files."""
        if not ctx.repo.is_symlink(rel):
            return None
        try:
            raw = os.readlink(ctx.repo.path(rel))
        except OSError:
            return None
        target = os.path.normpath(os.path.join(os.path.dirname(rel), raw)).replace(os.sep, "/")
        if target.startswith("../") or os.path.isabs(target):
            return raw
        return target

    def _extract_document(
        self, ctx: RepoContext, rel: str, doc: Document
    ) -> tuple[dict[str, Any] | None, list[_Node], list[str]]:
        """Title, section nodes (preamble first) and level-1 heading order of one parsed file."""
        sections = doc.sections[1:]
        preamble_parts: list[Section] = [doc.sections[0]]
        title: dict[str, Any] | None = None
        if doc.title is not None:
            title_section = sections[0]
            sections = sections[1:]
            preamble_parts.append(title_section)
            where = locator(rel, TITLE)
            title = {
                "text": self._normalise_heading(ctx, title_section.heading_raw, where=where),
                "display": ctx.substitute(_display_heading(title_section.heading_raw), where=where),
                "source": rel,
            }
        offset = min((s.level for s in sections), default=1) - 1

        nodes = [self._node(ctx, rel, path=(), display=(), level=0, line=0, body=preamble_parts)]
        stack: list[_Open] = []
        seen_paths: dict[tuple[str, ...], int] = {}
        for section in sections:
            level = section.level - offset
            while stack and stack[-1].level >= level:
                stack.pop()
            parent = stack[-1] if stack else None
            if parent is not None and parent.ignored:
                stack.append(_Open(level, parent.path, parent.display, ignored=True))
                continue
            parent_path = parent.path if parent else ()
            parent_display = parent.display if parent else ()
            raw = section.heading_raw
            display_raw = _display_heading(raw)
            where = locator(rel, *parent_display, display_raw or UNTITLED)
            display = ctx.substitute(display_raw, where=where)
            norm = self._normalise_heading(ctx, raw, where=where)
            unmatchable = not norm
            if unmatchable:
                norm = ctx.substitute(" ".join(raw.split()), where=where)
            if not display:
                display = norm or UNTITLED
            path = (*parent_path, norm)
            shown = (*parent_display, display)
            count = seen_paths.get(path, 0) + 1
            seen_paths[path] = count
            if count > 1:
                path = (*parent_path, f"{norm}@{count}")
                shown = (*parent_display, f"{display}@{count}")
            ignored_by = self._ignored_by(norm, " > ".join(path))
            if ignored_by is not None:
                ctx.ignored.append(f"{locator(rel, *shown)} (ignore_sections: {ignored_by})")
                stack.append(_Open(level, path, shown, ignored=True))
                continue
            stack.append(_Open(level, path, shown, ignored=False))
            nodes.append(
                self._node(
                    ctx,
                    rel,
                    path=path,
                    display=shown,
                    level=level,
                    line=section.line,
                    body=[section],
                    unmatchable=unmatchable,
                )
            )

        for node in nodes:
            depth = len(node.path)
            node.children = sum(1 for n in nodes if len(n.path) == depth + 1 and n.path[:depth] == node.path)
        top_order = [node.path[0] for node in nodes if len(node.path) == 1]
        return title, nodes, top_order

    def _normalise_heading(self, ctx: RepoContext, raw: str, *, where: str) -> str:
        norm = ctx.substitute(normalize_heading(raw), where=where)
        return self._alias_map.get(norm, norm)

    def _ignored_by(self, heading: str, joined: str) -> str | None:
        for pattern in self._ignore_patterns:
            if pattern.fullmatch(heading) or pattern.fullmatch(joined):
                return pattern.pattern
        return None

    def _mode_for(self, path: tuple[str, ...]) -> str:
        if not path:
            return self.opts.default_mode
        leaf = _RE_DUPLICATE.sub("", path[-1])
        joined = " > ".join(_RE_DUPLICATE.sub("", segment) for segment in path)
        for key, mode in self._section_modes:
            if key == leaf:
                return mode
        for key, mode in self._section_modes:
            if fnmatch.fnmatchcase(joined, key):
                return mode
        return self.opts.default_mode

    def _node(
        self,
        ctx: RepoContext,
        rel: str,
        *,
        path: tuple[str, ...],
        display: tuple[str, ...],
        level: int,
        line: int,
        body: Sequence[Section],
        unmatchable: bool = False,
    ) -> _Node:
        """Rules, ordered lists, prose and code of the own body made of ``body`` (several parts for the preamble)."""
        where = locator(rel, *(display or (PREAMBLE,)))
        rules: list[_Rule] = []
        ordered: list[list[_Rule]] = []
        seen: set[str] = set()
        for section in body:
            for block in section.lists:
                self._walk_items(ctx, block.items, None, block.ordered, rules, ordered, seen, where)
        prose: list[str] = []
        for section in body:
            for paragraph in section.paragraphs:
                text = ctx.substitute(normalize_text(paragraph), where=where)
                if text:
                    prose.append(text)
        if self.opts.paragraph_rules:
            for section in body:
                for paragraph in section.paragraphs:
                    text = ctx.substitute(normalize_text(paragraph), where=where)
                    if text and text not in seen:
                        seen.add(text)
                        rules.append(
                            _Rule(text=text, raw=ctx.substitute(paragraph.strip(), where=where), line=line, parent=None)
                        )
            prose = []
        prose = _cap(prose, self.opts.max_chars)
        code: list[str] = []
        if self.opts.compare_code:
            for section in body:
                for code_block in section.code_blocks:
                    for code_line in code_block.lines:
                        text = " ".join(_RE_PROMPT.sub("", code_line.strip()).split())
                        if text:
                            code.append(ctx.substitute(text, where=where))
        return _Node(
            source=rel,
            path=path,
            display=display,
            level=level,
            line=line,
            mode=self._mode_for(path),
            rules=rules,
            ordered=ordered,
            prose=prose,
            code=code,
            unmatchable=unmatchable,
        )

    def _walk_items(
        self,
        ctx: RepoContext,
        items: Sequence[Item],
        parent: _Rule | None,
        is_ordered: bool,
        rules: list[_Rule],
        ordered: list[list[_Rule]],
        seen: set[str],
        where: str,
    ) -> None:
        converted: list[tuple[Item, _Rule | None]] = []
        for item in items:
            text = ctx.substitute(normalize_text(item.text), where=where)
            rule = None
            if text:
                raw = ctx.substitute(" ".join(item.raw.split()), where=where)
                rule = _Rule(text=text, raw=raw, line=item.line, parent=parent.text if parent else None)
            converted.append((item, rule))
        if is_ordered:
            sequence = [rule for _, rule in converted if rule is not None]
            if sequence:
                ordered.append(sequence)
        else:
            for _, rule in converted:
                if rule is not None and rule.text not in seen:
                    seen.add(rule.text)
                    rules.append(rule)
        for item, rule in converted:
            if not item.children:
                continue
            ordered_children = [child for child in item.children if child.ordered]
            unordered_children = [child for child in item.children if not child.ordered]
            if ordered_children:
                self._walk_items(ctx, ordered_children, rule, True, rules, ordered, seen, where)
            if unordered_children:
                self._walk_items(ctx, unordered_children, rule, False, rules, ordered, seen, where)

    # ----- comparison -----------------------------------------------------------------------------------------

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Findings for satellite ``other`` relative to ``main`` (see the module docstring for the algorithm)."""
        out: list[Finding] = []
        repo = other.repo
        main_files: Mapping[str, Any] = main.data["files"]
        other_files: Mapping[str, Any] = other.data["files"]
        for rel in other_files.get("unsupported", []):
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.UNPARSEABLE,
                    subject=Subject.FILE,
                    locator=rel,
                    message=f"{rel}: {RST_MESSAGE}",
                    content_key=rel,
                    severity=Severity.INFO,
                )
            )
        main_content: list[str] = list(main_files["content"])
        other_content: list[str] = list(other_files["content"])
        if main_content and not other_content:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.FILE,
                    locator=main_content[0],
                    message=f"no {main_content[0]}: main has {_describe_files(main_files)}, repo has none of "
                    + ", ".join(self.opts.files),
                    content_key=main_content[0],
                )
            )
            return out
        if other_content and not main_content:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.FILE,
                    locator=other_content[0],
                    message=f"{_describe_files(other_files)} present in repo; main has none of "
                    + ", ".join(self.opts.files),
                    content_key=other_content[0],
                )
            )
            return out
        if not main_content:
            return out

        file_missing, file_extra = self._compare_roles(repo, main_files, other_files, out)
        if self.opts.title:
            self._compare_titles(repo, main.data.get("title"), other.data.get("title"), out)

        main_nodes = [_Node.from_dict(d) for d in main.data["sections"]]
        other_nodes = [_Node.from_dict(d) for d in other.data["sections"]]
        pairs, unmatched_main, unmatched_other = self._match(main_nodes, other_nodes)
        main_all = set(main.data.get("all_rules", []))
        other_all = set(other.data.get("all_rules", []))
        for main_node, other_node, moved in pairs:
            if moved:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MOVED,
                        subject=Subject.SECTION,
                        locator=main_node.locator,
                        message=f"section moved: main {' > '.join(main_node.display)} ; repo "
                        f"{' > '.join(other_node.display)}",
                        detail=f"main: {' > '.join(main_node.display)}\nrepo: {' > '.join(other_node.display)}",
                        detail_kind="text",
                        content_key=main_node.leaf,
                    )
                )
            self._compare_pair(repo, main_node, other_node, main_all, other_all, out)
        self._report_unmatched(repo, unmatched_main, main_nodes, kind=Kind.MISSING, skip_roles=file_missing, out=out)
        self._report_unmatched(repo, unmatched_other, other_nodes, kind=Kind.EXTRA, skip_roles=file_extra, out=out)
        if self.opts.heading_order:
            self._compare_order(repo, main, other, main_nodes, other_nodes, out)
        return out

    def _compare_roles(
        self,
        repo: str,
        main_files: Mapping[str, Any],
        other_files: Mapping[str, Any],
        out: list[Finding],
    ) -> tuple[set[str], set[str]]:
        """File-role findings; returns the roles reported as missing/extra files (their sections stay silent)."""
        main_roles = set(main_files.get("roles", []))
        other_roles = set(other_files.get("roles", []))
        missing_roles = sorted(main_roles - other_roles)
        extra_roles = sorted(other_roles - main_roles)
        if missing_roles and extra_roles and not (main_roles & other_roles):
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.DIFFERS,
                    subject=Subject.NAME,
                    locator=main_files["content"][0],
                    message=f"naming differs: main uses {_describe_files(main_files)}; repo uses "
                    f"{_describe_files(other_files)}",
                    content_key=" ".join(extra_roles),
                    severity=Severity.INFO,
                )
            )
            return set(), set()
        for role in missing_roles:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.FILE,
                    locator=_path_for_role(main_files, role),
                    message=f"{role} missing: main uses {_describe_files(main_files)}; repo has "
                    f"{_describe_files(other_files)}",
                    content_key=role,
                    severity=Severity.WARNING,
                )
            )
        for role in extra_roles:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.FILE,
                    locator=_path_for_role(other_files, role),
                    message=f"{role} only in repo (main uses {_describe_files(main_files)})",
                    content_key=role,
                )
            )
        return set(missing_roles), set(extra_roles)

    def _compare_titles(
        self, repo: str, main_title: Mapping[str, Any] | None, other_title: Mapping[str, Any] | None, out: list[Finding]
    ) -> None:
        if not main_title or not other_title or main_title["text"] == other_title["text"]:
            return
        out.append(
            self.finding(
                repo=repo,
                kind=Kind.DIFFERS,
                subject=Subject.TITLE,
                locator=locator(str(main_title["source"]), TITLE),
                message=f"title differs: main '{main_title['display']}' ; repo '{other_title['display']}'",
                detail=f"main: {main_title['display']}\nrepo: {other_title['display']}",
                detail_kind="text",
                content_key=str(main_title["text"]),
                severity=Severity.INFO,
            )
        )

    @staticmethod
    def _match(
        main_nodes: Sequence[_Node], other_nodes: Sequence[_Node]
    ) -> tuple[list[tuple[_Node, _Node, bool]], list[_Node], list[_Node]]:
        """Pair sections: exact path (with, then without, the source), path suffix, then gated leaf heading."""
        longest = max((len(n.path) for n in (*main_nodes, *other_nodes)), default=0)
        keys: list[Callable[[_Node], Hashable | None]] = [
            lambda n: (n.source, n.path),
            lambda n: n.path,
        ]
        keys.extend(_suffix_key(length) for length in range(longest, 1, -1))
        first = match_by_keys(list(main_nodes), list(other_nodes), keys=keys)
        pairs = [(m, o, index >= 2) for m, o, index in first.pairs]
        second = match_by_keys(list(first.unmatched_main), list(first.unmatched_other), keys=[_leaf_key])
        unmatched_main = list(second.unmatched_main)
        unmatched_other = list(second.unmatched_other)
        for m, o, _ in second.pairs:
            if _leaf_gate(m, o):
                pairs.append((m, o, True))
            else:
                unmatched_main.append(m)
                unmatched_other.append(o)
        main_index = {id(n): i for i, n in enumerate(main_nodes)}
        other_index = {id(n): i for i, n in enumerate(other_nodes)}
        pairs.sort(key=lambda p: main_index[id(p[0])])
        unmatched_main.sort(key=lambda n: main_index[id(n)])
        unmatched_other.sort(key=lambda n: other_index[id(n)])
        return pairs, unmatched_main, unmatched_other

    def _report_unmatched(
        self,
        repo: str,
        unmatched: Sequence[_Node],
        all_nodes: Sequence[_Node],
        *,
        kind: Kind,
        skip_roles: set[str],
        out: list[Finding],
    ) -> None:
        """``missing``/``extra`` section findings for the highest unmatched nodes only."""
        unmatched_keys = {(n.source, n.path) for n in unmatched if n.path}
        for node in unmatched:
            if node.is_preamble or node.role in skip_roles:
                continue
            if (node.source, node.path[:-1]) in unmatched_keys:
                continue
            depth = len(node.path)
            subtree = [
                n for n in all_nodes if n.source == node.source and len(n.path) > depth and n.path[:depth] == node.path
            ]
            rule_count = sum(len(n.rule_texts()) for n in (node, *subtree))
            prose_count = sum(len(n.prose) for n in (node, *subtree))
            what = "section missing" if kind is Kind.MISSING else "section only in repo"
            out.append(
                self.finding(
                    repo=repo,
                    kind=kind,
                    subject=Subject.SECTION,
                    locator=node.locator,
                    message=f"{what}: {node.display[-1]}",
                    detail=f"{len(subtree)} subsections, {rule_count} rules, {prose_count} prose lines",
                    detail_kind="text",
                    content_key=node.leaf,
                )
            )

    def _compare_pair(
        self,
        repo: str,
        main: _Node,
        other: _Node,
        main_all: set[str],
        other_all: set[str],
        out: list[Finding],
    ) -> None:
        """Compare one matched section pair according to main's mode."""
        mode = main.mode
        if mode == "presence":
            return
        where = main.locator
        if mode == "identical":
            a = main.body_lines(rules=True, code=self.opts.compare_code)
            b = other.body_lines(rules=True, code=self.opts.compare_code)
            if a != b:
                added, removed = line_counts(a, b)
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.DIFFERS,
                        subject=Subject.PROSE,
                        locator=where,
                        message=f"content differs from main (+{added} -{removed} lines; mode identical)",
                        detail=unified_diff(a, b, fromfile=f"main/{where}", tofile=f"{repo}/{where}"),
                        detail_kind="diff",
                        content_key=main.joined,
                        direction=Direction.DOWNSTREAM,
                    )
                )
            return
        if mode in ("rules", "full"):
            self._compare_rules(repo, main, other, main_all, other_all, out)
        if mode in ("similar", "full"):
            include_rules = mode == "similar"
            a = main.body_lines(rules=include_rules, code=False)
            b = other.body_lines(rules=include_rules, code=False)
            self._compare_prose(repo, main, a, b, Subject.PROSE, out)
            if self.opts.compare_code:
                self._compare_prose(repo, main, main.code, other.code, Subject.CODE, out)

    def _compare_rules(
        self,
        repo: str,
        main: _Node,
        other: _Node,
        main_all: set[str],
        other_all: set[str],
        out: list[Finding],
    ) -> None:
        diff = diff_sets(main.rules, other.rules, key=lambda r: r.text)
        self._report_rule_leftovers(repo, main, list(diff.missing), list(diff.extra), main_all, other_all, out)
        for index, (main_list, other_list) in enumerate(zip(main.ordered, other.ordered, strict=False)):
            seq = diff_sequences(main_list, other_list, key=lambda r: r.text)
            self._report_rule_leftovers(repo, main, list(seq.missing), list(seq.extra), main_all, other_all, out)
            if seq.reordered:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.REORDERED,
                        subject=Subject.RULE,
                        locator=main.locator,
                        message=f"ordered list {index + 1} has its steps in a different order",
                        detail=f"main:\n{_numbered(main_list)}\nrepo:\n{_numbered(other_list)}",
                        detail_kind="text",
                        content_key=f"{main.joined}#ordered-list-{index + 1}",
                    )
                )
        for main_list in main.ordered[len(other.ordered) :]:
            self._report_rule_leftovers(repo, main, list(main_list), [], main_all, other_all, out)
        for other_list in other.ordered[len(main.ordered) :]:
            self._report_rule_leftovers(repo, main, [], list(other_list), main_all, other_all, out)

    def _report_rule_leftovers(
        self,
        repo: str,
        main: _Node,
        missing: list[_Rule],
        extra: list[_Rule],
        main_all: set[str],
        other_all: set[str],
        out: list[Finding],
    ) -> None:
        """Pair reworded rules, then report moved/missing main rules and extra repo rules."""
        where = main.locator
        by_text_main = {r.text: r for r in missing}
        by_text_other = {r.text: r for r in extra}
        paired = close_matches(list(by_text_main), list(by_text_other), cutoff=self.opts.rule_match_cutoff)
        paired_main = {a for a, _, _ in paired}
        paired_other = {b for _, b, _ in paired}
        for a, b, score in paired:
            main_rule, other_rule = by_text_main[a], by_text_other[b]
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.DIFFERS,
                    subject=Subject.RULE,
                    locator=where,
                    message=f"rule differs: '{other_rule.quoted}' ≈ '{main_rule.quoted}' ({score:.2f})",
                    detail=f"main: {main_rule.raw}\nrepo: {other_rule.raw}",
                    detail_kind="text",
                    content_key=main_rule.text,
                    direction=Direction.NONE,
                )
            )
        for rule in missing:
            if rule.text in paired_main:
                continue
            if rule.text in other_all:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MOVED,
                        subject=Subject.RULE,
                        locator=where,
                        message=f"rule found in another section of the repo: '{rule.quoted}'",
                        content_key=rule.text,
                    )
                )
            else:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MISSING,
                        subject=Subject.RULE,
                        locator=where,
                        message=f"rule missing: '{rule.quoted}'",
                        content_key=rule.text,
                    )
                )
        for rule in extra:
            if rule.text in paired_other or rule.text in main_all:
                continue
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.RULE,
                    locator=where,
                    message=f"rule only in repo: '{rule.quoted}'",
                    content_key=rule.text,
                )
            )

    def _compare_prose(
        self, repo: str, main: _Node, a: Sequence[str], b: Sequence[str], subject: Subject, out: list[Finding]
    ) -> None:
        """Shingle similarity plus line opcodes on two normalised line lists (prose or code)."""
        score = jaccard(shingles(" ".join(a)), shingles(" ".join(b)))
        if score >= PROSE_EQUAL_JACCARD:
            return
        change = classify_lines(a, b)
        if change == "equal":
            return
        where = main.locator
        label = subject.value
        inserted, deleted = _changed_lines(a, b)
        if change == "insert":
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=subject,
                    locator=where,
                    message=f"{label} only in repo: {len(inserted)} line(s) not in main",
                    detail="\n".join(inserted),
                    detail_kind="list",
                    content_key=main.joined,
                )
            )
        elif change == "delete":
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=subject,
                    locator=where,
                    message=f"{label} missing: {len(deleted)} line(s) of main not in repo",
                    detail="\n".join(deleted),
                    detail_kind="list",
                    content_key=main.joined,
                )
            )
        elif score < self.opts.similarity_threshold:
            added, removed = line_counts(a, b)
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.DIFFERS,
                    subject=subject,
                    locator=where,
                    message=f"{label} similarity {score:.2f} (+{added} -{removed} lines)",
                    detail=unified_diff(a, b, fromfile=f"main/{where}", tofile=f"{repo}/{where}"),
                    detail_kind="diff",
                    content_key=main.joined,
                    direction=Direction.NONE,
                    option=self.option_ref("similarity_threshold"),
                )
            )

    def _compare_order(
        self,
        repo: str,
        main: Snapshot,
        other: Snapshot,
        main_nodes: Sequence[_Node],
        other_nodes: Sequence[_Node],
        out: list[Finding],
    ) -> None:
        main_top: list[str] = list(main.data.get("top_order", []))
        other_top: list[str] = list(other.data.get("top_order", []))
        seq = diff_sequences(main_top, other_top)
        if not seq.reordered:
            return
        shared = set(main_top) & set(other_top)
        main_shared = [h for h in dict.fromkeys(main_top) if h in shared]
        other_shared = [h for h in dict.fromkeys(other_top) if h in shared]
        main_display = {n.leaf: n.display[-1] for n in reversed(main_nodes) if len(n.path) == 1}
        other_display = {n.leaf: n.display[-1] for n in reversed(other_nodes) if len(n.path) == 1}
        file = str(main.data["files"]["content"][0])
        out.append(
            self.finding(
                repo=repo,
                kind=Kind.REORDERED,
                subject=Subject.SECTION,
                locator=locator(file, ORDER),
                message="top-level sections are in a different order than in main",
                detail="main: "
                + " > ".join(main_display.get(h, h) for h in main_shared)
                + "\nrepo: "
                + " > ".join(other_display.get(h, h) for h in other_shared),
                detail_kind="text",
                content_key=" > ".join(main_shared),
            )
        )

    # ----- self-check -----------------------------------------------------------------------------------------

    def self_check(self, snap: Snapshot) -> list[Finding]:
        """Duplicate headings within one file and drift between merged files sharing a section path."""
        out: list[Finding] = []
        nodes = [_Node.from_dict(d) for d in snap.data.get("sections", [])]
        for node in nodes:
            if node.path and _RE_DUPLICATE.search(node.leaf):
                out.append(
                    self.finding(
                        repo=snap.repo,
                        kind=Kind.DIFFERS,
                        subject=Subject.SECTION,
                        locator=node.locator,
                        message=f"duplicate heading: {_RE_DUPLICATE.sub('', node.display[-1])} appears more than once",
                        content_key=node.leaf,
                        severity=Severity.INFO,
                    )
                )
        by_path: dict[tuple[str, ...], list[_Node]] = {}
        for node in nodes:
            if node.path:
                by_path.setdefault(node.path, []).append(node)
        for group in by_path.values():
            first = group[0]
            for node in group[1:]:
                if node.source == first.source:
                    continue
                a = first.body_lines(rules=True, code=self.opts.compare_code)
                b = node.body_lines(rules=True, code=self.opts.compare_code)
                if a == b:
                    continue
                where = locator(f"{first.source}+{node.source}", *first.display)
                out.append(
                    self.finding(
                        repo=snap.repo,
                        kind=Kind.DIFFERS,
                        subject=Subject.SECTION,
                        locator=where,
                        message=f"{first.source} and {node.source} have drifted at {first.display[-1]}",
                        detail=unified_diff(
                            a, b, fromfile=f"{first.source}/{first.locator}", tofile=f"{node.source}/{node.locator}"
                        ),
                        detail_kind="diff",
                        content_key=first.joined,
                        severity=Severity.WARNING,
                    )
                )
        return out


__all__ = ["MODES", "MarkdownAspect", "MarkdownOptions"]
