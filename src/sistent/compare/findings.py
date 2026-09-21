"""Turn comparison results into :class:`~sistent.model.Finding` lists.

Every finding goes through :meth:`Aspect.finding` so direction, severity and ids are derived in one place. This is
the only module under ``compare/`` that knows about findings; it depends on :class:`~sistent.aspects.base.Aspect`
for typing only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, TypeVar

from sistent.compare.mappings import KeyDiff
from sistent.compare.sets import SetDiff
from sistent.model import Direction, Finding, Kind, Severity, Subject, locator

if TYPE_CHECKING:
    from sistent.aspects.base import Aspect

T = TypeVar("T")

VALUE_WIDTH = 60
"""Longest rendered value shown inline in a ``differs`` message; longer ones are truncated and moved to ``detail``."""


def findings_from_setdiff(
    diff: SetDiff[T],
    *,
    aspect: Aspect,
    repo: str,
    subject: Subject,
    locator: Callable[[T], str],
    describe: Callable[[T], str],
    content_key: Callable[[T], str],
    missing_severity: Severity | None = None,
) -> list[Finding]:
    """``missing`` findings (downstream) for ``diff.missing`` and ``extra`` findings (upstream) for ``diff.extra``.

    Findings are in input order, missing first. ``missing_severity`` is the aspect's own default for the missing
    findings (the user's ``severity`` table still wins); extra findings are always ``info``.
    """
    out: list[Finding] = []
    for item in diff.missing:
        out.append(
            aspect.finding(
                repo=repo,
                kind=Kind.MISSING,
                subject=subject,
                locator=locator(item),
                message=f"{subject.value} missing: {describe(item)}",
                content_key=content_key(item),
                severity=missing_severity,
            )
        )
    for item in diff.extra:
        out.append(
            aspect.finding(
                repo=repo,
                kind=Kind.EXTRA,
                subject=subject,
                locator=locator(item),
                message=f"{subject.value} only in repo: {describe(item)}",
                content_key=content_key(item),
            )
        )
    return out


def format_value(value: Any) -> str:
    """Compact JSON rendering of a mapping leaf (sorted keys, non-JSON types via ``str``)."""
    return json.dumps(value, sort_keys=True, default=str, ensure_ascii=False, separators=(",", ":"))


def _truncate(text: str, width: int = VALUE_WIDTH) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def findings_from_keydiffs(
    diffs: Iterable[KeyDiff],
    *,
    aspect: Aspect,
    repo: str,
    file: str,
    authoritative: bool,
) -> list[Finding]:
    """Findings for :func:`~sistent.compare.mappings.diff_mapping` results inside ``file``.

    Locators are ``<file>:<dotted.key>``. ``missing`` -> ``missing``/``key`` (downstream), ``extra`` ->
    ``extra``/``key`` (upstream), ``differs`` -> ``differs``/``value`` with direction downstream when main is
    ``authoritative`` for this key, else ``none``. Values are rendered as compact JSON; when either side exceeds
    :data:`VALUE_WIDTH` characters the message shows truncated values and ``detail`` carries the full ones.
    """
    out: list[Finding] = []
    for diff in diffs:
        dotted = diff.dotted
        where = locator(file, dotted, sep=":") if dotted else locator(file)
        if diff.kind == "missing":
            out.append(
                aspect.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.KEY,
                    locator=where,
                    message=f"key missing: {dotted}",
                    content_key=dotted,
                )
            )
        elif diff.kind == "extra":
            out.append(
                aspect.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.KEY,
                    locator=where,
                    message=f"key only in repo: {dotted}",
                    content_key=dotted,
                )
            )
        else:
            main_text = format_value(diff.main)
            other_text = format_value(diff.other)
            detail: str | None = None
            if len(main_text) > VALUE_WIDTH or len(other_text) > VALUE_WIDTH:
                detail = f"main: {main_text}\nrepo: {other_text}"
            out.append(
                aspect.finding(
                    repo=repo,
                    kind=Kind.DIFFERS,
                    subject=Subject.VALUE,
                    locator=where,
                    message=f"main: {_truncate(main_text)} ; repo: {_truncate(other_text)}",
                    detail=detail,
                    detail_kind="text" if detail is not None else None,
                    content_key=dotted,
                    direction=Direction.DOWNSTREAM if authoritative else Direction.NONE,
                )
            )
    return out
