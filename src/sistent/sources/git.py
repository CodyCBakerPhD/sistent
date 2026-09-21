"""Shallow git materialisation of ``url`` repositories into the cache directory.

Each ``(url, rev)`` pair gets its own checkout under ``cache_dir`` (see :func:`cache_key`). A new checkout is built
in ``<dir>.tmp`` and renamed into place only after the fetch and checkout succeeded, so an interrupted run never
leaves a half-built directory behind. A per-checkout ``<dir>.lock`` file (``fcntl.flock``; a no-op where ``fcntl``
is unavailable) serialises concurrent materialisation of the same key.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from sistent.repository import parse_remote

if sys.platform != "win32":
    import fcntl

GIT_ENV: dict[str, str] = {"GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
"""Environment overrides for every git call: never prompt for credentials, never download LFS objects."""

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_ABBREV_SHA = re.compile(r"^[0-9a-f]{7,39}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SourceError(Exception):
    """A repository could not be materialised; the message names the cause (and, from ``resolve``, the repo)."""


def git_available() -> bool:
    """Whether a ``git`` executable is on ``PATH``."""
    return shutil.which("git") is not None


def cache_key(url: str, rev: str | None) -> str:
    """Directory name for ``(url, rev)`` under the cache: ``<host>__<org>__<repo>-<sha1 prefix>``.

    The slug is derived from the URL (a ``file://`` URL uses its last two path components) and sanitised to
    ``[A-Za-z0-9._-]``; the digest makes different URLs or revisions with the same repo name distinct.
    """
    digest = hashlib.sha1((url + "\n" + (rev or "")).encode("utf-8")).hexdigest()[:10]
    return f"{_slug(url)}-{digest}"


def _slug(url: str) -> str:
    if url.startswith("file://"):
        parts = [p for p in url[len("file://") :].split("/") if p]
    else:
        parsed = parse_remote(url)
        parts = list(parsed) if parsed else [p for p in re.split(r"[/:]+", url) if p]
    parts = parts[-3:] if not url.startswith("file://") else parts[-2:]
    if parts:
        parts[-1] = parts[-1].removesuffix(".git")
    cleaned = [_UNSAFE.sub("_", p).strip("._-") or "_" for p in parts]
    return "__".join(cleaned) or "repo"


def materialise(url: str, rev: str | None, cache_dir: Path, *, fetch: bool = True, timeout: int = 120) -> Path:
    """Ensure ``cache_dir / cache_key(url, rev)`` holds a checkout of ``rev`` (remote ``HEAD`` when ``None``).

    With ``fetch`` false an existing checkout is returned untouched and a missing one is a :class:`SourceError`.
    Otherwise a new checkout is created (``git init``, ``remote add``, ``fetch --depth 1``, ``checkout --detach``)
    or an existing one is updated (``remote set-url``, fetch, checkout); a full 40-hex ``rev`` whose commit is
    already present skips the fetch. Every git call is bounded by ``timeout`` seconds.
    """
    cache_dir = Path(cache_dir).expanduser()
    target = cache_dir / cache_key(url, rev)
    if not fetch:
        if not target.is_dir():
            raise SourceError(f"{url} ({rev or 'HEAD'}) is not cached under {cache_dir}; run without --no-fetch")
        return target
    if not git_available():
        raise SourceError("git executable not found on PATH")
    cache_dir.mkdir(parents=True, exist_ok=True)
    with _locked(target.with_name(target.name + ".lock")):
        if target.is_dir():
            _update(target, url, rev, timeout=timeout)
        else:
            _create(target, url, rev, timeout=timeout)
    return target


def _create(target: Path, url: str, rev: str | None, *, timeout: int) -> None:
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    done = False
    try:
        _git(["init", "-q"], cwd=tmp, timeout=timeout)
        _git(["remote", "add", "origin", url], cwd=tmp, timeout=timeout)
        _fetch_and_checkout(tmp, url, rev, timeout=timeout)
        done = True
    finally:
        if not done:
            shutil.rmtree(tmp, ignore_errors=True)
    os.replace(tmp, target)


def _update(target: Path, url: str, rev: str | None, *, timeout: int) -> None:
    _git(["remote", "set-url", "origin", url], cwd=target, timeout=timeout)
    if rev is not None and _FULL_SHA.match(rev) and _has_commit(target, rev, timeout=timeout):
        head = _run(["rev-parse", "HEAD"], cwd=target, timeout=timeout)
        if head.returncode != 0 or head.stdout.strip() != rev:
            _git(["checkout", "-q", "--detach", rev], cwd=target, timeout=timeout)
        return
    _fetch_and_checkout(target, url, rev, timeout=timeout)


def _has_commit(cwd: Path, sha: str, *, timeout: int) -> bool:
    return _run(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=cwd, timeout=timeout).returncode == 0


def _fetch_and_checkout(cwd: Path, url: str, rev: str | None, *, timeout: int) -> None:
    proc = _run(["fetch", "-q", "--depth", "1", "origin", rev or "HEAD"], cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        hint = ""
        if rev is not None and _ABBREV_SHA.match(rev):
            hint = " (abbreviated commit SHAs cannot be fetched; use the full 40-character SHA)"
        raise SourceError(f"git fetch of {rev or 'HEAD'} from {url} failed: {_stderr(proc)}{hint}")
    _git(["checkout", "-q", "--detach", "FETCH_HEAD"], cwd=cwd, timeout=timeout)


def _run(args: Sequence[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run ``git *args`` in ``cwd``; process failures are left to the caller, everything else is a SourceError."""
    command = ["git", *args]
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **GIT_ENV},
        )
    except subprocess.TimeoutExpired:
        raise SourceError(f"git {args[0]} timed out after {timeout}s (cwd {cwd})") from None
    except OSError as exc:
        raise SourceError(f"cannot run git: {exc}") from None


def _git(args: Sequence[str], *, cwd: Path, timeout: int) -> str:
    proc = _run(args, cwd=cwd, timeout=timeout)
    if proc.returncode != 0:
        raise SourceError(f"git {' '.join(args)} failed: {_stderr(proc)}")
    return proc.stdout


def _stderr(proc: subprocess.CompletedProcess[str]) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    return text.splitlines()[-1] if text else f"exit status {proc.returncode}"


@contextmanager
def _locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` (no-op where ``fcntl`` is unavailable)."""
    if sys.platform == "win32":
        yield
        return
    with open(lock_path, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["GIT_ENV", "SourceError", "cache_key", "git_available", "materialise"]
