"""Typed, self-documenting option dataclasses for aspect types.

Every aspect type declares an ``options_cls`` deriving from :class:`BaseOptions`. Options are parsed from the TOML
table with :func:`parse_options`, which rejects unknown keys (with a "did you mean" hint) and wrong value types, and
never coerces (``"true"`` is not a bool). Each field's ``metadata["help"]`` feeds ``sistent aspects``.
"""

from __future__ import annotations

import difflib
import types
import typing
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, field, fields
from typing import Any, TypeVar

from sistent.model import Kind, Severity, Subject


class OptionsError(ValueError):
    """Invalid option table for an aspect."""


@dataclass(frozen=True, kw_only=True)
class BaseOptions:
    """Options common to every aspect type."""

    enabled: bool = field(default=True, metadata={"help": "Set to false to remove this aspect instance."})
    tags: list[str] | None = field(
        default=None,
        metadata={"help": "Apply only to repos having any of these tags. Absent means every repo."},
    )
    ignore: list[str] = field(
        default_factory=list,
        metadata={"help": "Locator globs (fnmatch, case-insensitive) whose findings are suppressed in every repo."},
    )
    severity: dict[str, str] = field(
        default_factory=dict,
        metadata={"help": "Severity overrides keyed by '<kind>' or '<kind>.<subject>' (info | warning | error)."},
    )


OptionsT = TypeVar("OptionsT", bound=BaseOptions)


@dataclass(frozen=True)
class OptionInfo:
    name: str
    type: str
    default: Any
    help: str


def describe_options(cls: type[BaseOptions]) -> list[OptionInfo]:
    """Name, type, default and help of every option of ``cls`` (base options first)."""
    hints = typing.get_type_hints(cls)
    out: list[OptionInfo] = []
    for f in fields(cls):
        if f.default is not MISSING:
            default: Any = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:
            default = "(required)"
        out.append(OptionInfo(f.name, _type_name(hints[f.name]), default, str(f.metadata.get("help", ""))))
    return out


def options_to_dict(options: BaseOptions) -> dict[str, Any]:
    """Plain, JSON-serialisable dict of the option values (copies lists/dicts)."""
    out: dict[str, Any] = {}
    for f in fields(options):
        value = getattr(options, f.name)
        out[f.name] = _copy(value)
    return out


def parse_options(cls: type[OptionsT], mapping: Mapping[str, Any], *, where: str) -> OptionsT:
    """Build ``cls`` from a TOML table, validating keys and value types strictly."""
    hints = typing.get_type_hints(cls)
    known = {f.name: f for f in fields(cls)}
    unknown = [k for k in mapping if k not in known]
    if unknown:
        valid = ", ".join(sorted(known))
        parts = []
        for key in unknown:
            hint = ""
            close = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
            if close:
                hint = f" (did you mean '{close[0]}'?)"
            parts.append(f"{where}.{key}: unknown option{hint}")
        raise OptionsError(f"{'; '.join(parts)}. Valid options: {valid}")

    values: dict[str, Any] = {}
    for key, value in mapping.items():
        hint_type = hints[key]
        if not check_type(value, hint_type):
            raise OptionsError(
                f"{where}.{key}: expected {_type_name(hint_type)}, got {type(value).__name__} ({value!r})"
            )
        values[key] = _copy(value)

    missing = [
        name
        for name, f in known.items()
        if name not in values and f.default is MISSING and f.default_factory is MISSING
    ]
    if missing:
        raise OptionsError(f"{where}: missing required option(s): {', '.join(missing)}")

    if "severity" in values:
        _validate_severity_table(values["severity"], where=where)
    return cls(**values)


def _validate_severity_table(table: Mapping[str, str], *, where: str) -> None:
    kinds = {k.value for k in Kind}
    subjects = {s.value for s in Subject}
    for key, value in table.items():
        kind, _, subject = key.partition(".")
        if kind not in kinds or (subject and subject not in subjects):
            raise OptionsError(
                f"{where}.severity: unknown key {key!r} (use '<kind>' or '<kind>.<subject>'; "
                f"kinds: {', '.join(sorted(kinds))})"
            )
        try:
            Severity.parse(value)
        except ValueError as exc:
            raise OptionsError(f"{where}.severity.{key}: {exc}") from None


# ----- type checking helpers ---------------------------------------------------------------------------------------


def check_type(value: Any, hint: Any) -> bool:
    """Structural check of ``value`` against a limited set of annotations (no coercion).

    Supported: ``str``, ``bool``, ``int``, ``float``, ``list[X]``, ``dict[K, V]``, ``X | None`` / ``Optional[X]``,
    ``Any`` and unions of those. ``bool`` never satisfies ``int``/``float``; ``int`` satisfies ``float``.
    """
    if hint is Any:
        return True
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        return any(check_type(value, arg) for arg in typing.get_args(hint))
    if hint is type(None):
        return value is None
    if hint is bool:
        return isinstance(value, bool)
    if hint is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if hint is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if hint is str:
        return isinstance(value, str)
    if origin is list:
        (item_type,) = typing.get_args(hint) or (Any,)
        return isinstance(value, list) and all(check_type(v, item_type) for v in value)
    if origin is dict:
        args = typing.get_args(hint) or (Any, Any)
        key_type, value_type = args
        return isinstance(value, dict) and all(
            check_type(k, key_type) and check_type(v, value_type) for k, v in value.items()
        )
    if isinstance(hint, type):
        return isinstance(value, hint)
    return False


def _type_name(hint: Any) -> str:
    if hint is Any:
        return "any"
    origin = typing.get_origin(hint)
    if origin is types.UnionType or origin is typing.Union:
        return " | ".join(_type_name(a) for a in typing.get_args(hint))
    if hint is type(None):
        return "none"
    if origin is list:
        args = typing.get_args(hint)
        return f"list[{_type_name(args[0])}]" if args else "list"
    if origin is dict:
        args = typing.get_args(hint)
        return f"dict[{_type_name(args[0])}, {_type_name(args[1])}]" if args else "dict"
    if isinstance(hint, type):
        return hint.__name__
    return str(hint)


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value
