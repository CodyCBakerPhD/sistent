"""The ``sistent`` command line, run in-process through click's runner with the tiny aspects from tests.helpers."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

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

[repos.main]
path = "main"

[repos.sat1]
path = "sat1"

[repos.sat2]
path = "sat2"

[aspects.rules]
type = "tests.helpers:LinesAspect"
"""


@pytest.fixture
def fleet(tmp_path: Path, make_config: Callable[..., Path]) -> Path:
    write_files(tmp_path, FLEET)
    return make_config(CONFIG)


def test_check_text_report_and_exit_code(run_cli: Callable[..., Any], fleet: Path) -> None:
    result = run_cli(["-q", "check", "-c", str(fleet)])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "main main" in out.splitlines()[0]
    assert "sat1" in out
    assert "RULES.txt#gamma" in out
    assert "Candidates for main" in out
    assert "RULES.txt#delta" in out
    assert out.rstrip().splitlines()[-1].endswith("exit 1")


def test_check_fail_on_and_summary(run_cli: Callable[..., Any], fleet: Path) -> None:
    result = run_cli(["-q", "check", "-c", str(fleet), "--fail-on", "never", "--summary"])
    assert result.exit_code == 0, result.output
    assert "RULES.txt#gamma" not in result.output
    assert "TOTAL" in result.output


