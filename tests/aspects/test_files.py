"""Tests for the ``files`` aspect: any-of globs across search dirs, identical normalisation and comparison."""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterable
from typing import Any

import pytest

from sistent.aspects import DEFAULT_CONFIG
from sistent.aspects.files import FilesAspect, FilesOptions, alternatives
from sistent.model import Direction, Finding, Kind, Severity, Subject
from sistent.options import OptionsError, parse_options
from tests.conftest import MakeRepo

PYPROJECT = '[project]\nname = "mypkg"\n'

LICENSE = "LICENSE*|COPYING*|license.*"
CONTRIBUTING = "CONTRIBUTING.*"
COC = "CODE_OF_CONDUCT.*"
SECURITY = "SECURITY.*"
PRECOMMIT = ".pre-commit-config.yaml"
GITIGNORE = ".gitignore"
CODESPELL = ".codespellrc|.codespell*"
CITATION = "CITATION.cff"


def community_table() -> dict[str, Any]:
    """The ``[aspects.community_files]`` default table without its ``type`` key."""
    table = dict(tomllib.loads(DEFAULT_CONFIG)["aspects"]["community_files"])
    table.pop("type")
    return table


def community(**overrides: Any) -> FilesAspect:
    """A ``files`` aspect with the DEFAULT_CONFIG ``community_files`` options, some of them overridden."""
    table = {**community_table(), **overrides}
    return FilesAspect("community_files", parse_options(FilesOptions, table, where="aspects.community_files"))


def tuples(findings: Iterable[Finding]) -> set[tuple[Kind, Subject, Direction, str]]:
    return {(f.kind, f.subject, f.direction, f.locator) for f in findings}


def entry(paths: list[str], alternative: str | None = None) -> dict[str, Any]:
    if alternative is None and paths:
        alternative = paths[0].rsplit("/", 1)[-1]
    return {"paths": paths, "alternative": alternative}


def absent() -> dict[str, Any]:
    return {"paths": [], "alternative": None}


def identical(path: str, text: str) -> dict[str, Any]:
    return {"path": path, "normalized": text, "hash": hashlib.sha1(text.encode()).hexdigest()}


def snap(
    make_snapshot: Any,
    repo: str,
    entries: dict[str, Any],
    ident: dict[str, Any] | None = None,
    *,
    is_main: bool = False,
) -> Any:
    return make_snapshot("community_files", repo, {"is_main": is_main, "entries": entries, "identical": ident or {}})


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_default_config_table_parses_with_the_documented_names(self) -> None:
        options = parse_options(FilesOptions, community_table(), where="aspects.community_files")
        assert options.files[0] == LICENSE
        assert options.required == "from-main"
        assert options.identical == [COC]
        assert options.search_dirs == [".", ".github", "docs"]
        assert len(options.ignore_patterns) == 1

    def test_files_is_required(self) -> None:
        with pytest.raises(OptionsError, match=r"missing required option.*files"):
            parse_options(FilesOptions, {}, where="aspects.x")

    def test_defaults(self) -> None:
        options = FilesOptions(files=["LICENSE*"])
        assert options.required == "from-main"
        assert options.identical == []
        assert options.search_dirs == [".", ".github", "docs"]
        assert options.ignore_patterns == []

    def test_invalid_required_mode(self) -> None:
        with pytest.raises(OptionsError, match="required: expected one of 'from-main', 'all'"):
            FilesOptions(files=["LICENSE*"], required="sometimes")

    def test_identical_must_be_a_files_entry(self) -> None:
        with pytest.raises(OptionsError, match=r"identical: 'CODE_OF_CONDUCT\.\*' not listed in files"):
            FilesOptions(files=["LICENSE*"], identical=["CODE_OF_CONDUCT.*"])

    def test_ignore_patterns_must_compile(self) -> None:
        with pytest.raises(OptionsError, match="ignore_patterns: invalid regular expression"):
            FilesOptions(files=["LICENSE*"], ignore_patterns=["(unclosed"])

    @pytest.mark.parametrize("files", [[], ["|"], [" "]])
    def test_entries_must_contain_a_glob(self, files: list[str]) -> None:
        with pytest.raises(OptionsError, match="files:"):
            FilesOptions(files=files)

    def test_through_parse_options(self) -> None:
        with pytest.raises(OptionsError, match="required:"):
            parse_options(FilesOptions, {"files": ["LICENSE*"], "required": "never"}, where="aspects.x")

    def test_alternatives(self) -> None:
        assert alternatives(LICENSE) == ["LICENSE*", "COPYING*", "license.*"]
        assert alternatives(" a | b ") == ["a", "b"]
        assert alternatives("|") == []

    def test_aspect_metadata(self) -> None:
        assert FilesAspect.type_name == "files"
        assert FilesAspect.options_cls is FilesOptions
        assert FilesAspect.description
        assert community().name == "community_files"


