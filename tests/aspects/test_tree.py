"""Tests for the ``tree`` aspect: layout extraction (depth, includes, ignores, git) and set comparison."""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from sistent.aspects import DEFAULT_CONFIG
from sistent.aspects.tree import TreeAspect, TreeOptions, compile_glob
from sistent.model import Direction, Finding, Kind, Severity, Subject
from sistent.options import OptionsError, parse_options
from tests.conftest import MakeRepo, git, requires_git, write_files

PYPROJECT = '[project]\nname = "mypkg"\n'

LAYOUT_FILES: dict[str, str] = {
    "pyproject.toml": PYPROJECT,
    "README.md": "# mypkg\n",
    "src/mypkg/__init__.py": "",
    "src/mypkg/core/engine.py": "",
    "tests/test_core.py": "",
    "docs/conf.py": "",
    "docs/api/index.md": "",
    ".github/workflows/ci.yml": "",
    ".github/dependabot.yml": "",
    "__pycache__/x.pyc": "",
    "build/lib/x.py": "",
}

LAYOUT_DATA: dict[str, list[str]] = {
    "dirs": [".github/", ".github/workflows/", "docs/", "docs/api/", "src/", "src/{{name}}/", "tests/"],
    "files": [".github/dependabot.yml", ".github/workflows/ci.yml", "README.md", "docs/conf.py", "pyproject.toml"],
}


def layout_table() -> dict[str, Any]:
    """The ``[aspects.layout]`` default table without its ``type`` key."""
    table = dict(tomllib.loads(DEFAULT_CONFIG)["aspects"]["layout"])
    table.pop("type")
    return table


def layout(**overrides: Any) -> TreeAspect:
    """A ``tree`` aspect with the DEFAULT_CONFIG ``layout`` options, some of them overridden."""
    return TreeAspect("layout", parse_options(TreeOptions, {**layout_table(), **overrides}, where="aspects.layout"))


def tuples(findings: Iterable[Finding]) -> set[tuple[Kind, Subject, Direction, str]]:
    return {(f.kind, f.subject, f.direction, f.locator) for f in findings}


def snap(make_snapshot: Any, repo: str, dirs: list[str], files: list[str] | None = None) -> Any:
    return make_snapshot("layout", repo, {"dirs": dirs, "files": files or []})


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_default_config_table_parses_with_the_documented_names(self) -> None:
        options = parse_options(TreeOptions, layout_table(), where="aspects.layout")
        assert options.depth == 2
        assert options.file_depth == 1
        assert options.include == [".github/**", "docs/*"]
        assert "__pycache__" in options.ignore
        assert ".claude/skills" in options.ignore
        assert options.use_git is True

    def test_defaults(self) -> None:
        options = TreeOptions()
        assert (options.depth, options.file_depth, options.include, options.ignore, options.use_git) == (
            2,
            1,
            [],
            [],
            True,
        )

    @pytest.mark.parametrize("table", [{"depth": -1}, {"file_depth": -2}, {"include": [" "]}])
    def test_invalid_values_are_rejected(self, table: dict[str, Any]) -> None:
        with pytest.raises(OptionsError):
            parse_options(TreeOptions, table, where="aspects.layout")

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(OptionsError, match="unknown option"):
            parse_options(TreeOptions, {"depths": 3}, where="aspects.layout")

    def test_aspect_metadata(self) -> None:
        aspect = layout()
        assert TreeAspect.type_name == "tree"
        assert TreeAspect.options_cls is TreeOptions
        assert TreeAspect.description
        assert aspect.name == "layout"


class TestCompileGlob:
    @pytest.mark.parametrize(
        ("pattern", "path", "expected"),
        [
            ("docs/*", "docs/conf.py", True),
            ("docs/*", "docs/api/index.md", False),
            ("docs/*", "DOCS/Conf.py", True),
            (".github/**", ".github/dependabot.yml", True),
            (".github/**", ".github/workflows/ci.yml", True),
            (".github/**", ".githubx/ci.yml", False),
            ("**/*.md", "README.md", True),
            ("**/*.md", "docs/api/index.md", True),
            ("*.md", "docs/index.md", False),
            ("docs/?.md", "docs/a.md", True),
            ("docs/?.md", "docs/ab.md", False),
            ("docs/[ab].md", "docs/a.md", True),
            ("docs/[!ab].md", "docs/a.md", False),
            ("src/*/__init__.py", "src/pkg/__init__.py", True),
            ("src/*/__init__.py", "src/pkg/sub/__init__.py", False),
        ],
    )
    def test_matching(self, pattern: str, path: str, expected: bool) -> None:
        assert bool(compile_glob(pattern).match(path)) is expected


# ----- extraction --------------------------------------------------------------------------------------------------


