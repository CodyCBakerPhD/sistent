"""The ``files`` aspect: presence (and optionally identity) of community and policy files.

Each configured entry is one or more case-insensitive globs separated by ``|`` (any-of), searched in
``search_dirs`` in order of the alternatives: ``LICENSE*|COPYING*`` accepts ``LICENSE``, ``license.txt`` or
``COPYING`` anywhere in ``.``, ``.github`` or ``docs``. Entries listed in ``identical`` must also match main's
content after identity substitution, removal of ``ignore_patterns`` (years by default) and whitespace collapsing.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sistent.aspects.base import Aspect
from sistent.compare.text import unified_diff
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject
from sistent.options import BaseOptions, OptionsError
from sistent.repository import RepoContext

REQUIRED_MODES: tuple[str, ...] = ("from-main", "all")


def alternatives(entry: str) -> list[str]:
    """The any-of globs of one ``files`` entry (``"LICENSE*|COPYING*"`` -> ``["LICENSE*", "COPYING*"]``)."""
    return [alt.strip() for alt in entry.split("|") if alt.strip()]


@dataclass(frozen=True, kw_only=True)
class FilesOptions(BaseOptions):
    """Options of the ``files`` aspect type."""

    files: list[str] = field(
        metadata={
            "help": "Entries to look for; each is one or more case-insensitive globs separated by '|' meaning any-of "
            "(e.g. 'LICENSE*|COPYING*'), searched in every search_dirs directory."
        },
    )
    required: str = field(
        default="from-main",
        metadata={
            "help": "'from-main': an entry is required in a satellite only when main has it; "
            "'all': every entry is required everywhere (main included, reported by the self-check)."
        },
    )
    identical: list[str] = field(
        default_factory=list,
        metadata={"help": "Entries (from files) whose normalised content must equal main's."},
    )
    search_dirs: list[str] = field(
        default_factory=lambda: [".", ".github", "docs"],
        metadata={"help": "Directories searched for every entry, non-recursively."},
    )
    ignore_patterns: list[str] = field(
        default_factory=list,
        metadata={"help": "Regular expressions removed from the text before the identical comparison (years, ...)."},
    )

    def __post_init__(self) -> None:
        if self.required not in REQUIRED_MODES:
            valid = ", ".join(repr(m) for m in REQUIRED_MODES)
            raise OptionsError(f"required: expected one of {valid}, got {self.required!r}")
        if not self.files:
            raise OptionsError("files: at least one entry is required")
        for entry in self.files:
            if not alternatives(entry):
                raise OptionsError(f"files: entry {entry!r} contains no glob")
        unknown = [entry for entry in self.identical if entry not in self.files]
        if unknown:
            raise OptionsError(f"identical: {', '.join(repr(e) for e in unknown)} not listed in files")
        for pattern in self.ignore_patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise OptionsError(f"ignore_patterns: invalid regular expression {pattern!r}: {exc}") from None


class FilesAspect(Aspect):
    """Which community files exist, where, and (for ``identical`` entries) whether they match main."""

    type_name = "files"
    options_cls = FilesOptions
    description = "Presence and location of community files (LICENSE, CONTRIBUTING, ...), optionally identical to main."

    def __init__(self, name: str, options: BaseOptions) -> None:
        super().__init__(name, options)
        self._ignore_res = [re.compile(p) for p in self.opts.ignore_patterns]

    @property
    def opts(self) -> FilesOptions:
        assert isinstance(self.options, FilesOptions)
        return self.options

    # ----- extraction -----------------------------------------------------------------------------------------

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """Snapshot data.

        ``{"is_main": bool, "entries": {"<entry>": {"paths": [...], "alternative": "<glob>" | null}},
        "identical": {"<entry>": {"path": "...", "normalized": "<text>", "hash": "<sha1>"}}}``. For each entry the
        first alternative with a match wins and all its matches are listed (directories never match). ``is_main``
        lets :meth:`self_check`, which the pipeline runs on every repository, report main's own absences once.
        """
        opts = self.opts
        entries: dict[str, Any] = {}
        identical: dict[str, Any] = {}
        for entry in opts.files:
            paths: list[str] = []
            chosen: str | None = None
            for alt in alternatives(entry):
                matches = [rel for rel in ctx.glob(alt, dirs=opts.search_dirs) if not ctx.repo.is_dir(rel)]
                if matches:
                    paths, chosen = matches, alt
                    break
            entries[entry] = {"paths": [ctx.substitute(p, where=p) for p in paths], "alternative": chosen}
            if entry in opts.identical and paths:
                path = paths[0]
                normalized = self._normalize(ctx, ctx.read_text(path), where=path)
                identical[entry] = {
                    "path": ctx.substitute(path, where=path),
                    "normalized": normalized,
                    "hash": hashlib.sha1(normalized.encode("utf-8")).hexdigest(),
                }
        return {"is_main": ctx.is_main, "entries": entries, "identical": identical}

    def _normalize(self, ctx: RepoContext, text: str, *, where: str) -> str:
        """Identity-substitute, strip ``ignore_patterns``, collapse whitespace per line and drop blank lines."""
        text = ctx.substitute(text, where=where)
        for pattern in self._ignore_res:
            text = pattern.sub("", text)
        lines = (" ".join(line.split()) for line in text.splitlines())
        return "\n".join(line for line in lines if line)

    # ----- comparison -----------------------------------------------------------------------------------------

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Presence findings per entry, then content findings for ``identical`` entries present on both sides.

        * main has it, other does not -> ``missing``/``file`` (downstream, error by default);
        * other has it, main does not -> ``extra``/``file`` (upstream);
        * neither has it and ``required = "all"`` -> ``missing``/``file`` located at the first alternative glob;
        * both have it but at different paths -> ``moved``/``file`` (info);
        * ``identical`` entry whose normalised text differs -> ``differs``/``file`` (downstream, unified diff).
        """
        opts = self.opts
        main_entries: dict[str, Any] = _mapping(main.data.get("entries"))
        other_entries: dict[str, Any] = _mapping(other.data.get("entries"))
        out: list[Finding] = []
        for entry in [*main_entries, *(e for e in other_entries if e not in main_entries)]:
            main_paths = _paths(main_entries.get(entry))
            other_paths = _paths(other_entries.get(entry))
            if main_paths and not other_paths:
                out.append(
                    self.finding(
                        repo=other.repo,
                        kind=Kind.MISSING,
                        subject=Subject.FILE,
                        locator=main_paths[0],
                        message=f"{_basename(main_paths[0])} missing (main: {main_paths[0]})",
                        content_key=entry,
                    )
                )
            elif other_paths and not main_paths:
                out.append(
                    self.finding(
                        repo=other.repo,
                        kind=Kind.EXTRA,
                        subject=Subject.FILE,
                        locator=other_paths[0],
                        message=f"{_basename(other_paths[0])} only in repo ({other_paths[0]})",
                        content_key=entry,
                    )
                )
            elif not main_paths and not other_paths:
                if opts.required == "all":
                    pattern = _first_alternative(entry)
                    out.append(
                        self.finding(
                            repo=other.repo,
                            kind=Kind.MISSING,
                            subject=Subject.FILE,
                            locator=pattern,
                            message=f"{pattern} missing (required by configuration; absent in main too)",
                            content_key=entry,
                            option=self.option_ref("required"),
                        )
                    )
            elif not set(main_paths) & set(other_paths):
                out.append(
                    self.finding(
                        repo=other.repo,
                        kind=Kind.MOVED,
                        subject=Subject.FILE,
                        locator=main_paths[0],
                        message=f"{_basename(main_paths[0])} found at {other_paths[0]} (main: {main_paths[0]})",
                        detail=f"main: {main_paths[0]} ; repo: {other_paths[0]}",
                        detail_kind="text",
                        content_key=entry,
                    )
                )

        main_identical: dict[str, Any] = _mapping(main.data.get("identical"))
        other_identical: dict[str, Any] = _mapping(other.data.get("identical"))
        for entry, main_info in main_identical.items():
            other_info = other_identical.get(entry)
            if not isinstance(main_info, dict) or not isinstance(other_info, dict):
                continue
            main_text = str(main_info.get("normalized", ""))
            other_text = str(other_info.get("normalized", ""))
            if main_text == other_text:
                continue
            main_path = str(main_info.get("path", ""))
            other_path = str(other_info.get("path", ""))
            out.append(
                self.finding(
                    repo=other.repo,
                    kind=Kind.DIFFERS,
                    subject=Subject.FILE,
                    locator=main_path,
                    message=f"{_basename(main_path)} differs from main's",
                    detail=unified_diff(
                        main_text.splitlines(),
                        other_text.splitlines(),
                        fromfile=f"main:{main_path}",
                        tofile=f"{other.repo}:{other_path}",
                    ),
                    detail_kind="diff",
                    content_key=entry,
                    direction=Direction.DOWNSTREAM,
                    severity=Severity.WARNING,
                    option=self.option_ref("identical"),
                )
            )
        return out

    def self_check(self, snap: Snapshot) -> list[Finding]:
        """With ``required = "all"``, main's own absent entries (direction ``none``, ``warning``).

        Satellites are covered by :meth:`compare`, so snapshots not taken from main yield nothing here.
        """
        if self.opts.required != "all" or not snap.data.get("is_main"):
            return []
        out: list[Finding] = []
        for entry, info in _mapping(snap.data.get("entries")).items():
            if _paths(info):
                continue
            pattern = _first_alternative(entry)
            out.append(
                self.finding(
                    repo=snap.repo,
                    kind=Kind.MISSING,
                    subject=Subject.FILE,
                    locator=pattern,
                    message=f"{pattern} missing (required by configuration)",
                    content_key=entry,
                    direction=Direction.NONE,
                    severity=Severity.WARNING,
                    option=self.option_ref("required"),
                )
            )
        return out


# ----- helpers -----------------------------------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _paths(info: Any) -> list[str]:
    if not isinstance(info, dict):
        return []
    paths = info.get("paths")
    return [str(p) for p in paths] if isinstance(paths, list) else []


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _first_alternative(entry: str) -> str:
    alts = alternatives(entry)
    return alts[0] if alts else entry


__all__ = ["REQUIRED_MODES", "FilesAspect", "FilesOptions", "alternatives"]
