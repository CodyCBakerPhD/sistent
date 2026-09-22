"""Tests for :mod:`sistent.compare.mappings`."""

from __future__ import annotations

import copy
import pickle
from types import MappingProxyType
from typing import Any

import pytest

from sistent.compare.mappings import MISSING, KeyDiff, Missing, diff_mapping, get_path, parse_requirement

# ----- diff_mapping ------------------------------------------------------------------------------------------------

DIFF_CASES = [
    pytest.param({}, {}, [], id="both-empty"),
    pytest.param({"a": 1}, {"a": 1}, [], id="equal-scalar"),
    pytest.param({"a": 1}, {"a": 2}, [KeyDiff(("a",), "differs", 1, 2)], id="differs-scalar"),
    pytest.param({"a": 1}, {}, [KeyDiff(("a",), "missing", 1, MISSING)], id="missing-key"),
    pytest.param({}, {"a": 1}, [KeyDiff(("a",), "extra", MISSING, 1)], id="extra-key"),
    pytest.param(
        {"tool": {"ruff": {"line-length": 120, "select": ["E"]}}},
        {"tool": {"ruff": {"line-length": 100, "target": "py311"}}},
        [
            KeyDiff(("tool", "ruff", "line-length"), "differs", 120, 100),
            KeyDiff(("tool", "ruff", "select"), "missing", ["E"], MISSING),
            KeyDiff(("tool", "ruff", "target"), "extra", MISSING, "py311"),
        ],
        id="nested",
    ),
    pytest.param(
        {"a": {"b": {"c": 1}}},
        {"a": {}},
        [KeyDiff(("a", "b"), "missing", {"c": 1}, MISSING)],
        id="missing-subtree-reported-once",
    ),
    pytest.param({"a": [1, 2]}, {"a": [1, 2]}, [], id="equal-list"),
    pytest.param({"a": [1, 2]}, {"a": [2, 1]}, [KeyDiff(("a",), "differs", [1, 2], [2, 1])], id="list-is-a-leaf"),
    pytest.param(
        {"a": [{"x": 1}]},
        {"a": [{"x": 2}]},
        [KeyDiff(("a",), "differs", [{"x": 1}], [{"x": 2}])],
        id="list-of-dicts-is-a-leaf",
    ),
    pytest.param({"a": {"b": 1}}, {"a": 1}, [KeyDiff(("a",), "differs", {"b": 1}, 1)], id="dict-vs-scalar"),
    pytest.param({"a": 1}, {"a": {"b": 1}}, [KeyDiff(("a",), "differs", 1, {"b": 1})], id="scalar-vs-dict"),
    pytest.param({"a": {"b": 1}}, {"a": [1]}, [KeyDiff(("a",), "differs", {"b": 1}, [1])], id="dict-vs-list"),
    pytest.param({"a": None}, {}, [KeyDiff(("a",), "missing", None, MISSING)], id="none-is-a-value"),
    pytest.param({"a": None}, {"a": None}, [], id="none-equals-none"),
    pytest.param({"a": None}, {"a": 0}, [KeyDiff(("a",), "differs", None, 0)], id="none-vs-zero"),
    pytest.param({"a": "1"}, {"a": 1}, [KeyDiff(("a",), "differs", "1", 1)], id="str-vs-int"),
]


@pytest.mark.parametrize(("main", "other", "expected"), DIFF_CASES)
def test_diff_mapping(main: Any, other: Any, expected: list[KeyDiff]) -> None:
    assert diff_mapping(main, other) == expected


def test_diff_mapping_order_is_main_keys_then_other_only_keys() -> None:
    main = {"b": 1, "a": 2, "c": 3}
    other = {"z": 0, "a": 2, "y": 0, "b": 9}
    assert [(d.kind, d.path) for d in diff_mapping(main, other)] == [
        ("differs", ("b",)),
        ("missing", ("c",)),
        ("extra", ("z",)),
        ("extra", ("y",)),
    ]


