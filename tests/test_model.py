"""Tests for the core data model: enums, locators, findings, reports and candidate aggregation."""

from __future__ import annotations

import dataclasses
import json
import re
from typing import Any

import pytest

from sistent.model import (
    SCHEMA_VERSION,
    AspectInfo,
    Candidate,
    Direction,
    Finding,
    Kind,
    Report,
    RepoStatus,
    RunError,
    Severity,
    Subject,
    aggregate_candidates,
    locator,
)

LOCATOR_RE = re.compile(r"^[^#:]+([#:].+)?$")


def make_finding(**overrides: Any) -> Finding:
    base: dict[str, Any] = {
        "aspect": "readme",
        "repo": "roiextractors",
        "kind": Kind.MISSING,
        "subject": Subject.SECTION,
        "severity": Severity.WARNING,
        "direction": Direction.DOWNSTREAM,
        "locator": "README.md#Installation",
        "message": "section 'Installation' missing",
        "content_key": "installation",
    }
    base.update(overrides)
    return Finding(**base)


def make_status(name: str, *, is_main: bool = False, status: str = "ok") -> RepoStatus:
    return RepoStatus(
        name=name,
        is_main=is_main,
        source=f"../{name}",
        resolved=f"/abs/{name}",
        rev=None,
        head=None,
        status=status,  # type: ignore[arg-type]
        error=None if status == "ok" else "boom",
    )


def make_report(
    findings: tuple[Finding, ...],
    *,
    repos: tuple[str, ...] = ("roiextractors", "spikeinterface"),
    candidates: tuple[Candidate, ...] = (),
    errors: tuple[RunError, ...] = (),
    fail_on: Severity | None = Severity.ERROR,
) -> Report:
    return Report(
        sistent_version="0.1.0",
        generated_at="2026-09-19T12:00:00Z",
        config_path="/home/me/sistent.toml",
        main=make_status("neuroconv", is_main=True),
        repos=tuple(make_status(r) for r in repos),
        aspects=(AspectInfo(name="readme", type="markdown", options={"files": ["README.md"]}, targets=repos),),
        findings=findings,
        candidates=candidates,
        errors=errors,
        fail_on=fail_on,
        exit_code=1,
        exit_reason="1 finding at or above fail_on=error",
    )


# ----- enums -------------------------------------------------------------------------------------------------------


class TestSeverity:
    def test_rank_orders_info_warning_error(self) -> None:
        assert [s.rank for s in Severity] == [0, 1, 2]
        assert Severity.INFO.rank < Severity.WARNING.rank < Severity.ERROR.rank

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("info", Severity.INFO),
            ("INFO", Severity.INFO),
            ("Warning", Severity.WARNING),
            ("error", Severity.ERROR),
            ("ERROR", Severity.ERROR),
        ],
    )
    def test_parse_is_case_insensitive(self, value: str, expected: Severity) -> None:
        assert Severity.parse(value) is expected

    def test_parse_rejects_unknown_and_lists_valid_values(self) -> None:
        with pytest.raises(ValueError, match=r"invalid severity 'fatal' \(valid: info, warning, error\)"):
            Severity.parse("fatal")

    def test_parse_result_is_the_enum_member(self) -> None:
        assert Severity.parse("warning") == "warning"
        assert Severity.parse("warning").rank == 1


class TestStrEnums:
    def test_members_compare_equal_to_their_strings(self) -> None:
        assert Kind.MISSING == "missing"
        assert Subject.FILE == "file"
        assert Direction.NONE == "none"
        assert Severity.ERROR == "error"

    def test_members_format_as_plain_strings(self) -> None:
        assert f"{Kind.MISSING}" == "missing"
        assert str(Direction.UPSTREAM) == "upstream"
        assert isinstance(Subject.RULE, str)

    def test_lookup_by_value(self) -> None:
        assert Kind("moved") is Kind.MOVED
        assert Subject("reference") is Subject.REFERENCE

    def test_value_sets_match_spec(self) -> None:
        assert {k.value for k in Kind} == {"missing", "extra", "differs", "moved", "reordered", "stale", "unparseable"}
        assert {d.value for d in Direction} == {"downstream", "upstream", "none"}
        assert {s.value for s in Subject} == {
            "file",
            "title",
            "section",
            "rule",
            "prose",
            "code",
            "badge",
            "key",
            "value",
            "path",
            "workflow",
            "job",
            "action",
            "hook",
            "name",
            "reference",
        }


