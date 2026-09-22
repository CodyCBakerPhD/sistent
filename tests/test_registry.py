"""Tests for the aspect type registry: names, dotted paths, plugins and lazy import."""

from __future__ import annotations

import importlib.metadata
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from sistent.aspects import BUILTIN
from sistent.aspects.base import Aspect
from sistent.config import AspectSpec
from sistent.model import Finding, Snapshot
from sistent.options import BaseOptions
from sistent.registry import ENTRY_POINT_GROUP, Registry, RegistryError, default_registry, import_target
from sistent.repository import RepoContext


@dataclass(frozen=True, kw_only=True)
class DummyOptions(BaseOptions):
    depth: int = field(default=1, metadata={"help": "How deep to look."})


class DummyAspect(Aspect):
    type_name = "dummy"
    options_cls = DummyOptions
    description = "A test aspect that extracts nothing."

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        return {}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        return []


class OtherAspect(DummyAspect):
    type_name = "other"


class NotAnAspect:
    """Deliberately not an Aspect subclass."""


DUMMY_PATH = f"{__name__}:DummyAspect"


def fake_entry_points(*entries: tuple[str, str]) -> Callable[..., list[importlib.metadata.EntryPoint]]:
    def _entry_points(**params: Any) -> list[importlib.metadata.EntryPoint]:
        assert params == {"group": ENTRY_POINT_GROUP}
        return [importlib.metadata.EntryPoint(name, value, ENTRY_POINT_GROUP) for name, value in entries]

    return _entry_points


# ----- lookup ------------------------------------------------------------------------------------------------------


def test_get_by_name() -> None:
    registry = Registry({"dummy": DummyAspect})
    assert registry.get("dummy") is DummyAspect
    assert registry.has("dummy")
    assert "dummy" in registry
    assert registry.names() == ["dummy"]


def test_get_by_dotted_path_without_registration() -> None:
    registry = Registry()
    assert registry.get(DUMMY_PATH) is DummyAspect
    assert registry.get(f"{__name__}:OtherAspect") is OtherAspect
    assert not registry.has(DUMMY_PATH)


def test_unknown_name_suggests_close_match() -> None:
    registry = Registry({"markdown": DummyAspect, "badges": OtherAspect})
    with pytest.raises(RegistryError, match=r"unknown aspect type 'markdwn' \(did you mean 'markdown'\?\)"):
        registry.get("markdwn")
    with pytest.raises(RegistryError, match="Known types: badges, markdown"):
        registry.get("zzz")


def test_non_aspect_target_is_rejected() -> None:
    registry = Registry()
    with pytest.raises(RegistryError, match="is not an Aspect subclass"):
        registry.get(f"{__name__}:NotAnAspect")
    with pytest.raises(RegistryError, match="is not an Aspect subclass"):
        registry.get(f"{__name__}:DUMMY_PATH")
    with pytest.raises(RegistryError, match="is not an Aspect subclass"):
        Registry({"bad": NotAnAspect})  # type: ignore[dict-item]
    with pytest.raises(RegistryError, match="is not an Aspect subclass"):
        Registry({"base": Aspect})


def test_unimportable_dotted_paths() -> None:
    registry = Registry()
    with pytest.raises(RegistryError, match=re.escape("cannot import aspect type 'no.such.module:Thing'")):
        registry.get("no.such.module:Thing")
    with pytest.raises(RegistryError, match=f"module '{__name__}' has no attribute 'Missing'"):
        registry.get(f"{__name__}:Missing")
    with pytest.raises(RegistryError, match=re.escape("expected 'pkg.module:ClassName'")):
        import_target("just.a.module:")


# ----- registration ------------------------------------------------------------------------------------------------


