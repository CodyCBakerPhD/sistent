"""Report renderers: text, markdown and JSON.

Everything in this package depends only on :mod:`sistent.model`. The text and markdown renderers share the display
filtering and grouping implemented here; the JSON renderer ignores :class:`RenderOptions` and always carries the
whole report. Display options never influence the exit code.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from sistent.model import Direction, Finding, Report, Severity

__all__ = [
    "DETAIL_LINES",
    "FORMATS",
    "MAX_LOCATOR",
    "Block",
    "Format",
    "RenderOptions",
    "Section",
    "clean_names",
    "detail_lines",
    "display_name",
    "filter_findings",
    "group_findings",
    "header_line",
    "hidden_note",
    "is_listed",
    "matches_filters",
    "render",
    "truncate_locator",
]

Format = Literal["text", "markdown", "json"]
DirectionFilter = Literal["downstream", "upstream", "both"]
GroupBy = Literal["repo", "aspect"]
SortOrder = Literal["config", "severity"]

FORMATS: tuple[Format, ...] = ("text", "markdown", "json")
DETAIL_LINES = 10
"""Detail lines shown per finding unless :attr:`RenderOptions.full_detail` is set."""
MAX_LOCATOR = 60
"""Longest locator printed as-is; longer ones are truncated with an ellipsis."""

ELLIPSIS = "\u2026"
MIDDLE_DOT = "\u00b7"


@dataclass(frozen=True, kw_only=True)
class RenderOptions:
    """Display options for the text and markdown renderers.

    Every option here is a *display* filter: none of them changes the exit code, and the JSON renderer ignores them.
    """

    direction: DirectionFilter = "downstream"
    """Which findings the per-repo blocks list: ``downstream`` = downstream + none, ``upstream`` = upstream only,
    ``both`` = everything. The candidates section is independent of this."""
    min_severity: Severity = Severity.INFO
    """Findings below this severity are counted in a "hidden" line instead of being listed."""
    kinds: frozenset[str] = frozenset()
    """Only list findings of these kinds (empty = all kinds)."""
    full_detail: bool = False
    """Show complete detail blocks (``--diff``) instead of truncating them to :data:`DETAIL_LINES` lines."""
    summary_only: bool = False
    """Header, summary table, errors and footer only."""
    group_by: GroupBy = "repo"
    """Top level of the detail blocks: repositories (aspects nested) or aspects (repositories nested)."""
    sort: SortOrder = "config"
    """``config`` keeps configuration/insertion order; ``severity`` orders blocks and findings error -> warn -> info."""
    show_baseline: bool = False
    """Also list baselined findings, tagged ``[baselined]``."""
    show_suppressed: bool = False
    """Also list suppressed findings, tagged ``[suppressed]``."""
    verbose: int = 0
    """1: cite :attr:`Finding.option` after each finding and list hidden findings; 2: include run-error tracebacks."""
    color: bool = False
    """Wrap severity tokens in ANSI colours (text renderer only)."""
    hide_candidates: bool = False
    """Omit the "Candidates for main" section."""


@dataclass(frozen=True)
class Section:
    """Second level of a detail block: an aspect within a repo block, or a repo within an aspect block."""

    key: str
    title: str
    findings: tuple[Finding, ...]


@dataclass(frozen=True)
class Block:
    """One top-level detail block (a repository, or an aspect when grouping by aspect)."""

    key: str
    title: str
    sections: tuple[Section, ...]
    hidden: tuple[Finding, ...]
    """Findings of this block that are listed (not suppressed/baselined) but removed by the display filters."""

    @property
    def findings(self) -> tuple[Finding, ...]:
        """All listed findings, in display order."""
        return tuple(f for section in self.sections for f in section.findings)


# ----- filtering -------------------------------------------------------------------------------------------------


def is_listed(finding: Finding, options: RenderOptions) -> bool:
    """Whether a finding may appear at all: suppressed/baselined findings need the matching ``show_*`` flag."""
    if finding.suppressed:
        return options.show_suppressed
    if finding.baselined:
        return options.show_baseline
    return True


def _direction_ok(finding: Finding, options: RenderOptions) -> bool:
    if options.direction == "both":
        return True
    if options.direction == "upstream":
        return finding.direction is Direction.UPSTREAM
    return finding.direction is not Direction.UPSTREAM


def _severity_ok(finding: Finding, options: RenderOptions) -> bool:
    return finding.severity.rank >= options.min_severity.rank


def _kind_ok(finding: Finding, options: RenderOptions) -> bool:
    return not options.kinds or finding.kind.value in options.kinds


def matches_filters(finding: Finding, options: RenderOptions) -> bool:
    """Whether a (listed) finding passes the direction, severity and kind display filters."""
    return _direction_ok(finding, options) and _severity_ok(finding, options) and _kind_ok(finding, options)


def filter_findings(report: Report, options: RenderOptions) -> list[Finding]:
    """The findings the text and markdown renderers list, in report order."""
    return [f for f in report.findings if is_listed(f, options) and matches_filters(f, options)]


def hidden_note(hidden: Sequence[Finding], options: RenderOptions) -> str:
    """Describe findings removed by display filters, e.g. ``3 info findings hidden (--min-severity info to show)``."""
    severities = {f.severity for f in hidden}
    noun = "finding" if len(hidden) == 1 else "findings"
    what = f"{severities.pop().value} {noun}" if len(severities) == 1 else noun
    hints: list[str] = []
    by_severity = [f.severity for f in hidden if not _severity_ok(f, options)]
    if by_severity:
        lowest = min(by_severity, key=lambda s: s.rank)
        hints.append(f"--min-severity {lowest.value}")
    if any(not _direction_ok(f, options) for f in hidden):
        hints.append("--direction both")
    kinds = sorted({f.kind.value for f in hidden if not _kind_ok(f, options)})
    hints.extend(f"--kind {kind}" for kind in kinds)
    return f"{len(hidden)} {what} hidden ({', '.join(hints)} to show)"


# ----- grouping --------------------------------------------------------------------------------------------------


def display_name(report: Report, repo: str) -> str:
    """``name (main)`` for the main repository, the bare name otherwise."""
    return f"{repo} (main)" if repo == report.main.name else repo


def _ordered(present: Iterable[str], preferred: Sequence[str]) -> list[str]:
    """``preferred`` order for the keys that occur, then the remaining keys in first-appearance order."""
    seen = list(dict.fromkeys(present))
    known = [key for key in preferred if key in seen]
    return known + [key for key in seen if key not in known]


def _severity_counts(findings: Iterable[Finding]) -> tuple[int, int, int, int]:
    """Sort key for ``sort="severity"``: worst severity first, then error/warn/info counts (all negated)."""
    ranks = [f.severity.rank for f in findings]
    return (
        -max(ranks, default=-1),
        -sum(1 for r in ranks if r == Severity.ERROR.rank),
        -sum(1 for r in ranks if r == Severity.WARNING.rank),
        -sum(1 for r in ranks if r == Severity.INFO.rank),
    )


def group_findings(report: Report, options: RenderOptions) -> list[Block]:
    """Arrange the listed findings into blocks according to ``group_by`` and ``sort``.

    Blocks without visible findings but with filtered-out ones are kept (they render as a single "hidden" line);
    repositories and aspects without any listed finding get no block at all (see :func:`clean_names`).
    """
    listed = [f for f in report.findings if is_listed(f, options)]
    repo_order = [report.main.name, *(r.name for r in report.repos)]
    aspect_order = [a.name for a in report.aspects]

    def by_repo(f: Finding) -> str:
        return f.repo

    def by_aspect(f: Finding) -> str:
        return f.aspect

    def repo_title(key: str) -> str:
        return display_name(report, key)

    def aspect_title(key: str) -> str:
        return key

    if options.group_by == "repo":
        outer, inner, outer_order, inner_order = by_repo, by_aspect, repo_order, aspect_order
        outer_title, inner_title = repo_title, aspect_title
    else:
        outer, inner, outer_order, inner_order = by_aspect, by_repo, aspect_order, repo_order
        outer_title, inner_title = aspect_title, repo_title

    by_severity = options.sort == "severity"
    blocks: list[Block] = []
    for outer_key in _ordered((outer(f) for f in listed), outer_order):
        members = [f for f in listed if outer(f) == outer_key]
        visible = [f for f in members if matches_filters(f, options)]
        hidden = tuple(f for f in members if not matches_filters(f, options))
        sections: list[Section] = []
        for inner_key in _ordered((inner(f) for f in visible), inner_order):
            findings = [f for f in visible if inner(f) == inner_key]
            if by_severity:
                findings.sort(key=lambda f: -f.severity.rank)
            sections.append(Section(key=inner_key, title=inner_title(inner_key), findings=tuple(findings)))
        if by_severity:
            sections.sort(key=lambda s: _severity_counts(s.findings))
        blocks.append(Block(key=outer_key, title=outer_title(outer_key), sections=tuple(sections), hidden=hidden))
    if by_severity:
        blocks.sort(key=lambda b: _severity_counts(b.findings))
    return blocks


def clean_names(report: Report, blocks: Sequence[Block], options: RenderOptions) -> list[str]:
    """Satellites (or aspects, when grouping by aspect) without any listed finding, in configuration order."""
    used = {block.key for block in blocks}
    if options.group_by == "repo":
        return [r.name for r in report.repos if r.status == "ok" and r.name not in used]
    return [a.name for a in report.aspects if a.name not in used]


# ----- shared fragments ------------------------------------------------------------------------------------------


def header_line(report: Report) -> str:
    """``sistent 0.1.0 · main neuroconv @3f9a2c1 · 3 satellites · 8 aspects · fail_on = error``."""
    main = f"main {report.main.name}"
    if report.main.head:
        main += f" @{report.main.head[:7]}"
    satellites = _plural(len(report.repos), "satellite")
    aspects = _plural(len(report.aspects), "aspect")
    fail_on = report.fail_on.value if report.fail_on else "never"
    sep = f" {MIDDLE_DOT} "
    return sep.join([f"sistent {report.sistent_version}", main, satellites, aspects, f"fail_on = {fail_on}"])


def truncate_locator(locator: str, width: int = MAX_LOCATOR) -> str:
    """Cut a locator to ``width`` characters, ending in an ellipsis when it was longer."""
    if len(locator) <= width:
        return locator
    return locator[: width - 1] + ELLIPSIS


def detail_lines(finding: Finding, options: RenderOptions) -> list[str]:
    """The lines of a finding's detail block, formatted by ``detail_kind`` and truncated unless ``full_detail``.

    ``diff`` and ``text`` details are rendered verbatim; ``list`` details become ``- item`` lines.
    """
    if not finding.detail:
        return []
    lines = finding.detail.splitlines()
    if finding.detail_kind == "list":
        lines = [line if line.lstrip().startswith(("- ", "* ")) else f"- {line}" for line in lines if line.strip()]
    if options.full_detail or len(lines) <= DETAIL_LINES:
        return lines
    remaining = len(lines) - DETAIL_LINES
    return [*lines[:DETAIL_LINES], f"... ({remaining} more lines; --diff to show)"]


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


# ----- entry point -----------------------------------------------------------------------------------------------


def render(report: Report, fmt: Format, options: RenderOptions | None = None) -> str:
    """Render ``report`` in the given format. ``options`` default to :class:`RenderOptions`."""
    # Imported here so that the sub-modules can import the shared helpers from this package without a cycle.
    from sistent.render.json_ import render_json
    from sistent.render.markdown import render_markdown
    from sistent.render.text import render_text

    renderers: dict[str, Callable[[Report, RenderOptions], str]] = {
        "text": render_text,
        "markdown": render_markdown,
        "json": render_json,
    }
    try:
        renderer = renderers[fmt]
    except KeyError:
        raise ValueError(f"unknown report format {fmt!r} (valid: {', '.join(FORMATS)})") from None
    return renderer(report, options or RenderOptions())
