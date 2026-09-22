"""sistent checked with its own ``sistent.toml``: the repository is main, ``tests/fixtures/example-repo`` a satellite."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sistent import api
from sistent.config import load_config
from sistent.model import Direction, Kind

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "sistent.toml"

pytest.importorskip("sistent.aspects.markdown")


@pytest.fixture(scope="module")
def report() -> Any:
    return api.run(load_config(CONFIG), options=api.RunOptions(fetch=False))


def test_dogfood_runs_cleanly(report: Any) -> None:
    assert report.errors == ()
    assert report.main.name == "sistent"
    assert report.main.status == "ok"
    assert [r.name for r in report.repos] == ["example"]
    assert report.repos[0].status == "ok"
    assert report.exit_code == 0  # fail_on = never in sistent.toml
    assert {a.name for a in report.aspects} >= {"agent_instructions", "readme", "readme_badges", "layout", "pyproject"}


def test_dogfood_skills_are_ignored(report: Any) -> None:
    assert not [f for f in report.findings if "skill" in f.locator.lower() or "skill" in f.content_key.lower()]
    snap = report.snapshots[("example", "agent_instructions")]
    assert any("skills" in item.lower() for item in snap.ignored)


def test_dogfood_finds_expected_drift_and_candidates(report: Any) -> None:
    locators = {(f.aspect, f.kind, f.direction, f.locator) for f in report.findings if not f.hidden}
    # the fixture's extra "Release process" section is a candidate for main
    assert any(
        aspect == "agent_instructions" and kind is Kind.EXTRA and direction is Direction.UPSTREAM and "Release" in loc
        for aspect, kind, direction, loc in locators
    )
    assert any(
        c.aspect == "agent_instructions" and "Release" in c.locator and c.support == 1 for c in report.candidates
    )
    # ruff line-length 120 (main) vs 100 (fixture)
    assert ("pyproject", Kind.DIFFERS, Direction.DOWNSTREAM, "pyproject.toml:tool.ruff.line-length") in locators
    # the fixture lacks the CI badge main has
    assert any(aspect == "readme_badges" and kind is Kind.MISSING for aspect, kind, _, _ in locators)
    # no rule of the fixture's "Development" section that also exists in main is reported
    assert not [
        f
        for f in report.findings
        if f.aspect == "agent_instructions" and f.kind is Kind.MISSING and "python >= 3.11" in f.content_key
    ]


def test_dogfood_cli_json(run_cli: Callable[..., Any]) -> None:
    result = run_cli(["-q", "check", "-c", str(CONFIG), "--no-fetch", "--format", "json"], cwd=ROOT)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["main"]["name"] == "sistent"
    assert payload["errors"] == []
