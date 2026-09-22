"""Tests for the ``toml`` aspect type: option validation, extraction and per-mode comparison."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable, Iterable
from typing import Any

import pytest

from sistent.aspects import DEFAULT_CONFIG
from sistent.aspects.base import UnparseableFile
from sistent.aspects.toml import MODES, TomlAspect, TomlOptions
from sistent.model import Direction, Finding, Identity, Severity, Snapshot
from sistent.options import BaseOptions, OptionsError, parse_options
from tests.conftest import MakeRepo

MakeSnapshot = Callable[..., Snapshot]
Tuple = tuple[str, str, str, str]

DEFAULT_TABLE: dict[str, Any] = tomllib.loads(DEFAULT_CONFIG)["aspects"]["pyproject"]
DEFAULT_KEYS: dict[str, str] = DEFAULT_TABLE["keys"]

PYPROJECT = """\
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "neuroconv"
requires-python = ">=3.10"
classifiers = [
    "Programming Language :: Python :: 3",
    "Programming Language :: Python :: 3.11",
]

[project.urls]
Homepage = "https://github.com/catalystneuro/neuroconv"
Documentation = "https://neuroconv.readthedocs.io/"

[project.optional-dependencies]
test = ["pytest>=7", "pytest-cov"]
docs = ["sphinx"]

[tool.hatch.build.targets.wheel]
packages = ["src/neuroconv"]

[tool.ruff]
line-length = 120

[tool.ruff.lint]
select = ["E", "F"]

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def tuples(findings: Iterable[Finding]) -> set[Tuple]:
    return {(f.kind.value, f.subject.value, f.direction.value, f.locator) for f in findings}


def aspect(keys: dict[str, str] | None = None, **options: Any) -> TomlAspect:
    options.setdefault("file", "pyproject.toml")
    return TomlAspect("pyproject", TomlOptions(keys=keys or {}, **options))


def snap(
    make_snapshot: MakeSnapshot, repo: str, values: dict[str, Any] | None = None, *, present: bool = True
) -> Snapshot:
    return make_snapshot("pyproject", repo, {"present": present, "values": values or {}})


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_default_config_table_parses(self) -> None:
        table = {key: value for key, value in DEFAULT_TABLE.items() if key != "type"}
        options = parse_options(TomlOptions, table, where="aspects.pyproject")
        assert options.file == "pyproject.toml"
        assert options.keys == DEFAULT_KEYS
        assert set(options.keys.values()) <= MODES
        assert options.required is False

    def test_every_mode_is_accepted(self) -> None:
        options = TomlOptions(file="x.toml", keys={f"k.{mode}": mode for mode in MODES})
        assert set(options.keys.values()) == MODES
        assert sorted(MODES) == ["exact", "keys", "present", "requirements", "set"]

    def test_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(
            OptionsError,
            match=r"keys\.tool\.ruff: unknown mode 'fuzzy' \(valid: exact, keys, present, requirements, set\)",
        ):
            TomlOptions(file="pyproject.toml", keys={"tool.ruff": "fuzzy"})

    def test_unknown_mode_is_rejected_through_parse_options(self) -> None:
        with pytest.raises(OptionsError, match="unknown mode"):
            parse_options(TomlOptions, {"file": "pyproject.toml", "keys": {"a": "b"}}, where="aspects.x")

    def test_empty_key_is_rejected(self) -> None:
        with pytest.raises(OptionsError, match="empty dotted key"):
            TomlOptions(file="pyproject.toml", keys={"": "exact"})

    def test_file_is_required(self) -> None:
        with pytest.raises(OptionsError, match=r"missing required option\(s\): file"):
            parse_options(TomlOptions, {}, where="aspects.x")

    def test_wrong_value_types_are_rejected(self) -> None:
        with pytest.raises(OptionsError, match="expected dict"):
            parse_options(TomlOptions, {"file": "a", "keys": ["a"]}, where="aspects.x")
        with pytest.raises(OptionsError, match="expected bool"):
            parse_options(TomlOptions, {"file": "a", "required": "yes"}, where="aspects.x")

    def test_aspect_requires_its_options_class(self) -> None:
        with pytest.raises(TypeError, match="TomlOptions"):
            TomlAspect("pyproject", BaseOptions())

    def test_class_attributes(self) -> None:
        a = aspect()
        assert a.type_name == "toml"
        assert a.options_cls is TomlOptions
        assert a.description
        assert a.options_dict()["keys"] == {}