# ----- extraction --------------------------------------------------------------------------------------------------


class TestExtract:
    def test_alternatives_across_search_dirs(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {
                "license.txt": "MIT",
                ".github/CODE_OF_CONDUCT.md": "Be nice.\n",
                "docs/CONTRIBUTING.rst": "",
                ".codespellrc": "",
                ".gitignore": "",
                "src/SECURITY.md": "",
            }
        )
        data = community().extract(ctx)
        assert data["is_main"] is False
        assert data["entries"] == {
            LICENSE: {"paths": ["license.txt"], "alternative": "LICENSE*"},
            CONTRIBUTING: {"paths": ["docs/CONTRIBUTING.rst"], "alternative": "CONTRIBUTING.*"},
            COC: {"paths": [".github/CODE_OF_CONDUCT.md"], "alternative": "CODE_OF_CONDUCT.*"},
            SECURITY: {"paths": [], "alternative": None},
            PRECOMMIT: {"paths": [], "alternative": None},
            GITIGNORE: {"paths": [".gitignore"], "alternative": ".gitignore"},
            CODESPELL: {"paths": [".codespellrc"], "alternative": ".codespellrc"},
            CITATION: {"paths": [], "alternative": None},
        }
        assert data["identical"] == {
            COC: {
                "path": ".github/CODE_OF_CONDUCT.md",
                "normalized": "Be nice.",
                "hash": hashlib.sha1(b"Be nice.").hexdigest(),
            }
        }

    def test_first_alternative_with_matches_wins(self, make_repo: MakeRepo) -> None:
        both = make_repo({"LICENSE.txt": "", "COPYING": ""})
        assert community().extract(both)["entries"][LICENSE] == {"paths": ["LICENSE.txt"], "alternative": "LICENSE*"}
        copying = make_repo({"COPYING": "", "COPYING.LESSER": ""})
        assert community().extract(copying)["entries"][LICENSE] == {
            "paths": ["COPYING", "COPYING.LESSER"],
            "alternative": "COPYING*",
        }
        lowercase = make_repo({"license.md": ""})
        assert community().extract(lowercase)["entries"][LICENSE] == {
            "paths": ["license.md"],
            "alternative": "LICENSE*",
        }

    def test_all_matches_of_the_winning_alternative_are_listed(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"CONTRIBUTING.md": "", ".github/CONTRIBUTING.md": "", "docs/contributing.rst": ""})
        assert community().extract(ctx)["entries"][CONTRIBUTING]["paths"] == [
            ".github/CONTRIBUTING.md",
            "CONTRIBUTING.md",
            "docs/contributing.rst",
        ]

    def test_search_dirs_option(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"docs/CONTRIBUTING.md": "", "legal/LICENSE": ""})
        data = community(search_dirs=["legal"]).extract(ctx)["entries"]
        assert data[LICENSE]["paths"] == ["legal/LICENSE"]
        assert data[CONTRIBUTING]["paths"] == []

    def test_directories_never_match(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"LICENSES/MIT.txt": "", "SECURITY.md/": ""})
        data = community().extract(ctx)["entries"]
        assert data[LICENSE] == {"paths": [], "alternative": None}
        assert data[SECURITY] == {"paths": [], "alternative": None}

    def test_identical_normalisation_removes_years_and_names(self, make_repo: MakeRepo) -> None:
        text = "# Code of Conduct\n\nCopyright 2019-2024   The mypkg team\n\n\n  Contact   mypkg@example.org.\n\nBe nice.\n"
        ctx = make_repo({"pyproject.toml": PYPROJECT, "CODE_OF_CONDUCT.md": text}, name="main", is_main=True)
        result = community().run_extract(ctx)
        info = result.data["identical"][COC]
        assert info["path"] == "CODE_OF_CONDUCT.md"
        assert (
            info["normalized"]
            == "# Code of Conduct\nCopyright The {{name}} team\nContact {{name}}@example.org.\nBe nice."
        )
        assert info["hash"] == hashlib.sha1(info["normalized"].encode()).hexdigest()
        assert result.data["is_main"] is True
        assert result.sources == ("CODE_OF_CONDUCT.md",), "identical files are read through the context"
        assert "mypkg" in result.aliases_applied

    def test_custom_ignore_patterns(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"CODE_OF_CONDUCT.md": "Contact: someone@example.org\nBe nice.\n"})
        aspect = community(ignore_patterns=[r"\S+@\S+"])
        assert aspect.extract(ctx)["identical"][COC]["normalized"] == "Contact:\nBe nice."

    def test_two_repos_differing_only_in_years_and_names_normalise_equally(self, make_repo: MakeRepo) -> None:
        one = make_repo({"pyproject.toml": PYPROJECT, "CODE_OF_CONDUCT.md": "(c) 2020 mypkg\nBe nice.\n"}, name="one")
        two = make_repo(
            {
                "pyproject.toml": '[project]\nname = "otherpkg"\n',
                "CODE_OF_CONDUCT.md": "(c) 2021 - 2024  otherpkg\nBe  nice.\n",
            },
            name="two",
        )
        aspect = community()
        assert aspect.extract(one)["identical"][COC]["hash"] == aspect.extract(two)["identical"][COC]["hash"]

    def test_identical_entry_absent_is_not_recorded(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"LICENSE": ""})
        data = community().extract(ctx)
        assert data["identical"] == {}
        assert data["entries"][COC] == {"paths": [], "alternative": None}

    def test_paths_are_identity_substituted(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"pyproject.toml": PYPROJECT, "docs/mypkg_license.txt": ""})
        data = community(files=["*license*"], identical=[]).extract(ctx)["entries"]
        assert data["*license*"] == {"paths": ["docs/{{name}}_license.txt"], "alternative": "*license*"}


