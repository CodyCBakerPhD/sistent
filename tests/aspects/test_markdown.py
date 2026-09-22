"""Tests for the ``markdown`` aspect type: options, extraction, comparison, self-check and real-fixture noise."""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import pytest

from sistent.aspects import DEFAULT_CONFIG
from sistent.aspects.markdown import MarkdownAspect, MarkdownOptions
from sistent.model import Direction, Finding, Identity, Kind, Severity, Snapshot, Subject
from sistent.options import OptionsError, parse_options
from tests.conftest import MakeRepo

Key = tuple[Kind, Subject, Direction, str]

NEUROCONV = Identity(name="neuroconv", aliases=("NeuroConv", "neuroconv"), org="catalystneuro", branch="main")
ROIEXTRACTORS = Identity(
    name="roiextractors", aliases=("ROIExtractors", "roiextractors"), org="catalystneuro", branch="main"
)
SPIKEINTERFACE = Identity(
    name="spikeinterface", aliases=("SpikeInterface", "spikeinterface"), org="SpikeInterface", branch="main"
)

SMALL_AGENTS = """\
# Instructions for agents

Read the README first.

## Development

- Run `pytest -q` before committing.
- Use **ruff** for linting.

### Testing

1. Write the test.
2. Run it.

## Style

Keep lines under 120 chars.
"""


# ----- helpers -----------------------------------------------------------------------------------------------------


def keys(findings: Iterable[Finding]) -> set[Key]:
    return {(f.kind, f.subject, f.direction, f.locator) for f in findings}


def aspect(name: str = "agents", **options: Any) -> MarkdownAspect:
    options.setdefault("files", ["AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"])
    return MarkdownAspect(name, MarkdownOptions(**options))


def default_options(name: str) -> MarkdownOptions:
    """The built-in ``[aspects.<name>]`` table of ``DEFAULT_CONFIG`` parsed against :class:`MarkdownOptions`."""
    table = dict(tomllib.loads(DEFAULT_CONFIG)["aspects"][name])
    assert table.pop("type") == "markdown"
    return parse_options(MarkdownOptions, table, where=f"[aspects.{name}]")


def rule(text: str, raw: str | None = None, line: int = 0, parent: str | None = None) -> dict[str, Any]:
    return {"text": text, "raw": raw if raw is not None else f"- {text}", "line": line, "parent": parent}


def node(
    path: Sequence[str],
    *,
    source: str = "AGENTS.md",
    display: Sequence[str] | None = None,
    mode: str = "full",
    rules: Sequence[str | dict[str, Any]] = (),
    ordered: Sequence[Sequence[str]] = (),
    prose: Sequence[str] = (),
    code: Sequence[str] = (),
    unmatchable: bool = False,
    children: int = 0,
) -> dict[str, Any]:
    """A section node dict as :meth:`MarkdownAspect.extract` produces it (display defaults to capitalised path)."""
    return {
        "source": source,
        "path": list(path),
        "display": list(display) if display is not None else [segment.capitalize() for segment in path],
        "level": len(path),
        "line": 0,
        "mode": mode,
        "rules": [r if isinstance(r, dict) else rule(r) for r in rules],
        "ordered_lists": [[rule(text) for text in seq] for seq in ordered],
        "prose": list(prose),
        "code": list(code),
        "children": children,
        "unmatchable": unmatchable,
    }


def files_record(
    *content: str, aliases: dict[str, str] | None = None, unsupported: Sequence[str] = ()
) -> dict[str, Any]:
    aliases = aliases or {}
    present = [*content, *aliases, *unsupported]
    roles = sorted({rel.rsplit("/", 1)[-1] for rel in (*content, *aliases.values(), *unsupported)})
    return {
        "present": present,
        "content": list(content),
        "aliases": aliases,
        "roles": roles,
        "unsupported": list(unsupported),
    }


def snapshot(
    make_snapshot: Callable[..., Snapshot],
    repo: str,
    nodes: Sequence[dict[str, Any]] = (),
    *,
    files: dict[str, Any] | None = None,
    title: dict[str, Any] | None = None,
    top_order: Sequence[str] | None = None,
    aspect_name: str = "agents",
) -> Snapshot:
    nodes = list(nodes)
    sources = list(dict.fromkeys(n["source"] for n in nodes)) or ["AGENTS.md"]
    if files is None:
        files = files_record(*sources)
    if not any(not n["path"] for n in nodes):
        nodes = [node((), source=sources[0]), *nodes]
    all_rules = list(
        dict.fromkeys(
            [r["text"] for n in nodes for r in n["rules"]]
            + [r["text"] for n in nodes for s in n["ordered_lists"] for r in s]
        )
    )
    if top_order is None:
        top_order = [n["path"][0] for n in nodes if len(n["path"]) == 1]
    data = {
        "files": files,
        "front_matter": {},
        "title": title,
        "sections": nodes,
        "all_rules": all_rules,
        "top_order": list(top_order),
    }
    return make_snapshot(aspect_name, repo, data)


# ----- options -----------------------------------------------------------------------------------------------------


