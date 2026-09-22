"""Read-only access to a materialised repository, identity probing, and the per-extraction context.

A :class:`Repository` is a directory on disk (a local ``path`` or a cached clone). Aspects never touch the filesystem
directly: they read through a :class:`RepoContext`, which records the files an extraction depended on (for the
snapshot fingerprint) and carries the :class:`~sistent.compare.text.Substituter` for that repository.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tomllib
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sistent.compare.text import Substituter
from sistent.model import Identity

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_MIN_ALIAS = 4
_NOT_PACKAGES = frozenset(
    {
        "tests",
        "test",
        "testing",
        "docs",
        "doc",
        "examples",
        "example",
        "scripts",
        "tools",
        "benchmarks",
        "build",
        "dist",
    }
)
"""Directory names that carry an ``__init__.py`` in many repos without being the importable package."""


@dataclass(frozen=True)
class Entry:
    """One path inside a repository, relative to its root, POSIX separators; directories end without a slash."""

    rel: str
    is_dir: bool

    @property
    def depth(self) -> int:
        return self.rel.count("/") + 1

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]


class Repository:
    """A repository checkout on disk.

    ``root`` is the directory analysed (it includes ``[repos.X].root`` for monorepos); ``checkout`` is the real
    git working tree (``root`` or one of its parents) used for git commands.
    """

    def __init__(self, name: str, root: Path, *, checkout: Path | None = None) -> None:
        self.name = name
        self.root = Path(root).resolve()
        self.checkout = Path(checkout).resolve() if checkout else self.root
        self._text_cache: dict[str, str] = {}
        self._head: str | bool | None = False
        self._git_ok: bool | None = None

    def __repr__(self) -> str:
        return f"Repository({self.name!r}, {str(self.root)!r})"

    # ----- files ----------------------------------------------------------------------------------------------

    def path(self, rel: str) -> Path:
        return self.root / rel

    def exists(self, rel: str) -> bool:
        return self.path(rel).exists()

    def is_dir(self, rel: str) -> bool:
        return self.path(rel).is_dir()

    def is_symlink(self, rel: str) -> bool:
        return self.path(rel).is_symlink()

    def read_bytes(self, rel: str) -> bytes:
        return self.path(rel).read_bytes()

    def read_text(self, rel: str, *, errors: str = "replace") -> str:
        """UTF-8 text with a BOM stripped and CRLF line endings normalised to LF; memoised per path."""
        cached = self._text_cache.get(rel)
        if cached is None:
            cached = self.read_bytes(rel).decode("utf-8", errors=errors).lstrip("﻿").replace("\r\n", "\n")
            self._text_cache[rel] = cached
        return cached

    def glob(self, pattern: str, *, dirs: Sequence[str] = (".",)) -> list[str]:
        """Case-insensitive glob of ``pattern`` inside each of ``dirs`` (non-recursive); sorted relative paths."""
        out: list[str] = []
        for d in dirs:
            base = self.root if d in (".", "") else self.root / d
            if not base.is_dir():
                continue
            try:
                names = sorted(os.listdir(base))
            except OSError:
                continue
            for name in names:
                if fnmatch.fnmatchcase(name.casefold(), pattern.casefold()):
                    rel = name if d in (".", "") else f"{d.strip('/')}/{name}"
                    out.append(rel)
        return sorted(out)

    def iter_files(self, *, depth: int | None = None, ignore: Sequence[str] = ()) -> Iterator[Entry]:
        """Yield tracked/untracked-but-not-ignored files and their directories, up to ``depth`` components.

        Uses ``git ls-files`` when the checkout is a git work tree (respects ``.gitignore``), else a filesystem walk.
        ``ignore`` globs are matched against every path component (patterns without ``/``) or against the full
        relative path and its prefixes (patterns with ``/``). Directories are derived from the files inside them, so
        empty directories are not reported. Output is sorted; directories come before their content.
        """
        files = self._git_files()
        if files is None:
            files = list(self._walk_files(ignore))
        entries: dict[str, bool] = {}
        for rel in files:
            parts = rel.split("/")
            if any(_ignored("/".join(parts[: i + 1]), parts[i], ignore) for i in range(len(parts))):
                continue
            for i in range(1, len(parts)):
                entries.setdefault("/".join(parts[:i]), True)
            entries[rel] = False
        for rel in sorted(entries):
            entry = Entry(rel, entries[rel])
            if depth is None or entry.depth <= depth:
                yield entry

    def _walk_files(self, ignore: Sequence[str]) -> Iterator[str]:
        stack: list[str] = [""]
        while stack:
            rel_dir = stack.pop()
            base = self.root / rel_dir if rel_dir else self.root
            try:
                with os.scandir(base) as it:
                    children = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for child in children:
                rel = f"{rel_dir}/{child.name}" if rel_dir else child.name
                if _ignored(rel, child.name, ignore):
                    continue
                if child.is_dir(follow_symlinks=False):
                    stack.append(rel)
                elif child.is_file(follow_symlinks=False) or child.is_symlink():
                    yield rel

    # ----- git ------------------------------------------------------------------------------------------------

    @property
    def is_git(self) -> bool:
        if self._git_ok is None:
            self._git_ok = (self.checkout / ".git").exists() and self.git("rev-parse", "--git-dir") is not None
        return self._git_ok

    def git(self, *args: str, timeout: float = 30.0) -> str | None:
        """Run ``git`` in the checkout; ``None`` when git is unavailable or the command fails."""
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=self.checkout,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def head(self) -> str | None:
        if self._head is False:
            out = self.git("rev-parse", "HEAD") if self.is_git else None
            self._head = out.strip() if out else None
        return self._head  # type: ignore[return-value]

    def _git_files(self) -> list[str] | None:
        if not self.is_git:
            return None
        out = self.git("ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", ".")
        if out is None:
            return None
        prefix = ""
        if self.root != self.checkout:
            prefix = self.root.relative_to(self.checkout).as_posix().rstrip("/") + "/"
        files: list[str] = []
        for item in out.split("\0"):
            if not item:
                continue
            # ls-files is run from the checkout directory when root is a sub-directory, so strip the prefix
            if prefix and item.startswith(prefix):
                item = item[len(prefix) :]
            elif prefix:
                continue
            files.append(item)
        return files


def _ignored(rel: str, name: str, patterns: Sequence[str]) -> bool:
    for pat in patterns:
        p = pat.rstrip("/")
        if "/" in p:
            if fnmatch.fnmatchcase(rel, p) or rel.startswith(p + "/"):
                return True
        elif fnmatch.fnmatchcase(name, p):
            return True
    return False


# ----- identity probes ---------------------------------------------------------------------------------------------

Probe = Callable[[Repository, "RepoHints"], dict[str, Any]]


@dataclass(frozen=True, kw_only=True)
class RepoHints:
    """What the configuration says about a repo, for identity probing (kept free of the config module)."""

    name: str
    url: str | None = None
    rev: str | None = None
    aliases: tuple[str, ...] = ()
    vars: dict[str, str] = field(default_factory=dict)


def probe_config(repo: Repository, hints: RepoHints) -> dict[str, Any]:
    out: dict[str, Any] = {"aliases": [hints.name, *hints.aliases], "forced": list(hints.aliases)}
    extra: dict[str, str] = {}
    for key, value in hints.vars.items():
        if key == "name":
            out["aliases"].append(value)
            out["forced"].append(value)
        elif key == "org":
            out["org"] = value
        elif key == "branch":
            out["branch"] = value
        else:
            extra[key] = value
    out["extra"] = extra
    return out


def probe_pyproject(repo: Repository, hints: RepoHints) -> dict[str, Any]:
    if not repo.exists("pyproject.toml"):
        return {}
    try:
        doc = tomllib.loads(repo.read_text("pyproject.toml"))
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    aliases: list[str] = []
    project = doc.get("project") if isinstance(doc.get("project"), dict) else {}
    name = project.get("name") if isinstance(project, dict) else None
    if isinstance(name, str) and name:
        aliases.extend(name_variants(name))
    tool = doc.get("tool") if isinstance(doc.get("tool"), dict) else {}
    aliases.extend(_import_packages(repo, tool if isinstance(tool, dict) else {}, aliases))
    return {"aliases": aliases}


def name_variants(name: str) -> list[str]:
    """``name`` plus its PEP 503 normalised form and ``-``/``_`` spellings."""
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    variants = [name, normalized, normalized.replace("-", "_"), name.replace("-", "_"), name.replace("_", "-")]
    return list(dict.fromkeys(variants))


def _import_packages(repo: Repository, tool: dict[str, Any], variants: Sequence[str]) -> list[str]:
    found: list[str] = []
    hatch = _dig(tool, "hatch", "build", "targets", "wheel", "packages")
    if isinstance(hatch, list):
        found.extend(str(p).rstrip("/").rsplit("/", 1)[-1] for p in hatch if isinstance(p, str))
    setuptools = tool.get("setuptools") if isinstance(tool.get("setuptools"), dict) else {}
    packages = setuptools.get("packages") if isinstance(setuptools, dict) else None
    if isinstance(packages, list):
        found.extend(str(p).split(".")[0] for p in packages if isinstance(p, str))
    package_dir = setuptools.get("package-dir") if isinstance(setuptools, dict) else None
    if isinstance(package_dir, dict):
        found.extend(k for k in package_dir if isinstance(k, str) and k)
    candidates: list[str] = []
    for base in ("src", "."):
        base_path = repo.root / base if base != "." else repo.root
        if not base_path.is_dir():
            continue
        try:
            for child in sorted(base_path.iterdir()):
                name = child.name
                if name.startswith(".") or name.casefold() in _NOT_PACKAGES:
                    continue
                if child.is_dir() and (child / "__init__.py").exists():
                    candidates.append(name)
        except OSError:
            continue
        if candidates:
            break  # a src/ layout never has import packages at the root as well
    if len(candidates) > 3:
        folded = {v.casefold() for v in variants}
        candidates = [c for c in candidates if c.casefold() in folded]
    found.extend(candidates)
    return found


def _dig(doc: Any, *keys: str) -> Any:
    cur = doc
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


_REMOTE_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host1>[^/:]+)(?::\d+)?/|(?:[^@]+@)?(?P<host2>[^:/]+):)"
    r"(?P<path>.+?)(?:\.git)?/?$"
)


def parse_remote(url: str) -> tuple[str, str, str] | None:
    """``(host, org, repo)`` from an https/ssh/scp-style git URL, or ``None``."""
    stripped = url.strip()
    if stripped.lower().startswith("file:") or ("://" not in stripped and ":" not in stripped):
        return None  # local paths and file:// urls carry no org/host identity
    m = _REMOTE_RE.match(stripped)
    if not m:
        return None
    host = m.group("host1") or m.group("host2")
    parts = [p for p in m.group("path").split("/") if p]
    if len(parts) < 2:
        return None
    return host, parts[-2], parts[-1]


def probe_remote(repo: Repository, hints: RepoHints) -> dict[str, Any]:
    url = hints.url
    if not url and repo.is_git:
        out = repo.git("remote", "get-url", "origin")
        url = out.strip() if out else None
    if not url:
        return {}
    parsed = parse_remote(url)
    if parsed is None:
        return {}
    host, org, tail = parsed
    return {"aliases": [tail], "org": org, "slug": f"{host}/{org}/{tail}"}


def probe_branch(repo: Repository, hints: RepoHints) -> dict[str, Any]:
    if hints.rev and not _SHA_RE.match(hints.rev):
        return {"branch": hints.rev}
    if not repo.is_git:
        return {}
    out = repo.git("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if out and out.strip():
        return {"branch": out.strip().removeprefix("origin/")}
    out = repo.git("rev-parse", "--abbrev-ref", "HEAD")
    if out and out.strip() and out.strip() != "HEAD":
        return {"branch": out.strip()}
    return {}


PROBES: list[Probe] = [probe_config, probe_pyproject, probe_remote, probe_branch]


def build_identity(repo: Repository, hints: RepoHints, *, probes: Iterable[Probe] = PROBES) -> Identity:
    """Run every probe (a failing probe contributes nothing) and merge the results into an :class:`Identity`.

    Aliases shorter than four characters are kept only when they come from the configuration (``aliases`` or
    ``vars.name``). Config values win over probed values for ``org`` and ``branch``.
    """
    aliases: list[str] = []
    forced: set[str] = set()
    org: str | None = None
    branch: str | None = None
    slug: str | None = None
    extra: dict[str, str] = {}
    for probe in probes:
        try:
            result = probe(repo, hints)
        except Exception:  # a probe must never break a run
            continue
        aliases.extend(a for a in result.get("aliases", []) if isinstance(a, str))
        forced.update(a for a in result.get("forced", []) if isinstance(a, str))
        org = org or result.get("org")
        branch = branch or result.get("branch")
        slug = slug or result.get("slug")
        extra.update(result.get("extra", {}))
    seen: dict[str, str] = {}
    for alias in aliases:
        alias = alias.strip()
        if not alias:
            continue
        if len(alias) < _MIN_ALIAS and alias not in forced:
            continue
        seen.setdefault(alias.casefold(), alias)
    ordered = tuple(sorted(seen.values(), key=lambda a: (-len(a), a)))
    return Identity(name=hints.name, aliases=ordered, org=org, branch=branch, slug=slug, extra=extra)


# ----- extraction context ------------------------------------------------------------------------------------------


@dataclass(kw_only=True)
class RepoContext:
    """Everything one aspect needs to extract a snapshot from one repository."""

    repo: Repository
    identity: Identity
    is_main: bool
    subst: Substituter
    sources: set[str] = field(default_factory=set)
    """Files read through :meth:`read_text`; recorded for the snapshot fingerprint."""
    ignored: list[str] = field(default_factory=list)
    """Things the aspect dropped because of configuration (sections, files), for ``--explain``."""

    @property
    def name(self) -> str:
        return self.repo.name

    def exists(self, rel: str) -> bool:
        return self.repo.exists(rel)

    def read_text(self, rel: str) -> str:
        self.sources.add(rel)
        return self.repo.read_text(rel)

    def read_bytes(self, rel: str) -> bytes:
        self.sources.add(rel)
        return self.repo.read_bytes(rel)

    def glob(self, pattern: str, *, dirs: Sequence[str] = (".",)) -> list[str]:
        return self.repo.glob(pattern, dirs=dirs)

    def iter_files(self, *, depth: int | None = None, ignore: Sequence[str] = ()) -> Iterator[Entry]:
        return self.repo.iter_files(depth=depth, ignore=ignore)

    def substitute(self, text: str, *, where: str = "") -> str:
        return self.subst(text, where=where)
