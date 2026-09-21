"""Text-to-structure parsers (Markdown, badges, TOML, YAML). No knowledge of findings.

Every parser turns a string into plain data and raises :class:`ParseError` when the input cannot be interpreted at
all (malformed TOML/YAML, absurd input); parsers of loosely structured text (Markdown, badges) never raise on odd
input and simply do their best. :class:`MissingDependency` signals an optional third-party library that is not
installed.
"""

from __future__ import annotations


class ParseError(ValueError):
    """The input of a parser could not be interpreted.

    ``source`` is the caller-supplied label of the text (usually a file path relative to the repository root, may be
    empty), ``reason`` a human-readable description of the problem.
    """

    def __init__(self, source: str, reason: str) -> None:
        self.source = source
        self.reason = reason
        super().__init__(f"{source}: {reason}" if source else reason)


class MissingDependency(RuntimeError):
    """A third-party library needed by a parser is not installed."""
