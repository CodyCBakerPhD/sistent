"""TOML loading on top of :mod:`tomllib` with the package's :class:`ParseError`."""

from __future__ import annotations

import tomllib
from typing import Any

from sistent.parsers import ParseError


def load(text: str, *, source: str = "") -> dict[str, Any]:
    """Parse ``text`` as TOML.

    Raises :class:`ParseError` (with ``source`` and the decoder's message as ``reason``) on invalid input.
    """
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ParseError(source, str(exc)) from exc