class TestOptions:
    def test_defaults(self) -> None:
        opts = MarkdownOptions(files=["AGENTS.md"])
        assert (opts.merge, opts.default_mode, opts.similarity_threshold, opts.rule_match_cutoff) == (
            False,
            "full",
            0.6,
            0.75,
        )
        assert (opts.paragraph_rules, opts.compare_code, opts.heading_order, opts.title, opts.max_chars) == (
            False,
            False,
            False,
            True,
            20000,
        )
        assert opts.ignore_sections == []
        assert opts.sections == {}
        assert opts.heading_aliases == {}

    @pytest.mark.parametrize(
        ("options", "match"),
        [
            ({"default_mode": "fuzzy"}, r"default_mode: expected one of"),
            ({"sections": {"install": "exact"}}, r"sections\.install: expected one of"),
            ({"similarity_threshold": 1.5}, r"similarity_threshold: expected a number between 0 and 1"),
            ({"rule_match_cutoff": -0.1}, r"rule_match_cutoff: expected a number between 0 and 1"),
            ({"ignore_sections": ["skills("]}, r"ignore_sections: invalid regex 'skills\('"),
            ({"max_chars": 0}, r"max_chars: expected a positive integer"),
            ({"files": []}, r"files: at least one candidate file"),
        ],
    )
    def test_validation_errors(self, options: dict[str, Any], match: str) -> None:
        options.setdefault("files", ["AGENTS.md"])
        with pytest.raises(OptionsError, match=match):
            MarkdownOptions(**options)

    def test_parse_options_requires_files(self) -> None:
        with pytest.raises(OptionsError, match=r"missing required option\(s\): files"):
            parse_options(MarkdownOptions, {}, where="[aspects.x]")

    def test_default_config_tables_parse(self) -> None:
        agents = default_options("agent_instructions")
        assert agents.merge is True
        assert agents.default_mode == "full"
        assert agents.severity == {"missing.file": "error"}
        readme = default_options("readme")
        assert readme.default_mode == "presence"
        assert readme.heading_order is True
        assert readme.sections["contributing"] == "identical"
        assert "quick start" in readme.heading_aliases["installation"]

    def test_aspect_class_attributes(self) -> None:
        a = aspect("readme", files=["README.md"])
        assert a.type_name == "markdown"
        assert a.options_cls is MarkdownOptions
        assert a.schema_version == 1
        assert a.description
        assert a.name == "readme"


# ----- extraction --------------------------------------------------------------------------------------------------