def test_register_and_replace() -> None:
    registry = Registry()
    registry.register("dummy", DummyAspect)
    with pytest.raises(RegistryError, match="already registered"):
        registry.register("dummy", OtherAspect)
    assert registry.get("dummy") is DummyAspect
    registry.register("dummy", OtherAspect, replace=True)
    assert registry.get("dummy") is OtherAspect
    with pytest.raises(RegistryError, match="invalid aspect type name"):
        registry.register("a:b", DummyAspect)
    with pytest.raises(RegistryError, match=re.escape("expected 'pkg.module:ClassName'")):
        registry.register("nocolon", "pkg.module")


def test_string_targets_resolve_lazily() -> None:
    registry = Registry({"missing": "no.such.module:Thing", "dummy": DUMMY_PATH})
    # nothing is imported at registration time, so the broken entry is harmless until it is used
    assert registry.names() == ["dummy", "missing"]
    assert registry.has("missing")
    assert registry.target("missing") == "no.such.module:Thing"
    with pytest.raises(RegistryError, match="cannot import aspect type"):
        registry.get("missing")
    assert registry.get("dummy") is DummyAspect
    assert registry.target("dummy") == DUMMY_PATH  # the resolved class prints as the same dotted path


def test_build_instantiates_with_name_and_options() -> None:
    registry = Registry({"dummy": DummyAspect})
    options = DummyOptions(depth=3)
    spec = AspectSpec(
        name="layout",
        type="dummy",
        options=options,
        table={"type": "dummy", "depth": 3},
        inherited=frozenset(),
        is_default=False,
    )
    aspect = registry.build(spec)
    assert isinstance(aspect, DummyAspect)
    assert aspect.name == "layout"
    assert aspect.options is options
    spec_by_path = AspectSpec(
        name="x", type=DUMMY_PATH, options=options, table={"type": DUMMY_PATH}, inherited=frozenset(), is_default=False
    )
    assert isinstance(registry.build(spec_by_path), DummyAspect)


# ----- default registry and entry points ---------------------------------------------------------------------------


def test_default_registry_has_every_builtin_without_importing_them() -> None:
    registry = default_registry(entry_points=False)
    assert registry.names() == sorted(BUILTIN)
    for name, target in BUILTIN.items():
        assert registry.target(name) == target


def test_default_registry_ignores_entry_points_matching_builtins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points(*BUILTIN.items()))
    registry = default_registry()
    assert registry.names() == sorted(BUILTIN)


def test_installed_entry_points_do_not_collide_with_builtins() -> None:
    # sistent registers its own built-ins through the entry-point group; that must never be reported as a collision
    assert default_registry().names() == sorted(BUILTIN)


def test_entry_point_collision_with_builtin_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points(("markdown", "evil.plugin:Markdown")))
    with pytest.raises(RegistryError, match="aspect type 'markdown' is provided twice"):
        default_registry()


def test_entry_point_collision_between_plugins_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        fake_entry_points(("sphinx", "plugin_a.aspects:Sphinx"), ("sphinx", "plugin_b.aspects:Sphinx")),
    )
    with pytest.raises(RegistryError, match="'sphinx' is provided twice"):
        default_registry()


def test_duplicate_entry_point_with_same_target_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        importlib.metadata, "entry_points", fake_entry_points(("dummy", DUMMY_PATH), ("dummy", DUMMY_PATH))
    )
    assert default_registry().get("dummy") is DummyAspect


def test_plugin_entry_point_is_registered_lazily(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        importlib.metadata,
        "entry_points",
        fake_entry_points(("dummy", DUMMY_PATH), ("broken", "no.such.plugin:Aspect")),
    )
    registry = default_registry()
    assert registry.has("dummy")
    assert registry.has("broken")  # registered, not imported
    assert registry.get("dummy") is DummyAspect
    with pytest.raises(RegistryError, match=re.escape("cannot import aspect type 'no.such.plugin:Aspect'")):
        registry.get("broken")


def test_entry_points_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points(("dummy", DUMMY_PATH)))
    assert not default_registry(entry_points=False).has("dummy")
