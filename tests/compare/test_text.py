"""Tests for :mod:`sistent.compare.text`."""

from __future__ import annotations

import difflib
import random
import re
from collections.abc import Callable, Sequence

import pytest

from sistent.compare.text import (
    BRANCH,
    NAME,
    ORG,
    NullSubstituter,
    Substituter,
    classify_lines,
    excerpt,
    jaccard,
    line_counts,
    normalize_heading,
    normalize_text,
    ratio,
    shingles,
    similarity,
    strip_inline_markup,
    unified_diff,
    unwrap,
)
from sistent.model import Identity, StaleHit

# ----- strip_inline_markup -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("See [LICENSE](https://x/y) for details", "See LICENSE for details", id="link-to-text"),
        pytest.param("[ref link][1] here", "ref link here", id="reference-link"),
        pytest.param("![logo](img.png) text", " text", id="image-dropped"),
        pytest.param("![img][ref] text", " text", id="reference-image-dropped"),
        pytest.param("[![badge](img.svg)](https://ci)", "", id="badge-dropped"),
        pytest.param("use `pip install x` now", "use pip install x now", id="code-span"),
        pytest.param("``a `b` c`` d", "a `b` c d", id="double-backtick-span"),
        pytest.param("**bold** and __strong__", "bold and strong", id="strong"),
        pytest.param("*em* and _em_", "em and em", id="emphasis"),
        pytest.param("~~gone~~ kept", "gone kept", id="strike-markers"),
        pytest.param(
            "snake_case_name and my_var_1 stay", "snake_case_name and my_var_1 stay", id="snake-case-untouched"
        ),
        pytest.param("2 * 3 * 4", "2 * 3 * 4", id="lone-asterisks-untouched"),
        pytest.param("<b>html</b> <a name='x'></a> tag <br/>", "html  tag ", id="html-tags"),
        pytest.param("<https://example.com> autolink", "https://example.com autolink", id="autolink"),
        pytest.param("note[^1] here", "note here", id="footnote"),
        pytest.param("<!-- hidden\nacross lines --> visible", " visible", id="html-comment"),
        pytest.param("plain text.", "plain text.", id="plain"),
        pytest.param("", "", id="empty"),
    ],
)
def test_strip_inline_markup(text: str, expected: str) -> None:
    assert strip_inline_markup(text) == expected


# ----- normalize_text ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("  Hello   World. ", "hello world", id="whitespace-case-trailing-dot"),
        pytest.param("Run `pytest` **now**:", "run pytest now", id="markup-and-trailing-colon"),
        pytest.param("End;", "end", id="trailing-semicolon"),
        pytest.param("\u201cQuoted\u201d \u2018text\u2019", "\"quoted\" 'text'", id="straight-quotes"),
        pytest.param("ﬁ ligature", "fi ligature", id="nfkc"),
        pytest.param("Straße", "strasse", id="casefold"),
        pytest.param("see [docs](https://d) and ![x](y)", "see docs and", id="links-images"),
        pytest.param("a\tb\nc", "a b c", id="tabs-newlines"),
        pytest.param("...", "", id="only-punctuation"),
        pytest.param("", "", id="empty"),
    ],
)
def test_normalize_text(text: str, expected: str) -> None:
    assert normalize_text(text) == expected


