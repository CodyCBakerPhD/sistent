"""Dependency direction between the packages of sistent, checked by importing each module in a fresh interpreter."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

# Each module may import (transitively) only sistent modules whose top-level package/module is in its allowance.
ALLOWED: dict[str, set[str]] = {
    "sistent.model": set(),
    "sistent.options": {"model"},
    "sistent.compare": {"model"},
    "sistent.parsers": {"model", "compare"},
    "sistent.repository": {"model", "compare"},
    "sistent.render": {"model"},
    "sistent.baseline": {"model"},
    "sistent.aspects.base": {"model", "options", "compare", "repository"},
    "sistent.registry": {"model", "options", "compare", "repository", "aspects", "config"},
}


def _loaded(module: str) -> set[str]:
    code = (
        "import json, sys\n"
        f"import {module}\n"
        "print(json.dumps(sorted(m for m in sys.modules if m.startswith('sistent'))))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60)
    return set(json.loads(out.stdout))


def _top(name: str) -> str:
    parts = name.split(".")
    if len(parts) == 1:
        return ""
    return parts[1]


@pytest.mark.parametrize("module", sorted(ALLOWED))
def test_import_graph_respects_layering(module: str) -> None:
    loaded = _loaded(module)
    own_top = _top(module)
    others = {_top(m) for m in loaded if m != "sistent" and _top(m) not in ("", own_top)}
    # aspects.base may pull in the aspects package __init__ (constants only), never the aspect implementations.
    if module == "sistent.aspects.base":
        assert not any(m.startswith("sistent.aspects.") and m != "sistent.aspects.base" for m in loaded)
    unexpected = others - ALLOWED[module]
    assert not unexpected, f"{module} transitively imports {sorted(unexpected)}"


def test_render_depends_only_on_model() -> None:
    loaded = _loaded("sistent.render.text") | _loaded("sistent.render.markdown") | _loaded("sistent.render.json_")
    assert {m for m in loaded if not m.startswith("sistent.render")} <= {"sistent", "sistent.model"}