# ----- extraction --------------------------------------------------------------------------------------------------


class TestExtract:
    def test_default_keys_on_a_realistic_pyproject(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {"pyproject.toml": PYPROJECT}, name="neuroconv", url="https://github.com/catalystneuro/neuroconv"
        )
        snapshot = aspect(DEFAULT_KEYS).run_extract(ctx)
        assert snapshot.data == {
            "present": True,
            "values": {
                "build-system": {"requires": ["hatchling>=1.27"], "build-backend": "hatchling.build"},
                "project.requires-python": ">=3.10",
                "project.classifiers": [
                    "Programming Language :: Python :: 3",
                    "Programming Language :: Python :: 3.11",
                ],
                "project.urls": {
                    "Homepage": "https://github.com/{{org}}/{{name}}",
                    "Documentation": "https://{{name}}.readthedocs.io/",
                },
                "project.optional-dependencies.test": ["pytest>=7", "pytest-cov"],
                "project.optional-dependencies.dev": None,
                "project.optional-dependencies.docs": ["sphinx"],
                "tool.ruff": {"line-length": 120, "lint": {"select": ["E", "F"]}},
                "tool.pytest.ini_options": {"testpaths": ["tests"]},
                "tool.codespell": None,
                "tool.coverage": None,
                "tool.mypy": None,
            },
        }
        assert snapshot.sources == ("pyproject.toml",)
        assert "neuroconv" in snapshot.aliases_applied
        assert list(snapshot.data["values"]) == list(DEFAULT_KEYS), "values follow the configured key order"

    def test_package_paths_are_substituted(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"pyproject.toml": PYPROJECT}, name="neuroconv")
        data = aspect({"tool.hatch.build.targets.wheel.packages": "exact"}).extract(ctx)
        assert data == {"present": True, "values": {"tool.hatch.build.targets.wheel.packages": ["src/{{name}}"]}}

    def test_short_package_name_is_substituted_when_forced(self, make_repo: MakeRepo) -> None:
        text = '[project]\nname = "pkg"\n[tool.hatch.build.targets.wheel]\npackages = ["src/pkg"]\n'
        ctx = make_repo({"pyproject.toml": text}, name="pkg", aliases=("pkg",))
        data = aspect({"tool.hatch.build.targets.wheel.packages": "set", "project.name": "exact"}).extract(ctx)
        assert data["values"] == {
            "tool.hatch.build.targets.wheel.packages": ["src/{{name}}"],
            "project.name": "{{name}}",
        }

    def test_substitution_descends_into_nested_tables_and_lists(self, make_repo: MakeRepo) -> None:
        text = '[tool.mypy]\npackages = ["neuroconv"]\n[[tool.mypy.overrides]]\nmodule = "neuroconv.*"\nignore_missing_imports = true\n'
        ctx = make_repo({"pyproject.toml": text}, name="neuroconv")
        data = aspect({"tool.mypy": "exact"}).extract(ctx)
        assert data["values"]["tool.mypy"] == {
            "packages": ["{{name}}"],
            "overrides": [{"module": "{{name}}.*", "ignore_missing_imports": True}],
        }

    def test_absent_file(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": "no pyproject here"})
        snapshot = aspect(DEFAULT_KEYS).run_extract(ctx)
        assert snapshot.data == {"present": False, "values": {}}
        assert snapshot.sources == ()

    def test_other_file_name(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"ruff.toml": "line-length = 100\n"}, substitute=False)
        data = aspect({"line-length": "exact"}, file="ruff.toml").extract(ctx)
        assert data == {"present": True, "values": {"line-length": 100}}

    def test_absent_keys_are_none(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"pyproject.toml": "[project]\nname = 'x'\n"}, substitute=False)
        data = aspect({"project.name": "exact", "project.urls": "keys", "tool.ruff.line-length": "present"}).extract(
            ctx
        )
        assert data["values"] == {"project.name": "x", "project.urls": None, "tool.ruff.line-length": None}

    def test_invalid_toml_raises_unparseable_file(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"pyproject.toml": "[project\nname = 1\n"})
        with pytest.raises(UnparseableFile) as info:
            aspect(DEFAULT_KEYS).extract(ctx)
        assert info.value.rel == "pyproject.toml"
        assert info.value.reason

    def test_values_are_json_serialisable(self, make_repo: MakeRepo) -> None:
        text = (
            "[tool.release]\nstamp = 2024-01-02T03:04:05Z\nday = 2024-01-02\nat = 03:04:05\nratio = 0.5\nflag = false\n"
        )
        ctx = make_repo({"pyproject.toml": text}, substitute=False)
        data = aspect({"tool.release": "exact"}).extract(ctx)
        assert data["values"]["tool.release"] == {
            "stamp": "2024-01-02T03:04:05+00:00",
            "day": "2024-01-02",
            "at": "03:04:05",
            "ratio": 0.5,
            "flag": False,
        }
        json.dumps(data)


