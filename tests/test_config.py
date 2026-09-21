"""Tests for ``sistent.toml`` loading, default merging, discovery and resolved TOML output."""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sistent.aspects import BUILTIN, DEFAULT_ASPECT_NAMES
from sistent.aspects.base import Aspect
from sistent.config import (
    AspectSpec,
    Config,
    ConfigError,
    RepoSpec,
    default_aspect_tables,
    default_config_text,
    default_registry,
    dump_config,
    expand_url,
    find_config,
    load_config,
    loads_config,
    merge_aspect_tables,
)
from sistent.model import Finding, Severity, Snapshot
from sistent.options import BaseOptions
from sistent.registry import Registry
from sistent.repository import RepoContext, RepoHints

MakeConfig = Callable[..., Path]

# ----- permissive stand-ins for the built-in aspect types (their modules are written separately) -------------------


@dataclass(frozen=True, kw_only=True)
class MarkdownOptions(BaseOptions):
    files: list[str] = field(default_factory=list)
    merge: bool = False
    ignore_sections: list[str] = field(default_factory=list)
    default_mode: str = "full"
    similarity_threshold: float = 0.6
    heading_order: bool = False
    sections: dict[str, str] = field(default_factory=dict)
    heading_aliases: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class BadgesOptions(BaseOptions):
    file: str = "README.md"
    order: bool = True


@dataclass(frozen=True, kw_only=True)
class TreeOptions(BaseOptions):
    depth: int = 2
    file_depth: int = 1
    include: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class FilesOptions(BaseOptions):
    files: list[str] = field(default_factory=list)
    required: str = "from-main"
    identical: list[str] = field(default_factory=list)
    search_dirs: list[str] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class TomlOptions(BaseOptions):
    file: str = "pyproject.toml"
    keys: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class WorkflowsOptions(BaseOptions):
    dir: str = ".github/workflows"


@dataclass(frozen=True, kw_only=True)
class PrecommitOptions(BaseOptions):
    file: str = ".pre-commit-config.yaml"


class StubAspect(Aspect):
    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        return {}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        return []


class MarkdownStub(StubAspect):
    type_name = "markdown"
    options_cls = MarkdownOptions


class BadgesStub(StubAspect):
    type_name = "badges"
    options_cls = BadgesOptions


class TreeStub(StubAspect):
    type_name = "tree"
    options_cls = TreeOptions


class FilesStub(StubAspect):
    type_name = "files"
    options_cls = FilesOptions


class TomlStub(StubAspect):
    type_name = "toml"
    options_cls = TomlOptions


class WorkflowsStub(StubAspect):
    type_name = "workflows"
    options_cls = WorkflowsOptions


class PrecommitStub(StubAspect):
    type_name = "precommit"
    options_cls = PrecommitOptions


STUBS: dict[str, type[Aspect]] = {
    "markdown": MarkdownStub,
    "badges": BadgesStub,
    "tree": TreeStub,
    "files": FilesStub,
    "toml": TomlStub,
    "workflows": WorkflowsStub,
    "precommit": PrecommitStub,
}
assert set(STUBS) == set(BUILTIN)


@pytest.fixture
def registry() -> Registry:
    return Registry(STUBS)


MINIMAL = """
[sistent]
main = "neuroconv"
github = "catalystneuro"

[repos.neuroconv]
[repos.roiextractors]
[repos.spikeinterface]
url = "SpikeInterface/spikeinterface"
"""


def load(text: str, make_config: MakeConfig, registry: Registry, *, name: str = "sistent.toml") -> Config:
    return load_config(make_config(text, name=name), registry=registry)


# ----- minimal example and repos -----------------------------------------------------------------------------------


