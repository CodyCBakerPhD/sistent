"""Tests for the ``badges`` aspect type (:mod:`sistent.aspects.badges`)."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from typing import Any

import pytest

from sistent.aspects import BUILTIN, DEFAULT_CONFIG
from sistent.aspects.badges import BadgesAspect, BadgesOptions, Classified, classify
from sistent.aspects.base import UnparseableFile
from sistent.model import Direction, Finding, Identity, Kind, Severity, Snapshot, StaleHit, Subject
from sistent.options import describe_options, parse_options
from tests.conftest import MakeRepo

LOCATOR_RE = re.compile(r"^[^#:]+([#:].+)?$")
WHERE = "README.md#badges"

NEUROCONV = Identity(name="neuroconv", aliases=("NeuroConv", "neuroconv"), org="catalystneuro", branch="main")
ROIEXTRACTORS = Identity(
    name="roiextractors", aliases=("ROIExtractors", "roiextractors"), org="catalystneuro", branch="main"
)
SPIKEINTERFACE = Identity(
    name="spikeinterface", aliases=("SpikeInterface", "spikeinterface"), org="SpikeInterface", branch="main"
)
PYNWB = Identity(name="pynwb", aliases=("PyNWB", "pynwb"), org="NeurodataWithoutBorders", branch="dev")
HDMF = Identity(name="hdmf", aliases=("HDMF", "hdmf"), org="hdmf-dev", branch="dev")


def aspect(**options: Any) -> BadgesAspect:
    return BadgesAspect("readme_badges", BadgesOptions(**options))


def shape(finding: Finding) -> tuple[Kind, Subject, Direction, str]:
    return finding.kind, finding.subject, finding.direction, finding.locator


def badge(
    kind: str, provider: str = "img.shields.io", params: tuple[str, ...] = (), **overrides: Any
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "kind": kind,
        "provider": provider,
        "params": list(params),
        "alt": kind,
        "img": f"https://{provider}/{kind}.svg",
        "href": None,
        "line": 1,
        "syntax": "md-linked",
    }
    record.update(overrides)
    return record


MakeSnapshot = Callable[..., Snapshot]


def snap(make_snapshot: MakeSnapshot, repo: str, badges: list[dict[str, Any]], *, present: bool = True) -> Snapshot:
    return make_snapshot("readme_badges", repo, {"present": present, "badges": badges})


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_defaults(self) -> None:
        options = BadgesOptions()
        assert (options.file, options.order, options.ignore_kinds) == ("README.md", True, [])
        assert options.enabled is True
        assert (options.tags, options.ignore, options.severity) == (None, [], {})

    def test_every_option_has_help(self) -> None:
        infos = {info.name: info for info in describe_options(BadgesOptions)}
        assert {"file", "order", "ignore_kinds"} <= set(infos)
        assert all(info.help for info in infos.values())
        assert infos["ignore_kinds"].type == "list[str]"

    def test_default_config_table_uses_these_option_names(self) -> None:
        table = tomllib.loads(DEFAULT_CONFIG)["aspects"]["readme_badges"]
        assert table.pop("type") == "badges"
        assert parse_options(BadgesOptions, table, where="aspects.readme_badges") == BadgesOptions(
            file="README.md", order=True
        )

    def test_registered_as_builtin(self) -> None:
        assert BUILTIN["badges"] == "sistent.aspects.badges:BadgesAspect"
        instance = aspect()
        assert instance.type_name == "badges"
        assert instance.options_cls is BadgesOptions
        assert instance.description


# ----- classification ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # PyPI
        ("https://img.shields.io/pypi/v/{{name}}.svg", ("pypi-version", "img.shields.io", ("pypi", "v"))),
        ("https://img.shields.io/pypi/l/{{name}}.svg", ("pypi-license", "img.shields.io", ("pypi", "l"))),
        (
            "https://img.shields.io/pypi/pyversions/{{name}}.svg",
            ("pypi-pyversions", "img.shields.io", ("pypi", "pyversions")),
        ),
        (
            "https://img.shields.io/pypi/dm/{{name}}?style=flat&logo=pypi",
            ("pypi-downloads", "img.shields.io", ("pypi", "dm")),
        ),
        ("https://badge.fury.io/py/{{name}}.svg", ("pypi-version", "badge.fury.io", ("py",))),
        ("https://pepy.tech/badge/{{name}}/month", ("pypi-downloads", "pepy.tech", ("badge", "month"))),
        (
            "https://static.pepy.tech/personalized-badge/{{name}}?period=total&units=international_system&left_color=grey&right_color=blue",
            ("pypi-downloads", "static.pepy.tech", ("personalized-badge", "total")),
        ),
        ("https://img.shields.io/pypi/l/pynwb.svg", ("pypi-license", "img.shields.io", ("pypi", "l", "pynwb"))),
        # conda
        (
            "https://img.shields.io/conda/vn/conda-forge/{{name}}.svg",
            ("conda-version", "img.shields.io", ("conda", "vn", "conda-forge")),
        ),
        (
            "https://anaconda.org/conda-forge/{{name}}/badges/version.svg",
            ("conda-version", "anaconda.org", ("conda-forge", "badges", "version")),
        ),
        # CI
        (
            "https://github.com/{{org}}/{{name}}/actions/workflows/run-tests.yml/badge.svg",
            ("ci:run-tests.yml", "github.com", ("actions", "workflows")),
        ),
        (
            "https://github.com/{{org}}/{{name}}/actions/workflows/ci.yml/badge.svg?branch={{branch}}&event=push",
            ("ci:ci.yml", "github.com", ("actions", "workflows")),
        ),
        (
            "https://github.com/{{org}}/{{name}}/workflows/Full%20Tests/badge.svg",
            ("ci:Full Tests", "github.com", ("workflows",)),
        ),
        (
            "https://img.shields.io/github/actions/workflow/status/{{org}}/{{name}}/ci.yml?branch={{branch}}",
            ("ci:ci.yml", "img.shields.io", ("github", "actions", "workflow", "status")),
        ),
        # coverage (both codecov URL styles, coveralls)
        ("https://codecov.io/github/{{org}}/{{name}}/coverage.svg?branch={{branch}}", ("coverage", "codecov.io", ())),
        ("https://codecov.io/gh/{{org}}/{{name}}/branch/main/graph/badge.svg", ("coverage", "codecov.io", ())),
        ("https://coveralls.io/repos/github/{{org}}/{{name}}/badge.svg?branch=main", ("coverage", "coveralls.io", ())),
        (
            "https://img.shields.io/codecov/c/github/{{org}}/{{name}}",
            ("coverage", "img.shields.io", ("codecov", "c", "github")),
        ),
        # docs (both hosts, any ?version=)
        ("https://readthedocs.org/projects/{{name}}/badge/?version=latest", ("docs", "readthedocs.org", ())),
        ("https://app.readthedocs.org/projects/{{name}}/badge/?version=stable", ("docs", "app.readthedocs.org", ())),
        ("https://img.shields.io/readthedocs/{{name}}", ("docs", "img.shields.io", ("readthedocs",))),
        # DOI
        ("https://zenodo.org/badge/221010083.svg", ("doi", "zenodo.org", ("badge",))),
        ("https://zenodo.org/badge/DOI/10.5281/zenodo.11039361.svg", ("doi", "zenodo.org", ("badge",))),
        (
            "https://img.shields.io/badge/doi-10.25080%2Fcehj4257-informational?style=flat&labelColor=555",
            ("doi", "img.shields.io", ("badge",)),
        ),
        # license
        (
            "https://img.shields.io/github/license/{{org}}/{{name}}",
            ("license", "img.shields.io", ("github", "license")),
        ),
        ("https://img.shields.io/badge/License-MIT-yellow.svg", ("license", "img.shields.io", ("badge", "mit"))),
        # code style
        (
            "https://img.shields.io/badge/code%20style-black-000000.svg",
            ("code-style:black", "img.shields.io", ("badge",)),
        ),
        ("https://img.shields.io/badge/code_style-ruff-D7FF64", ("code-style:ruff", "img.shields.io", ("badge",))),
        (
            "https://img.shields.io/static/v1?label=code%20style&message=black&color=000000",
            ("code-style:black", "img.shields.io", ("badge",)),
        ),
        (
            "https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json",
            ("code-style:ruff", "img.shields.io", ("endpoint",)),
        ),
        # pre-commit, binder
        (
            "https://results.pre-commit.ci/badge/github/{{org}}/{{name}}/main.svg",
            ("pre-commit", "results.pre-commit.ci", ()),
        ),
        (
            "https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit",
            ("pre-commit", "img.shields.io", ("badge",)),
        ),
        ("https://mybinder.org/badge_logo.svg", ("binder", "mybinder.org", ())),
        # social
        (
            "https://img.shields.io/badge/@{{name}}-%231DA1F2.svg?style=for-the-badge&logo=Twitter&logoColor=white",
            ("social:twitter", "img.shields.io", ("badge",)),
        ),
        (
            "https://img.shields.io/badge/-@{{name}}-%232B90D9?style=for-the-badge&logo=mastodon&logoColor=white",
            ("social:mastodon", "img.shields.io", ("badge",)),
        ),
        ("https://img.shields.io/badge/chat-on%20discord-7289da.svg", ("social:discord", "img.shields.io", ("badge",))),
        ("https://img.shields.io/discord/1234567890?label=chat", ("social:discord", "img.shields.io", ("discord",))),
        (
            "https://img.shields.io/twitter/follow/{{name}}?style=social",
            ("social:twitter", "img.shields.io", ("twitter", "follow")),
        ),
        (
            "https://img.shields.io/github/stars/{{org}}/{{name}}?style=social",
            ("social:github-stars", "img.shields.io", ("github", "stars")),
        ),
        # static
        (
            "https://img.shields.io/badge/python-3.10%20|%203.11-blue",
            ("static:python", "img.shields.io", ("badge", "3.10 | 3.11")),
        ),
        (
            "https://img.shields.io/badge/Made%20with-Python-1f425f.svg",
            ("static:made with", "img.shields.io", ("badge", "python")),
        ),
        ("https://img.shields.io/badge/stable-brightgreen", ("static:stable", "img.shields.io", ("badge",))),
        # unknown
        (
            "https://bestpractices.coreinfrastructure.org/projects/1234/badge",
            ("other:bestpractices.coreinfrastructure.org/projects/badge", "bestpractices.coreinfrastructure.org", ()),
        ),
        ("https://example.com/static/badge/ci.svg", ("other:example.com/static/badge/ci", "example.com", ())),
        ("https://img.shields.io/pypi/status/{{name}}", ("other:img.shields.io/pypi/status", "img.shields.io", ())),
        ("docs/badges/ci.svg", ("other:/docs/badges/ci", "", ())),
        ("http://[invalid", ("other:http://[invalid", "", ())),
    ],
)
def test_classify(url: str, expected: tuple[str, str, tuple[str, ...]]) -> None:
    result = classify(url)
    assert isinstance(result, Classified)
    assert (result.kind, result.provider, result.params) == expected


def test_classification_is_case_insensitive_for_hosts_and_endpoints() -> None:
    assert classify("HTTPS://IMG.SHIELDS.IO/PyPI/V/{{name}}.SVG").kind == "pypi-version"
    assert classify("https://Codecov.io/gh/{{org}}/{{name}}/graph/badge.svg").provider == "codecov.io"


def test_substitution_makes_two_packages_badges_identical(make_repo: MakeRepo) -> None:
    neuroconv = make_repo({}, name="neuroconv", aliases=("NeuroConv",), vars={"org": "catalystneuro"})
    roi = make_repo({}, name="roiextractors", aliases=("ROIExtractors",), vars={"org": "catalystneuro"})
    pairs = [
        ("https://badge.fury.io/py/neuroconv.svg", "https://badge.fury.io/py/roiextractors.svg"),
        ("https://img.shields.io/pypi/l/NeuroConv.svg", "https://img.shields.io/pypi/l/ROIExtractors.svg"),
        (
            "https://github.com/catalystneuro/neuroconv/actions/workflows/auto-publish.yml/badge.svg",
            "https://github.com/catalystneuro/roiextractors/actions/workflows/auto-publish.yml/badge.svg",
        ),
        (
            "https://codecov.io/github/catalystneuro/neuroconv/coverage.svg?branch=main",
            "https://codecov.io/github/catalystneuro/roiextractors/coverage.svg?branch=master",
        ),
    ]
    for left, right in pairs:
        assert classify(neuroconv.substitute(left)) == classify(roi.substitute(right))
    assert classify(neuroconv.substitute(pairs[0][0])) == Classified("pypi-version", "badge.fury.io", ("py",))


# ----- extraction --------------------------------------------------------------------------------------------------


WIDGET_README = """\
[![PyPI][pypi-badge]][pypi-link]
<a href="https://codecov.io/gh/acme/widget"><img src="https://codecov.io/gh/acme/widget/branch/main/graph/badge.svg" alt="widget coverage"></a>
![Widget logo](docs/widget_logo.png)