# ----- normalize_heading -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        pytest.param("1 Environment awareness", "environment awareness", id="number-no-dot"),
        pytest.param("1. Environment awareness", "environment awareness", id="number-dot"),
        pytest.param("2.3 Foo", "foo", id="dotted-number"),
        pytest.param("2.3. Foo", "foo", id="dotted-number-trailing-dot"),
        pytest.param("3) Foo", "foo", id="number-paren"),
        pytest.param("IV. Setup", "setup", id="roman"),
        pytest.param("iv) Setup", "setup", id="roman-lower-paren"),
        pytest.param("Step 3: Foo", "foo", id="step-colon"),
        pytest.param("Step 3 - Foo", "foo", id="step-dash"),
        pytest.param("Step 3 \u2013 Foo", "foo", id="step-en-dash"),
        pytest.param("\U0001f680 Quick start", "quick start", id="emoji"),
        pytest.param("Testing \U0001f9ea", "testing", id="trailing-emoji"),
        pytest.param("\u2705 Done \ufe0f", "done", id="emoji-with-variation-selector"),
        pytest.param(":rocket: Quick start", "quick start", id="shortcode"),
        pytest.param("Foo {#foo-id}", "foo", id="anchor-id"),
        pytest.param("Foo ##", "foo", id="trailing-hashes"),
        pytest.param("Foo #", "foo", id="trailing-hash"),
        pytest.param("**NeuroConv**", "neuroconv", id="bold"),
        pytest.param("`code` heading", "code heading", id="code-span"),
        pytest.param("CI / workflow changes", "ci / workflow changes", id="ci-slash-not-numbering"),
        pytest.param('Installation <a name="install"></a>', "installation", id="html-anchor"),
        pytest.param("Usage [docs](https://x)", "usage docs", id="link"),
        pytest.param("  License:  ", "license", id="trailing-colon-whitespace"),
        pytest.param("\U0001f389", "", id="only-emoji-is-empty"),
        pytest.param("", "", id="empty"),
    ],
)
def test_normalize_heading(heading: str, expected: str) -> None:
    assert normalize_heading(heading) == expected


@pytest.mark.parametrize(
    ("heading", "expected"),
    [("CLI: Usage", "cli: usage"), ("CI: run tests", "ci: run tests")],
)
def test_normalize_heading_keeps_roman_looking_words(heading: str, expected: str) -> None:
    assert normalize_heading(heading) == expected


# ----- unwrap ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        pytest.param(["a line", "continued here"], ["a line continued here"], id="joins-wrapped"),
        pytest.param(["one", "", "two"], ["one", "two"], id="blank-splits"),
        pytest.param(["one", "  ", "two"], ["one", "two"], id="whitespace-line-splits"),
        pytest.param(["  indented ", "  more  "], ["indented more"], id="strips-each-line"),
        pytest.param(
            ["para", "- item", "lazy", "- item 2"], ["para", "- item lazy", "- item 2"], id="list-starts-block"
        ),
        pytest.param(["para", "1. step", "2) step"], ["para", "1. step", "2) step"], id="ordered-list"),
        pytest.param(["para", "# heading"], ["para", "# heading"], id="heading-starts-block"),
        pytest.param(["para", "```", "code"], ["para", "``` code"], id="fence-starts-block"),
        pytest.param(["para", "| a | b |"], ["para", "| a | b |"], id="table-starts-block"),
        pytest.param(["para", "> quote"], ["para", "> quote"], id="quote-starts-block"),
        pytest.param([], [], id="empty"),
        pytest.param(["", ""], [], id="only-blank"),
    ],
)
def test_unwrap(lines: Sequence[str], expected: list[str]) -> None:
    assert unwrap(lines) == expected


# ----- shingles / jaccard ------------------------------------------------------------------------------------------


def test_shingles_ngrams_and_short_texts() -> None:
    assert shingles("a b c d") == frozenset({("a", "b", "c"), ("b", "c", "d")})
    assert shingles("a b c") == frozenset({("a", "b", "c")})
    assert shingles("a b") == frozenset({("a",), ("b",)})
    assert shingles("") == frozenset()
    assert shingles("a b c d", n=2) == frozenset({("a", "b"), ("b", "c"), ("c", "d")})


def test_jaccard_basics() -> None:
    assert jaccard(frozenset(), frozenset()) == 1.0
    assert jaccard(frozenset({("a",)}), frozenset()) == 0.0
    assert jaccard(frozenset({("a",), ("b",)}), frozenset({("b",), ("c",)})) == pytest.approx(1 / 3)
    assert similarity("a b c d", "a b c d") == 1.0
    assert similarity("a b c d", "x y z w") == 0.0