def test_minimal_example_resolves_urls_and_defaults(
    make_config: MakeConfig, registry: Registry, tmp_path: Path
) -> None:
    config = load(MINIMAL, make_config, registry)
    assert config.path == tmp_path / "sistent.toml"
    assert config.directory == tmp_path
    assert config.main == "neuroconv"
    assert config.github == "catalystneuro"
    assert list(config.repos) == ["neuroconv", "roiextractors", "spikeinterface"]
    assert config.repos["neuroconv"].url == "https://github.com/catalystneuro/neuroconv"
    assert config.repos["roiextractors"].url == "https://github.com/catalystneuro/roiextractors"
    assert config.repos["spikeinterface"].url == "https://github.com/SpikeInterface/spikeinterface"
    assert config.repos["neuroconv"].path is None
    assert config.main_spec is config.repos["neuroconv"]
    assert [spec.name for spec in config.satellites] == ["roiextractors", "spikeinterface"]
    # [sistent] defaults
    assert config.fail_on is Severity.ERROR
    assert config.jobs == 8
    assert config.git_timeout == 120
    assert config.defaults is True
    assert config.stale is True
    assert config.baseline is None
    assert config.cache_dir.is_absolute()
    # all eight defaults, in order
    assert config.aspect_names == list(DEFAULT_ASPECT_NAMES)
    assert all(spec.is_default for spec in config.aspects)
    assert config.aspect("readme").type == "markdown"
    assert config.aspect("readme").inherited == frozenset(config.aspect("readme").table)
    assert isinstance(config.aspect("readme").options, MarkdownOptions)
    assert config.aspect("pyproject").table["keys"]["build-system"] == "exact"
    assert config.aspect("layout").options.severity == {}


def test_repo_spec_fields_source_and_hints(make_config: MakeConfig, registry: Registry, tmp_path: Path) -> None:
    config = load(
        """
        [sistent]
        main = "a"
        [repos.a]
        path = "../a"
        tags = ["python", "public"]
        skip_aspects = ["pre_commit"]
        ignore = ["readme:README.md#Funding*"]
        aliases = ["NeuroConv"]
        vars = { org = "catalystneuro", branch = "main", product = "NWB" }
        substitute = false
        [repos.b]
        url = "org/b"
        rev = "v1"
        root = "packages/b"
        """,
        make_config,
        registry,
    )
    a = config.repos["a"]
    assert a.path == tmp_path / "../a"
    assert a.source == str(tmp_path / "../a")
    assert a.tags == ("python", "public")
    assert a.skip_aspects == ("pre_commit",)
    assert a.ignore == ("readme:README.md#Funding*",)
    assert a.aliases == ("NeuroConv",)
    assert a.vars == {"org": "catalystneuro", "branch": "main", "product": "NWB"}
    assert a.substitute is False
    assert a.hints == RepoHints(name="a", url=None, rev=None, aliases=("NeuroConv",), vars=a.vars)
    b = config.repos["b"]
    assert b.url == "https://github.com/org/b"
    assert b.source == "https://github.com/org/b"
    assert b.rev == "v1"
    assert b.root == "packages/b"
    assert b.hints == RepoHints(name="b", url="https://github.com/org/b", rev="v1")


def test_relative_path_resolves_against_config_directory(
    make_config: MakeConfig, registry: Registry, tmp_path: Path
) -> None:
    nested = tmp_path / "fleet" / "cfg"
    nested.mkdir(parents=True)
    config = load(
        '[sistent]\nmain = "a"\n[repos.a]\npath = "../../checkouts/a"\n[repos.b]\npath = "/abs/b"\n',
        make_config,
        registry,
        name="fleet/cfg/sistent.toml",
    )
    assert config.directory == nested
    assert config.repos["a"].path == nested / "../../checkouts/a"
    assert config.repos["a"].path is not None
    assert config.repos["a"].path.is_absolute()
    assert config.repos["b"].path == Path("/abs/b")


