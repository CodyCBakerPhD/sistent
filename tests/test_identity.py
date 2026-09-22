"""Tests for identity probing (remotes, pyproject, branch), :func:`build_identity` and the :class:`Substituter`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sistent.compare.text import NullSubstituter, Substituter
from sistent.model import Identity, StaleHit
from sistent.repository import (
    PROBES,
    Probe,
    RepoHints,
    Repository,
    build_identity,
    name_variants,
    parse_remote,
    probe_branch,
    probe_config,
    probe_pyproject,
    probe_remote,
)
from tests.conftest import MakeRepo, git, requires_git, write_files

SHA40 = "0123456789abcdef0123456789abcdef01234567"


def init_git(root: Path, files: dict[str, str], *, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_files(root, files)
    git("init", "-q", f"--initial-branch={branch}", cwd=root)
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "initial", cwd=root)


def const(result: dict[str, Any]) -> Probe:
    def probe(repo: Repository, hints: RepoHints) -> dict[str, Any]:
        return dict(result)

    return probe


def boom(repo: Repository, hints: RepoHints) -> dict[str, Any]:
    raise RuntimeError("probe exploded")


def pyproject(name: str | None, extra: str = "") -> str:
    head = f'[project]\nname = "{name}"\n' if name is not None else ""
    return head + extra


# ----- parse_remote / name_variants --------------------------------------------------------------------------------


class TestParseRemote:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/catalystneuro/neuroconv", ("github.com", "catalystneuro", "neuroconv")),
            ("https://github.com/catalystneuro/neuroconv.git", ("github.com", "catalystneuro", "neuroconv")),
            ("https://github.com/catalystneuro/neuroconv/", ("github.com", "catalystneuro", "neuroconv")),
            ("http://github.com/catalystneuro/neuroconv.git/", ("github.com", "catalystneuro", "neuroconv")),
            ("https://user@github.com/catalystneuro/neuroconv.git", ("github.com", "catalystneuro", "neuroconv")),
            ("https://gitlab.example.com:8443/group/sub/repo.git", ("gitlab.example.com", "sub", "repo")),
            ("git@github.com:org/repo.git", ("github.com", "org", "repo")),
            ("git@github.com:org/repo", ("github.com", "org", "repo")),
            ("ssh://git@host/org/repo", ("host", "org", "repo")),
            ("ssh://git@github.com/org/repo.git", ("github.com", "org", "repo")),
            ("git://github.com/org/repo.git", ("github.com", "org", "repo")),
            ("  https://github.com/org/repo.git\n", ("github.com", "org", "repo")),
        ],
    )
    def test_valid(self, url: str, expected: tuple[str, str, str]) -> None:
        assert parse_remote(url) == expected

    @pytest.mark.parametrize(
        "url", ["", "not a url", "https://github.com/org", "https://github.com/", "repo", "org/repo"]
    )
    def test_invalid(self, url: str) -> None:
        assert parse_remote(url) is None


class TestNameVariants:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("roi-extractors", ["roi-extractors", "roi_extractors"]),
            ("roi_extractors", ["roi_extractors", "roi-extractors"]),
            ("NeuroConv", ["NeuroConv", "neuroconv"]),
            ("neuroconv", ["neuroconv"]),
            ("Zarr.Python", ["Zarr.Python", "zarr-python", "zarr_python"]),
            ("a__b--c", ["a__b--c", "a-b-c", "a_b_c", "a__b__c", "a--b--c"]),
        ],
    )
    def test_variants(self, name: str, expected: list[str]) -> None:
        assert name_variants(name) == expected

    def test_first_is_the_original_and_no_duplicates(self) -> None:
        variants = name_variants("My-Pkg")
        assert variants[0] == "My-Pkg"
        assert len(variants) == len(set(variants))
        assert "my-pkg" in variants


# ----- probes ------------------------------------------------------------------------------------------------------


class TestProbeConfig:
    def test_collects_everything_from_hints(self, tmp_path: Path) -> None:
        hints = RepoHints(
            name="neuroconv",
            aliases=("nc", "NeuroConv"),
            vars={"name": "nwbconv", "org": "catalystneuro", "branch": "main", "docs": "https://docs.example"},
        )
        result = probe_config(Repository("neuroconv", tmp_path), hints)
        assert result == {
            "aliases": ["neuroconv", "nc", "NeuroConv", "nwbconv"],
            "forced": ["nc", "NeuroConv", "nwbconv"],
            "org": "catalystneuro",
            "branch": "main",
            "extra": {"docs": "https://docs.example"},
        }

    def test_minimal_hints(self, tmp_path: Path) -> None:
        result = probe_config(Repository("x", tmp_path), RepoHints(name="x"))
        assert result == {"aliases": ["x"], "forced": [], "extra": {}}


class TestProbePyproject:
    def probe(self, tmp_path: Path, files: dict[str, str]) -> list[str]:
        write_files(tmp_path, files)
        result = probe_pyproject(Repository("r", tmp_path), RepoHints(name="r"))
        assert set(result) <= {"aliases"}
        return list(result.get("aliases", []))

    def test_no_pyproject(self, tmp_path: Path) -> None:
        assert probe_pyproject(Repository("r", tmp_path), RepoHints(name="r")) == {}

    def test_invalid_toml(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"pyproject.toml": "[project\nname = "})
        assert probe_pyproject(Repository("r", tmp_path), RepoHints(name="r")) == {}

    def test_project_name_variants(self, tmp_path: Path) -> None:
        assert self.probe(tmp_path, {"pyproject.toml": pyproject("roi-extractors")}) == [
            "roi-extractors",
            "roi_extractors",
        ]

    def test_no_project_table_still_finds_packages(self, tmp_path: Path) -> None:
        files = {"pyproject.toml": pyproject(None, "[tool.other]\nx = 1\n"), "src/pkg/__init__.py": ""}
        assert self.probe(tmp_path, files) == ["pkg"]

    def test_hatch_packages(self, tmp_path: Path) -> None:
        extra = '[tool.hatch.build.targets.wheel]\npackages = ["src/roiextractors", "lib/helpers/"]\n'
        aliases = self.probe(tmp_path, {"pyproject.toml": pyproject("roi-extractors", extra)})
        assert aliases == ["roi-extractors", "roi_extractors", "roiextractors", "helpers"]

    def test_setuptools_packages_and_package_dir(self, tmp_path: Path) -> None:
        extra = '[tool.setuptools]\npackages = ["mypkg", "mypkg.sub"]\npackage-dir = {"" = "src", alt = "lib/alt"}\n'
        aliases = self.probe(tmp_path, {"pyproject.toml": pyproject("my-pkg", extra)})
        assert aliases == ["my-pkg", "my_pkg", "mypkg", "mypkg", "alt"]

    def test_package_directories_under_src_and_root(self, tmp_path: Path) -> None:
        files = {
            "pyproject.toml": pyproject("roi-extractors"),
            "src/roiextractors/__init__.py": "",
            "src/nopkg/module.py": "",
            "rootpkg/__init__.py": "",
            ".hidden/__init__.py": "",
            "docs/conf.py": "",
        }
        # a src/ layout wins: root-level packages are not scanned as well
        assert self.probe(tmp_path, files) == ["roi-extractors", "roi_extractors", "roiextractors"]

    def test_root_layout_skips_conventional_non_packages(self, tmp_path: Path) -> None:
        files = {
            "pyproject.toml": pyproject("rootpkg"),
            "rootpkg/__init__.py": "",
            "tests/__init__.py": "",
            "docs/__init__.py": "",
            "examples/__init__.py": "",
        }
        assert self.probe(tmp_path, files) == ["rootpkg"]

    def test_more_than_three_candidates_keeps_only_name_matches(self, tmp_path: Path) -> None:
        files = {
            "pyproject.toml": pyproject("my-pkg"),
            "src/My_Pkg/__init__.py": "",
            "src/aaa/__init__.py": "",
            "src/bbb/__init__.py": "",
            "src/ccc/__init__.py": "",
        }
        aliases = self.probe(tmp_path, files)
        assert "My_Pkg" in aliases, "matched case-insensitively against a project.name variant"
        assert not {"aaa", "bbb", "ccc"} & set(aliases)

    def test_exactly_three_candidates_are_all_kept(self, tmp_path: Path) -> None:
        files = {
            "pyproject.toml": pyproject("my-pkg"),
            "src/aaa/__init__.py": "",
            "src/bbb/__init__.py": "",
            "src/ccc/__init__.py": "",
        }
        assert self.probe(tmp_path, files)[2:] == ["aaa", "bbb", "ccc"]

    def test_garbage_shapes_are_ignored(self, tmp_path: Path) -> None:
        extra = (
            "[tool.hatch.build.targets.wheel]\npackages = 3\n[tool.setuptools]\npackages = [1, 2]\npackage-dir = 'x'\n"
        )
        files = {"pyproject.toml": "[project]\nname = 5\n" + extra}
        assert self.probe(tmp_path, files) == []


class TestProbeRemote:
    def test_url_hint(self, tmp_path: Path) -> None:
        result = probe_remote(
            Repository("r", tmp_path), RepoHints(name="r", url="https://github.com/catalystneuro/neuroconv.git")
        )
        assert result == {
            "aliases": ["neuroconv"],
            "org": "catalystneuro",
            "slug": "github.com/catalystneuro/neuroconv",
        }

    def test_unparseable_url_hint(self, tmp_path: Path) -> None:
        assert probe_remote(Repository("r", tmp_path), RepoHints(name="r", url="nonsense")) == {}

    def test_no_url_and_no_git(self, tmp_path: Path) -> None:
        assert probe_remote(Repository("r", tmp_path), RepoHints(name="r")) == {}

    @requires_git
    def test_git_remote(self, tmp_path: Path) -> None:
        git("init", "-q", cwd=tmp_path)
        git("remote", "add", "origin", "git@gitlab.example.com:catalystneuro/neuroconv.git", cwd=tmp_path)
        result = probe_remote(Repository("r", tmp_path), RepoHints(name="r"))
        assert result == {
            "aliases": ["neuroconv"],
            "org": "catalystneuro",
            "slug": "gitlab.example.com/catalystneuro/neuroconv",
        }

    @requires_git
    def test_git_without_origin(self, tmp_path: Path) -> None:
        git("init", "-q", cwd=tmp_path)
        assert probe_remote(Repository("r", tmp_path), RepoHints(name="r")) == {}

    @requires_git
    def test_url_hint_wins_over_git_remote(self, tmp_path: Path) -> None:
        git("init", "-q", cwd=tmp_path)
        git("remote", "add", "origin", "https://gitlab.example.com/probed/fromgit.git", cwd=tmp_path)
        result = probe_remote(Repository("r", tmp_path), RepoHints(name="r", url="https://example.org/cfg/fromconfig"))
        assert result["slug"] == "example.org/cfg/fromconfig"


class TestProbeBranch:
    @pytest.mark.parametrize("rev", ["develop", "v1.2.3", "release/2.0", "deadbeefcafe-branch"])
    def test_rev_hint_that_is_not_a_sha(self, tmp_path: Path, rev: str) -> None:
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r", rev=rev)) == {"branch": rev}

    @pytest.mark.parametrize("rev", [SHA40, "abc1234", "0123456789abcdef"])
    def test_sha_rev_is_ignored(self, tmp_path: Path, rev: str) -> None:
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r", rev=rev)) == {}

    def test_no_rev_and_no_git(self, tmp_path: Path) -> None:
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r")) == {}

    @requires_git
    def test_symbolic_ref_origin_head(self, tmp_path: Path) -> None:
        init_git(tmp_path, {"a.txt": ""}, branch="feature")
        git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/develop", cwd=tmp_path)
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r")) == {"branch": "develop"}
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r", rev=SHA40)) == {"branch": "develop"}

    @requires_git
    def test_abbrev_ref_fallback(self, tmp_path: Path) -> None:
        init_git(tmp_path, {"a.txt": ""}, branch="feature")
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r")) == {"branch": "feature"}

    @requires_git
    def test_detached_head_gives_nothing(self, tmp_path: Path) -> None:
        init_git(tmp_path, {"a.txt": ""})
        git("checkout", "-q", "--detach", cwd=tmp_path)
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r")) == {}

    @requires_git
    def test_rev_hint_wins_over_git(self, tmp_path: Path) -> None:
        init_git(tmp_path, {"a.txt": ""}, branch="feature")
        assert probe_branch(Repository("r", tmp_path), RepoHints(name="r", rev="release")) == {"branch": "release"}


# ----- build_identity ----------------------------------------------------------------------------------------------


class TestBuildIdentity:
    def test_default_probe_order(self) -> None:
        assert [probe_config, probe_pyproject, probe_remote, probe_branch] == PROBES

    def test_dedupes_casefolded_keeping_first_spelling(self, tmp_path: Path) -> None:
        probes = [const({"aliases": ["NeuroConv", "neuroconv", "NEUROCONV", "neuroconv"]})]
        identity = build_identity(Repository("r", tmp_path), RepoHints(name="r"), probes=probes)
        assert identity.aliases == ("NeuroConv",)
        assert identity.name == "r"

    def test_sorted_longest_first_then_alphabetically(self, tmp_path: Path) -> None:
        probes = [const({"aliases": ["abcd", "abcdef", "bbbb", "aaaa", "Abcdefg"]})]
        identity = build_identity(Repository("r", tmp_path), RepoHints(name="r"), probes=probes)
        assert identity.aliases == ("Abcdefg", "abcdef", "aaaa", "abcd", "bbbb")

    def test_short_aliases_dropped_unless_forced(self, tmp_path: Path) -> None:
        repo = Repository("nwb", tmp_path)
        probes = [probe_config, const({"aliases": ["xyz", "long-enough"]})]
        plain = build_identity(repo, RepoHints(name="nwb"), probes=probes)
        assert plain.aliases == ("long-enough",), "config key and probed 3-letter aliases are dropped"
        forced = build_identity(repo, RepoHints(name="nwb", aliases=("nwb", "ab")), probes=probes)
        assert forced.aliases == ("long-enough", "nwb", "ab")
        via_vars = build_identity(repo, RepoHints(name="nwb", vars={"name": "nc"}), probes=probes)
        assert via_vars.aliases == ("long-enough", "nc")

    def test_four_character_alias_kept(self, tmp_path: Path) -> None:
        identity = build_identity(Repository("r", tmp_path), RepoHints(name="r"), probes=[const({"aliases": ["abcd"]})])
        assert identity.aliases == ("abcd",)

    def test_config_vars_win_over_probes(self, tmp_path: Path) -> None:
        hints = RepoHints(
            name="neuroconv",
            url="https://github.com/probed-org/probed-repo",
            rev="probed-branch",
            vars={"org": "cfg-org", "branch": "cfg-branch"},
        )
        identity = build_identity(Repository("neuroconv", tmp_path), hints)
        assert identity.org == "cfg-org"
        assert identity.branch == "cfg-branch"
        assert identity.slug == "github.com/probed-org/probed-repo"
        assert "probed-repo" in identity.aliases
        assert identity.extra == {}

    def test_probed_org_and_branch_used_when_not_configured(self, tmp_path: Path) -> None:
        hints = RepoHints(name="neuroconv", url="https://github.com/probed-org/probed-repo", rev="probed-branch")
        identity = build_identity(Repository("neuroconv", tmp_path), hints)
        assert identity.org == "probed-org"
        assert identity.branch == "probed-branch"

    def test_extra_vars_kept(self, tmp_path: Path) -> None:
        hints = RepoHints(name="neuroconv", vars={"docs": "https://docs.example", "channel": "nwb-slack", "org": "o"})
        identity = build_identity(Repository("neuroconv", tmp_path), hints)
        assert identity.extra == {"docs": "https://docs.example", "channel": "nwb-slack"}
        assert identity.org == "o"

    def test_raising_probe_is_ignored(self, tmp_path: Path) -> None:
        probes = [probe_config, boom, const({"aliases": ["from-const"], "org": "const-org"})]
        identity = build_identity(Repository("neuroconv", tmp_path), RepoHints(name="neuroconv"), probes=probes)
        assert identity.aliases == ("from-const", "neuroconv")
        assert identity.org == "const-org"

    def test_first_probe_wins_for_scalars(self, tmp_path: Path) -> None:
        probes = [const({"org": "first", "branch": None}), const({"org": "second", "branch": "b", "slug": "s"})]
        identity = build_identity(Repository("r", tmp_path), RepoHints(name="r"), probes=probes)
        assert (identity.org, identity.branch, identity.slug) == ("first", "b", "s")

    def test_blank_and_non_string_aliases_skipped(self, tmp_path: Path) -> None:
        probes = [const({"aliases": ["", "   ", " padded ", 42, None]})]
        identity = build_identity(Repository("r", tmp_path), RepoHints(name="r"), probes=probes)
        assert identity.aliases == ("padded",)

    def test_no_probes(self, tmp_path: Path) -> None:
        identity = build_identity(Repository("r", tmp_path), RepoHints(name="r"), probes=[])
        assert identity == Identity(name="r", aliases=(), org=None, branch=None, slug=None, extra={})

    def test_end_to_end_with_default_probes(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(
            {"pyproject.toml": pyproject("neuroconv"), "src/neuroconv/__init__.py": ""},
            name="neuroconv",
            url="https://github.com/catalystneuro/neuroconv",
            vars={"branch": "main"},
        )
        identity = ctx.identity
        assert identity.name == "neuroconv"
        assert identity.aliases == ("neuroconv",)
        assert identity.org == "catalystneuro"
        assert identity.branch == "main"
        assert identity.slug == "github.com/catalystneuro/neuroconv"
        assert identity.to_dict() == {
            "name": "neuroconv",
            "aliases": ["neuroconv"],
            "org": "catalystneuro",
            "branch": "main",
            "slug": "github.com/catalystneuro/neuroconv",
            "extra": {},
        }


# ----- Substituter with real identities ----------------------------------------------------------------------------


ROI_FILES = {
    "pyproject.toml": pyproject("roi-extractors"),
    "src/roiextractors/__init__.py": "",
}
NEUROCONV_FILES = {
    "pyproject.toml": pyproject("neuroconv"),
    "src/neuroconv/__init__.py": "",
}


class TestSubstituterIntegration:
    def test_probed_aliases(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(ROI_FILES, name="roi")
        assert ctx.identity.aliases == ("roi-extractors", "roi_extractors", "roiextractors")
        assert "roi" not in ctx.identity.aliases, "the 3-letter config key is not forced"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("pip install roi-extractors[full]", "pip install {{name}}[full]"),
            ("from roiextractors import X", "from {{name}} import X"),
            ("import roi_extractors.extractors", "import {{name}}.extractors"),
            ("https://roi-extractors.readthedocs.io", "https://{{name}}.readthedocs.io"),
            ("RoiExtractors", "{{name}}"),
            ("roiextractors2", "roiextractors2"),
        ],
    )
    def test_substitution(self, make_repo: MakeRepo, text: str, expected: str) -> None:
        ctx = make_repo(ROI_FILES, name="roi")
        assert ctx.substitute(text, where="README.md") == expected
        assert ctx.subst.hits == []

    def test_applied_records_the_matched_spellings(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(ROI_FILES, name="roi")
        ctx.substitute("pip install roi-extractors then from roiextractors import X and RoiExtractors")
        assert ctx.subst.applied == {"roi-extractors", "roiextractors", "RoiExtractors"}

    def test_stale_detection_between_two_repos(self, make_repo: MakeRepo) -> None:
        main = make_repo(NEUROCONV_FILES, name="neuroconv", is_main=True)
        sat = make_repo(ROI_FILES, name="roi", foreign=[main.identity])
        text = sat.substitute("Install neuroconv, then roiextractors.", where="README.md#Installation")
        assert text == "Install neuroconv, then {{name}}.", "foreign mentions are left in place"
        assert sat.subst.hits == [
            StaleHit(
                where="README.md#Installation",
                alias="neuroconv",
                other_repo="neuroconv",
                excerpt="Install neuroconv, then {{name}}.",
            )
        ]

    def test_stale_detection_is_symmetric(self, make_repo: MakeRepo) -> None:
        sat = make_repo(ROI_FILES, name="roi")
        main = make_repo(NEUROCONV_FILES, name="neuroconv", is_main=True, foreign=[sat.identity])
        assert main.substitute("neuroconv wraps ROI-Extractors", where="AGENTS.md") == "{{name}} wraps ROI-Extractors"
        (hit,) = main.subst.hits
        assert (hit.other_repo, hit.alias, hit.where) == ("roi", "ROI-Extractors", "AGENTS.md")

    def test_own_mentions_are_never_stale(self, make_repo: MakeRepo) -> None:
        main = make_repo(NEUROCONV_FILES, name="neuroconv")
        sat = make_repo(ROI_FILES, name="roi", foreign=[main.identity])
        sat.substitute("roi-extractors and roiextractors")
        assert sat.subst.hits == []

    def test_substitute_false_uses_null_substituter(self, make_repo: MakeRepo) -> None:
        ctx = make_repo(ROI_FILES, name="roi", substitute=False)
        assert isinstance(ctx.subst, NullSubstituter)
        assert ctx.substitute("pip install roi-extractors") == "pip install roi-extractors"
        assert ctx.subst.hits == []
        assert ctx.subst.applied == set()


# ----- Substituter semantics ---------------------------------------------------------------------------------------


OWN = Identity(
    name="neuroconv",
    aliases=("neuroconv",),
    org="catalystneuro",
    branch="main",
    slug="github.com/catalystneuro/neuroconv",
    extra={"channel": "nwb-slack"},
)
FOREIGN = (
    Identity(name="spikeinterface", aliases=("spikeinterface", "si")),
    Identity(name="shared", aliases=("neuroconv", "tools")),
)


class TestSubstituterSemantics:
    @pytest.fixture
    def subst(self) -> Substituter:
        return Substituter(OWN, FOREIGN)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("neuroconv", "{{name}}"),
            ("NEUROCONV", "{{name}}"),
            ("neuroconv[dandi]", "{{name}}[dandi]"),
            ("neuroconv.git", "{{name}}.git"),
            ("neuroconv_logo.png", "{{name}}_logo.png"),
            ("(neuroconv)", "({{name}})"),
            ("`neuroconv`", "`{{name}}`"),
            ("neuroconvx", "neuroconvx"),
            ("myneuroconv", "myneuroconv"),
            ("neuroconv2", "neuroconv2"),
            ("by catalystneuro", "by {{org}}"),
            ("catalystneuro/neuroconv", "{{org}}/{{name}}"),
            ("CatalystNeuro/NeuroConv", "{{org}}/{{name}}"),
            (
                "https://github.com/catalystneuro/neuroconv/blob/main/README.md",
                "https://github.com/{{org}}/{{name}}/blob/{{branch}}/README.md",
            ),
            ("the main entry point", "the main entry point"),
            ("branch=main", "branch={{branch}}"),
            ("?version=main", "?version={{branch}}"),
            ("/tree/main/docs", "/tree/{{branch}}/docs"),
            ("/raw/main/logo.png", "/raw/{{branch}}/logo.png"),
            ("/en/main/", "/en/{{branch}}/"),
            ("neuroconv.git@main", "{{name}}.git@{{branch}}"),
            ("pkg@main", "pkg@{{branch}}"),
            ("/blob/main-branch/", "/blob/main-branch/"),
            ("/blob/mainline/", "/blob/mainline/"),
            ("/blob/main.old/", "/blob/main.old/"),
            ("join nwb-slack", "join {{channel}}"),
            ("", ""),
        ],
    )
    def test_substitution(self, subst: Substituter, text: str, expected: str) -> None:
        assert subst(text, where="x") == expected

    def test_applied_records_matched_spelling_of_aliases_only(self, subst: Substituter) -> None:
        subst("NeuroConv and neuroconv by catalystneuro on main")
        assert subst.applied == {"NeuroConv", "neuroconv"}

    def test_longest_alias_wins(self) -> None:
        subst = Substituter(Identity(name="roi", aliases=("roi", "roi-extractors")))
        assert subst("roi-extractors and roi") == "{{name}} and {{name}}"

    def test_foreign_alias_records_stale_hit_and_leaves_text(self, subst: Substituter) -> None:
        assert subst("use spikeinterface here", where="README.md#Usage") == "use spikeinterface here"
        assert subst.hits == [
            StaleHit(
                where="README.md#Usage",
                alias="spikeinterface",
                other_repo="spikeinterface",
                excerpt="use spikeinterface here",
            )
        ]

    def test_foreign_alias_matching_is_case_insensitive_and_bounded(self, subst: Substituter) -> None:
        subst("SpikeInterface, spikeinterface[full], spikeinterfaces")
        assert [h.alias for h in subst.hits] == ["SpikeInterface", "spikeinterface"]

    def test_short_foreign_aliases_are_ignored(self, subst: Substituter) -> None:
        subst("si is short")
        assert subst.hits == []

    def test_shared_alias_is_own_not_stale(self, subst: Substituter) -> None:
        assert subst("neuroconv") == "{{name}}"
        assert subst.hits == []

    def test_other_alias_of_a_partially_shared_identity_is_stale(self, subst: Substituter) -> None:
        subst("tools")
        assert [(h.other_repo, h.alias) for h in subst.hits] == [("shared", "tools")]

    def test_foreign_identity_with_own_name_is_skipped(self) -> None:
        subst = Substituter(OWN, [Identity(name="neuroconv", aliases=("other-name",))])
        subst("other-name")
        assert subst.hits == []

    def test_hits_accumulate_across_calls(self, subst: Substituter) -> None:
        subst("spikeinterface", where="a")
        subst("tools and spikeinterface", where="b")
        assert sorted((h.where, h.other_repo) for h in subst.hits) == [
            ("a", "spikeinterface"),
            ("b", "shared"),
            ("b", "spikeinterface"),
        ]

    def test_extra_var_containing_an_alias_wins(self) -> None:
        own = Identity(name="neuroconv", aliases=("neuroconv",), extra={"docs": "neuroconv.readthedocs.io"})
        subst = Substituter(own)
        assert subst("see neuroconv.readthedocs.io and neuroconv") == "see {{docs}} and {{name}}"

    def test_excerpt_is_a_one_line_window(self, subst: Substituter) -> None:
        text = "x" * 60 + "\n spikeinterface \n" + "y" * 60
        subst(text)
        (hit,) = subst.hits
        assert "\n" not in hit.excerpt
        assert hit.excerpt.startswith("...")
        assert hit.excerpt.endswith("...")
        assert "spikeinterface" in hit.excerpt

    def test_disabled_substitution_still_scans_for_stale(self) -> None:
        subst = Substituter(OWN, FOREIGN, enabled=False)
        assert subst("neuroconv and spikeinterface") == "neuroconv and spikeinterface"
        assert [h.other_repo for h in subst.hits] == ["spikeinterface"]
        assert subst.applied == set()

    def test_stale_disabled(self) -> None:
        subst = Substituter(OWN, FOREIGN, stale=False)
        assert subst("neuroconv and spikeinterface") == "{{name}} and spikeinterface"
        assert subst.hits == []

    def test_many(self, subst: Substituter) -> None:
        assert subst.many(["neuroconv", "plain", "spikeinterface"], where="list") == [
            "{{name}}",
            "plain",
            "spikeinterface",
        ]
        assert [h.where for h in subst.hits] == ["list"]

    def test_identity_without_org_branch_or_extra(self) -> None:
        subst = Substituter(Identity(name="x", aliases=("xxxx",)))
        assert subst("xxxx by someone on /blob/main/") == "{{name}} by someone on /blob/main/"

    def test_identity_without_aliases(self) -> None:
        subst = Substituter(Identity(name="x", aliases=(), org="acme"))
        assert subst("acme/anything") == "{{org}}/anything"

    def test_null_substituter(self) -> None:
        null = NullSubstituter()
        assert null("neuroconv catalystneuro", where="x") == "neuroconv catalystneuro"
        assert null.hits == []
        assert null.applied == set()

    def test_default_where_is_empty(self, subst: Substituter) -> None:
        subst("spikeinterface")
        assert subst.hits[0].where == ""
