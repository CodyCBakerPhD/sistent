"""The ``tree`` aspect: folder layout as two sets, directories and files.

Directories are recorded up to ``depth`` components, files up to ``file_depth`` components plus everything matching
an ``include`` glob (``.github/**``, ``docs/*``). Every path component is identity-substituted so ``src/neuroconv``
and ``src/roiextractors`` both become ``src/{{name}}``. Comparison is a set difference per kind; a path missing on
one side and present with the same basename under another parent on the other side is one ``moved`` finding
(flat versus ``src`` layout), and children of an unmatched directory are never reported on their own.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sistent.aspects.base import Aspect
from sistent.compare.sets import diff_sets
from sistent.model import Finding, Kind, Snapshot, Subject
from sistent.options import BaseOptions, OptionsError
from sistent.repository import Entry, RepoContext


@dataclass(frozen=True, kw_only=True)
class TreeOptions(BaseOptions):
    """Options of the ``tree`` aspect type."""

    depth: int = field(
        default=2,
        metadata={"help": "Record directories up to this many path components deep (2 = 'src/pkg')."},
    )
    file_depth: int = field(
        default=1,
        metadata={"help": "Record files up to this many path components deep (1 = repository root only)."},
    )
    include: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Extra file globs recorded regardless of file_depth, matched case-insensitively against the "
            "relative path; '*' stays within one component, '**' spans directories (e.g. '.github/**', 'docs/*')."
        },
    )
    ignore: list[str] = field(
        default_factory=list,
        metadata={
            "help": "Names (or paths, when the glob contains '/') left out of the layout: build artefacts, caches, "
            "skills directories. As for every aspect, findings whose locator matches are suppressed too."
        },
    )
    use_git: bool = field(
        default=True,
        metadata={
            "help": "List files with git (tracked plus untracked-but-not-ignored) when the repository is a git "
            "checkout; false always walks the filesystem, so .gitignore is not applied."
        },
    )

    def __post_init__(self) -> None:
        if self.depth < 0:
            raise OptionsError(f"depth: expected a non-negative integer, got {self.depth}")
        if self.file_depth < 0:
            raise OptionsError(f"file_depth: expected a non-negative integer, got {self.file_depth}")
        for pattern in self.include:
            if not pattern.strip():
                raise OptionsError("include: empty glob")


class TreeAspect(Aspect):
    """Directories and files of a repository, compared as sets of identity-substituted paths."""

    type_name = "tree"
    options_cls = TreeOptions
    description = "Folder layout: directories up to a depth, root files and included file globs, compared as sets."

    @property
    def opts(self) -> TreeOptions:
        assert isinstance(self.options, TreeOptions)
        return self.options

    # ----- extraction -----------------------------------------------------------------------------------------

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """``{"dirs": ["src/", "src/{{name}}/", ...], "files": ["pyproject.toml", ...]}``, both sorted."""
        opts = self.opts
        patterns = [compile_glob(g) for g in opts.include]
        limit = self._scan_depth()
        if opts.use_git:
            entries: Iterator[Entry] = ctx.iter_files(depth=limit, ignore=opts.ignore)
        else:
            entries = _walk(ctx, depth=limit, ignore=opts.ignore)
        dirs: set[str] = set()
        files: set[str] = set()
        for entry in entries:
            if entry.is_dir:
                if entry.depth <= opts.depth:
                    dirs.add(_placeholder(ctx, entry.rel) + "/")
            elif entry.depth <= opts.file_depth or any(p.match(entry.rel) for p in patterns):
                files.add(_placeholder(ctx, entry.rel))
        return {"dirs": sorted(dirs), "files": sorted(files)}

    def _scan_depth(self) -> int | None:
        """How deep the listing must go: unbounded when an include glob contains ``**``."""
        opts = self.opts
        deepest = max(opts.depth, opts.file_depth)
        for pattern in opts.include:
            if "**" in pattern:
                return None
            deepest = max(deepest, pattern.strip("/").count("/") + 1)
        return deepest

    # ----- comparison -----------------------------------------------------------------------------------------

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """``missing``/``extra``/``moved`` findings with subject ``path``.

        Missing and extra paths with the same basename under different parents are paired into one ``moved`` finding
        first; then, following the highest-node rule, a missing or extra path whose ancestor is itself unmatched
        (missing, extra or moved) is dropped, and a moved pair whose parents moved together is dropped too.
        """
        dirs = diff_sets(_strings(main.data.get("dirs")), _strings(other.data.get("dirs")))
        files = diff_sets(_strings(main.data.get("files")), _strings(other.data.get("files")))
        missing = [*dirs.missing, *files.missing]
        extra = [*dirs.extra, *files.extra]
        moved = _pair_moves(missing, extra)
        moved_main = {m for m, _ in moved}
        moved_other = {o for _, o in moved}
        missing = [p for p in missing if p not in moved_main]
        extra = [p for p in extra if p not in moved_other]
        main_roots = set(missing) | moved_main
        other_roots = set(extra) | moved_other

        out: list[Finding] = []
        for path in missing:
            if _has_ancestor(path, main_roots):
                continue
            out.append(
                self.finding(
                    repo=other.repo,
                    kind=Kind.MISSING,
                    subject=Subject.PATH,
                    locator=path,
                    message=f"{_noun(path)} missing: {path}",
                    content_key=path,
                )
            )
        for main_path, other_path in moved:
            if _has_ancestor(main_path, moved_main) and _has_ancestor(other_path, moved_other):
                continue
            out.append(
                self.finding(
                    repo=other.repo,
                    kind=Kind.MOVED,
                    subject=Subject.PATH,
                    locator=main_path,
                    message=f"{_noun(main_path)} moved: {main_path} -> {other_path}",
                    detail=f"main: {main_path} ; repo: {other_path}",
                    detail_kind="text",
                    content_key=main_path,
                )
            )
        for path in extra:
            if _has_ancestor(path, other_roots):
                continue
            out.append(
                self.finding(
                    repo=other.repo,
                    kind=Kind.EXTRA,
                    subject=Subject.PATH,
                    locator=path,
                    message=f"{_noun(path)} only in repo: {path}",
                    content_key=path,
                )
            )
        return out


# ----- helpers -----------------------------------------------------------------------------------------------------


def compile_glob(pattern: str) -> re.Pattern[str]:
    """Translate a path glob into a case-insensitive regex.

    ``*`` and ``?`` do not cross ``/``; ``**`` matches any number of components (``**/`` also matches none);
    ``[...]`` character classes are kept (``[!...]`` negates). The regex must match the whole relative path.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        elif ch == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape(ch))
                i += 1
            else:
                inner = pattern[i + 1 : end]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner.replace("\\", "\\\\") + "]")
                i = end + 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("".join(out) + r"\Z", re.IGNORECASE)


