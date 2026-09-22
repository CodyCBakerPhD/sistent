"""Tests for the ``precommit`` aspect type: option validation, extraction and comparison."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

import pytest

from sistent.aspects.base import UnparseableFile
from sistent.aspects.precommit import PrecommitAspect, PrecommitOptions, repo_slug
from sistent.model import Direction, Finding, Identity, Severity, Snapshot
from sistent.options import BaseOptions, OptionsError, parse_options
from tests.conftest import MakeRepo

MakeSnapshot = Callable[..., Snapshot]
Tuple = tuple[str, str, str, str]
FILE = ".pre-commit-config.yaml"

CONFIG = """\
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format
  - repo: git@github.com:pre-commit/pre-commit-hooks.git
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
  - repo: https://gitlab.com/someone/tool.git
    rev: 1.0
    hooks:
      - id: tool-check
  - repo: local
    hooks:
      - id: mypy
        name: mypy
        entry: mypy
        language: system
  - repo: meta
    hooks:
      - id: check-hooks-apply
  - repo: https://github.com/codespell-project/codespell
    rev: v2.3.0
"""


def tuples(findings: Iterable[Finding]) -> set[Tuple]:
    return {(f.kind.value, f.subject.value, f.direction.value, f.locator) for f in findings}


def aspect(**options: Any) -> PrecommitAspect:
    return PrecommitAspect("pre_commit", PrecommitOptions(**options))


def entry(rev: str | None = None, hooks: Iterable[str] = ()) -> dict[str, Any]:
    return {"rev": rev, "hooks": list(hooks)}


def snap(
    make_snapshot: MakeSnapshot, repo: str, repos: dict[str, Any] | None = None, *, present: bool = True
) -> Snapshot:
    return make_snapshot("pre_commit", repo, {"present": present, "repos": dict(repos or {})})


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_defaults(self) -> None:
        options = PrecommitOptions()
        assert options.file == FILE
        assert options.compare_rev is True

    def test_default_config_table_parses(self) -> None:
        options = parse_options(PrecommitOptions, {"file": ".pre-commit-config.yaml"}, where="aspects.pre_commit")
        assert options.file == FILE

    def test_wrong_types_and_unknown_keys_are_rejected(self) -> None:
        with pytest.raises(OptionsError, match="expected bool"):
            parse_options(PrecommitOptions, {"compare_rev": "no"}, where="aspects.pre_commit")
        with pytest.raises(OptionsError, match="expected str"):
            parse_options(PrecommitOptions, {"file": ["a"]}, where="aspects.pre_commit")
        with pytest.raises(OptionsError, match=r"compare_revs: unknown option \(did you mean 'compare_rev'\?\)"):
            parse_options(PrecommitOptions, {"compare_revs": False}, where="aspects.pre_commit")

    def test_aspect_requires_its_options_class(self) -> None:
        with pytest.raises(TypeError, match="PrecommitOptions"):
            PrecommitAspect("pre_commit", BaseOptions())

    def test_class_attributes(self) -> None:
        a = aspect()
        assert a.type_name == "precommit"
        assert a.options_cls is PrecommitOptions
        assert a.default_severity == {"missing.file": "warning"}
        assert a.description


# ----- helpers -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/astral-sh/ruff-pre-commit", "astral-sh/ruff-pre-commit"),
        ("https://github.com/astral-sh/ruff-pre-commit.git", "astral-sh/ruff-pre-commit"),
        ("https://github.com/astral-sh/ruff-pre-commit/", "astral-sh/ruff-pre-commit"),
        ("http://www.github.com/a/b", "a/b"),
        ("git@github.com:a/b.git", "a/b"),
        ("ssh://git@github.com/a/b", "a/b"),
        ("git://github.com/a/b", "a/b"),
        ("https://GitHub.com/A/B", "A/B"),
        ("https://gitlab.com/a/b.git", "https://gitlab.com/a/b"),
        ("https://gitlab.com/a/b/", "https://gitlab.com/a/b"),
        ("https://github.com/a/b/c", "https://github.com/a/b/c"),
        ("local", "local"),
        ("meta", "meta"),
        ("  local ", "local"),
    ],
)
def test_repo_slug(url: str, expected: str) -> None:
    assert repo_slug(url) == expected


# ----- extraction --------------------------------------------------------------------------------------------------


class TestExtract:
    def test_realistic_config(self, make_repo: MakeRepo) -> None:
        snapshot = aspect().run_extract(make_repo({FILE: CONFIG}, substitute=False))
        assert snapshot.data == {
            "present": True,
            "repos": {
                "astral-sh/ruff-pre-commit": {"rev": "v0.6.9", "hooks": ["ruff", "ruff-format"]},
                "pre-commit/pre-commit-hooks": {"rev": "v4.6.0", "hooks": ["trailing-whitespace", "end-of-file-fixer"]},
                "https://gitlab.com/someone/tool": {"rev": "1.0", "hooks": ["tool-check"]},
                "local": {"rev": None, "hooks": ["mypy"]},
                "meta": {"rev": None, "hooks": ["check-hooks-apply"]},
                "codespell-project/codespell": {"rev": "v2.3.0", "hooks": []},
            },
        }
        assert snapshot.sources == (FILE,)

    def test_duplicate_repositories_merge_hooks_and_keep_the_first_rev(self, make_repo: MakeRepo) -> None:
        text = (
            "repos:\n"
            "  - repo: https://github.com/psf/black\n    rev: 24.1.0\n    hooks:\n      - id: black\n"
            "  - repo: https://github.com/psf/black.git\n    rev: 23.1.0\n    hooks:\n      - id: black-jupyter\n      - id: black\n"
        )
        payload = aspect().extract(make_repo({FILE: text}, substitute=False))
        assert payload["repos"] == {"psf/black": {"rev": "24.1.0", "hooks": ["black", "black-jupyter"]}}

    def test_identity_is_substituted(self, make_repo: MakeRepo) -> None:
        text = "repos:\n  - repo: https://github.com/catalystneuro/neuroconv-hooks\n    rev: v1\n    hooks:\n      - id: neuroconv-style\n"
        identity = Identity(name="neuroconv", aliases=("neuroconv",), org="catalystneuro")
        payload = aspect().extract(make_repo({FILE: text}, identity=identity))
        assert payload["repos"] == {"{{org}}/{{name}}-hooks": {"rev": "v1", "hooks": ["{{name}}-style"]}}

    def test_absent_file(self, make_repo: MakeRepo) -> None:
        snapshot = aspect().run_extract(make_repo({"README.md": "x"}))
        assert snapshot.data == {"present": False, "repos": {}}
        assert snapshot.sources == ()

    def test_custom_file_name(self, make_repo: MakeRepo) -> None:
        payload = aspect(file="ci/pre-commit.yaml").extract(make_repo({"ci/pre-commit.yaml": "repos: []\n"}))
        assert payload == {"present": True, "repos": {}}

    def test_empty_document(self, make_repo: MakeRepo) -> None:
        assert aspect().extract(make_repo({FILE: "# nothing yet\n"})) == {"present": True, "repos": {}}

    def test_invalid_yaml_raises_unparseable_file(self, make_repo: MakeRepo) -> None:
        with pytest.raises(UnparseableFile) as info:
            aspect().extract(make_repo({FILE: "repos: [\n"}))
        assert info.value.rel == FILE
        assert info.value.reason

    def test_non_mapping_document_raises_unparseable_file(self, make_repo: MakeRepo) -> None:
        with pytest.raises(UnparseableFile, match="not a mapping"):
            aspect().extract(make_repo({FILE: "- repo: local\n"}))

    def test_odd_shapes_do_not_raise(self, make_repo: MakeRepo) -> None:
        text = (
            "repos:\n"
            "  - not a mapping\n"
            "  - repo: 42\n"
            "  - repo: local\n    hooks: nope\n"
            "  - repo: https://github.com/a/b\n    rev: 2.0\n    hooks:\n      - id: 7\n      - name: no id\n      - plain\n"
        )
        payload = aspect().extract(make_repo({FILE: text}, substitute=False))
        assert payload["repos"] == {"local": {"rev": None, "hooks": []}, "a/b": {"rev": "2.0", "hooks": ["7"]}}


# ----- comparison --------------------------------------------------------------------------------------------------


class TestFileGate:
    def test_missing_file_is_a_warning(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"astral-sh/ruff-pre-commit": entry("v1", ["ruff"])})
        other = snap(make_snapshot, "sat", present=False)
        findings = aspect().compare(main, other)
        assert tuples(findings) == {("missing", "file", "downstream", FILE)}
        assert findings[0].severity is Severity.WARNING

    def test_user_severity_beats_type_default(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {})
        other = snap(make_snapshot, "sat", present=False)
        assert aspect(severity={"missing.file": "error"}).compare(main, other)[0].severity is Severity.ERROR

    def test_absent_everywhere_or_only_in_main_is_silent(self, make_snapshot: MakeSnapshot) -> None:
        absent_main = snap(make_snapshot, "main", present=False)
        assert aspect().compare(absent_main, snap(make_snapshot, "sat", present=False)) == []
        assert aspect().compare(absent_main, snap(make_snapshot, "sat", {"local": entry(None, ["x"])})) == []

    def test_custom_file_locator(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {})
        other = snap(make_snapshot, "sat", present=False)
        assert tuples(aspect(file="ci/pre-commit.yaml").compare(main, other)) == {
            ("missing", "file", "downstream", "ci/pre-commit.yaml")
        }


class TestRepositories:
    def test_missing_and_extra_repositories(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            {"astral-sh/ruff-pre-commit": entry("v1", ["ruff"]), "psf/black": entry("24.1.0", ["black"])},
        )
        other = snap(
            make_snapshot, "sat", {"astral-sh/ruff-pre-commit": entry("v1", ["ruff"]), "local": entry(None, ["mypy"])}
        )
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "key", "downstream", f"{FILE}:psf/black"),
            ("extra", "key", "upstream", f"{FILE}:local"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["missing"].severity is Severity.WARNING
        assert by_kind["missing"].content_key == "psf/black"
        assert by_kind["extra"].severity is Severity.INFO
        assert by_kind["extra"].content_key == "local"

    def test_unmatched_repositories_are_not_descended(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"psf/black": entry("24.1.0", ["black", "black-jupyter"])})
        other = snap(make_snapshot, "sat", {"psf/flake8": entry("7.0.0", ["flake8"])})
        assert tuples(aspect().compare(main, other)) == {
            ("missing", "key", "downstream", f"{FILE}:psf/black"),
            ("extra", "key", "upstream", f"{FILE}:psf/flake8"),
        }


class TestHooks:
    def test_hook_sets_per_shared_repository(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"astral-sh/ruff-pre-commit": entry("v1", ["ruff", "ruff-format"])})
        other = snap(make_snapshot, "sat", {"astral-sh/ruff-pre-commit": entry("v1", ["ruff", "ruff-check"])})
        findings = aspect().compare(main, other)
        assert tuples(findings) == {
            ("missing", "hook", "downstream", f"{FILE}:astral-sh/ruff-pre-commit:ruff-format"),
            ("extra", "hook", "upstream", f"{FILE}:astral-sh/ruff-pre-commit:ruff-check"),
        }
        by_kind = {f.kind.value: f for f in findings}
        assert by_kind["missing"].severity is Severity.WARNING
        assert by_kind["missing"].content_key == "ruff-format"
        assert by_kind["extra"].content_key == "ruff-check"

    def test_hook_order_is_irrelevant(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"local": entry(None, ["a", "b"])})
        other = snap(make_snapshot, "sat", {"local": entry(None, ["b", "a"])})
        assert aspect().compare(main, other) == []


class TestRevisions:
    REPO = "astral-sh/ruff-pre-commit"

    def test_rev_drift_is_a_downstream_warning(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.REPO: entry("v0.6.9", ["ruff"])})
        other = snap(make_snapshot, "sat", {self.REPO: entry("v0.5.0", ["ruff"])})
        findings = aspect().compare(main, other)
        assert tuples(findings) == {("differs", "value", "downstream", f"{FILE}:{self.REPO}")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].content_key == "rev"
        assert findings[0].option == "aspects.pre_commit.compare_rev=True"

    def test_compare_rev_off(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.REPO: entry("v0.6.9", ["ruff"])})
        other = snap(make_snapshot, "sat", {self.REPO: entry("v0.5.0", ["ruff"])})
        assert aspect(compare_rev=False).compare(main, other) == []

    def test_equal_or_absent_revs_are_silent(self, make_snapshot: MakeSnapshot) -> None:
        a = aspect()
        assert (
            a.compare(
                snap(make_snapshot, "main", {self.REPO: entry("v1", ["ruff"])}),
                snap(make_snapshot, "sat", {self.REPO: entry("v1", ["ruff"])}),
            )
            == []
        )
        assert (
            a.compare(
                snap(make_snapshot, "main", {"local": entry(None, ["x"])}),
                snap(make_snapshot, "sat", {"local": entry(None, ["x"])}),
            )
            == []
        )
        assert (
            a.compare(
                snap(make_snapshot, "main", {self.REPO: entry("v1", ["ruff"])}),
                snap(make_snapshot, "sat", {self.REPO: entry(None, ["ruff"])}),
            )
            == []
        )
        assert (
            a.compare(
                snap(make_snapshot, "main", {self.REPO: entry(None, ["ruff"])}),
                snap(make_snapshot, "sat", {self.REPO: entry("v1", ["ruff"])}),
            )
            == []
        )

    def test_rev_and_hooks_are_reported_together(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {self.REPO: entry("v0.6.9", ["ruff", "ruff-format"])})
        other = snap(make_snapshot, "sat", {self.REPO: entry("v0.5.0", ["ruff"])})
        assert tuples(aspect().compare(main, other)) == {
            ("differs", "value", "downstream", f"{FILE}:{self.REPO}"),
            ("missing", "hook", "downstream", f"{FILE}:{self.REPO}:ruff-format"),
        }


class TestDirections:
    def test_upstream_findings_are_info_and_never_gate(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", {"astral-sh/ruff-pre-commit": entry("v1", ["ruff"])})
        other = snap(
            make_snapshot,
            "sat",
            {"astral-sh/ruff-pre-commit": entry("v1", ["ruff", "ruff-format"]), "local": entry(None, ["mypy"])},
        )
        findings = aspect(severity={"extra": "error"}).compare(main, other)
        assert len(findings) == 2
        assert {f.kind.value for f in findings} == {"extra"}
        assert all(f.direction is Direction.UPSTREAM and f.severity is Severity.INFO and not f.gates for f in findings)
        assert all((f.aspect, f.repo) == ("pre_commit", "sat") for f in findings)


class TestRoundTrip:
    def test_identical_configs_agree(self, make_repo: MakeRepo) -> None:
        a = aspect()
        main = a.run_extract(make_repo({FILE: CONFIG}, name="main-repo"))
        other = a.run_extract(make_repo({FILE: CONFIG}, name="sat-repo"))
        assert main.data == other.data
        assert a.compare(main, other) == []

    def test_drift_is_found_end_to_end(self, make_repo: MakeRepo) -> None:
        a = aspect()
        main = a.run_extract(make_repo({FILE: CONFIG}, name="main-repo"))
        drifted = (
            CONFIG.replace("rev: v0.6.9", "rev: v0.5.0")
            .replace("      - id: ruff-format\n", "")
            .replace("  - repo: meta\n    hooks:\n      - id: check-hooks-apply\n", "")
        )
        other = a.run_extract(make_repo({FILE: drifted}, name="sat-repo"))
        assert tuples(a.compare(main, other)) == {
            ("differs", "value", "downstream", f"{FILE}:astral-sh/ruff-pre-commit"),
            ("missing", "hook", "downstream", f"{FILE}:astral-sh/ruff-pre-commit:ruff-format"),
            ("missing", "key", "downstream", f"{FILE}:meta"),
        }
