"""Tests for :mod:`sistent.parsers.markdown`."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from sistent.parsers import ParseError
from sistent.parsers.markdown import MAX_CHARS, Document, Section, SectionNode, parse_document


def _headings(doc: Document) -> list[tuple[int, str]]:
    return [(s.level, s.heading_raw) for s in doc.sections[1:]]


def _section(doc: Document, heading: str) -> Section:
    return next(s for s in doc.sections if s.heading_raw == heading)


def _shape(nodes: list[SectionNode]) -> list[tuple[str, list[object]]]:
    return [(n.section.heading_raw, _shape(n.children)) for n in nodes]


# ----- headings ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("# A\n## B\n### C\n#### D\n##### E\n###### F", [(1, "A"), (2, "B"), (3, "C"), (4, "D"), (5, "E"), (6, "F")]),
        ("# A #\n## B ##  \n### C#\n#### D ###### ", [(1, "A"), (2, "B"), (3, "C#"), (4, "D")]),
        ("#NotHeading\n####### seven hashes", []),
        ("#", [(1, "")]),
        ("   ### three spaces ok\n    # four spaces is code", [(3, "three spaces ok")]),
        ("# Title with `code`, **bold** and [link](x)", [(1, "Title with `code`, **bold** and [link](x)")]),
        ("Title\n=====\nSub\n---\nbody", [(1, "Title"), (2, "Sub")]),
        ("Title\n=\n", [(1, "Title")]),
        ("wrapped\nTitle\n===", [(1, "Title")]),  # only the last paragraph line becomes the heading
        ("para\n\n---\n# A", [(1, "A")]),  # `---` after a blank line is a thematic break
        ("---\n# A", [(1, "A")]),  # at document start (no closing fence) too
        ("- item\n---", []),  # after a list item
        ("| a | b |\n---", []),  # after a table row
        ("> quote\n---", []),  # after a block quote
        ("para\n***", []),  # `***` is never a setext underline
        ("<h1>Not a heading</h1>\n<h2 align='center'>Nor this</h2>\n# Real", [(1, "Real")]),
        ("```\n# not a heading\n```\n~~~\n## nor this\n~~~\n# yes", [(1, "yes")]),
        ("````md\n```\n# still code\n```\n````\n# yes", [(1, "yes")]),
        ("```\n# unclosed fence runs to EOF\n", []),
        ("   ```\n   # fenced with 3 spaces indent\n   ```\n", []),
        ("> # quoted heading is prose", []),
        ("# A\r\n## B\r\n", [(1, "A"), (2, "B")]),
    ],
)
def test_headings(text: str, expected: list[tuple[int, str]]) -> None:
    assert _headings(parse_document(text)) == expected


def test_heading_lines_and_own_body() -> None:
    doc = parse_document("pre\n# A\na1\n\na2\n## B\nb1\nSetext\n======\ns1\n# C")
    assert [(s.heading_raw, s.line) for s in doc.sections] == [("", 0), ("A", 2), ("B", 6), ("Setext", 8), ("C", 11)]
    assert [s.body_lines for s in doc.sections] == [["pre"], ["a1", "", "a2"], ["b1"], ["s1"], []]
    assert [s.paragraphs for s in doc.sections] == [["pre"], ["a1", "a2"], ["b1"], ["s1"], []]
    assert doc.lines[1] == "# A"


# ----- front matter and comments -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "front_matter", "headings", "first_line"),
    [
        ("---\ntitle: x\ntags: [a]\n---\n# H\ntext", "title: x\ntags: [a]", [(1, "H")], 5),
        ("---\ntitle: x\n...\n# H", "title: x", [(1, "H")], 4),
        ("+++\nname = 'x'\n+++\n# H", "name = 'x'", [(1, "H")], 4),
        ("---\n---\n# H", "", [(1, "H")], 3),
        ("---\nnot closed\n# H", None, [(1, "H")], 3),
        ("text\n---\nmore\n", None, [(2, "text")], 1),  # not at line 1: an ordinary setext heading
        # blank first line: no front matter; `---` is then a thematic break and `a: 1` + `---` a setext heading
        ("\n---\na: 1\n---\n", None, [(2, "a: 1")], 3),
    ],
)
def test_front_matter(text: str, front_matter: str | None, headings: list[tuple[int, str]], first_line: int) -> None:
    doc = parse_document(text)
    assert doc.front_matter == front_matter
    assert _headings(doc) == headings
    if headings:
        assert doc.sections[1].line == first_line
    if front_matter is not None:
        assert doc.sections[0].paragraphs == []
        assert doc.lines[0] in ("---", "+++")  # the original lines are untouched


def test_html_comments_are_removed_and_line_numbers_kept() -> None:
    text = "<!-- c1 -->\n# A\n<!-- multi\nline\ncomment -->\ntext\n## B <!-- inline -->\n- x <!-- y -->\nafter"
    doc = parse_document(text)
    assert _headings(doc) == [(1, "A"), (2, "B")]
    assert doc.sections[1].paragraphs == ["text"]
    assert doc.sections[1].body_lines == ["", "", "", "text"]
    assert doc.sections[2].line == 7
    [block] = doc.sections[2].lists
    assert (block.items[0].text, block.items[0].line) == ("x after", 8)  # lazy continuation, comment gone
    assert doc.lines[0] == "<!-- c1 -->"


def test_unclosed_html_comment_hides_the_rest_of_the_document() -> None:
    # As in HTML and CommonMark: everything after an unclosed `<!--` is invisible when rendered.
    doc = parse_document("# A\ntext <!-- unclosed\n# B\n- item\n```\ncode\n```\n![img](x.png)")
    assert _headings(doc) == [(1, "A")]
    assert doc.sections[1].paragraphs == ["text"]
    assert doc.sections[1].lists == []
    assert doc.sections[1].code_blocks == []
    assert doc.badges == []
    assert doc.sections[1].body_lines == ["text ", "", "", "", "", "", ""]  # line numbers preserved


def test_html_comments_inside_fenced_code_are_kept() -> None:
    doc = parse_document("```\n<!-- keep -->\n```\n<!-- drop -->\n```\n<!-- also kept\n```\n# still visible")
    assert [c.lines for c in doc.sections[0].code_blocks] == [["<!-- keep -->"], ["<!-- also kept"]]
    assert doc.sections[0].paragraphs == []
    assert _headings(doc) == [(1, "still visible")]


# ----- lists -------------------------------------------------------------------------------------------------------


def test_nested_lists_three_levels() -> None:
    doc = parse_document("- a\n  - b\n    - c\n  - d\n- e\n\ntrailing")
    [block] = doc.sections[0].lists
    assert (block.ordered, block.line) == (False, 1)
    assert [i.text for i in block.items] == ["a", "e"]
    a = block.items[0]
    assert [c.text for c in a.children] == ["b", "d"]
    assert [c.text for c in a.children[0].children] == ["c"]
    assert a.children[0].children[0].line == 3
    assert a.children[0].children[0].raw == "- c"
    assert doc.sections[0].paragraphs == ["trailing"]


@pytest.mark.parametrize(
    ("text", "items", "paragraphs"),
    [
        ("- first line\nlazy continuation\n- second", ["first line lazy continuation", "second"], []),
        ("- first\n  indented continuation\n  more\n- second", ["first indented continuation more", "second"], []),
        ("- first\n\n  second paragraph of first\n- second", ["first second paragraph of first", "second"], []),
        ("- first\n\nnot part of the list", ["first"], ["not part of the list"]),
        ("- first\n# heading interrupts", ["first"], []),
        ("- first\n```\ncode\n```\nafter", ["first"], ["after"]),
        ("- first\n  ```\n  code in item\n  ```\n- second", ["first", "second"], []),
        ("- first\n> quote", ["first"], ["quote"]),
        ("- first\n---\nafter", ["first"], ["after"]),
        ("- first\n![img](x.png)\nafter", ["first"], ["after"]),
        ("-\n  text after empty marker", ["text after empty marker"], []),
        ("- first\n\n\n- second after two blanks", ["first", "second after two blanks"], []),
    ],
)
def test_continuation_and_termination(text: str, items: list[str], paragraphs: list[str]) -> None:
    doc = parse_document(text)
    assert [i.text for block in doc.sections[0].lists for i in block.items] == items
    assert doc.sections[0].paragraphs == paragraphs


def test_continuation_joins_the_deepest_open_item() -> None:
    doc = parse_document("1. one\n   - sub\n   continues sub\n\n   back to one\n2. two")
    [block] = doc.sections[0].lists
    assert block.ordered
    assert [i.text for i in block.items] == ["one back to one", "two"]
    assert [c.text for c in block.items[0].children] == ["sub continues sub"]


def test_code_inside_items_goes_to_the_section_not_the_item_text() -> None:
    doc = parse_document("- run:\n  ```shell\n  pip install x\n  ```\n- then\n\n      indented code in 'then'\n- last")
    [block] = doc.sections[0].lists
    assert [i.text for i in block.items] == ["run:", "then", "last"]
    assert [(c.info, c.lines, c.fenced, c.line) for c in doc.sections[0].code_blocks] == [
        ("shell", ["pip install x"], True, 2),
        ("", ["indented code in 'then'"], False, 7),
    ]


@pytest.mark.parametrize(
    ("line", "checkbox", "text"),
    [
        ("- [ ] todo", False, "todo"),
        ("- [x] done", True, "done"),
        ("- [X] DONE", True, "DONE"),
        ("1. [ ] ordered todo", False, "ordered todo"),
        ("- [y] not a box", None, "[y] not a box"),
        ("- [ ]not a box either", None, "[ ]not a box either"),
        ("- plain", None, "plain"),
        ("- [ ]", False, ""),
    ],
)
def test_task_boxes(line: str, checkbox: bool | None, text: str) -> None:
    [block] = parse_document(line).sections[0].lists
    item = block.items[0]
    assert (item.checkbox, item.text, item.raw) == (checkbox, text, line)


def test_ordered_lists_and_list_boundaries() -> None:
    doc = parse_document("1. a\n2. b\n3) c\n\n- x\n* y\n+ z\n\n10. ten\n+ back to bullets")
    lists = doc.sections[0].lists
    assert [(block.ordered, [i.text for i in block.items]) for block in lists] == [
        (True, ["a", "b", "c"]),
        (False, ["x", "y", "z"]),
        (True, ["ten"]),
        (False, ["back to bullets"]),
    ]
    assert [block.line for block in lists] == [1, 5, 9, 10]
    assert all(i.ordered for i in lists[0].items)
    assert not any(i.ordered for i in lists[1].items)
    assert lists[0].items[0].raw == "1. a"
    assert lists[0].items[2].line == 3


def test_ordered_item_not_starting_at_one_does_not_interrupt_a_paragraph() -> None:
    doc = parse_document("In the year\n2019. things happened\n1. but this starts a list")
    assert doc.sections[0].paragraphs == ["In the year 2019. things happened"]
    assert [i.text for i in doc.sections[0].lists[0].items] == ["but this starts a list"]


def test_mixed_nesting_by_column() -> None:
    doc = parse_document("1. one\n   1. one.one\n      - deep\n   2. one.two\n2. two\n 3. sibling at one space")
    [block] = doc.sections[0].lists
    assert [i.text for i in block.items] == ["one", "two", "sibling at one space"]
    one = block.items[0]
    assert [(c.text, c.ordered) for c in one.children] == [("one.one", True), ("one.two", True)]
    assert [c.text for c in one.children[0].children] == ["deep"]
    assert not one.children[0].children[0].ordered


def test_switching_between_bullets_and_numbers_starts_a_new_list() -> None:
    doc = parse_document("1. one\n2. two\n- bullet\n3. three")
    assert [(block.ordered, [i.text for i in block.items]) for block in doc.sections[0].lists] == [
        (True, ["one", "two"]),
        (False, ["bullet"]),
        (True, ["three"]),
    ]


# ----- code, tables, quotes, prose ---------------------------------------------------------------------------------


def test_code_blocks() -> None:
    text = "```python\nprint(1)\n```\n\n~~~ info string\ntilde\n~~~\n\n    indented\n\n    code\n\ntext\n\n\tfoo\n"
    doc = parse_document(text)
    pre = doc.sections[0]
    assert [(c.info, c.lines, c.line, c.fenced) for c in pre.code_blocks] == [
        ("python", ["print(1)"], 1, True),
        ("info string", ["tilde"], 5, True),
        ("", ["indented", "", "code"], 9, False),
        ("", ["foo"], 15, False),
    ]
    assert pre.paragraphs == ["text"]


def test_fence_content_is_dedented_by_the_fence_indent_and_never_scanned() -> None:
    doc = parse_document("  ```\n    x = 1\n  - not a list\n  # not a heading\n  ```\n")
    [block] = doc.sections[0].code_blocks
    assert block.lines == ["  x = 1", "- not a list", "# not a heading"]
    assert doc.sections[0].lists == []
    assert len(doc.sections) == 1


def test_tables_are_excluded_from_paragraphs() -> None:
    text = "intro\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\nafter\n\nx | y\n--|--\n1 | 2\nno pipe ends it\n\n| lone |\n"
    doc = parse_document(text)
    pre = doc.sections[0]
    assert pre.paragraphs == ["intro", "after", "no pipe ends it"]
    assert pre.tables == [["| a | b |", "|---|---|", "| 1 | 2 |"], ["x | y", "--|--", "1 | 2"], ["| lone |"]]


def test_pipe_in_prose_without_delimiter_row_is_not_a_table() -> None:
    doc = parse_document("use a | b\nto mean either")
    assert doc.sections[0].paragraphs == ["use a | b to mean either"]
    assert doc.sections[0].tables == []


def test_blockquotes_are_prose_with_markers_stripped() -> None:
    doc = parse_document("> quoted line\n> continues\n>\n> > nested\n\n> - not a list here\n>\n>   nor here")
    assert doc.sections[0].paragraphs == ["quoted line continues", "nested", "- not a list here", "nor here"]
    assert doc.sections[0].lists == []


def test_paragraphs_unwrap_and_html_tags_stay_prose() -> None:
    text = 'line one\nline two\n\n<h3 align="center">Tagline</h3>\n<table>\n<tr>\n    <td>Cell</td>\n\t<td>Tab</td>\n</tr>\n</table>'
    doc = parse_document(text)
    # tag-only lines (<table>, <tr>, </tr>) separate paragraphs; consecutive prose lines join
    assert doc.sections[0].paragraphs == [
        "line one line two",
        '<h3 align="center">Tagline</h3>',
        "<td>Cell</td> <td>Tab</td>",
    ]
    assert doc.sections[0].code_blocks == []  # indented markup inside an HTML block is not code


def test_thematic_breaks_separate_paragraphs() -> None:
    doc = parse_document("a\n\n* * *\nb\n___\nc")
    assert doc.sections[0].paragraphs == ["a", "b", "c"]
    assert doc.sections[0].lists == []


# ----- images, references, preamble --------------------------------------------------------------------------------


def test_image_only_lines_go_to_images_not_paragraphs() -> None:
    text = (
        "[![b](https://img.shields.io/x.svg)](https://x) ![logo](logo.png)\n"
        '<p align="center">\n  <img src="pic.png" alt="p"/>\n  <a href="https://d"><strong>Docs</strong></a>\n</p>\n'
        "real text ![inline](i.png)\n<br/>\n&nbsp;\n"
    )
    doc = parse_document(text)
    pre = doc.sections[0]
    assert pre.paragraphs == ['<a href="https://d"><strong>Docs</strong></a>', "real text ![inline](i.png)"]
    assert pre.images == ["https://img.shields.io/x.svg", "logo.png", "pic.png", "i.png"]
    assert [(b.badge, b.line) for b in doc.badges] == [(True, 1), (False, 1), (False, 3), (False, 6)]


def test_images_are_attributed_to_their_section() -> None:
    doc = parse_document("![a](a.png)\n# H\n![b](b.png)\n\n## I\ntext\n![c](c.png) ![d](d.png)\n```\n![e](e.png)\n```")
    assert [s.images for s in doc.sections] == [["a.png"], ["b.png"], ["c.png", "d.png"]]
    assert [b.img for b in doc.badges] == ["a.png", "b.png", "c.png", "d.png"]


def test_reference_definitions() -> None:
    text = (
        "[![ci][ci-img]][ci-link]\n\n[CI-IMG]: https://img.shields.io/ci.svg\n"
        '[ci-link]: <https://example.com/ci> "CI"\nSee [docs][Docs Ref].\n\n'
        "[docs  ref]:  https://docs.example.com  'Docs'\n[dup]: first\n[dup]: second\n[not a def]: \n"
    )
    doc = parse_document(text)
    assert doc.links == {
        "ci-img": "https://img.shields.io/ci.svg",
        "ci-link": "https://example.com/ci",
        "docs ref": "https://docs.example.com",
        "dup": "first",
    }
    assert doc.sections[0].paragraphs == ["See [docs][Docs Ref].", "[not a def]:"]
    [badge] = doc.badges
    assert (badge.img, badge.href, badge.syntax, badge.badge) == (
        "https://img.shields.io/ci.svg",
        "https://example.com/ci",
        "md-ref",
        True,
    )


def test_preamble_is_always_first() -> None:
    doc = parse_document("intro text\nwrapped\n\n- a\n\n# First\nbody")
    pre = doc.sections[0]
    assert (pre.level, pre.heading_raw, pre.line) == (0, "", 0)
    assert pre.paragraphs == ["intro text wrapped"]
    assert [i.text for i in pre.lists[0].items] == ["a"]
    assert pre.body_lines == ["intro text", "wrapped", "", "- a", ""]
    assert doc.sections[1].body_lines == ["body"]

    only = parse_document("# Only")
    assert only.sections[0].body_lines == []
    assert only.sections[0].paragraphs == []
    assert len(only.sections) == 2

    empty = parse_document("")
    assert (empty.sections[0].level, empty.lines, empty.front_matter, empty.badges) == (0, [], None, [])


# ----- title and tree ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("# T\n## A\n## B", "T"),
        ("# T", "T"),
        ("intro\n\n# T\n## A", "T"),
        ("Title\n=====\n## A", "Title"),
        ("# T\ntext\n# U", None),  # two H1s
        ("## A\n# T", None),  # first heading is not an H1
        ("## A\n## B", None),
        ("no headings", None),
        ("", None),
        ("# **Bold** title `raw`", "**Bold** title `raw`"),  # not normalised
    ],
)
def test_title(text: str, title: str | None) -> None:
    assert parse_document(text).title == title


def test_tree() -> None:
    doc = parse_document("pre\n# T\n## A\n### A1\n## B\n# U\n### deep\n## after deep")
    assert _shape(doc.tree()) == [
        ("T", [("A", [("A1", [])]), ("B", [])]),
        ("U", [("deep", []), ("after deep", [])]),
    ]
    assert doc.tree()[0].section is doc.sections[1]
    assert parse_document("pre only").tree() == []
    assert _shape(parse_document("## A\n# B").tree()) == [("A", []), ("B", [])]


# ----- robustness --------------------------------------------------------------------------------------------------


def test_parse_error_on_absurd_input() -> None:
    with pytest.raises(ParseError, match="too large"):
        parse_document("x" * (MAX_CHARS + 1), source="big.md")
    with pytest.raises(ParseError) as info:
        parse_document("a\x00b", source="bin.md")
    assert info.value.source == "bin.md"
    assert "NUL" in info.value.reason


@pytest.mark.parametrize(
    "text",
    [
        "\n\n\n",
        "```",
        "~~~\n```",
        "---",
        "- ",
        "-",
        "1.",
        "1)",
        "[]: ",
        "[x]:",
        "<!--",
        "-->",
        "|",
        "||",
        "> ",
        ">",
        "#",
        "    ",
        "\t- x\n\t\t- y",
        "* * *",
        "=",
        "===",
        "- a\n  ```\n  unclosed in list",
        "<img",
        "![",
        "[![a](b)](c",
        "﻿# bom",
        "a\rb\r\nc",
        "- [ ]\n  - [x]\n    - [ ] ",
        "1. a\n\n\n\n        deep indented code\n2. b",
    ],
)
def test_odd_input_never_raises(text: str) -> None:
    doc = parse_document(text)
    assert doc.sections[0].level == 0
    assert doc.tree() is not None


def test_line_endings_and_bom() -> None:
    doc = parse_document("﻿# A\r\ntext\rmore\r\n")
    assert doc.lines == ["# A", "text", "more"]
    assert _headings(doc) == [(1, "A")]
    assert doc.sections[1].paragraphs == ["text more"]


# ----- real fixtures -----------------------------------------------------------------------------------------------


def test_real_copilot_instructions(real_fixture: Callable[[str], str]) -> None:
    doc = parse_document(real_fixture("neuroconv__.github_copilot-instructions.md"), source="copilot-instructions.md")
    assert doc.source == "copilot-instructions.md"
    assert doc.title == "Custom instructions for GitHub Copilot in the **NeuroConv** repository"
    level2 = [s for s in doc.sections if s.level == 2]
    assert len(level2) == 5
    assert [s.level for s in doc.sections] == [0, 1, 2, 2, 2, 2, 2]

    workflow = _section(doc, "2 Committing & pushing workflow")
    assert workflow.line == 10
    [block] = workflow.lists
    assert block.ordered
    assert len(block.items) == 4
    assert [len(i.children) for i in block.items] == [0, 0, 3, 1]
    assert block.items[0].text == "**Make your code changes** and test them locally."
    assert block.items[2].text == "**Repeat until pre-commit passes:**"
    assert [c.line for c in block.items[2].children] == [14, 15, 16]
    assert (
        block.items[2].children[0].text == "Pre-commit hooks will run automatically on commit and may auto-fix issues"
    )
    assert block.items[3].children[0].text.startswith("The tool should only push existing commits")
    assert workflow.paragraphs == []

    env = level2[0]
    [env_list] = env.lists
    assert not env_list.ordered
    assert [i.line for i in env_list.items] == [4, 6]
    assert env_list.items[0].text == (
        "**Read `.github/copilot-setup-steps.yml` first.** It shows the Python version, pre-installed packages, "
        "and tools (pytest, pre-commit, ruff, mypy, etc.) already available on the runner."
    )
    assert env_list.items[1].text.endswith("pin the version and briefly explain the need in the PR body.")

    assert _shape(doc.tree()) == [(doc.title or "", [(s.heading_raw, []) for s in level2])]


def test_real_spikeinterface_agents(real_fixture: Callable[[str], str]) -> None:
    doc = parse_document(real_fixture("spikeinterface__AGENTS.md"))
    assert doc.title == "Instructions for automated agents"
    assert _headings(doc) == [
        (1, "Instructions for automated agents"),
        (2, "General instructions"),
        (2, "Testing"),
    ]
    general = _section(doc, "General instructions")
    assert len(general.paragraphs) == 3
    assert general.paragraphs[0] == "Do not open pull requests against this repository."
    assert general.paragraphs[1].startswith(
        "You may: read the code, answer questions about it, suggest patches in your reply"
    )
    assert general.paragraphs[1].endswith("open issues or comment on existing issues.")
    assert general.paragraphs[2].startswith("If your user asked you to contribute a fix")
    assert general.lists == []
    assert general.code_blocks == []
    assert _section(doc, "Testing").paragraphs == [
        "Do not blow up the number of tests. We want the testing suite to remain manageable and efficient."
    ]


@pytest.mark.parametrize(
    ("name", "title", "images", "badges", "level2"),
    [
        (
            "neuroconv__README.md",
            None,
            10,
            9,
            ["Table of Contents", "About", "Installation", "Documentation", "Citing NeuroConv", "License"],
        ),
        (
            "roiextractors__README.md",
            "ROIExtractors",
            7,
            7,
            ["Table of Contents", "About", "Installation", "Documentation", "Funding", "License"],
        ),
        (
            "spikeinterface__README.md",
            "SpikeInterface: a unified framework for spike sorting",
            7,
            7,
            ["Documentation", "How to install spikeinterface", "Citation"],
        ),
    ],
)
def test_real_readmes_parse_and_expose_badges(
    real_fixture: Callable[[str], str], name: str, title: str | None, images: int, badges: int, level2: list[str]
) -> None:
    doc = parse_document(real_fixture(name), source=name)
    assert doc.title == title
    assert len(doc.badges) == images
    assert sum(b.badge for b in doc.badges) == badges
    assert [s.heading_raw for s in doc.sections if s.level == 2] == level2
    assert sum(len(s.images) for s in doc.sections) == images


def test_real_neuroconv_readme_structure(real_fixture: Callable[[str], str]) -> None:
    doc = parse_document(real_fixture("neuroconv__README.md"))
    pre = doc.sections[0]
    assert len(pre.images) == 10
    assert pre.images[-1].endswith("/docs/img/neuroconv_logo.png")
    assert pre.paragraphs == [
        '<h3 align="center">Automatically convert neurophysiology data to NWB</h3>',
        '<a href="https://neuroconv.readthedocs.io/"><strong>Explore our documentation »</strong></a>',
    ]
    toc = _section(doc, "Table of Contents")
    assert [i.text for i in toc.lists[0].items] == [
        "[About](#about)",
        "[Installation](#installation)",
        "[Documentation](#documentation)",
        "[License](#license)",
    ]
    about = _section(doc, "About")
    assert about.paragraphs[1] == "Features:"
    assert len(about.lists[0].items) == 5
    install = _section(doc, "Installation")
    assert [c.info for c in install.code_blocks] == ["shell"] * 4
    assert install.code_blocks[1].lines == ["pip install neuroconv"]
    assert len(install.paragraphs) == 6
    citing = _section(doc, "Citing NeuroConv")
    assert citing.paragraphs[1].startswith("Mayorquin, H., Baker, C.")  # block quote stripped
    bibtex = _section(doc, "BibTeX")
    assert (bibtex.level, bibtex.code_blocks[0].info) == (3, "bibtex")
    assert _shape(doc.tree())[4] == ("Citing NeuroConv", [("BibTeX", [])])


def test_real_spikeinterface_readme_structure(real_fixture: Callable[[str], str]) -> None:
    doc = parse_document(real_fixture("spikeinterface__README.md"))
    top = doc.sections[1]
    assert top.level == 1
    assert len(top.images) == 7  # 5 in the HTML table, 2 social badges on line 47
    assert [len(block.items) for block in top.lists] == [7, 14]
    assert top.lists[1].items[2].text.startswith("run many popular, semi-automatic spike sorters")
    assert "tridesclous, ironclust, herdingspikes, yass, waveclus)" in top.lists[1].items[2].text
    assert top.paragraphs[:5] == [
        "<td>Latest Release</td>",
        "<td>Documentation</td>",
        "<td>License</td>",
        "<td>Build Status</td>",
        "<td>Codecov</td>",
    ]
    assert top.paragraphs[5].startswith("Please [Star]")
    assert top.paragraphs[-1] == "With SpikeInterface, users can:"
    assert top.code_blocks == []
    install = _section(doc, "How to install spikeinterface")
    assert [c.lines for c in install.code_blocks] == [
        ['pip install "spikeinterface[full]"'],
        [' pip install "spikeinterface[full,widgets]"'],
        [
            "git clone https://github.com/SpikeInterface/spikeinterface.git",
            "cd spikeinterface",
            "pip install -e .",
            "cd ..",
        ],
    ]
