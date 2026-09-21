"""Baseline file: finding ids that are accepted for now and hidden from the report until they change.

Format (``.sistent-baseline.json``)::

    {"schema_version": 1, "generated_at": "2026-09-19T12:00:00Z",
     "findings": {"<finding id>": "<repo> <kind>/<subject> <locator>"}}

The description next to each id is for humans reading the file; only the id is used for matching.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sistent.model import Finding

__all__ = [
    "BASELINE_SCHEMA_VERSION",
    "Baseline",
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
class Baseline:
    """Contents of a baseline file: finding id -> human description."""

    generated_at: str
    entries: dict[str, str] = field(default_factory=dict)

    def __contains__(self, finding_id: object) -> bool:
        return finding_id in self.entries

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def describe(finding: Finding) -> str:
    """The human-readable value stored next to a finding id: ``<repo> <kind>/<subject> <locator>``."""
    return f"{finding.repo} {finding.kind.value}/{finding.subject.value} {finding.locator}"


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
    if not isinstance(findings, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in findings.items()
    ):
        raise BaselineError(f"{path}: 'findings' must be an object mapping finding ids to descriptions")
    generated_at = document.get("generated_at", "")
    if not isinstance(generated_at, str):
        raise BaselineError(f"{path}: 'generated_at' must be a string")
    return Baseline(generated_at=generated_at, entries=dict(findings))


def write_baseline(path: Path, findings: Iterable[Finding], *, generated_at: str) -> Baseline:
    """Write the ids of all non-suppressed ``findings`` (sorted by id) to ``path`` and return the new baseline.

    The file is written atomically (temporary file + rename) and its parent directory is created if needed.
    """
    entries = dict(sorted((f.id, describe(f)) for f in findings if not f.suppressed))
    document = {"schema_version": BASELINE_SCHEMA_VERSION, "generated_at": generated_at, "findings": entries}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)
    return Baseline(generated_at=generated_at, entries=entries)


def apply_baseline(findings: Iterable[Finding], baseline: Baseline | None) -> tuple[list[Finding], list[str]]:
    """Mark findings whose id is in ``baseline`` as ``baselined``.

    Returns the (possibly replaced) findings in input order and the stale ids: baseline entries that no finding
    produced any more (the user should run ``--update-baseline``). Without a baseline the findings pass through.
    """
    items = list(findings)
    if baseline is None:
        return items, []
    marked = [f.replace(baselined=True) if f.id in baseline and not f.baselined else f for f in items]
    produced = {f.id for f in items}
    stale = [finding_id for finding_id in baseline if finding_id not in produced]
    return marked, stale