def test_path_expands_user_and_env(
    make_config: MakeConfig, registry: Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FLEET", "/srv/fleet")
    monkeypatch.setenv("HOME", "/home/someone")
    config = load(
        '[sistent]\nmain = "a"\nbaseline = "base.json"\n[repos.a]\npath = "$FLEET/a"\n[repos.b]\npath = "~/b"\n',
        make_config,
        registry,
    )
    assert config.repos["a"].path == Path("/srv/fleet/a")
    assert config.repos["b"].path == Path("/home/someone/b")
    assert config.baseline == config.directory / "base.json"


def test_url_shorthand_expansion() -> None:
    assert expand_url("org/repo") == "https://github.com/org/repo"
    assert expand_url("Org-1/repo.name_x") == "https://github.com/Org-1/repo.name_x"
    assert expand_url("https://gitlab.com/org/repo") == "https://gitlab.com/org/repo"
    assert expand_url("git@github.com:org/repo.git") == "git@github.com:org/repo.git"
    assert expand_url("./local/path") == "./local/path"
    assert expand_url("../local/path") == "../local/path"
    assert expand_url("file:///tmp/repo.git") == "file:///tmp/repo.git"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            '[sistent]\nmain = "a"\n[repos.a]\npath = "x"\nurl = "o/r"\n',
            r"\[repos\.a\]: give either 'path' or 'url', not both",
        ),
        ('[sistent]\nmain = "a"\n[repos.a]\n', r"\[repos\.a\]: needs 'path' or 'url' \(or set \[sistent\]\.github"),
        (
            '[sistent]\nmain = "a"\n[repos.a]\npath = "x"\nrev = "main"\n',
            r"\[repos\.a\]\.rev: only applies to 'url' repos",
        ),
        (
            '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/r"\nroot = "/abs"\n',
            r"\[repos\.a\]\.root: must be a sub-directory",
        ),
        ('[sistent]\nmain = "a"\n[repos.a]\nurl = ""\n', r"\[repos\.a\]\.url: must not be empty"),
        (
            '[sistent]\nmain = "a"\n[repos.a]\npth = "x"\n',
            r"\[repos\.a\]\.pth: unknown key \(did you mean 'path'\?\)\. Valid keys: aliases, ignore, path, rev, root, skip_aspects, substitute, tags, url, vars",
        ),
        (
            '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/r"\ntags = "python"\n',
            r"\[repos\.a\]\.tags: expected list\[str\], got str \('python'\)",
        ),
        (
            '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/r"\nignore = [1]\n',
            r"\[repos\.a\]\.ignore: expected list\[str\]",
        ),
        (
            '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/r"\nvars = { org = 1 }\n',
            r"\[repos\.a\]\.vars: expected dict\[str, str\]",
        ),
        ('[sistent]\nmain = "a"\n[repos]\na = "not a table"\n', r"\[repos\.a\]: expected a table, got str"),
    ],
)
def test_repo_errors(text: str, message: str, make_config: MakeConfig, registry: Registry) -> None:
    with pytest.raises(ConfigError, match=message):
        load(text, make_config, registry)


# ----- [sistent] ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[sistent]\ngithub = "o"\n[repos.a]\n', r"\[sistent\]\.main: required"),
        (
            '[sistent]\nmain = "b"\n[repos.a]\nurl = "o/a"\n[repos.bb]\nurl = "o/bb"\n',
            r"\[sistent\]\.main: 'b' is not a configured repo \(did you mean 'bb'\?\)\. Configured repos: a, bb",
        ),
        ('[sistent]\nmain = "a"\n', r"no repositories configured"),
        (
            '[sistent]\nmain = "a"\njobz = 2\n[repos.a]\nurl = "o/a"\n',
            r"\[sistent\]\.jobz: unknown key \(did you mean 'jobs'\?\)\. Valid keys: baseline, cache_dir, defaults, fail_on, git_timeout, github, jobs, main, stale",
        ),
        (
            '[sistent]\nmain = "a"\njobs = "8"\n[repos.a]\nurl = "o/a"\n',
            r"\[sistent\]\.jobs: expected int, got str \('8'\)",
        ),
        ('[sistent]\nmain = "a"\njobs = 0\n[repos.a]\nurl = "o/a"\n', r"\[sistent\]\.jobs: must be a positive integer"),
        (
            '[sistent]\nmain = "a"\ndefaults = "yes"\n[repos.a]\nurl = "o/a"\n',
            r"\[sistent\]\.defaults: expected bool, got str",
        ),
        (
            '[sistent]\nmain = "a"\nfail_on = "fatal"\n[repos.a]\nurl = "o/a"\n',
            r"\[sistent\]\.fail_on: expected one of info, warning, error, never; got 'fatal'",
        ),
        (
            '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/a"\n[extra]\nx = 1\n',
            r"\[extra\]: unknown top-level table\. Valid tables: sistent, repos, aspects",
        ),
        (
            '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/a"\n[repo.b]\nurl = "o/b"\n',
            r"\[repo\]: unknown top-level table \(did you mean 'repos'\?\)",
        ),
        ('[repos.a]\nurl = "o/a"\n', r"\[sistent\]: table is required"),
        ('sistent = 1\n[repos.a]\nurl = "o/a"\n', r"\[sistent\]: expected a table, got int"),
        ("[sistent\nmain = 1\n", r"invalid TOML"),
    ],
)
def test_sistent_table_errors(text: str, message: str, make_config: MakeConfig, registry: Registry) -> None:
    with pytest.raises(ConfigError, match=message):
        load(text, make_config, registry)


