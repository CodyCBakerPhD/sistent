"""Tests for the ``workflows`` aspect type: option validation, extraction and comparison."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from sistent.aspects.workflows import (
    HEURISTIC_THRESHOLD,
    WorkflowsAspect,
    WorkflowsOptions,
    as_str_list,
    heuristic_pairs,
    job_actions,
    matrix_axes,
    normalised_name,
    scalar_text,
    split_uses,
    trigger_names,
)
from sistent.model import Direction, Finding, Identity, Severity, Snapshot
from sistent.options import BaseOptions, OptionsError, parse_options
from tests.conftest import MakeRepo

MakeSnapshot = Callable[..., Snapshot]
Tuple = tuple[str, str, str, str]
DIR = ".github/workflows"

CI = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:
  schedule:
    - cron: "0 0 * * *"
  workflow_dispatch:
permissions:
  contents: read
concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
jobs:
  test:
    name: Run tests
    runs-on: ${{ matrix.os }}
    if: github.repository == 'org/repo'
    env:
      FOO: bar
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, windows-latest]
        python-version: ["3.11", 3.12]
        include:
          - os: macos-latest
            python-version: "3.12"
        exclude:
          - os: windows-latest
            python-version: "3.11"
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - run: pip install -e ".[test]"
      - uses: ./.github/actions/local-thing
      - uses: docker://alpine:3.18
      - uses: codecov/codecov-action@v4
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - run: ruff check .
  docs:
    uses: org/shared/.github/workflows/docs.yml@v1
    with:
      target: pages
"""

RELEASE = """\
name: Release
on: [push, release]
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: pypa/gh-action-pypi-publish@release/v1
"""

MANUAL = """\
name: Manual
on: workflow_dispatch
jobs:
  ping:
    runs-on: ubuntu-latest
    steps:
      - run: echo pong
"""

BROKEN = "name: Broken\non: [push\njobs: {\n"


def tuples(findings: Iterable[Finding]) -> set[Tuple]:
    return {(f.kind.value, f.subject.value, f.direction.value, f.locator) for f in findings}


def aspect(**options: Any) -> WorkflowsAspect:
    return WorkflowsAspect("workflows", WorkflowsOptions(**options))


