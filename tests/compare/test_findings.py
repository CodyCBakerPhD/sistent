"""Tests for :mod:`sistent.compare.findings`."""

from __future__ import annotations

import datetime
import re
from typing import Any

import pytest

from sistent.aspects.base import Aspect
from sistent.compare.findings import VALUE_WIDTH, findings_from_keydiffs, findings_from_setdiff, format_value
from sistent.compare.mappings import MISSING, KeyDiff, diff_mapping
from sistent.compare.sets import diff_sets
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject, locator
from sistent.options import BaseOptions

LOCATOR_RE = re.compile(r"^[^#:]+([#:].+)?$")


class DummyAspect(Aspect):
    type_name = "dummy"
    description = "test double"

    def extract(self, ctx: Any) -> dict[str, Any]:
        return {}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        return []


def make_aspect(name: str = "dummy", **options: Any) -> DummyAspect:
    return DummyAspect(name, BaseOptions(**options))


def badge_locator(_: str) -> str:
    return locator("README.md", "badges")


def shape(finding: Finding) -> tuple[Kind, Subject, Direction, Severity, str]:
    return finding.kind, finding.subject, finding.direction, finding.severity, finding.locator


# ----- findings_from_setdiff ---------------------------------------------------------------------------------------


def test_setdiff_kinds_directions_and_messages() -> None:
    diff = diff_sets(["pypi-version", "docs"], ["docs", "coverage"])
    found = findings_from_setdiff(
        diff,
        aspect=make_aspect("readme_badges"),
        repo="roiextractors",
        subject=Subject.BADGE,
        locator=badge_locator,
        describe=str.upper,
        content_key=str,
    )
    assert [shape(f) for f in found] == [
        (Kind.MISSING, Subject.BADGE, Direction.DOWNSTREAM, Severity.WARNING, "README.md#badges"),
        (Kind.EXTRA, Subject.BADGE, Direction.UPSTREAM, Severity.INFO, "README.md#badges"),
    ]
    missing, extra = found
    assert missing.message == "badge missing: PYPI-VERSION"
    assert missing.content_key == "pypi-version"
    assert extra.message == "badge only in repo: COVERAGE"
    assert extra.content_key == "coverage"
    assert all(f.aspect == "readme_badges" and f.repo == "roiextractors" for f in found)
    assert all(f.detail is None and f.detail_kind is None for f in found)
    assert all(LOCATOR_RE.match(f.locator) for f in found)


def test_setdiff_order_is_missing_then_extra_in_input_order() -> None:
    diff = diff_sets(["c", "a", "b"], ["b", "z", "y"])
    found = findings_from_setdiff(
        diff,
        aspect=make_aspect(),
        repo="r",
        subject=Subject.PATH,
        locator=lambda p: f"{p}/",
        describe=str,
        content_key=str,
    )
    assert [(f.kind, f.content_key) for f in found] == [
        (Kind.MISSING, "c"),
        (Kind.MISSING, "a"),
        (Kind.EXTRA, "z"),
        (Kind.EXTRA, "y"),
    ]
    assert [f.locator for f in found] == ["c/", "a/", "z/", "y/"]


def test_setdiff_empty() -> None:
    diff = diff_sets(["a"], ["a"])
    assert (
        findings_from_setdiff(
            diff, aspect=make_aspect(), repo="r", subject=Subject.PATH, locator=str, describe=str, content_key=str
        )
        == []
    )


def test_setdiff_uses_callables_on_original_items() -> None:
    main = [{"kind": "ci:ci.yml", "img": "https://x/ci.yml/badge.svg"}]
    diff = diff_sets(main, [], key=lambda b: b["kind"])
    (finding,) = findings_from_setdiff(
        diff,
        aspect=make_aspect(),
        repo="r",
        subject=Subject.BADGE,
        locator=lambda b: locator("README.md", "badges"),
        describe=lambda b: f"{b['kind']} ({b['img']})",
        content_key=lambda b: b["kind"],
    )
    assert finding.message == "badge missing: ci:ci.yml (https://x/ci.yml/badge.svg)"
    assert finding.content_key == "ci:ci.yml"


def test_setdiff_missing_severity_is_the_aspect_default() -> None:
    diff = diff_sets(["a"], ["b"])
    missing, extra = findings_from_setdiff(
        diff,
        aspect=make_aspect(),
        repo="r",
        subject=Subject.VALUE,
        locator=str,
        describe=str,
        content_key=str,
        missing_severity=Severity.ERROR,
    )
    assert missing.severity is Severity.ERROR
    assert extra.severity is Severity.INFO


