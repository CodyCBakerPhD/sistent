"""``toml`` aspect type: selected keys of a TOML file (``pyproject.toml``) compared under per-key modes.

Each configured dotted key is compared with one of five modes:

* ``exact`` — the value must be identical (mappings are descended, leaf locators such as
  ``pyproject.toml:tool.ruff.line-length``);
* ``set`` — a list compared as a set of elements, order and duplicates ignored;
* ``keys`` — a table whose key set must match, values ignored (``project.urls``);
* ``requirements`` — a list of PEP 508 requirements compared by package name, with a low-priority note when the
  specifier of a shared package differs;
* ``present`` — the key only has to exist in the satellite when it exists in main.

Every mode is gated on main: a key absent in main is never required in a satellite (only reported as an upstream
``extra`` when the satellite has it).
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sistent.aspects.base import Aspect, UnparseableFile
from sistent.compare.findings import findings_from_keydiffs, findings_from_setdiff, format_value
from sistent.compare.mappings import MISSING, diff_mapping, get_path, parse_requirement
from sistent.compare.sets import diff_sets
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject, locator
from sistent.options import BaseOptions, OptionsError
from sistent.parsers import ParseError, toml_
from sistent.repository import RepoContext

MODES: frozenset[str] = frozenset({"exact", "set", "keys", "requirements", "present"})
"""Valid values of :attr:`TomlOptions.keys`."""


@dataclass(frozen=True, kw_only=True)
class TomlOptions(BaseOptions):
    """Options of the ``toml`` aspect type."""

    file: str = field(metadata={"help": "TOML file to compare, relative to the repository root."})
    keys: dict[str, str] = field(
        default_factory=dict,
        metadata={"help": "Dotted key -> comparison mode (exact | set | keys | requirements | present)."},
    )
    required: bool = field(
        default=False,
        metadata={"help": "Report the file as missing in a satellite even when main does not have it."},
    )

    def __post_init__(self) -> None:
        valid = ", ".join(sorted(MODES))
        for dotted, mode in self.keys.items():
            if not dotted:
                raise OptionsError("keys: empty dotted key")
            if mode not in MODES:
                raise OptionsError(f"keys.{dotted}: unknown mode {mode!r} (valid: {valid})")


class TomlAspect(Aspect):
    """Compare selected keys of one TOML file per key mode; see the module docstring."""

    type_name: ClassVar[str] = "toml"
    options_cls: ClassVar[type[BaseOptions]] = TomlOptions
    description: ClassVar[str] = "Selected keys of a TOML file (pyproject.toml) compared per key mode."

    def __init__(self, name: str, options: BaseOptions) -> None:
        if not isinstance(options, TomlOptions):
            raise TypeError(f"{type(self).__name__} requires TomlOptions, got {type(options).__name__}")
        super().__init__(name, options)
        self.opts: TomlOptions = options

    # ----- extraction -----------------------------------------------------------------------------------------

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """``{"present": bool, "values": {"<dotted>": value | None}}`` (``None`` = key absent)."""
        file = self.opts.file
        if not ctx.exists(file):
            return {"present": False, "values": {}}
        try:
            doc = toml_.load(ctx.read_text(file), source=file)
        except ParseError as exc:
            raise UnparseableFile(file, exc.reason) from exc
        values: dict[str, Any] = {}
        for dotted in self.opts.keys:
            value = get_path(doc, dotted)
            if value is MISSING:
                values[dotted] = None
            else:
                values[dotted] = _normalise(value, ctx, where=locator(file, dotted, sep=":"))
        return {"present": True, "values": values}

    # ----- comparison -----------------------------------------------------------------------------------------

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Findings for ``other`` relative to ``main``; see the module docstring for the per-mode semantics."""
        file = self.opts.file
        repo = other.repo
        main_present = bool(main.data.get("present"))
        other_present = bool(other.data.get("present"))
        if not other_present:
            if main_present or self.opts.required:
                option = None if main_present else self.option_ref("required")
                return [
                    self.finding(
                        repo=repo,
                        kind=Kind.MISSING,
                        subject=Subject.FILE,
                        locator=locator(file),
                        message=f"file missing: {file}",
                        option=option,
                    )
                ]
            return []
        if not main_present:
            return []

        main_values: Mapping[str, Any] = main.data.get("values", {})
        other_values: Mapping[str, Any] = other.data.get("values", {})
        out: list[Finding] = []
        for dotted, mode in self.opts.keys.items():
            main_value = main_values.get(dotted)
            other_value = other_values.get(dotted)
            where = locator(file, dotted, sep=":")
            if main_value is None:
                if other_value is not None:
                    out.append(
                        self.finding(
                            repo=repo,
                            kind=Kind.EXTRA,
                            subject=Subject.KEY,
                            locator=where,
                            message=f"key only in repo: {dotted}",
                            content_key=dotted,
                        )
                    )
                continue
            if other_value is None:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MISSING,
                        subject=Subject.KEY,
                        locator=where,
                        message=f"key missing: {dotted}",
                        content_key=dotted,
                    )
                )
                continue
            if mode == "present":
                continue
            if mode == "set" and isinstance(main_value, list) and isinstance(other_value, list):
                out.extend(self._compare_set(repo, where, main_value, other_value))
            elif mode == "keys" and isinstance(main_value, Mapping) and isinstance(other_value, Mapping):
                out.extend(self._compare_keys(repo, dotted, main_value, other_value))
            elif mode == "requirements" and isinstance(main_value, list) and isinstance(other_value, list):
                out.extend(self._compare_requirements(repo, where, main_value, other_value))
            else:
                # ``exact``, or a mode whose value shape does not fit (a scalar under ``set``, a list under
                # ``keys``): the values are compared whole, which yields a single ``differs`` leaf on mismatch.
                out.extend(self._compare_exact(repo, dotted, main_value, other_value))
        return out

    def _compare_exact(self, repo: str, dotted: str, main_value: Any, other_value: Any) -> list[Finding]:
        diffs = diff_mapping(main_value, other_value, path=tuple(dotted.split(".")))
        return findings_from_keydiffs(diffs, aspect=self, repo=repo, file=self.opts.file, authoritative=True)

    def _compare_set(self, repo: str, where: str, main_value: list[Any], other_value: list[Any]) -> list[Finding]:
        diff = diff_sets(main_value, other_value, key=_element_key)
        return findings_from_setdiff(
            diff,
            aspect=self,
            repo=repo,
            subject=Subject.VALUE,
            locator=lambda _item: where,
            describe=_element_key,
            content_key=_element_key,
        )

    def _compare_keys(
        self, repo: str, dotted: str, main_value: Mapping[str, Any], other_value: Mapping[str, Any]
    ) -> list[Finding]:
        file = self.opts.file
        diff = diff_sets([str(k) for k in main_value], [str(k) for k in other_value])
        child: Callable[[str], str] = lambda key: f"{dotted}.{key}"  # noqa: E731
        return findings_from_setdiff(
            diff,
            aspect=self,
            repo=repo,
            subject=Subject.KEY,
            locator=lambda key: locator(file, child(key), sep=":"),
            describe=child,
            content_key=child,
        )

    def _compare_requirements(
        self, repo: str, where: str, main_value: list[Any], other_value: list[Any]
    ) -> list[Finding]:
        main_reqs = [_requirement(item) for item in main_value]
        other_reqs = [_requirement(item) for item in other_value]
        diff = diff_sets(main_reqs, other_reqs, key=lambda req: req[0])
        out: list[Finding] = []
        for name, _rest, raw in diff.missing:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.VALUE,
                    locator=where,
                    message=f"requirement missing: {raw}",
                    content_key=name,
                    severity=Severity.WARNING,
                )
            )
        for name, _rest, raw in diff.extra:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.VALUE,
                    locator=where,
                    message=f"requirement only in repo: {raw}",
                    content_key=name,
                )
            )
        for (name, main_rest, main_raw), (_name, other_rest, other_raw) in diff.common:
            if main_rest != other_rest:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.DIFFERS,
                        subject=Subject.VALUE,
                        locator=where,
                        message=f"{name}: main {main_raw!r} ; repo {other_raw!r}",
                        content_key=name,
                        direction=Direction.DOWNSTREAM,
                        severity=Severity.INFO,
                    )
                )
        return out


# ----- helpers -----------------------------------------------------------------------------------------------------


def _normalise(value: Any, ctx: RepoContext, *, where: str) -> Any:
    """Substitute identities in every string (recursively) and make the value JSON-serialisable."""
    if isinstance(value, str):
        return ctx.substitute(value, where=where)
    if isinstance(value, Mapping):
        return {str(key): _normalise(item, ctx, where=where) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalise(item, ctx, where=where) for item in value]
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()
    return value


def _element_key(item: Any) -> str:
    """Identity of a list element: the string itself, or compact JSON for anything else."""
    return item if isinstance(item, str) else format_value(item)


def _requirement(item: Any) -> tuple[str, str, str]:
    """``(normalised name, remainder, original text)`` of one requirement list element."""
    raw = item if isinstance(item, str) else format_value(item)
    name, rest = parse_requirement(raw)
    return name, rest, raw.strip()


__all__ = ["MODES", "TomlAspect", "TomlOptions"]
