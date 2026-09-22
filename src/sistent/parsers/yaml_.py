"""YAML loading via PyYAML's ``safe_load`` with the YAML 1.1 boolean-key fix applied.

PyYAML reads ``on:`` (the GitHub Actions trigger key) as the boolean ``True`` and ``off:`` as ``False``. :func:`load`
maps such keys back to the strings ``"on"`` / ``"off"`` in every mapping of the document so callers can address
``doc["on"]`` as written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sistent.parsers import MissingDependency, ParseError

_BOOL_KEYS: dict[bool, str] = {True: "on", False: "off"}


def _fix_bool_keys(value: Any) -> Any:
    if isinstance(value, dict):
        fixed: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = _BOOL_KEYS[key] if isinstance(key, bool) else key
            fixed[new_key] = _fix_bool_keys(item)
        return fixed
    if isinstance(value, list):
        return [_fix_bool_keys(item) for item in value]
    return value


def load(text: str, *, source: str = "") -> Any:
    """Parse ``text`` with ``yaml.safe_load`` and normalise boolean mapping keys to ``"on"`` / ``"off"``.

    An empty document yields ``None`` (as PyYAML does); the caller decides what that means. Raises
    :class:`ParseError` on malformed YAML and :class:`MissingDependency` when PyYAML is not installed.
    """
    try:
        import yaml
    except ImportError as exc:
        raise MissingDependency("PyYAML is required for parsing YAML files; pip install pyyaml") from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParseError(source, str(exc)) from exc
    return _fix_bool_keys(doc)


def get(doc: Any, *keys: str | int, default: Any = None) -> Any:
    """Nested lookup that never raises: ``get(doc, "jobs", "test", "steps", 0, "uses")``.

    String keys index mappings, integer keys index sequences (not strings); ``default`` is returned as soon as a
    step cannot be taken.
    """
    current = doc
    for key in keys:
        if isinstance(current, Mapping):
            if key not in current:
                return default
            current = current[key]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes) and isinstance(key, int):
            if not -len(current) <= key < len(current):
                return default
            current = current[key]
        else:
            return default
    return current