def test_setdiff_user_severity_table_wins_over_missing_severity() -> None:
    diff = diff_sets(["a"], ["b"])
    aspect = make_aspect(severity={"missing.value": "info", "extra": "error"})
    missing, extra = findings_from_setdiff(
        diff,
        aspect=aspect,
        repo="r",
        subject=Subject.VALUE,
        locator=str,
        describe=str,
        content_key=str,
        missing_severity=Severity.ERROR,
    )
    assert missing.severity is Severity.INFO
    assert extra.severity is Severity.INFO, "upstream findings are pinned at info; the severity table cannot raise them"
    assert extra.direction is Direction.UPSTREAM
    assert not extra.gates


def test_setdiff_missing_file_defaults_to_error() -> None:
    diff = diff_sets(["CODE_OF_CONDUCT.md"], [])
    (finding,) = findings_from_setdiff(
        diff, aspect=make_aspect(), repo="r", subject=Subject.FILE, locator=str, describe=str, content_key=str
    )
    assert finding.severity is Severity.ERROR
    assert finding.message == "file missing: CODE_OF_CONDUCT.md"


def test_setdiff_ids_depend_on_repo_but_candidate_keys_do_not() -> None:
    diff = diff_sets([], ["funding"])
    kwargs: dict[str, Any] = {
        "subject": Subject.SECTION,
        "locator": lambda s: f"README.md#{s}",
        "describe": str,
        "content_key": str,
    }
    (a,) = findings_from_setdiff(diff, aspect=make_aspect("readme"), repo="repo-a", **kwargs)
    (b,) = findings_from_setdiff(diff, aspect=make_aspect("readme"), repo="repo-b", **kwargs)
    assert a.id != b.id
    assert a.candidate_key == b.candidate_key


# ----- findings_from_keydiffs --------------------------------------------------------------------------------------

MAIN_TOML = {"tool": {"ruff": {"line-length": 120, "select": ["E"]}}}
OTHER_TOML = {"tool": {"ruff": {"line-length": 100, "target": "py311"}}}


def test_keydiffs_kinds_locators_and_messages() -> None:
    diffs = diff_mapping(MAIN_TOML, OTHER_TOML)
    found = findings_from_keydiffs(
        diffs, aspect=make_aspect("pyproject"), repo="r", file="pyproject.toml", authoritative=True
    )
    assert [shape(f) for f in found] == [
        (Kind.DIFFERS, Subject.VALUE, Direction.DOWNSTREAM, Severity.WARNING, "pyproject.toml:tool.ruff.line-length"),
        (Kind.MISSING, Subject.KEY, Direction.DOWNSTREAM, Severity.WARNING, "pyproject.toml:tool.ruff.select"),
        (Kind.EXTRA, Subject.KEY, Direction.UPSTREAM, Severity.INFO, "pyproject.toml:tool.ruff.target"),
    ]
    differs, missing, extra = found
    assert differs.message == "main: 120 ; repo: 100"
    assert differs.detail is None
    assert differs.detail_kind is None
    assert missing.message == "key missing: tool.ruff.select"
    assert extra.message == "key only in repo: tool.ruff.target"
    assert [f.content_key for f in found] == ["tool.ruff.line-length", "tool.ruff.select", "tool.ruff.target"]
    assert all(f.aspect == "pyproject" and f.repo == "r" for f in found)
    assert all(LOCATOR_RE.match(f.locator) for f in found)


@pytest.mark.parametrize(
    ("authoritative", "direction"),
    [(True, Direction.DOWNSTREAM), (False, Direction.NONE)],
)
def test_keydiffs_authoritative_flag_sets_differs_direction(authoritative: bool, direction: Direction) -> None:
    diffs = [KeyDiff(("a",), "differs", 1, 2)]
    (finding,) = findings_from_keydiffs(
        diffs, aspect=make_aspect(), repo="r", file="f.toml", authoritative=authoritative
    )
    assert finding.direction is direction
    assert finding.severity is Severity.WARNING
    assert finding.gates


