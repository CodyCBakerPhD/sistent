"""``precommit`` aspect type: the repositories, hooks and pinned revisions of ``.pre-commit-config.yaml``.

Hook repositories are keyed by a *slug*: ``github.com`` URLs reduce to ``org/repo`` (``astral-sh/ruff-pre-commit``),
any other URL is kept whole (scheme included, trailing ``.git`` dropped), and the special ``local`` and ``meta``
repositories keep their names. Hooks are compared as sets per shared repository; a differing ``rev`` of a shared
repository is a downstream warning (main's pin is authoritative) unless ``compare_rev`` is off.

Pre-commit is optional tooling, so a satellite without the file only gets a ``warning`` (not the usual
``missing.file`` error).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sistent.aspects.base import Aspect, UnparseableFile
from sistent.compare.sets import diff_sets
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject, locator
from sistent.options import BaseOptions
from sistent.parsers import ParseError, yaml_
from sistent.repository import RepoContext

_GITHUB_RE = re.compile(
    r"^(?:(?:https?|ssh|git)://)?(?:[^@/:]+@)?(?:www\.)?github\.com[/:](?P<path>[^/]+/[^/]+?)(?:\.git)?/?$",
    re.IGNORECASE,
)
_SPECIAL = frozenset({"local", "meta"})


@dataclass(frozen=True, kw_only=True)
class PrecommitOptions(BaseOptions):
    """Options of the ``precommit`` aspect type."""

    file: str = field(
        default=".pre-commit-config.yaml",
        metadata={"help": "pre-commit configuration file, relative to the repository root."},
    )
    compare_rev: bool = field(
        default=True,
        metadata={"help": "Report a shared hook repository whose pinned rev differs from main."},
    )


class PrecommitAspect(Aspect):
    """Compare pre-commit hook repositories, hooks and revisions; see the module docstring."""

    type_name: ClassVar[str] = "precommit"
    options_cls: ClassVar[type[BaseOptions]] = PrecommitOptions
    description: ClassVar[str] = "pre-commit hook repositories, hook ids and pinned revisions."
    default_severity: ClassVar[dict[str, str]] = {"missing.file": "warning"}

    def __init__(self, name: str, options: BaseOptions) -> None:
        if not isinstance(options, PrecommitOptions):
            raise TypeError(f"{type(self).__name__} requires PrecommitOptions, got {type(options).__name__}")
        super().__init__(name, options)
        self.opts: PrecommitOptions = options

    # ----- extraction -----------------------------------------------------------------------------------------

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """``{"present": bool, "repos": {"<slug>": {"rev": str | None, "hooks": [ids]}}}`` in file order."""
        file = self.opts.file
        if not ctx.exists(file):
            return {"present": False, "repos": {}}
        try:
            doc = yaml_.load(ctx.read_text(file), source=file)
        except ParseError as exc:
            raise UnparseableFile(file, exc.reason) from exc
        if doc is None:
            return {"present": True, "repos": {}}
        if not isinstance(doc, Mapping):
            raise UnparseableFile(file, "top level is not a mapping")
        repos: dict[str, dict[str, Any]] = {}
        entries = doc.get("repos")
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, Mapping) or not isinstance(entry.get("repo"), str):
                    continue
                slug = ctx.substitute(repo_slug(entry["repo"]), where=locator(file))
                where = locator(file, slug, sep=":")
                record = repos.setdefault(slug, {"rev": _rev(entry.get("rev")), "hooks": []})
                hooks = entry.get("hooks")
                if not isinstance(hooks, list):
                    continue
                for hook in hooks:
                    if isinstance(hook, Mapping) and hook.get("id") is not None:
                        hook_id = ctx.substitute(str(hook["id"]), where=where)
                        if hook_id not in record["hooks"]:
                            record["hooks"].append(hook_id)
        return {"present": True, "repos": repos}

    # ----- comparison -----------------------------------------------------------------------------------------

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Findings for ``other`` relative to ``main``: file presence, repository set, hook sets, revisions."""
        file = self.opts.file
        repo = other.repo
        if not other.data.get("present"):
            if main.data.get("present"):
                return [
                    self.finding(
                        repo=repo,
                        kind=Kind.MISSING,
                        subject=Subject.FILE,
                        locator=locator(file),
                        message=f"file missing: {file}",
                    )
                ]
            return []
        if not main.data.get("present"):
            return []

        main_repos: Mapping[str, Mapping[str, Any]] = main.data.get("repos", {})
        other_repos: Mapping[str, Mapping[str, Any]] = other.data.get("repos", {})
        out: list[Finding] = []
        diff = diff_sets(list(main_repos), list(other_repos))
        for slug in diff.missing:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.KEY,
                    locator=locator(file, slug, sep=":"),
                    message=f"hook repository missing: {slug}",
                    content_key=slug,
                )
            )
        for slug in diff.extra:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.KEY,
                    locator=locator(file, slug, sep=":"),
                    message=f"hook repository only in repo: {slug}",
                    content_key=slug,
                )
            )
        for slug, _slug in diff.common:
            out.extend(self._compare_repo(repo, slug, main_repos[slug], other_repos[slug]))
        return out

    def _compare_repo(
        self, repo: str, slug: str, main_entry: Mapping[str, Any], other_entry: Mapping[str, Any]
    ) -> list[Finding]:
        file = self.opts.file
        where = locator(file, slug, sep=":")
        out: list[Finding] = []
        hooks = diff_sets([str(h) for h in main_entry.get("hooks", [])], [str(h) for h in other_entry.get("hooks", [])])
        for hook in hooks.missing:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.HOOK,
                    locator=f"{where}:{hook}",
                    message=f"hook missing: {hook}",
                    content_key=hook,
                )
            )
        for hook in hooks.extra:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.HOOK,
                    locator=f"{where}:{hook}",
                    message=f"hook only in repo: {hook}",
                    content_key=hook,
                )
            )
        main_rev = main_entry.get("rev")
        other_rev = other_entry.get("rev")
        if self.opts.compare_rev and main_rev is not None and other_rev is not None and main_rev != other_rev:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.DIFFERS,
                    subject=Subject.VALUE,
                    locator=where,
                    message=f"{slug}: main {main_rev} ; repo {other_rev}",
                    content_key="rev",
                    direction=Direction.DOWNSTREAM,
                    severity=Severity.WARNING,
                    option=self.option_ref("compare_rev"),
                )
            )
        return out


# ----- helpers -----------------------------------------------------------------------------------------------------


def repo_slug(url: str) -> str:
    """``local``/``meta`` as-is, ``github.com`` URLs (https, ssh, scp-style) as ``org/repo``, others whole."""
    text = url.strip()
    if text in _SPECIAL:
        return text
    match = _GITHUB_RE.match(text)
    if match:
        return match.group("path")
    text = text.rstrip("/")
    return text[:-4] if text.endswith(".git") else text


def _rev(value: Any) -> str | None:
    """The pinned revision as text (YAML may read ``rev: 1.0`` as a number); ``None`` when absent."""
    if value is None:
        return None
    return value if isinstance(value, str) else str(value)


__all__ = ["PrecommitAspect", "PrecommitOptions", "repo_slug"]