class TestExtract:
    def test_small_agents_file(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"AGENTS.md": SMALL_AGENTS}, name="demo")
        snap = aspect().run_extract(ctx)
        assert snap.sources == ("AGENTS.md",)
        assert snap.data == {
            "files": {
                "present": ["AGENTS.md"],
                "content": ["AGENTS.md"],
                "aliases": {},
                "roles": ["AGENTS.md"],
                "unsupported": [],
            },
            "front_matter": {},
            "title": {"text": "instructions for agents", "display": "Instructions for agents", "source": "AGENTS.md"},
            "sections": [
                {
                    "source": "AGENTS.md",
                    "path": [],
                    "display": [],
                    "level": 0,
                    "line": 0,
                    "mode": "full",
                    "rules": [],
                    "ordered_lists": [],
                    "prose": ["read the readme first"],
                    "code": [],
                    "children": 2,
                    "unmatchable": False,
                },
                {
                    "source": "AGENTS.md",
                    "path": ["development"],
                    "display": ["Development"],
                    "level": 1,
                    "line": 5,
                    "mode": "full",
                    "rules": [
                        {
                            "text": "run pytest -q before committing",
                            "raw": "- Run `pytest -q` before committing.",
                            "line": 7,
                            "parent": None,
                        },
                        {
                            "text": "use ruff for linting",
                            "raw": "- Use **ruff** for linting.",
                            "line": 8,
                            "parent": None,
                        },
                    ],
                    "ordered_lists": [],
                    "prose": [],
                    "code": [],
                    "children": 1,
                    "unmatchable": False,
                },
                {
                    "source": "AGENTS.md",
                    "path": ["development", "testing"],
                    "display": ["Development", "Testing"],
                    "level": 2,
                    "line": 10,
                    "mode": "full",
                    "rules": [],
                    "ordered_lists": [
                        [
                            {"text": "write the test", "raw": "1. Write the test.", "line": 12, "parent": None},
                            {"text": "run it", "raw": "2. Run it.", "line": 13, "parent": None},
                        ]
                    ],
                    "prose": [],
                    "code": [],
                    "children": 0,
                    "unmatchable": False,
                },
                {
                    "source": "AGENTS.md",
                    "path": ["style"],
                    "display": ["Style"],
                    "level": 1,
                    "line": 15,
                    "mode": "full",
                    "rules": [],
                    "ordered_lists": [],
                    "prose": ["keep lines under 120 chars"],
                    "code": [],
                    "children": 0,
                    "unmatchable": False,
                },
            ],
            "all_rules": ["run pytest -q before committing", "use ruff for linting", "write the test", "run it"],
            "top_order": ["development", "style"],
        }

    def test_identity_substitution_and_where(self, make_repo: MakeRepo) -> None:
        text = "# NeuroConv\n\n## About NeuroConv\n\n- Install neuroconv with pip.\n\nNeuroConv converts data.\n"
        ctx = make_repo({"AGENTS.md": text}, name="neuroconv", identity=NEUROCONV)
        data = aspect().extract(ctx)
        assert data["title"] == {"text": "{{name}}", "display": "{{name}}", "source": "AGENTS.md"}
        section = data["sections"][1]
        assert section["path"] == ["about {{name}}"]
        assert section["display"] == ["About {{name}}"]
        assert section["rules"][0]["text"] == "install {{name}} with pip"
        assert section["rules"][0]["raw"] == "- Install {{name}} with pip."
        assert section["prose"] == ["{{name}} converts data"]

    def test_claude_import_alias(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"AGENTS.md": SMALL_AGENTS, "CLAUDE.md": "@AGENTS.md\n"})
        data = aspect().extract(ctx)
        assert data["files"] == {
            "present": ["AGENTS.md", "CLAUDE.md"],
            "content": ["AGENTS.md"],
            "aliases": {"CLAUDE.md": "AGENTS.md"},
            "roles": ["AGENTS.md"],
            "unsupported": [],
        }

    def test_symlink_alias(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"AGENTS.md": SMALL_AGENTS})
        ctx.repo.path("CLAUDE.md").symlink_to("AGENTS.md")
        data = aspect().extract(ctx)
        assert data["files"]["aliases"] == {"CLAUDE.md": "AGENTS.md"}
        assert data["files"]["content"] == ["AGENTS.md"]
        assert data["files"]["roles"] == ["AGENTS.md"]
        assert ctx.sources == {"AGENTS.md"}

    def test_duplicate_content_is_an_alias(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"AGENTS.md": SMALL_AGENTS, "CLAUDE.md": SMALL_AGENTS + "\n\n"})
        data = aspect(merge=True).extract(ctx)
        assert data["files"]["aliases"] == {"CLAUDE.md": "AGENTS.md"}
        assert data["files"]["content"] == ["AGENTS.md"]

    def test_merge_true_and_false(self, make_repo: MakeRepo) -> None:
        files = {"AGENTS.md": SMALL_AGENTS, ".github/copilot-instructions.md": "## Style\n\nUse black.\n"}
        merged = aspect(merge=True).run_extract(make_repo(files, name="a"))
        assert merged.data["files"]["content"] == ["AGENTS.md", ".github/copilot-instructions.md"]
        assert merged.data["files"]["roles"] == ["AGENTS.md", "copilot-instructions.md"]
        assert [(n["source"], n["path"]) for n in merged.data["sections"] if n["source"] != "AGENTS.md"] == [
            (".github/copilot-instructions.md", []),
            (".github/copilot-instructions.md", ["style"]),
        ]
        assert merged.data["top_order"] == ["development", "style", "style"]
        assert merged.ignored == ()

        single = aspect(merge=False).run_extract(make_repo(files, name="b"))
        assert single.data["files"]["content"] == ["AGENTS.md"]
        assert single.data["files"]["roles"] == ["AGENTS.md", "copilot-instructions.md"]
        assert {n["source"] for n in single.data["sections"]} == {"AGENTS.md"}
        assert single.ignored == (".github/copilot-instructions.md (merge = false: only AGENTS.md is compared)",)

    def test_ignore_sections_drops_subtree(self, make_repo: MakeRepo) -> None:
        text = "## Skills\n\n- Use apm.\n\n### Installed\n\n- release\n\n## Other\n\n- keep\n"
        snap = aspect(ignore_sections=["skills?"]).run_extract(make_repo({"AGENTS.md": text}))
        assert [n["path"] for n in snap.data["sections"]] == [[], ["other"]]
        assert snap.data["all_rules"] == ["keep"]
        assert snap.ignored == ("AGENTS.md#Skills (ignore_sections: skills?)",)

    def test_ignore_sections_matches_joined_path(self, make_repo: MakeRepo) -> None:
        text = "## Development\n\n### Skills\n\n- x\n\n### Testing\n\n- y\n"
        snap = aspect(ignore_sections=["development > skills"]).run_extract(make_repo({"AGENTS.md": text}))
        assert [n["path"] for n in snap.data["sections"]] == [[], ["development"], ["development", "testing"]]
        assert snap.ignored == ("AGENTS.md#Development > Skills (ignore_sections: development > skills)",)

    def test_heading_aliases(self, make_repo: MakeRepo) -> None:
        opts = {"files": ["README.md"], "heading_aliases": {"installation": ["getting started"]}}
        data = aspect("readme", **opts).extract(make_repo({"README.md": "## Getting started\n\ntext\n"}))
        assert data["sections"][1]["path"] == ["installation"]
        assert data["sections"][1]["display"] == ["Getting started"]

    def test_local_md_never_included(self, make_repo: MakeRepo) -> None:
        files = {"AGENTS.md": SMALL_AGENTS, "NOTES.local.md": "# private\n", "README.md": "# r\n"}
        data = aspect(files=["*.md"], merge=True).extract(make_repo(files))
        assert data["files"]["present"] == ["AGENTS.md", "README.md"]

    def test_glob_inside_github_directory(self, make_repo: MakeRepo) -> None:
        files = {".github/copilot-instructions.md": "## A\n", ".github/other.md": "## B\n"}
        data = aspect(files=[".github/*.md"], merge=True).extract(make_repo(files))
        assert data["files"]["present"] == [".github/copilot-instructions.md", ".github/other.md"]

    def test_no_title_when_first_heading_is_not_h1(self, make_repo: MakeRepo) -> None:
        data = aspect().extract(make_repo({"AGENTS.md": "intro\n\n## A\n\n### B\n\n## C\n"}))
        assert data["title"] is None
        assert [(n["path"], n["level"]) for n in data["sections"]] == [([], 0), (["a"], 1), (["a", "b"], 2), (["c"], 1)]
        assert data["sections"][0]["prose"] == ["intro"]

    def test_levels_rebased_and_multiple_h1_keep_no_title(self, make_repo: MakeRepo) -> None:
        data = aspect().extract(make_repo({"AGENTS.md": "### Deep\n\n#### Deeper\n"}))
        assert [(n["path"], n["level"]) for n in data["sections"]] == [([], 0), (["deep"], 1), (["deep", "deeper"], 2)]
        data = aspect().extract(make_repo({"AGENTS.md": "# One\n\n# Two\n"}, name="two-h1"))
        assert data["title"] is None
        assert data["top_order"] == ["one", "two"]

    def test_title_body_joins_preamble(self, make_repo: MakeRepo) -> None:
        data = aspect().extract(make_repo({"AGENTS.md": "before\n\n# Title\n\nafter\n\n- rule\n\n## A\n"}))
        assert data["sections"][0]["prose"] == ["before", "after"]
        assert data["sections"][0]["rules"][0]["text"] == "rule"

    def test_numbered_emoji_and_markup_headings(self, make_repo: MakeRepo) -> None:
        text = "## 1. Environment :rocket:\n\n## Step 2: **Commit** hygiene\n\n## \U0001f389\n"
        data = aspect().extract(make_repo({"AGENTS.md": text}))
        sections = data["sections"][1:]
        assert [n["path"] for n in sections] == [["environment"], ["commit hygiene"], ["\U0001f389"]]
        assert [n["display"] for n in sections] == [
            ["1. Environment :rocket:"],
            ["Step 2: Commit hygiene"],
            ["\U0001f389"],
        ]
        assert [n["unmatchable"] for n in sections] == [False, False, True]

    def test_duplicate_paths_get_ordinal_suffixes(self, make_repo: MakeRepo) -> None:
        data = aspect().extract(make_repo({"AGENTS.md": "## Testing\n\n- a\n\n## Testing\n\n- b\n\n### Sub\n"}))
        assert [n["path"] for n in data["sections"][1:]] == [["testing"], ["testing@2"], ["testing@2", "sub"]]
        assert data["sections"][2]["display"] == ["Testing@2"]

    def test_nested_items_are_rules_with_parent(self, make_repo: MakeRepo) -> None:
        text = "## A\n\n- Outer rule\n  - Inner rule\n  1. First step\n  2. Second step\n"
        data = aspect().extract(make_repo({"AGENTS.md": text}))
        section = data["sections"][1]
        assert [(r["text"], r["parent"]) for r in section["rules"]] == [
            ("outer rule", None),
            ("inner rule", "outer rule"),
        ]
        assert [[r["text"] for r in seq] for seq in section["ordered_lists"]] == [["first step", "second step"]]
        assert section["ordered_lists"][0][0]["parent"] == "outer rule"

    def test_section_modes(self, make_repo: MakeRepo) -> None:
        text = "## Installation\n\n### From source\n\n## Usage\n\n## Contributing\n"
        opts = {
            "files": ["README.md"],
            "default_mode": "presence",
            "sections": {"installation": "similar", "installation > *": "rules", "Contributing": "identical"},
        }
        data = aspect("readme", **opts).extract(make_repo({"README.md": text}))
        assert [(n["path"], n["mode"]) for n in data["sections"]] == [
            ([], "presence"),
            (["installation"], "similar"),
            (["installation", "from source"], "rules"),
            (["usage"], "presence"),
            (["contributing"], "identical"),
        ]

    def test_compare_code_and_paragraph_rules(self, make_repo: MakeRepo) -> None:
        text = "## Install\n\nRun the installer.\n\n```shell\n$ pip   install demo\n>>> import demo\n```\n"
        ctx = make_repo({"AGENTS.md": text}, name="demo")
        plain = aspect().extract(ctx)["sections"][1]
        assert plain["code"] == []
        assert plain["prose"] == ["run the installer"]
        assert plain["rules"] == []
        coded = aspect(compare_code=True, paragraph_rules=True).extract(ctx)["sections"][1]
        assert coded["code"] == ["pip install {{name}}", "import {{name}}"]
        assert coded["prose"] == []
        assert coded["rules"] == [{"text": "run the installer", "raw": "Run the installer.", "line": 1, "parent": None}]

    def test_max_chars_truncates_prose(self, make_repo: MakeRepo) -> None:
        text = "## A\n\nfirst paragraph\n\nsecond paragraph\n\nthird paragraph\n"
        data = aspect(max_chars=32).extract(make_repo({"AGENTS.md": text}))
        assert data["sections"][1]["prose"] == ["first paragraph", "second paragraph", "(truncated)"]

    def test_front_matter_is_kept_but_not_a_section(self, make_repo: MakeRepo) -> None:
        data = aspect().extract(make_repo({"AGENTS.md": "---\ntitle: x\n---\n## A\n"}))
        assert data["front_matter"] == {"AGENTS.md": "title: x"}
        assert [n["path"] for n in data["sections"]] == [[], ["a"]]

    def test_rst_candidate_is_unsupported(self, make_repo: MakeRepo) -> None:
        data = aspect("readme", files=["README.md", "README.rst"]).extract(make_repo({"README.rst": "Title\n=====\n"}))
        assert data["files"] == {
            "present": ["README.rst"],
            "content": [],
            "aliases": {},
            "roles": ["README.rst"],
            "unsupported": ["README.rst"],
        }
        assert data["sections"] == []
        assert data["title"] is None

    def test_absent_files(self, make_repo: MakeRepo) -> None:
        snap = aspect().run_extract(make_repo({"README.md": "x"}))
        assert snap.data["files"]["present"] == []
        assert snap.data["sections"] == []
        assert snap.sources == ()