class TestExtract:
    def test_default_layout_options(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(LAYOUT_FILES)
        assert "mypkg" in ctx.identity.aliases, "probed from pyproject.toml and src/mypkg/__init__.py"
        data = layout().extract(ctx)
        assert data == LAYOUT_DATA

    def test_run_extract_wraps_the_snapshot(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(LAYOUT_FILES)
        result = layout().run_extract(ctx)
        assert result.data == LAYOUT_DATA
        assert result.sources == (), "the tree aspect lists paths, it reads no file"
        assert result.aliases_applied == ("mypkg",)
        assert result.stale_hits == ()

    def test_substitution_is_per_component(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {"pyproject.toml": PYPROJECT, "src/mypkg/__init__.py": "", "mypkg.toml": "", "mypkg_utils/x": ""}
        )
        data = layout().extract(ctx)
        assert data == {
            "dirs": ["src/", "src/{{name}}/", "{{name}}_utils/"],
            "files": ["pyproject.toml", "{{name}}.toml"],
        }

    def test_without_substitution(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"pyproject.toml": PYPROJECT, "src/mypkg/__init__.py": ""}, substitute=False)
        assert layout().extract(ctx) == {"dirs": ["src/", "src/mypkg/"], "files": ["pyproject.toml"]}

    def test_depth_limits_directories(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"a/b/c/d.txt": "", "top.txt": ""})
        assert layout(depth=1, include=[]).extract(ctx) == {"dirs": ["a/"], "files": ["top.txt"]}
        assert layout(depth=3, include=[]).extract(ctx) == {"dirs": ["a/", "a/b/", "a/b/c/"], "files": ["top.txt"]}
        assert layout(depth=0, include=[]).extract(ctx) == {"dirs": [], "files": ["top.txt"]}

    def test_file_depth(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"a/b/c.txt": "", "a/x.txt": "", "top.txt": ""})
        assert layout(file_depth=2, include=[]).extract(ctx)["files"] == ["a/x.txt", "top.txt"]
        assert layout(file_depth=3, include=[]).extract(ctx)["files"] == ["a/b/c.txt", "a/x.txt", "top.txt"]
        assert layout(file_depth=0, include=[]).extract(ctx)["files"] == []

    def test_include_globs_add_files_beyond_file_depth(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"docs/conf.py": "", "docs/api/index.md": "", ".github/a/b/c.yml": "", "src/p/q/r.md": ""})
        assert layout(include=["docs/*"]).extract(ctx)["files"] == ["docs/conf.py"]
        assert layout(include=[".github/**"]).extract(ctx)["files"] == [".github/a/b/c.yml"]
        assert layout(include=["**/*.md"]).extract(ctx)["files"] == ["docs/api/index.md", "src/p/q/r.md"]
        assert layout(include=[]).extract(ctx)["files"] == []

    def test_include_depth_does_not_widen_directories(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"a/b/c/e.txt": "", "a/b/c/d/deep.txt": ""})
        # the include glob raises the scan depth to 4 for files, directories stay limited to ``depth``
        data = layout(depth=1, include=["a/b/c/*"]).extract(ctx)
        assert data == {"dirs": ["a/"], "files": ["a/b/c/e.txt"]}
        data = layout(depth=1, include=["**/*.txt"]).extract(ctx)
        assert data == {"dirs": ["a/"], "files": ["a/b/c/d/deep.txt", "a/b/c/e.txt"]}

    def test_default_ignores_drop_artefacts_and_skills(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {
                "README.md": "",
                ".claude/skills/demo/SKILL.md": "",
                ".claude/settings.json": "",
                "skills/demo/SKILL.md": "",
                "SKILL.md": "",
                "src/pkg/skills/x.py": "",
                "src/pkg/__init__.py": "",
                "build/lib/x.py": "",
                "dist/x.whl": "",
                "pkg.egg-info/PKG-INFO": "",
                ".venv/lib/x.py": "",
                "node_modules/x/index.js": "",
                ".git/HEAD": "",
            }
        )
        data = layout().extract(ctx)
        assert data == {"dirs": [".claude/", "src/", "src/pkg/"], "files": ["README.md"]}

    def test_custom_ignore_matches_components_or_paths(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"a/keep.txt": "", "a/drop/x.txt": "", "b/drop/y.txt": "", "c/d/e.txt": ""})
        # a bare name matches that component anywhere; ``b/`` disappears with it because it held nothing else
        data = layout(ignore=["drop"], include=[]).extract(ctx)
        assert data["dirs"] == ["a/", "c/", "c/d/"]
        # a pattern with a slash matches one path prefix only
        data = layout(ignore=["a/drop"], include=[]).extract(ctx)
        assert data["dirs"] == ["a/", "b/", "b/drop/", "c/", "c/d/"]

    def test_empty_directories_are_not_listed(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"empty/": "", "full/x.txt": ""})
        assert layout(include=[]).extract(ctx) == {"dirs": ["full/"], "files": []}

    def test_use_git_false_walks_the_filesystem(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(LAYOUT_FILES)
        assert layout(use_git=False).extract(ctx) == LAYOUT_DATA

    def test_use_git_false_does_not_follow_symlinked_directories(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"real/x.txt": "", "top.txt": ""})
        (ctx.repo.root / "link").symlink_to(ctx.repo.root / "real", target_is_directory=True)
        data = layout(use_git=False, include=[]).extract(ctx)
        assert data == {"dirs": ["real/"], "files": ["link", "top.txt"]}

    @requires_git
    def test_git_backed_repo_respects_gitignore(self, make_repo: MakeRepo, tmp_path: Path) -> None:
        root = tmp_path / "gitrepo"
        write_files(root, {**LAYOUT_FILES, ".gitignore": "secrets.txt\ngenerated/\n"})
        git("init", "-q", cwd=root)
        git("add", "-A", cwd=root)
        git("commit", "-q", "-m", "initial", cwd=root)
        write_files(root, {"secrets.txt": "", "generated/out.txt": "", "untracked.txt": ""})
        ctx = make_repo(name="gitrepo")
        assert ctx.repo.is_git
        data = layout().extract(ctx)
        assert data["dirs"] == LAYOUT_DATA["dirs"]
        assert data["files"] == [
            ".github/dependabot.yml",
            ".github/workflows/ci.yml",
            ".gitignore",
            "README.md",
            "docs/conf.py",
            "pyproject.toml",
            "untracked.txt",
        ]
        walked = layout(use_git=False).extract(ctx)
        assert "secrets.txt" in walked["files"]
        assert "generated/" in walked["dirs"]
        assert walked["files"] == [
            ".github/dependabot.yml",
            ".github/workflows/ci.yml",
            ".gitignore",
            "README.md",
            "docs/conf.py",
            "pyproject.toml",
            "secrets.txt",
            "untracked.txt",
        ]


