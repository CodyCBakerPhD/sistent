"""Tests for repository sources: local paths and shallow git materialisation into the cache."""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from sistent.config import RepoSpec
from sistent.repository import Repository
from sistent.sources import SourceError, cache_key, git_available, materialise, resolve
from tests.conftest import git, requires_git

pytestmark = requires_git

MakeRemote = Callable[..., str]


def spec(name: str = "sat", **kwargs: Any) -> RepoSpec:
    return RepoSpec(name=name, **kwargs)


def bare_path(url: str) -> Path:
    assert url.startswith("file://")
    return Path(url[len("file://") :])


def head_of(repo: Repository) -> str:
    head = repo.head()
    assert head is not None
    return head


# ----- cache keys --------------------------------------------------------------------------------------------------


def test_cache_key_is_filesystem_safe_and_distinct_per_url_and_rev() -> None:
    a = cache_key("https://github.com/org-a/repo", None)
    b = cache_key("https://github.com/org-b/repo", None)
    c = cache_key("https://github.com/org-a/repo", "main")
    assert len({a, b, c}) == 3
    for key in (a, b, c):
        assert re.fullmatch(r"[A-Za-z0-9._-]+", key), key
    assert a.startswith("github.com__org-a__repo-")
    assert b.startswith("github.com__org-b__repo-")
    assert cache_key("git@github.com:org/repo.git", None).startswith("github.com__org__repo-")
    assert cache_key("https://github.com/org/repo.git", None).startswith("github.com__org__repo-")
    assert cache_key("file:///tmp/fleet/remote1.git", None).startswith("fleet__remote1-")
    assert cache_key("weird url with spaces", None) == cache_key("weird url with spaces", None)
    assert re.fullmatch(r"[A-Za-z0-9._-]+", cache_key("weird url with spaces", None))


def test_git_available() -> None:
    assert git_available()


# ----- url repos ---------------------------------------------------------------------------------------------------


def test_materialise_branch(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"README.md": "hello\n"}, branch="develop")
    cache = tmp_path / "cache"
    repo = resolve(spec(url=url, rev="develop"), cache_dir=cache)
    assert repo.read_text("README.md") == "hello\n"
    assert repo.root == (cache / cache_key(url, "develop")).resolve()
    assert repo.checkout == repo.root
    assert head_of(repo) == git("rev-parse", "HEAD", cwd=bare_path(url)).strip()
    assert not (cache / (cache_key(url, "develop") + ".tmp")).exists()


def test_materialise_remote_head_when_rev_is_absent(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"a.txt": "a"}, branch="trunk")
    repo = resolve(spec(url=url), cache_dir=tmp_path / "cache")
    assert repo.read_text("a.txt") == "a"


def test_materialise_tag(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"VERSION": "1.0"}, tag="v1.0")
    repo = resolve(spec(url=url, rev="v1.0"), cache_dir=tmp_path / "cache")
    assert repo.read_text("VERSION") == "1.0"
    assert head_of(repo) == git("rev-parse", "v1.0^{commit}", cwd=bare_path(url)).strip()