# ----- comparison: files and titles -----------------------------------------------------------------------------


class TestCompareFiles:
    def test_missing_file_is_an_error_and_stops(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], rules=["x"])])
        other = snapshot(make_snapshot, "sat", [], files=files_record())
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, "AGENTS.md")}
        assert findings[0].severity is Severity.ERROR

    def test_extra_file_is_upstream_and_stops(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [], files=files_record())
        other = snapshot(make_snapshot, "sat", [node(["a"], rules=["x"])])
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.EXTRA, Subject.FILE, Direction.UPSTREAM, "AGENTS.md")}
        assert findings[0].severity is Severity.INFO

    def test_nothing_when_neither_has_files(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [], files=files_record())
        other = snapshot(make_snapshot, "sat", [], files=files_record())
        assert aspect().compare(main, other) == []

    def test_naming_differs(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], prose=["same"])])
        other = snapshot(make_snapshot, "sat", [node(["a"], source="CLAUDE.md", prose=["same"])])
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.DIFFERS, Subject.NAME, Direction.NONE, "AGENTS.md")}
        assert findings[0].severity is Severity.INFO

    def test_alias_role_counts_as_the_same_naming(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot, "main", [node(["a"])], files=files_record("AGENTS.md", aliases={"CLAUDE.md": "AGENTS.md"})
        )
        other = snapshot(make_snapshot, "sat", [node(["a"])])
        assert aspect().compare(main, other) == []

    def test_role_missing_is_a_warning_and_silences_its_sections(self, make_snapshot: Callable[..., Snapshot]) -> None:
        copilot = ".github/copilot-instructions.md"
        main = snapshot(
            make_snapshot,
            "main",
            [node(["a"], rules=["x"]), node([], source=copilot), node(["b"], source=copilot, rules=["y"])],
            files=files_record("AGENTS.md", copilot),
        )
        other = snapshot(make_snapshot, "sat", [node(["a"], rules=["x"])])
        findings = aspect(merge=True).compare(main, other)
        assert keys(findings) == {(Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, copilot)}
        assert findings[0].severity is Severity.WARNING

    def test_extra_role_silences_its_sections(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"])])
        other = snapshot(
            make_snapshot,
            "sat",
            [node(["a"]), node([], source="CLAUDE.md"), node(["b"], source="CLAUDE.md", rules=["y"])],
            files=files_record("AGENTS.md", "CLAUDE.md"),
        )
        assert keys(aspect(merge=True).compare(main, other)) == {
            (Kind.EXTRA, Subject.FILE, Direction.UPSTREAM, "CLAUDE.md")
        }

    def test_unparseable_rst(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], source="README.md")], aspect_name="readme")
        other = snapshot(make_snapshot, "sat", [], files=files_record(unsupported=["README.rst"]), aspect_name="readme")
        findings = aspect("readme", files=["README.md", "README.rst"]).compare(main, other)
        assert keys(findings) == {
            (Kind.UNPARSEABLE, Subject.FILE, Direction.NONE, "README.rst"),
            (Kind.MISSING, Subject.FILE, Direction.DOWNSTREAM, "README.md"),
        }
        rst = next(f for f in findings if f.kind is Kind.UNPARSEABLE)
        assert rst.severity is Severity.INFO
        assert "reStructuredText" in rst.message

    def test_title_differs(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot, "main", [node(["a"])], title={"text": "one", "display": "One", "source": "AGENTS.md"}
        )
        other = snapshot(
            make_snapshot, "sat", [node(["a"])], title={"text": "two", "display": "Two", "source": "AGENTS.md"}
        )
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.DIFFERS, Subject.TITLE, Direction.NONE, "AGENTS.md#(title)")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].detail == "main: One\nrepo: Two"
        assert aspect(title=False).compare(main, other) == []
        same = snapshot(
            make_snapshot, "sat", [node(["a"])], title={"text": "one", "display": "ONE", "source": "AGENTS.md"}
        )
        assert aspect().compare(main, same) == []


