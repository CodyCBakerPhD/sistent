"""Shared fixtures: in-memory repositories, snapshots, config files, bare git remotes and a CLI runner."""

from __future__ import annotations

import itertools
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from sistent.compare.text import NullSubstituter, Substituter
from sistent.model import Identity, Snapshot
from sistent.repository import RepoContext, RepoHints, Repository, build_identity

FIXTURES = Path(__file__).parent / "fixtures"
GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git executable not available")

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "sistent tests",
    "GIT_AUTHOR_EMAIL": "tests@example.invalid",
    "GIT_COMMITTER_NAME": "sistent tests",
    "GIT_COMMITTER_EMAIL": "tests@example.invalid",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def write_files(root: Path, files: Mapping[str, str | bytes]) -> None:
    """Write ``{relative path: content}`` under ``root``, creating directories. A trailing ``/`` makes a directory."""
    for rel, content in files.items():
        target = root / rel
        if rel.endswith("/"):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            with open(target, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True, timeout=60)
    return proc.stdout


MakeRepo = Callable[..., RepoContext]


@pytest.fixture
def make_repo(tmp_path: Path) -> MakeRepo:
    """Create a repository directory from ``files`` and return a :class:`RepoContext` for it.

    ``identity`` defaults to the probed identity (config key = ``name``); ``foreign`` identities enable stale
    detection; ``substitute=False`` uses a :class:`NullSubstituter`.
    """
    counter = itertools.count(1)

    def _make(
        files: Mapping[str, str | bytes] | None = None,
        *,
        name: str | None = None,
        identity: Identity | None = None,
        foreign: Iterable[Identity] = (),
        is_main: bool = False,
        substitute: bool = True,
        aliases: tuple[str, ...] = (),
        vars: dict[str, str] | None = None,  # mirrors the config key
        url: str | None = None,
    ) -> RepoContext:
        name = name or f"repo{next(counter)}"
        root = tmp_path / name
        root.mkdir(parents=True, exist_ok=True)
        write_files(root, files or {})
        repo = Repository(name, root)
        if identity is None:
            identity = build_identity(repo, RepoHints(name=name, url=url, aliases=aliases, vars=vars or {}))
        subst: Substituter = Substituter(identity, tuple(foreign)) if substitute else NullSubstituter()
        return RepoContext(repo=repo, identity=identity, is_main=is_main, subst=subst)

    return _make


@pytest.fixture
def make_snapshot() -> Callable[..., Snapshot]:
    def _make(aspect: str, repo: str, data: dict[str, Any], **kwargs: Any) -> Snapshot:
        kwargs.setdefault("schema_version", 1)
        return Snapshot(aspect=aspect, repo=repo, data=data, **kwargs)

    return _make


@pytest.fixture
def make_config(tmp_path: Path) -> Callable[..., Path]:
    """Write a ``sistent.toml`` (or ``name``) into ``tmp_path`` and return its path."""

    def _make(text: str, *, name: str = "sistent.toml") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return _make


@pytest.fixture
def bare_git_repo(tmp_path: Path) -> Callable[..., str]:
    """Create a bare git repository with one commit of ``files`` on ``branch``; returns its ``file://`` URL.

    ``tag`` additionally tags that commit. The returned URL can be used as ``[repos.X] url``.
    """
    if GIT is None:
        pytest.skip("git executable not available")
    counter = itertools.count(1)

    def _make(
        files: Mapping[str, str | bytes],
        *,
        name: str | None = None,
        branch: str = "main",
        tag: str | None = None,
    ) -> str:
        name = name or f"remote{next(counter)}"
        work = tmp_path / f"{name}-work"
        bare = tmp_path / f"{name}.git"
        work.mkdir()
        git("init", "-q", f"--initial-branch={branch}", cwd=work)
        write_files(work, files)
        git("add", "-A", cwd=work)
        git("commit", "-q", "-m", "initial", cwd=work)
        if tag:
            git("tag", tag, cwd=work)
        git("clone", "-q", "--bare", str(work), str(bare), cwd=tmp_path)
        git("symbolic-ref", "HEAD", f"refs/heads/{branch}", cwd=bare)
        return bare.as_uri()

    return _make


@contextmanager
def chdir(path: Path | None):  # type: ignore[no-untyped-def]
    if path is None:
        yield
        return
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


@pytest.fixture
def run_cli() -> Callable[..., Any]:
    """Invoke the ``sistent`` CLI in-process: ``run_cli(["check", "-c", path])`` -> click ``Result``."""
    from click.testing import CliRunner

    def _run(args: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> Any:
        from sistent.cli import main

        runner = CliRunner()
        with chdir(cwd):
            return runner.invoke(main, args, env=env, catch_exceptions=False)

    return _run


@pytest.fixture
def real_fixture() -> Callable[[str], str]:
    """Contents of a file under ``tests/fixtures/real/`` (README/AGENTS files of public scientific Python repos)."""

    def _read(name: str) -> str:
        return (FIXTURES / "real" / name).read_text(encoding="utf-8")

    return _read
