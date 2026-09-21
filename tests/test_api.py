"""The pipeline in :mod:`sistent.api`, exercised with the tiny aspect types from :mod:`tests.helpers`."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from sistent import api
from sistent.baseline import read_baseline
from sistent.config import Config, load_config
from sistent.model import Direction, Kind, Report, Severity, Subject
from tests.conftest import write_files

FLEET = {
    "main/RULES.txt": "alpha\nbeta\ngamma\n",
    "sat1/RULES.txt": "alpha\nbeta\ndelta\n",
    "sat2/RULES.txt": "alpha\ngamma\ndelta\n",
}

CONFIG = """
[sistent]
main = "main"
defaults = false
fail_on = "warning"
stale = true

[repos.main]
path = "main"

[repos.sat1]
path = "sat1"
tags = ["python"]

[repos.sat2]
path = "sat2"
tags = ["docs"]

[aspects.rules]
type = "tests.helpers:LinesAspect"
"""


@pytest.fixture
def fleet(tmp_path: Path, make_config: Callable[..., Path]) -> Callable[..., Config]:
    """Write the three-repo fleet plus a config (optionally amended) and load it."""

    def _make(extra_config: str = "", files: dict[str, str] | None = None) -> Config:
        write_files(tmp_path, {**FLEET, **(files or {})})
        return load_config(make_config(CONFIG + extra_config))

    return _make


def keys(report: Report, repo: str | None = None) -> set[tuple[str, str, str, str]]:
    return {
        (f.repo, f.kind.value, f.direction.value, f.locator)
        for f in report.findings
        if (repo is None or f.repo == repo) and not f.hidden
    }


# ----- the happy path ----------------------------------------------------------------------------------------------


def test_run_reports_drift_and_candidates(fleet: Callable[..., Config]) -> None:
    report = api.run(fleet(), options=api.RunOptions(fetch=False))

    assert report.main.name == "main"
    assert report.main.status == "ok"
    assert [r.name for r in report.repos] == ["sat1", "sat2"]
    assert keys(report, "sat1") == {
        ("sat1", "missing", "downstream", "RULES.txt#gamma"),
        ("sat1", "extra", "upstream", "RULES.txt#delta"),
    }
    assert keys(report, "sat2") == {
        ("sat2", "missing", "downstream", "RULES.txt#beta"),
        ("sat2", "extra", "upstream", "RULES.txt#delta"),
    }
    # upstream findings are info and aggregated across satellites
    extras = [f for f in report.findings if f.kind is Kind.EXTRA]
    assert {f.severity for f in extras} == {Severity.INFO}
    assert [(c.locator, c.support, c.total, c.repos) for c in report.candidates] == [
        ("RULES.txt#delta", 2, 2, ("sat1", "sat2"))
    ]
    # two warnings at fail_on = warning -> exit 1
    assert report.exit_code == 1
    assert "fail_on = warning" in report.exit_reason
    assert report.aspects[0].name == "rules"
    assert report.aspects[0].type == "lines"
    assert report.aspects[0].targets == ("sat1", "sat2")
    assert ("sat1", "rules") in report.snapshots
    json.dumps(report.to_dict())  # serialisable


def test_fail_on_controls_exit_code(fleet: Callable[..., Config]) -> None:
    config = fleet()
    assert api.run(config, options=api.RunOptions(fetch=False, fail_on="never")).exit_code == 0
    assert api.run(config, options=api.RunOptions(fetch=False, fail_on="error")).exit_code == 0
    assert api.run(config, options=api.RunOptions(fetch=False, fail_on="info")).exit_code == 1
    assert api.run(config, options=api.RunOptions(fetch=False, fail_on="warning")).exit_code == 1


def test_upstream_findings_never_gate(fleet: Callable[..., Config]) -> None:
    # main has strictly fewer rules than the satellite: only upstream findings
    config = fleet(files={"main/RULES.txt": "alpha\n", "sat1/RULES.txt": "alpha\nbeta\n", "sat2/RULES.txt": "alpha\n"})
    report = api.run(config, options=api.RunOptions(fetch=False, fail_on="info"))
    assert {f.direction for f in report.findings} == {Direction.UPSTREAM}
    assert report.exit_code == 0
    assert report.exit_reason.startswith("clean")


# ----- selection ---------------------------------------------------------------------------------------------------


def test_repo_and_tag_filters(fleet: Callable[..., Config]) -> None:
    config = fleet()
    only = api.run(config, options=api.RunOptions(fetch=False, repos=("sat1",)))
    assert [r.name for r in only.repos] == ["sat1"]
    tagged = api.run(config, options=api.RunOptions(fetch=False, tags=("docs",)))
    assert [r.name for r in tagged.repos] == ["sat2"]
    globbed = api.run(config, options=api.RunOptions(fetch=False, repos=("SAT*",)))
    assert [r.name for r in globbed.repos] == ["sat1", "sat2"]
    with pytest.raises(api.SelectionError, match="--repo 'nope'"):
        api.run(config, options=api.RunOptions(fetch=False, repos=("nope",)))
    with pytest.raises(api.SelectionError, match="--aspect 'nope'"):
        api.run(config, options=api.RunOptions(fetch=False, aspects=("nope",)))


def test_aspect_tags_and_skip_aspects(fleet: Callable[..., Config]) -> None:
    # the aspect is tagged "python": sat2 (tagged docs) is not a target
    config = fleet('tags = ["python"]\n')
    report = api.run(config, options=api.RunOptions(fetch=False))
    assert report.aspects[0].targets == ("sat1",)
    assert keys(report, "sat2") == set()

    # [repos.sat1].skip_aspects removes the aspect for that repo only
    config = fleet()
    config.path.write_text(CONFIG.replace('tags = ["python"]', 'tags = ["python"]\nskip_aspects = ["rules"]'))
    report = api.run(load_config(config.path), options=api.RunOptions(fetch=False))
    assert report.aspects[0].targets == ("sat2",)
    assert keys(report, "sat1") == set()


# ----- suppression and baseline ------------------------------------------------------------------------------------


def test_ignore_globs_suppress_findings(fleet: Callable[..., Config]) -> None:
    text = CONFIG.replace('tags = ["python"]', 'tags = ["python"]\nignore = ["rules:RULES.txt#gam*"]').replace(
        'type = "tests.helpers:LinesAspect"', 'type = "tests.helpers:LinesAspect"\nignore = ["RULES.txt#beta"]'
    )
    config = fleet()
    config.path.write_text(text)
    report = api.run(load_config(config.path), options=api.RunOptions(fetch=False))
    suppressed = {(f.repo, f.locator) for f in report.findings if f.suppressed}
    assert suppressed == {("sat1", "RULES.txt#gamma"), ("sat2", "RULES.txt#beta")}
    assert keys(report) == {
        ("sat1", "extra", "upstream", "RULES.txt#delta"),
        ("sat2", "extra", "upstream", "RULES.txt#delta"),
    }
    assert report.exit_code == 0
    assert report.summary()["suppressed"] == 2


def test_baseline_round_trip(fleet: Callable[..., Config], tmp_path: Path) -> None:
    config = fleet()
    baseline = tmp_path / "baseline.json"
    first = api.run(config, options=api.RunOptions(fetch=False, baseline=baseline, update_baseline=True))
    assert baseline.exists()
    assert all(f.baselined for f in first.findings)
    assert first.exit_code == 0
    stored = read_baseline(baseline)
    assert len(stored.entries) == 4

    # a new rule appears in main -> one new (non-baselined) finding per satellite, the rest stays hidden
    (tmp_path / "main" / "RULES.txt").write_text("alpha\nbeta\ngamma\nepsilon\n")
    second = api.run(config, options=api.RunOptions(fetch=False, baseline=baseline))
    fresh = [f for f in second.findings if not f.baselined]
    assert {(f.repo, f.locator) for f in fresh} == {("sat1", "RULES.txt#epsilon"), ("sat2", "RULES.txt#epsilon")}
    assert second.exit_code == 1
    assert second.baseline_stale == ()

    # a baselined finding that disappears is reported as stale
    (tmp_path / "sat1" / "RULES.txt").write_text("alpha\nbeta\ngamma\nepsilon\ndelta\n")
    third = api.run(config, options=api.RunOptions(fetch=False, baseline=baseline))
    assert len(third.baseline_stale) == 1


# ----- availability and errors -------------------------------------------------------------------------------------


def test_unavailable_satellite_is_reported_and_exit_3(fleet: Callable[..., Config], tmp_path: Path) -> None:
    config = fleet()
    import shutil

    shutil.rmtree(tmp_path / "sat2")
    report = api.run(config, options=api.RunOptions(fetch=False, fail_on="never"))
    sat2 = next(r for r in report.repos if r.name == "sat2")
    assert sat2.status == "unavailable"
    assert sat2.error
    assert report.exit_code == 3
    assert keys(report, "sat1")  # others still checked
    allowed = api.run(config, options=api.RunOptions(fetch=False, fail_on="never", allow_unavailable=True))
    assert allowed.exit_code == 0


def test_main_unavailable_is_fatal(fleet: Callable[..., Config], tmp_path: Path) -> None:
    config = fleet()
    import shutil

    shutil.rmtree(tmp_path / "main")
    with pytest.raises(api.MainUnavailable):
        api.run(config, options=api.RunOptions(fetch=False))


def test_run_errors_fail_even_with_fail_on_never(fleet: Callable[..., Config]) -> None:
    config = fleet('\n[aspects.boom]\ntype = "tests.helpers:BoomAspect"\n')
    report = api.run(config, options=api.RunOptions(fetch=False, fail_on="never"))
    assert [e.aspect for e in report.errors] == ["boom"]
    assert report.errors[0].stage == "extract"
    assert report.errors[0].repo == "main"
    assert report.errors[0].traceback is not None
    assert "RuntimeError: boom" in report.errors[0].traceback
    assert report.exit_code == 1
    # the healthy aspect still ran
    assert keys(report, "sat1")


def test_unparseable_satellite_becomes_a_finding_but_main_disables_the_aspect(fleet: Callable[..., Config]) -> None:
    config = fleet('\n[aspects.parse]\ntype = "tests.helpers:UnparseableAspect"\n', files={"sat1/BROKEN": "x"})
    report = api.run(config, options=api.RunOptions(fetch=False, fail_on="never"))
    unparseable = [f for f in report.findings if f.kind is Kind.UNPARSEABLE]
    assert [(f.repo, f.aspect, f.subject, f.locator) for f in unparseable] == [
        ("sat1", "parse", Subject.FILE, "BROKEN")
    ]
    assert not report.errors

    config = fleet('\n[aspects.parse]\ntype = "tests.helpers:UnparseableAspect"\n', files={"main/BROKEN": "x"})
    report = api.run(config, options=api.RunOptions(fetch=False, fail_on="never"))
    assert [(e.repo, e.aspect) for e in report.errors] == [("main", "parse")]
    assert not [f for f in report.findings if f.aspect == "parse"]


def test_stale_references_to_other_repos(fleet: Callable[..., Config]) -> None:
    config = fleet(files={"sat1/RULES.txt": "alpha\nbeta\ngamma\nsee sat2 for details\n"})
    report = api.run(config, options=api.RunOptions(fetch=False, fail_on="never"))
    stale = [f for f in report.findings if f.kind is Kind.STALE]
    assert [(f.repo, f.direction, f.locator, f.content_key) for f in stale] == [
        ("sat1", Direction.NONE, "RULES.txt", "sat2")
    ]
    assert "sat2" in stale[0].message
    off = fleet(files={"sat1/RULES.txt": "alpha\nbeta\ngamma\nsee sat2 for details\n"})
    off.path.write_text(CONFIG.replace("stale = true", "stale = false"))
    report = api.run(load_config(off.path), options=api.RunOptions(fetch=False, fail_on="never"))
    assert not [f for f in report.findings if f.kind is Kind.STALE]


# ----- helpers for the other commands ------------------------------------------------------------------------------


def test_extract_snapshots_and_repo_statuses(fleet: Callable[..., Config]) -> None:
    config = fleet()
    snapshots, errors = api.extract_snapshots(config, repos=["main", "sat1"], fetch=False)
    assert not errors
    assert set(snapshots) == {("main", "rules"), ("sat1", "rules")}
    assert snapshots[("sat1", "rules")].data["lines"] == ["alpha", "beta", "delta"]
    assert snapshots[("sat1", "rules")].sources == ("RULES.txt",)
    with pytest.raises(api.SelectionError):
        api.extract_snapshots(config, repos=["nope"], fetch=False)
    statuses = api.repo_statuses(config, fetch=False)
    assert [(s.name, s.is_main, s.status) for s in statuses] == [
        ("main", True, "ok"),
        ("sat1", False, "ok"),
        ("sat2", False, "ok"),
    ]
    assert statuses[0].identity is not None
    assert "main" in statuses[0].identity.aliases


def test_compute_exit_reasons() -> None:
    code, reason = api.compute_exit([], [], fail_on=Severity.ERROR, unavailable=0, allow_unavailable=False)
    assert (code, reason) == (0, "clean → exit 0")
    code, reason = api.compute_exit([], [], fail_on=None, unavailable=2, allow_unavailable=False)
    assert code == 3
    assert "2 satellites unavailable" in reason
    code, reason = api.compute_exit([], [], fail_on=None, unavailable=1, allow_unavailable=True)
    assert code == 0
    assert "(allowed)" in reason