# ----- comparison: sections ---------------------------------------------------------------------------------------


class TestCompareSections:
    def test_missing_section_reports_highest_node_only(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot,
            "main",
            [node(["dev"], rules=["a"], children=1), node(["dev", "testing"], rules=["b", "c"]), node(["style"])],
        )
        other = snapshot(make_snapshot, "sat", [node(["style"])])
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, "AGENTS.md#Dev")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].detail == "1 subsections, 3 rules, 0 prose lines"
        assert findings[0].content_key == "dev"

    def test_extra_section_reports_highest_node_only(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["style"])])
        other = snapshot(
            make_snapshot, "sat", [node(["style"]), node(["release"], rules=["tag"]), node(["release", "pypi"])]
        )
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "AGENTS.md#Release")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].content_key == "release"

    def test_moved_by_path_suffix_and_leaf(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot, "main", [node(["install"]), node(["install", "from source"])], aspect_name="readme"
        )
        other = snapshot(
            make_snapshot,
            "sat",
            [node(["setup"]), node(["setup", "install"]), node(["setup", "install", "from source"])],
            aspect_name="readme",
        )
        findings = aspect("readme", files=["README.md"]).compare(main, other)
        assert keys(findings) == {
            (Kind.MOVED, Subject.SECTION, Direction.DOWNSTREAM, "AGENTS.md#Install"),
            (Kind.MOVED, Subject.SECTION, Direction.DOWNSTREAM, "AGENTS.md#Install > From source"),
            (Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "AGENTS.md#Setup"),
        }
        moved = next(f for f in findings if f.locator == "AGENTS.md#Install > From source")
        assert moved.severity is Severity.INFO
        assert moved.detail == "main: Install > From source\nrepo: Setup > Install > From source"

    def test_leaf_match_is_gated_on_body_similarity(self, make_snapshot: Callable[..., Snapshot]) -> None:
        prose = ["call foo() to start the conversion and wait for it to finish"]
        main = snapshot(make_snapshot, "main", [node(["usage"]), node(["usage", "example"], prose=prose)])
        similar = snapshot(make_snapshot, "sat", [node(["usage"]), node(["example"], prose=prose)])
        assert keys(aspect().compare(main, similar)) == {
            (Kind.MOVED, Subject.SECTION, Direction.DOWNSTREAM, "AGENTS.md#Usage > Example")
        }
        unrelated = snapshot(
            make_snapshot, "sat", [node(["usage"]), node(["example"], prose=["a totally different body about badges"])]
        )
        assert keys(aspect().compare(main, unrelated)) == {
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, "AGENTS.md#Usage > Example"),
            (Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "AGENTS.md#Example"),
        }

    def test_unmatchable_sections_only_match_exactly(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot,
            "main",
            [node(["a"]), node(["a", "\U0001f389"], display=["A", "\U0001f389"], unmatchable=True)],
        )
        other = snapshot(
            make_snapshot, "sat", [node(["a"]), node(["\U0001f389"], display=["\U0001f389"], unmatchable=True)]
        )
        assert keys(aspect().compare(main, other)) == {
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, "AGENTS.md#A > \U0001f389"),
            (Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "AGENTS.md#\U0001f389"),
        }
        exact = snapshot(
            make_snapshot,
            "sat",
            [node(["a"]), node(["a", "\U0001f389"], display=["A", "\U0001f389"], unmatchable=True)],
        )
        assert aspect().compare(main, exact) == []

    def test_cross_source_path_match_is_not_moved(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["testing"], rules=["x"])])
        other = snapshot(make_snapshot, "sat", [node(["testing"], source="CLAUDE.md", rules=["x"])])
        assert keys(aspect(merge=True).compare(main, other)) == {
            (Kind.DIFFERS, Subject.NAME, Direction.NONE, "AGENTS.md")
        }

    def test_heading_order(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot,
            "main",
            [node(["a"], source="README.md"), node(["b"], source="README.md"), node(["c"], source="README.md")],
        )
        other = snapshot(
            make_snapshot,
            "sat",
            [node(["b"], source="README.md"), node(["a"], source="README.md"), node(["c"], source="README.md")],
        )
        findings = aspect("readme", files=["README.md"], heading_order=True, default_mode="presence").compare(
            main, other
        )
        assert keys(findings) == {(Kind.REORDERED, Subject.SECTION, Direction.DOWNSTREAM, "README.md#(order)")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].detail == "main: A > B > C\nrepo: B > A > C"
        assert (
            aspect("readme", files=["README.md"], heading_order=False, default_mode="presence").compare(main, other)
            == []
        )
        added = snapshot(
            make_snapshot,
            "sat",
            [node(["a"], source="README.md"), node(["x"], source="README.md"), node(["b"], source="README.md")],
        )
        assert not [
            f
            for f in aspect("readme", files=["README.md"], heading_order=True).compare(main, added)
            if f.kind is Kind.REORDERED
        ]