# ----- comparison --------------------------------------------------------------------------------------------------


MAIN_ENTRIES: dict[str, Any] = {
    LICENSE: entry(["LICENSE"], "LICENSE*"),
    CONTRIBUTING: entry(["CONTRIBUTING.md"], "CONTRIBUTING.*"),
    COC: entry([".github/CODE_OF_CONDUCT.md"], "CODE_OF_CONDUCT.*"),
    SECURITY: absent(),
    GITIGNORE: entry([".gitignore"]),
}
MAIN_IDENTICAL: dict[str, Any] = {COC: identical(".github/CODE_OF_CONDUCT.md", "# Code of Conduct\nBe nice.")}


class TestCompare:
    def test_identical_snapshots_yield_nothing(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, MAIN_IDENTICAL, is_main=True)
        other = snap(make_snapshot, "sat", MAIN_ENTRIES, MAIN_IDENTICAL)
        assert community().compare(main, other) == []

    def test_missing_is_an_error_located_at_mains_path(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, MAIN_IDENTICAL, is_main=True)
        other = snap(make_snapshot, "sat", {**MAIN_ENTRIES, LICENSE: absent(), COC: absent()})
        findings = community().compare(main, other)
        assert tuples(findings) == {
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, "LICENSE"),
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md"),
        }
        by_locator = {f.locator: f for f in findings}
        assert by_locator["LICENSE"].severity is Severity.ERROR
        assert by_locator["LICENSE"].message == "LICENSE missing (main: LICENSE)"
        assert by_locator["LICENSE"].content_key == LICENSE
        assert by_locator[".github/CODE_OF_CONDUCT.md"].message == (
            "CODE_OF_CONDUCT.md missing (main: .github/CODE_OF_CONDUCT.md)"
        )
        assert all(f.repo == "sat" and f.aspect == "community_files" for f in findings)

    def test_missing_severity_can_be_lowered(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        other = snap(make_snapshot, "sat", {**MAIN_ENTRIES, LICENSE: absent()})
        findings = community(severity={"missing.file": "warning"}).compare(main, other)
        assert [f.severity for f in findings] == [Severity.WARNING]

    def test_extra_is_an_upstream_info(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        other = snap(make_snapshot, "sat", {**MAIN_ENTRIES, SECURITY: entry(["SECURITY.md"], "SECURITY.*")})
        findings = community().compare(main, other)
        assert tuples(findings) == {(Kind.EXTRA, Subject.FILE, Direction.UPSTREAM, "SECURITY.md")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].message == "SECURITY.md only in repo (SECURITY.md)"
        assert findings[0].content_key == SECURITY

    def test_moved_when_only_the_location_differs(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        other = snap(
            make_snapshot,
            "sat",
            {
                **MAIN_ENTRIES,
                CONTRIBUTING: entry(["docs/CONTRIBUTING.rst"], "CONTRIBUTING.*"),
                COC: entry(["CODE_OF_CONDUCT.md"], "CODE_OF_CONDUCT.*"),
            },
        )
        findings = community().compare(main, other)
        assert tuples(findings) == {
            (Kind.MOVED, Subject.FILE, Direction.DOWNSTREAM, "CONTRIBUTING.md"),
            (Kind.MOVED, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md"),
        }
        moved = {f.locator: f for f in findings}["CONTRIBUTING.md"]
        assert moved.severity is Severity.INFO
        assert moved.message == "CONTRIBUTING.md found at docs/CONTRIBUTING.rst (main: CONTRIBUTING.md)"
        assert moved.detail == "main: CONTRIBUTING.md ; repo: docs/CONTRIBUTING.rst"
        assert moved.detail_kind == "text"
        assert moved.content_key == CONTRIBUTING

    def test_one_shared_path_is_enough(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", {CONTRIBUTING: entry(["CONTRIBUTING.md"])}, is_main=True)
        other = snap(make_snapshot, "sat", {CONTRIBUTING: entry([".github/CONTRIBUTING.md", "CONTRIBUTING.md"])})
        assert community().compare(main, other) == []

    def test_identical_content_differs(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, MAIN_IDENTICAL, is_main=True)
        other = snap(
            make_snapshot,
            "sat",
            MAIN_ENTRIES,
            {COC: identical(".github/CODE_OF_CONDUCT.md", "# Code of Conduct\nBe kind.\nReport abuse.")},
        )
        findings = community().compare(main, other)
        assert tuples(findings) == {(Kind.DIFFERS, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md")}
        f = findings[0]
        assert f.severity is Severity.WARNING
        assert f.message == "CODE_OF_CONDUCT.md differs from main's"
        assert f.detail_kind == "diff"
        assert f.content_key == COC
        assert f.option == "aspects.community_files.identical=['CODE_OF_CONDUCT.*']"
        assert f.detail is not None
        assert f.detail.splitlines()[:2] == [
            "--- main:.github/CODE_OF_CONDUCT.md",
            "+++ sat:.github/CODE_OF_CONDUCT.md",
        ]
        assert "-Be nice." in f.detail.splitlines()
        assert "+Be kind." in f.detail.splitlines()
        assert "+Report abuse." in f.detail.splitlines()

    def test_identical_moved_and_differs_are_both_reported(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, MAIN_IDENTICAL, is_main=True)
        other = snap(
            make_snapshot,
            "sat",
            {**MAIN_ENTRIES, COC: entry(["CODE_OF_CONDUCT.md"], "CODE_OF_CONDUCT.*")},
            {COC: identical("CODE_OF_CONDUCT.md", "# Code of Conduct\nBe kind.")},
        )
        assert tuples(community().compare(main, other)) == {
            (Kind.MOVED, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md"),
            (Kind.DIFFERS, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md"),
        }

    def test_identical_entry_absent_in_other_is_only_missing(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, MAIN_IDENTICAL, is_main=True)
        other = snap(make_snapshot, "sat", {**MAIN_ENTRIES, COC: absent()})
        assert tuples(community().compare(main, other)) == {
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md")
        }

    def test_required_from_main_ignores_entries_main_lacks(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        other = snap(make_snapshot, "sat", MAIN_ENTRIES)
        assert community().compare(main, other) == []

    def test_required_all_reports_entries_absent_everywhere(self, make_snapshot: Any) -> None:
        aspect = community(required="all")
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        other = snap(make_snapshot, "sat", {**MAIN_ENTRIES, LICENSE: absent()})
        findings = aspect.compare(main, other)
        assert tuples(findings) == {
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, "LICENSE"),
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, "SECURITY.*"),
        }
        by_locator = {f.locator: f for f in findings}
        assert by_locator["SECURITY.*"].severity is Severity.ERROR
        assert by_locator["SECURITY.*"].message == "SECURITY.* missing (required by configuration; absent in main too)"
        assert by_locator["SECURITY.*"].content_key == SECURITY
        assert by_locator["SECURITY.*"].option == "aspects.community_files.required=all"
        assert by_locator["LICENSE"].message == "LICENSE missing (main: LICENSE)"

    def test_missing_keys_in_data_are_tolerated(self, make_snapshot: Any) -> None:
        aspect = community()
        assert (
            aspect.compare(make_snapshot("community_files", "main", {}), make_snapshot("community_files", "sat", {}))
            == []
        )
        assert aspect.self_check(make_snapshot("community_files", "main", {})) == []


class TestSelfCheck:
    def test_main_reports_required_entries_it_lacks_once(self, make_snapshot: Any) -> None:
        aspect = community(required="all")
        main = snap(make_snapshot, "main", {**MAIN_ENTRIES, CITATION: absent()}, is_main=True)
        findings = aspect.self_check(main)
        assert tuples(findings) == {
            (Kind.MISSING, Subject.FILE, Direction.NONE, "SECURITY.*"),
            (Kind.MISSING, Subject.FILE, Direction.NONE, "CITATION.cff"),
        }
        assert {f.severity for f in findings} == {Severity.WARNING}
        assert {f.repo for f in findings} == {"main"}
        by_locator = {f.locator: f for f in findings}
        assert by_locator["SECURITY.*"].message == "SECURITY.* missing (required by configuration)"
        assert by_locator["SECURITY.*"].content_key == SECURITY
        assert by_locator["SECURITY.*"].option == "aspects.community_files.required=all"

    def test_satellites_are_covered_by_compare_not_self_check(self, make_snapshot: Any) -> None:
        aspect = community(required="all")
        other = snap(make_snapshot, "sat", MAIN_ENTRIES)
        assert aspect.self_check(other) == []

    def test_from_main_has_no_self_check(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        assert community().self_check(main) == []

    def test_self_check_severity_override(self, make_snapshot: Any) -> None:
        aspect = community(required="all", severity={"missing.file": "error"})
        main = snap(make_snapshot, "main", MAIN_ENTRIES, is_main=True)
        assert [f.severity for f in aspect.self_check(main)] == [Severity.ERROR]


class TestEndToEnd:
    def test_extract_and_compare_two_repositories(self, make_repo: MakeRepo) -> None:
        aspect = community()
        main = aspect.run_extract(
            make_repo(
                {
                    "pyproject.toml": PYPROJECT,
                    "LICENSE": "BSD",
                    "CONTRIBUTING.md": "",
                    ".github/CODE_OF_CONDUCT.md": "Copyright 2020 mypkg\nBe nice.\n",
                    ".gitignore": "",
                },
                name="main",
                is_main=True,
            )
        )
        other = aspect.run_extract(
            make_repo(
                {
                    "pyproject.toml": '[project]\nname = "otherpkg"\n',
                    "license.txt": "MIT",
                    "docs/CONTRIBUTING.rst": "",
                    "CODE_OF_CONDUCT.md": "Copyright 2024 otherpkg\nBe nice.\nReport abuse.\n",
                    "SECURITY.md": "",
                },
                name="sat",
            )
        )
        assert tuples(aspect.compare(main, other)) == {
            (Kind.MOVED, Subject.FILE, Direction.DOWNSTREAM, "LICENSE"),  # LICENSE vs license.txt
            (Kind.MOVED, Subject.FILE, Direction.DOWNSTREAM, "CONTRIBUTING.md"),
            (Kind.MOVED, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md"),
            (Kind.DIFFERS, Subject.FILE, Direction.DOWNSTREAM, ".github/CODE_OF_CONDUCT.md"),
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, ".gitignore"),
            (Kind.EXTRA, Subject.FILE, Direction.UPSTREAM, "SECURITY.md"),
        }
        assert aspect.self_check(main) == []
        assert aspect.self_check(other) == []