def test_sistent_values(make_config: MakeConfig, registry: Registry, tmp_path: Path) -> None:
    config = load(
        """
        [sistent]
        main = "a"
        cache_dir = "cache"
        fail_on = "warning"
        git_timeout = 5
        jobs = 2
        defaults = false
        stale = false
        baseline = "/tmp/base.json"
        [repos.a]
        url = "o/a"
        """,
        make_config,
        registry,
    )
    assert config.cache_dir == tmp_path / "cache"
    assert config.fail_on is Severity.WARNING
    assert config.git_timeout == 5
    assert config.jobs == 2
    assert config.defaults is False
    assert config.stale is False
    assert config.baseline == Path("/tmp/base.json")
    assert config.aspects == ()


def test_fail_on_never_is_none(make_config: MakeConfig, registry: Registry) -> None:
    config = load('[sistent]\nmain = "a"\nfail_on = "never"\n[repos.a]\nurl = "o/a"\n', make_config, registry)
    assert config.fail_on is None


def test_cache_dir_default_and_expansion(
    make_config: MakeConfig, registry: Registry, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = '[sistent]\nmain = "a"\n[repos.a]\nurl = "o/a"\n'
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert load(text, make_config, registry).cache_dir == tmp_path / "home" / ".cache" / "sistent"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert load(text, make_config, registry).cache_dir == tmp_path / "xdg" / "sistent"
    monkeypatch.setenv("CACHES", str(tmp_path / "envcache"))
    config = load('[sistent]\nmain = "a"\ncache_dir = "$CACHES/s"\n[repos.a]\nurl = "o/a"\n', make_config, registry)
    assert config.cache_dir == tmp_path / "envcache" / "s"
    config = load('[sistent]\nmain = "a"\ncache_dir = "~/c"\n[repos.a]\nurl = "o/a"\n', make_config, registry)
    assert config.cache_dir == tmp_path / "home" / "c"


# ----- aspects: defaults and merging --------------------------------------------------------------------------------


def test_default_aspect_tables_are_fresh_copies_in_default_order() -> None:
    tables = default_aspect_tables()
    assert list(tables) == list(DEFAULT_ASPECT_NAMES)
    assert all(table["type"] in BUILTIN for table in tables.values())
    tables["readme"]["files"].append("mutated")
    assert default_aspect_tables()["readme"]["files"] == ["README.md"]


def test_merge_overrides_keep_other_default_keys() -> None:
    resolved, inherited = merge_aspect_tables(
        {"readme": {"similarity_threshold": 0.5, "tags": ["python"]}}, defaults=True
    )
    assert list(resolved) == list(DEFAULT_ASPECT_NAMES)
    readme = resolved["readme"]
    assert readme["similarity_threshold"] == 0.5
    assert readme["tags"] == ["python"]
    assert readme["files"] == ["README.md"]
    assert readme["type"] == "markdown"
    assert inherited["readme"] == frozenset(default_aspect_tables()["readme"]) - {"similarity_threshold"}
    assert "tags" not in inherited["readme"]
    assert inherited["layout"] == frozenset(default_aspect_tables()["layout"])


def test_merge_lists_and_dicts_replace_as_a_whole() -> None:
    resolved, _ = merge_aspect_tables(
        {"pyproject": {"keys": {"tool.ruff": "exact"}}, "layout": {"ignore": [".git"]}}, defaults=True
    )
    assert resolved["pyproject"]["keys"] == {"tool.ruff": "exact"}
    assert resolved["layout"]["ignore"] == [".git"]


def test_merge_enabled_false_removes_default_and_new_instances() -> None:
    resolved, inherited = merge_aspect_tables(
        {"workflows": {"enabled": False}, "changelog": {"type": "markdown", "enabled": False}}, defaults=True
    )
    assert "workflows" not in resolved
    assert "workflows" not in inherited
    assert "changelog" not in resolved
    assert len(resolved) == len(DEFAULT_ASPECT_NAMES) - 1


def test_merge_new_instances_follow_defaults_in_config_order() -> None:
    user = {"zeta": {"type": "toml", "file": "x.toml"}, "readme": {"merge": True}, "alpha": {"type": "markdown"}}
    resolved, inherited = merge_aspect_tables(user, defaults=True)
    assert list(resolved) == [*DEFAULT_ASPECT_NAMES, "zeta", "alpha"]
    assert inherited["zeta"] == frozenset()
    assert resolved["zeta"] == {"type": "toml", "file": "x.toml"}


def test_merge_without_defaults() -> None:
    resolved, inherited = merge_aspect_tables({"readme": {"type": "markdown", "files": ["README.md"]}}, defaults=False)
    assert resolved == {"readme": {"type": "markdown", "files": ["README.md"]}}
    assert inherited == {"readme": frozenset()}
    with pytest.raises(ConfigError, match=r"\[aspects\.readme\]\.type: required for a new aspect"):
        merge_aspect_tables({"readme": {"files": ["README.md"]}}, defaults=False)


@pytest.mark.parametrize(
    ("user", "message"),
    [
        (
            {"readme": {"type": "badges"}},
            r"\[aspects\.readme\]\.type: the type of built-in aspect 'readme' is fixed to 'markdown' \(got 'badges'\)",
        ),
        (
            {"changelog": {"files": ["CHANGELOG.md"]}},
            r"\[aspects\.changelog\]\.type: required for a new aspect \(built-in defaults: agent_instructions, ",
        ),
        ({"readme": {"enabled": "no"}}, r"\[aspects\.readme\]\.enabled: expected bool, got str"),
        ({"readme": {"type": 3}}, r"\[aspects\.readme\]\.type: expected str, got int"),
        ({"readme": "oops"}, r"\[aspects\.readme\]: expected a table, got str"),
    ],
)
def test_merge_errors(user: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        merge_aspect_tables(user, defaults=True)


def test_same_type_on_default_name_is_allowed() -> None:
    resolved, inherited = merge_aspect_tables({"readme": {"type": "markdown"}}, defaults=True)
    assert resolved["readme"]["type"] == "markdown"
    assert "type" not in inherited["readme"]


def test_loaded_aspects_override_and_extend(make_config: MakeConfig, registry: Registry) -> None:
    config = load(
        MINIMAL
        + """
        [aspects.agent_instructions]
        similarity_threshold = 0.5
        severity = { "missing.file" = "error", missing = "warning" }

        [aspects.workflows]
        enabled = false

        [aspects.changelog]
        type = "markdown"
        files = ["CHANGELOG.md"]
        default_mode = "presence"

        [aspects.docs_conf]
        type = "tests.test_config:TomlStub"
        file = "docs/conf.toml"
        """,
        make_config,
        registry,
    )
    assert config.aspect_names == [name for name in DEFAULT_ASPECT_NAMES if name != "workflows"] + [
        "changelog",
        "docs_conf",
    ]
    agent = config.aspect("agent_instructions")
    assert isinstance(agent.options, MarkdownOptions)
    assert agent.options.similarity_threshold == 0.5
    assert agent.options.severity == {"missing.file": "error", "missing": "warning"}
    assert agent.options.merge is True
    assert agent.inherited == frozenset(default_aspect_tables()["agent_instructions"]) - {
        "similarity_threshold",
        "severity",
    }
    assert agent.is_default
    changelog = config.aspect("changelog")
    assert changelog.type == "markdown"
    assert not changelog.is_default
    assert changelog.inherited == frozenset()
    assert changelog.table == {"type": "markdown", "files": ["CHANGELOG.md"], "default_mode": "presence"}
    docs = config.aspect("docs_conf")
    assert isinstance(docs.options, TomlOptions)
    assert docs.options.file == "docs/conf.toml"
    with pytest.raises(
        ConfigError, match=r"unknown aspect 'changelg' \(did you mean 'changelog'\?\)\. Configured aspects: "
    ):
        config.aspect("changelg")
    # the disabled default is gone for good, so it is not suggested either
    with pytest.raises(ConfigError, match=r"unknown aspect 'workflows'\. Configured aspects: "):
        config.aspect("workflows")


def test_defaults_false_yields_only_user_aspects(make_config: MakeConfig, registry: Registry) -> None:
    config = load(
        '[sistent]\nmain = "a"\ndefaults = false\n[repos.a]\nurl = "o/a"\n[aspects.only]\ntype = "badges"\nfile = "R.md"\n',
        make_config,
        registry,
    )
    assert config.aspect_names == ["only"]
    assert not config.aspect("only").is_default
    assert config.aspect("only").inherited == frozenset()


def test_unknown_option_with_did_you_mean(make_config: MakeConfig, registry: Registry) -> None:
    with pytest.raises(
        ConfigError,
        match=r"^\[aspects\.readme\]\.simlarity_threshold: unknown option for type 'markdown' \(did you mean 'similarity_threshold'\?\)\. Valid options: ",
    ):
        load(MINIMAL + "[aspects.readme]\nsimlarity_threshold = 0.5\n", make_config, registry)


def test_wrong_option_type(make_config: MakeConfig, registry: Registry) -> None:
    with pytest.raises(ConfigError, match=r"^\[aspects\.readme\]\.merge: expected bool, got str \('true'\)"):
        load(MINIMAL + '[aspects.readme]\nmerge = "true"\n', make_config, registry)


def test_unknown_aspect_type(make_config: MakeConfig, registry: Registry) -> None:
    with pytest.raises(
        ConfigError, match=r"^\[aspects\.x\]\.type: unknown aspect type 'markdwn' \(did you mean 'markdown'\?\)"
    ):
        load(MINIMAL + '[aspects.x]\ntype = "markdwn"\n', make_config, registry)
    with pytest.raises(ConfigError, match=r"^\[aspects\.x\]\.type: cannot import aspect type 'no.such:Thing'"):
        load(MINIMAL + '[aspects.x]\ntype = "no.such:Thing"\n', make_config, registry)


def test_skip_aspects_must_name_configured_aspects(make_config: MakeConfig, registry: Registry) -> None:
    text = MINIMAL + '[repos.extra]\nskip_aspects = ["pre-commit"]\n'
    with pytest.raises(
        ConfigError,
        match=r"^\[repos\.extra\]\.skip_aspects: unknown aspect 'pre-commit' \(did you mean 'pre_commit'\?\)\. Configured aspects: ",
    ):
        load(text, make_config, registry)
    # a disabled default is unknown too
    with pytest.raises(ConfigError, match=r"\[repos\.extra\]\.skip_aspects: unknown aspect 'workflows'"):
        load(
            MINIMAL + '[repos.extra]\nskip_aspects = ["workflows"]\n[aspects.workflows]\nenabled = false\n',
            make_config,
            registry,
        )
    config = load(MINIMAL + '[repos.extra]\nskip_aspects = ["workflows", "readme"]\n', make_config, registry)
    assert config.repos["extra"].skip_aspects == ("workflows", "readme")


def test_default_registry_is_used_when_none_given(make_config: MakeConfig) -> None:
    pytest.importorskip("sistent.aspects.markdown")
    for name in BUILTIN:
        pytest.importorskip(BUILTIN[name].partition(":")[0])
    config = load_config(make_config(MINIMAL))
    assert config.aspect_names == list(DEFAULT_ASPECT_NAMES)
    assert type(config.aspect("readme").options) is default_registry().get("markdown").options_cls


# ----- discovery ---------------------------------------------------------------------------------------------------


def test_find_config_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit.toml"
    explicit.write_text("", encoding="utf-8")
    from_env = tmp_path / "env.toml"
    from_env.write_text("", encoding="utf-8")
    (tmp_path / "sistent.toml").write_text("", encoding="utf-8")
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)

    monkeypatch.delenv("SISTENT_CONFIG", raising=False)
    assert find_config(start=nested) == tmp_path / "sistent.toml"
    assert find_config(start=tmp_path) == tmp_path / "sistent.toml"
    monkeypatch.setenv("SISTENT_CONFIG", str(from_env))
    assert find_config(start=nested) == from_env
    assert find_config(explicit, start=nested) == explicit
    assert find_config(str(explicit)) == explicit

    monkeypatch.chdir(nested)
    monkeypatch.delenv("SISTENT_CONFIG", raising=False)
    assert find_config() == tmp_path / "sistent.toml"


def test_find_config_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SISTENT_CONFIG", raising=False)
    with pytest.raises(ConfigError, match=r"config file not found: .*missing\.toml"):
        find_config(tmp_path / "missing.toml", start=tmp_path)
    monkeypatch.setenv("SISTENT_CONFIG", str(tmp_path / "env-missing.toml"))
    with pytest.raises(ConfigError, match=r"config file not found: .*env-missing.toml \(from \$SISTENT_CONFIG\)"):
        find_config(start=tmp_path)
    monkeypatch.delenv("SISTENT_CONFIG", raising=False)
    empty = tmp_path / "empty" / "deeper"
    empty.mkdir(parents=True)
    if not any((parent / "sistent.toml").exists() for parent in (empty, *empty.parents)):
        with pytest.raises(
            ConfigError, match=r"no sistent.toml found in .* \(use -c/--config or set \$SISTENT_CONFIG\)"
        ):
            find_config(start=empty)


def test_load_config_missing_file(tmp_path: Path, registry: Registry) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "nope.toml", registry=registry)


