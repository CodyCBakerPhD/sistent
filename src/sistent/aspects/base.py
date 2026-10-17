"""Base class for aspect types.

An *aspect type* knows how to extract a normalised snapshot of one facet of a repository and how to compare two such
snapshots. An *aspect instance* is a type plus a name plus options (one ``[aspects.<name>]`` table).

Contract for every aspect type:

* read files only through the :class:`~sistent.repository.RepoContext` (``ctx.read_text``, ``ctx.glob``, ...);
* pass every string that ends up in the snapshot through ``ctx.substitute(text, where=locator)``;
* emit findings only through :meth:`Aspect.finding` (direction, severity and ids are derived there);
* report ``missing``/``extra`` for the highest unmatched node only, never for the descendants of an unmatched node.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, ClassVar

from sistent.model import DetailKind, Direction, Finding, Kind, Severity, Snapshot, Subject
from sistent.options import BaseOptions, options_to_dict
from sistent.repository import RepoContext


class UnparseableFile(Exception):
    """A file this aspect needs could not be parsed. For satellites this becomes an ``unparseable`` finding."""

    def __init__(self, rel: str, reason: str) -> None:
        super().__init__(f"{rel}: {reason}")
        self.rel = rel
        self.reason = reason


class SnapshotMismatch(Exception):
    """Two snapshots were produced by different schema versions of the aspect and cannot be compared."""


DEFAULT_DIRECTION: dict[Kind, Direction] = {
    Kind.MISSING: Direction.DOWNSTREAM,
    Kind.EXTRA: Direction.UPSTREAM,
    Kind.DIFFERS: Direction.NONE,
    Kind.MOVED: Direction.DOWNSTREAM,
    Kind.REORDERED: Direction.DOWNSTREAM,
    Kind.STALE: Direction.NONE,
    Kind.UNPARSEABLE: Direction.NONE,
}

DEFAULT_SEVERITY: dict[Kind, Severity] = {
    Kind.MISSING: Severity.WARNING,
    Kind.EXTRA: Severity.INFO,
    Kind.DIFFERS: Severity.WARNING,
    Kind.MOVED: Severity.INFO,
    Kind.REORDERED: Severity.INFO,
    Kind.STALE: Severity.WARNING,
    Kind.UNPARSEABLE: Severity.WARNING,
}

DEFAULT_SUBJECT_SEVERITY: dict[str, Severity] = {
    "missing.file": Severity.ERROR,
}


class Aspect(ABC):
    """One configured aspect instance. Subclasses implement :meth:`extract` and :meth:`compare`."""

    type_name: ClassVar[str] = ""
    options_cls: ClassVar[type[BaseOptions]] = BaseOptions
    schema_version: ClassVar[int] = 1
    description: ClassVar[str] = ""
    default_severity: ClassVar[dict[str, str]] = {}
    """Per-type defaults keyed like the ``severity`` option (``"<kind>"`` or ``"<kind>.<subject>"``)."""

    def __init__(self, name: str, options: BaseOptions) -> None:
        self.name = name
        self.options = options

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.name!r})"

    # ----- to implement ---------------------------------------------------------------------------------------

    @abstractmethod
    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """Return the JSON-serialisable snapshot data for one repository."""

    @abstractmethod
    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Findings for satellite ``other`` relative to ``main``."""

    def self_check(self, snap: Snapshot) -> list[Finding]:
        """Direction-``none`` findings about one repository alone (also run on main)."""
        return []

    # ----- provided -------------------------------------------------------------------------------------------

    def targets(self, tags: Iterable[str]) -> bool:
        """Whether a repo with ``tags`` is covered by this aspect."""
        wanted = self.options.tags
        if wanted is None:
            return True
        return bool(set(wanted) & set(tags))

    def options_dict(self) -> dict[str, Any]:
        return options_to_dict(self.options)

    def run_extract(self, ctx: RepoContext) -> Snapshot:
        """Call :meth:`extract` and wrap the result with provenance (sources, fingerprint, head, stale hits)."""
        data = self.extract(ctx)
        sources = tuple(sorted(ctx.sources))
        hasher = hashlib.sha256()
        for rel in sources:
            try:
                digest = hashlib.blake2b(ctx.repo.read_bytes(rel), digest_size=16).hexdigest()
            except OSError:
                digest = "unreadable"
            hasher.update(f"{rel}\0{digest}\n".encode())
        hasher.update(json.dumps(self.options_dict(), sort_keys=True, default=str).encode())
        hasher.update(f"\0schema={self.schema_version}".encode())
        return Snapshot(
            aspect=self.name,
            repo=ctx.repo.name,
            schema_version=self.schema_version,
            data=data,
            sources=sources,
            fingerprint=hasher.hexdigest(),
            head=ctx.repo.head(),
            aliases_applied=tuple(sorted(ctx.subst.applied)),
            ignored=tuple(ctx.ignored),
            stale_hits=tuple(ctx.subst.hits),
        )

    def check_versions(self, main: Snapshot, other: Snapshot) -> None:
        if main.schema_version != other.schema_version or main.schema_version != self.schema_version:
            raise SnapshotMismatch(
                f"{self.name}: snapshot schema versions differ "
                f"(main={main.schema_version}, {other.repo}={other.schema_version}, aspect={self.schema_version})"
            )

    def severity_for(self, kind: Kind, subject: Subject, *, default: Severity | None = None) -> Severity:
        """User override (``kind.subject`` then ``kind``), else ``default``, else type default, else global."""
        keys = (f"{kind.value}.{subject.value}", kind.value)
        for key in keys:
            if key in self.options.severity:
                return Severity.parse(self.options.severity[key])
        if default is not None:
            return default
        for key in keys:
            if key in self.default_severity:
                return Severity.parse(self.default_severity[key])
        for key in keys:
            if key in DEFAULT_SUBJECT_SEVERITY:
                return DEFAULT_SUBJECT_SEVERITY[key]
        return DEFAULT_SEVERITY[kind]

    def finding(
        self,
        *,
        repo: str,
        kind: Kind,
        subject: Subject,
        locator: str,
        message: str,
        detail: str | None = None,
        detail_kind: DetailKind | None = None,
        content_key: str = "",
        direction: Direction | None = None,
        severity: Severity | None = None,
        option: str | None = None,
    ) -> Finding:
        """Build a :class:`Finding` with derived direction/severity.

        ``direction`` defaults per kind (missing -> downstream, extra -> upstream, ...). ``severity`` is the
        aspect's own default for this finding; the user's ``severity`` table overrides it. Upstream findings are
        always ``info``.
        """
        direction = direction if direction is not None else DEFAULT_DIRECTION[kind]
        if direction is Direction.UPSTREAM:
            resolved = Severity.INFO
        else:
            resolved = self.severity_for(kind, subject, default=severity)
        return Finding(
            aspect=self.name,
            repo=repo,
            kind=kind,
            subject=subject,
            severity=resolved,
            direction=direction,
            locator=locator,
            message=message,
            detail=detail,
            detail_kind=detail_kind,
            option=option,
            content_key=content_key,
        )

    def option_ref(self, key: str) -> str:
        """``aspects.<name>.<key>=<value>`` for :attr:`Finding.option`."""
        value = getattr(self.options, key, None)
        return f"aspects.{self.name}.{key}={value}"
