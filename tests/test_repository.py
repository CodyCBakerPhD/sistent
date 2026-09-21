"""Tests for :class:`sistent.repository.Repository`: file access, globbing, file iteration and git helpers."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

from sistent.repository import Entry, Repository
from tests.conftest import git, requires_git, write_files

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def init_git(root: Path, files: Mapping[str, str | bytes], *, branch: str = "main") -> str:
    """Create a git repository at ``root`` with one commit of ``files``; returns the commit sha."""
    root.mkdir(parents=True, exist_ok=True)
    write_files(root, files)
    git("init", "-q", f"--initial-branch={branch}", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "initial", cwd=root)
    return git("rev-parse", "HEAD", cwd=root).strip()


def entries(repo: Repository, **kwargs: object) -> list[tuple[str, bool]]:
    return [(e.rel, e.is_dir) for e in repo.iter_files(**kwargs)]  # type: ignore[arg-type]


def rels(repo: Repository, **kwargs: object) -> list[str]:
    return [e.rel for e in repo.iter_files(**kwargs)]  # type: ignore[arg-type]


# ----- basics ------------------------------------------------------------------------------------------------------


class TestBasics:
    def test_paths_are_resolved_and_checkout_defaults_to_root(self, tmp_path: Path) -> None:
        repo = Repository("r", tmp_path / "r")
        assert repo.name == "r"
        assert repo.root == (tmp_path / "r").resolve()
        assert repo.checkout == repo.root
        assert repo.path("a/b.txt") == repo.root / "a" / "b.txt"
        assert repr(repo) == f"Repository('r', {str(repo.root)!r})"

    def test_exists_is_dir_is_symlink(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"README.md": "x", "docs/": ""})
        repo = Repository("r", tmp_path)
        assert repo.exists("README.md")
        assert repo.exists("docs")
        assert not repo.exists("missing.md")
        assert repo.is_dir("docs")
        assert not repo.is_dir("README.md")
        assert not repo.is_symlink("README.md")

    def test_read_bytes(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"bin.dat": b"\x00\x01"})
        assert Repository("r", tmp_path).read_bytes("bin.dat") == b"\x00\x01"


class TestReadText:
    def test_strips_bom(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"README.md": b"\xef\xbb\xbf# Title\n"})
        assert Repository("r", tmp_path).read_text("README.md") == "# Title\n"

    def test_plain_utf8(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"README.md": "café\n"})
        assert Repository("r", tmp_path).read_text("README.md") == "café\n"

    def test_invalid_bytes_are_replaced_by_default(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"bad.txt": b"caf\xe9"})
        repo = Repository("r", tmp_path)
        assert repo.read_text("bad.txt") == "caf�"

    def test_errors_strict_raises(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"bad.txt": b"caf\xe9"})
        with pytest.raises(UnicodeDecodeError):
            Repository("r", tmp_path).read_text("bad.txt", errors="strict")

    def test_memoises_per_path(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"README.md": "first"})
        repo = Repository("r", tmp_path)
        assert repo.read_text("README.md") == "first"
        write_files(tmp_path, {"README.md": "second"})
        assert repo.read_text("README.md") == "first", "second read comes from the cache"
        assert repo.read_bytes("README.md") == b"second", "read_bytes is not cached"
        assert Repository("r2", tmp_path).read_text("README.md") == "second"

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            Repository("r", tmp_path).read_text("nope.md")


class TestGlob:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Repository:
        write_files(
            tmp_path,
            {
                "README.md": "",
                "readme.rst": "",
                "License": "",
                "LICENSE.txt": "",
                "docs/Guide.MD": "",
                "docs/notes.txt": "",
                "docs/sub/deep.md": "",
            },
        )
        return Repository("r", tmp_path)

    def test_case_insensitive_and_sorted(self, repo: Repository) -> None:
        assert repo.glob("readme*") == ["README.md", "readme.rst"]
        assert repo.glob("README.MD") == ["README.md"]
        assert repo.glob("license*") == ["LICENSE.txt", "License"]
        assert repo.glob("*.md") == ["README.md"]

    def test_non_recursive(self, repo: Repository) -> None:
        assert repo.glob("*.md") == ["README.md"]
        assert repo.glob("deep.md") == []

    def test_dirs(self, repo: Repository) -> None:
        assert repo.glob("*.md", dirs=("docs",)) == ["docs/Guide.MD"]
        assert repo.glob("*.md", dirs=("docs/",)) == ["docs/Guide.MD"]
        assert repo.glob("*.md", dirs=(".", "docs", "docs/sub")) == ["README.md", "docs/Guide.MD", "docs/sub/deep.md"]

    def test_missing_dirs_are_skipped(self, repo: Repository) -> None:
        assert repo.glob("*.md", dirs=("nope",)) == []
        assert repo.glob("*.md", dirs=("nope", "docs")) == ["docs/Guide.MD"]
        assert repo.glob("*", dirs=("README.md",)) == [], "a file is not a directory"

    def test_matches_directories_too(self, repo: Repository) -> None:
        assert repo.glob("DOCS") == ["docs"]


# ----- iter_files without git --------------------------------------------------------------------------------------


PLAIN_FILES = {
    "README.md": "",
    "src/pkg/__init__.py": "",
    "src/pkg/mod.py": "",
    "src/pkg/__pycache__/mod.cpython-311.pyc": b"",
    "src/pkg.egg-info/PKG-INFO": "",
    ".claude/skills/demo/SKILL.md": "",
    ".claude/settings.json": "",
    "other/skills/x.md": "",
    "empty/": "",
}
PLAIN_IGNORE = ("__pycache__", "*.egg-info", ".claude/skills")


class TestIterFilesPlain:
    @pytest.fixture
    def repo(self, tmp_path: Path) -> Repository:
        write_files(tmp_path, PLAIN_FILES)
        return Repository("r", tmp_path)

    def test_lists_files_and_derived_dirs_sorted(self, repo: Repository) -> None:
        assert entries(repo, ignore=PLAIN_IGNORE) == [
            (".claude", True),
            (".claude/settings.json", False),
            ("README.md", False),
            ("other", True),
            ("other/skills", True),
            ("other/skills/x.md", False),
            ("src", True),
            ("src/pkg", True),
            ("src/pkg/__init__.py", False),
            ("src/pkg/mod.py", False),
        ]

    def test_empty_directories_are_not_reported(self, repo: Repository) -> None:
        assert (repo.root / "empty").is_dir()
        assert "empty" not in rels(repo, ignore=PLAIN_IGNORE)

    def test_directories_come_before_their_content(self, repo: Repository) -> None:
        listed = rels(repo, ignore=PLAIN_IGNORE)
        assert listed.index("src") < listed.index("src/pkg") < listed.index("src/pkg/__init__.py")
        assert listed == sorted(listed)

    def test_no_ignore_lists_everything(self, repo: Repository) -> None:
        listed = rels(repo)
        assert "src/pkg/__pycache__/mod.cpython-311.pyc" in listed
        assert "src/pkg.egg-info/PKG-INFO" in listed
        assert ".claude/skills/demo/SKILL.md" in listed

    def test_ignore_by_component(self, repo: Repository) -> None:
        listed = rels(repo, ignore=("__pycache__", "*.egg-info"))
        assert "src/pkg/__pycache__" not in listed
        assert "src/pkg/__pycache__/mod.cpython-311.pyc" not in listed
        assert "src/pkg.egg-info" not in listed
        assert "src/pkg.egg-info/PKG-INFO" not in listed
        assert "src/pkg/mod.py" in listed

    def test_ignore_by_path_only_matches_that_path(self, repo: Repository) -> None:
        listed = rels(repo, ignore=(".claude/skills",))
        assert ".claude/skills" not in listed
        assert ".claude/skills/demo/SKILL.md" not in listed
        assert ".claude/settings.json" in listed
        assert "other/skills/x.md" in listed, "a path pattern does not match the same component elsewhere"

    def test_ignore_component_matches_anywhere(self, repo: Repository) -> None:
        listed = rels(repo, ignore=("skills",))
        assert ".claude/skills/demo/SKILL.md" not in listed
        assert "other/skills/x.md" not in listed
        assert "other" not in listed, "a directory with only ignored content disappears"

    def test_ignore_trailing_slash_and_glob_paths(self, repo: Repository) -> None:
        assert "src/pkg/__pycache__/mod.cpython-311.pyc" not in rels(repo, ignore=("__pycache__/",))
        assert ".claude/skills/demo/SKILL.md" not in rels(repo, ignore=(".claude/sk*",))
        assert "src/pkg/mod.py" not in rels(repo, ignore=("src/pkg/*.py",))

    @pytest.mark.parametrize(
        ("depth", "expected"),
        [
            (1, [".claude", "README.md", "other", "src"]),
            (2, [".claude", ".claude/settings.json", "README.md", "other", "other/skills", "src", "src/pkg"]),
        ],
    )
    def test_depth_limits_components(self, repo: Repository, depth: int, expected: list[str]) -> None:
        assert rels(repo, depth=depth, ignore=PLAIN_IGNORE) == expected

    def test_depth_none_is_unlimited(self, repo: Repository) -> None:
        assert rels(repo, depth=None, ignore=PLAIN_IGNORE) == rels(repo, ignore=PLAIN_IGNORE)

    def test_symlinked_file_is_listed_as_a_file(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"README.md": "x", "docs/a.md": ""})
        try:
            os.symlink(tmp_path / "README.md", tmp_path / "LINK.md")
            os.symlink(tmp_path / "docs", tmp_path / "docs-link")
        except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
            pytest.skip("symlinks not supported here")
        repo = Repository("r", tmp_path)
        listed = entries(repo)
        assert ("LINK.md", False) in listed
        assert ("docs-link", False) in listed, "a symlinked directory is one entry, not descended into"
        assert "docs-link/a.md" not in [rel for rel, _ in listed]
        assert repo.is_symlink("LINK.md")
        assert repo.read_text("LINK.md") == "x"

    def test_dangling_symlink_is_listed(self, tmp_path: Path) -> None:
        try:
            os.symlink(tmp_path / "nowhere", tmp_path / "dangling")
        except (OSError, NotImplementedError):  # pragma: no cover
            pytest.skip("symlinks not supported here")
        assert rels(Repository("r", tmp_path)) == ["dangling"]

    def test_empty_repository(self, tmp_path: Path) -> None:
        assert rels(Repository("r", tmp_path)) == []

    def test_missing_root_yields_nothing(self, tmp_path: Path) -> None:
        assert rels(Repository("r", tmp_path / "missing")) == []


class TestEntry:
    def test_depth_and_name(self) -> None:
        assert Entry("README.md", False).depth == 1
        assert Entry("README.md", False).name == "README.md"
        assert Entry("src/pkg/mod.py", False).depth == 3
        assert Entry("src/pkg/mod.py", False).name == "mod.py"
        assert Entry("src", True).depth == 1


# ----- root sub-directory ------------------------------------------------------------------------------------------


SUBDIR_FILES = {
    "pkg/README.md": "",
    "pkg/src/a.py": "",
    "pkg/build/junk.txt": "",
    "other/x.txt": "",
    "TOP.md": "",
}


class TestRootSubdirectory:
    def test_without_git(self, tmp_path: Path) -> None:
        write_files(tmp_path, SUBDIR_FILES)
        repo = Repository("r", tmp_path / "pkg", checkout=tmp_path)
        assert repo.root == (tmp_path / "pkg").resolve()
        assert repo.checkout == tmp_path.resolve()
        assert not repo.is_git
        assert entries(repo, ignore=("build",)) == [("README.md", False), ("src", True), ("src/a.py", False)]
        assert repo.exists("README.md")
        assert not repo.exists("TOP.md")
        assert repo.head() is None

    @requires_git
    def test_with_git(self, tmp_path: Path) -> None:
        sha = init_git(tmp_path, SUBDIR_FILES)
        repo = Repository("r", tmp_path / "pkg", checkout=tmp_path)
        assert repo.is_git
        assert entries(repo, ignore=("build",)) == [("README.md", False), ("src", True), ("src/a.py", False)]
        assert repo.head() == sha


# ----- git ---------------------------------------------------------------------------------------------------------


class TestGit:
    def test_plain_directory_is_not_git(self, tmp_path: Path) -> None:
        repo = Repository("r", tmp_path)
        assert not repo.is_git
        assert repo.head() is None

    def test_git_returns_none_when_the_command_fails(self, tmp_path: Path) -> None:
        assert Repository("r", tmp_path).git("definitely-not-a-git-subcommand") is None

    def test_git_returns_none_when_git_cannot_run(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("no git here")

        monkeypatch.setattr(subprocess, "run", boom)
        assert Repository("r", tmp_path).git("--version") is None

    def test_git_returns_none_on_timeout(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def slow(*args: object, **kwargs: object) -> None:
            raise subprocess.TimeoutExpired(cmd="git", timeout=0.1)

        monkeypatch.setattr(subprocess, "run", slow)
        assert Repository("r", tmp_path).git("status", timeout=0.1) is None

    @requires_git
    def test_git_returns_stdout_on_success(self, tmp_path: Path) -> None:
        init_git(tmp_path, {"a.txt": "a"})
        out = Repository("r", tmp_path).git("rev-parse", "--is-inside-work-tree")
        assert out is not None
        assert out.strip() == "true"

    @requires_git
    def test_head_returns_sha_and_memoises(self, tmp_path: Path) -> None:
        sha = init_git(tmp_path, {"a.txt": "a"})
        repo = Repository("r", tmp_path)
        assert repo.is_git
        assert repo.head() == sha
        assert SHA_RE.match(sha)
        write_files(tmp_path, {"b.txt": "b"})
        git("add", "-A", cwd=tmp_path)
        git("commit", "-q", "-m", "second", cwd=tmp_path)
        assert repo.head() == sha, "memoised"
        assert Repository("r2", tmp_path).head() != sha

    @requires_git
    def test_head_none_before_first_commit(self, tmp_path: Path) -> None:
        git("init", "-q", cwd=tmp_path)
        repo = Repository("r", tmp_path)
        assert repo.is_git
        assert repo.head() is None

    @requires_git
    def test_iter_files_with_git(self, tmp_path: Path) -> None:
        init_git(
            tmp_path,
            {
                ".gitignore": "ignored.txt\nbuild/\n",
                "README.md": "",
                "src/pkg/__init__.py": "",
                "docs/index.md": "",
            },
        )
        write_files(
            tmp_path,
            {
                "untracked.txt": "new",
                "src/pkg/new_module.py": "",
                "ignored.txt": "ignored",
                "build/out.txt": "ignored",
                "empty/": "",
            },
        )
        repo = Repository("r", tmp_path)
        assert repo.is_git
        listed = entries(repo)
        assert listed == [
            (".gitignore", False),
            ("README.md", False),
            ("docs", True),
            ("docs/index.md", False),
            ("src", True),
            ("src/pkg", True),
            ("src/pkg/__init__.py", False),
            ("src/pkg/new_module.py", False),
            ("untracked.txt", False),
        ]
        assert ".git" not in [rel for rel, _ in listed]

    @requires_git
    def test_iter_files_with_git_applies_ignore_and_depth(self, tmp_path: Path) -> None:
        init_git(tmp_path, {"README.md": "", "src/pkg/__init__.py": "", "docs/index.md": "", "docs/api/x.md": ""})
        repo = Repository("r", tmp_path)
        assert rels(repo, ignore=("docs",)) == ["README.md", "src", "src/pkg", "src/pkg/__init__.py"]
        assert rels(repo, depth=1) == ["README.md", "docs", "src"]
        # ignoring src/pkg leaves src without files, so the derived directory disappears too
        assert rels(repo, depth=2, ignore=("src/pkg",)) == ["README.md", "docs", "docs/api", "docs/index.md"]

    @requires_git
    def test_iter_files_with_git_lists_deleted_but_tracked_file(self, tmp_path: Path) -> None:
        # `git ls-files --cached` reports index entries; a file removed from the work tree but still in the index
        # is therefore listed. This documents the behaviour rather than prescribing it.
        init_git(tmp_path, {"a.txt": "", "b.txt": ""})
        (tmp_path / "b.txt").unlink()
        assert rels(Repository("r", tmp_path)) == ["a.txt", "b.txt"]
