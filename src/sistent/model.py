"""Core data model shared by every part of sistent.

This module deliberately imports nothing else from the package: parsers, comparison primitives, aspects and
renderers all depend on it, never the other way round.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

SCHEMA_VERSION = 1
"""Version of the JSON report shape produced by :meth:`Report.to_dict`."""


class Severity(StrEnum):
    """How serious a finding is. Ordered ``INFO < WARNING < ERROR`` through :attr:`rank`."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]

    @classmethod
    def parse(cls, value: str) -> Severity:
        try:
            return cls(value.lower())
        except ValueError:
            valid = ", ".join(s.value for s in cls)
            raise ValueError(f"invalid severity {value!r} (valid: {valid})") from None


_SEVERITY_RANK: dict[Severity, int] = {Severity.INFO: 0, Severity.WARNING: 1, Severity.ERROR: 2}


class Direction(StrEnum):
    """Who should change to resolve a finding."""

    DOWNSTREAM = "downstream"
    """main -> satellite: main has something the satellite lacks or does differently; the satellite should adopt it."""
    UPSTREAM = "upstream"
    """satellite -> main: the satellite has something main lacks; an improvement that may percolate into main."""
    NONE = "none"
    """Neither side is authoritative (content differs both ways, stale reference, self-inconsistency)."""


class Kind(StrEnum):
    """What was observed."""

    MISSING = "missing"
    EXTRA = "extra"
    DIFFERS = "differs"
    MOVED = "moved"
    REORDERED = "reordered"
    STALE = "stale"
    UNPARSEABLE = "unparseable"


class Subject(StrEnum):
    """What the observation is about."""

    FILE = "file"
    TITLE = "title"
    SECTION = "section"
    RULE = "rule"
    PROSE = "prose"
    CODE = "code"
    BADGE = "badge"
    KEY = "key"
    VALUE = "value"
    PATH = "path"
    WORKFLOW = "workflow"
    JOB = "job"
    ACTION = "action"
    HOOK = "hook"
    NAME = "name"
    REFERENCE = "reference"


DetailKind = Literal["diff", "list", "text"]
Stage = Literal["resolve", "extract", "compare"]
RepoState = Literal["ok", "unavailable"]


def locator(path: str, *segments: str, sep: str = "#") -> str:
    """Build a locator string.

    ``locator("README.md", "Usage", "Example")`` -> ``"README.md#Usage > Example"``;
    ``locator("pyproject.toml", "tool.ruff", sep=":")`` -> ``"pyproject.toml:tool.ruff"``;
    ``locator("AGENTS.md")`` -> ``"AGENTS.md"``.
    """
    if not segments:
        return path
    return f"{path}{sep}{' > '.join(segments)}"


def _digest(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, kw_only=True)
class Finding:
    """One observed inconsistency between a satellite and main (or within one repository)."""

    aspect: str
    repo: str
    kind: Kind
    subject: Subject
    severity: Severity
    direction: Direction
    locator: str
    message: str
    detail: str | None = None
    detail_kind: DetailKind | None = None
    option: str | None = None
    """The configuration knob that produced this finding, e.g. ``aspects.readme.similarity_threshold=0.6``."""
    content_key: str = ""
    """Normalised identity of the content (rule text, badge kind, path, ...). Never the human message."""
    suppressed: bool = False
    """Matched an ``ignore`` glob."""
    baselined: bool = False
    """Its :attr:`id` is listed in the baseline file."""
    id: str = field(init=False, default="")
    """Stable identifier: hash of aspect, repo, kind, subject, locator and content key."""
    candidate_key: str = field(init=False, default="")
    """Like :attr:`id` but repo-independent, used to aggregate upstream candidates across satellites."""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "id",
            _digest(self.aspect, self.repo, self.kind.value, self.subject.value, self.locator, self.content_key),
        )
        object.__setattr__(
            self,
            "candidate_key",
            _digest(self.aspect, self.kind.value, self.subject.value, self.locator, self.content_key),
        )

    @property
    def hidden(self) -> bool:
        return self.suppressed or self.baselined

    @property
    def gates(self) -> bool:
        """Whether this finding can affect the exit code (upstream candidates and hidden findings never do)."""
        return not self.hidden and self.direction is not Direction.UPSTREAM

    def replace(self, **changes: Any) -> Finding:
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_key": self.candidate_key,
            "aspect": self.aspect,
            "repo": self.repo,
            "kind": self.kind.value,
            "subject": self.subject.value,
            "severity": self.severity.value,
            "direction": self.direction.value,
            "locator": self.locator,
            "message": self.message,
            "detail": self.detail,
            "detail_kind": self.detail_kind,
            "option": self.option,
            "content_key": self.content_key,
            "suppressed": self.suppressed,
            "baselined": self.baselined,
        }