# ----- dump and round trip -----------------------------------------------------------------------------------------

FULL = """
[sistent]
main = "neuroconv"
github = "catalystneuro"
cache_dir = "cache"
fail_on = "warning"
git_timeout = 30
jobs = 4
baseline = ".sistent-baseline.json"

[repos.neuroconv]
path = "../neuroconv"
tags = ["python", "public"]
aliases = ["NeuroConv"]
vars = { org = "catalystneuro" }

[repos.roiextractors]
rev = "main"
ignore = ["readme:README.md#Funding*"]
skip_aspects = ["pre_commit"]

[repos."odd name"]
url = "SpikeInterface/spikeinterface"
rev = "0.101.2"
root = "packages/foo"
substitute = false

[aspects.agent_instructions]
similarity_threshold = 0.5
severity = { "missing.file" = "error", missing = "warning" }

[aspects.readme]
tags = ["python"]
sections = { installation = "similar", "weird key" = "presence" }

[aspects.workflows]
enabled = false

[aspects.changelog]
type = "markdown"
files = ["CHANGELOG.md"]
default_mode = "presence"
ignore_sections = ["quote \\" and backslash \\\\ and tab \\t"]
"""


def strip_provenance(config: Config) -> Config:
    """``path``/``directory`` and the inherited-key provenance cannot survive a text round trip."""
    aspects = tuple(dataclasses.replace(spec, inherited=frozenset()) for spec in config.aspects)
    return dataclasses.replace(config, path=Path("/x"), directory=Path("/"), aspects=aspects)