def test_check_json_and_markdown(run_cli: Callable[..., Any], fleet: Path, tmp_path: Path) -> None:
    result = run_cli(["-q", "check", "-c", str(fleet), "--format", "json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["schema_version"] == 1
    assert payload["exit_code"] == 1
    assert {f["repo"] for f in payload["findings"]} == {"sat1", "sat2"}
    assert payload["candidates"][0]["support"] == 2

    out = tmp_path / "report.md"
    result = run_cli(["-q", "check", "-c", str(fleet), "--format", "markdown", "-o", str(out), "--repo", "sat1"])
    assert result.exit_code == 1
    text = out.read_text()
    assert text.startswith("# sistent report")
    assert "## sat1" in text
    assert "## sat2" not in text


def test_check_filters_and_flags(run_cli: Callable[..., Any], fleet: Path) -> None:
    result = run_cli(
        ["-q", "check", "-c", str(fleet), "--repo", "sat2", "--direction", "both", "--min-severity", "warning"]
    )
    assert result.exit_code == 1
    assert "sat1" not in result.output.split("Candidates")[0].split("\n", 6)[-1]
    result = run_cli(["-q", "check", "-c", str(fleet), "--repo", "nope"])
    assert result.exit_code == 2
    assert "--repo 'nope'" in result.output
    result = run_cli(
        [
            "-q",
            "check",
            "-c",
            str(fleet),
            "--aspect",
            "rules",
            "--group-by",
            "aspect",
            "--sort",
            "severity",
            "--no-candidates",
        ]
    )
    assert result.exit_code == 1
    assert "Candidates for main" not in result.output


def test_check_baseline_flags(run_cli: Callable[..., Any], fleet: Path, tmp_path: Path) -> None:
    baseline = tmp_path / "base.json"
    result = run_cli(["check", "-c", str(fleet), "--baseline", str(baseline), "--update-baseline"])
    assert result.exit_code == 0, result.output
    assert baseline.exists()
    result = run_cli(["-q", "check", "-c", str(fleet), "--baseline", str(baseline)])
    assert result.exit_code == 0
    result = run_cli(["-q", "check", "-c", str(fleet), "--baseline", str(baseline), "--show-baseline"])
    assert "[baselined]" in result.output


def test_config_errors_exit_2(run_cli: Callable[..., Any], tmp_path: Path, make_config: Callable[..., Path]) -> None:
    bad = make_config('[sistent]\nmain = "x"\n[repos.x]\npath = "."\n[aspects.a]\ntype = "nope"\n', name="bad.toml")
    result = run_cli(["check", "-c", str(bad)])
    assert result.exit_code == 2
    assert "error:" in result.output
    result = run_cli(["check", "-c", str(tmp_path / "missing.toml")])
    assert result.exit_code == 2


def test_unavailable_satellite_exit_3(run_cli: Callable[..., Any], fleet: Path, tmp_path: Path) -> None:
    import shutil

    shutil.rmtree(tmp_path / "sat2")
    result = run_cli(["-q", "check", "-c", str(fleet), "--fail-on", "never"])
    assert result.exit_code == 3
    assert "unavailable" in result.output
    result = run_cli(["-q", "check", "-c", str(fleet), "--fail-on", "never", "--allow-unavailable"])
    assert result.exit_code == 0


def test_snapshot_and_diff(run_cli: Callable[..., Any], fleet: Path) -> None:
    result = run_cli(["-q", "snapshot", "-c", str(fleet), "sat1"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repo"] == "sat1"
    assert payload["data"]["lines"] == ["alpha", "beta", "delta"]
    result = run_cli(["-q", "snapshot", "-c", str(fleet), "sat1", "--format", "text", "--explain"])
    assert "== sat1 / rules" in result.output

    result = run_cli(["-q", "diff", "-c", str(fleet), "sat1"])
    assert result.exit_code == 0, result.output
    assert "--- main/rules" in result.output
    assert "+++ sat1/rules" in result.output
    assert any(line.startswith("-") and '"gamma"' in line for line in result.output.splitlines())
    result = run_cli(["-q", "diff", "-c", str(fleet), "sat1", "sat2", "--raw"])
    assert "--- sat1/RULES.txt" in result.output
    assert "+++ sat2/RULES.txt" in result.output


def test_repos_fetch_aspects_config(run_cli: Callable[..., Any], fleet: Path) -> None:
    result = run_cli(["-q", "repos", "-c", str(fleet)])
    assert result.exit_code == 0, result.output
    assert "main (main)" in result.output
    assert "sat2" in result.output

    result = run_cli(["-q", "fetch", "-c", str(fleet)])
    assert result.exit_code == 0
    assert "nothing to fetch" in result.output

    result = run_cli(["-q", "aspects", "--type", "tests.helpers:LinesAspect"])
    assert result.exit_code == 0, result.output
    assert "file" in result.output
    assert "non-blank lines" in result.output

    result = run_cli(["-q", "config", "-c", str(fleet)])
    assert result.exit_code == 0, result.output
    assert "[aspects.rules]" in result.output
    assert 'type = "tests.helpers:LinesAspect"' in result.output


def test_init_writes_a_loadable_config(run_cli: Callable[..., Any], tmp_path: Path) -> None:
    from sistent.config import load_config

    target = tmp_path / "sistent.toml"
    result = run_cli(["init", str(target), "--main", "neuroconv", "--github", "catalystneuro"])
    assert result.exit_code == 0, result.output
    text = target.read_text()
    assert 'main = "neuroconv"' in text
    assert "[aspects.agent_instructions]" in text
    assert "skills" in text
    pytest.importorskip("sistent.aspects.markdown")
    config = load_config(target)
    assert config.main == "neuroconv"
    assert config.repos["neuroconv"].url == "https://github.com/catalystneuro/neuroconv"

    result = run_cli(["init", str(target)])
    assert result.exit_code == 2  # exists

    (tmp_path / "siblings" / "one" / ".git").mkdir(parents=True)
    (tmp_path / "siblings" / "two" / ".git").mkdir(parents=True)
    other = tmp_path / "siblings" / "sistent.toml"
    result = run_cli(
        ["init", str(other), "--from", str(tmp_path / "siblings" / "one"), "--repos", str(tmp_path / "siblings" / "*")]
    )
    assert result.exit_code == 0, result.output
    text = other.read_text()
    assert 'main = "one"' in text
    assert '[repos.one]\npath = "one"' in text
    assert '[repos.two]\npath = "two"' in text