def wf(
    file: str, *, name: str | None = None, triggers: Iterable[str] = (), jobs: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {"file": file, "name": name, "triggers": sorted(set(triggers)), "jobs": dict(jobs or {})}


def job(
    *,
    name: str | None = None,
    matrix: dict[str, list[str]] | None = None,
    uses: Iterable[tuple[str, str | None]] = (),
    reusable: str | None = None,
    runs_on: Iterable[str] = ("ubuntu-latest",),
) -> dict[str, Any]:
    return {
        "name": name,
        "runs_on": list(runs_on),
        "matrix": dict(matrix or {}),
        "uses": [{"action": action, "version": version, "step": i} for i, (action, version) in enumerate(uses)],
        "reusable": reusable,
    }


def data(
    *workflows: dict[str, Any],
    present: bool = True,
    unparseable: dict[str, str] | None = None,
    actions: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Snapshot data with the ``actions`` aggregate derived from the workflows unless given explicitly."""
    if actions is None:
        found: dict[str, set[str]] = {}
        for workflow in workflows:
            for j in workflow["jobs"].values():
                for use in j["uses"]:
                    found.setdefault(use["action"], set())
                    if use["version"] is not None:
                        found[use["action"]].add(use["version"])
                if j["reusable"]:
                    action, version = split_uses(j["reusable"])
                    found.setdefault(action, set())
                    if version is not None:
                        found[action].add(version)
        actions = {action: sorted(versions) for action, versions in sorted(found.items())}
    return {
        "present": present,
        "workflows": list(workflows),
        "actions": actions,
        "unparseable": dict(unparseable or {}),
    }


def snap(make_snapshot: MakeSnapshot, repo: str, *workflows: dict[str, Any], **kwargs: Any) -> Snapshot:
    return make_snapshot("workflows", repo, data(*workflows, **kwargs))


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_defaults(self) -> None:
        options = WorkflowsOptions()
        assert options.dir == ".github/workflows"
        assert options.ignore_files == []
        assert options.compare_matrix is True

    def test_default_config_table_parses(self) -> None:
        options = parse_options(WorkflowsOptions, {"dir": ".github/workflows"}, where="aspects.workflows")
        assert options.dir == ".github/workflows"

    def test_wrong_types_and_unknown_keys_are_rejected(self) -> None:
        with pytest.raises(OptionsError, match="expected list"):
            parse_options(WorkflowsOptions, {"ignore_files": "dependabot.yml"}, where="aspects.workflows")
        with pytest.raises(OptionsError, match="expected bool"):
            parse_options(WorkflowsOptions, {"compare_matrix": "no"}, where="aspects.workflows")
        with pytest.raises(OptionsError, match=r"ignore_file: unknown option \(did you mean 'ignore_files'\?\)"):
            parse_options(WorkflowsOptions, {"ignore_file": []}, where="aspects.workflows")

    def test_aspect_requires_its_options_class(self) -> None:
        with pytest.raises(TypeError, match="WorkflowsOptions"):
            WorkflowsAspect("workflows", BaseOptions())

    def test_class_attributes(self) -> None:
        a = aspect()
        assert a.type_name == "workflows"
        assert a.options_cls is WorkflowsOptions
        assert a.default_severity == {"missing.file": "warning"}
        assert a.description
        assert aspect(dir="/ci/workflows/").directory == "ci/workflows"


# ----- helpers -----------------------------------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        ("uses", "expected"),
        [
            ("actions/checkout@v4", ("actions/checkout", "v4")),
            ("  actions/checkout@v4\n", ("actions/checkout", "v4")),
            ("pypa/gh-action-pypi-publish@release/v1", ("pypa/gh-action-pypi-publish", "release/v1")),
            (
                "actions/checkout@0123456789abcdef0123456789abcdef01234567",
                ("actions/checkout", "0123456789abcdef0123456789abcdef01234567"),
            ),
            ("org/repo/.github/workflows/x.yml@v1", ("org/repo/.github/workflows/x.yml", "v1")),
            ("./.github/actions/local", ("./.github/actions/local", None)),
            ("docker://alpine:3.18", ("docker://alpine:3.18", None)),
            ("docker://ghcr.io/org/image@sha256:abc", ("docker://ghcr.io/org/image@sha256:abc", None)),
            ("@v1", ("@v1", None)),
        ],
    )
    def test_split_uses(self, uses: str, expected: tuple[str, str | None]) -> None:
        assert split_uses(uses) == expected

    @pytest.mark.parametrize(
        ("on", "expected"),
        [
            ("push", ["push"]),
            (["push", "release", "push"], ["push", "release"]),
            (
                {"push": {"branches": ["main"]}, "pull_request": None, "schedule": [{"cron": "0 0 * * *"}]},
                ["pull_request", "push", "schedule"],
            ),
            (None, []),
            (True, []),
            ([None, "push"], ["push"]),
        ],
    )
    def test_trigger_names(self, on: Any, expected: list[str]) -> None:
        assert trigger_names(on) == expected

    def test_matrix_axes_skip_include_and_exclude_and_stringify(self) -> None:
        strategy = {
            "fail-fast": False,
            "matrix": {
                "python-version": [3.12, "3.11", 3.10, True],
                "os": "ubuntu-latest",
                "include": [{"os": "macos-latest"}],
                "exclude": [{"os": "windows-latest"}],
                "extra": [{"a": 1}],
            },
        }
        assert matrix_axes(strategy) == {
            "python-version": [
                "3.1",
                "3.11",
                "3.12",
                "true",
            ],  # unquoted 3.10 is the float 3.1 in YAML, as GitHub sees it
            "os": ["ubuntu-latest"],
            "extra": ['{"a":1}'],
        }
        assert matrix_axes({"matrix": "${{ fromJson(needs.plan.outputs.matrix) }}"}) == {}
        assert matrix_axes(None) == {}

    def test_as_str_list_and_scalar_text(self) -> None:
        assert as_str_list(None) == []
        assert as_str_list("ubuntu-latest") == ["ubuntu-latest"]
        assert as_str_list(["a", 1, None]) == ["a", "1", "null"]
        assert as_str_list({"group": "big"}) == ['{"group":"big"}']
        assert scalar_text("x") == "x"
        assert scalar_text(False) == "false"

    def test_normalised_name(self) -> None:
        assert normalised_name("  Run   Tests ") == "run tests"
        assert normalised_name("CI") == normalised_name("ci")
        assert normalised_name("") is None
        assert normalised_name(None) is None
        assert normalised_name(3) is None

    def test_job_actions_include_reusable_and_deduplicate(self) -> None:
        j = job(
            uses=[("actions/checkout", "v4"), ("actions/checkout", "v3"), ("actions/cache", None)],
            reusable="org/shared/.github/workflows/x.yml@v1",
        )
        assert job_actions(j) == ["actions/checkout", "actions/cache", "org/shared/.github/workflows/x.yml"]
        assert job_actions(job()) == []

    def test_heuristic_pairs(self) -> None:
        a = wf("a.yml", triggers=["schedule", "workflow_dispatch"], jobs={"run-tests": job(), "notify": job()})
        b = wf("b.yml", triggers=["schedule", "workflow_dispatch"], jobs={"run-tests": job()})
        c = wf("c.yml", triggers=["push"], jobs={"lint": job()})
        assert heuristic_pairs([a, c], [b]) == [(a, b)]
        assert heuristic_pairs([a], [c]) == []
        assert heuristic_pairs([wf("e.yml")], [wf("f.yml")]) == [], "two empty workflows are not similar"
        assert heuristic_pairs([a, a | {"file": "a2.yml"}], [b]) == [], "an ambiguous best match pairs nothing"
        assert heuristic_pairs([a], [b, b | {"file": "b2.yml"}]) == []
        assert HEURISTIC_THRESHOLD == 0.5


# ----- extraction --------------------------------------------------------------------------------------------------


class TestExtract:
    def test_realistic_workflow_directory(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {
                f"{DIR}/ci.yml": CI,
                f"{DIR}/release.yaml": RELEASE,
                f"{DIR}/manual.yml": MANUAL,
                f"{DIR}/broken.yml": BROKEN,
                f"{DIR}/notes.txt": "not a workflow",
                ".github/dependabot.yml": "version: 2\n",
            },
            substitute=False,
        )
        snapshot = aspect().run_extract(ctx)
        payload = dict(snapshot.data)
        unparseable = payload.pop("unparseable")
        assert list(unparseable) == ["broken.yml"]
        assert unparseable["broken.yml"]
        assert payload == {
            "present": True,
            "workflows": [
                {
                    "file": "ci.yml",
                    "name": "CI",
                    "triggers": ["pull_request", "push", "schedule", "workflow_dispatch"],
                    "jobs": {
                        "test": {
                            "name": "Run tests",
                            "runs_on": ["${{ matrix.os }}"],
                            "matrix": {"os": ["ubuntu-latest", "windows-latest"], "python-version": ["3.11", "3.12"]},
                            "uses": [
                                {"action": "actions/checkout", "version": "v4", "step": 0},
                                {"action": "actions/setup-python", "version": "v5", "step": 1},
                                {"action": "./.github/actions/local-thing", "version": None, "step": 3},
                                {"action": "docker://alpine:3.18", "version": None, "step": 4},
                                {"action": "codecov/codecov-action", "version": "v4", "step": 5},
                            ],
                            "reusable": None,
                        },
                        "lint": {
                            "name": None,
                            "runs_on": ["ubuntu-latest"],
                            "matrix": {},
                            "uses": [{"action": "actions/checkout", "version": "v3", "step": 0}],
                            "reusable": None,
                        },
                        "docs": {
                            "name": None,
                            "runs_on": [],
                            "matrix": {},
                            "uses": [],
                            "reusable": "org/shared/.github/workflows/docs.yml@v1",
                        },
                    },
                },
                {
                    "file": "manual.yml",
                    "name": "Manual",
                    "triggers": ["workflow_dispatch"],
                    "jobs": {
                        "ping": {"name": None, "runs_on": ["ubuntu-latest"], "matrix": {}, "uses": [], "reusable": None}
                    },
                },
                {
                    "file": "release.yaml",
                    "name": "Release",
                    "triggers": ["push", "release"],
                    "jobs": {
                        "build": {
                            "name": None,
                            "runs_on": ["ubuntu-latest"],
                            "matrix": {},
                            "uses": [
                                {"action": "actions/checkout", "version": "v4", "step": 0},
                                {"action": "pypa/gh-action-pypi-publish", "version": "release/v1", "step": 1},
                            ],
                            "reusable": None,
                        }
                    },
                },
            ],
            "actions": {
                "./.github/actions/local-thing": [],
                "actions/checkout": ["v3", "v4"],
                "actions/setup-python": ["v5"],
                "codecov/codecov-action": ["v4"],
                "docker://alpine:3.18": [],
                "org/shared/.github/workflows/docs.yml": ["v1"],
                "pypa/gh-action-pypi-publish": ["release/v1"],
            },
        }
        assert snapshot.sources == (f"{DIR}/broken.yml", f"{DIR}/ci.yml", f"{DIR}/manual.yml", f"{DIR}/release.yaml")

    def test_identity_is_substituted(self, make_repo: MakeRepo) -> None:
        text = (
            "name: neuroconv tests\non: push\njobs:\n  test-neuroconv:\n    name: Test neuroconv\n    runs-on: ubuntu-latest\n"
            "    strategy:\n      matrix:\n        package: [neuroconv, other]\n"
            "    steps:\n      - uses: catalystneuro/setup-action@v2\n"
            "  docs:\n    uses: catalystneuro/neuroconv/.github/workflows/docs.yml@v1\n"
        )
        identity = Identity(name="neuroconv", aliases=("neuroconv",), org="catalystneuro")
        ctx = make_repo({f"{DIR}/ci.yml": text}, identity=identity)
        payload = aspect().extract(ctx)
        (workflow,) = payload["workflows"]
        assert workflow["name"] == "{{name}} tests"
        assert list(workflow["jobs"]) == ["test-{{name}}", "docs"]
        test = workflow["jobs"]["test-{{name}}"]
        assert test["name"] == "Test {{name}}"
        assert test["matrix"] == {"package": ["other", "{{name}}"]}
        assert test["uses"] == [{"action": "{{org}}/setup-action", "version": "v2", "step": 0}]
        assert workflow["jobs"]["docs"]["reusable"] == "{{org}}/{{name}}/.github/workflows/docs.yml@v1"
        assert payload["actions"] == {
            "{{org}}/setup-action": ["v2"],
            "{{org}}/{{name}}/.github/workflows/docs.yml": ["v1"],
        }

    def test_directory_absent(self, make_repo: MakeRepo) -> None:
        snapshot = aspect().run_extract(make_repo({"README.md": "x"}))
        assert snapshot.data == {"present": False, "workflows": [], "actions": {}, "unparseable": {}}
        assert snapshot.sources == ()

    def test_directory_present_but_empty(self, make_repo: MakeRepo) -> None:
        payload = aspect().extract(make_repo({f"{DIR}/": ""}))
        assert payload == {"present": True, "workflows": [], "actions": {}, "unparseable": {}}

    def test_custom_directory(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"ci/manual.yml": MANUAL, f"{DIR}/ci.yml": CI}, substitute=False)
        payload = aspect(dir="ci/").extract(ctx)
        assert [w["file"] for w in payload["workflows"]] == ["manual.yml"]

    def test_ignore_files_are_skipped_and_recorded(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {f"{DIR}/ci.yml": CI, f"{DIR}/dependabot-auto.yml": MANUAL, f"{DIR}/codeql.yml": MANUAL}, substitute=False
        )
        payload = aspect(ignore_files=["dependabot*", "CODEQL.yml"]).extract(ctx)
        assert [w["file"] for w in payload["workflows"]] == ["ci.yml"]
        assert ctx.ignored == [f"{DIR}/codeql.yml (ignore_files)", f"{DIR}/dependabot-auto.yml (ignore_files)"]

    def test_compare_matrix_off_extracts_no_matrix(self, make_repo: MakeRepo) -> None:
        payload = aspect(compare_matrix=False).extract(make_repo({f"{DIR}/ci.yml": CI}, substitute=False))
        assert payload["workflows"][0]["jobs"]["test"]["matrix"] == {}

    def test_empty_and_non_mapping_documents_are_unparseable(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({f"{DIR}/empty.yml": "# nothing\n", f"{DIR}/list.yml": "- a\n- b\n"})
        payload = aspect().extract(ctx)
        assert payload["workflows"] == []
        assert payload["unparseable"] == {"empty.yml": "empty document", "list.yml": "top level is not a mapping"}

    def test_odd_shapes_do_not_raise(self, make_repo: MakeRepo) -> None:
        text = "on:\n  push:\njobs:\n  weird: just a string\n  ok:\n    runs-on: [self-hosted, linux]\n    steps: not-a-list\n"
        payload = aspect().extract(make_repo({f"{DIR}/ci.yml": text}, substitute=False))
        (workflow,) = payload["workflows"]
        assert workflow["name"] is None
        assert workflow["jobs"] == {
            "weird": {"name": None, "runs_on": [], "matrix": {}, "uses": [], "reusable": None},
            "ok": {"name": None, "runs_on": ["self-hosted", "linux"], "matrix": {}, "uses": [], "reusable": None},
        }


# ----- comparison --------------------------------------------------------------------------------------------------


class TestDirectoryGate:
    def test_missing_directory_is_a_warning(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", triggers=["push"]))
        other = snap(make_snapshot, "sat", present=False)
        findings = aspect().compare(main, other)
        assert tuples(findings) == {("missing", "file", "downstream", DIR)}
        assert findings[0].severity is Severity.WARNING

    def test_user_severity_beats_type_default(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml"))
        other = snap(make_snapshot, "sat", present=False)
        assert aspect(severity={"missing.file": "error"}).compare(main, other)[0].severity is Severity.ERROR

    def test_absent_everywhere_or_only_in_main_is_silent(self, make_snapshot: MakeSnapshot) -> None:
        absent_main = snap(make_snapshot, "main", present=False)
        assert aspect().compare(absent_main, snap(make_snapshot, "sat", present=False)) == []
        assert aspect().compare(absent_main, snap(make_snapshot, "sat", wf("ci.yml", triggers=["push"]))) == []

    def test_custom_directory_locator(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml"))
        other = snap(make_snapshot, "sat", present=False)
        assert tuples(aspect(dir="ci/").compare(main, other)) == {("missing", "file", "downstream", "ci")}


class TestWorkflowSet:
    def test_missing_and_extra_workflows(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", name="CI", triggers=["push"], jobs={"test": job()}),
            wf("release.yml", name="Release", triggers=["release"], jobs={"build": job()}),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf("ci.yml", name="CI", triggers=["push"], jobs={"test": job()}),
            wf("extra.yml", name="Extra", triggers=["push"], jobs={"deploy": job()}),
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "workflow", "downstream", f"{DIR}:release.yml"),
            ("extra", "workflow", "upstream", f"{DIR}:extra.yml"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["missing"].severity is Severity.WARNING
        assert by_kind["missing"].content_key == "release.yml"
        assert by_kind["extra"].severity is Severity.INFO

    def test_filename_match_wins_over_name(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", name="CI", triggers=["push"]))
        other = snap(make_snapshot, "sat", wf("ci.yml", name="Continuous integration", triggers=["push"]))
        assert aspect().compare(main, other) == []

    def test_matched_by_name_is_moved_and_uses_main_locators(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", name="CI", triggers=["push"], jobs={"test": job()}))
        other = snap(
            make_snapshot, "sat", wf("tests.yml", name="  ci ", triggers=["push", "schedule"], jobs={"test": job()})
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("moved", "workflow", "downstream", f"{DIR}:ci.yml"),
            ("extra", "key", "upstream", f"{DIR}:ci.yml:on.schedule"),
        }
        moved = next(f for f in findings if f.kind.value == "moved")
        assert moved.severity is Severity.INFO
        assert moved.content_key == "ci.yml"

    def test_matched_by_heuristic(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf(
                "dailies.yml",
                name="Daily tests",
                triggers=["schedule", "workflow_dispatch"],
                jobs={"run-tests": job(), "notify": job()},
            ),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf("run-tests.yml", name="Nightly", triggers=["schedule", "workflow_dispatch"], jobs={"run-tests": job()}),
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("moved", "workflow", "downstream", f"{DIR}:dailies.yml"),
            ("missing", "job", "downstream", f"{DIR}:dailies.yml:jobs.notify"),
        }

    def test_heuristic_below_threshold(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("a.yml", name="A", triggers=["push"], jobs={"test": job(), "lint": job(), "docs": job()}),
        )
        other = snap(make_snapshot, "sat", wf("b.yml", name="B", triggers=["push"], jobs={"build": job()}))
        assert tuples(aspect().compare(main, other)) == {
            ("missing", "workflow", "downstream", f"{DIR}:a.yml"),
            ("extra", "workflow", "upstream", f"{DIR}:b.yml"),
        }

    def test_ambiguous_heuristic_pairs_nothing(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("a.yml", name="A", triggers=["push"], jobs={"test": job()}))
        other = snap(
            make_snapshot,
            "sat",
            wf("b.yml", name="B", triggers=["push"], jobs={"test": job()}),
            wf("c.yml", name="C", triggers=["push"], jobs={"test": job()}),
        )
        assert tuples(aspect().compare(main, other)) == {
            ("missing", "workflow", "downstream", f"{DIR}:a.yml"),
            ("extra", "workflow", "upstream", f"{DIR}:b.yml"),
            ("extra", "workflow", "upstream", f"{DIR}:c.yml"),
        }

    def test_unnamed_workflows_only_match_by_file_or_heuristic(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("a.yml", triggers=["push"], jobs={"x": job()}))
        other = snap(make_snapshot, "sat", wf("b.yml", triggers=["release"], jobs={"y": job()}))
        assert tuples(aspect().compare(main, other)) == {
            ("missing", "workflow", "downstream", f"{DIR}:a.yml"),
            ("extra", "workflow", "upstream", f"{DIR}:b.yml"),
        }


class TestTriggers:
    def test_trigger_sets(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", triggers=["push", "pull_request", "schedule"]))
        other = snap(make_snapshot, "sat", wf("ci.yml", triggers=["push", "workflow_dispatch"]))
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "key", "downstream", f"{DIR}:ci.yml:on.pull_request"),
            ("missing", "key", "downstream", f"{DIR}:ci.yml:on.schedule"),
            ("extra", "key", "upstream", f"{DIR}:ci.yml:on.workflow_dispatch"),
        }
        assert {f.content_key for f in findings} == {"pull_request", "schedule", "workflow_dispatch"}
        assert all(f.severity is Severity.WARNING for f in findings if f.kind.value == "missing")


class TestJobs:
    def test_missing_extra_and_moved_jobs(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", jobs={"test": job(), "lint": job(name="Lint"), "docs": job()}))
        other = snap(
            make_snapshot, "sat", wf("ci.yml", jobs={"test": job(), "linting": job(name="lint"), "build": job()})
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "job", "downstream", f"{DIR}:ci.yml:jobs.docs"),
            ("extra", "job", "upstream", f"{DIR}:ci.yml:jobs.build"),
            ("moved", "job", "downstream", f"{DIR}:ci.yml:jobs.lint"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["missing"].severity is Severity.WARNING
        assert by_kind["missing"].content_key == "docs"
        assert by_kind["moved"].severity is Severity.INFO
        assert by_kind["moved"].content_key == "lint"

    def test_moved_job_is_compared_under_main_id(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot, "main", wf("ci.yml", jobs={"lint": job(name="Lint", matrix={"os": ["ubuntu-latest"]})})
        )
        other = snap(
            make_snapshot, "sat", wf("ci.yml", jobs={"linting": job(name="Lint", matrix={"os": ["macos-latest"]})})
        )
        assert tuples(aspect().compare(main, other)) == {
            ("moved", "job", "downstream", f"{DIR}:ci.yml:jobs.lint"),
            ("differs", "value", "downstream", f"{DIR}:ci.yml:jobs.lint.strategy.matrix.os"),
        }

    def test_unmatched_jobs_are_not_descended(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"docs": job(matrix={"os": ["ubuntu-latest"]}, uses=[("actions/checkout", "v4")])}),
        )
        other = snap(make_snapshot, "sat", wf("ci.yml", jobs={"build": job(uses=[("actions/checkout", "v4")])}))
        assert tuples(aspect().compare(main, other)) == {
            ("missing", "job", "downstream", f"{DIR}:ci.yml:jobs.docs"),
            ("extra", "job", "upstream", f"{DIR}:ci.yml:jobs.build"),
        }


class TestMatrix:
    def test_axes_and_values(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"test": job(matrix={"python-version": ["3.11", "3.12"], "os": ["ubuntu-latest"]})}),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf("ci.yml", jobs={"test": job(matrix={"python-version": ["3.10", "3.11"], "numpy": ["1", "2"]})}),
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "value", "downstream", f"{DIR}:ci.yml:jobs.test.strategy.matrix.os"),
            ("extra", "value", "upstream", f"{DIR}:ci.yml:jobs.test.strategy.matrix.numpy"),
            ("differs", "value", "downstream", f"{DIR}:ci.yml:jobs.test.strategy.matrix.python-version"),
        }
        assert all(f.severity is Severity.INFO for f in findings)
        assert all(f.option == "aspects.workflows.compare_matrix=True" for f in findings)
        assert {f.content_key for f in findings} == {"os", "numpy", "python-version"}

    def test_order_is_irrelevant(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot, "main", wf("ci.yml", jobs={"test": job(matrix={"python-version": ["3.12", "3.11"]})})
        )
        other = snap(
            make_snapshot, "sat", wf("ci.yml", jobs={"test": job(matrix={"python-version": ["3.11", "3.12", "3.11"]})})
        )
        assert aspect().compare(main, other) == []

    def test_compare_matrix_off(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", jobs={"test": job(matrix={"os": ["ubuntu-latest"]})}))
        other = snap(make_snapshot, "sat", wf("ci.yml", jobs={"test": job(matrix={"os": ["macos-latest"]})}))
        assert aspect(compare_matrix=False).compare(main, other) == []


class TestJobActions:
    def test_action_used_elsewhere_in_repo_is_reported_per_job(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4"), ("actions/cache", "v4")])}),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf(
                "ci.yml",
                jobs={"test": job(uses=[("actions/checkout", "v4")]), "lint": job(uses=[("actions/cache", "v4")])},
            ),
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "action", "downstream", f"{DIR}:ci.yml:jobs.test.steps"),
            ("extra", "job", "upstream", f"{DIR}:ci.yml:jobs.lint"),
        }
        missing = next(f for f in findings if f.kind.value == "missing")
        assert missing.severity is Severity.INFO
        assert missing.content_key == "actions/cache"

    def test_action_absent_from_repo_is_reported_once_at_the_aggregate(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4"), ("actions/cache", "v4")])}),
        )
        other = snap(make_snapshot, "sat", wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4")])}))
        findings = aspect().compare(main, other)
        assert tuples(findings) == {("missing", "action", "downstream", f"{DIR}:actions/cache")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].content_key == "actions/cache"

    def test_extra_actions_mirror_the_missing_rules(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf(
                "ci.yml",
                jobs={
                    "test": job(uses=[("actions/checkout", "v4")]),
                    "lint": job(uses=[("actions/setup-python", "v5")]),
                },
            ),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf(
                "ci.yml",
                jobs={
                    "test": job(
                        uses=[("actions/checkout", "v4"), ("actions/setup-python", "v5"), ("actions/cache", "v4")]
                    ),
                    "lint": job(uses=[("actions/setup-python", "v5")]),
                },
            ),
        )
        assert tuples(aspect().compare(main, other)) == {
            ("extra", "action", "upstream", f"{DIR}:ci.yml:jobs.test.steps"),
            ("extra", "action", "upstream", f"{DIR}:actions/cache"),
        }

    def test_reusable_workflow_counts_as_an_action(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"docs": job(runs_on=(), reusable="org/shared/.github/workflows/docs.yml@v1")}),
        )
        other = snap(make_snapshot, "sat", wf("ci.yml", jobs={"docs": job(uses=[("actions/checkout", "v4")])}))
        assert tuples(aspect().compare(main, other)) == {
            ("missing", "action", "downstream", f"{DIR}:org/shared/.github/workflows/docs.yml"),
            ("extra", "action", "upstream", f"{DIR}:actions/checkout"),
        }


class TestActionAggregate:
    def test_version_drift(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4")])}))
        other = snap(make_snapshot, "sat", wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v3")])}))
        findings = aspect().compare(main, other)
        assert tuples(findings) == {("differs", "action", "downstream", f"{DIR}:actions/checkout")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].content_key == "actions/checkout"

    def test_version_sets_across_workflows(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4")])}),
            wf("docs.yml", jobs={"docs": job(uses=[("actions/checkout", "v3")])}),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf("ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4")])}),
            wf("docs.yml", jobs={"docs": job(uses=[("actions/checkout", "v4")])}),
        )
        assert tuples(aspect().compare(main, other)) == {("differs", "action", "downstream", f"{DIR}:actions/checkout")}
        assert snap(make_snapshot, "x", *main.data["workflows"]).data["actions"] == {"actions/checkout": ["v3", "v4"]}

    def test_reusable_workflow_version_drift(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", jobs={"docs": job(runs_on=(), reusable="org/shared/.github/workflows/docs.yml@v1")}),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf("ci.yml", jobs={"docs": job(runs_on=(), reusable="org/shared/.github/workflows/docs.yml@v2")}),
        )
        assert tuples(aspect().compare(main, other)) == {
            ("differs", "action", "downstream", f"{DIR}:org/shared/.github/workflows/docs.yml")
        }

    def test_same_versions_and_unpinned_actions_are_silent(self, make_snapshot: MakeSnapshot) -> None:
        workflow = wf(
            "ci.yml", jobs={"test": job(uses=[("actions/checkout", "v4"), ("./.github/actions/local", None)])}
        )
        assert aspect().compare(snap(make_snapshot, "main", workflow), snap(make_snapshot, "sat", workflow)) == []

    def test_aggregate_is_independent_of_workflow_layout(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf("ci.yml", name="CI", triggers=["push"], jobs={"test": job(uses=[("actions/checkout", "v4")])}),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf(
                "tests.yaml", name="Tests", triggers=["release"], jobs={"build": job(uses=[("actions/checkout", "v3")])}
            ),
        )
        findings = aspect().compare(main, other)
        assert ("differs", "action", "downstream", f"{DIR}:actions/checkout") in tuples(findings)
        assert ("missing", "workflow", "downstream", f"{DIR}:ci.yml") in tuples(findings)


class TestUnparseable:
    def test_unparseable_in_satellite(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot, "main", wf("broken.yml", name="Broken", triggers=["push"]), wf("ci.yml", triggers=["push"])
        )
        other = snap(
            make_snapshot,
            "sat",
            wf("ci.yml", triggers=["push"]),
            unparseable={"broken.yml": "mapping values are not allowed here"},
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {("unparseable", "file", "none", f"{DIR}:broken.yml")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].detail == "mapping values are not allowed here"
        assert findings[0].detail_kind == "text"

    def test_unparseable_in_main_is_not_extra_in_satellite(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", wf("ci.yml", triggers=["push"]), unparseable={"broken.yml": "bad"})
        other = snap(
            make_snapshot, "sat", wf("ci.yml", triggers=["push"]), wf("broken.yml", name="Broken", triggers=["push"])
        )
        assert aspect().compare(main, other) == []


class TestDirections:
    def test_upstream_findings_are_info_and_never_gate(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            wf(
                "ci.yml", triggers=["push"], jobs={"test": job(uses=[("actions/checkout", "v4")], matrix={"os": ["a"]})}
            ),
        )
        other = snap(
            make_snapshot,
            "sat",
            wf(
                "ci.yml",
                triggers=["push", "schedule"],
                jobs={
                    "test": job(
                        uses=[("actions/checkout", "v4"), ("actions/cache", "v4")], matrix={"os": ["a"], "py": ["3"]}
                    ),
                    "lint": job(),
                },
            ),
            wf("extra.yml", name="Extra", triggers=["release"], jobs={"x": job()}),
        )
        findings = aspect(severity={"extra": "error"}).compare(main, other)
        assert findings
        assert {f.kind.value for f in findings} == {"extra"}
        assert all(f.direction is Direction.UPSTREAM and f.severity is Severity.INFO and not f.gates for f in findings)
        assert all((f.aspect, f.repo) == ("workflows", "sat") for f in findings)


class TestRoundTrip:
    def test_identical_workflows_agree(self, make_repo: MakeRepo) -> None:
        files = {f"{DIR}/ci.yml": CI, f"{DIR}/release.yaml": RELEASE}
        a = aspect()
        main = a.run_extract(make_repo(files, name="main-repo"))
        other = a.run_extract(make_repo(files, name="sat-repo"))
        assert main.data == other.data
        assert a.compare(main, other) == []

    def test_drift_is_found_end_to_end(self, make_repo: MakeRepo) -> None:
        a = aspect()
        main = a.run_extract(make_repo({f"{DIR}/ci.yml": CI, f"{DIR}/release.yaml": RELEASE}, name="main-repo"))
        drifted = CI.replace("actions/checkout@v4", "actions/checkout@v3").replace("  pull_request:\n", "")
        other = a.run_extract(make_repo({f"{DIR}/ci.yml": drifted, f"{DIR}/broken.yml": BROKEN}, name="sat-repo"))
        assert tuples(a.compare(main, other)) == {
            ("unparseable", "file", "none", f"{DIR}:broken.yml"),
            ("missing", "workflow", "downstream", f"{DIR}:release.yaml"),
            ("missing", "key", "downstream", f"{DIR}:ci.yml:on.pull_request"),
            ("missing", "action", "downstream", f"{DIR}:pypa/gh-action-pypi-publish"),
            ("differs", "action", "downstream", f"{DIR}:actions/checkout"),
        }