def test_dump_config_round_trips(make_config: MakeConfig, registry: Registry) -> None:
    config = load(FULL, make_config, registry)
    text = dump_config(config)
    tomllib.loads(text)  # valid TOML
    again = loads_config(text, path=config.path, registry=registry)
    assert strip_provenance(again) == strip_provenance(config)
    assert again.repos == config.repos
    assert again.cache_dir == config.cache_dir
    assert again.baseline == config.baseline
    assert again.fail_on is config.fail_on
    assert [spec.table for spec in again.aspects] == [spec.table for spec in config.aspects]
    assert [spec.options for spec in again.aspects] == [spec.options for spec in config.aspects]
    # a second dump is stable in values and layout; only the provenance markers differ, because every key of the
    # reloaded config was written explicitly and is therefore no longer inherited
    assert dump_config(again).replace("  # default", "") == text.replace("  # default", "")
    assert "# default" in text
    assert "# default" not in dump_config(again)
    assert all(spec.inherited == frozenset() for spec in again.aspects)


def test_dump_config_round_trips_never_and_no_github(make_config: MakeConfig, registry: Registry) -> None:
    config = load('[sistent]\nmain = "a"\nfail_on = "never"\n[repos.a]\nurl = "o/a"\n', make_config, registry)
    text = dump_config(config)
    assert 'fail_on = "never"' in text
    assert not any(line.startswith("github") for line in text.splitlines())
    again = loads_config(text, path=config.path, registry=registry)
    assert again.fail_on is None
    assert strip_provenance(again) == strip_provenance(config)


