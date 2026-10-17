"""GitHub-flavoured markdown report renderer (issue bodies, ``$GITHUB_STEP_SUMMARY``).

Same filtering and grouping as the text renderer: a ``## <repo>`` heading per block, ``### <aspect>`` per section,
one task-list item per finding with its detail in a collapsed ``<details>`` element, then the candidates table,
run errors, the "Ignored" footer and the exit reason as the last line.
"""

from __future__ import annotations

from collections.abc import Sequence

from sistent.model import Finding, Report, RunError
from sistent.render import (
    Block,
    RenderOptions,
    clean_names,
    detail_lines,
    display_name,
    group_findings,
    header_line,
    hidden_note,
    is_listed,
)

__all__ = ["render_markdown"]

DASH = "\u2013"
EM_DASH = "\u2014"
MIDDLE_DOT = "\u00b7"

_SEVERITY = {"error": "error", "warning": "warn", "info": "info"}


def render_markdown(report: Report, options: RenderOptions) -> str:
    """Render the report as GitHub-flavoured markdown; the last line is :attr:`Report.exit_reason`."""
    lines: list[str] = ["# sistent report", "", header_line(report), "", *_summary_table(report)]
    if not options.summary_only:
        blocks = group_findings(report, options)
        for block in blocks:
            lines.extend(["", *_block(block, options)])
        clean = clean_names(report, blocks, options)
        if clean:
            noun = "repo" if options.group_by == "repo" else "aspect"
            lines.extend(["", f"**{_plural(len(clean), noun)} clean:** {', '.join(clean)}"])
        if report.candidates and not options.hide_candidates:
            lines.extend(["", *_candidates(report)])
    if report.errors:
        lines.extend(["", *_errors(report.errors, options)])
    lines.extend(["", *_footer(report, options), "", report.exit_reason])
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    """Escape a table cell: pipes would split the cell and newlines would end the row."""
    return text.replace("|", "\\|").replace("\n", " ")


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(_cell(c) for c in cells) + " |"


# ----- summary ---------------------------------------------------------------------------------------------------


def _summary_table(report: Report) -> list[str]:
    by_repo = report.summary()["by_repo"]
    out = [_row(("repo", "status", "error", "warn", "info", "candidates")), "|---|---|---:|---:|---:|---:|"]
    totals = [0, 0, 0, 0]
    for status in (report.main, *report.repos):
        name = display_name(report, status.name)
        if status.status != "ok":
            note = f"{status.status}: {status.error}" if status.error else status.status
            out.append(_row((name, note, DASH, DASH, DASH, DASH)))
            continue
        counts = by_repo.get(status.name, {})
        values = [int(counts.get(key, 0)) for key in ("error", "warning", "info", "candidates")]
        totals = [a + b for a, b in zip(totals, values, strict=True)]
        candidates = DASH if status.is_main else str(values[3])
        out.append(_row((name, status.status, str(values[0]), str(values[1]), str(values[2]), candidates)))
    out.append(_row(("**TOTAL**", "", *(str(t) for t in totals))))
    return out


# ----- detail blocks ---------------------------------------------------------------------------------------------


def _block(block: Block, options: RenderOptions) -> list[str]:
    out = [f"## {block.title}"]
    for section in block.sections:
        out.extend(["", f"### {section.title}", ""])
        for finding in section.findings:
            out.append(_item(finding, options))
            out.extend(_details(finding, options))
    if block.hidden:
        out.extend(["", f"_... {hidden_note(block.hidden, options)}_"])
    return out


def _tag(finding: Finding) -> str:
    if finding.suppressed:
        return "[suppressed] "
    if finding.baselined:
        return "[baselined] "
    return ""


def _item(finding: Finding, options: RenderOptions) -> str:
    severity = _SEVERITY[finding.severity.value]
    line = f"- [ ] **{severity}** `{finding.locator}` {EM_DASH} {_tag(finding)}{finding.message}"
    if options.verbose >= 1 and finding.option:
        line += f" (`{finding.option}`)"
    return line


def _details(finding: Finding, options: RenderOptions) -> list[str]:
    lines = detail_lines(finding, options)
    if not lines:
        return []
    language = "diff" if finding.detail_kind == "diff" else ""
    body = ["<details><summary>detail</summary>", "", f"```{language}", *lines, "```", "</details>"]
    return [f"  {line}" if line else "" for line in body]


# ----- candidates, errors, footer --------------------------------------------------------------------------------


def _candidates(report: Report) -> list[str]:
    header = _row(("support", "aspect", "locator", "message", "repos"))
    out = ["## Candidates for main", "", header, "|---|---|---|---|---|"]
    for c in report.candidates:
        out.append(_row((f"{c.support}/{c.total}", c.aspect, f"`{c.locator}`", c.message, ", ".join(c.repos))))
    return out


def _errors(errors: Sequence[RunError], options: RenderOptions) -> list[str]:
    out = ["## Errors", ""]
    for error in errors:
        where = "/".join(part for part in (error.repo, error.aspect) if part) or "-"
        out.append(f"- **{error.stage}** {where} {EM_DASH} {error.message}")
        if options.verbose >= 2 and error.traceback:
            out.extend(["", "  ```", *(f"  {line}" for line in error.traceback.rstrip().splitlines()), "  ```", ""])
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
    if options.verbose >= 1 and unlisted:
        out.append("")
        for finding in unlisted:
            tag = "suppressed" if finding.suppressed else "baselined"
            out.append(f"- [{tag}] {finding.repo} {finding.kind.value} `{finding.locator}`")
    return out


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"
