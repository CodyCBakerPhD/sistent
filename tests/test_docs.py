"""The developer catalogue of inconsistency cases stays in step with the code."""

from __future__ import annotations

from pathlib import Path

import pytest

from sistent.aspects import BUILTIN, DEFAULT_ASPECT_NAMES
from sistent.model import Kind, Subject

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "docs" / "inconsistencies.md"


@pytest.fixture(scope="module")
def catalogue() -> str:
    return CATALOGUE.read_text(encoding="utf-8")


def test_every_default_aspect_and_type_is_documented(catalogue: str) -> None:
    for name in DEFAULT_ASPECT_NAMES:
        assert f"`{name}`" in catalogue, name
    for type_name in BUILTIN:
        assert f"type `{type_name}`" in catalogue, type_name


def test_every_finding_kind_is_documented(catalogue: str) -> None:
    for kind in Kind:
        assert f"`{kind.value}`" in catalogue, kind


def test_documented_subjects_exist(catalogue: str) -> None:
    import re

    kinds = {k.value for k in Kind}
    used = set(re.findall(r"`(?:missing|extra|differs|moved|reordered|stale|unparseable)`/`([a-z]+)`", catalogue))
    used -= kinds
    assert used <= {s.value for s in Subject}, used - {s.value for s in Subject}


def test_readme_links_the_catalogue() -> None:
    assert "docs/inconsistencies.md" in (ROOT / "README.md").read_text(encoding="utf-8")