def test_dump_config_marks_inherited_keys(make_config: MakeConfig, registry: Registry) -> None:
    config = load(FULL, make_config, registry)
    lines = dump_config(config).splitlines()
    assert "[sistent]" in lines
    assert 'main = "neuroconv"' in lines
    assert 'url = "https://github.com/catalystneuro/roiextractors"' in lines
    assert '[repos."odd name"]' in lines
    assert "[aspects.agent_instructions]" in lines
    assert "similarity_threshold = 0.5" in lines
    assert 'type = "markdown"  # default' in lines
    assert "merge = true  # default" in lines
    # a disabled default is written out as disabled, in its default position, so a reload does not resurrect it
    workflows_index = lines.index("[aspects.workflows]")
    assert lines[workflows_index + 1] == "enabled = false"
    assert lines.index("[aspects.pyproject]") < workflows_index < lines.index("[aspects.pre_commit]")
    assert "[aspects.pyproject.keys]  # default" in lines
    assert 'build-system = "exact"' in lines  # a valid bare key stays bare
    assert '"project.requires-python" = "exact"' in lines  # dotted keys are quoted
    assert "[aspects.changelog]" in lines
    marked = [line for line in lines if line.endswith("# default")]
    assert marked
    changelog_index = lines.index("[aspects.changelog]")
    assert not any(line.endswith("# default") for line in lines[changelog_index:])
    # the user's own keys are never marked
    for line in lines:
        if line.startswith(("similarity_threshold", "severity =")):
            assert not line.endswith("# default")
    # long lists are wrapped, one item per line, still valid TOML
    assert "files = [" in lines
    assert '    "AGENTS.md",' in lines