def _permutations(chunks: list[str]) -> list[list[str]]:
    half = len(chunks) // 2
    shuffled = chunks[:]
    random.Random(7).shuffle(shuffled)
    return [chunks[::-1], chunks[1:] + chunks[:1], chunks[half:] + chunks[:half], shuffled]


def test_jaccard_is_stable_under_section_shuffle(real_fixture: Callable[[str], str]) -> None:
    text = real_fixture("spikeinterface__README.md")
    sections = [s for s in re.split(r"\n(?=#{1,3} )", text) if s.strip()]
    assert len(sections) >= 4
    base = shingles("\n\n".join(sections))
    for permuted in _permutations(sections):
        assert permuted != sections
        assert jaccard(base, shingles("\n\n".join(permuted))) >= 0.85


def test_jaccard_is_stable_under_paragraph_shuffle(real_fixture: Callable[[str], str]) -> None:
    text = real_fixture("pynwb__README.rst")
    paragraphs = [p for p in text.split("\n\n") if len(p.split()) >= 30]
    assert len(paragraphs) >= 5
    base = shingles(normalize_text("\n\n".join(paragraphs)))
    for permuted in _permutations(paragraphs):
        assert jaccard(base, shingles(normalize_text("\n\n".join(permuted)))) >= 0.85


def test_jaccard_of_unrelated_readmes_is_low(real_fixture: Callable[[str], str]) -> None:
    neuroconv = shingles(normalize_text(real_fixture("neuroconv__README.md")))
    spike = shingles(normalize_text(real_fixture("spikeinterface__README.md")))
    hdmf = shingles(normalize_text(real_fixture("hdmf__README.rst")))
    assert jaccard(neuroconv, spike) < 0.2
    assert jaccard(neuroconv, hdmf) < 0.2
    assert jaccard(spike, hdmf) < 0.2


# ----- classify_lines / line_counts --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("main", "other", "expected"),
    [
        pytest.param(["a", "b"], ["a", "b"], "equal", id="equal"),
        pytest.param([], [], "equal", id="both-empty"),
        pytest.param(["a", "b"], ["a", "x", "b"], "insert", id="insert-middle"),
        pytest.param(["a"], ["a", "x", "y"], "insert", id="insert-end"),
        pytest.param([], ["a"], "insert", id="insert-into-empty"),
        pytest.param(["a", "x", "b"], ["a", "b"], "delete", id="delete-middle"),
        pytest.param(["a"], [], "delete", id="delete-all"),
        pytest.param(["a", "x"], ["a", "y"], "mixed", id="replace"),
        pytest.param(["a", "x", "b"], ["a", "b", "y"], "mixed", id="delete-and-insert"),
        pytest.param(["a", "b"], ["b", "a"], "mixed", id="reorder"),
    ],
)
def test_classify_lines(main: Sequence[str], other: Sequence[str], expected: str) -> None:
    assert classify_lines(main, other) == expected


@pytest.mark.parametrize(
    ("main", "other", "expected"),
    [
        pytest.param(["a", "b"], ["a", "b"], (0, 0), id="equal"),
        pytest.param(["a", "b"], ["a", "x", "b"], (1, 0), id="insert"),
        pytest.param(["a", "x", "b"], ["a", "b"], (0, 1), id="delete"),
        pytest.param(["a", "x"], ["a", "y"], (1, 1), id="replace"),
        pytest.param(["a", "b", "c"], ["a", "x", "c", "d"], (2, 1), id="replace-and-insert"),
        pytest.param(["a", "b", "c"], [], (0, 3), id="all-removed"),
    ],
)
def test_line_counts(main: Sequence[str], other: Sequence[str], expected: tuple[int, int]) -> None:
    assert line_counts(main, other) == expected


# ----- ratio -------------------------------------------------------------------------------------------------------


