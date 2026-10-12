"""Baseline file: write/read round trip, applying it to findings, and malformed files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sistent.baseline import (
    BASELINE_SCHEMA_VERSION,
    Baseline,
    BaselineEntry,
    BaselineError,
    apply_baseline,
    describe,
    read_baseline,
    write_baseline,
)
from sistent.model import Direction, Finding, Kind, Severity, Subject


def finding(
    repo: str, locator: str, *, suppressed: bool = False, baselined: bool = False, aspect: str = "readme"
) -> Finding:
    return Finding(
        aspect=aspect,
        repo=repo,
        kind=Kind.MISSING,
        subject=Subject.SECTION,
        severity=Severity.WARNING,
        direction=Direction.DOWNSTREAM,
        locator=locator,
        message=f"section missing: {locator}",
        suppressed=suppressed,
        baselined=baselined,
    )


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    findings = [finding("b", "README.md#Zeta"), finding("a", "README.md#Alpha"), finding("c", "README.md#Mid")]
    path = tmp_path / "nested" / ".sistent-baseline.json"
    written = write_baseline(path, findings, generated_at="2026-09-19T12:00:00Z")
    assert path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()

    document = json.loads(path.read_text(encoding="utf-8"))
    assert list(document) == ["schema_version", "generated_at", "findings"]
    assert document["schema_version"] == BASELINE_SCHEMA_VERSION == 1
    assert document["generated_at"] == "2026-09-19T12:00:00Z"
    assert list(document["findings"]) == sorted(f.id for f in findings)
    assert document["findings"][findings[1].id] == {
        "repo": "a",
        "aspect": "readme",
        "about": "a missing/section README.md#Alpha",
    }
    assert describe(findings[1]) == "a missing/section README.md#Alpha"
    assert written.describe(findings[1].id) == "a missing/section README.md#Alpha"

    read = read_baseline(path)
    assert read == written
    assert read.generated_at == "2026-09-19T12:00:00Z"
    assert set(read.entries) == {f.id for f in findings}
    assert len(read) == 3
    assert findings[0].id in read
    assert list(read) == sorted(f.id for f in findings)


def test_suppressed_findings_are_not_written(tmp_path: Path) -> None:
    kept = finding("a", "README.md#Kept")
    already = finding("a", "README.md#Already", baselined=True)
    dropped = finding("a", "README.md#Dropped", suppressed=True)
    baseline = write_baseline(tmp_path / "b.json", [kept, dropped, already], generated_at="now")
    assert set(baseline.entries) == {kept.id, already.id}
    assert dropped.id not in baseline


def test_write_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    write_baseline(path, [finding("a", "README.md#One")], generated_at="t1")
    write_baseline(path, [finding("a", "README.md#Two")], generated_at="t2")
    read = read_baseline(path)
    assert read.generated_at == "t2"
    assert [e.about for e in read.entries.values()] == ["a missing/section README.md#Two"]


def test_partial_run_keeps_entries_outside_its_scope(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    full = write_baseline(
        path,
        [finding("a", "README.md#One"), finding("b", "README.md#Two"), finding("a", "AGENTS.md#X", aspect="agents")],
        generated_at="t1",
    )
    # a run restricted to repo "a" and aspect "readme" rewrites only that scope
    partial = write_baseline(
        path, [finding("a", "README.md#Changed")], generated_at="t2", keep=full, covered={("a", "readme")}
    )
    kept = {e.about for e in partial.entries.values()}
    assert kept == {
        "a missing/section README.md#Changed",
        "b missing/section README.md#Two",
        "a missing/section AGENTS.md#X",
    }
    assert read_baseline(path) == partial
    # a complete run replaces everything
    complete = write_baseline(path, [finding("a", "README.md#Only")], generated_at="t3", keep=partial, covered=None)
    assert [e.about for e in complete.entries.values()] == ["a missing/section README.md#Only"]


def test_apply_marks_baselined_and_reports_stale() -> None:
    known = finding("a", "README.md#Known")
    fresh = finding("a", "README.md#Fresh")
    gone = finding("a", "README.md#Gone")
    baseline = Baseline(
        generated_at="t",
        entries={
            known.id: BaselineEntry(describe(known), "a", "readme"),
            gone.id: BaselineEntry(describe(gone), "a", "readme"),
        },
    )

    applied, stale = apply_baseline([known, fresh], baseline)
    assert [f.locator for f in applied] == ["README.md#Known", "README.md#Fresh"]
    assert applied[0].baselined is True
    assert applied[0].id == known.id
    assert applied[0].hidden
    assert not applied[0].gates
    assert applied[1].baselined is False
    assert applied[1] is fresh
    assert stale == [gone.id]


def test_stale_detection_is_scoped_to_what_the_run_covered() -> None:
    gone_a = finding("a", "README.md#GoneA")
    gone_b = finding("b", "README.md#GoneB")
    legacy = finding("c", "README.md#Legacy")
    baseline = Baseline(
        generated_at="t",
        entries={
            gone_a.id: BaselineEntry(describe(gone_a), "a", "readme"),
            gone_b.id: BaselineEntry(describe(gone_b), "b", "readme"),
            legacy.id: BaselineEntry(describe(legacy)),  # unscoped entry from an older file
        },
    )
    _, stale = apply_baseline([], baseline, covered={("a", "readme")})
    assert stale == [gone_a.id]
    _, stale = apply_baseline([], baseline, covered=None)
    assert stale == [gone_a.id, gone_b.id, legacy.id]


def test_apply_without_baseline_passes_through() -> None:
    findings = [finding("a", "README.md#X"), finding("b", "README.md#Y", suppressed=True)]
    applied, stale = apply_baseline(iter(findings), None)
    assert applied == findings
    assert stale == []


def test_apply_keeps_suppressed_flag_and_is_idempotent() -> None:
    suppressed = finding("a", "README.md#S", suppressed=True)
    baseline = Baseline(generated_at="t", entries={suppressed.id: BaselineEntry("x")})
    applied, stale = apply_baseline([suppressed], baseline)
    assert applied[0].suppressed
    assert applied[0].baselined
    again, _ = apply_baseline(applied, baseline)
    assert again == applied
    assert stale == []


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(BaselineError, match="not found"):
        read_baseline(tmp_path / "absent.json")


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "JSON object"),
        ('{"schema_version": 2, "generated_at": "t", "findings": {}}', "schema_version 2"),
        ('{"generated_at": "t", "findings": {}}', "schema_version None"),
        ('{"schema_version": 1, "generated_at": "t", "findings": []}', "'findings' must be an object"),
        ('{"schema_version": 1, "generated_at": "t", "findings": {"abc": 1}}', "must be a string or an object"),
        (
            '{"schema_version": 1, "generated_at": "t", "findings": {"abc": {"repo": "a"}}}',
            "must be a string or an object",
        ),
        ('{"schema_version": 1, "generated_at": 5, "findings": {}}', "'generated_at' must be a string"),
        ('{"schema_version": 1, "generated_at": "t"}', "'findings' must be an object"),
    ],
)
def test_bad_files(tmp_path: Path, content: str, match: str) -> None:
    path = tmp_path / "b.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(BaselineError, match=match):
        read_baseline(path)


def test_legacy_string_entries_are_read_as_unscoped(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text('{"schema_version": 1, "generated_at": "t", "findings": {"abc": "a missing/section X"}}')
    baseline = read_baseline(path)
    assert baseline.entries == {"abc": BaselineEntry("a missing/section X")}


def test_empty_baseline_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    path.write_text('{"schema_version": 1, "generated_at": "t", "findings": {}}', encoding="utf-8")
    baseline = read_baseline(path)
    assert baseline.entries == {}
    assert len(baseline) == 0
    applied, stale = apply_baseline([finding("a", "README.md#X")], baseline)
    assert not applied[0].baselined
    assert stale == []