def test_materialise_full_sha_skips_fetch_when_cached(
    bare_git_repo: MakeRemote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = bare_git_repo({"f": "x"})
    sha = git("rev-parse", "HEAD", cwd=bare_path(url)).strip()
    cache = tmp_path / "cache"
    repo = resolve(spec(url=url, rev=sha), cache_dir=cache)
    assert head_of(repo) == sha

    commands: list[list[str]] = []
    real_run = subprocess.run

    def recording_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        commands.append(list(command))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    again = resolve(spec(url=url, rev=sha), cache_dir=cache)
    assert again.root == repo.root
    assert not any(cmd[1] == "fetch" for cmd in commands), commands


def test_existing_checkout_is_updated_on_fetch(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"f": "one"}, branch="main")
    cache = tmp_path / "cache"
    first = resolve(spec(url=url, rev="main"), cache_dir=cache)
    assert first.read_text("f") == "one"
    # advance the remote branch, then materialise again: the same cache entry follows the branch
    work = tmp_path / "advance"
    git("clone", "-q", url, str(work), cwd=tmp_path)
    (work / "f").write_text("two", encoding="utf-8")
    git("commit", "-qam", "second", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    second = resolve(spec(url=url, rev="main"), cache_dir=cache)
    assert second.root == first.root
    assert second.read_text("f") == "two"


def test_no_fetch_uses_cache_without_running_git(
    bare_git_repo: MakeRemote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = bare_git_repo({"f": "x"})
    cache = tmp_path / "cache"
    first = resolve(spec(url=url, rev="main"), cache_dir=cache)

    def no_git(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("git must not run with fetch=False")

    monkeypatch.setattr(subprocess, "run", no_git)
    second = resolve(spec(url=url, rev="main"), cache_dir=cache, fetch=False)
    assert second.root == first.root
    assert second.read_text("f") == "x"


def test_no_fetch_without_cache_is_an_error(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"f": "x"})
    with pytest.raises(SourceError, match=r"^sat: .*not cached.*run without --no-fetch"):
        resolve(spec(url=url, rev="main"), cache_dir=tmp_path / "cache", fetch=False)
    assert not (tmp_path / "cache").exists()


def test_two_urls_with_the_same_repo_name_get_separate_checkouts(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    url_a = bare_git_repo({"f": "A"}, name="one/repo")
    url_b = bare_git_repo({"f": "B"}, name="two/repo")
    assert bare_path(url_a).name == bare_path(url_b).name
    cache = tmp_path / "cache"
    repo_a = resolve(spec("repo", url=url_a), cache_dir=cache)
    repo_b = resolve(spec("repo", url=url_b), cache_dir=cache)
    assert repo_a.root != repo_b.root
    assert repo_a.read_text("f") == "A"
    assert repo_b.read_text("f") == "B"


def test_unknown_rev_and_abbreviated_sha_errors(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"f": "x"})
    sha = git("rev-parse", "HEAD", cwd=bare_path(url)).strip()
    cache = tmp_path / "cache"
    with pytest.raises(SourceError, match=r"^sat: git fetch of no-such-branch .* failed"):
        resolve(spec(url=url, rev="no-such-branch"), cache_dir=cache)
    with pytest.raises(SourceError, match="use the full 40-character SHA"):
        resolve(spec(url=url, rev=sha[:8]), cache_dir=cache)
    assert not list(cache.glob("*.tmp"))
    assert not (cache / cache_key(url, "no-such-branch")).exists()


def test_stale_tmp_directory_is_replaced(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"f": "x"})
    cache = tmp_path / "cache"
    stale = cache / (cache_key(url, "main") + ".tmp")
    stale.mkdir(parents=True)
    (stale / "junk").write_text("junk", encoding="utf-8")
    repo = resolve(spec(url=url, rev="main"), cache_dir=cache)
    assert repo.read_text("f") == "x"
    assert not stale.exists()
    assert not repo.exists("junk")


def test_timeout_becomes_source_error(
    bare_git_repo: MakeRemote, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = bare_git_repo({"f": "x"})

    def slow_run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))

    monkeypatch.setattr(subprocess, "run", slow_run)
    cache = tmp_path / "cache"
    with pytest.raises(SourceError, match=r"^sat: git init timed out after 7s"):
        resolve(spec(url=url, rev="main"), cache_dir=cache, timeout=7)
    assert not list(cache.glob("*.tmp"))


def test_concurrent_materialisation_of_one_key(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"f": "x"})
    cache = tmp_path / "cache"
    results: list[Path | BaseException] = []

    def worker() -> None:
        try:
            results.append(materialise(url, "main", cache))
        except BaseException as exc:  # collected for the assertion below
            results.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert all(isinstance(result, Path) for result in results), results
    assert len({str(result) for result in results}) == 1
    assert (cache / cache_key(url, "main") / "f").read_text(encoding="utf-8") == "x"
    assert not list(cache.glob("*.tmp"))


# ----- path repos and roots ----------------------------------------------------------------------------------------


def test_path_repo_is_used_in_place(tmp_path: Path) -> None:
    checkout = tmp_path / "local"
    checkout.mkdir()
    (checkout / "README.md").write_text("local", encoding="utf-8")
    repo = resolve(spec("local", path=checkout), cache_dir=tmp_path / "cache")
    assert repo.root == checkout.resolve()
    assert repo.checkout == checkout.resolve()
    assert repo.read_text("README.md") == "local"
    assert not (tmp_path / "cache").exists()


def test_path_repo_missing_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match=r"^local: path .*nowhere does not exist"):
        resolve(spec("local", path=tmp_path / "nowhere"), cache_dir=tmp_path / "cache")
    (tmp_path / "file").write_text("", encoding="utf-8")
    with pytest.raises(SourceError, match="is not a directory"):
        resolve(spec("local", path=tmp_path / "file"), cache_dir=tmp_path / "cache")


def test_root_subdirectory(bare_git_repo: MakeRemote, tmp_path: Path) -> None:
    url = bare_git_repo({"packages/foo/README.md": "foo", "packages/bar/README.md": "bar", "README.md": "top"})
    cache = tmp_path / "cache"
    repo = resolve(spec("foo", url=url, root="packages/foo"), cache_dir=cache)
    assert repo.root == (cache / cache_key(url, None) / "packages" / "foo").resolve()
    assert repo.checkout == (cache / cache_key(url, None)).resolve()
    assert repo.read_text("README.md") == "foo"
    assert repo.is_git
    assert [entry.rel for entry in repo.iter_files()] == ["README.md"]
    with pytest.raises(SourceError, match=r"^foo: root 'packages/nope' is not a directory"):
        resolve(spec("foo", url=url, root="packages/nope"), cache_dir=cache)


def test_root_subdirectory_for_path_repo(tmp_path: Path) -> None:
    checkout = tmp_path / "mono"
    (checkout / "pkg").mkdir(parents=True)
    repo = resolve(spec("mono", path=checkout, root="pkg"), cache_dir=tmp_path / "cache")
    assert repo.root == (checkout / "pkg").resolve()
    assert repo.checkout == checkout.resolve()
    assert resolve(spec("mono", path=checkout, root="."), cache_dir=tmp_path / "cache").root == checkout.resolve()
