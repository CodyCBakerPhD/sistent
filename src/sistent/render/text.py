"""Plain-text report renderer (the default ``sistent check`` output).

Layout: header line, aligned summary table, one block per repository (or per aspect), the aggregated
"Candidates for main" section, run errors, the "Ignored" footer and, always as the very last line, the exit reason.
"""

from __future__ import annotations

from collections.abc import Sequence

from sistent.model import Finding, Report, RunError, Severity
from sistent.render import (
    MAX_LOCATOR,
    MIDDLE_DOT,
    Block,
    RenderOptions,
    clean_names,
    detail_lines,
    display_name,
    group_findings,
    header_line,
    hidden_note,
    is_listed,
    truncate_locator,
)

__all__ = ["render_text"]

DASH = "\u2013"
"""Placeholder for a count that does not apply (unavailable repo, candidates of main)."""

SEVERITY_ABBREVIATION: dict[Severity, str] = {
    Severity.ERROR: "error",
    Severity.WARNING: "warn",
    Severity.INFO: "info",
}
_ANSI: dict[Severity, str] = {
    Severity.ERROR: "\x1b[31m",
    Severity.WARNING: "\x1b[33m",
    Severity.INFO: "\x1b[36m",
}
_RESET = "\x1b[0m"

_FINDING_INDENT = "    "
_DETAIL_INDENT = "      "
_KIND_WIDTH = 10


def render_text(report: Report, options: RenderOptions) -> str:
    """Render the report as aligned plain text; the last line is :attr:`Report.exit_reason`."""
    lines: list[str] = [header_line(report), "", *_summary_table(report)]
    if not options.summary_only:
        blocks = group_findings(report, options)
        for block in blocks:
            lines.extend(["", *_block(block, options)])
        clean = clean_names(report, blocks, options)
        if clean:
            noun = "repo" if options.group_by == "repo" else "aspect"
            lines.extend(["", f"{_plural(len(clean), noun)} clean: {', '.join(clean)}"])
        if report.candidates and not options.hide_candidates:
            lines.extend(["", *_candidates(report)])
    if report.errors:
        lines.extend(["", *_errors(report.errors, options)])
    lines.extend(["", *_footer(report, options), report.exit_reason])
    return "\n".join(lines) + "\n"


# ----- summary table ---------------------------------------------------------------------------------------------


def _summary_rows(report: Report) -> list[tuple[str, str, str, str, str, str, str]]:
    """``(repo, status, error, warn, info, candidates, note)`` per repository plus a TOTAL row."""
    by_repo = report.summary()["by_repo"]
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    totals = [0, 0, 0, 0]
    for status in (report.main, *report.repos):
        name = display_name(report, status.name)
        if status.status != "ok":
            rows.append((name, status.status, DASH, DASH, DASH, DASH, status.error or ""))
            continue
        counts = by_repo.get(status.name, {})
        values = [int(counts.get(key, 0)) for key in ("error", "warning", "info", "candidates")]
        totals = [a + b for a, b in zip(totals, values, strict=True)]
        candidates = DASH if status.is_main else str(values[3])
        rows.append((name, status.status, str(values[0]), str(values[1]), str(values[2]), candidates, ""))
    rows.append(("TOTAL", "", str(totals[0]), str(totals[1]), str(totals[2]), str(totals[3]), ""))
    return rows


def _summary_table(report: Report) -> list[str]:
    header = ("repo", "status", "error", "warn", "info", "candidates", "")
    rows = [header, *_summary_rows(report)]
    widths = [max(len(row[column]) for row in rows) for column in range(6)]
    out: list[str] = []
    for repo, status, error, warn, info, candidates, note in rows:
        line = (
            f"{repo:<{widths[0]}} {status:<{widths[1]}}  {error:>{widths[2]}}  {warn:>{widths[3]}}"
            f"  {info:>{widths[4]}}  {candidates:>{widths[5]}}"
        )
        if note:
            line += f"   {note}"
        out.append(line)
    return out