# ----- comparison: rules and prose --------------------------------------------------------------------------------


class TestCompareRules:
    def test_missing_extra_differs_and_moved(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot,
            "main",
            [
                node(
                    ["dev"],
                    rules=["always run pytest before committing", "use ruff for linting", "keep the suite fast"],
                ),
                node(["notes"]),
            ],
        )
        other = snapshot(
            make_snapshot,
            "sat",
            [
                node(["dev"], rules=["always run pytest before committing changes", "document public functions"]),
                node(["notes"], rules=["keep the suite fast"]),
            ],
        )
        findings = aspect().compare(main, other)
        assert keys(findings) == {
            (Kind.DIFFERS, Subject.RULE, Direction.NONE, "AGENTS.md#Dev"),
            (Kind.MISSING, Subject.RULE, Direction.DOWNSTREAM, "AGENTS.md#Dev"),
            (Kind.MOVED, Subject.RULE, Direction.DOWNSTREAM, "AGENTS.md#Dev"),
            (Kind.EXTRA, Subject.RULE, Direction.UPSTREAM, "AGENTS.md#Dev"),
        }
        by_kind = {f.kind: f for f in findings}
        assert by_kind[Kind.DIFFERS].content_key == "always run pytest before committing"
        assert by_kind[Kind.DIFFERS].severity is Severity.WARNING
        assert by_kind[Kind.DIFFERS].detail_kind == "text"
        assert by_kind[Kind.MISSING].content_key == "use ruff for linting"
        assert by_kind[Kind.MOVED].content_key == "keep the suite fast"
        assert by_kind[Kind.MOVED].severity is Severity.INFO
        assert by_kind[Kind.EXTRA].content_key == "document public functions"

    def test_rule_match_cutoff_option(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["dev"], rules=["always run pytest before committing"])])
        other = snapshot(make_snapshot, "sat", [node(["dev"], rules=["always run pytest before committing changes"])])
        strict = aspect(rule_match_cutoff=0.99).compare(main, other)
        assert {f.kind for f in strict} == {Kind.MISSING, Kind.EXTRA}

    def test_ordered_lists(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["steps"], ordered=[["write the test", "run it", "commit"]])])
        reordered = snapshot(make_snapshot, "sat", [node(["steps"], ordered=[["write the test", "commit", "run it"]])])
        findings = aspect().compare(main, reordered)
        assert keys(findings) == {(Kind.REORDERED, Subject.RULE, Direction.DOWNSTREAM, "AGENTS.md#Steps")}
        assert findings[0].severity is Severity.INFO
        assert findings[0].detail_kind == "text"
        shorter = snapshot(make_snapshot, "sat", [node(["steps"], ordered=[["write the test", "run it"]])])
        findings = aspect().compare(main, shorter)
        assert keys(findings) == {(Kind.MISSING, Subject.RULE, Direction.DOWNSTREAM, "AGENTS.md#Steps")}
        assert findings[0].content_key == "commit"
        none = snapshot(make_snapshot, "sat", [node(["steps"])])
        assert [f.content_key for f in aspect().compare(main, none)] == ["write the test", "run it", "commit"]

    def test_rules_mode_ignores_prose(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], mode="rules", rules=["x"], prose=["one two three"])])
        other = snapshot(make_snapshot, "sat", [node(["a"], mode="rules", rules=["x"], prose=["four five six"])])
        assert aspect().compare(main, other) == []