# ----- comparison --------------------------------------------------------------------------------------------------


class TestCompare:
    def test_identical_snapshots_yield_nothing(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", ["src/", "src/{{name}}/"], ["pyproject.toml"])
        other = snap(make_snapshot, "sat", ["src/", "src/{{name}}/"], ["pyproject.toml"])
        assert layout().compare(main, other) == []

    def test_missing_and_extra_dirs_and_files(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", ["docs/", "src/", "tests/"], ["README.md", "pyproject.toml"])
        other = snap(make_snapshot, "sat", ["examples/", "src/", "tests/"], ["pyproject.toml", "setup.py"])
        findings = layout().compare(main, other)
        assert tuples(findings) == {
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "docs/"),
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "README.md"),
            (Kind.EXTRA, Subject.PATH, Direction.UPSTREAM, "examples/"),
            (Kind.EXTRA, Subject.PATH, Direction.UPSTREAM, "setup.py"),
        }
        by_locator = {f.locator: f for f in findings}
        assert by_locator["docs/"].severity is Severity.WARNING
        assert by_locator["docs/"].message == "directory missing: docs/"
        assert by_locator["docs/"].content_key == "docs/"
        assert by_locator["README.md"].message == "file missing: README.md"
        assert by_locator["examples/"].severity is Severity.INFO
        assert by_locator["examples/"].message == "directory only in repo: examples/"
        assert by_locator["setup.py"].message == "file only in repo: setup.py"
        assert all(f.repo == "sat" and f.aspect == "layout" for f in findings)

    def test_flat_versus_src_layout_is_moved(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", ["src/", "src/{{name}}/", "tests/"])
        other = snap(make_snapshot, "sat", ["{{name}}/", "tests/"])
        findings = layout().compare(main, other)
        assert tuples(findings) == {
            (Kind.MOVED, Subject.PATH, Direction.DOWNSTREAM, "src/{{name}}/"),
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "src/"),
        }
        moved = next(f for f in findings if f.kind is Kind.MOVED)
        assert moved.severity is Severity.INFO
        assert moved.message == "directory moved: src/{{name}}/ -> {{name}}/"
        assert moved.detail == "main: src/{{name}}/ ; repo: {{name}}/"
        assert moved.detail_kind == "text"
        assert moved.content_key == "src/{{name}}/"

    def test_moved_file(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", [".github/"], [".github/dependabot.yml"])
        other = snap(make_snapshot, "sat", [".github/"], ["dependabot.yml"])
        findings = layout().compare(main, other)
        assert tuples(findings) == {(Kind.MOVED, Subject.PATH, Direction.DOWNSTREAM, ".github/dependabot.yml")}
        assert findings[0].message == "file moved: .github/dependabot.yml -> dependabot.yml"

    def test_files_and_directories_never_pair(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", ["docs/"], [])
        other = snap(make_snapshot, "sat", [], ["docs"])
        assert tuples(layout().compare(main, other)) == {
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "docs/"),
            (Kind.EXTRA, Subject.PATH, Direction.UPSTREAM, "docs"),
        }

    def test_ambiguous_basenames_are_not_paired(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", ["a/", "a/x/", "b/", "b/x/"])
        other = snap(make_snapshot, "sat", ["a/", "b/", "x/"])
        assert tuples(layout().compare(main, other)) == {
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "a/x/"),
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "b/x/"),
            (Kind.EXTRA, Subject.PATH, Direction.UPSTREAM, "x/"),
        }

    def test_highest_node_rule_for_missing(self, make_snapshot: Any) -> None:
        main = snap(
            make_snapshot,
            "main",
            ["docs/", "docs/api/", "src/"],
            ["docs/conf.py", "docs/api/index.md", "pyproject.toml"],
        )
        other = snap(make_snapshot, "sat", ["src/"], ["pyproject.toml"])
        assert tuples(layout().compare(main, other)) == {(Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "docs/")}

    def test_highest_node_rule_for_extra(self, make_snapshot: Any) -> None:
        main = snap(make_snapshot, "main", ["src/"], [])
        other = snap(
            make_snapshot,
            "sat",
            [".github/", ".github/workflows/", "src/"],
            [".github/workflows/ci.yml", ".github/dependabot.yml"],
        )
        assert tuples(layout().compare(main, other)) == {(Kind.EXTRA, Subject.PATH, Direction.UPSTREAM, ".github/")}

    def test_highest_node_rule_with_deep_ancestor(self, make_snapshot: Any) -> None:
        # The unmatched ancestor need not be the direct parent: ``a/`` is missing, so ``a/b/c.txt`` is not reported.
        main = snap(make_snapshot, "main", ["a/"], ["a/b/c.txt"])
        other = snap(make_snapshot, "sat", [], [])
        assert tuples(layout().compare(main, other)) == {(Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "a/")}

    def test_children_of_a_moved_directory_are_not_reported(self, make_snapshot: Any) -> None:
        main = snap(
            make_snapshot, "main", ["src/", "src/{{name}}/"], ["src/{{name}}/__init__.py", "src/{{name}}/core.py"]
        )
        other = snap(make_snapshot, "sat", ["{{name}}/"], ["{{name}}/__init__.py", "{{name}}/core.py"])
        assert tuples(layout().compare(main, other)) == {
            (Kind.MOVED, Subject.PATH, Direction.DOWNSTREAM, "src/{{name}}/"),
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "src/"),
        }

    def test_severity_overrides_apply(self, make_snapshot: Any) -> None:
        aspect = layout(severity={"missing.path": "error", "moved": "warning", "extra": "error"})
        main = snap(make_snapshot, "main", ["src/", "src/{{name}}/"], ["README.md"])
        other = snap(make_snapshot, "sat", ["{{name}}/"], ["extra.txt"])
        by_kind = {f.kind: f for f in aspect.compare(main, other)}
        assert by_kind[Kind.MISSING].severity is Severity.ERROR
        assert by_kind[Kind.MOVED].severity is Severity.WARNING
        assert by_kind[Kind.EXTRA].severity is Severity.INFO, "upstream findings are pinned at info"

    def test_missing_keys_in_data_are_tolerated(self, make_snapshot: Any) -> None:
        assert layout().compare(make_snapshot("layout", "main", {}), make_snapshot("layout", "sat", {})) == []

    def test_end_to_end_flat_versus_src(self, make_repo: MakeRepo) -> None:
        aspect = layout()
        main = aspect.run_extract(
            make_repo(
                {"pyproject.toml": PYPROJECT, "src/mypkg/__init__.py": "", "tests/test_x.py": ""},
                name="main",
                is_main=True,
            )
        )
        other = aspect.run_extract(
            make_repo(
                {"pyproject.toml": '[project]\nname = "otherpkg"\n', "otherpkg/__init__.py": "", "tests/test_x.py": ""},
                name="sat",
            )
        )
        assert main.data == {"dirs": ["src/", "src/{{name}}/", "tests/"], "files": ["pyproject.toml"]}
        assert other.data == {"dirs": ["tests/", "{{name}}/"], "files": ["pyproject.toml"]}
        assert tuples(aspect.compare(main, other)) == {
            (Kind.MOVED, Subject.PATH, Direction.DOWNSTREAM, "src/{{name}}/"),
            (Kind.MISSING, Subject.PATH, Direction.DOWNSTREAM, "src/"),
        }