# ----- detail blocks ---------------------------------------------------------------------------------------------


def _block(block: Block, options: RenderOptions) -> list[str]:
    out = [block.title]
    locator_width = min(MAX_LOCATOR, max((len(f.locator) for f in block.findings), default=0))
    for section in block.sections:
        out.append(f"  {section.title}")
        for finding in section.findings:
            out.append(_finding_line(finding, locator_width, options))
            out.extend(_DETAIL_INDENT + line for line in detail_lines(finding, options))
    if block.hidden:
        out.append(f"  ... {hidden_note(block.hidden, options)}")
    return out


def _severity_token(severity: Severity, options: RenderOptions) -> str:
    token = f"{SEVERITY_ABBREVIATION[severity]:<5}"
    if options.color:
        return f"{_ANSI[severity]}{token}{_RESET}"
    return token


def _tag(finding: Finding) -> str:
    if finding.suppressed:
        return "[suppressed] "
    if finding.baselined:
        return "[baselined] "
    return ""


def _finding_line(finding: Finding, locator_width: int, options: RenderOptions) -> str:
    severity = _severity_token(finding.severity, options)
    kind = f"{finding.kind.value:<{_KIND_WIDTH}}"
    locator = f"{truncate_locator(finding.locator):<{locator_width}}"
    line = f"{_FINDING_INDENT}{severity} {kind} {locator}  {_tag(finding)}{finding.message}"
    if options.verbose >= 1 and finding.option:
        line += f"  ({finding.option})"
    return line


# ----- candidates, errors, footer --------------------------------------------------------------------------------


def _candidates(report: Report) -> list[str]:
    out = ["Candidates for main (upstream, never fail the build)"]
    rows = [
        (f"{c.support}/{c.total}", c.aspect, truncate_locator(c.locator), c.message, ", ".join(c.repos))
        for c in report.candidates
    ]
    widths = [max(len(row[column]) for row in rows) for column in range(4)]
    for support, aspect, locator, message, repos in rows:
        out.append(
            f"  {support:>{widths[0]}}  {aspect:<{widths[1]}}  {locator:<{widths[2]}}  {message:<{widths[3]}} ({repos})"
        )
    return out


def _error_location(error: RunError) -> str:
    return "/".join(part for part in (error.repo, error.aspect) if part) or "-"


def _errors(errors: Sequence[RunError], options: RenderOptions) -> list[str]:
    out = ["Errors"]
    width = max(len(_error_location(e)) for e in errors)
    for error in errors:
        out.append(f"  {error.stage:<7}  {_error_location(error):<{width}}  {error.message}")
        if options.verbose >= 2 and error.traceback:
            out.extend(_DETAIL_INDENT + line for line in error.traceback.rstrip().splitlines())
    return out


def _footer(report: Report, options: RenderOptions) -> list[str]:
    summary = report.summary()
    parts: list[str] = []
    ignored_items = sum(len(snapshot.ignored) for snapshot in report.snapshots.values())
    if ignored_items:
        parts.append(f"{_plural(ignored_items, 'item')} ignored by aspect config")
    parts.append(f"{_plural(int(summary['suppressed']), 'finding')} suppressed by ignore globs")
    parts.append(f"{int(summary['baselined'])} baselined")
    unlisted = [f for f in report.findings if f.hidden and not is_listed(f, options)]
    line = f"Ignored: {f' {MIDDLE_DOT} '.join(parts)}"
    if unlisted and options.verbose < 1:
        line += " (-v to list)"
    out = [line]
    if options.verbose >= 1:
        for finding in unlisted:
            tag = "suppressed" if finding.suppressed else "baselined"
            out.append(f"  [{tag}] {finding.repo}  {finding.kind.value}  {finding.locator}")
    if report.baseline_stale:
        out.append(
            f"{_plural(len(report.baseline_stale), 'baseline entry').replace('entrys', 'entries')} no longer "
            "produced; run --update-baseline to drop them"
        )
    return out


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
