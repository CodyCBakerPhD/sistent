"""Ordered comparison and one-to-one matching of small sequences.

* :func:`diff_sequences` adds *order* awareness to :func:`~sistent.compare.sets.diff_sets`: besides what is missing
  and extra it tells whether the shared elements appear in the same relative order (used for heading order, badge
  order and ordered lists).
* :func:`match_by_keys` pairs items of two sequences through a cascade of key functions (exact first, looser later)
  and an optional fuzzy predicate, greedily and one-to-one.
* :func:`close_matches` pairs short strings by character similarity, highest similarity first.

All inputs are expected to be small (headings, list items, badges); the algorithms are quadratic at worst.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from sistent.compare.sets import identity
from sistent.compare.text import ratio

T = TypeVar("T")


@dataclass(frozen=True)
class SequenceDiff(Generic[T]):
    """Result of :func:`diff_sequences`."""

    missing: tuple[T, ...]
    """Items whose key occurs only in ``main`` (main order)."""
    extra: tuple[T, ...]
    """Items whose key occurs only in ``other`` (other order)."""
    reordered: bool
    """The shared items do not appear in the same relative order on both sides."""
    lcs_len: int
    """Length of the longest common subsequence of the shared keys (equals their count when not reordered)."""


def _lcs_length(a: Sequence[Hashable], b: Sequence[Hashable]) -> int:
    """Length of the longest common subsequence of ``a`` and ``b`` (classic two-row dynamic programme)."""
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for item in a:
        current = [0] * (len(b) + 1)
        for j, other in enumerate(b, start=1):
            if item == other:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return previous[-1]


def diff_sequences(
    main: Sequence[T],
    other: Sequence[T],
    *,
    key: Callable[[T], Hashable] = identity,
) -> SequenceDiff[T]:
    """Compare two sequences by ``key``, reporting missing/extra items and whether the shared ones are reordered.

    Duplicates (by key) within one side collapse to their first occurrence. ``reordered`` is ``True`` when the longest
    common subsequence of the shared keys is shorter than the number of shared keys, i.e. at least one shared item
    would have to move to make both orders agree. Pure additions or removals never count as reordering.
    """
    main_keys: dict[Hashable, T] = {}
    for item in main:
        main_keys.setdefault(key(item), item)
    other_keys: dict[Hashable, T] = {}
    for item in other:
        other_keys.setdefault(key(item), item)

    missing = tuple(item for k, item in main_keys.items() if k not in other_keys)
    extra = tuple(item for k, item in other_keys.items() if k not in main_keys)
    shared_main = [k for k in main_keys if k in other_keys]
    shared_other = [k for k in other_keys if k in main_keys]
    lcs_len = _lcs_length(shared_main, shared_other)
    return SequenceDiff(missing=missing, extra=extra, reordered=lcs_len < len(shared_main), lcs_len=lcs_len)


@dataclass(frozen=True)
class Matches(Generic[T]):
    """Result of :func:`match_by_keys`."""

    pairs: tuple[tuple[T, T, int], ...]
    """``(main_item, other_item, key_index)``, ordered by the main item's position. ``key_index`` is the index of
    the key function that paired them (``0`` = first/exact) or ``len(keys)`` when the ``fuzzy`` predicate did."""
    unmatched_main: tuple[T, ...]
    unmatched_other: tuple[T, ...]


def match_by_keys(
    main: Sequence[T],
    other: Sequence[T],
    *,
    keys: Sequence[Callable[[T], Hashable | None]],
    fuzzy: Callable[[T, T], bool] | None = None,
) -> Matches[T]:
    """Greedy one-to-one matching through a cascade of key functions, then an optional fuzzy predicate.

    For each key function in order, the still-unmatched items on both sides are keyed; a key that is non-``None``
    and occurs exactly once on each side pairs those two items. Ambiguous keys (duplicated on either side) do not
    match at that step; a later, looser key or ``fuzzy`` may still pair them. Finally ``fuzzy(main_item,
    other_item)`` is tried on the leftovers in main order; the first other item it accepts wins.
    """
    main_left = list(range(len(main)))
    other_left = list(range(len(other)))
    pairs: list[tuple[int, int, int]] = []

    for key_index, key in enumerate(keys):
        main_keyed = {i: key(main[i]) for i in main_left}
        other_keyed = {j: key(other[j]) for j in other_left}
        main_count = _count(main_keyed.values())
        other_count = _count(other_keyed.values())
        other_by_key = {k: j for j, k in other_keyed.items() if k is not None and other_count[k] == 1}
        matched_other: set[int] = set()
        still_main: list[int] = []
        for i in main_left:
            k = main_keyed[i]
            j = other_by_key.get(k) if k is not None and main_count[k] == 1 else None
            if j is None:
                still_main.append(i)
                continue
            pairs.append((i, j, key_index))
            matched_other.add(j)
        main_left = still_main
        other_left = [j for j in other_left if j not in matched_other]

    if fuzzy is not None:
        still_main = []
        for i in main_left:
            for pos, j in enumerate(other_left):
                if fuzzy(main[i], other[j]):
                    pairs.append((i, j, len(keys)))
                    del other_left[pos]
                    break
            else:
                still_main.append(i)
        main_left = still_main

    pairs.sort(key=lambda p: p[0])
    return Matches(
        pairs=tuple((main[i], other[j], key_index) for i, j, key_index in pairs),
        unmatched_main=tuple(main[i] for i in main_left),
        unmatched_other=tuple(other[j] for j in other_left),
    )


def _count(values: Iterable[Hashable | None]) -> dict[Hashable, int]:
    """Occurrences of each non-``None`` key."""
    counts: dict[Hashable, int] = {}
    for value in values:
        if value is not None:
            counts[value] = counts.get(value, 0) + 1
    return counts


def close_matches(
    main: Sequence[str],
    other: Sequence[str],
    *,
    cutoff: float = 0.75,
) -> list[tuple[str, str, float]]:
    """Greedy one-to-one pairing of short strings by :func:`~sistent.compare.text.ratio`.

    Every cross pair with ``ratio >= cutoff`` is a candidate; candidates are taken highest ratio first (ties broken by
    position), each string used at most once. Returns ``(main_item, other_item, ratio)`` triples in that order.
    ``ratio`` is asked to bail out early below ``cutoff`` (cheap upper bounds), so the quadratic loop stays cheap.
    """
    candidates: list[tuple[float, int, int]] = []
    for i, a in enumerate(main):
        for j, b in enumerate(other):
            score = ratio(a, b, cutoff=cutoff)
            if score >= cutoff:
                candidates.append((score, i, j))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))

    used_main: set[int] = set()
    used_other: set[int] = set()
    out: list[tuple[str, str, float]] = []
    for score, i, j in candidates:
        if i in used_main or j in used_other:
            continue
        used_main.add(i)
        used_other.add(j)
        out.append((main[i], other[j], score))
    return out