def test_ratio_matches_difflib_without_autojunk() -> None:
    a, b = "run pytest tests/ before committing", "run pytest before committing"
    assert ratio(a, b) == difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    assert ratio(a, b) == pytest.approx(0.889, abs=0.001)
    assert ratio("same", "same") == 1.0
    assert ratio("", "") == 1.0
    assert ratio("abc", "") == 0.0


def test_ratio_cutoff_short_circuits_below_and_is_exact_above() -> None:
    full = ratio("kitten", "sitting")
    assert 0.6 < full < 0.7
    assert ratio("kitten", "sitting", cutoff=0.5) == full
    assert ratio("kitten", "sitting", cutoff=0.9) == 0.0
    assert ratio("abc", "xyz", cutoff=0.1) == 0.0


def test_ratio_long_strings_are_not_junked() -> None:
    """``autojunk=False``: a repeated character over a long string still scores as identical."""
    long = "x" * 400
    assert ratio(long, long) == 1.0
    assert ratio(long, long + "y") > 0.99


# ----- unified_diff ------------------------------------------------------------------------------------------------


def test_unified_diff_headers_and_context() -> None:
    out = unified_diff(["a", "b", "c"], ["a", "x", "c"], fromfile="main/README.md#L", tofile="repo/README.md#L")
    lines = out.split("\n")
    assert lines[0] == "--- main/README.md#L"
    assert lines[1] == "+++ repo/README.md#L"
    assert "-b" in lines
    assert "+x" in lines
    assert not out.endswith("\n")


def test_unified_diff_equal_is_empty() -> None:
    assert unified_diff(["a"], ["a"], fromfile="a", tofile="b") == ""


def test_unified_diff_context_option() -> None:
    a = [str(i) for i in range(20)]
    b = [*a[:10], "x", *a[10:]]
    wide = unified_diff(a, b, fromfile="a", tofile="b", context=3)
    narrow = unified_diff(a, b, fromfile="a", tofile="b", context=0)
    assert len(wide.split("\n")) > len(narrow.split("\n"))
    assert " 9" in wide.split("\n")
    assert " 9" not in narrow.split("\n")


def test_unified_diff_truncation() -> None:
    a = [str(i) for i in range(50)]
    b = [str(i) for i in range(1, 51)]
    full = list(difflib.unified_diff(a, b, fromfile="a", tofile="b", n=3, lineterm=""))
    assert len(full) > 10
    out = unified_diff(a, b, fromfile="a", tofile="b", max_lines=10)
    lines = out.split("\n")
    assert lines[:10] == full[:10]
    assert lines[10] == f"... (truncated, {len(full) - 10} more lines)"
    assert len(lines) == 11
    untruncated = unified_diff(a, b, fromfile="a", tofile="b", max_lines=len(full))
    assert untruncated.split("\n") == full


# ----- excerpt -----------------------------------------------------------------------------------------------------


def test_excerpt_window_and_ellipses() -> None:
    text = "x" * 100 + "HIT" + "y" * 100
    assert excerpt(text, 100, 103, width=5) == "...xxxxxHITyyyyy..."
    assert excerpt("short HIT text", 6, 9) == "short HIT text"
    assert excerpt("HIT then more", 0, 3, width=4) == "HIT the..."
    assert excerpt("some HIT", 5, 8, width=2) == "...e HIT"
    assert excerpt("HIT", 0, 3) == "HIT"


def test_excerpt_collapses_whitespace_to_one_line() -> None:
    assert excerpt("a\n\n  b HIT c\td", 7, 10, width=100) == "a b HIT c d"


# ----- Substituter -------------------------------------------------------------------------------------------------

NEUROCONV = Identity(name="neuroconv", aliases=("NeuroConv", "neuroconv"), org="catalystneuro", branch="main")
PYNWB = Identity(name="pynwb", aliases=("pynwb", "PyNWB"))
LICENSE_SENTENCE = (
    "NeuroConv is distributed under the BSD3 License. See "
    "[LICENSE](https://github.com/catalystneuro/neuroconv/blob/main/license.txt) for details."
)


