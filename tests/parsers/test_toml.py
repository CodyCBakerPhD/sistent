"""Tests for :mod:`sistent.parsers.toml_`."""

from __future__ import annotations

import pytest

from sistent.parsers import ParseError
from sistent.parsers.toml_ import load


def test_load_returns_nested_dicts() -> None:
    text = '[project]\nname = "x"\ndependencies = ["click>=8"]\n\n[tool.ruff]\nline-length = 120\n'
    assert load(text) == {
        "project": {"name": "x", "dependencies": ["click>=8"]},
        "tool": {"ruff": {"line-length": 120}},
    }


def test_load_empty_text_is_empty_mapping() -> None:
    assert load("") == {}
    assert load("# just a comment\n") == {}


@pytest.mark.parametrize(
    "text",
    [
        "[project\nname = 1",
        "a = ",
        "a = 1\na = 2",
        'name = "unterminated',
    ],
)
def test_invalid_toml_raises_parse_error(text: str) -> None:
    with pytest.raises(ParseError) as info:
        load(text, source="pyproject.toml")
    assert info.value.source == "pyproject.toml"
    assert info.value.reason
    assert str(info.value).startswith("pyproject.toml: ")


def test_parse_error_is_a_value_error_and_keeps_empty_source() -> None:
    assert issubclass(ParseError, ValueError)
    with pytest.raises(ValueError, match="Invalid statement") as info:
        load("= 1")
    assert isinstance(info.value, ParseError)
    assert info.value.source == ""
    assert str(info.value) == info.value.reason