def _placeholder(ctx: RepoContext, rel: str) -> str:
    """Identity-substitute each path component separately (``src/neuroconv`` -> ``src/{{name}}``)."""
    return "/".join(ctx.substitute(part, where=rel) for part in rel.split("/"))


def _is_ignored(rel: str, name: str, patterns: Sequence[str]) -> bool:
    """Same rule as :meth:`Repository.iter_files`: bare globs match a component, globs with ``/`` a path prefix."""
    for pattern in patterns:
        p = pattern.rstrip("/")
        if "/" in p:
            if fnmatch.fnmatchcase(rel, p) or rel.startswith(p + "/"):
                return True
        elif fnmatch.fnmatchcase(name, p):
            return True
    return False


def _walk(ctx: RepoContext, *, depth: int | None, ignore: Sequence[str]) -> Iterator[Entry]:
    """Filesystem listing through ``ctx.glob`` (``use_git = false``): the same shape as ``ctx.iter_files``.

    Ignored names are pruned before descending, symbolic links are listed as files and never followed, and
    directories are derived from the files inside them so empty directories are not reported.
    """
    files: list[str] = []
    stack: list[str] = [""]
    while stack:
        rel_dir = stack.pop()
        for rel in ctx.glob("*", dirs=[rel_dir or "."]):
            name = rel.rsplit("/", 1)[-1]
            if _is_ignored(rel, name, ignore):
                continue
            if not ctx.repo.is_symlink(rel) and ctx.repo.is_dir(rel):
                stack.append(rel)
            else:
                files.append(rel)
    entries: dict[str, bool] = {}
    for rel in files:
        parts = rel.split("/")
        for i in range(1, len(parts)):
            entries.setdefault("/".join(parts[:i]), True)
        entries[rel] = False
    for rel in sorted(entries):
        entry = Entry(rel, entries[rel])
        if depth is None or entry.depth <= depth:
            yield entry


def _strings(value: Any) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _noun(path: str) -> str:
    return "directory" if path.endswith("/") else "file"


def _basename(path: str) -> str:
    """Last component; directories keep their trailing slash so they only pair with directories."""
    trimmed = path.rstrip("/")
    name = trimmed.rsplit("/", 1)[-1]
    return name + "/" if path.endswith("/") else name


def _parent(path: str) -> str:
    trimmed = path.rstrip("/")
    return trimmed.rsplit("/", 1)[0] + "/" if "/" in trimmed else ""


def _ancestors(path: str) -> list[str]:
    parts = path.rstrip("/").split("/")
    return ["/".join(parts[:i]) + "/" for i in range(1, len(parts))]


def _has_ancestor(path: str, roots: set[str]) -> bool:
    return any(a in roots for a in _ancestors(path))


def _pair_moves(missing: Sequence[str], extra: Sequence[str]) -> list[tuple[str, str]]:
    """``(main_path, other_path)`` pairs sharing a basename that is unique on both sides, under different parents."""
    by_name_main: dict[str, list[str]] = {}
    for path in missing:
        by_name_main.setdefault(_basename(path), []).append(path)
    by_name_other: dict[str, list[str]] = {}
    for path in extra:
        by_name_other.setdefault(_basename(path), []).append(path)
    pairs: list[tuple[str, str]] = []
    for name, mains in by_name_main.items():
        others = by_name_other.get(name)
        if others is None or len(mains) != 1 or len(others) != 1:
            continue
        if _parent(mains[0]) != _parent(others[0]):
            pairs.append((mains[0], others[0]))
    return pairs


__all__ = ["TreeAspect", "TreeOptions", "compile_glob"]
