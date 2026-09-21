"""Tests for :mod:`sistent.compare.sets`."""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Sequence
from typing import Any

import pytest

from sistent.compare.sets import SetDiff, diff_sets, identity

SYMMETRY_CASES = [
    pytest.param([], [], id="both-empty"),
    pytest.param(["a"], [], id="main-only"),
    pytest.param([], ["a"], id="other-only"),
    pytest.param(["a", "b", "c"], ["b", "c", "d"], id="overlap"),
    pytest.param(["a", "b", "c"], ["x", "y"], id="disjoint"),
    pytest.param(["a", "a", "b"], ["b", "b", "a"], id="duplicates"),
    pytest.param([1, 2, 3], [3, 2, 1], id="ints-reversed"),
]


@pytest.mark.parametrize(("main", "other"), SYMMETRY_CASES)
def test_symmetry(main: Sequence[Any], other: Sequence[Any]) -> None:
    forward = diff_sets(main, other)
    backward = diff_sets(other, main)
    assert forward.missing == backward.extra
    assert forward.extra == backward.missing
    assert set(forward.common) == {(b, a) for a, b in backward.common}


@pytest.mark.parametrize("items", [[], ["a"], ["a", "b", "c"], ["x", "x", "y"], [1, 2, 3]])
def test_identity_has_no_missing_or_extra(items: Sequence[Any]) -> None:
    diff = diff_sets(items, items)
    assert diff.missing == ()
    assert diff.extra == ()
    assert diff.clean
    assert diff.common == tuple((item, item) for item in dict.fromkeys(items))


def test_partition_preserves_input_order() -> None:
    diff = diff_sets(["c", "a", "b"], ["b", "d", "c", "e"])
    assert diff.missing == ("a",)
    assert diff.extra == ("d", "e")
    assert diff.common == (("c", "c"), ("b", "b"))
    assert not diff.clean


def test_key_function_pairs_items_and_keeps_originals() -> None:
    main = [("Install", 1), ("Usage", 2)]
    other = [("usage", 20), ("INSTALL", 10), ("license", 30)]
    diff = diff_sets(main, other, key=lambda item: item[0].lower())
    assert diff.missing == ()
    assert diff.extra == (("license", 30),)
    assert diff.common == ((("Install", 1), ("INSTALL", 10)), (("Usage", 2), ("usage", 20)))


def test_key_function_on_unhashable_items() -> None:
    main = [{"kind": "pypi-version", "provider": "shields"}, {"kind": "docs", "provider": "rtd"}]
    other = [{"kind": "docs", "provider": "rtd"}, {"kind": "coverage", "provider": "codecov"}]
    diff = diff_sets(main, other, key=lambda badge: badge["kind"])
    assert diff.missing == ({"kind": "pypi-version", "provider": "shields"},)
    assert diff.extra == ({"kind": "coverage", "provider": "codecov"},)
    assert diff.common == (({"kind": "docs", "provider": "rtd"}, {"kind": "docs", "provider": "rtd"}),)


@pytest.mark.parametrize(
    ("main", "other", "missing", "extra", "common"),
    [
        pytest.param(["a", "a", "b"], ["b", "b"], ("a",), (), (("b", "b"),), id="dupes-both-sides"),
        pytest.param(["a"], ["b", "b"], ("a",), ("b",), (), id="dupes-in-extra"),
        pytest.param(["a", "a"], ["a"], (), (), (("a", "a"),), id="dupes-in-common"),
    ],
)
def test_duplicates_collapse(
    main: Sequence[str],
    other: Sequence[str],
    missing: tuple[str, ...],
    extra: tuple[str, ...],
    common: tuple[tuple[str, str], ...],
) -> None:
    diff = diff_sets(main, other)
    assert (diff.missing, diff.extra, diff.common) == (missing, extra, common)


def test_duplicates_keep_first_occurrence_by_key() -> None:
    main = [("A", 1), ("a", 2), ("b", 3)]
    other = [("B", 4), ("b", 5)]
    diff = diff_sets(main, other, key=lambda item: item[0].lower())
    assert diff.missing == (("A", 1),)
    assert diff.common == ((("b", 3), ("B", 4)),)


def test_accepts_any_iterable() -> None:
    def gen(values: Iterable[str]) -> Iterable[str]:
        yield from values

    diff = diff_sets(gen("abc"), iter("bcd"))
    assert diff.missing == ("a",)
    assert diff.extra == ("d",)
    assert diff.common == (("b", "b"), ("c", "c"))


def test_identity_returns_its_argument() -> None:
    sentinel = object()
    assert identity(sentinel) is sentinel


def test_result_is_frozen() -> None:
    diff: SetDiff[str] = diff_sets(["a"], ["b"])
    with pytest.raises(dataclasses.FrozenInstanceError):
        diff.missing = ()  # type: ignore[misc]