@pytest.mark.parametrize(
    ("max_depth", "expected_path"),
    [
        pytest.param(None, ("tool", "ruff", "x"), id="unlimited"),
        pytest.param(0, (), id="zero-compares-roots"),
        pytest.param(1, ("tool",), id="one"),
        pytest.param(2, ("tool", "ruff"), id="two"),
        pytest.param(3, ("tool", "ruff", "x"), id="three-reaches-leaf"),
        pytest.param(99, ("tool", "ruff", "x"), id="deeper-than-tree"),
    ],
)
def test_diff_mapping_max_depth(max_depth: int | None, expected_path: tuple[str, ...]) -> None:
    main = {"tool": {"ruff": {"x": 1}}}
    other = {"tool": {"ruff": {"x": 2}}}
    diffs = diff_mapping(main, other, max_depth=max_depth)
    assert len(diffs) == 1
    assert diffs[0].kind == "differs"
    assert diffs[0].path == expected_path
    assert diffs[0].main == get_path(main, ".".join(expected_path))
    assert diffs[0].other == get_path(other, ".".join(expected_path))


def test_diff_mapping_max_depth_turns_missing_keys_into_whole_value_differs() -> None:
    diffs = diff_mapping({"a": {"b": 1}}, {"a": {}}, max_depth=1)
    assert diffs == [KeyDiff(("a",), "differs", {"b": 1}, {})]


def test_diff_mapping_max_depth_zero_equal_roots() -> None:
    assert diff_mapping({"a": {"b": 1}}, {"a": {"b": 1}}, max_depth=0) == []


def test_diff_mapping_path_prefix() -> None:
    diffs = diff_mapping({"a": 1}, {"a": 2}, path=("tool", "x"))
    assert diffs == [KeyDiff(("tool", "x", "a"), "differs", 1, 2)]


@pytest.mark.parametrize(
    ("main", "other", "expected"),
    [
        pytest.param(MISSING, MISSING, [], id="both-missing"),
        pytest.param(1, MISSING, [KeyDiff(("k",), "missing", 1, MISSING)], id="main-only"),
        pytest.param(MISSING, 1, [KeyDiff(("k",), "extra", MISSING, 1)], id="other-only"),
        pytest.param({"a": 1}, MISSING, [KeyDiff(("k",), "missing", {"a": 1}, MISSING)], id="dict-vs-missing"),
        pytest.param(1, 1, [], id="equal-scalars"),
        pytest.param(1, 2, [KeyDiff(("k",), "differs", 1, 2)], id="scalar-roots"),
    ],
)
def test_diff_mapping_with_missing_roots(main: Any, other: Any, expected: list[KeyDiff]) -> None:
    """``diff_mapping(get_path(a, k), get_path(b, k), path=(k,))`` behaves like a key lookup."""
    assert diff_mapping(main, other, path=("k",)) == expected


def test_diff_mapping_non_string_keys_are_stringified_in_path() -> None:
    diffs = diff_mapping({1: "x"}, {})
    assert diffs == [KeyDiff(("1",), "missing", "x", MISSING)]


def test_diff_mapping_accepts_any_mapping() -> None:
    main = MappingProxyType({"a": MappingProxyType({"b": 1})})
    diffs = diff_mapping(main, {"a": {"b": 2}})
    assert diffs == [KeyDiff(("a", "b"), "differs", 1, 2)]


def test_keydiff_dotted() -> None:
    assert KeyDiff(("tool", "ruff", "line-length"), "differs", 1, 2).dotted == "tool.ruff.line-length"
    assert KeyDiff((), "differs", 1, 2).dotted == ""


# ----- get_path ----------------------------------------------------------------------------------------------------

DOC = {"tool": {"ruff": {"line-length": 120, "none": None}}, "a.b": "dotted", "list": [1, 2]}