# ----- locator -----------------------------------------------------------------------------------------------------


class TestLocator:
    @pytest.mark.parametrize(
        ("path", "segments", "sep", "expected"),
        [
            ("AGENTS.md", (), "#", "AGENTS.md"),
            ("README.md", ("Installation", "From source"), "#", "README.md#Installation > From source"),
            ("AGENTS.md", ("Testing@2",), "#", "AGENTS.md#Testing@2"),
            ("README.md", ("badges",), "#", "README.md#badges"),
            ("pyproject.toml", ("tool.ruff.line-length",), ":", "pyproject.toml:tool.ruff.line-length"),
            (".github/workflows", ("ci.yml",), ":", ".github/workflows:ci.yml"),
            (".github/workflows", ("actions/checkout",), ":", ".github/workflows:actions/checkout"),
            (
                ".pre-commit-config.yaml",
                ("astral-sh/ruff-pre-commit",),
                ":",
                ".pre-commit-config.yaml:astral-sh/ruff-pre-commit",
            ),
            ("src/{{name}}/", (), "#", "src/{{name}}/"),
            (".github/dependabot.yml", (), ":", ".github/dependabot.yml"),
        ],
    )
    def test_forms(self, path: str, segments: tuple[str, ...], sep: str, expected: str) -> None:
        result = locator(path, *segments, sep=sep)
        assert result == expected
        assert LOCATOR_RE.fullmatch(result), result

    def test_docstring_examples(self) -> None:
        assert locator("README.md", "Usage", "Example") == "README.md#Usage > Example"
        assert locator("pyproject.toml", "tool.ruff", sep=":") == "pyproject.toml:tool.ruff"
        assert locator("AGENTS.md") == "AGENTS.md"

    def test_sep_only_applies_to_first_join(self) -> None:
        assert locator("a.toml", "x", "y", sep=":") == "a.toml:x > y"

    @pytest.mark.parametrize(
        "example",
        [
            "AGENTS.md",
            "README.md#Installation > From source",
            "AGENTS.md#Testing@2",
            "README.md#badges",
            "pyproject.toml:tool.ruff.line-length",
            "src/{{name}}/",
            ".github/dependabot.yml",
            ".github/workflows:ci.yml:jobs.test",
            ".github/workflows:actions/checkout",
            ".pre-commit-config.yaml:astral-sh/ruff-pre-commit",
        ],
    )
    def test_spec_examples_match_grammar(self, example: str) -> None:
        assert LOCATOR_RE.fullmatch(example)

    @pytest.mark.parametrize("bad", ["", "#Heading", ":key", "README.md#"])
    def test_grammar_rejects_degenerate_forms(self, bad: str) -> None:
        assert LOCATOR_RE.fullmatch(bad) is None


# ----- Finding -----------------------------------------------------------------------------------------------------