class TestCompareProse:
    LINES = [
        "the first paragraph talks about installing the package in a clean environment",
        "the second paragraph explains how to run the test suite locally",
    ]

    def test_insert_is_extra(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], prose=self.LINES)])
        other = snapshot(make_snapshot, "sat", [node(["a"], prose=[*self.LINES, "a brand new closing paragraph"])])
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.EXTRA, Subject.PROSE, Direction.UPSTREAM, "AGENTS.md#A")}
        assert findings[0].detail == "a brand new closing paragraph"
        assert findings[0].detail_kind == "list"

    def test_delete_is_missing(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], prose=self.LINES)])
        other = snapshot(make_snapshot, "sat", [node(["a"], prose=self.LINES[:1])])
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.MISSING, Subject.PROSE, Direction.DOWNSTREAM, "AGENTS.md#A")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].detail == self.LINES[1]

    def test_mixed_below_threshold_differs(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], prose=self.LINES)])
        other = snapshot(
            make_snapshot, "sat", [node(["a"], prose=["completely unrelated words here", "and more of them"])]
        )
        findings = aspect("agents").compare(main, other)
        assert keys(findings) == {(Kind.DIFFERS, Subject.PROSE, Direction.NONE, "AGENTS.md#A")}
        f = findings[0]
        assert f.severity is Severity.WARNING
        assert f.detail_kind == "diff"
        assert f.option == "aspects.agents.similarity_threshold=0.6"
        assert f.detail is not None
        assert f.detail.startswith("--- main/AGENTS.md#A\n+++ sat/AGENTS.md#A\n")
        assert f.message.startswith("prose similarity 0.00")

    def test_mixed_above_threshold_is_silent(self, make_snapshot: Callable[..., Snapshot]) -> None:
        shared = [
            f"line number {i} of a rather long shared paragraph that never changes between the repos" for i in range(6)
        ]
        main = snapshot(make_snapshot, "main", [node(["a"], prose=[*shared, "the final sentence says foo"])])
        other = snapshot(make_snapshot, "sat", [node(["a"], prose=[*shared, "the final sentence says bar"])])
        assert aspect().compare(main, other) == []
        assert keys(aspect(similarity_threshold=1.0).compare(main, other)) == {
            (Kind.DIFFERS, Subject.PROSE, Direction.NONE, "AGENTS.md#A")
        }

    def test_identical_mode(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(
            make_snapshot, "main", [node(["contributing"], mode="identical", prose=["open a pr"], rules=["be kind"])]
        )
        other = snapshot(
            make_snapshot,
            "sat",
            [node(["contributing"], mode="identical", prose=["open a pull request"], rules=["be kind"])],
        )
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.DIFFERS, Subject.PROSE, Direction.DOWNSTREAM, "AGENTS.md#Contributing")}
        assert findings[0].severity is Severity.WARNING
        assert findings[0].detail_kind == "diff"
        same = snapshot(
            make_snapshot, "sat", [node(["contributing"], mode="identical", prose=["open a pr"], rules=["be kind"])]
        )
        assert aspect().compare(main, same) == []

    def test_presence_mode(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], mode="presence", prose=["one"], rules=["x"])])
        other = snapshot(make_snapshot, "sat", [node(["a"], mode="presence", prose=["two"], rules=["y"])])
        assert aspect().compare(main, other) == []

    def test_similar_mode_includes_rules_in_the_body(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], mode="similar", rules=["run the tests before pushing"])])
        other = snapshot(
            make_snapshot, "sat", [node(["a"], mode="similar", rules=["run the tests before pushing", "and lint"])]
        )
        findings = aspect().compare(main, other)
        assert keys(findings) == {(Kind.EXTRA, Subject.PROSE, Direction.UPSTREAM, "AGENTS.md#A")}

    def test_code_compared_only_when_enabled(self, make_snapshot: Callable[..., Snapshot]) -> None:
        main = snapshot(make_snapshot, "main", [node(["a"], code=["pip install {{name}}"])])
        other = snapshot(make_snapshot, "sat", [node(["a"], code=["pip install {{name}}", "pip install extras"])])
        assert aspect().compare(main, other) == []
        assert keys(aspect(compare_code=True).compare(main, other)) == {
            (Kind.EXTRA, Subject.CODE, Direction.UPSTREAM, "AGENTS.md#A")
        }


# ----- self-check --------------------------------------------------------------------------------------------------


