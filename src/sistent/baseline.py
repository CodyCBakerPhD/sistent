"""Baseline file: finding ids that are accepted for now and hidden from the report until they change.

Format (``.sistent-baseline.json``)::

    {"schema_version": 1, "generated_at": "2026-09-19T12:00:00Z",
     "findings": {"<finding id>": {"repo": "roiextractors", "aspect": "readme",
                                   "about": "roiextractors missing/section README.md#Funding"}}}

Only the id is used for matching; ``repo``/``aspect`` scope the entry so that a partial run (``--repo``, ``--aspect``,
``--tag``, an unavailable satellite) neither reports the untouched entries as stale nor drops them when the baseline
is rewritten. A plain string value (the first format) is accepted and treated as unscoped.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sistent.model import Finding

__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "Baseline",
    "BaselineEntry",
    "BaselineError",
    "apply_baseline",
    "describe",
    "read_baseline",
    "write_baseline",
]

BASELINE_SCHEMA_VERSION = 1


class BaselineError(Exception):
    """The baseline file is missing or malformed."""


@dataclass(frozen=True)
class BaselineEntry:
    """One accepted finding: a human description plus the repo and aspect it belongs to (``None`` when unknown)."""

    about: str
    repo: str | None = None
    aspect: str | None = None

    def covered_by(self, covered: Collection[tuple[str, str]] | None) -> bool:
        """Whether a run that covered ``covered`` (``None`` = everything) can say anything about this entry."""
        if covered is None:
            return True
        return self.repo is not None and self.aspect is not None and (self.repo, self.aspect) in covered


@dataclass(frozen=True)
class Baseline:
    """Contents of a baseline file: finding id -> entry."""

    generated_at: str
    entries: dict[str, BaselineEntry] = field(default_factory=dict)

    def __contains__(self, finding_id: object) -> bool:
        return finding_id in self.entries

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def describe(self, finding_id: str) -> str:
        return self.entries[finding_id].about


def describe(finding: Finding) -> str:
    """The human-readable value stored next to a finding id: ``<repo> <kind>/<subject> <locator>``."""
    return f"{finding.repo} {finding.kind.value}/{finding.subject.value} {finding.locator}"


def _entry(finding: Finding) -> BaselineEntry:
    return BaselineEntry(about=describe(finding), repo=finding.repo, aspect=finding.aspect)


def read_baseline(path: Path) -> Baseline:
    """Load a baseline file; raise :class:`BaselineError` when it is missing, not JSON or of the wrong shape."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise BaselineError(f"baseline file not found: {path}") from None
    except OSError as exc:
        raise BaselineError(f"cannot read baseline file {path}: {exc}") from exc
    try:
        document: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BaselineError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(document, dict):
        raise BaselineError(f"{path}: expected a JSON object at the top level")
    version = document.get("schema_version")
    if version != BASELINE_SCHEMA_VERSION:
        raise BaselineError(
            f"{path}: unsupported baseline schema_version {version!r} (expected {BASELINE_SCHEMA_VERSION})"
        )
    findings = document.get("findings")
    if not isinstance(findings, dict):
        raise BaselineError(f"{path}: 'findings' must be an object mapping finding ids to entries")
    entries: dict[str, BaselineEntry] = {}
    for key, value in findings.items():
        if not isinstance(key, str):
            raise BaselineError(f"{path}: finding ids must be strings")
        if isinstance(value, str):
            entries[key] = BaselineEntry(about=value)
        elif isinstance(value, dict) and isinstance(value.get("about"), str):
            repo = value.get("repo")
            aspect = value.get("aspect")
            entries[key] = BaselineEntry(
                about=value["about"],
                repo=repo if isinstance(repo, str) else None,
                aspect=aspect if isinstance(aspect, str) else None,
            )
        else:
            raise BaselineError(f"{path}: entry {key!r} must be a string or an object with 'about'")
    generated_at = document.get("generated_at", "")
    if not isinstance(generated_at, str):
        raise BaselineError(f"{path}: 'generated_at' must be a string")
    return Baseline(generated_at=generated_at, entries=entries)


def write_baseline(
    path: Path,
    findings: Iterable[Finding],
    *,
    generated_at: str,
    keep: Baseline | None = None,
    covered: Collection[tuple[str, str]] | None = None,
) -> Baseline:
    """Write the ids of all non-suppressed ``findings`` to ``path`` and return the new baseline.

    With ``keep`` and ``covered`` (a partial run), entries of ``keep`` outside the covered ``(repo, aspect)`` pairs
    are carried over instead of being dropped. The file is written atomically (temporary file + rename) and its
    parent directory is created if needed.
    """
    entries: dict[str, BaselineEntry] = {}
    if keep is not None and covered is not None:
        entries.update({key: entry for key, entry in keep.entries.items() if not entry.covered_by(covered)})
    entries.update({f.id: _entry(f) for f in findings if not f.suppressed})
    entries = dict(sorted(entries.items()))
    document = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "findings": {
            key: {"repo": entry.repo, "aspect": entry.aspect, "about": entry.about} for key, entry in entries.items()
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return Baseline(generated_at=generated_at, entries=entries)


def apply_baseline(
    findings: Iterable[Finding],
    baseline: Baseline | None,
    *,
    covered: Collection[tuple[str, str]] | None = None,
) -> tuple[list[Finding], list[str]]:
    """Mark findings whose id is in ``baseline`` as ``baselined``.

    Returns the (possibly replaced) findings in input order and the stale ids: baseline entries that no finding
    produced any more although the run covered their repo and aspect (``covered``; ``None`` = a complete run).
    Without a baseline the findings pass through.
    """
    items = list(findings)
    if baseline is None:
        return items, []
    marked = [f.replace(baselined=True) if f.id in baseline and not f.baselined else f for f in items]
    produced = {f.id for f in items}
    stale = [
        finding_id
        for finding_id, entry in baseline.entries.items()
        if finding_id not in produced and entry.covered_by(covered)
    ]
    return marked, stale
