"""Tests for :mod:`sistent.parsers.badges`."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sistent.parsers.badges import BADGE_HOSTS, Badge, is_badge, parse_badges

SHIELD = "https://img.shields.io/pypi/v/x.svg"


def _summary(badges: list[Badge]) -> list[tuple[str, str, str | None, str, bool]]:
    return [(b.alt, b.img, b.href, b.syntax, b.badge) for b in badges]


# ----- is_badge ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SHIELD, True),
        ("https://shields.io/x.svg", True),
        ("https://badge.fury.io/py/x.svg", True),
        ("https://github.com/o/r/actions/workflows/ci.yml/badge.svg", True),
        ("https://github.com/o/r/workflows/CI/badge.svg", True),
        ("https://github.com/o/r/actions/workflows/ci.yml", True),
        ("https://github.com/o/r/blob/main/docs/logo.png", False),
        ("https://raw.githubusercontent.com/o/r/main/docs/logo.png", False),
        ("https://example.com/static/badge/ci.svg", True),  # "badge" in the path of an unknown host
        ("https://example.com/BADGES/ci.png", True),
        ("https://example.com/logo.png?badge=1", False),  # the query string does not count
        ("docs/source/figures/logo.png", False),
        ("https://readthedocs.org/projects/x/badge/?version=latest", True),
        ("https://app.readthedocs.org/projects/x/badge/?version=latest", True),
        ("https://x.readthedocs.io/en/latest/status.svg", True),  # subdomain of a listed host
        ("https://codecov.io/gh/o/r/branch/main/graph/badge.svg", True),
        ("https://codecov.io/github/o/r/coverage.svg?branch=main", True),
        ("https://coveralls.io/repos/github/o/r/badge.svg", True),
        ("https://zenodo.org/badge/1.svg", True),
        ("https://anaconda.org/conda-forge/x/badges/version.svg", True),
        ("https://results.pre-commit.ci/badge/github/o/r/main.svg", True),
        ("https://static.pepy.tech/personalized-badge/x", True),
        ("https://dev.azure.com/o/p/_apis/build/status/x?branchName=main", False),
        ("HTTPS://IMG.SHIELDS.IO/x.svg", True),
        ("", False),
        ("http://[invalid", False),
    ],
)
def test_is_badge(url: str, expected: bool) -> None:
    assert is_badge(url) is expected


def test_badge_hosts_cover_the_spec_list() -> None:
    expected = {
        "img.shields.io",
        "shields.io",
        "badge.fury.io",
        "codecov.io",
        "coveralls.io",
        "readthedocs.org",
        "app.readthedocs.org",
        "readthedocs.io",
        "zenodo.org",
        "results.pre-commit.ci",
        "mybinder.org",
        "pepy.tech",
        "static.pepy.tech",
        "anaconda.org",
        "img.badgesize.io",
        "badgen.net",
        "snyk.io",
        "bestpractices.coreinfrastructure.org",
        "api.codacy.com",
        "github.com",
    }
    assert frozenset(expected) == BADGE_HOSTS


# ----- syntaxes ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"[![Alt]({SHIELD})](https://a.example)", [("Alt", SHIELD, "https://a.example", "md-linked", True)]),
        ("![Logo](docs/logo.png)", [("Logo", "docs/logo.png", None, "md-image", False)]),
        (f'![t]({SHIELD} "Title")', [("t", SHIELD, None, "md-image", True)]),
        (f"![t](<{SHIELD}>)", [("t", SHIELD, None, "md-image", True)]),
        (f"[![Alt]({SHIELD})]()", [("Alt", SHIELD, None, "md-linked", True)]),
        (f'<img src="{SHIELD}" alt="A">', [("A", SHIELD, None, "html", True)]),
        (f"<IMG ALT='A' SRC='{SHIELD}' />", [("A", SHIELD, None, "html", True)]),
        (f"<img src={SHIELD} width=100>", [("", SHIELD, None, "html", True)]),
        (
            f"<a href='https://a.example'><img alt='A' src='{SHIELD}' /></a>",
            [("A", SHIELD, "https://a.example", "html", True)],
        ),
        (
            f'<a target="_blank" href="https://a.example">\n  <img\n    src="{SHIELD}"\n    alt="A" />\n</a>',
            [("A", SHIELD, "https://a.example", "html", True)],
        ),
        ('<a href="https://x">text</a> <img src="i.png">', [("", "i.png", None, "html", False)]),
        (
            ".. image:: https://badge.fury.io/py/x.svg\n     :target: https://badge.fury.io/py/x\n     :alt: PyPI",
            [("PyPI", "https://badge.fury.io/py/x.svg", "https://badge.fury.io/py/x", "rst", True)],
        ),
        (".. image:: docs/logo.png\n    :width: 200px\n", [("", "docs/logo.png", None, "rst", False)]),
        (
            f".. |PyPI| image:: {SHIELD}\n   :target: https://pypi.org/project/x/",
            [("PyPI", SHIELD, "https://pypi.org/project/x/", "rst", True)],
        ),
        (
            ".. image:: https://x/badge.svg\n\n    :target: not-an-option-after-a-blank",
            [("", "https://x/badge.svg", None, "rst", True)],
        ),
        (
            "no images here, just [a link](https://example.com) and `![code](x)`",
            [("code", "x", None, "md-image", False)],
        ),
        ("", []),
        ("![]()", [("", "", None, "md-image", False)]),
    ],
)
def test_syntaxes(text: str, expected: list[tuple[str, str, str | None, str, bool]]) -> None:
    assert _summary(parse_badges(text)) == expected


def test_several_badges_on_one_line_are_all_found_in_order() -> None:
    text = (
        f"[![A]({SHIELD})](https://a) [![B](https://img.shields.io/b.svg)](https://b) "
        '![C](c.png) <img src="d.png"> <a href="https://e"><img src="e.png"></a>'
    )
    badges = parse_badges(text)
    assert [b.img for b in badges] == [SHIELD, "https://img.shields.io/b.svg", "c.png", "d.png", "e.png"]
    assert [b.syntax for b in badges] == ["md-linked", "md-linked", "md-image", "html", "html"]
    assert [b.href for b in badges] == ["https://a", "https://b", None, None, "https://e"]
    assert all(b.line == 1 for b in badges)


def test_line_numbers_are_one_based_and_point_at_the_image() -> None:
    text = "intro\n\n![a](a.png)\ntext ![b](b.png)\n\n<a href='x'>\n<img src='c.png'>\n</a>\n.. image:: d.png\n"
    assert [(b.img, b.line) for b in parse_badges(text)] == [("a.png", 3), ("b.png", 4), ("c.png", 7), ("d.png", 9)]


def test_whitespace_inside_urls_is_removed() -> None:
    text = "[![a](https://img.shields.io/\n  pypi/v/x.svg)](https://pypi.org/\n  project/x)"
    [badge] = parse_badges(text)
    assert (badge.img, badge.href) == ("https://img.shields.io/pypi/v/x.svg", "https://pypi.org/project/x")


# ----- references --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "links", "expected"),
    [
        (
            "[![ci][ci-img]][ci-link]",
            {"ci-img": SHIELD, "ci-link": "https://ci"},
            (SHIELD, "https://ci", "md-ref", True),
        ),
        ("![ci][ci-img]", {"CI-IMG": SHIELD}, (SHIELD, None, "md-ref", True)),  # labels are case-insensitive
        ("![ci][  Ci  Img ]", {"ci img": SHIELD}, (SHIELD, None, "md-ref", True)),  # and whitespace-collapsed
        ("![ci][]", {"ci": SHIELD}, (SHIELD, None, "md-ref", True)),  # collapsed reference uses the alt text
        ("[![ci][]][]", {"ci": SHIELD}, (SHIELD, SHIELD, "md-ref", True)),
        ("![ci][missing]", {}, ("missing", None, "md-ref", False)),  # unresolved: the label stays
        ("![ci][missing]", None, ("missing", None, "md-ref", False)),
        (f"[![ci]({SHIELD})][ci-link]", {"ci-link": "https://ci"}, (SHIELD, "https://ci", "md-ref", True)),
        (f"[![ci][ci-img]]({SHIELD})", {"ci-img": SHIELD}, (SHIELD, SHIELD, "md-ref", True)),
    ],
)
def test_reference_resolution(
    text: str, links: dict[str, str] | None, expected: tuple[str, str | None, str, bool]
) -> None:
    [badge] = parse_badges(text, links=links)
    assert (badge.img, badge.href, badge.syntax, badge.badge) == expected
    assert badge.alt == "ci"


# ----- exclusions --------------------------------------------------------------------------------------------------


def test_images_inside_fenced_code_and_html_comments_are_ignored() -> None:
    text = (
        "![a](a.png)\n```md\n![b](b.png)\n<img src='c.png'>\n```\n~~~\n![d](d.png)\n~~~\n"
        "<!-- ![e](e.png) -->\n<!--\n<img src='f.png'>\n-->\n![g](g.png)\n````\n```\n![h](h.png)\n```\n````\n![i](i.png)"
    )
    assert [(b.img, b.line) for b in parse_badges(text)] == [("a.png", 1), ("g.png", 13), ("i.png", 19)]


def test_unclosed_fence_hides_everything_after_it() -> None:
    assert [b.img for b in parse_badges("![a](a.png)\n```\n![b](b.png)\n")] == ["a.png"]


def test_unclosed_comment_hides_everything_after_it() -> None:
    assert [b.img for b in parse_badges("![a](a.png)\n<!-- unclosed\n![b](b.png)\n<img src='c.png'>")] == ["a.png"]


def test_multiline_alt_text_is_collapsed_to_single_spaces() -> None:
    [badge] = parse_badges('<img src="https://img.shields.io/\n  a.svg" alt="NeuroConv\n   logo">')
    assert (badge.img, badge.alt) == ("https://img.shields.io/a.svg", "NeuroConv logo")


# ----- real READMEs ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "total", "flagged", "syntaxes"),
    [
        ("neuroconv__README.md", 10, 9, {"md-linked": 7, "md-image": 2, "html": 1}),
        ("roiextractors__README.md", 7, 7, {"md-linked": 5, "md-image": 2}),
        ("spikeinterface__README.md", 7, 7, {"html": 5, "md-linked": 2}),
        ("pynwb__README.rst", 16, 14, {"rst": 16}),
        ("hdmf__README.rst", 15, 15, {"rst": 15}),
    ],
)
def test_real_readme_badge_counts(
    real_fixture: Callable[[str], str], name: str, total: int, flagged: int, syntaxes: dict[str, int]
) -> None:
    badges = parse_badges(real_fixture(name))
    assert len(badges) == total
    assert sum(b.badge for b in badges) == flagged
    counts: dict[str, int] = {}
    for badge in badges:
        counts[badge.syntax] = counts.get(badge.syntax, 0) + 1
    assert counts == syntaxes
    assert [b.line for b in badges] == sorted(b.line for b in badges)


def test_real_neuroconv_readme_details(real_fixture: Callable[[str], str]) -> None:
    badges = parse_badges(real_fixture("neuroconv__README.md"))
    assert badges[0] == Badge(
        alt="PyPI version",
        img="https://badge.fury.io/py/neuroconv.svg",
        href="https://badge.fury.io/py/neuroconv.svg",
        line=1,
        syntax="md-linked",
        badge=True,
    )
    assert (badges[1].syntax, badges[1].href, badges[1].badge) == ("md-image", None, True)
    assert badges[1].img.endswith("/actions/workflows/dailies.yml/badge.svg")
    logo = badges[-1]
    assert (logo.syntax, logo.line, logo.alt, logo.badge, logo.href) == ("html", 12, "NeuroConv logo", False, None)
    assert logo.img.endswith("/docs/img/neuroconv_logo.png")


def test_real_roiextractors_keeps_stale_pynwb_badge_verbatim(real_fixture: Callable[[str], str]) -> None:
    badges = parse_badges(real_fixture("roiextractors__README.md"))
    licence = badges[5]
    assert licence.img == "https://img.shields.io/pypi/l/pynwb.svg"  # copy-paste leftover, reported by the aspect
    assert badges[6].img == "https://zenodo.org/badge/221010083.svg"
    assert badges[6].href == "https://doi.org/10.5281/zenodo.11039361"


def test_real_spikeinterface_html_table_badges(real_fixture: Callable[[str], str]) -> None:
    badges = parse_badges(real_fixture("spikeinterface__README.md"))
    first = badges[0]
    assert (first.line, first.alt, first.href) == (8, "latest release", "https://pypi.org/project/spikeinterface/")
    assert first.img == "https://img.shields.io/pypi/v/spikeinterface.svg"
    assert [b.line for b in badges[:5]] == [8, 16, 24, 32, 40]
    assert badges[3].img.endswith("/actions/workflows/full-test-with-codecov.yml/badge.svg")
    assert [b.href for b in badges[5:]] == [
        "https://twitter.com/spikeinterface",
        "https://fosstodon.org/@spikeinterface",
    ]
    assert [b.line for b in badges[5:]] == [47, 47]


def test_real_rst_readmes(real_fixture: Callable[[str], str]) -> None:
    pynwb = parse_badges(real_fixture("pynwb__README.rst"))
    logo = pynwb[0]
    assert (logo.img, logo.href, logo.line, logo.alt, logo.badge) == (
        "docs/source/figures/logo_pynwb.png",
        None,
        1,
        "",
        False,
    )
    assert pynwb[1].img == "https://badge.fury.io/py/pynwb.svg"
    assert pynwb[1].href == "https://badge.fury.io/py/pynwb"
    assert sum(b.href is not None for b in pynwb) == 15
    docs = next(b for b in pynwb if "readthedocs" in b.img)
    assert (docs.alt, docs.href, docs.line) == (
        "Documentation Status",
        "https://pynwb.readthedocs.io/en/latest/?badge=latest",
        49,
    )
    azure = pynwb[-1]
    assert azure.alt == "Conda Feedstock Status"
    assert azure.badge is False  # dev.azure.com is not a known badge host and its path has no "badge"

    hdmf = parse_badges(real_fixture("hdmf__README.rst"))
    assert all(b.href is not None for b in hdmf)
    assert sum("/actions/workflows/" in b.img for b in hdmf) == 10
    assert hdmf[-1].alt == "Documentation Status"
