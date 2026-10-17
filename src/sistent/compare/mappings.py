"""Recursive comparison of nested mappings (TOML/YAML documents) and small helpers around them.

:func:`diff_mapping` walks two dict trees in parallel and reports, per leaf, whether a key is ``missing`` (only in
main), ``extra`` (only in the other document) or ``differs`` (present on both sides with unequal values). Lists and
scalars are leaves and compared with ``==``; a dict on one side against a non-dict on the other is one ``differs``
leaf. :data:`MISSING` marks an absent value in :class:`KeyDiff` and in :func:`get_path` results.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


class Missing:
    """Sentinel type for "no value here"; its single instance is :data:`MISSING`."""

    __slots__ = ()
    _instance: Missing | None = None

    def __new__(cls) -> Missing:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "<missing>"

    def __reduce__(self) -> str:
        return "MISSING"


MISSING = Missing()
"""The absent-value sentinel (distinct from ``None``, which is a legitimate value in YAML)."""

KeyDiffKind = Literal["missing", "extra", "differs"]


@dataclass(frozen=True)
class KeyDiff:
    """One leaf-level difference between two mapping trees."""

    path: tuple[str, ...]
    """Key path from the root; empty when the roots themselves are compared as leaves."""
    kind: KeyDiffKind
    main: Any
    """Value in main (:data:`MISSING` for ``extra``)."""
    other: Any
    """Value in the other document (:data:`MISSING` for ``missing``)."""

    @property
    def dotted(self) -> str:
        return ".".join(self.path)


def diff_mapping(
    main: Any,
    other: Any,
    *,
    path: tuple[str, ...] = (),
    max_depth: int | None = None,
) -> list[KeyDiff]:
    """Leaf-level differences between two values, descending through mappings.

    Keys are visited in main order, then the keys only ``other`` has. ``max_depth`` limits the descent: at depth
    ``0`` the two values are compared whole (``==``), whatever they are. :data:`MISSING` on either side yields a
    ``missing``/``extra`` entry, so ``diff_mapping(get_path(a, k), get_path(b, k), path=(k,))`` behaves like a
    key lookup.
    """
    if main is MISSING and other is MISSING:
        return []
    if other is MISSING:
        return [KeyDiff(path, "missing", main, MISSING)]
    if main is MISSING:
        return [KeyDiff(path, "extra", MISSING, other)]

    descend = max_depth is None or max_depth > 0
    if descend and isinstance(main, Mapping) and isinstance(other, Mapping):
        next_depth = None if max_depth is None else max_depth - 1
        out: list[KeyDiff] = []
        for key, value in main.items():
            child = (*path, str(key))
            if key in other:
                out.extend(diff_mapping(value, other[key], path=child, max_depth=next_depth))
            else:
                out.append(KeyDiff(child, "missing", value, MISSING))
        for key, value in other.items():
            if key not in main:
                out.append(KeyDiff((*path, str(key)), "extra", MISSING, value))
        return out

    if main == other:
        return []
    return [KeyDiff(path, "differs", main, other)]


def get_path(doc: Mapping[str, Any], dotted: str) -> Any:
    """Value at a dotted key path (``"tool.ruff.line-length"``), or :data:`MISSING` when any segment is absent.

    Segments are split on ``"."``; keys that themselves contain a dot cannot be addressed. The empty path returns
    ``doc``.
    """
    if not dotted:
        return doc
    current: Any = doc
    for segment in dotted.split("."):
        if not isinstance(current, Mapping) or segment not in current:
            return MISSING
        current = current[segment]
    return current


_RE_REQUIREMENT = re.compile(r"^\s*(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?P<rest>.*)$", re.S)
_RE_NORMALISE = re.compile(r"[-_.]+")
_REST_STARTS = "[@;(<>=!~"


def parse_requirement(requirement: str) -> tuple[str, str]:
    """Split a PEP 508 requirement into ``(PEP 503 normalised name, remainder)``.

    The remainder is everything after the name (extras, specifier, URL, markers) with *all* whitespace removed, so
    ``"Pytest-Cov >= 4, <5 ; python_version<'3.12'"`` -> ``("pytest-cov", ">=4,<5;python_version<'3.12'")``.
    Strings that do not start with a valid name followed by a valid continuation are returned as
    ``(requirement.strip().lower(), "")`` so they still compare as a name.
    """
    match = _RE_REQUIREMENT.match(requirement)
    if match is None:
        return requirement.strip().lower(), ""
    rest = "".join(match.group("rest").split())
    if rest and rest[0] not in _REST_STARTS:
        return requirement.strip().lower(), ""
    name = _RE_NORMALISE.sub("-", match.group("name")).lower()
    return name, rest
