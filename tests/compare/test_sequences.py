"""Tests for :mod:`sistent.compare.sequences`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest

from sistent.compare.sequences import Matches, close_matches, diff_sequences, match_by_keys
from sistent.compare.sets import identity
from sistent.compare.text import ratio

# ----- diff_sequences ----------------------------------------------------------------------------------------------

DIFF_CASES = [
    pytest.param(["a", "b", "c"], ["a", "b", "c"], (), (), False, 3, id="identical"),
    pytest.param(["a", "b", "c"], ["c", "a", "b"], (), (), True, 2, id="rotated"),
    pytest.param(["a", "b", "c"], ["c", "b", "a"], (), (), True, 1, id="reversed"),
    pytest.param(["a", "b", "c"], ["b", "a", "c"], (), (), True, 2, id="one-swap"),
    pytest.param(["a", "b", "c"], ["a", "c"], ("b",), (), False, 2, id="pure-missing"),
    pytest.param(["a", "c"], ["a", "b", "c"], (), ("b",), False, 2, id="pure-extra"),
    pytest.param(["a", "b", "c"], ["x", "a", "y", "b", "z"], ("c",), ("x", "y", "z"), False, 2, id="missing-and-extra"),
    pytest.param(["a", "b", "c"], ["x", "c", "b"], ("a",), ("x",), True, 1, id="missing-extra-reordered"),
    pytest.param([], ["a"], (), ("a",), False, 0, id="empty-main"),
    pytest.param(["a"], [], ("a",), (), False, 0, id="empty-other"),
    pytest.param([], [], (), (), False, 0, id="both-empty"),
    pytest.param(["a", "b", "a", "c"], ["b", "a", "c", "c"], (), (), True, 2, id="duplicates-collapse"),
]


@pytest.mark.parametrize(("main", "other", "missing", "extra", "reordered", "lcs_len"), DIFF_CASES)
def test_diff_sequences(
    main: Sequence[str],
    other: Sequence[str],
    missing: tuple[str, ...],
    extra: tuple[str, ...],
    reordered: bool,
    lcs_len: int,
) -> None:
    diff = diff_sequences(main, other)
    assert diff.missing == missing
    assert diff.extra == extra
    assert diff.reordered is reordered
    assert diff.lcs_len == lcs_len


def test_diff_sequences_key_function_returns_original_items() -> None:
    main = [{"kind": "pypi", "n": 1}, {"kind": "docs", "n": 2}, {"kind": "ci", "n": 3}]
    other = [{"kind": "ci", "n": 30}, {"kind": "pypi", "n": 10}, {"kind": "coverage", "n": 40}]
    diff = diff_sequences(main, other, key=lambda badge: badge["kind"])
    assert diff.missing == ({"kind": "docs", "n": 2},)
    assert diff.extra == ({"kind": "coverage", "n": 40},)
    assert diff.reordered is True
    assert diff.lcs_len == 1


def test_diff_sequences_symmetry_of_missing_and_extra() -> None:
    main, other = ["a", "b", "c", "d"], ["d", "b", "x"]
    forward, backward = diff_sequences(main, other), diff_sequences(other, main)
    assert forward.missing == backward.extra
    assert forward.extra == backward.missing
    assert forward.reordered is backward.reordered
    assert forward.lcs_len == backward.lcs_len


# ----- match_by_keys -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Section:
    path: str
    leaf: str
    body: str


def by_path(section: Section) -> str:
    return section.path


def by_leaf(section: Section) -> str:
    return section.leaf


def same_body(a: Section, b: Section) -> bool:
    return a.body == b.body


def test_match_by_keys_cascade_and_fuzzy() -> None:
    main = [
        Section("usage > example", "example", "print()"),
        Section("install", "install", "pip install"),
        Section("license", "license", "bsd"),
        Section("funding", "funding", "nih"),
    ]
    other = [
        Section("licence", "licence", "bsd"),
        Section("example", "example", "print()"),
        Section("install", "install", "pip install"),
        Section("contributing", "contributing", "prs welcome"),
    ]
    matches = match_by_keys(main, other, keys=[by_path, by_leaf], fuzzy=same_body)
    assert matches.pairs == (
        (main[0], other[1], 1),  # leaf "example"
        (main[1], other[2], 0),  # exact path
        (main[2], other[0], 2),  # fuzzy: same body -> index == len(keys)
    )
    assert matches.unmatched_main == (main[3],)
    assert matches.unmatched_other == (other[3],)


def test_match_by_keys_pairs_are_in_main_order() -> None:
    main = ["c", "a", "b"]
    other = ["a", "b", "c"]
    matches = match_by_keys(main, other, keys=[identity])
    assert matches.pairs == (("c", "c", 0), ("a", "a", 0), ("b", "b", 0))


@pytest.mark.parametrize(
    ("main", "other", "pairs", "unmatched_main", "unmatched_other"),
    [
        pytest.param(["a", "a", "b"], ["a", "b"], (("b", "b", 0),), ("a", "a"), ("a",), id="dupe-on-main"),
        pytest.param(["a", "b"], ["a", "a", "b"], (("b", "b", 0),), ("a",), ("a", "a"), id="dupe-on-other"),
        pytest.param(["a", "a"], ["a", "a"], (), ("a", "a"), ("a", "a"), id="dupe-both"),
    ],
)
def test_match_by_keys_ambiguous_keys_do_not_match(
    main: Sequence[str],
    other: Sequence[str],
    pairs: tuple[tuple[str, str, int], ...],
    unmatched_main: tuple[str, ...],
    unmatched_other: tuple[str, ...],
) -> None:
    matches = match_by_keys(main, other, keys=[identity])
    assert matches.pairs == pairs
    assert matches.unmatched_main == unmatched_main
    assert matches.unmatched_other == unmatched_other


def test_match_by_keys_ambiguity_is_per_step() -> None:
    """A key duplicated at step 0 can still be resolved by a later, distinguishing key."""
    main = [("x", 1), ("x", 2)]
    other = [("x", 2), ("x", 1)]
    matches = match_by_keys(main, other, keys=[lambda t: t[0], lambda t: t])
    assert matches.pairs == ((("x", 1), ("x", 1), 1), (("x", 2), ("x", 2), 1))


def test_match_by_keys_none_keys_never_match() -> None:
    matches = match_by_keys(["a", "b"], ["a", "b"], keys=[lambda _: None])
    assert matches.pairs == ()
    assert matches.unmatched_main == ("a", "b")
    assert matches.unmatched_other == ("a", "b")


def test_match_by_keys_partial_none_keys() -> None:
    def first_letter(s: str) -> str | None:
        return s[0] if s.startswith("k") else None

    matches = match_by_keys(["key", "other"], ["kit", "other"], keys=[first_letter])
    assert matches.pairs == (("key", "kit", 0),)
    assert matches.unmatched_main == ("other",)
    assert matches.unmatched_other == ("other",)


def test_match_by_keys_fuzzy_is_greedy_in_main_order() -> None:
    same_initial = lambda a, b: a[0] == b[0]  # noqa: E731
    matches = match_by_keys(["ab", "ac"], ["ay", "ax"], keys=[identity], fuzzy=same_initial)
    assert matches.pairs == (("ab", "ay", 1), ("ac", "ax", 1))


def test_match_by_keys_fuzzy_only_sees_leftovers() -> None:
    calls: list[tuple[str, str]] = []

    def record(a: str, b: str) -> bool:
        calls.append((a, b))
        return False

    matches = match_by_keys(["a", "b"], ["a", "c"], keys=[identity], fuzzy=record)
    assert matches.pairs == (("a", "a", 0),)
    assert calls == [("b", "c")]
    assert matches.unmatched_main == ("b",)
    assert matches.unmatched_other == ("c",)


def test_match_by_keys_without_keys_uses_only_fuzzy() -> None:
    matches = match_by_keys(["a"], ["A"], keys=[], fuzzy=lambda a, b: a.lower() == b.lower())
    assert matches.pairs == (("a", "A", 0),)


@pytest.mark.parametrize(("main", "other"), [([], []), (["a"], []), ([], ["a"])])
def test_match_by_keys_empty_sides(main: Sequence[str], other: Sequence[str]) -> None:
    matches: Matches[str] = match_by_keys(main, other, keys=[identity], fuzzy=lambda a, b: True)
    assert matches.pairs == ()
    assert matches.unmatched_main == tuple(main)
    assert matches.unmatched_other == tuple(other)


# ----- close_matches -----------------------------------------------------------------------------------------------

RULE = "run pytest before committing"
RULE_TESTS_DIR = "run pytest tests/ before committing"  # ratio 0.889: the spec's example
RULE_PUSHING = "run pytest before pushing"  # ratio 0.792
RULE_REWORDED = "always run the full test suite before you push"  # ratio 0.459


def test_close_matches_spec_examples() -> None:
    assert ratio(RULE, RULE_TESTS_DIR) >= 0.75
    assert ratio(RULE, RULE_REWORDED) < 0.75
    assert close_matches([RULE], [RULE_TESTS_DIR]) == [(RULE, RULE_TESTS_DIR, pytest.approx(0.889, abs=0.01))]
    assert close_matches([RULE], [RULE_REWORDED]) == []


def test_close_matches_takes_highest_ratio_first_and_is_one_to_one() -> None:
    main = [RULE, "keep functions small"]
    other = [RULE_PUSHING, RULE_TESTS_DIR]
    result = close_matches(main, other)
    assert result == [(RULE, RULE_TESTS_DIR, pytest.approx(0.889, abs=0.01))]


def test_close_matches_each_item_used_once() -> None:
    result = close_matches([RULE, RULE], [RULE_TESTS_DIR, RULE_PUSHING])
    assert [(a, b) for a, b, _ in result] == [(RULE, RULE_TESTS_DIR), (RULE, RULE_PUSHING)]
    scores = [score for _, _, score in result]
    assert scores == sorted(scores, reverse=True)


def test_close_matches_ignores_position() -> None:
    result = close_matches(["abcd", "abce"], ["abce", "abcd"])
    assert result == [("abcd", "abcd", 1.0), ("abce", "abce", 1.0)]


@pytest.mark.parametrize(
    ("cutoff", "expected"),
    [
        pytest.param(0.75, [(RULE, RULE_PUSHING)], id="default-includes-0.79"),
        pytest.param(0.8, [], id="raised-excludes-0.79"),
        pytest.param(0.0, [(RULE, RULE_PUSHING)], id="zero-pairs-everything"),
    ],
)
def test_close_matches_cutoff(cutoff: float, expected: list[tuple[str, str]]) -> None:
    result = close_matches([RULE], [RULE_PUSHING], cutoff=cutoff)
    assert [(a, b) for a, b, _ in result] == expected
    assert all(score >= cutoff for _, _, score in result)


def test_close_matches_zero_cutoff_pairs_unrelated_strings() -> None:
    result = close_matches(["abc", "def"], ["xyz"], cutoff=0.0)
    assert [(a, b) for a, b, _ in result] == [("abc", "xyz")]


@pytest.mark.parametrize(("main", "other"), [([], []), (["a"], []), ([], ["a"])])
def test_close_matches_empty(main: list[str], other: list[str]) -> None:
    assert close_matches(main, other) == []


def test_close_matches_returns_plain_list_of_triples() -> None:
    result: list[tuple[str, str, float]] = close_matches(["same"], ["same"])
    assert result == [("same", "same", 1.0)]
    assert all(isinstance(item, tuple) and len(item) == 3 for item in result)


def test_close_matches_scores_are_ratios() -> None:
    result: list[Any] = close_matches([RULE], [RULE_PUSHING])
    assert result[0][2] == ratio(RULE, RULE_PUSHING)