def test_dump_config_escapes_strings(make_config: MakeConfig, registry: Registry) -> None:
    config = load(FULL, make_config, registry)
    text = dump_config(config)
    parsed = tomllib.loads(text)
    assert parsed["aspects"]["changelog"]["ignore_sections"] == ['quote " and backslash \\ and tab \t']
    assert parsed["aspects"]["readme"]["sections"] == {"installation": "similar", "weird key": "presence"}
    assert parsed["repos"]["odd name"]["url"] == "https://github.com/SpikeInterface/spikeinterface"


def test_dump_config_of_defaults_only_lists_all_defaults(make_config: MakeConfig, registry: Registry) -> None:
    config = load(MINIMAL, make_config, registry)
    parsed = tomllib.loads(dump_config(config))
    assert list(parsed["aspects"]) == list(DEFAULT_ASPECT_NAMES)
    assert parsed["aspects"] == {
        name: spec.table for name, spec in zip(DEFAULT_ASPECT_NAMES, config.aspects, strict=True)
    }
    assert parsed["sistent"]["cache_dir"] == str(config.cache_dir)


# ----- init template -----------------------------------------------------------------------------------------------


def test_default_config_text_loads_back_cleanly(registry: Registry, tmp_path: Path) -> None:
    text = default_config_text()
    config = loads_config(text, path=tmp_path / "sistent.toml", registry=registry)
    assert config.main == "my-main-repo"
    assert list(config.repos) == ["my-main-repo", "another-repo"]
    assert config.repos["my-main-repo"].path == tmp_path / "../my-main-repo"
    assert config.github is None
    assert config.aspect_names == list(DEFAULT_ASPECT_NAMES)
    # every default key is spelled out in the file the user owns, so nothing is inherited any more
    assert all(spec.inherited == frozenset() for spec in config.aspects)
    assert [spec.table for spec in config.aspects] == list(default_aspect_tables().values())
    assert "# Agent-instruction files" in text


def test_default_config_text_with_explicit_paths(registry: Registry, tmp_path: Path) -> None:
    text = default_config_text(
        main="core",
        repos=("plugin-a", "core", "plugin-b"),
        github="my-org",
        paths={"core": ".", "plugin-a": "../plugin-a"},
    )
    lines = text.splitlines()
    assert lines.count("[repos.core]") == 1  # names are de-duplicated
    assert lines[lines.index("[repos.core]") + 1] == 'path = "."  # relative to this file'
    assert lines[lines.index("[repos.plugin-a]") + 1] == 'path = "../plugin-a"  # relative to this file'
    assert lines[lines.index("[repos.plugin-b]") + 1].startswith(
        "# url derived from [sistent].github: https://github.com/my-org/plugin-b"
    )
    config = loads_config(text, path=tmp_path / "sistent.toml", registry=registry)
    assert list(config.repos) == ["core", "plugin-a", "plugin-b"]
    assert config.repos["core"].path == tmp_path
    assert config.repos["plugin-a"].path == tmp_path / "../plugin-a"
    assert config.repos["plugin-b"].url == "https://github.com/my-org/plugin-b"
    # without github, a repo missing from ``paths`` falls back to the ../<name> template
    text = default_config_text(main="core", repos=("other",), paths={"core": "."})
    lines = text.splitlines()
    assert lines[lines.index("[repos.other]") + 1].startswith('path = "../other"')
    assert loads_config(text, path=tmp_path / "sistent.toml", registry=registry).repos["core"].path == tmp_path


def test_default_config_text_with_github(registry: Registry, tmp_path: Path) -> None:
    text = default_config_text(main="core", repos=("plugin-a", "plugin-b"), github="my-org")
    config = loads_config(text, path=tmp_path / "sistent.toml", registry=registry)
    assert config.github == "my-org"
    assert config.repos["core"].url == "https://github.com/my-org/core"
    assert config.repos["plugin-b"].url == "https://github.com/my-org/plugin-b"
    assert config.repos["core"].path is None
    assert 'github = "my-org"' in text


def test_aspect_spec_and_repo_spec_are_frozen() -> None:
    spec = RepoSpec(name="a", url="https://example.invalid/a")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "b"  # type: ignore[misc]
    aspect = AspectSpec(
        name="x",
        type="markdown",
        options=BaseOptions(),
        table={"type": "markdown"},
        inherited=frozenset(),
        is_default=False,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        aspect.name = "y"  # type: ignore[misc]