class TestFindingIdentity:
    def test_ids_are_deterministic(self) -> None:
        a, b = make_finding(), make_finding()
        assert a.id == b.id
        assert a.candidate_key == b.candidate_key
        assert re.fullmatch(r"[0-9a-f]{12}", a.id)
        assert re.fullmatch(r"[0-9a-f]{12}", a.candidate_key)
        assert a.id != a.candidate_key

    def test_repo_changes_id_but_not_candidate_key(self) -> None:
        a = make_finding(repo="roiextractors")
        b = make_finding(repo="spikeinterface")
        assert a.id != b.id
        assert a.candidate_key == b.candidate_key

    def test_message_does_not_change_id(self) -> None:
        a = make_finding(message="one wording")
        b = make_finding(message="another wording")
        assert a.id == b.id
        assert a.candidate_key == b.candidate_key

    def test_content_key_changes_id_and_candidate_key(self) -> None:
        a = make_finding(content_key="installation")
        b = make_finding(content_key="install")
        assert a.id != b.id
        assert a.candidate_key != b.candidate_key

    @pytest.mark.parametrize(
        "change",
        [
            {"aspect": "agents"},
            {"kind": Kind.EXTRA},
            {"subject": Subject.RULE},
            {"locator": "README.md#Usage"},
        ],
    )
    def test_identity_fields_change_both_keys(self, change: dict[str, Any]) -> None:
        a = make_finding()
        b = make_finding(**change)
        assert a.id != b.id
        assert a.candidate_key != b.candidate_key

    @pytest.mark.parametrize(
        "change",
        [
            {"severity": Severity.ERROR},
            {"direction": Direction.NONE},
            {"detail": "some detail", "detail_kind": "text"},
            {"option": "aspects.readme.threshold=0.6"},
            {"suppressed": True},
            {"baselined": True},
        ],
    )
    def test_non_identity_fields_do_not_change_keys(self, change: dict[str, Any]) -> None:
        a = make_finding()
        b = make_finding(**change)
        assert a.id == b.id
        assert a.candidate_key == b.candidate_key

    def test_replace_recomputes_ids(self) -> None:
        original = make_finding()
        moved = original.replace(repo="spikeinterface")
        assert moved.repo == "spikeinterface"
        assert moved.id != original.id
        assert moved.candidate_key == original.candidate_key
        rekeyed = original.replace(content_key="other")
        assert rekeyed.id != original.id
        assert rekeyed.candidate_key != original.candidate_key
        flagged = original.replace(suppressed=True, baselined=True)
        assert flagged.id == original.id
        assert flagged.suppressed
        assert flagged.baselined
        # the original is untouched
        assert original.repo == "roiextractors"
        assert not original.suppressed

    def test_is_frozen(self) -> None:
        f = make_finding()
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.message = "changed"  # type: ignore[misc]


class TestFindingFlags:
    @pytest.mark.parametrize(
        ("direction", "suppressed", "baselined", "hidden", "gates"),
        [
            (Direction.DOWNSTREAM, False, False, False, True),
            (Direction.NONE, False, False, False, True),
            (Direction.UPSTREAM, False, False, False, False),
            (Direction.DOWNSTREAM, True, False, True, False),
            (Direction.DOWNSTREAM, False, True, True, False),
            (Direction.NONE, True, True, True, False),
            (Direction.UPSTREAM, True, False, True, False),
        ],
    )
    def test_hidden_and_gates(
        self, direction: Direction, suppressed: bool, baselined: bool, hidden: bool, gates: bool
    ) -> None:
        f = make_finding(direction=direction, suppressed=suppressed, baselined=baselined)
        assert f.hidden is hidden
        assert f.gates is gates

    def test_to_dict_keys_and_values(self) -> None:
        f = make_finding(detail="a\nb", detail_kind="diff", option="aspects.readme.x=1")
        d = f.to_dict()
        assert list(d) == [
            "id",
            "candidate_key",
            "aspect",
            "repo",
            "kind",
            "subject",
            "severity",
            "direction",
            "locator",
            "message",
            "detail",
            "detail_kind",
            "option",
            "content_key",
            "suppressed",
            "baselined",
        ]
        assert d["id"] == f.id
        assert d["candidate_key"] == f.candidate_key
        assert d["kind"] == "missing"
        assert d["subject"] == "section"
        assert d["severity"] == "warning"
        assert d["direction"] == "downstream"
        assert d["detail"] == "a\nb"
        assert d["detail_kind"] == "diff"
        assert d["option"] == "aspects.readme.x=1"
        assert d["suppressed"] is False
        assert d["baselined"] is False
        assert json.loads(json.dumps(d)) == d

    def test_to_dict_defaults_are_none(self) -> None:
        d = make_finding().to_dict()
        assert d["detail"] is None
        assert d["detail_kind"] is None
        assert d["option"] is None


# ----- Report ------------------------------------------------------------------------------------------------------


