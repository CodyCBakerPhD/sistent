"""Order-preserving set difference keyed by an arbitrary function.

``missing`` is what main has and the other repository lacks (a downstream finding); ``extra`` is what only the other
repository has (an upstream candidate). Input order is preserved on both sides so findings come out in document
order, and duplicates (by key) within one side collapse to their first occurrence.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")


def identity(item: Any) -> Any:
    """Default key function: the item itself."""
    return item


@dataclass(frozen=True)
class SetDiff(Generic[T]):
    """Result of :func:`diff_sets`."""

    missing: tuple[T, ...]
    """Items whose key occurs only in ``main`` (main order)."""
    extra: tuple[T, ...]
    """Items whose key occurs only in ``other`` (other order)."""
    common: tuple[tuple[T, T], ...]
    """``(main_item, other_item)`` pairs sharing a key (main order)."""

    @property
    def clean(self) -> bool:
        """``True`` when both sides hold the same keys."""
        return not self.missing and not self.extra


def diff_sets(
    main: Iterable[T],
    other: Iterable[T],
    *,
    key: Callable[[T], Hashable] = identity,
) -> SetDiff[T]:
    """Compare two collections by ``key``.

    Items are grouped by ``key(item)``; within one side, later items with an already seen key are ignored. ``missing``
    and ``common`` follow the order of ``main``, ``extra`` the order of ``other``.
    """
    main_by_key: dict[Hashable, T] = {}
    for item in main:
        main_by_key.setdefault(key(item), item)
    other_by_key: dict[Hashable, T] = {}
    for item in other:
        other_by_key.setdefault(key(item), item)

    missing = tuple(item for k, item in main_by_key.items() if k not in other_by_key)
    extra = tuple(item for k, item in other_by_key.items() if k not in main_by_key)
    common = tuple((item, other_by_key[k]) for k, item in main_by_key.items() if k in other_by_key)
    return SetDiff(missing=missing, extra=extra, common=common)