@pytest.mark.parametrize(
    ("dotted", "expected"),
    [
        pytest.param("tool.ruff.line-length", 120, id="nested"),
        pytest.param("tool.ruff", {"line-length": 120, "none": None}, id="subtree"),
        pytest.param("tool.ruff.none", None, id="none-value-is-not-missing"),
        pytest.param("", DOC, id="empty-path-is-doc"),
        pytest.param("missing", MISSING, id="absent-top"),
        pytest.param("tool.black", MISSING, id="absent-nested"),
        pytest.param("tool.ruff.line-length.x", MISSING, id="through-scalar"),
        pytest.param("list.0", MISSING, id="through-list"),
        pytest.param("a.b", MISSING, id="keys-containing-dots-unsupported"),
        pytest.param("tool..ruff", MISSING, id="empty-segment"),
    ],
)
def test_get_path(dotted: str, expected: Any) -> None:
    assert get_path(DOC, dotted) == expected


def test_get_path_result_identity() -> None:
    assert get_path(DOC, "missing") is MISSING
    assert get_path(DOC, "tool.ruff.none") is None


def test_get_path_accepts_any_mapping() -> None:
    assert get_path(MappingProxyType({"a": MappingProxyType({"b": 1})}), "a.b") == 1


# ----- Missing -----------------------------------------------------------------------------------------------------


def test_missing_is_a_singleton_with_repr() -> None:
    assert Missing() is MISSING
    assert repr(MISSING) == "<missing>"
    assert MISSING is not None
    assert MISSING != None  # noqa: E711 - the point is that the sentinel is distinct from None
    assert copy.copy(MISSING) is MISSING
    assert copy.deepcopy(MISSING) is MISSING
    assert pickle.loads(pickle.dumps(MISSING)) is MISSING


# ----- parse_requirement -------------------------------------------------------------------------------------------

REQUIREMENT_CASES = [
    pytest.param(
        "Pytest-Cov >= 4, <5 ; python_version<'3.12'",
        ("pytest-cov", ">=4,<5;python_version<'3.12'"),
        id="specifier-and-marker",
    ),
    pytest.param("numpy[extra]>=1", ("numpy", "[extra]>=1"), id="extras"),
    pytest.param("numpy [extra1, extra2] >= 1", ("numpy", "[extra1,extra2]>=1"), id="extras-spaced"),
    pytest.param("pkg @ https://example.com/pkg-1.0.zip", ("pkg", "@https://example.com/pkg-1.0.zip"), id="url"),
    pytest.param("pytest", ("pytest", ""), id="bare"),
    pytest.param("  pytest  ", ("pytest", ""), id="bare-padded"),
    pytest.param("Django", ("django", ""), id="lowercased"),
    pytest.param("zope.interface", ("zope-interface", ""), id="dot-normalised"),
    pytest.param("Foo__Bar-baz.qux==1", ("foo-bar-baz-qux", "==1"), id="pep503-runs-collapsed"),
    pytest.param("pytest (>=7)", ("pytest", "(>=7)"), id="parenthesised-specifier"),
    pytest.param("requests~=2.0", ("requests", "~=2.0"), id="compatible-release"),
    pytest.param("requests!=2.0", ("requests", "!=2.0"), id="exclusion"),
    pytest.param("requests==2.0.*", ("requests", "==2.0.*"), id="wildcard"),
    pytest.param("pkg; extra == 'x'", ("pkg", ";extra=='x'"), id="marker-only"),
    pytest.param("", ("", ""), id="empty"),
    pytest.param("   ", ("", ""), id="whitespace"),
    pytest.param("not a requirement", ("not a requirement", ""), id="prose"),
    pytest.param("-e .", ("-e .", ""), id="editable-flag"),
    pytest.param("./local/path", ("./local/path", ""), id="path"),
]


@pytest.mark.parametrize(("requirement", "expected"), REQUIREMENT_CASES)
def test_parse_requirement(requirement: str, expected: tuple[str, str]) -> None:
    assert parse_requirement(requirement) == expected


def test_parse_requirement_names_compare_across_spellings() -> None:
    assert parse_requirement("Pytest_Cov")[0] == parse_requirement("pytest-cov>=4")[0] == "pytest-cov"