@dataclass(frozen=True, kw_only=True)
class Identity:
    """Names by which one repository refers to itself; replaced by placeholders before comparison."""

    name: str
    """The ``[repos.X]`` key."""
    aliases: tuple[str, ...]
    """Deduplicated, longest first. Matching is case-insensitive."""
    org: str | None = None
    branch: str | None = None
    slug: str | None = None
    """``host/org/repo`` when known."""
    extra: dict[str, str] = field(default_factory=dict)
    """Other ``[repos.X].vars`` entries: value -> ``{{key}}``."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "org": self.org,
            "branch": self.branch,
            "slug": self.slug,
            "extra": dict(self.extra),
        }


@dataclass(frozen=True, kw_only=True)
class StaleHit:
    """A mention of *another* configured repository's identity (copy-paste leftover)."""

    where: str
    alias: str
    other_repo: str
    excerpt: str


@dataclass(frozen=True, kw_only=True)
class Snapshot:
    """The normalised representation one aspect extracted from one repository."""

    aspect: str
    repo: str
    schema_version: int
    data: dict[str, Any]
    sources: tuple[str, ...] = ()
    fingerprint: str = ""
    head: str | None = None
    aliases_applied: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    stale_hits: tuple[StaleHit, ...] = ()

    def to_dict(self, *, explain: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "aspect": self.aspect,
            "repo": self.repo,
            "schema_version": self.schema_version,
            "sources": list(self.sources),
            "head": self.head,
            "data": self.data,
        }
        if explain:
            out["fingerprint"] = self.fingerprint
            out["aliases_applied"] = list(self.aliases_applied)
            out["ignored"] = list(self.ignored)
            out["stale_hits"] = [dataclasses.asdict(h) for h in self.stale_hits]
        return out


@dataclass(frozen=True, kw_only=True)
class RunError:
    """Something went wrong while resolving, extracting or comparing; counts as an error for ``fail_on``."""

    stage: Stage
    repo: str | None
    aspect: str | None
    message: str
    traceback: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True, kw_only=True)
class RepoStatus:
    name: str
    is_main: bool
    source: str
    resolved: str | None
    rev: str | None
    head: str | None
    status: RepoState
    error: str | None
    tags: tuple[str, ...] = ()
    identity: Identity | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "is_main": self.is_main,
            "source": self.source,
            "resolved": self.resolved,
            "rev": self.rev,
            "head": self.head,
            "status": self.status,
            "error": self.error,
            "tags": list(self.tags),
            "identity": self.identity.to_dict() if self.identity else None,
        }


@dataclass(frozen=True, kw_only=True)
class Candidate:
    """An upstream finding aggregated across satellites: ``support`` of ``total`` satellites report it."""

    aspect: str
    kind: Kind
    subject: Subject
    locator: str
    content_key: str
    candidate_key: str
    message: str
    repos: tuple[str, ...]
    support: int
    total: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_key": self.candidate_key,
            "aspect": self.aspect,
            "kind": self.kind.value,
            "subject": self.subject.value,
            "locator": self.locator,
            "content_key": self.content_key,
            "message": self.message,
            "repos": list(self.repos),
            "support": self.support,
            "total": self.total,
        }


@dataclass(frozen=True, kw_only=True)
class AspectInfo:
    """A configured aspect instance as it appears in the report."""

    name: str
    type: str
    options: dict[str, Any]
    targets: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "options": self.options, "targets": list(self.targets)}