class TestReport:
    def test_summary_counts(self) -> None:
        findings = (
            make_finding(repo="roiextractors", severity=Severity.ERROR, locator="A"),
            make_finding(repo="roiextractors", severity=Severity.WARNING, locator="B", suppressed=True),
            make_finding(
                repo="roiextractors", kind=Kind.EXTRA, direction=Direction.UPSTREAM, severity=Severity.INFO, locator="C"
            ),
            make_finding(repo="roiextractors", severity=Severity.INFO, locator="D", baselined=True),
            make_finding(
                repo="roiextractors", kind=Kind.STALE, direction=Direction.NONE, severity=Severity.WARNING, locator="E"
            ),
            make_finding(
                repo="spikeinterface",
                kind=Kind.EXTRA,
                direction=Direction.UPSTREAM,
                severity=Severity.INFO,
                locator="C",
            ),
            make_finding(repo="spikeinterface", severity=Severity.ERROR, locator="F", suppressed=True, baselined=True),
        )
        errors = (RunError(stage="extract", repo="spikeinterface", aspect="readme", message="boom"),)
        summary = make_report(findings, errors=errors).summary()

        assert summary["by_repo"] == {
            "neuroconv": {"error": 0, "warning": 0, "info": 0, "candidates": 0, "suppressed": 0, "baselined": 0},
            "roiextractors": {"error": 1, "warning": 1, "info": 0, "candidates": 1, "suppressed": 1, "baselined": 1},
            "spikeinterface": {"error": 0, "warning": 0, "info": 0, "candidates": 1, "suppressed": 1, "baselined": 0},
        }
        assert summary["by_severity"] == {"info": 2, "warning": 1, "error": 1}
        assert summary["by_direction"] == {"downstream": 1, "upstream": 2, "none": 1}
        assert summary["new"] == 4
        assert summary["baselined"] == 1
        assert summary["suppressed"] == 2, "a finding that is both suppressed and baselined counts as suppressed"
        assert summary["errors"] == 1

    def test_summary_hidden_findings_do_not_count_towards_severity(self) -> None:
        findings = (
            make_finding(severity=Severity.ERROR, locator="A", suppressed=True),
            make_finding(severity=Severity.ERROR, locator="B", baselined=True),
        )
        summary = make_report(findings).summary()
        assert summary["by_severity"] == {"info": 0, "warning": 0, "error": 0}
        assert summary["by_direction"] == {"downstream": 0, "upstream": 0, "none": 0}
        assert summary["new"] == 0
        assert summary["by_repo"]["roiextractors"]["error"] == 0

    def test_summary_includes_repos_without_findings_and_unknown_repos(self) -> None:
        summary = make_report((make_finding(repo="stranger"),)).summary()
        assert set(summary["by_repo"]) == {"neuroconv", "roiextractors", "spikeinterface", "stranger"}
        assert summary["by_repo"]["stranger"]["warning"] == 1

    def test_to_dict_top_level_keys(self) -> None:
        findings = (make_finding(), make_finding(locator="X", suppressed=True))
        candidates = aggregate_candidates((make_finding(kind=Kind.EXTRA, direction=Direction.UPSTREAM),), {"readme": 2})
        report = make_report(
            findings, candidates=candidates, errors=(RunError(stage="resolve", repo="x", aspect=None, message="m"),)
        )
        d = report.to_dict()
        assert list(d) == [
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
        assert d["schema_version"] == SCHEMA_VERSION == 1
        assert d["baseline_stale"] == []
        assert d["fail_on"] == "error"
        assert d["main"]["is_main"] is True
        assert d["main"]["identity"] is None
        assert [r["name"] for r in d["repos"]] == ["roiextractors", "spikeinterface"]
        assert d["aspects"] == [
            {
                "name": "readme",
                "type": "markdown",
                "options": {"files": ["README.md"]},
                "targets": ["roiextractors", "spikeinterface"],
            }
        ]
        assert len(d["findings"]) == 2, "hidden findings are serialised too"
        assert d["candidates"][0]["support"] == 1
        assert d["errors"] == [{"stage": "resolve", "repo": "x", "aspect": None, "message": "m", "traceback": None}]
        assert "snapshots" not in d
        json.dumps(d)

    def test_to_dict_fail_on_never(self) -> None:
        assert make_report((), fail_on=None).to_dict()["fail_on"] == "never"

    def test_to_dict_baseline_stale(self) -> None:
        report = dataclasses.replace(make_report(()), baseline_stale=("0123456789ab", "ba9876543210"))
        assert report.to_dict()["baseline_stale"] == ["0123456789ab", "ba9876543210"]

    def test_convenience_views(self) -> None:
        visible = make_finding(repo="roiextractors", locator="A")
        hidden = make_finding(repo="roiextractors", locator="B", suppressed=True)
        other = make_finding(repo="spikeinterface", locator="C")
        report = make_report((visible, hidden, other))
        assert report.visible_findings == (visible, other)
        assert report.findings_for("roiextractors") == (visible,)
        assert report.findings_for("roiextractors", include_hidden=True) == (visible, hidden)
        assert report.unavailable == ()

    def test_unavailable_lists_broken_repos(self) -> None:
        report = dataclasses.replace(make_report(()), repos=(make_status("a"), make_status("b", status="unavailable")))
        assert [r.name for r in report.unavailable] == ["b"]
        assert report.to_dict()["repos"][1]["error"] == "boom"


# ----- aggregate_candidates ----------------------------------------------------------------------------------------


class TestAggregateCandidates:
    def test_groups_by_candidate_key_and_counts_support(self) -> None:
        def upstream(repo: str, loc: str, **kw: Any) -> Finding:
            return make_finding(
                repo=repo,
                kind=Kind.EXTRA,
                subject=Subject.SECTION,
                direction=Direction.UPSTREAM,
                severity=Severity.INFO,
                locator=loc,
                content_key=loc.lower(),
                message=f"section {loc} not in main",
                **kw,
            )

        findings = (
            upstream("a", "X"),
            upstream("b", "X"),
            upstream("a", "X"),  # same repo twice: counted once
            upstream("a", "Y"),
            upstream("c", "Z", suppressed=True),  # hidden: excluded
            upstream("c", "W", baselined=True),  # hidden: excluded
            make_finding(repo="a", locator="V"),  # downstream: excluded
            make_finding(repo="a", locator="U", direction=Direction.NONE),  # none: excluded
        )
        result = aggregate_candidates(findings, {"readme": 3})
        assert [(c.locator, c.repos, c.support, c.total) for c in result] == [
            ("X", ("a", "b"), 2, 3),
            ("Y", ("a",), 1, 3),
        ]
        first = result[0]
        assert isinstance(first, Candidate)
        assert first.aspect == "readme"
        assert first.kind is Kind.EXTRA
        assert first.subject is Subject.SECTION
        assert first.content_key == "x"
        assert first.candidate_key == findings[0].candidate_key
        assert first.message == "section X not in main"
        assert first.to_dict() == {
            "candidate_key": findings[0].candidate_key,
            "aspect": "readme",
            "kind": "extra",
            "subject": "section",
            "locator": "X",
            "content_key": "x",
            "message": "section X not in main",
            "repos": ["a", "b"],
            "support": 2,
            "total": 3,
        }

    def test_sorted_by_support_then_aspect_then_locator(self) -> None:
        def upstream(repo: str, aspect: str, loc: str) -> Finding:
            return make_finding(repo=repo, aspect=aspect, kind=Kind.EXTRA, direction=Direction.UPSTREAM, locator=loc)

        findings = (
            upstream("a", "zeta", "B"),
            upstream("a", "alpha", "B"),
            upstream("a", "alpha", "A"),
            upstream("a", "zeta", "A"),
            upstream("b", "zeta", "A"),
        )
        result = aggregate_candidates(findings, {})
        assert [(c.aspect, c.locator, c.support) for c in result] == [
            ("zeta", "A", 2),
            ("alpha", "A", 1),
            ("alpha", "B", 1),
            ("zeta", "B", 1),
        ]

    def test_total_defaults_to_support_when_aspect_untargeted(self) -> None:
        findings = (
            make_finding(repo="a", kind=Kind.EXTRA, direction=Direction.UPSTREAM),
            make_finding(repo="b", kind=Kind.EXTRA, direction=Direction.UPSTREAM),
        )
        (candidate,) = aggregate_candidates(findings, {"other_aspect": 9})
        assert candidate.total == candidate.support == 2

    def test_empty_input(self) -> None:
        assert aggregate_candidates((), {"readme": 3}) == ()
        assert aggregate_candidates((make_finding(),), {"readme": 3}) == ()