# Widget

Text mentioning widget.

[pypi-badge]: https://img.shields.io/pypi/v/widget.svg
[pypi-link]: https://pypi.org/project/widget/
"""

MIXED_README = """\
[![PyPI](https://img.shields.io/pypi/v/widget.svg)](https://pypi.org/project/widget/)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://python.org)
[![Twitter](https://img.shields.io/twitter/follow/widget?style=social)](https://twitter.com/widget)
"""


class TestExtract:
    def test_reference_style_and_html_badges(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": WIDGET_README}, name="widget", vars={"org": "acme", "branch": "main"})
        snapshot = aspect().run_extract(ctx)
        assert snapshot.data == {
            "present": True,
            "badges": [
                {
                    "kind": "pypi-version",
                    "provider": "img.shields.io",
                    "params": ["pypi", "v"],
                    "alt": "PyPI",
                    "img": "https://img.shields.io/pypi/v/{{name}}.svg",
                    "href": "https://pypi.org/project/{{name}}/",
                    "line": 1,
                    "syntax": "md-ref",
                },
                {
                    "kind": "coverage",
                    "provider": "codecov.io",
                    "params": [],
                    "alt": "{{name}} coverage",
                    "img": "https://codecov.io/gh/{{org}}/{{name}}/branch/main/graph/badge.svg",
                    "href": "https://codecov.io/gh/{{org}}/{{name}}",
                    "line": 2,
                    "syntax": "html",
                },
            ],
        }
        assert snapshot.sources == ("README.md",)
        assert snapshot.ignored == ()
        assert "widget" in snapshot.aliases_applied

    def test_absent_file(self, make_repo: MakeRepo) -> None:
        snapshot = aspect().run_extract(make_repo({"docs/index.md": "x"}))
        assert snapshot.data == {"present": False, "badges": []}
        assert snapshot.sources == ()

    def test_file_option_and_rst(self, make_repo: MakeRepo) -> None:
        rst = ".. image:: https://badge.fury.io/py/widget.svg\n     :target: https://badge.fury.io/py/widget\n     :alt: PyPI\n"
        ctx = make_repo({"README.rst": rst}, name="widget")
        snapshot = aspect(file="README.rst").run_extract(ctx)
        assert snapshot.sources == ("README.rst",)
        assert snapshot.data["present"] is True
        assert [(b["kind"], b["syntax"], b["line"], b["alt"]) for b in snapshot.data["badges"]] == [
            ("pypi-version", "rst", 1, "PyPI")
        ]

    def test_ignore_kinds_globs_are_case_insensitive_and_recorded(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": MIXED_README}, name="widget")
        snapshot = aspect(ignore_kinds=["static:*", "SOCIAL:*"]).run_extract(ctx)
        assert [b["kind"] for b in snapshot.data["badges"]] == ["pypi-version"]
        assert snapshot.ignored == (
            "README.md#badges: badge static:python (matched ignore_kinds 'static:*')",
            "README.md#badges: badge social:twitter (matched ignore_kinds 'SOCIAL:*')",
        )
        everything = aspect().run_extract(make_repo({"README.md": MIXED_README}, name="widget"))
        assert [b["kind"] for b in everything.data["badges"]] == ["pypi-version", "static:python", "social:twitter"]

    def test_badges_anywhere_in_the_document_and_code_blocks_skipped(self, make_repo: MakeRepo) -> None:
        text = (
            "# Title\n\n## Status\n\n| a | b |\n|---|---|\n| ci | ![CI](https://github.com/o/r/actions/workflows/ci.yml/badge.svg) |\n\n"
            "```md\n![Docs](https://readthedocs.org/projects/r/badge/)\n```\n"
        )
        snapshot = aspect().run_extract(make_repo({"README.md": text}, name="r", substitute=False))
        assert [(b["kind"], b["line"]) for b in snapshot.data["badges"]] == [("ci:ci.yml", 7)]

    def test_unparseable_input(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": "x\x00y"})
        with pytest.raises(UnparseableFile, match=r"^README\.md: binary content") as info:
            aspect().run_extract(ctx)
        assert info.value.rel == "README.md"


# ----- comparison --------------------------------------------------------------------------------------------------


class TestCompare:
    def test_missing_and_extra(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot, "main", [badge("pypi-version", params=("pypi", "v")), badge("docs", "readthedocs.org")]
        )
        other = snap(make_snapshot, "sat", [badge("docs", "readthedocs.org"), badge("coverage", "codecov.io")])
        found = aspect().compare(main, other)
        assert [shape(f) for f in found] == [
            (Kind.MISSING, Subject.BADGE, Direction.DOWNSTREAM, WHERE),
            (Kind.EXTRA, Subject.BADGE, Direction.UPSTREAM, WHERE),
        ]
        missing, extra = found
        assert (missing.content_key, missing.severity) == ("pypi-version", Severity.WARNING)
        assert (extra.content_key, extra.severity) == ("coverage", Severity.INFO)
        assert all(f.aspect == "readme_badges" and f.repo == "sat" and LOCATOR_RE.match(f.locator) for f in found)

    def test_identical_is_clean(self, make_snapshot: MakeSnapshot) -> None:
        badges = [
            badge("pypi-version", params=("pypi", "v")),
            badge("coverage", "codecov.io"),
            badge("ci:ci.yml", "github.com", ("actions", "workflows")),
        ]
        assert aspect().compare(snap(make_snapshot, "main", badges), snap(make_snapshot, "sat", list(badges))) == []

    def test_provider_differs(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("coverage", "codecov.io")])
        other = snap(make_snapshot, "sat", [badge("coverage", "coveralls.io")])
        (found,) = aspect().compare(main, other)
        assert shape(found) == (Kind.DIFFERS, Subject.BADGE, Direction.DOWNSTREAM, WHERE)
        assert found.severity is Severity.INFO
        assert found.content_key == "coverage"
        assert found.detail_kind == "text"
        assert found.detail is not None

    def test_params_differ(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("pypi-license", params=("pypi", "l"))])
        other = snap(make_snapshot, "sat", [badge("pypi-license", params=("pypi", "l", "pynwb"))])
        assert [shape(f) for f in aspect().compare(main, other)] == [
            (Kind.DIFFERS, Subject.BADGE, Direction.DOWNSTREAM, WHERE)
        ]

    def test_cosmetic_fields_do_not_matter(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(
            make_snapshot,
            "main",
            [badge("docs", "readthedocs.org", alt="docs", line=3, syntax="md-linked", href="https://a")],
        )
        other = snap(
            make_snapshot,
            "sat",
            [badge("docs", "readthedocs.org", alt="Documentation", line=9, syntax="html", href=None, img="x")],
        )
        assert aspect().compare(main, other) == []

    def test_reordered(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a"), badge("b"), badge("c")])
        other = snap(make_snapshot, "sat", [badge("c"), badge("a"), badge("b")])
        (found,) = aspect().compare(main, other)
        assert shape(found) == (Kind.REORDERED, Subject.BADGE, Direction.DOWNSTREAM, WHERE)
        assert found.severity is Severity.INFO
        assert found.detail_kind == "list"
        assert found.detail == "main: a, b, c\nrepo: c, a, b"
        assert found.option == "aspects.readme_badges.order=True"

    def test_order_can_be_switched_off(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a"), badge("b")])
        other = snap(make_snapshot, "sat", [badge("b"), badge("a")])
        assert aspect(order=False).compare(main, other) == []

    def test_additions_and_removals_are_not_reorders(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a"), badge("b"), badge("c")])
        other = snap(make_snapshot, "sat", [badge("a"), badge("x"), badge("c"), badge("y")])
        kinds = {f.kind for f in aspect().compare(main, other)}
        assert kinds == {Kind.MISSING, Kind.EXTRA}

    def test_order_only_over_shared_kinds(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a"), badge("b"), badge("c")])
        other = snap(make_snapshot, "sat", [badge("z"), badge("b"), badge("a")])
        assert {shape(f) for f in aspect().compare(main, other)} == {
            (Kind.MISSING, Subject.BADGE, Direction.DOWNSTREAM, WHERE),
            (Kind.EXTRA, Subject.BADGE, Direction.UPSTREAM, WHERE),
            (Kind.REORDERED, Subject.BADGE, Direction.DOWNSTREAM, WHERE),
        }

    def test_duplicate_kinds_compare_first_occurrence_only(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("doi", "zenodo.org"), badge("doi", "img.shields.io"), badge("docs")])
        other = snap(make_snapshot, "sat", [badge("doi", "zenodo.org"), badge("docs"), badge("doi", "other.example")])
        assert aspect().compare(main, other) == []
        flipped = snap(make_snapshot, "sat", [badge("doi", "img.shields.io"), badge("docs")])
        assert [f.kind for f in aspect().compare(main, flipped)] == [Kind.DIFFERS]

    @pytest.mark.parametrize(
        ("main_present", "other_present", "expected"),
        [
            (True, False, [(Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, "README.md")]),
            (False, True, [(Kind.EXTRA, Subject.FILE, Direction.UPSTREAM, "README.md")]),
            (False, False, []),
        ],
    )
    def test_absent_files(
        self,
        make_snapshot: MakeSnapshot,
        main_present: bool,
        other_present: bool,
        expected: list[tuple[Kind, Subject, Direction, str]],
    ) -> None:
        main = snap(make_snapshot, "main", [badge("a")] if main_present else [], present=main_present)
        other = snap(make_snapshot, "sat", [badge("a")] if other_present else [], present=other_present)
        found = aspect().compare(main, other)
        assert [shape(f) for f in found] == expected
        assert all(f.content_key == "README.md" for f in found)

    def test_missing_file_is_a_warning_by_default_and_overridable(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a")])
        other = snap(make_snapshot, "sat", [], present=False)
        (found,) = aspect().compare(main, other)
        assert found.severity is Severity.WARNING
        (strict,) = aspect(severity={"missing.file": "error"}).compare(main, other)
        assert strict.severity is Severity.ERROR

    def test_present_but_empty(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a"), badge("b")])
        other = snap(make_snapshot, "sat", [])
        assert [shape(f) for f in aspect().compare(main, other)] == [
            (Kind.MISSING, Subject.BADGE, Direction.DOWNSTREAM, WHERE)
        ] * 2

    def test_custom_file_in_locators(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a")])
        other = snap(make_snapshot, "sat", [badge("b")])
        assert {f.locator for f in aspect(file="docs/index.md").compare(main, other)} == {"docs/index.md#badges"}
        absent = snap(make_snapshot, "sat", [], present=False)
        assert [f.locator for f in aspect(file="docs/index.md").compare(main, absent)] == ["docs/index.md"]

    def test_user_severity_table_applies(self, make_snapshot: MakeSnapshot) -> None:
        main = snap(make_snapshot, "main", [badge("a")])
        other = snap(make_snapshot, "sat", [badge("b")])
        found = aspect(severity={"missing.badge": "error", "extra": "error"}).compare(main, other)
        assert [(f.kind, f.severity) for f in found] == [(Kind.MISSING, Severity.ERROR), (Kind.EXTRA, Severity.INFO)]


# ----- real fixtures -----------------------------------------------------------------------------------------------


class TestRealFixtures:
    def test_neuroconv_vs_roiextractors(self, make_repo: MakeRepo, real_fixture: Callable[[str], str]) -> None:
        main = make_repo(
            {"README.md": real_fixture("neuroconv__README.md")}, name="neuroconv", identity=NEUROCONV, is_main=True
        )
        sat = make_repo(
            {"README.md": real_fixture("roiextractors__README.md")}, name="roiextractors", identity=ROIEXTRACTORS
        )
        instance = aspect()
        main_snap = instance.run_extract(main)
        sat_snap = instance.run_extract(sat)
        assert [b["kind"] for b in main_snap.data["badges"]] == [
            "pypi-version",
            "ci:dailies.yml",
            "ci:auto-publish.yml",
            "coverage",
            "docs",
            "pypi-pyversions",
            "code-style:black",
            "pypi-license",
            "doi",
        ]
        assert [b["kind"] for b in sat_snap.data["badges"]] == [
            "pypi-version",
            "ci:run-tests.yml",
            "ci:auto-publish.yml",
            "coverage",
            "docs",
            "pypi-license",
            "doi",
        ]
        # every badge that names the package has it substituted; the static ones (code style, DOI) never name it
        assert {b["kind"] for b in main_snap.data["badges"] if "{{name}}" not in b["img"]} == {
            "code-style:black",
            "doi",
        }

        found = instance.compare(main_snap, sat_snap)
        by_kind: dict[Kind, set[str]] = {}
        for f in found:
            by_kind.setdefault(f.kind, set()).add(f.content_key)
        assert by_kind == {
            Kind.MISSING: {"ci:dailies.yml", "pypi-pyversions", "code-style:black"},
            Kind.EXTRA: {"ci:run-tests.yml"},
            # doi: shields static vs zenodo (providers truly differ); pypi-license: the stale pynwb package name
            Kind.DIFFERS: {"doi", "pypi-license"},
        }
        assert all(f.locator == WHERE for f in found)
        assert len(found) <= 6

    def test_stale_foreign_identity_in_badge_url(self, make_repo: MakeRepo, real_fixture: Callable[[str], str]) -> None:
        sat = make_repo(
            {"README.md": real_fixture("roiextractors__README.md")},
            name="roiextractors",
            identity=ROIEXTRACTORS,
            foreign=[PYNWB, NEUROCONV],
        )
        snapshot = aspect().run_extract(sat)
        assert snapshot.stale_hits == (
            StaleHit(where=WHERE, alias="pynwb", other_repo="pynwb", excerpt="https://img.shields.io/pypi/l/pynwb.svg"),
        )
        license_badge = next(b for b in snapshot.data["badges"] if b["kind"] == "pypi-license")
        assert license_badge["img"] == "https://img.shields.io/pypi/l/pynwb.svg", "foreign names are left in place"
        assert license_badge["params"] == ["pypi", "l", "pynwb"]

    def test_no_stale_hits_without_foreign_mentions(
        self, make_repo: MakeRepo, real_fixture: Callable[[str], str]
    ) -> None:
        main = make_repo(
            {"README.md": real_fixture("neuroconv__README.md")},
            name="neuroconv",
            identity=NEUROCONV,
            foreign=[ROIEXTRACTORS, PYNWB],
        )
        assert aspect().run_extract(main).stale_hits == ()

    def test_spikeinterface_html_table(self, make_repo: MakeRepo, real_fixture: Callable[[str], str]) -> None:
        ctx = make_repo(
            {"README.md": real_fixture("spikeinterface__README.md")}, name="spikeinterface", identity=SPIKEINTERFACE
        )
        snapshot = aspect().run_extract(ctx)
        assert [(b["kind"], b["provider"], b["syntax"]) for b in snapshot.data["badges"]] == [
            ("pypi-version", "img.shields.io", "html"),
            ("docs", "readthedocs.org", "html"),
            ("pypi-license", "img.shields.io", "html"),
            ("ci:full-test-with-codecov.yml", "github.com", "html"),
            ("coverage", "codecov.io", "html"),
            ("social:twitter", "img.shields.io", "md-linked"),
            ("social:mastodon", "img.shields.io", "md-linked"),
        ]
        assert snapshot.data["badges"][0]["href"] == "https://pypi.org/project/{{name}}/"

    def test_pynwb_vs_hdmf_rst(self, make_repo: MakeRepo, real_fixture: Callable[[str], str]) -> None:
        instance = aspect(file="README.rst")
        main = make_repo({"README.rst": real_fixture("pynwb__README.rst")}, name="pynwb", identity=PYNWB, is_main=True)
        sat = make_repo({"README.rst": real_fixture("hdmf__README.rst")}, name="hdmf", identity=HDMF)
        main_snap = instance.run_extract(main)
        sat_snap = instance.run_extract(sat)
        assert [b["kind"] for b in main_snap.data["badges"]][:3] == ["pypi-version", "conda-version", "doi"]
        assert all(b["syntax"] == "rst" for b in main_snap.data["badges"])
        found = instance.compare(main_snap, sat_snap)
        assert {(f.kind, f.content_key) for f in found} == {
            (Kind.MISSING, "ci:run_inspector_tests.yml"),
            (Kind.MISSING, "pypi-license"),
            (Kind.EXTRA, "ci:run_pynwb_tests.yml"),
            (Kind.EXTRA, "ci:run_{{name}}_zarr_tests.yml"),
            (Kind.EXTRA, "ci:run_nwb_extension_tests.yml"),
            (Kind.DIFFERS, "docs"),  # app.readthedocs.org vs readthedocs.org
        }
        assert all(f.locator == "README.rst#badges" for f in found)