@dataclass(frozen=True, kw_only=True)
class Report:
    sistent_version: str
    generated_at: str
    config_path: str
    main: RepoStatus
    repos: tuple[RepoStatus, ...]
    aspects: tuple[AspectInfo, ...]
    findings: tuple[Finding, ...]
    candidates: tuple[Candidate, ...]
    errors: tuple[RunError, ...]
    fail_on: Severity | None
    exit_code: int
    exit_reason: str
    snapshots: dict[tuple[str, str], Snapshot] = field(default_factory=dict, compare=False)
    """``(repo, aspect) -> snapshot``; not serialised into the JSON report."""
    schema_version: int = SCHEMA_VERSION

    # ----- convenience views -------------------------------------------------------------------------------------

    @property
    def visible_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.hidden)

    def findings_for(self, repo: str, *, include_hidden: bool = False) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.repo == repo and (include_hidden or not f.hidden))

    @property
    def unavailable(self) -> tuple[RepoStatus, ...]:
        return tuple(r for r in self.repos if r.status != "ok")

    def summary(self) -> dict[str, Any]:
        by_repo: dict[str, dict[str, int]] = {}
        for status in (self.main, *self.repos):
            by_repo[status.name] = {
                "error": 0,
                "warning": 0,
                "info": 0,
                "candidates": 0,
                "suppressed": 0,
                "baselined": 0,
            }
        by_severity = {s.value: 0 for s in Severity}
        by_direction = {d.value: 0 for d in Direction}
        new = baselined = suppressed = 0
        for f in self.findings:
            bucket = by_repo.setdefault(
                f.repo,
                {"error": 0, "warning": 0, "info": 0, "candidates": 0, "suppressed": 0, "baselined": 0},
            )
            if f.suppressed:
                bucket["suppressed"] += 1
                suppressed += 1
                continue
            if f.baselined:
                bucket["baselined"] += 1
                baselined += 1
                continue
            new += 1
            by_severity[f.severity.value] += 1
            by_direction[f.direction.value] += 1
            if f.direction is Direction.UPSTREAM:
                bucket["candidates"] += 1
            else:
                bucket[f.severity.value] += 1
        return {
            "by_repo": by_repo,
            "by_severity": by_severity,
            "by_direction": by_direction,
            "new": new,
            "baselined": baselined,
            "suppressed": suppressed,
            "errors": len(self.errors),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "sistent_version": self.sistent_version,
            "generated_at": self.generated_at,
            "config_path": self.config_path,
            "fail_on": self.fail_on.value if self.fail_on else "never",
            "exit_code": self.exit_code,
            "exit_reason": self.exit_reason,
            "main": self.main.to_dict(),
            "repos": [r.to_dict() for r in self.repos],
            "aspects": [a.to_dict() for a in self.aspects],
            "summary": self.summary(),
            "findings": [f.to_dict() for f in self.findings],
            "candidates": [c.to_dict() for c in self.candidates],
            "errors": [e.to_dict() for e in self.errors],
        }


def aggregate_candidates(findings: Iterable[Finding], totals: Mapping[str, int]) -> tuple[Candidate, ...]:
    """Group upstream findings by :attr:`Finding.candidate_key`.

    ``totals`` maps an aspect name to the number of satellites that aspect targeted.
    Sorted by support (descending), then aspect, then locator.
    """
    groups: dict[str, list[Finding]] = {}
    for f in findings:
        if f.direction is Direction.UPSTREAM and not f.hidden:
            groups.setdefault(f.candidate_key, []).append(f)
    out: list[Candidate] = []
    for key, members in groups.items():
        first = members[0]
        repos = tuple(dict.fromkeys(m.repo for m in members))
        out.append(
            Candidate(
                aspect=first.aspect,
                kind=first.kind,
                subject=first.subject,
                locator=first.locator,
                content_key=first.content_key,
                candidate_key=key,
                message=first.message,
                repos=repos,
                support=len(repos),
                total=totals.get(first.aspect, len(repos)),
            )
        )
    out.sort(key=lambda c: (-c.support, c.aspect, c.locator, c.content_key))
    return tuple(out)