def test_placeholders() -> None:
    assert (NAME, ORG, BRANCH) == ("{{name}}", "{{org}}", "{{branch}}")


def test_substituter_license_sentence() -> None:
    subst = Substituter(NEUROCONV)
    assert subst(LICENSE_SENTENCE, where="README.md#License") == (
        "{{name}} is distributed under the BSD3 License. See "
        "[LICENSE](https://github.com/{{org}}/{{name}}/blob/{{branch}}/license.txt) for details."
    )
    assert subst.hits == []


def test_substituter_makes_templated_sections_identical() -> None:
    roi = Identity(name="roiextractors", aliases=("roiextractors", "ROIExtractors"), org="catalystneuro", branch="main")
    roi_sentence = LICENSE_SENTENCE.replace("NeuroConv", "ROIExtractors").replace("neuroconv", "roiextractors")
    assert Substituter(NEUROCONV)(LICENSE_SENTENCE) == Substituter(roi)(roi_sentence)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("pip install neuroconv[dandi]", "pip install {{name}}[dandi]", id="extras-bracket"),
        pytest.param("git clone neuroconv.git", "git clone {{name}}.git", id="dot-git"),
        pytest.param("neuroconv_logo.png", "{{name}}_logo.png", id="underscore-boundary"),
        pytest.param("neuroconvert is different", "neuroconvert is different", id="longer-word-untouched"),
        pytest.param("myneuroconv", "myneuroconv", id="prefixed-untouched"),
        pytest.param("neuroconv2", "neuroconv2", id="digit-suffix-untouched"),
        pytest.param("NEUROCONV and neuroConv", "{{name}} and {{name}}", id="case-insensitive"),
        pytest.param("(neuroconv)", "({{name}})", id="parentheses"),
        pytest.param("the main branch in prose", "the main branch in prose", id="branch-word-in-prose"),
        pytest.param("main", "main", id="bare-branch"),
        pytest.param("/blob/main/README.md", "/blob/{{branch}}/README.md", id="branch-blob"),
        pytest.param("/tree/main", "/tree/{{branch}}", id="branch-tree"),
        pytest.param("/blob/mainline/x", "/blob/mainline/x", id="branch-prefix-untouched"),
        pytest.param("?branch=main&x=1", "?branch={{branch}}&x=1", id="branch-query"),
        pytest.param("badge/?version=main", "badge/?version={{branch}}", id="version-query"),
        pytest.param(
            "https://neuroconv.readthedocs.io/en/main/", "https://{{name}}.readthedocs.io/en/{{branch}}/", id="rtd-en"
        ),
        pytest.param(
            "pip install git+https://github.com/catalystneuro/neuroconv.git@main",
            "pip install git+https://github.com/{{org}}/{{name}}.git@{{branch}}",
            id="git-at-branch",
        ),
        pytest.param("by catalystneuro", "by {{org}}", id="org-alone"),
        pytest.param("catalystneuro/neuroconv", "{{org}}/{{name}}", id="slug"),
        pytest.param("", "", id="empty"),
    ],
)
def test_substituter_boundaries_and_contexts(text: str, expected: str) -> None:
    assert Substituter(NEUROCONV)(text) == expected


def test_substituter_org_equal_to_alias_in_url() -> None:
    spike = Identity(name="spikeinterface", aliases=("spikeinterface",), org="SpikeInterface")
    assert (
        Substituter(spike)("https://github.com/SpikeInterface/spikeinterface") == "https://github.com/{{org}}/{{name}}"
    )


def test_substituter_extra_vars() -> None:
    ident = Identity(name="pkg", aliases=("pkg",), extra={"docs": "docs.example.org", "name": "ignored"})
    assert Substituter(ident)("see https://docs.example.org/ for pkg") == "see https://{{docs}}/ for {{name}}"


