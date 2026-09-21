"""Tests for :mod:`sistent.parsers.yaml_` (PyYAML wrapper with the YAML 1.1 boolean-key fix)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from sistent.parsers import MissingDependency, ParseError
from sistent.parsers.yaml_ import get, load


def test_github_workflow_on_key_is_a_string() -> None:
    doc = load("name: CI\non:\n  push:\n    branches: [main]\n  pull_request:\njobs: {}\n")
    assert True not in doc
    assert doc["on"] == {"push": {"branches": ["main"]}, "pull_request": None}
    assert list(doc) == ["name", "on", "jobs"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("on: 1\noff: 2\n", {"on": 1, "off": 2}),
        ("a:\n  on: 1\n  off: 2\n", {"a": {"on": 1, "off": 2}}),
        ("a:\n  - on: 1\n  - off: 2\n", {"a": [{"on": 1}, {"off": 2}]}),
        ("True: 1\nFalse: 2\n", {"on": 1, "off": 2}),
        ("yes: 1\nno: 2\n", {"on": 1, "off": 2}),  # YAML 1.1 reads these as booleans too
        ("deep:\n  deeper:\n    deepest:\n      on: [1]\n", {"deep": {"deeper": {"deepest": {"on": [1]}}}}),
        ("1: int key stays\n", {1: "int key stays"}),
        ("on: true\n", {"on": True}),  # boolean *values* are untouched
    ],
)
def test_boolean_keys_become_on_off_in_every_mapping(text: str, expected: Any) -> None:
    assert load(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("# only a comment\n", None),
        ("42", 42),
        ("- a\n- b\n", ["a", "b"]),
        ("key: value", {"key": "value"}),
    ],
)
def test_empty_and_scalar_documents_pass_through(text: str, expected: Any) -> None:
    assert load(text) == expected


@pytest.mark.parametrize("text", ["a: [1, 2", "a: b: c", "\tindented with tab: 1", "key: 'unterminated"])
def test_invalid_yaml_raises_parse_error(text: str) -> None:
    with pytest.raises(ParseError) as info:
        load(text, source="ci.yml")
    assert info.value.source == "ci.yml"
    assert info.value.reason
    assert str(info.value).startswith("ci.yml: ")


def test_missing_pyyaml_raises_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(MissingDependency, match="pip install pyyaml"):
        load("a: 1")


def test_missing_dependency_is_a_runtime_error() -> None:
    assert issubclass(MissingDependency, RuntimeError)


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (("jobs", "test", "runs-on"), "ubuntu-latest"),
        (("jobs", "test", "steps", 0, "uses"), "actions/checkout@v4"),
        (("jobs", "test", "steps", -1, "uses"), "actions/setup-python@v5"),
        (("jobs", "test", "steps", 5, "uses"), "default"),
        (("jobs", "missing"), "default"),
        (("jobs", "test", "runs-on", 0), "default"),  # strings are not indexed
        (("jobs", "test", "steps", "0"), "default"),  # sequences need integer keys
        (("on", "push"), None),
        ((), None),
    ],
)
def test_get_walks_mappings_and_sequences_safely(keys: tuple[str | int, ...], expected: Any) -> None:
    doc = load(
        "on:\n  push:\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
        "    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-python@v5\n"
    )
    if keys == ():
        assert get(doc, *keys) is doc
    else:
        assert get(doc, *keys, default="default") == expected


def test_get_on_none_document() -> None:
    assert get(None, "a") is None
    assert get(None, "a", default=[]) == []
