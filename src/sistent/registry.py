"""Aspect type registry: built-in types, entry-point plugins and dotted ``pkg.module:Class`` paths.

A :class:`Registry` maps type names (``markdown``, ``tree``, ...) to :class:`~sistent.aspects.base.Aspect` subclasses.
Targets may be registered as ``"pkg.module:ClassName"`` strings, which are imported on first use so that an aspect
whose optional dependency is missing only breaks when that aspect is actually requested. There is no module-level
mutable registry: :func:`default_registry` builds a fresh one from :data:`sistent.aspects.BUILTIN` and the
``sistent.aspects`` entry-point group.
"""

from __future__ import annotations

import difflib
import importlib
import importlib.metadata
from collections.abc import Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from sistent.aspects import BUILTIN
from sistent.aspects.base import Aspect

if TYPE_CHECKING:
    from sistent.config import AspectSpec

ENTRY_POINT_GROUP = "sistent.aspects"
"""Entry-point group through which built-in and third-party aspect types are discovered."""

_NO_TYPES: Mapping[str, type[Aspect] | str] = MappingProxyType({})


class RegistryError(LookupError):
    """Unknown aspect type, unimportable dotted path, target that is not an aspect, or a plugin name collision."""


def import_target(path: str) -> type[Aspect]:
    """Import ``"pkg.module:ClassName"`` and return the class, checking that it is an :class:`Aspect` subclass."""
    module_name, sep, attr = path.partition(":")
    module_name, attr = module_name.strip(), attr.strip()
    if not sep or not module_name or not attr:
        raise RegistryError(f"invalid aspect type path {path!r}: expected 'pkg.module:ClassName'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise RegistryError(f"cannot import aspect type {path!r}: {exc}") from exc
    obj: object = module
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise RegistryError(
                f"cannot import aspect type {path!r}: module {module_name!r} has no attribute {attr!r}"
            ) from None
    return _as_aspect_class(obj, path)


def _as_aspect_class(obj: object, label: str) -> type[Aspect]:
    if isinstance(obj, type) and issubclass(obj, Aspect) and obj is not Aspect:
        return obj
    raise RegistryError(f"{label!r} is not an Aspect subclass (got {obj!r})")


def _target_string(target: type[Aspect] | str) -> str:
    if isinstance(target, str):
        return target
    return f"{target.__module__}:{target.__qualname__}"


class Registry:
    """Name -> aspect class lookup with lazy import of string targets."""

    def __init__(self, types: Mapping[str, type[Aspect] | str] = _NO_TYPES) -> None:
        self._types: dict[str, type[Aspect] | str] = {}
        for name, target in types.items():
            self.register(name, target)

    def __repr__(self) -> str:
        return f"Registry({', '.join(self.names())})"

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._types

    def names(self) -> list[str]:
        """Registered type names, sorted."""
        return sorted(self._types)

    def has(self, name: str) -> bool:
        return name in self._types

    def get(self, name: str) -> type[Aspect]:
        """The class for a registered name, or for a dotted ``pkg.module:ClassName`` path.

        Raises :class:`RegistryError` for unknown names (with a did-you-mean hint), unimportable paths and targets
        that are not :class:`Aspect` subclasses.
        """
        if ":" in name:
            return import_target(name)
        target = self._types.get(name)
        if target is None:
            close = difflib.get_close_matches(name, self.names(), n=1, cutoff=0.6)
            hint = f" (did you mean '{close[0]}'?)" if close else ""
            known = ", ".join(self.names()) or "(none)"
            raise RegistryError(
                f"unknown aspect type {name!r}{hint}. Known types: {known}; "
                "a dotted 'pkg.module:ClassName' path is also accepted"
            )
        if isinstance(target, str):
            cls = import_target(target)
            self._types[name] = cls
            return cls
        return target

    def register(self, name: str, cls: type[Aspect] | str, *, replace: bool = False) -> None:
        """Register ``cls`` (a class or a ``"pkg.module:ClassName"`` string, imported lazily) under ``name``."""
        if not name or ":" in name:
            raise RegistryError(f"invalid aspect type name {name!r}")
        if name in self._types and not replace:
            raise RegistryError(
                f"aspect type {name!r} is already registered ({_target_string(self._types[name])}); "
                "pass replace=True to override it"
            )
        if isinstance(cls, str):
            module_name, sep, attr = cls.partition(":")
            if not sep or not module_name.strip() or not attr.strip():
                raise RegistryError(f"invalid aspect type path {cls!r} for {name!r}: expected 'pkg.module:ClassName'")
            self._types[name] = cls
        else:
            self._types[name] = _as_aspect_class(cls, name)

    def target(self, name: str) -> str | None:
        """The dotted ``module:Class`` string of a registered name (without importing it), or ``None``."""
        target = self._types.get(name)
        return None if target is None else _target_string(target)

    def build(self, spec: AspectSpec) -> Aspect:
        """Instantiate the aspect described by ``spec``: ``cls(spec.name, spec.options)``."""
        cls = self.get(spec.type)
        return cls(spec.name, spec.options)


def default_registry(*, entry_points: bool = True) -> Registry:
    """The built-in types plus, when ``entry_points`` is true, every ``sistent.aspects`` entry point.

    An entry point whose name is already registered is ignored when it resolves to the same target (sistent
    registers its own built-ins through the entry-point group, so that case is the norm) and is a
    :class:`RegistryError` when it points somewhere else. Targets stay unimported until first use.
    """
    registry = Registry(BUILTIN)
    if not entry_points:
        return registry
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        if not ep.attr:
            raise RegistryError(f"entry point {ep.name!r} ({ENTRY_POINT_GROUP}) must name a class, got {ep.value!r}")
        target = f"{ep.module}:{ep.attr}"
        existing = registry.target(ep.name)
        if existing is not None:
            if existing == target:
                continue
            dist = ep.dist.name if ep.dist is not None else "an unknown distribution"
            raise RegistryError(
                f"aspect type {ep.name!r} is provided twice: {existing} and {target} (entry point from {dist}); "
                "uninstall one of the plugins or rename the entry point"
            )
        registry.register(ep.name, target)
    return registry


__all__ = ["ENTRY_POINT_GROUP", "Registry", "RegistryError", "default_registry", "import_target"]