# ----- comparison --------------------------------------------------------------------------------------------------


class TestFileGate:
    def test_missing_in_satellite_is_an_error_and_stops(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"tool.ruff": {"line-length": 120}})
        other = snap(make_snapshot, "sat", present=False)
        findings = aspect({"tool.ruff": "exact"}).compare(main, other)
        assert tuples(findings) == {("missing", "file", "downstream", "pyproject.toml")}
        assert findings[0].severity is Severity.ERROR
        assert findings[0].option is None

    def test_absent_everywhere_is_silent(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", present=False)
        other = snap(make_snapshot, "sat", present=False)
        assert aspect({"tool.ruff": "exact"}).compare(main, other) == []

    def test_required_reports_even_when_main_lacks_it(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", present=False)
        other = snap(make_snapshot, "sat", present=False)
        findings = aspect({"tool.ruff": "exact"}, required=True).compare(main, other)
        assert tuples(findings) == {("missing", "file", "downstream", "pyproject.toml")}
        assert findings[0].option == "aspects.pyproject.required=True"

    def test_absent_in_main_only_is_silent(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", present=False)
        other = snap(make_snapshot, "sat", {"tool.ruff": {"line-length": 120}})
        assert aspect({"tool.ruff": "exact"}).compare(main, other) == []

    def test_user_severity_override(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {})
        other = snap(make_snapshot, "sat", present=False)
        findings = aspect(severity={"missing.file": "warning"}).compare(main, other)
        assert findings[0].severity is Severity.WARNING


class TestKeyGate:
    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_key_absent_in_main_is_extra_when_satellite_has_it(self, make_snapshot: MakeSnapshot, mode: str) -> None:
        main = snap(make_snapshot, "main", {"tool.mypy": None})
        other = snap(make_snapshot, "sat", {"tool.mypy": {"strict": True}})
        findings = aspect({"tool.mypy": mode}).compare(main, other)
        assert tuples(findings) == {("extra", "key", "upstream", "pyproject.toml:tool.mypy")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].content_key == "tool.mypy"

    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_key_absent_on_both_sides_is_silent(self, make_snapshot: MakeSnapshot, mode: str) -> None:
        main = snap(make_snapshot, "main", {"tool.mypy": None})
        other = snap(make_snapshot, "sat", {})
        assert aspect({"tool.mypy": mode}).compare(main, other) == []

    @pytest.mark.parametrize("mode", sorted(MODES))
    def test_key_absent_in_satellite_is_missing(self, make_snapshot: MakeSnapshot, mode: str) -> None:
        main = snap(make_snapshot, "main", {"project.urls": {"Homepage": "x"}})
        other = snap(make_snapshot, "sat", {"project.urls": None})
        findings = aspect({"project.urls": mode}).compare(main, other)
        assert tuples(findings) == {("missing", "key", "downstream", "pyproject.toml:project.urls")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].content_key == "project.urls"

    def test_only_configured_keys_are_compared(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"tool.ruff": {"a": 1}, "tool.mypy": {"strict": True}})
        other = snap(make_snapshot, "sat", {"tool.ruff": {"a": 1}, "tool.mypy": None})
        assert aspect({"tool.ruff": "exact"}).compare(main, other) == []


class TestExact:
    def test_leaf_locators(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot, "main", {"tool.ruff": {"line-length": 120, "lint": {"select": ["E"]}, "extend": "x"}}
        )
        other = snap(
            make_snapshot, "sat", {"tool.ruff": {"line-length": 100, "lint": {"select": ["E"], "ignore": ["E501"]}}}
        )
        findings = aspect({"tool.ruff": "exact"}).compare(main, other)
        assert tuples(findings) == {
            ("differs", "value", "downstream", "pyproject.toml:tool.ruff.line-length"),
            ("missing", "key", "downstream", "pyproject.toml:tool.ruff.extend"),
            ("extra", "key", "upstream", "pyproject.toml:tool.ruff.lint.ignore"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["differs"].severity is Severity.WARNING
        assert by_kind["differs"].content_key == "tool.ruff.line-length"
        assert by_kind["missing"].severity is Severity.WARNING
        assert by_kind["extra"].severity is Severity.INFO

    def test_scalar_and_list_leaves(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.requires-python": ">=3.10", "build-system": {"requires": ["a"]}})
        other = snap(make_snapshot, "sat", {"project.requires-python": ">=3.11", "build-system": {"requires": ["b"]}})
        findings = aspect({"project.requires-python": "exact", "build-system": "exact"}).compare(main, other)
        assert tuples(findings) == {
            ("differs", "value", "downstream", "pyproject.toml:project.requires-python"),
            ("differs", "value", "downstream", "pyproject.toml:build-system.requires"),
        }

    def test_identical_is_silent(self, make_snapshot: MakeSnapshot) -> None:
        values = {"tool.ruff": {"line-length": 120, "lint": {"select": ["E"]}}, "project.requires-python": ">=3.10"}
        main = snap(make_snapshot, "main", values)
        other = snap(make_snapshot, "sat", dict(values))
        assert aspect({"tool.ruff": "exact", "project.requires-python": "exact"}).compare(main, other) == []


class TestSet:
    def test_order_and_duplicates_are_ignored(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.classifiers": ["A", "B"]})
        other = snap(make_snapshot, "sat", {"project.classifiers": ["B", "A", "B"]})
        assert aspect({"project.classifiers": "set"}).compare(main, other) == []

    def test_missing_and_extra_elements(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.classifiers": ["A", "B"]})
        other = snap(make_snapshot, "sat", {"project.classifiers": ["B", "C"]})
        findings = aspect({"project.classifiers": "set"}).compare(main, other)
        assert tuples(findings) == {
            ("missing", "value", "downstream", "pyproject.toml:project.classifiers"),
            ("extra", "value", "upstream", "pyproject.toml:project.classifiers"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["missing"].content_key == "A"
        assert by_kind["missing"].severity is Severity.WARNING
        assert by_kind["extra"].content_key == "C"
        assert by_kind["extra"].severity is Severity.INFO
        assert len({f.id for f in findings}) == 2, "content_key keeps ids distinct at one locator"

    def test_non_string_elements(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.authors": [{"name": "Ada"}, {"name": "Bob"}]})
        other = snap(make_snapshot, "sat", {"project.authors": [{"name": "Bob"}]})
        findings = aspect({"project.authors": "set"}).compare(main, other)
        assert tuples(findings) == {("missing", "value", "downstream", "pyproject.toml:project.authors")}
        assert findings[0].content_key == '{"name":"Ada"}'

    def test_shape_mismatch_falls_back_to_exact(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.classifiers": ["A"]})
        other = snap(make_snapshot, "sat", {"project.classifiers": "A"})
        findings = aspect({"project.classifiers": "set"}).compare(main, other)
        assert tuples(findings) == {("differs", "value", "downstream", "pyproject.toml:project.classifiers")}


class TestKeys:
    def test_only_key_sets_are_compared(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.urls": {"Homepage": "https://a", "Issues": "https://a/issues"}})
        other = snap(make_snapshot, "sat", {"project.urls": {"Homepage": "https://b", "Docs": "https://b/docs"}})
        findings = aspect({"project.urls": "keys"}).compare(main, other)
        assert tuples(findings) == {
            ("missing", "key", "downstream", "pyproject.toml:project.urls.Issues"),
            ("extra", "key", "upstream", "pyproject.toml:project.urls.Docs"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["missing"].content_key == "project.urls.Issues"
        assert by_kind["extra"].content_key == "project.urls.Docs"

    def test_same_keys_different_values_is_silent(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.urls": {"Homepage": "https://a"}})
        other = snap(make_snapshot, "sat", {"project.urls": {"Homepage": "https://b"}})
        assert aspect({"project.urls": "keys"}).compare(main, other) == []

    def test_shape_mismatch_falls_back_to_exact(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"project.urls": {"Homepage": "https://a"}})
        other = snap(make_snapshot, "sat", {"project.urls": ["https://a"]})
        findings = aspect({"project.urls": "keys"}).compare(main, other)
        assert tuples(findings) == {("differs", "value", "downstream", "pyproject.toml:project.urls")}


class TestRequirements:
    KEY = "project.optional-dependencies.test"
    LOC = "pyproject.toml:project.optional-dependencies.test"

    def test_names_specifiers_and_extras(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.KEY: ["pytest>=7", "Pytest_Cov", "numpy"]})
        other = snap(make_snapshot, "sat", {self.KEY: ["pytest", "pytest-cov>=4", "scipy"]})
        findings = aspect({self.KEY: "requirements"}).compare(main, other)
        assert tuples(findings) == {
            ("missing", "value", "downstream", self.LOC),
            ("extra", "value", "upstream", self.LOC),
            ("differs", "value", "downstream", self.LOC),
        }
        assert len(findings) == 4
        by_kind: dict[str, list[Finding]] = {}
        for f in findings:
            by_kind.setdefault(f.kind.value, []).append(f)
        assert [f.content_key for f in by_kind["missing"]] == ["numpy"]
        assert by_kind["missing"][0].severity is Severity.WARNING
        assert [f.content_key for f in by_kind["extra"]] == ["scipy"]
        assert by_kind["extra"][0].severity is Severity.INFO
        assert {f.content_key for f in by_kind["differs"]} == {"pytest", "pytest-cov"}
        assert all(f.severity is Severity.INFO for f in by_kind["differs"])
        assert len({f.id for f in findings}) == 4

    def test_whitespace_and_case_are_normalised(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.KEY: ["Pytest >= 7, <9", "pytest-cov[toml]"]})
        other = snap(make_snapshot, "sat", {self.KEY: ["pytest>=7,<9", "Pytest.Cov[toml]"]})
        assert aspect({self.KEY: "requirements"}).compare(main, other) == []

    def test_superset_in_satellite_is_only_upstream(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.KEY: ["pytest"]})
        other = snap(make_snapshot, "sat", {self.KEY: ["pytest", "hypothesis"]})
        findings = aspect({self.KEY: "requirements"}).compare(main, other)
        assert tuples(findings) == {("extra", "value", "upstream", self.LOC)}

    def test_shape_mismatch_falls_back_to_exact(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.KEY: ["pytest"]})
        other = snap(make_snapshot, "sat", {self.KEY: {"pytest": ">=7"}})
        findings = aspect({self.KEY: "requirements"}).compare(main, other)
        assert tuples(findings) == {("differs", "value", "downstream", self.LOC)}


class TestPresent:
    def test_values_are_never_compared(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"tool.codespell": {"skip": "*.svg"}})
        other = snap(make_snapshot, "sat", {"tool.codespell": {"ignore-words-list": "nd"}})
        assert aspect({"tool.codespell": "present"}).compare(main, other) == []


class TestDirections:
    def test_upstream_findings_are_info_and_downstream_gate(self, make_snapshot: MakeSnapshot) -> None:
        keys = {
            "tool.ruff": "exact",
            "project.classifiers": "set",
            "project.urls": "keys",
            "project.optional-dependencies.test": "requirements",
            "tool.mypy": "present",
        }
        main = snap(
            make_snapshot,
            "main",
            {
                "tool.ruff": {"line-length": 120},
                "project.classifiers": ["A"],
                "project.urls": {"Homepage": "x"},
                "project.optional-dependencies.test": ["pytest>=7"],
                "tool.mypy": None,
            },
        )
        other = snap(
            make_snapshot,
            "sat",
            {
                "tool.ruff": {"line-length": 120, "target-version": "py311"},
                "project.classifiers": ["A", "B"],
                "project.urls": {"Homepage": "y", "Docs": "z"},
                "project.optional-dependencies.test": ["pytest>=7", "coverage"],
                "tool.mypy": {"strict": True},
            },
        )
        findings = aspect(keys, severity={"extra": "error"}).compare(main, other)
        assert {f.kind.value for f in findings} == {"extra"}
        assert len(findings) == 5
        assert all(f.direction is Direction.UPSTREAM and f.severity is Severity.INFO for f in findings)
        assert not any(f.gates for f in findings)

    def test_every_finding_carries_the_aspect_and_repo(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"tool.ruff": {"a": 1}})
        other = snap(make_snapshot, "roiextractors", {"tool.ruff": {"a": 2}})
        findings = aspect({"tool.ruff": "exact"}).compare(main, other)
        assert [(f.aspect, f.repo) for f in findings] == [("pyproject", "roiextractors")]


class TestRoundTrip:
    def test_templated_pyprojects_of_two_packages_agree(self, make_repo: MakeRepo) -> None:
        template = PYPROJECT
        main_ctx = make_repo(
            {"pyproject.toml": template}, name="neuroconv", url="https://github.com/catalystneuro/neuroconv"
        )
        other_text = template.replace("neuroconv", "roiextractors")
        other_ctx = make_repo(
            {"pyproject.toml": other_text}, name="roiextractors", url="https://github.com/catalystneuro/roiextractors"
        )
        a = aspect(DEFAULT_KEYS)
        main = a.run_extract(main_ctx)
        other = a.run_extract(other_ctx)
        assert main.data == other.data
        assert a.compare(main, other) == []

    def test_drift_is_found_end_to_end(self, make_repo: MakeRepo) -> None:
        identity = Identity(name="x", aliases=())
        main_ctx = make_repo({"pyproject.toml": PYPROJECT}, name="main-repo", identity=identity)
        drifted = PYPROJECT.replace("line-length = 120", "line-length = 100").replace(
            '"pytest-cov"', '"pytest-cov>=4", "hypothesis"'
        )
        other_ctx = make_repo({"pyproject.toml": drifted}, name="sat-repo", identity=identity)
        a = aspect(DEFAULT_KEYS)
        findings = a.compare(a.run_extract(main_ctx), a.run_extract(other_ctx))
        assert tuples(findings) == {
            ("differs", "value", "downstream", "pyproject.toml:tool.ruff.line-length"),
            ("differs", "value", "downstream", "pyproject.toml:project.optional-dependencies.test"),
            ("extra", "value", "upstream", "pyproject.toml:project.optional-dependencies.test"),
        }