def test_substituter_extra_var_containing_an_alias() -> None:
    ident = Identity(name="neuroconv", aliases=("neuroconv",), extra={"docs": "neuroconv.readthedocs.io"})
    assert Substituter(ident)("https://neuroconv.readthedocs.io/en/") == "https://{{docs}}/en/"


def test_substituter_records_applied_aliases() -> None:
    subst = Substituter(NEUROCONV)
    assert subst.applied == set()
    subst(LICENSE_SENTENCE)
    assert "NeuroConv" in subst.applied
    subst("pip install neuroconv")
    assert {"NeuroConv", "neuroconv"} <= subst.applied


def test_substituter_stale_hit_for_foreign_alias() -> None:
    roi = Identity(name="roiextractors", aliases=("roiextractors",), org="catalystneuro")
    subst = Substituter(roi, [PYNWB])
    text = "https://img.shields.io/pypi/l/pynwb"
    assert subst(text, where="README.md#badges") == text
    assert subst.hits == [StaleHit(where="README.md#badges", alias="pynwb", other_repo="pynwb", excerpt=text)]


def test_substituter_stale_hits_accumulate_with_excerpt_and_case() -> None:
    subst = Substituter(NEUROCONV, [PYNWB])
    subst("Built on PyNWB. " + "x " * 60 + "See pynwb docs.", where="README.md#Intro")
    assert [(h.alias, h.other_repo, h.where) for h in subst.hits] == [
        ("PyNWB", "pynwb", "README.md#Intro"),
        ("pynwb", "pynwb", "README.md#Intro"),
    ]
    assert subst.hits[0].excerpt.startswith("Built on PyNWB.")
    assert subst.hits[0].excerpt.endswith("...")
    assert subst.hits[1].excerpt.endswith("See pynwb docs.")


def test_substituter_own_aliases_are_never_stale() -> None:
    own = Identity(name="a", aliases=("shared", "mine"))
    foreign = Identity(name="b", aliases=("shared", "other-b", "sho"))
    subst = Substituter(own, [foreign])
    assert subst("shared and mine and other-b and sho") == "{{name}} and {{name}} and other-b and sho"
    assert [(h.alias, h.other_repo) for h in subst.hits] == [("other-b", "b")], "short (<4) foreign aliases are ignored"


def test_substituter_skips_foreign_entry_for_itself() -> None:
    subst = Substituter(NEUROCONV, [NEUROCONV, PYNWB])
    subst("neuroconv and pynwb")
    assert [h.other_repo for h in subst.hits] == ["pynwb"]


def test_substituter_disabled_leaves_text_but_still_scans() -> None:
    subst = Substituter(NEUROCONV, [PYNWB], enabled=False)
    assert subst("NeuroConv and pynwb") == "NeuroConv and pynwb"
    assert subst.applied == set()
    assert [h.alias for h in subst.hits] == ["pynwb"]


def test_substituter_stale_disabled() -> None:
    subst = Substituter(NEUROCONV, [PYNWB], stale=False)
    assert subst("NeuroConv and pynwb") == "{{name}} and pynwb"
    assert subst.hits == []


def test_substituter_many() -> None:
    subst = Substituter(NEUROCONV, [PYNWB])
    assert subst.many(["neuroconv", "pynwb", ""], where="w") == ["{{name}}", "pynwb", ""]
    assert [h.where for h in subst.hits] == ["w"]


def test_substituter_without_org_or_branch() -> None:
    subst = Substituter(Identity(name="x", aliases=("xpkg",)))
    assert subst("xpkg by someorg on /blob/main/") == "{{name}} by someorg on /blob/main/"


def test_null_substituter() -> None:
    subst = NullSubstituter()
    text = "NeuroConv by catalystneuro on /blob/main/ with pynwb"
    assert subst(text, where="anywhere") == text
    assert subst.hits == []
    assert subst.applied == set()
    assert subst.many([text]) == [text]
