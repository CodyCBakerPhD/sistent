"""Tests for the :class:`Aspect` base class: extraction provenance, finding derivation and helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from sistent.aspects.base import (
    DEFAULT_DIRECTION,
    DEFAULT_SEVERITY,
    DEFAULT_SUBJECT_SEVERITY,
    Aspect,
    SnapshotMismatch,
    UnparseableFile,
)
from sistent.compare.text import NullSubstituter
from sistent.model import Direction, Finding, Identity, Kind, Severity, Snapshot, StaleHit, Subject, locator
from sistent.options import BaseOptions, options_to_dict
from sistent.repository import RepoContext, Repository
from tests.conftest import MakeRepo, git, requires_git, write_files


@dataclass(frozen=True, kw_only=True)
class EchoOptions(BaseOptions):
    path: str = field(default="README.md", metadata={"help": "File to echo."})
    threshold: float = field(default=0.6, metadata={"help": "Unused knob, only hashed."})


class EchoAspect(Aspect):
    """Reads one file through the context and stores its substituted text."""

    type_name = "echo"
    options_cls = EchoOptions
    description = "Echoes one file after identity substitution."

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        path: str = self.options.path  # type: ignore[attr-defined]
        if not ctx.exists(path):
            ctx.ignored.append(f"{path} (absent)")
            return {"text": None}
        return {"text": ctx.substitute(ctx.read_text(path), where=locator(path))}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        return []


class EchoV2(EchoAspect):
    schema_version = 2


class StrictEcho(EchoAspect):
    default_severity = {"missing": "error", "moved": "warning", "extra": "error"}


class LenientEcho(EchoAspect):
    default_severity = {"missing": "info"}


class PrecommitLikeEcho(EchoAspect):
    default_severity = {"missing.file": "warning"}


class MultiEcho(EchoAspect):
    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        return {"b": ctx.read_text("b.md"), "a": ctx.read_text("a.md"), "again": ctx.read_text("a.md")}


class GhostEcho(EchoAspect):
    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        ctx.sources.add("ghost.md")
        return {}


class BrokenEcho(EchoAspect):
    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        raise UnparseableFile("README.md", "bad front matter")


def echo(name: str = "echo", cls: type[EchoAspect] = EchoAspect, **options: Any) -> EchoAspect:
    return cls(name, EchoOptions(**options))


# ----- run_extract -------------------------------------------------------------------------------------------------


class TestRunExtract:
    def test_records_sources_and_provenance(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": "hello"}, name="alpha")
        snap = echo().run_extract(ctx)
        assert isinstance(snap, Snapshot)
        assert snap.aspect == "echo"
        assert snap.repo == "alpha"
        assert snap.schema_version == 1
        assert snap.data == {"text": "hello"}
        assert snap.sources == ("README.md",)
        assert re.fullmatch(r"[0-9a-f]{64}", snap.fingerprint)
        assert snap.head is None
        assert snap.aliases_applied == ()
        assert snap.ignored == ()
        assert snap.stale_hits == ()

    def test_sources_are_sorted_and_unique(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"a.md": "A", "b.md": "B"})
        snap = echo(cls=MultiEcho).run_extract(ctx)
        assert snap.sources == ("a.md", "b.md")
        assert snap.data == {"b": "B", "a": "A", "again": "A"}

    def test_fingerprint_is_stable_for_same_content_and_options(self, make_repo: MakeRepo) -> None:
        one = echo().run_extract(make_repo({"README.md": "same"}, name="one"))
        two = echo("other-name").run_extract(make_repo({"README.md": "same"}, name="two"))
        assert one.fingerprint == two.fingerprint, "repo name and aspect name are not part of the fingerprint"

    def test_fingerprint_changes_with_file_content(self, make_repo: MakeRepo) -> None:
        one = echo().run_extract(make_repo({"README.md": "one"}))
        two = echo().run_extract(make_repo({"README.md": "two"}))
        assert one.fingerprint != two.fingerprint

    def test_fingerprint_changes_when_a_source_is_added(self, make_repo: MakeRepo) -> None:
        with_file = echo().run_extract(make_repo({"README.md": "x"}))
        without = echo().run_extract(make_repo({}))
        assert with_file.fingerprint != without.fingerprint
        assert without.sources == ()

    def test_fingerprint_changes_with_options(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": "same"})
        base = echo().run_extract(ctx).fingerprint
        assert echo(threshold=0.7).run_extract(ctx).fingerprint != base
        assert echo(ignore=["x"]).run_extract(ctx).fingerprint != base
        assert echo(severity={"missing": "error"}).run_extract(ctx).fingerprint != base
        assert echo().run_extract(ctx).fingerprint == base

    def test_fingerprint_changes_with_schema_version(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": "same"})
        v1 = echo().run_extract(ctx)
        v2 = echo(cls=EchoV2).run_extract(ctx)
        assert v2.schema_version == 2
        assert v1.fingerprint != v2.fingerprint

    def test_fingerprint_survives_an_unreadable_source(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({})
        snap = echo(cls=GhostEcho).run_extract(ctx)
        assert snap.sources == ("ghost.md",)
        assert re.fullmatch(r"[0-9a-f]{64}", snap.fingerprint)

    def test_head_is_none_without_git(self, make_repo: MakeRepo) -> None:
        assert echo().run_extract(make_repo({"README.md": "x"})).head is None

    @requires_git
    def test_head_is_recorded_in_a_git_repo(self, tmp_path: Path) -> None:
        write_files(tmp_path, {"README.md": "x"})
        git("init", "-q", cwd=tmp_path)
        git("add", "-A", cwd=tmp_path)
        git("commit", "-q", "-m", "initial", cwd=tmp_path)
        sha = git("rev-parse", "HEAD", cwd=tmp_path).strip()
        ctx = RepoContext(
            repo=Repository("r", tmp_path),
            identity=Identity(name="r", aliases=()),
            is_main=False,
            subst=NullSubstituter(),
        )
        assert echo().run_extract(ctx).head == sha

    def test_aliases_applied_populated(self, make_repo: MakeRepo) -> None:
        ctx = make_repo({"README.md": "Install neuroconv; NeuroConv rocks"}, name="neuroconv")
        snap = echo().run_extract(ctx)
        assert snap.data == {"text": "Install {{name}}; {{name}} rocks"}
        assert snap.aliases_applied == ("NeuroConv", "neuroconv")

    def test_stale_hits_carried(self, make_repo: MakeRepo) -> None:
        main = make_repo({}, name="neuroconv", is_main=True)
        sat = make_repo({"README.md": "see neuroconv"}, name="satellite", foreign=[main.identity])
        snap = echo().run_extract(sat)
        assert snap.data == {"text": "see neuroconv"}
        assert snap.stale_hits == (
            StaleHit(where="README.md", alias="neuroconv", other_repo="neuroconv", excerpt="see neuroconv"),
        )

    def test_ignored_carried(self, make_repo: MakeRepo) -> None:
        snap = echo(path="MISSING.md").run_extract(make_repo({"README.md": "x"}))
        assert snap.data == {"text": None}
        assert snap.sources == ()
        assert snap.ignored == ("MISSING.md (absent)",)

    def test_unparseable_file_propagates(self, make_repo: MakeRepo) -> None:
        with pytest.raises(UnparseableFile, match=r"^README\.md: bad front matter$") as info:
            echo(cls=BrokenEcho).run_extract(make_repo({"README.md": "x"}))
        assert info.value.rel == "README.md"
        assert info.value.reason == "bad front matter"


# ----- finding() ---------------------------------------------------------------------------------------------------


def make(aspect: Aspect, **overrides: Any) -> Finding:
    args: dict[str, Any] = {
        "repo": "roiextractors",
        "kind": Kind.MISSING,
        "subject": Subject.SECTION,
        "locator": "README.md#Installation",
        "message": "section missing",
    }
    args.update(overrides)
    return aspect.finding(**args)


class TestFindingDirection:
    @pytest.mark.parametrize(
        ("kind", "direction"),
        [
            (Kind.MISSING, Direction.DOWNSTREAM),
            (Kind.EXTRA, Direction.UPSTREAM),
            (Kind.DIFFERS, Direction.NONE),
            (Kind.MOVED, Direction.DOWNSTREAM),
            (Kind.REORDERED, Direction.DOWNSTREAM),
            (Kind.STALE, Direction.NONE),
            (Kind.UNPARSEABLE, Direction.NONE),
        ],
    )
    def test_default_per_kind(self, kind: Kind, direction: Direction) -> None:
        assert make(echo(), kind=kind).direction is direction
        assert DEFAULT_DIRECTION[kind] is direction

    def test_table_covers_every_kind(self) -> None:
        assert set(DEFAULT_DIRECTION) == set(Kind)
        assert set(DEFAULT_SEVERITY) == set(Kind)

    def test_explicit_direction_is_kept(self) -> None:
        f = make(echo(), kind=Kind.DIFFERS, direction=Direction.DOWNSTREAM)
        assert f.direction is Direction.DOWNSTREAM
        assert f.severity is Severity.WARNING

    def test_fields_pass_through(self) -> None:
        f = make(
            echo("readme"),
            detail="- a\n+ b",
            detail_kind="diff",
            content_key="installation",
            option="aspects.readme.threshold=0.6",
        )
        assert f.aspect == "readme"
        assert f.repo == "roiextractors"
        assert f.kind is Kind.MISSING
        assert f.subject is Subject.SECTION
        assert f.locator == "README.md#Installation"
        assert f.message == "section missing"
        assert f.detail == "- a\n+ b"
        assert f.detail_kind == "diff"
        assert f.content_key == "installation"
        assert f.option == "aspects.readme.threshold=0.6"
        assert not f.suppressed
        assert not f.baselined
        assert f.id
        assert f.candidate_key


class TestFindingSeverity:
    @pytest.mark.parametrize("kind", list(Kind))
    def test_global_default_per_kind(self, kind: Kind) -> None:
        f = make(echo(), kind=kind, subject=Subject.SECTION)
        expected = Severity.INFO if f.direction is Direction.UPSTREAM else DEFAULT_SEVERITY[kind]
        assert f.severity is expected

    def test_missing_file_is_an_error_by_default(self) -> None:
        assert DEFAULT_SUBJECT_SEVERITY == {"missing.file": Severity.ERROR}
        assert make(echo(), subject=Subject.FILE).severity is Severity.ERROR
        assert make(echo(), subject=Subject.SECTION).severity is Severity.WARNING
        assert echo().severity_for(Kind.MISSING, Subject.FILE) is Severity.ERROR

    @pytest.mark.parametrize(
        ("aspect", "kwargs"),
        [
            (echo(severity={"extra": "error", "extra.section": "error"}), {"kind": Kind.EXTRA}),
            (echo(), {"kind": Kind.EXTRA, "severity": Severity.ERROR}),
            (echo(cls=StrictEcho), {"kind": Kind.EXTRA}),
            (echo(severity={"missing": "error"}), {"kind": Kind.MISSING, "direction": Direction.UPSTREAM}),
            (echo(), {"kind": Kind.MISSING, "subject": Subject.FILE, "direction": Direction.UPSTREAM}),
        ],
    )
    def test_upstream_is_pinned_to_info(self, aspect: Aspect, kwargs: dict[str, Any]) -> None:
        f = make(aspect, **kwargs)
        assert f.direction is Direction.UPSTREAM
        assert f.severity is Severity.INFO

    def test_user_kind_subject_beats_user_kind(self) -> None:
        aspect = echo(severity={"missing": "info", "missing.rule": "error"})
        assert make(aspect, subject=Subject.RULE).severity is Severity.ERROR
        assert make(aspect, subject=Subject.SECTION).severity is Severity.INFO

    def test_user_kind_beats_explicit_default(self) -> None:
        aspect = echo(severity={"missing": "info"})
        assert make(aspect, severity=Severity.ERROR).severity is Severity.INFO

    def test_user_table_beats_subject_default(self) -> None:
        aspect = echo(severity={"missing.file": "info"})
        assert make(aspect, subject=Subject.FILE).severity is Severity.INFO
        assert make(echo(severity={"missing": "info"}), subject=Subject.FILE).severity is Severity.INFO

    def test_explicit_default_beats_type_default(self) -> None:
        aspect = echo(cls=StrictEcho)
        assert make(aspect).severity is Severity.ERROR, "type default applies without an explicit default"
        assert make(aspect, severity=Severity.INFO).severity is Severity.INFO

    def test_explicit_default_beats_global_defaults(self) -> None:
        assert make(echo(), severity=Severity.INFO).severity is Severity.INFO
        assert make(echo(), subject=Subject.FILE, severity=Severity.WARNING).severity is Severity.WARNING

    def test_type_default_beats_global_subject_default(self) -> None:
        assert make(echo(cls=LenientEcho), subject=Subject.FILE).severity is Severity.INFO
        assert make(echo(cls=PrecommitLikeEcho), subject=Subject.FILE).severity is Severity.WARNING
        assert make(echo(cls=PrecommitLikeEcho), subject=Subject.SECTION).severity is Severity.WARNING

    def test_type_default_kind_subject_beats_type_default_kind(self) -> None:
        class Both(EchoAspect):
            default_severity = {"missing": "error", "missing.rule": "info"}

        assert make(echo(cls=Both), subject=Subject.RULE).severity is Severity.INFO
        assert make(echo(cls=Both), subject=Subject.SECTION).severity is Severity.ERROR

    def test_type_default_only_affects_listed_kinds(self) -> None:
        assert make(echo(cls=StrictEcho), kind=Kind.MOVED).severity is Severity.WARNING
        assert make(echo(cls=StrictEcho), kind=Kind.STALE).severity is Severity.WARNING
        assert make(echo(cls=StrictEcho), kind=Kind.REORDERED).severity is Severity.INFO

    def test_user_severity_values_are_case_insensitive(self) -> None:
        assert make(echo(severity={"missing": "ERROR"})).severity is Severity.ERROR


# ----- helpers -----------------------------------------------------------------------------------------------------


class TestHelpers:
    def test_class_attributes(self) -> None:
        aspect = echo("readme")
        assert aspect.name == "readme"
        assert aspect.type_name == "echo"
        assert aspect.options_cls is EchoOptions
        assert aspect.schema_version == 1
        assert aspect.description == "Echoes one file after identity substitution."
        assert Aspect.default_severity == {}
        assert repr(aspect) == "EchoAspect('readme')"

    def test_base_class_is_abstract(self) -> None:
        with pytest.raises(TypeError, match="abstract"):
            Aspect("x", BaseOptions())  # type: ignore[abstract]

    @pytest.mark.parametrize(
        ("wanted", "tags", "expected"),
        [
            (None, (), True),
            (None, ("python",), True),
            (["python"], ("python", "docs"), True),
            (["python", "rust"], ("rust",), True),
            (["python"], ("docs",), False),
            (["python"], (), False),
            ([], ("python",), False),
        ],
    )
    def test_targets(self, wanted: list[str] | None, tags: tuple[str, ...], expected: bool) -> None:
        assert echo(tags=wanted).targets(tags) is expected
        assert echo(tags=wanted).targets(iter(tags)) is expected

    def test_check_versions(self, make_snapshot: Any) -> None:
        main = make_snapshot("echo", "main", {})
        ok = make_snapshot("echo", "sat", {})
        echo().check_versions(main, ok)
        stale = make_snapshot("echo", "sat", {}, schema_version=2)
        with pytest.raises(
            SnapshotMismatch, match=r"^echo: snapshot schema versions differ \(main=1, sat=2, aspect=1\)$"
        ):
            echo().check_versions(main, stale)
        with pytest.raises(SnapshotMismatch, match=r"aspect=2"):
            echo(cls=EchoV2).check_versions(main, ok)
        assert issubclass(SnapshotMismatch, Exception)

    def test_option_ref(self) -> None:
        aspect = echo("readme", threshold=0.6, ignore=["a", "b"])
        assert aspect.option_ref("threshold") == "aspects.readme.threshold=0.6"
        assert aspect.option_ref("path") == "aspects.readme.path=README.md"
        assert aspect.option_ref("ignore") == "aspects.readme.ignore=['a', 'b']"
        assert aspect.option_ref("enabled") == "aspects.readme.enabled=True"
        assert aspect.option_ref("does_not_exist") == "aspects.readme.does_not_exist=None"

    def test_options_dict(self) -> None:
        aspect = echo(threshold=0.9, tags=["python"])
        d = aspect.options_dict()
        assert d == options_to_dict(aspect.options)
        assert d["threshold"] == 0.9
        assert d["tags"] == ["python"]
        d["tags"].append("x")
        assert aspect.options.tags == ["python"]

    def test_self_check_default_is_empty(self, make_snapshot: Any) -> None:
        assert echo().self_check(make_snapshot("echo", "main", {})) == []

    def test_compare_of_echo_is_empty(self, make_snapshot: Any) -> None:
        assert echo().compare(make_snapshot("echo", "main", {}), make_snapshot("echo", "sat", {})) == []

    def test_unparseable_file_attributes(self) -> None:
        exc = UnparseableFile(".github/workflows/ci.yml", "mapping values are not allowed here")
        assert exc.rel == ".github/workflows/ci.yml"
        assert exc.reason == "mapping values are not allowed here"
        assert str(exc) == ".github/workflows/ci.yml: mapping values are not allowed here"
        assert isinstance(exc, Exception)
