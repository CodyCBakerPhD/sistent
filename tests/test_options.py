"""Tests for the typed option dataclasses: description, strict parsing and type checks."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional  # Optional is exercised on purpose

import pytest

from sistent.options import (
    BaseOptions,
    OptionInfo,
    OptionsError,
    check_type,
    describe_options,
    options_to_dict,
    parse_options,
)


@dataclass(frozen=True, kw_only=True)
class EchoOptions(BaseOptions):
    files: list[str] = field(metadata={"help": "Files to read."})
    threshold: float = field(default=0.6, metadata={"help": "Similarity threshold."})
    depth: int = field(default=2, metadata={"help": "How deep to look."})
    label: str | None = field(default=None, metadata={"help": "Optional label."})
    sections: dict[str, str] = field(default_factory=dict, metadata={"help": "Section modes."})
    aliases: dict[str, list[str]] = field(default_factory=dict, metadata={"help": "Heading aliases."})


WHERE = "aspects.readme"


def happy_mapping() -> dict[str, Any]:
    return {
        "enabled": True,
        "tags": ["python", "docs"],
        "ignore": ["README.md#badges"],
        "severity": {"missing.file": "error", "extra": "info"},
        "files": ["README.md", "docs/index.md"],
        "threshold": 0.8,
        "depth": 3,
        "label": "readme",
        "sections": {"installation": "similar"},
        "aliases": {"installation": ["install", "getting started"]},
    }


# ----- describe_options --------------------------------------------------------------------------------------------


class TestDescribeOptions:
    def test_base_fields_with_help(self) -> None:
        infos = describe_options(BaseOptions)
        assert [i.name for i in infos] == ["enabled", "tags", "ignore", "severity"]
        assert all(isinstance(i, OptionInfo) for i in infos)
        assert all(i.help for i in infos), "every base option carries help text"
        by_name = {i.name: i for i in infos}
        assert by_name["enabled"].type == "bool"
        assert by_name["enabled"].default is True
        assert by_name["tags"].type == "list[str] | none"
        assert by_name["tags"].default is None
        assert by_name["ignore"].type == "list[str]"
        assert by_name["ignore"].default == []
        assert by_name["severity"].type == "dict[str, str]"
        assert by_name["severity"].default == {}

    def test_subclass_lists_base_first_and_marks_required(self) -> None:
        infos = describe_options(EchoOptions)
        assert [i.name for i in infos] == [
            "enabled",
            "tags",
            "ignore",
            "severity",
            "files",
            "threshold",
            "depth",
            "label",
            "sections",
            "aliases",
        ]
        by_name = {i.name: i for i in infos}
        assert by_name["files"].default == "(required)"
        assert by_name["files"].type == "list[str]"
        assert by_name["files"].help == "Files to read."
        assert by_name["threshold"].type == "float"
        assert by_name["threshold"].default == 0.6
        assert by_name["depth"].type == "int"
        assert by_name["label"].type == "str | none"
        assert by_name["sections"].type == "dict[str, str]"
        assert by_name["aliases"].type == "dict[str, list[str]]"

    def test_missing_help_is_empty_string(self) -> None:
        @dataclass(frozen=True, kw_only=True)
        class Bare(BaseOptions):
            flag: bool = False

        (info,) = [i for i in describe_options(Bare) if i.name == "flag"]
        assert info.help == ""
        assert info.default is False


# ----- parse_options -----------------------------------------------------------------------------------------------


class TestParseOptionsHappyPath:
    def test_all_fields(self) -> None:
        opts = parse_options(EchoOptions, happy_mapping(), where=WHERE)
        assert isinstance(opts, EchoOptions)
        assert opts.enabled is True
        assert opts.tags == ["python", "docs"]
        assert opts.ignore == ["README.md#badges"]
        assert opts.severity == {"missing.file": "error", "extra": "info"}
        assert opts.files == ["README.md", "docs/index.md"]
        assert opts.threshold == 0.8
        assert opts.depth == 3
        assert opts.label == "readme"
        assert opts.sections == {"installation": "similar"}
        assert opts.aliases == {"installation": ["install", "getting started"]}

    def test_defaults_apply_when_omitted(self) -> None:
        opts = parse_options(EchoOptions, {"files": ["README.md"]}, where=WHERE)
        assert opts.enabled is True
        assert opts.tags is None
        assert opts.ignore == []
        assert opts.severity == {}
        assert opts.threshold == 0.6
        assert opts.depth == 2
        assert opts.label is None
        assert opts.sections == {}
        assert opts.aliases == {}

    def test_base_class_itself(self) -> None:
        opts = parse_options(BaseOptions, {"enabled": False, "tags": ["x"]}, where=WHERE)
        assert type(opts) is BaseOptions
        assert opts.enabled is False
        assert opts.tags == ["x"]

    def test_int_accepted_for_float(self) -> None:
        opts = parse_options(EchoOptions, {"files": ["a"], "threshold": 1}, where=WHERE)
        assert opts.threshold == 1

    def test_none_accepted_for_optional(self) -> None:
        opts = parse_options(EchoOptions, {"files": ["a"], "tags": None, "label": None}, where=WHERE)
        assert opts.tags is None
        assert opts.label is None

    def test_values_are_copied_from_the_mapping(self) -> None:
        mapping = happy_mapping()
        opts = parse_options(EchoOptions, mapping, where=WHERE)
        mapping["files"].append("EXTRA.md")
        mapping["aliases"]["installation"].append("setup")
        mapping["sections"]["new"] = "exact"
        assert opts.files == ["README.md", "docs/index.md"]
        assert opts.aliases == {"installation": ["install", "getting started"]}
        assert opts.sections == {"installation": "similar"}

    def test_severity_values_are_case_insensitive(self) -> None:
        opts = parse_options(EchoOptions, {"files": ["a"], "severity": {"differs.rule": "WARNING"}}, where=WHERE)
        assert opts.severity == {"differs.rule": "WARNING"}


class TestParseOptionsErrors:
    def test_unknown_key_with_did_you_mean(self) -> None:
        with pytest.raises(
            OptionsError, match=r"aspects\.readme\.threshhold: unknown option \(did you mean 'threshold'\?\)"
        ) as info:
            parse_options(EchoOptions, {"files": ["a"], "threshhold": 0.5}, where=WHERE)
        assert "Valid options:" in str(info.value)
        assert "threshold" in str(info.value)

    def test_unknown_key_without_close_match(self) -> None:
        with pytest.raises(OptionsError, match=r"aspects\.readme\.zzz: unknown option\. Valid options: ") as info:
            parse_options(EchoOptions, {"files": ["a"], "zzz": 1}, where=WHERE)
        assert "did you mean" not in str(info.value)

    def test_several_unknown_keys_are_all_reported(self) -> None:
        with pytest.raises(OptionsError, match=r"treshold.*; .*aspects\.readme\.filez"):
            parse_options(EchoOptions, {"treshold": 0.5, "filez": ["a"]}, where=WHERE)

    @pytest.mark.parametrize(
        ("mapping", "key", "expected_type", "got"),
        [
            ({"files": ["a"], "enabled": "true"}, "enabled", "bool", "str"),
            ({"files": ["a"], "enabled": 1}, "enabled", "bool", "int"),
            ({"files": ["a"], "depth": True}, "depth", "int", "bool"),
            ({"files": ["a"], "depth": 2.0}, "depth", "int", "float"),
            ({"files": ["a"], "threshold": "0.5"}, "threshold", "float", "str"),
            ({"files": ["a"], "threshold": True}, "threshold", "float", "bool"),
            ({"files": [1, 2]}, "files", "list[str]", "list"),
            ({"files": "README.md"}, "files", "list[str]", "str"),
            ({"files": ["a"], "tags": "python"}, "tags", "list[str] | none", "str"),
            ({"files": ["a"], "label": 3}, "label", "str | none", "int"),
            ({"files": ["a"], "sections": {"a": 1}}, "sections", "dict[str, str]", "dict"),
            ({"files": ["a"], "sections": ["a"]}, "sections", "dict[str, str]", "list"),
            ({"files": ["a"], "aliases": {"a": "b"}}, "aliases", "dict[str, list[str]]", "dict"),
            ({"files": ["a"], "aliases": {"a": [1]}}, "aliases", "dict[str, list[str]]", "dict"),
            ({"files": ["a"], "severity": "error"}, "severity", "dict[str, str]", "str"),
            ({"files": ["a"], "ignore": [None]}, "ignore", "list[str]", "list"),
        ],
    )
    def test_wrong_types_are_rejected_without_coercion(
        self, mapping: dict[str, Any], key: str, expected_type: str, got: str
    ) -> None:
        pattern = rf"aspects\.readme\.{key}: expected {re.escape(expected_type)}, got {got} \("
        with pytest.raises(OptionsError, match=pattern):
            parse_options(EchoOptions, mapping, where=WHERE)

    def test_missing_required(self) -> None:
        with pytest.raises(OptionsError, match=r"aspects\.readme: missing required option\(s\): files$"):
            parse_options(EchoOptions, {"threshold": 0.5}, where=WHERE)

    def test_missing_several_required(self) -> None:
        @dataclass(frozen=True, kw_only=True)
        class TwoRequired(BaseOptions):
            first: str
            second: int

        with pytest.raises(OptionsError, match=r"missing required option\(s\): first, second"):
            parse_options(TwoRequired, {}, where=WHERE)

    @pytest.mark.parametrize(
        ("table", "pattern"),
        [
            ({"bogus": "error"}, r"aspects\.readme\.severity: unknown key 'bogus'"),
            ({"missing.bogus": "error"}, r"aspects\.readme\.severity: unknown key 'missing\.bogus'"),
            ({"missing.file.extra": "error"}, r"unknown key 'missing\.file\.extra'"),
            (
                {"missing": "fatal"},
                r"aspects\.readme\.severity\.missing: invalid severity 'fatal' \(valid: info, warning, error\)",
            ),
            ({"missing.file": "ok", "extra": "info"}, r"severity\.missing\.file: invalid severity 'ok'"),
        ],
    )
    def test_severity_table_validation(self, table: dict[str, str], pattern: str) -> None:
        with pytest.raises(OptionsError, match=pattern):
            parse_options(EchoOptions, {"files": ["a"], "severity": table}, where=WHERE)

    def test_severity_table_error_lists_kinds(self) -> None:
        with pytest.raises(OptionsError, match=r"kinds: differs, extra, missing, moved, reordered, stale, unparseable"):
            parse_options(BaseOptions, {"severity": {"nope": "error"}}, where=WHERE)

    def test_severity_table_accepts_valid_keys(self) -> None:
        table = {
            "missing.file": "error",
            "missing": "warning",
            "extra": "info",
            "differs.rule": "warning",
            "stale.reference": "error",
        }
        opts = parse_options(BaseOptions, {"severity": table}, where=WHERE)
        assert opts.severity == table

    def test_options_error_is_a_value_error(self) -> None:
        assert issubclass(OptionsError, ValueError)


# ----- options_to_dict ---------------------------------------------------------------------------------------------


class TestOptionsToDict:
    def test_returns_plain_copies(self) -> None:
        opts = parse_options(EchoOptions, happy_mapping(), where=WHERE)
        d = options_to_dict(opts)
        assert d == happy_mapping()
        d["files"].append("EXTRA.md")
        d["aliases"]["installation"].append("setup")
        d["sections"]["new"] = "exact"
        d["severity"]["stale"] = "error"
        del d["enabled"]
        assert opts.files == ["README.md", "docs/index.md"]
        assert opts.aliases == {"installation": ["install", "getting started"]}
        assert opts.sections == {"installation": "similar"}
        assert opts.severity == {"missing.file": "error", "extra": "info"}
        assert opts.enabled is True

    def test_key_order_follows_fields(self) -> None:
        d = options_to_dict(BaseOptions())
        assert list(d) == ["enabled", "tags", "ignore", "severity"]
        assert d == {"enabled": True, "tags": None, "ignore": [], "severity": {}}


# ----- check_type --------------------------------------------------------------------------------------------------


class TestCheckType:
    @pytest.mark.parametrize(
        ("value", "hint", "expected"),
        [
            ("x", str, True),
            (1, str, False),
            (True, bool, True),
            ("true", bool, False),
            (1, bool, False),
            (1, int, True),
            (True, int, False),
            (1.0, int, False),
            ("1", int, False),
            (1.5, float, True),
            (1, float, True),
            (True, float, False),
            ("0.5", float, False),
            (["a", "b"], list[str], True),
            ([], list[str], True),
            (["a", 1], list[str], False),
            (("a",), list[str], False),
            ("a", list[str], False),
            ([1, 2], list[int], True),
            ([True], list[int], False),
            ({"a": "b"}, dict[str, str], True),
            ({}, dict[str, str], True),
            ({"a": 1}, dict[str, str], False),
            ({1: "a"}, dict[str, str], False),
            ({"a": ["b", "c"]}, dict[str, list[str]], True),
            ({"a": "b"}, dict[str, list[str]], False),
            ({"a": [1]}, dict[str, list[str]], False),
            (None, str | None, True),
            ("x", str | None, True),
            (1, str | None, False),
            (None, list[str] | None, True),
            (["x"], list[str] | None, True),
            ("x", list[str] | None, False),
            (None, Optional[int], True),  # noqa: UP045 - typing.Union spelling on purpose
            (3, Optional[int], True),  # noqa: UP045
            (None, str, False),
            (None, type(None), True),
            ("anything", Any, True),
            (None, Any, True),
            (1, int | str, True),
            ("s", int | str, True),
            (1.5, int | str, False),
        ],
    )
    def test_cases(self, value: Any, hint: Any, expected: bool) -> None:
        assert check_type(value, hint) is expected

    def test_bare_list_and_dict_accept_anything(self) -> None:
        assert check_type([1, "a"], list) is True
        assert check_type({"a": 1}, dict) is True
        assert check_type("a", list) is False