class TestSelfCheck:
    def test_drift_between_merged_files(self, make_snapshot: Callable[..., Snapshot]) -> None:
        snap = snapshot(
            make_snapshot,
            "main",
            [
                node(["testing"], rules=["run pytest"]),
                node([], source="CLAUDE.md"),
                node(["testing"], source="CLAUDE.md", rules=["run pytest", "and mypy"]),
            ],
            files=files_record("AGENTS.md", "CLAUDE.md"),
        )
        findings = aspect(merge=True).self_check(snap)
        assert keys(findings) == {(Kind.DIFFERS, Subject.SECTION, Direction.NONE, "AGENTS.md+CLAUDE.md#Testing")}
        assert findings[0].severity is Severity.WARNING
        assert "have drifted" in findings[0].message
        assert findings[0].detail_kind == "diff"

    def test_no_drift_when_bodies_agree(self, make_snapshot: Callable[..., Snapshot]) -> None:
        snap = snapshot(
            make_snapshot,
            "main",
            [node(["testing"], rules=["run pytest"]), node(["testing"], source="CLAUDE.md", rules=["run pytest"])],
            files=files_record("AGENTS.md", "CLAUDE.md"),
        )
        assert aspect(merge=True).self_check(snap) == []

    def test_duplicate_heading(self, make_snapshot: Callable[..., Snapshot]) -> None:
        snap = snapshot(make_snapshot, "main", [node(["testing"]), node(["testing@2"], display=["Testing@2"])])
        findings = aspect().self_check(snap)
        assert keys(findings) == {(Kind.DIFFERS, Subject.SECTION, Direction.NONE, "AGENTS.md#Testing@2")}
        assert findings[0].severity is Severity.INFO
        assert "duplicate heading" in findings[0].message

    def test_end_to_end_duplicate_from_extraction(self, make_repo: MakeRepo) -> None:
        a = aspect()
        snap = a.run_extract(make_repo({"AGENTS.md": "## Testing\n\n## Testing\n"}))
        assert keys(a.self_check(snap)) == {(Kind.DIFFERS, Subject.SECTION, Direction.NONE, "AGENTS.md#Testing@2")}


# ----- real fixtures -----------------------------------------------------------------------------------------------


class TestRealFixtures:
    def test_readme_neuroconv_vs_roiextractors(self, make_repo: MakeRepo, real_fixture: Callable[[str], str]) -> None:
        a = MarkdownAspect("readme", default_options("readme"))
        main = a.run_extract(
            make_repo(
                {"README.md": real_fixture("neuroconv__README.md")}, name="neuroconv", identity=NEUROCONV, is_main=True
            )
        )
        other = a.run_extract(
            make_repo(
                {"README.md": real_fixture("roiextractors__README.md")}, name="roiextractors", identity=ROIEXTRACTORS
            )
        )
        assert main.data["title"] is None
        assert other.data["title"]["text"] == "{{name}}"
        assert "README.md#Table of Contents (ignore_sections: table of contents)" in main.ignored
        assert main.data["top_order"] == ["about", "installation", "documentation", "citing {{name}}", "license"]
        assert other.data["top_order"] == ["about", "installation", "documentation", "funding", "license"]

        findings = a.compare(main, other)
        found = keys(findings)
        assert len(findings) <= 12, found
        differing_prose = {f.locator for f in findings if f.kind is Kind.DIFFERS and f.subject is Subject.PROSE}
        # License and Documentation are identical after identity substitution; Installation genuinely differs
        # (measured shingle Jaccard 0.05) and is the only prose drift.
        assert differing_prose <= {"README.md#Installation"}
        assert "README.md#License" not in {f.locator for f in findings}
        assert "README.md#Documentation" not in {f.locator for f in findings}
        assert (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, "README.md#Citing {{name}}") in found
        assert (Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "README.md#Funding") in found
        assert not [f for f in findings if f.kind is Kind.REORDERED]
        assert not [f for f in findings if f.subject is Subject.FILE]

    def test_copilot_instructions_vs_spikeinterface_agents(
        self, make_repo: MakeRepo, real_fixture: Callable[[str], str]
    ) -> None:
        a = MarkdownAspect("agent_instructions", default_options("agent_instructions"))
        copilot = ".github/copilot-instructions.md"
        main = a.run_extract(
            make_repo(
                {copilot: real_fixture("neuroconv__.github_copilot-instructions.md")},
                name="neuroconv",
                identity=NEUROCONV,
                is_main=True,
            )
        )
        other = a.run_extract(
            make_repo(
                {"AGENTS.md": real_fixture("spikeinterface__AGENTS.md")}, name="spikeinterface", identity=SPIKEINTERFACE
            )
        )
        assert main.data["title"] == {
            "text": "custom instructions for github copilot in the {{name}} repository",
            "display": "Custom instructions for GitHub Copilot in the {{name}} repository",
            "source": copilot,
        }
        assert other.data["title"]["text"] == "instructions for automated agents"
        assert [n["path"] for n in main.data["sections"]] == [
            [],
            ["environment awareness"],
            ["committing & pushing workflow"],
            ["documentation & typing"],
            ["commit hygiene"],
            ["ci / workflow changes"],
        ]
        assert [len(n["rules"]) for n in main.data["sections"]] == [0, 2, 4, 2, 1, 1]
        assert [len(n["ordered_lists"]) for n in main.data["sections"]] == [0, 0, 1, 0, 0, 0]
        assert [n["path"] for n in other.data["sections"]] == [[], ["general instructions"], ["testing"]]

        findings = a.compare(main, other)
        assert not [f for f in findings if f.subject is Subject.RULE]
        assert not [f for f in findings if f.subject is Subject.PROSE]
        assert keys(findings) == {
            (Kind.DIFFERS, Subject.NAME, Direction.NONE, copilot),
            (Kind.DIFFERS, Subject.TITLE, Direction.NONE, f"{copilot}#(title)"),
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, f"{copilot}#1 Environment awareness"),
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, f"{copilot}#2 Committing & pushing workflow"),
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, f"{copilot}#3 Documentation & typing"),
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, f"{copilot}#4 Commit hygiene"),
            (Kind.MISSING, Subject.SECTION, Direction.DOWNSTREAM, f"{copilot}#5 CI / workflow changes"),
            (Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "AGENTS.md#General instructions"),
            (Kind.EXTRA, Subject.SECTION, Direction.UPSTREAM, "AGENTS.md#Testing"),
        }
        assert a.self_check(main) == []
        assert a.self_check(other) == []