def test_keydiffs_authoritative_flag_does_not_affect_missing_and_extra() -> None:
    diffs = [KeyDiff(("a",), "missing", 1, MISSING), KeyDiff(("b",), "extra", MISSING, 2)]
    for authoritative in (True, False):
        missing, extra = findings_from_keydiffs(
            diffs, aspect=make_aspect(), repo="r", file="f.toml", authoritative=authoritative
        )
        assert missing.direction is Direction.DOWNSTREAM
        assert extra.direction is Direction.UPSTREAM
        assert extra.severity is Severity.INFO


def test_keydiffs_empty() -> None:
    assert findings_from_keydiffs([], aspect=make_aspect(), repo="r", file="f.toml", authoritative=True) == []


def test_keydiffs_root_leaf_locator_is_the_file() -> None:
    diffs = diff_mapping({"a": 1}, {"a": 2}, max_depth=0)
    (finding,) = findings_from_keydiffs(diffs, aspect=make_aspect(), repo="r", file="f.toml", authoritative=True)
    assert finding.locator == "f.toml"
    assert finding.content_key == ""
    assert finding.message == 'main: {"a":1} ; repo: {"a":2}'


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        pytest.param(120, "120", id="int"),
        pytest.param("py311", '"py311"', id="str-is-quoted"),
        pytest.param(True, "true", id="bool"),
        pytest.param(None, "null", id="none"),
        pytest.param([1, "a"], '[1,"a"]', id="list-compact"),
        pytest.param({"b": 1, "a": 2}, '{"a":2,"b":1}', id="dict-sorted-compact"),
        pytest.param(datetime.date(2024, 1, 2), '"2024-01-02"', id="non-json-via-str"),
        pytest.param("é", '"é"', id="unicode-kept"),
    ],
)
def test_format_value(value: Any, rendered: str) -> None:
    assert format_value(value) == rendered


def test_keydiffs_short_values_have_no_detail() -> None:
    diffs = [KeyDiff(("k",), "differs", "x" * (VALUE_WIDTH - 2), "y")]  # rendered with quotes: exactly VALUE_WIDTH
    (finding,) = findings_from_keydiffs(diffs, aspect=make_aspect(), repo="r", file="f.toml", authoritative=True)
    assert finding.detail is None
    assert finding.message == f'main: "{"x" * (VALUE_WIDTH - 2)}" ; repo: "y"'


def test_keydiffs_long_values_are_truncated_with_full_values_in_detail() -> None:
    main_value = [f"a-very-long-classifier-string-number-{i:02d}" for i in range(4)]
    other_value = "short"
    diffs = [KeyDiff(("project", "classifiers"), "differs", main_value, other_value)]
    (finding,) = findings_from_keydiffs(
        diffs, aspect=make_aspect(), repo="r", file="pyproject.toml", authoritative=True
    )
    main_text, other_text = format_value(main_value), format_value(other_value)
    assert len(main_text) > VALUE_WIDTH
    assert finding.message.startswith("main: ")
    assert " ; repo: " in finding.message
    shown_main, shown_other = finding.message[len("main: ") :].split(" ; repo: ")
    assert shown_main.endswith("…")
    assert len(shown_main) == VALUE_WIDTH
    assert main_text.startswith(shown_main[:-1])
    assert shown_other == other_text, "the short side is shown in full"
    assert finding.detail == f"main: {main_text}\nrepo: {other_text}"
    assert finding.detail_kind == "text"


def test_keydiffs_detail_when_only_repo_side_is_long() -> None:
    diffs = [KeyDiff(("k",), "differs", 1, "z" * 100)]
    (finding,) = findings_from_keydiffs(diffs, aspect=make_aspect(), repo="r", file="f.toml", authoritative=False)
    assert finding.detail == f"main: 1\nrepo: {format_value('z' * 100)}"
    assert finding.detail_kind == "text"
    assert finding.message.endswith("…")


def test_keydiffs_severity_override() -> None:
    aspect = make_aspect(severity={"differs.value": "error", "missing": "info"})
    diffs = [KeyDiff(("a",), "differs", 1, 2), KeyDiff(("b",), "missing", 1, MISSING)]
    differs, missing = findings_from_keydiffs(diffs, aspect=aspect, repo="r", file="f.toml", authoritative=True)
    assert differs.severity is Severity.ERROR
    assert missing.severity is Severity.INFO


def test_keydiffs_accepts_any_iterable() -> None:
    diffs = (d for d in [KeyDiff(("a",), "missing", 1, MISSING)])
    found = findings_from_keydiffs(diffs, aspect=make_aspect(), repo="r", file="f.toml", authoritative=True)
    assert len(found) == 1
