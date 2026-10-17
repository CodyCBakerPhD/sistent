"""Text, markdown and JSON renderers on a hand-built report."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

import pytest

from sistent.model import (
    AspectInfo,
    Direction,
    Finding,
    Identity,
    Kind,
    Report,
    RepoStatus,
    RunError,
    Severity,
    Snapshot,
    Subject,
    aggregate_candidates,
)
from sistent.render import RenderOptions, filter_findings, group_findings, render
from sistent.render.json_ import render_json
from sistent.render.markdown import render_markdown
from sistent.render.text import render_text

DASH = "\u2013"
DOT = "\u00b7"
ELLIPSIS = "\u2026"
EM_DASH = "\u2014"

EXIT_REASON = "1 error \u2192 exit 1 (fail_on = error); 1 satellite unavailable \u2192 exit 3"

LICENSE_DIFF = "\n".join(
    [
        "--- neuroconv/README.md#License",
        "+++ roiextractors/README.md#License",
        "@@ -1,12 +1,9 @@",
        " {{name}} is distributed under the BSD-3 license.",
        *(f"-removed line {i}" for i in range(1, 7)),
        *(f"+added line {i}" for i in range(1, 7)),
    ]
)
assert len(LICENSE_DIFF.splitlines()) == 16


def finding(
    repo: str,
    aspect: str,
    kind: Kind,
    subject: Subject,
    locator: str,
    message: str,
    *,
    severity: Severity = Severity.WARNING,
    direction: Direction = Direction.DOWNSTREAM,
    **extra: Any,
) -> Finding:
    return Finding(
        aspect=aspect,
        repo=repo,
        kind=kind,
        subject=subject,
        severity=severity,
        direction=direction,
        locator=locator,
        message=message,
        **extra,
    )


def status(name: str, *, is_main: bool = False, error: str | None = None, head: str | None = None) -> RepoStatus:
    ok = error is None
    return RepoStatus(
        name=name,
        is_main=is_main,
        source=f"../{name}",
        resolved=f"/work/{name}" if ok else None,
        rev=None,
        head=head,
        status="ok" if ok else "unavailable",
        error=error,
        tags=("python",),
        identity=Identity(name=name, aliases=(name,), org="org", branch="main", slug=f"github.com/org/{name}")
        if ok
        else None,
    )


def make_findings() -> list[Finding]:
    """Findings across repos, aspects, kinds, severities and directions, including hidden ones."""
    return [
        # main: self-check (direction none)
        finding(
            "neuroconv",
            "readme_badges",
            Kind.STALE,
            Subject.BADGE,
            "README.md#badges",
            "mentions 'pynwb' (repo pynwb) in pypi/l badge",
            direction=Direction.NONE,
        ),
        # roiextractors
        finding(
            "roiextractors",
            "agent_instructions",
            Kind.MISSING,
            Subject.FILE,
            "AGENTS.md",
            "no agent-instruction file (main has AGENTS.md)",
            severity=Severity.ERROR,
            option="aspects.agent_instructions.severity.missing.file=error",
        ),
        finding(
            "roiextractors",
            "readme",
            Kind.MISSING,
            Subject.SECTION,
            "README.md#Installation > From source",
            "section missing (1 subsection, 2 rules)",
            content_key="installation > from source",
        ),
        finding(
            "roiextractors",
            "readme",
            Kind.DIFFERS,
            Subject.PROSE,
            "README.md#License",
            "similarity 0.41 (+6 \u22126 lines; --diff to show)",
            direction=Direction.NONE,
            detail=LICENSE_DIFF,
            detail_kind="diff",
            option="aspects.readme.similarity_threshold=0.6",
        ),
        finding(
            "roiextractors",
            "readme",
            Kind.MOVED,
            Subject.SECTION,
            "README.md#Usage > Example",
            "main: Usage > Example ; repo: Example",
            severity=Severity.INFO,
        ),
        finding(
            "roiextractors",
            "readme",
            Kind.EXTRA,
            Subject.SECTION,
            "README.md#Funding",
            "section not in main",
            severity=Severity.INFO,
            direction=Direction.UPSTREAM,
            content_key="funding",
        ),
        finding(
            "roiextractors",
            "agent_instructions",
            Kind.EXTRA,
            Subject.SECTION,
            "AGENTS.md#Release process",
            "section not in main",
            severity=Severity.INFO,
            direction=Direction.UPSTREAM,
            content_key="release process",
        ),
        finding(
            "roiextractors",
            "readme",
            Kind.DIFFERS,
            Subject.SECTION,
            "README.md#Contributing",
            "section differs (identical required)",
            suppressed=True,
        ),
        finding(
            "roiextractors",
            "pyproject",
            Kind.DIFFERS,
            Subject.VALUE,
            "pyproject.toml:tool.ruff.line-length",
            "main: 120 ; repo: 100",
            baselined=True,
        ),
        finding(
            "roiextractors",
            "layout",
            Kind.MISSING,
            Subject.PATH,
            "docs/",
            "directory missing",
            severity=Severity.INFO,
        ),
        # spikeinterface
        finding(
            "spikeinterface",
            "agent_instructions",
            Kind.MISSING,
            Subject.RULE,
            "AGENTS.md#Development > Testing",
            "rule missing: 'Run pytest before committing'",
            detail="Run pytest before committing\n- Use pytest -x\nNever skip tests",
            detail_kind="list",
            content_key="run pytest before committing",
        ),
        finding(
            "spikeinterface",
            "agent_instructions",
            Kind.EXTRA,
            Subject.SECTION,
            "AGENTS.md#Release process",
            "section not in main",
            severity=Severity.INFO,
            direction=Direction.UPSTREAM,
            content_key="release process",
        ),
        finding(
            "spikeinterface",
            "readme_badges",
            Kind.EXTRA,
            Subject.BADGE,
            "README.md#badges",
            "badge: pypi-downloads",
            severity=Severity.INFO,
            direction=Direction.UPSTREAM,
            content_key="pypi-downloads",
        ),
        finding(
            "spikeinterface",
            "readme",
            Kind.REORDERED,
            Subject.SECTION,
            "README.md",
            "sections reordered: Usage before Installation",
            severity=Severity.INFO,
        ),
    ]


def make_report(**overrides: Any) -> Report:
    """Main + three satellites (one unavailable), findings of every flavour, candidates and one run error."""
    findings = tuple(overrides.pop("findings", make_findings()))
    candidates = overrides.pop(
        "candidates",
        aggregate_candidates(findings, {"agent_instructions": 3, "readme": 3, "readme_badges": 3}),
    )
    fields: dict[str, Any] = {
        "sistent_version": "0.1.0",
        "generated_at": "2026-09-19T12:00:00Z",
        "config_path": "/work/sistent.toml",
        "main": status("neuroconv", is_main=True, head="3f9a2c1deadbeef"),
        "repos": (
            status("roiextractors", head="0000001"),
            status("spikeinterface", head="0000002"),
            status("docs-site", error="git fetch failed: repository not found"),
        ),
        "aspects": tuple(
            AspectInfo(name=name, type=type_, options={}, targets=("roiextractors", "spikeinterface"))
            for name, type_ in [
                ("agent_instructions", "markdown"),
                ("readme", "markdown"),
                ("readme_badges", "badges"),
                ("layout", "tree"),
                ("pyproject", "toml"),
            ]
        ),
        "findings": findings,
        "candidates": candidates,
        "errors": (
            RunError(
                stage="extract",
                repo="spikeinterface",
                aspect="pyproject",
                message="tomllib: Invalid statement (at line 3, column 1)",
                traceback='Traceback (most recent call last):\n  File "x.py", line 1\nTOMLDecodeError: boom',
            ),
        ),
        "fail_on": Severity.ERROR,
        "exit_code": 3,
        "exit_reason": EXIT_REASON,
    }
    fields.update(overrides)
    return Report(**fields)


def block_lines(text: str, title: str) -> list[str]:
    """The lines of the text block that starts with ``title`` (up to the next blank line)."""
    lines = text.splitlines()
    start = lines.index(title)
    end = next((i for i in range(start, len(lines)) if not lines[i]), len(lines))
    return lines[start:end]


# ----- shared filtering ------------------------------------------------------------------------------------------


class TestFilterFindings:
    def test_default_hides_hidden_and_upstream(self) -> None:
        listed = filter_findings(make_report(), RenderOptions())
        assert all(not f.hidden for f in listed)
        assert all(f.direction is not Direction.UPSTREAM for f in listed)
        assert len(listed) == 8

    def test_direction_upstream_only(self) -> None:
        listed = filter_findings(make_report(), RenderOptions(direction="upstream"))
        assert len(listed) == 4
        assert all(f.direction is Direction.UPSTREAM for f in listed)

    def test_direction_both_lists_everything_visible(self) -> None:
        report = make_report()
        listed = filter_findings(report, RenderOptions(direction="both"))
        assert listed == list(report.visible_findings)

    def test_show_flags_include_hidden(self) -> None:
        report = make_report()
        opts = RenderOptions(show_suppressed=True, show_baseline=True, direction="both")
        assert filter_findings(report, opts) == list(report.findings)
        only_suppressed = filter_findings(report, RenderOptions(show_suppressed=True, direction="both"))
        assert any(f.suppressed for f in only_suppressed)
        assert not any(f.baselined for f in only_suppressed)

    def test_min_severity_and_kinds(self) -> None:
        report = make_report()
        errors = filter_findings(report, RenderOptions(min_severity=Severity.ERROR))
        assert [f.severity for f in errors] == [Severity.ERROR]
        moved = filter_findings(report, RenderOptions(kinds=frozenset({"moved"})))
        assert [f.kind for f in moved] == [Kind.MOVED]

    def test_group_by_aspect_titles(self) -> None:
        blocks = group_findings(make_report(), RenderOptions(group_by="aspect"))
        assert [b.title for b in blocks] == ["agent_instructions", "readme", "readme_badges", "layout"]
        readme = blocks[1]
        assert [s.title for s in readme.sections] == ["roiextractors", "spikeinterface"]
        badges = blocks[2]
        assert [s.title for s in badges.sections] == ["neuroconv (main)"]


# ----- text ------------------------------------------------------------------------------------------------------


class TestText:
    def test_header_line(self) -> None:
        text = render_text(make_report(), RenderOptions())
        assert text.splitlines()[0] == (
            f"sistent 0.1.0 {DOT} main neuroconv @3f9a2c1 {DOT} 3 satellites {DOT} 5 aspects {DOT} fail_on = error"
        )

    def test_header_without_head_and_singular_counts(self) -> None:
        report = make_report(main=status("neuroconv", is_main=True), repos=(status("a"),), aspects=(), fail_on=None)
        assert render_text(report, RenderOptions()).splitlines()[0] == (
            f"sistent 0.1.0 {DOT} main neuroconv {DOT} 1 satellite {DOT} 0 aspects {DOT} fail_on = never"
        )

    def test_summary_table_exact(self) -> None:
        text = render_text(make_report(), RenderOptions())
        expected = "\n".join(
            [
                "repo             status       error  warn  info  candidates",
                f"neuroconv (main) ok               0     1     0           {DASH}",
                "roiextractors    ok               1     2     2           2",
                "spikeinterface   ok               0     1     1           2",
                f"docs-site        unavailable      {DASH}     {DASH}     {DASH}           {DASH}"
                "   git fetch failed: repository not found",
                "TOTAL                             1     4     3           4",
            ]
        )
        assert expected in text
        assert text.splitlines()[2].startswith("repo ")

    def test_summary_table_widths_follow_content(self) -> None:
        report = make_report(repos=(status("a"),), findings=(), candidates=(), errors=())
        lines = render_text(report, RenderOptions()).splitlines()
        assert lines[2] == "repo             status  error  warn  info  candidates"
        assert lines[3] == f"neuroconv (main) ok          0     0     0           {DASH}"
        assert lines[5] == "TOTAL                        0     0     0           0"

    def test_per_repo_blocks_and_alignment(self) -> None:
        text = render_text(make_report(), RenderOptions())
        assert block_lines(text, "neuroconv (main)") == [
            "neuroconv (main)",
            "  readme_badges",
            "    warn  stale      README.md#badges  mentions 'pynwb' (repo pynwb) in pypi/l badge",
        ]
        roi = block_lines(text, "roiextractors")
        assert roi[:3] == [
            "roiextractors",
            "  agent_instructions",
            "    error missing    AGENTS.md                             no agent-instruction file (main has AGENTS.md)",
        ]
        assert roi[3] == "  readme"
        assert roi[4] == (
            "    warn  missing    README.md#Installation > From source  section missing (1 subsection, 2 rules)"
        )
        assert roi[5].startswith("    warn  differs    README.md#License                     similarity 0.41")
        # the diff detail is indented six spaces under its finding
        assert roi[6] == "      --- neuroconv/README.md#License"
        assert "  layout" in roi
        assert roi[-2] == "    info  missing    docs/                                 directory missing"
        # the two upstream findings are relocated to the candidates section and counted here
        assert roi[-1] == "  ... 2 info findings hidden (--direction both to show)"
        # blocks appear in config order, main first
        order = [text.index(t) for t in ("neuroconv (main)\n", "roiextractors\n", "spikeinterface\n")]
        assert order == sorted(order)
        # hidden (suppressed/baselined) findings are not listed by default
        assert "Contributing" not in text
        assert "line-length" not in text

    def test_group_by_aspect(self) -> None:
        text = render_text(make_report(), RenderOptions(group_by="aspect"))
        readme = block_lines(text, "readme")
        assert readme[0] == "readme"
        assert readme[1] == "  roiextractors"
        assert "  spikeinterface" in readme
        assert "    info  reordered  README.md" in "\n".join(readme)
        badges = block_lines(text, "readme_badges")
        assert badges[1] == "  neuroconv (main)"
        assert "\nroiextractors\n" not in text

    def test_min_severity_hides_and_counts(self) -> None:
        text = render_text(make_report(), RenderOptions(min_severity=Severity.WARNING))
        roi = block_lines(text, "roiextractors")
        assert not any(line.startswith("    info") for line in roi)
        # the moved section and the layout info finding are hidden by severity, the two upstream ones by direction
        assert roi[-1] == "  ... 4 info findings hidden (--min-severity info, --direction both to show)"
        spike = block_lines(text, "spikeinterface")
        assert spike[-1] == "  ... 3 info findings hidden (--min-severity info, --direction both to show)"
        assert "  ... " not in "\n".join(block_lines(text, "neuroconv (main)"))

    def test_hidden_note_singular_and_mixed(self) -> None:
        text = render_text(make_report(), RenderOptions(direction="both", min_severity=Severity.ERROR))
        roi = block_lines(text, "roiextractors")
        # two warnings and four infos hidden, so no single severity word; the hint names the lowest hidden severity
        assert roi[-1] == "  ... 6 findings hidden (--min-severity info to show)"
        text = render_text(make_report(), RenderOptions(direction="both", min_severity=Severity.WARNING))
        assert block_lines(text, "spikeinterface")[-1] == "  ... 3 info findings hidden (--min-severity info to show)"

    def test_direction_filter(self) -> None:
        report = make_report()
        default = render_text(report, RenderOptions())
        assert "    info  extra" not in default
        upstream = render_text(report, RenderOptions(direction="upstream"))
        roi = block_lines(upstream, "roiextractors")
        assert [line for line in roi if line.startswith("    ")] == [
            "    info  extra      AGENTS.md#Release process  section not in main",
            "    info  extra      README.md#Funding          section not in main",
        ]
        assert roi[-1] == "  ... 5 findings hidden (--direction both to show)"
        both = render_text(report, RenderOptions(direction="both"))
        assert "    info  extra" in both
        assert "    error missing" in both
        assert "hidden (" not in both

    def test_kinds_filter(self) -> None:
        text = render_text(make_report(), RenderOptions(kinds=frozenset({"missing", "stale"})))
        listed = [line for line in text.splitlines() if line.startswith("    ") and not line.startswith("      ")]
        # stale badge (main), AGENTS.md + Installation + docs/ missing (roiextractors), rule missing (spikeinterface)
        assert len(listed) == 5
        assert all(("missing" in line or "stale" in line) for line in listed)
        roi = block_lines(text, "roiextractors")
        assert (
            roi[-1] == "  ... 4 findings hidden (--direction both, --kind differs, --kind extra, --kind moved to show)"
        )

    def test_detail_truncation_vs_full(self) -> None:
        report = make_report()
        text = render_text(report, RenderOptions())
        roi = block_lines(text, "roiextractors")
        detail = [line for line in roi if line.startswith("      ")]
        assert len(detail) == 11
        assert detail[-1] == "      ... (6 more lines; --diff to show)"
        full = render_text(report, RenderOptions(full_detail=True))
        detail = [line for line in block_lines(full, "roiextractors") if line.startswith("      ")]
        assert len(detail) == 16
        assert detail[-1] == "      +added line 6"
        assert "more lines" not in full

    def test_list_detail_items(self) -> None:
        text = render_text(make_report(), RenderOptions())
        spike = block_lines(text, "spikeinterface")
        assert [line for line in spike if line.startswith("      ")] == [
            "      - Run pytest before committing",
            "      - Use pytest -x",
            "      - Never skip tests",
        ]

    def test_clean_repos_collapse(self) -> None:
        report = make_report(findings=tuple(f for f in make_findings() if f.repo == "neuroconv"), candidates=())
        text = render_text(report, RenderOptions())
        assert "\n2 repos clean: roiextractors, spikeinterface\n" in text
        assert "\nroiextractors\n" not in text
        # an unavailable repo is never "clean"
        assert "docs-site" not in text.split("2 repos clean")[1].splitlines()[0]
        one = make_report(findings=(), candidates=(), repos=(status("a"),))
        assert "\n1 repo clean: a\n" in render_text(one, RenderOptions())

    def test_clean_aspects_when_grouped_by_aspect(self) -> None:
        text = render_text(make_report(), RenderOptions(group_by="aspect"))
        assert "\n1 aspect clean: pyproject\n" in text

    def test_show_suppressed_and_baseline_tags(self) -> None:
        report = make_report()
        text = render_text(report, RenderOptions(show_suppressed=True))
        assert "    warn  differs    README.md#Contributing                [suppressed] section differs" in text
        assert "line-length" not in text
        text = render_text(report, RenderOptions(show_baseline=True))
        assert (
            "  pyproject\n    warn  differs    pyproject.toml:tool.ruff.line-length  [baselined] main: 120 ; repo: 100"
            in text
        )
        assert "Contributing" not in text

    def test_sort_severity(self) -> None:
        report = make_report()
        text = render_text(report, RenderOptions(sort="severity", direction="both"))
        roi = block_lines(text, "roiextractors")
        rank = {"error": 0, "warn": 1, "info": 2}
        # aspect sections are ordered by their worst finding, findings within a section by severity
        sections: dict[str, list[str]] = {}
        for line in roi[1:]:
            if line.startswith("  ") and not line.startswith("    "):
                sections[line.strip()] = []
            elif line.startswith("    ") and not line.startswith("      "):
                next(reversed(sections.values())).append(line[4:9].strip())
        assert list(sections) == ["agent_instructions", "readme", "layout"]
        assert sections["agent_instructions"] == ["error", "info"]
        assert sections["readme"] == ["warn", "warn", "info", "info"]
        for severities in sections.values():
            assert severities == sorted(severities, key=rank.__getitem__)
        # stable within a severity: README missing section keeps preceding README differs
        assert roi.index(next(x for x in roi if "Installation" in x)) < roi.index(
            next(x for x in roi if "License" in x)
        )
        # blocks with worse findings come first
        assert text.index("\nroiextractors\n") < text.index("\nneuroconv (main)\n")
        config = render_text(report, RenderOptions(direction="both"))
        assert config.index("\nneuroconv (main)\n") < config.index("\nroiextractors\n")

    def test_candidates_section(self) -> None:
        text = render_text(make_report(), RenderOptions())
        section = block_lines(text, "Candidates for main (upstream, never fail the build)")
        assert section == [
            "Candidates for main (upstream, never fail the build)",
            "  2/3  agent_instructions  AGENTS.md#Release process  section not in main   (roiextractors, spikeinterface)",
            "  1/3  readme              README.md#Funding          section not in main   (roiextractors)",
            "  1/3  readme_badges       README.md#badges           badge: pypi-downloads (spikeinterface)",
        ]

    def test_candidates_omitted(self) -> None:
        report = make_report()
        assert "Candidates for main" not in render_text(report, RenderOptions(hide_candidates=True))
        assert "Candidates for main" not in render_text(report, RenderOptions(summary_only=True))
        assert "Candidates for main" not in render_text(make_report(candidates=()), RenderOptions())

    def test_summary_only(self) -> None:
        text = render_text(make_report(), RenderOptions(summary_only=True))
        assert "TOTAL" in text
        assert "\nroiextractors\n" not in text
        assert "AGENTS.md" not in text.split("Errors")[0]
        assert "Errors\n" in text
        assert text.endswith(EXIT_REASON + "\n")

    def test_exit_reason_is_last_line(self) -> None:
        report = make_report()
        for opts in (RenderOptions(), RenderOptions(summary_only=True), RenderOptions(verbose=2, color=True)):
            text = render_text(report, opts)
            assert text.endswith("\n")
            assert text.splitlines()[-1] == EXIT_REASON

    def test_footer_counts(self) -> None:
        report = make_report()
        text = render_text(report, RenderOptions())
        assert f"\nIgnored: 1 finding suppressed by ignore globs {DOT} 1 baselined (-v to list)\n" in text
        listed = render_text(report, RenderOptions(verbose=1))
        assert "(-v to list)" not in listed
        assert "\n  [suppressed] roiextractors  differs  README.md#Contributing\n" in listed
        assert "\n  [baselined] roiextractors  differs  pyproject.toml:tool.ruff.line-length\n" in listed
        shown = render_text(report, RenderOptions(show_suppressed=True, show_baseline=True))
        assert "(-v to list)" not in shown

    def test_footer_counts_config_ignored_items(self) -> None:
        snapshots = {
            ("roiextractors", "readme"): Snapshot(
                aspect="readme", repo="roiextractors", schema_version=1, data={}, ignored=("Skills", "TOC")
            ),
        }
        text = render_text(make_report(snapshots=snapshots), RenderOptions())
        assert "Ignored: 2 items ignored by aspect config" in text

    def test_verbose_option_citation(self) -> None:
        report = make_report()
        plain = render_text(report, RenderOptions())
        assert "(aspects.agent_instructions.severity.missing.file=error)" not in plain
        verbose = render_text(report, RenderOptions(verbose=1))
        assert (
            "    error missing    AGENTS.md                             no agent-instruction file (main has AGENTS.md)"
            "  (aspects.agent_instructions.severity.missing.file=error)"
        ) in verbose

    def test_errors_section_and_traceback(self) -> None:
        report = make_report()
        text = render_text(report, RenderOptions())
        assert (
            "\nErrors\n  extract  spikeinterface/pyproject  tomllib: Invalid statement (at line 3, column 1)\n" in text
        )
        assert "Traceback" not in text
        assert "Traceback" not in render_text(report, RenderOptions(verbose=1))
        verbose = render_text(report, RenderOptions(verbose=2))
        assert "\n      Traceback (most recent call last):\n" in verbose
        assert "\n      TOMLDecodeError: boom\n" in verbose

    def test_color_codes_only_when_requested(self) -> None:
        report = make_report()
        assert "\x1b[" not in render_text(report, RenderOptions())
        colored = render_text(report, RenderOptions(color=True))
        assert "\x1b[31merror\x1b[0m missing" in colored
        assert "\x1b[33mwarn \x1b[0m" in colored
        assert "\x1b[36minfo \x1b[0m" in colored

    def test_locator_truncation(self) -> None:
        long = "README.md#" + "x" * 80
        report = make_report(
            findings=(finding("roiextractors", "readme", Kind.MISSING, Subject.SECTION, long, "gone"),), candidates=()
        )
        line = next(x for x in render_text(report, RenderOptions()).splitlines() if x.startswith("    warn"))
        shown = line.split()[2]
        assert len(shown) == 60
        assert shown.endswith(ELLIPSIS)
        assert line.endswith("  gone")

    def test_render_dispatch(self) -> None:
        report = make_report()
        assert render(report, "text") == render_text(report, RenderOptions())
        assert render(report, "markdown") == render_markdown(report, RenderOptions())
        assert render(report, "json") == render_json(report, RenderOptions())
        opts = RenderOptions(summary_only=True)
        assert render(report, "text", opts) == render_text(report, opts)
        with pytest.raises(ValueError, match="unknown report format"):
            render(report, "yaml")  # type: ignore[arg-type]


# ----- markdown --------------------------------------------------------------------------------------------------


class TestMarkdown:
    def test_header_and_table(self) -> None:
        md = render_markdown(make_report(), RenderOptions())
        lines = md.splitlines()
        assert lines[0] == "# sistent report"
        assert lines[2].startswith(f"sistent 0.1.0 {DOT} main neuroconv @3f9a2c1")
        assert "| repo | status | error | warn | info | candidates |" in lines
        assert "|---|---|---:|---:|---:|---:|" in lines
        assert f"| neuroconv (main) | ok | 0 | 1 | 0 | {DASH} |" in lines
        assert "| roiextractors | ok | 1 | 2 | 2 | 2 |" in lines
        assert (
            f"| docs-site | unavailable: git fetch failed: repository not found | {DASH} | {DASH} | {DASH} | {DASH} |"
            in lines
        )
        assert "| **TOTAL** |  | 1 | 4 | 3 | 4 |" in lines

    def test_task_items_and_headings(self) -> None:
        md = render_markdown(make_report(), RenderOptions())
        assert "\n## neuroconv (main)\n" in md
        assert "\n## roiextractors\n" in md
        assert "\n### agent_instructions\n" in md
        assert f"- [ ] **error** `AGENTS.md` {EM_DASH} no agent-instruction file (main has AGENTS.md)" in md
        assert (
            f"- [ ] **warn** `README.md#Installation > From source` {EM_DASH} section missing (1 subsection, 2 rules)"
            in md
        )
        assert f"- [ ] **info** `README.md#Usage > Example` {EM_DASH} main: Usage > Example ; repo: Example" in md
        # upstream findings are not listed per repo by default (they are in the candidates table)
        assert "`README.md#Funding`" not in md.split("## Candidates")[0]
        assert md.index("## neuroconv (main)") < md.index("## roiextractors") < md.index("## spikeinterface")

    def test_details_block(self) -> None:
        report = make_report()
        md = render_markdown(report, RenderOptions())
        assert "  <details><summary>detail</summary>\n\n  ```diff\n  --- neuroconv/README.md#License\n" in md
        assert "  ... (6 more lines; --diff to show)\n  ```\n  </details>\n" in md
        assert "  ```\n  - Run pytest before committing\n  - Use pytest -x\n  - Never skip tests\n  ```\n" in md
        full = render_markdown(report, RenderOptions(full_detail=True))
        assert "more lines" not in full
        assert "  +added line 6\n  ```\n" in full

    def test_same_filters_as_text(self) -> None:
        report = make_report()
        md = render_markdown(report, RenderOptions(min_severity=Severity.WARNING, group_by="aspect"))
        assert "\n## readme\n" in md
        assert "\n### roiextractors\n" in md
        assert "**info**" not in md
        # readme block: moved + reordered hidden by severity, the upstream Funding section by severity and direction
        assert "_... 3 info findings hidden (--min-severity info, --direction both to show)_" in md
        assert "[suppressed]" in render_markdown(report, RenderOptions(show_suppressed=True))
        assert "(`aspects.readme.similarity_threshold=0.6`)" in render_markdown(report, RenderOptions(verbose=1))
        clean = render_markdown(make_report(findings=(), candidates=()), RenderOptions())
        assert "**2 repos clean:** roiextractors, spikeinterface" in clean

    def test_candidates_table(self) -> None:
        md = render_markdown(make_report(), RenderOptions())
        assert (
            "\n## Candidates for main\n\n| support | aspect | locator | message | repos |\n|---|---|---|---|---|\n"
            in md
        )
        assert (
            "| 2/3 | agent_instructions | `AGENTS.md#Release process` | section not in main | roiextractors, spikeinterface |"
            in md
        )
        assert "| 1/3 | readme_badges | `README.md#badges` | badge: pypi-downloads | spikeinterface |" in md
        assert "Candidates" not in render_markdown(make_report(), RenderOptions(hide_candidates=True))

    def test_escaping(self) -> None:
        findings = (
            finding(
                "roiextractors",
                "readme",
                Kind.EXTRA,
                Subject.SECTION,
                "README.md#A | B",
                "pipe | here",
                severity=Severity.INFO,
                direction=Direction.UPSTREAM,
            ),
        )
        report = make_report(
            findings=findings,
            candidates=aggregate_candidates(findings, {"readme": 2}),
            repos=(status("roiextractors"), status("x", error="bad | url")),
        )
        md = render_markdown(report, RenderOptions())
        assert "| 1/2 | readme | `README.md#A \\| B` | pipe \\| here | roiextractors |" in md
        assert "| x | unavailable: bad \\| url |" in md

    def test_errors_and_exit_reason(self) -> None:
        report = make_report()
        md = render_markdown(report, RenderOptions())
        assert f"\n## Errors\n\n- **extract** spikeinterface/pyproject {EM_DASH} tomllib: Invalid statement" in md
        assert "Traceback" not in md
        assert "  ```\n  Traceback (most recent call last):" in render_markdown(report, RenderOptions(verbose=2))
        assert md.endswith(f"\n{EXIT_REASON}\n")
        assert f"Ignored: 1 finding suppressed by ignore globs {DOT} 1 baselined (-v to list)" in md


# ----- json ------------------------------------------------------------------------------------------------------


class TestJson:
    def test_round_trip_equals_to_dict(self) -> None:
        report = make_report()
        out = render_json(report, RenderOptions())
        assert out.endswith("\n")
        assert json.loads(out) == report.to_dict()

    def test_keys_per_spec(self) -> None:
        doc = json.loads(render_json(make_report(), RenderOptions()))
        assert list(doc) == [
            "schema_version",
            "sistent_version",
            "generated_at",
            "config_path",
            "fail_on",
            "exit_code",
            "exit_reason",
            "main",
            "repos",
            "aspects",
            "summary",
            "findings",
            "candidates",
            "errors",
            "baseline_stale",
        ]
        assert doc["schema_version"] == 1
        assert doc["fail_on"] == "error"
        assert doc["exit_code"] == 3
        assert doc["main"]["is_main"] is True
        assert [r["name"] for r in doc["repos"]] == ["roiextractors", "spikeinterface", "docs-site"]
        assert set(doc["summary"]) >= {"by_repo", "by_severity", "by_direction", "new", "baselined", "suppressed"}
        assert doc["summary"]["by_repo"]["roiextractors"] == {
            "error": 1,
            "warning": 2,
            "info": 2,
            "candidates": 2,
            "suppressed": 1,
            "baselined": 1,
        }
        first = doc["findings"][0]
        assert set(first) == {
            "id", "candidate_key", "aspect", "repo", "kind", "subject", "severity", "direction", "locator",
            "message", "detail", "detail_kind", "option", "content_key", "suppressed", "baselined",
        }  # fmt: skip
        assert set(doc["candidates"][0]) == {
            "candidate_key",
            "aspect",
            "kind",
            "subject",
            "locator",
            "content_key",
            "message",
            "repos",
            "support",
            "total",
        }
        assert doc["errors"][0] == {
            "stage": "extract",
            "repo": "spikeinterface",
            "aspect": "pyproject",
            "message": "tomllib: Invalid statement (at line 3, column 1)",
            "traceback": 'Traceback (most recent call last):\n  File "x.py", line 1\nTOMLDecodeError: boom',
        }

    def test_never_filtered(self) -> None:
        report = make_report()
        restrictive = RenderOptions(min_severity=Severity.ERROR, kinds=frozenset({"missing"}), summary_only=True)
        doc = json.loads(render_json(report, restrictive))
        assert len(doc["findings"]) == len(report.findings)
        assert any(f["suppressed"] for f in doc["findings"])
        assert any(f["baselined"] for f in doc["findings"])
        assert render(report, "json", restrictive) == render(report, "json")

    def test_unicode_is_not_escaped(self) -> None:
        assert "\u2212" in render_json(make_report(), RenderOptions())


def test_renderers_depend_only_on_model() -> None:
    code = (
        "import sys, sistent.render, sistent.render.text, sistent.render.markdown, sistent.render.json_, sistent.baseline;"
        "print(sorted(m for m in sys.modules if m.startswith('sistent')))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    loaded = json.loads(out.replace("'", '"'))
    assert loaded == [
        "sistent",
        "sistent.baseline",
        "sistent.model",
        "sistent.render",
        "sistent.render.json_",
        "sistent.render.markdown",
        "sistent.render.text",
    ]
