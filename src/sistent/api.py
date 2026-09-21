"""Orchestrator: resolve repositories, probe identities, extract snapshots, compare, suppress, aggregate, report.

Every stage is a separate function so that the CLI commands (``check``, ``snapshot``, ``diff``, ``fetch``, ``repos``)
and library users can reuse the pieces they need. :func:`run` is the whole pipeline.
"""

from __future__ import annotations

import fnmatch
import traceback
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sistent import __version__
from sistent.aspects.base import Aspect, SnapshotMismatch, UnparseableFile
from sistent.baseline import Baseline, apply_baseline, read_baseline, write_baseline
from sistent.compare.text import Substituter
from sistent.config import Config, RepoSpec
from sistent.model import (
    AspectInfo,
    Direction,
    Finding,
    Identity,
    Kind,
    Report,
    RepoStatus,
    RunError,
    Severity,
    Snapshot,
    Subject,
    aggregate_candidates,
)
from sistent.registry import Registry, default_registry
from sistent.repository import RepoContext, Repository, build_identity
from sistent.sources import SourceError, resolve

Progress = Callable[[str], None]


class RunFailure(Exception):
    """The run could not start (nothing to compare against): exit code 2."""


class MainUnavailable(RunFailure):
    """The main repository could not be resolved."""


class SelectionError(RunFailure):
    """A ``--repo``/``--aspect`` filter matched nothing or named something unknown."""


@dataclass(frozen=True, kw_only=True)
class RunOptions:
    """Knobs of one ``check`` run (the CLI maps its flags onto this)."""

    fetch: bool = True
    jobs: int | None = None
    aspects: tuple[str, ...] = ()
    """Glob filters on aspect instance names; empty = all enabled aspects."""
    repos: tuple[str, ...] = ()
    """Glob filters on satellite names; empty = all satellites."""
    tags: tuple[str, ...] = ()
    """Only satellites carrying any of these tags."""
    fail_on: str | None = None
    """``info``/``warning``/``error``/``never`` overriding the config; ``None`` = use the config."""
    baseline: Path | None = None
    update_baseline: bool = False
    allow_unavailable: bool = False


@dataclass
class _Resolved:
    spec: RepoSpec
    repo: Repository | None
    identity: Identity | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.repo is not None


@dataclass
class _Extracted:
    snapshots: dict[tuple[str, str], Snapshot] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)
    errors: list[RunError] = field(default_factory=list)
    skipped_aspects: set[str] = field(default_factory=set)
    """Aspects whose main snapshot failed; they are compared nowhere."""


# ----- selection ---------------------------------------------------------------------------------------------------


def _matches(name: str, patterns: Sequence[str]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(name.casefold(), p.casefold()) for p in patterns)


def select_aspects(config: Config, registry: Registry, patterns: Sequence[str] = ()) -> list[Aspect]:
    """Instantiate the enabled aspects of ``config``, optionally filtered by name globs."""
    built: list[Aspect] = []
    for spec in config.aspects:
        if not spec.options.enabled:
            continue
        if not _matches(spec.name, patterns):
            continue
        built.append(registry.build(spec))
    if patterns:
        known = [spec.name for spec in config.aspects]
        for pattern in patterns:
            if not any(fnmatch.fnmatchcase(n.casefold(), pattern.casefold()) for n in known):
                raise SelectionError(f"--aspect {pattern!r} matches no configured aspect (known: {', '.join(known)})")
    return built


def select_satellites(config: Config, patterns: Sequence[str] = (), tags: Sequence[str] = ()) -> list[RepoSpec]:
    """Satellites of ``config`` filtered by name globs and tags."""
    selected = [
        spec for spec in config.satellites if _matches(spec.name, patterns) and (not tags or set(tags) & set(spec.tags))
    ]
    if patterns:
        known = [spec.name for spec in config.satellites]
        for pattern in patterns:
            if not any(fnmatch.fnmatchcase(n.casefold(), pattern.casefold()) for n in known):
                raise SelectionError(f"--repo {pattern!r} matches no configured satellite (known: {', '.join(known)})")
    return selected


# ----- resolution --------------------------------------------------------------------------------------------------


def resolve_repos(
    config: Config,
    specs: Sequence[RepoSpec],
    *,
    fetch: bool = True,
    jobs: int | None = None,
    progress: Progress | None = None,
) -> list[_Resolved]:
    """Materialise every spec (in parallel) and probe its identity. Failures become ``_Resolved.error``."""
    workers = max(1, jobs or config.jobs)

    def _one(spec: RepoSpec) -> _Resolved:
        if progress:
            progress(f"resolving {spec.name} ({spec.source})")
        try:
            repo = resolve(spec, cache_dir=config.cache_dir, fetch=fetch, timeout=config.git_timeout)
        except SourceError as exc:
            return _Resolved(spec=spec, repo=None, identity=None, error=str(exc))
        except Exception as exc:  # unexpected: keep the run alive, report it
            return _Resolved(spec=spec, repo=None, identity=None, error=f"{type(exc).__name__}: {exc}")
        identity = build_identity(repo, spec.hints)
        return _Resolved(spec=spec, repo=repo, identity=identity, error=None)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(_one, specs))


def make_context(
    resolved: _Resolved,
    *,
    foreign: Iterable[Identity],
    is_main: bool,
    stale: bool,
) -> RepoContext:
    assert resolved.repo is not None
    assert resolved.identity is not None
    spec = resolved.spec
    subst = Substituter(
        resolved.identity,
        tuple(foreign),
        enabled=spec.substitute,
        stale=stale and spec.substitute,
    )
    return RepoContext(repo=resolved.repo, identity=resolved.identity, is_main=is_main, subst=subst)


# ----- extraction --------------------------------------------------------------------------------------------------


def _targets(aspect: Aspect, spec: RepoSpec) -> bool:
    return aspect.name not in spec.skip_aspects and aspect.targets(spec.tags)


def extract_all(
    aspects: Sequence[Aspect],
    main: _Resolved,
    satellites: Sequence[_Resolved],
    *,
    stale: bool,
    jobs: int,
    progress: Progress | None = None,
) -> _Extracted:
    """Extract every (repo, aspect) snapshot in parallel.

    A parse failure in a satellite becomes an ``unparseable`` finding for that repo and aspect; a parse failure or
    any other error in main disables the aspect for the whole run (a ``RunError``).
    """
    out = _Extracted()
    available = [r for r in satellites if r.ok]
    identities = {r.spec.name: r.identity for r in (main, *available) if r.identity is not None}

    jobs_list: list[tuple[_Resolved, Aspect, bool]] = []
    for aspect in aspects:
        if not _targets(aspect, main.spec):
            out.skipped_aspects.add(aspect.name)
            out.errors.append(
                RunError(
                    stage="extract",
                    repo=main.spec.name,
                    aspect=aspect.name,
                    message=f"aspect {aspect.name!r} is skipped for main ({main.spec.name}); nothing to compare",
                )
            )
            continue
        jobs_list.append((main, aspect, True))
        for sat in available:
            if _targets(aspect, sat.spec):
                jobs_list.append((sat, aspect, False))

    def _one(job: tuple[_Resolved, Aspect, bool]) -> tuple[_Resolved, Aspect, bool, Snapshot | Exception]:
        resolved, aspect, is_main = job
        if progress:
            progress(f"extracting {aspect.name} from {resolved.spec.name}")
        assert resolved.identity is not None
        foreign = [i for name, i in identities.items() if name != resolved.spec.name]
        ctx = make_context(resolved, foreign=foreign, is_main=is_main, stale=stale)
        try:
            return resolved, aspect, is_main, aspect.run_extract(ctx)
        except Exception as exc:
            return resolved, aspect, is_main, exc

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = list(pool.map(_one, jobs_list))

    # main first: an aspect whose main snapshot failed is skipped for every repo, so satellite results are dropped
    results.sort(key=lambda item: not item[2])
    for resolved, aspect, is_main, result in results:
        name = resolved.spec.name
        if not is_main and aspect.name in out.skipped_aspects:
            continue
        if isinstance(result, Snapshot):
            out.snapshots[(name, aspect.name)] = result
            continue
        if is_main:
            out.skipped_aspects.add(aspect.name)
            out.errors.append(
                RunError(
                    stage="extract",
                    repo=name,
                    aspect=aspect.name,
                    message=f"{type(result).__name__}: {result}",
                    traceback=None if isinstance(result, UnparseableFile) else _tb(result),
                )
            )
        elif isinstance(result, UnparseableFile):
            out.findings.append(
                aspect.finding(
                    repo=name,
                    kind=Kind.UNPARSEABLE,
                    subject=Subject.FILE,
                    locator=result.rel,
                    message=f"could not parse {result.rel}: {result.reason}",
                    detail=result.reason,
                    detail_kind="text",
                    content_key=result.rel,
                )
            )
        else:
            out.errors.append(
                RunError(
                    stage="extract",
                    repo=name,
                    aspect=aspect.name,
                    message=f"{type(result).__name__}: {result}",
                    traceback=_tb(result),
                )
            )
    return out


def _tb(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# ----- comparison --------------------------------------------------------------------------------------------------


def compare_all(
    aspects: Sequence[Aspect],
    main_name: str,
    satellite_names: Sequence[str],
    extracted: _Extracted,
) -> tuple[list[Finding], list[RunError]]:
    """Self-check main, then compare every satellite snapshot against main's, aspect by aspect."""
    findings: list[Finding] = []
    errors: list[RunError] = []
    for aspect in aspects:
        if aspect.name in extracted.skipped_aspects:
            continue
        main_snap = extracted.snapshots.get((main_name, aspect.name))
        if main_snap is None:
            continue
        try:
            findings.extend(aspect.self_check(main_snap))
        except Exception as exc:
            errors.append(
                RunError(stage="compare", repo=main_name, aspect=aspect.name, message=str(exc), traceback=_tb(exc))
            )
        for name in satellite_names:
            other = extracted.snapshots.get((name, aspect.name))
            if other is None:
                continue
            try:
                aspect.check_versions(main_snap, other)
                findings.extend(aspect.compare(main_snap, other))
                findings.extend(aspect.self_check(other))
            except SnapshotMismatch as exc:
                errors.append(RunError(stage="compare", repo=name, aspect=aspect.name, message=str(exc)))
            except Exception as exc:
                errors.append(
                    RunError(stage="compare", repo=name, aspect=aspect.name, message=str(exc), traceback=_tb(exc))
                )
    return findings, errors


def stale_findings(aspects: Sequence[Aspect], snapshots: dict[tuple[str, str], Snapshot]) -> list[Finding]:
    """One ``stale`` finding per ``(repo, aspect, where, alias)`` recorded during extraction (main included)."""
    by_name = {a.name: a for a in aspects}
    out: list[Finding] = []
    seen: set[tuple[str, str, str, str]] = set()
    for (repo, aspect_name), snap in snapshots.items():
        aspect = by_name.get(aspect_name)
        if aspect is None:
            continue
        for hit in snap.stale_hits:
            where = hit.where or (snap.sources[0] if snap.sources else aspect_name)
            key = (repo, aspect_name, where, hit.alias.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(
                aspect.finding(
                    repo=repo,
                    kind=Kind.STALE,
                    subject=Subject.REFERENCE,
                    locator=where,
                    message=f"mentions '{hit.alias}' (repo {hit.other_repo}) - copy-paste leftover?",
                    detail=hit.excerpt,
                    detail_kind="text",
                    content_key=hit.alias.casefold(),
                    direction=Direction.NONE,
                )
            )
    return out


# ----- suppression, baseline, exit code ----------------------------------------------------------------------------


def _glob_hits(aspect: str, locator: str, patterns: Iterable[str]) -> bool:
    qualified = f"{aspect}:{locator}".casefold()
    bare = locator.casefold()
    for pattern in patterns:
        p = pattern.casefold()
        if fnmatch.fnmatchcase(qualified, p) or fnmatch.fnmatchcase(bare, p):
            return True
    return False


def suppress(findings: Iterable[Finding], config: Config, aspects: Sequence[Aspect]) -> list[Finding]:
    """Mark findings matching ``[repos.X].ignore`` or ``[aspects.Y].ignore`` globs as suppressed."""
    aspect_ignores = {a.name: tuple(a.options.ignore) for a in aspects}
    out: list[Finding] = []
    for f in findings:
        repo_spec = config.repos.get(f.repo)
        repo_patterns = repo_spec.ignore if repo_spec else ()
        if _glob_hits(f.aspect, f.locator, repo_patterns) or _glob_hits(
            f.aspect, f.locator, aspect_ignores.get(f.aspect, ())
        ):
            f = f.replace(suppressed=True)
        out.append(f)
    return out


def _parse_fail_on(value: str | None, config: Config) -> Severity | None:
    if value is None:
        return config.fail_on
    if value.lower() == "never":
        return None
    return Severity.parse(value)


def compute_exit(
    findings: Sequence[Finding],
    errors: Sequence[RunError],
    *,
    fail_on: Severity | None,
    unavailable: int,
    allow_unavailable: bool,
) -> tuple[int, str]:
    """Exit code and one-line reason (see the CLI exit-code table)."""
    gating = [f for f in findings if f.gates]
    over = [f for f in gating if fail_on is not None and f.severity.rank >= fail_on.rank]
    threshold = fail_on.value if fail_on else "never"
    reasons: list[str] = []
    code = 0
    if errors:
        code = 1
        reasons.append(f"{len(errors)} run error{'s' if len(errors) != 1 else ''}")
    if over:
        code = 1
        worst = max(over, key=lambda f: f.severity.rank).severity.value
        reasons.append(
            f"{len(over)} finding{'s' if len(over) != 1 else ''} at or above fail_on = {threshold} (worst: {worst})"
        )
    if unavailable and not allow_unavailable:
        code = 3
        reasons.append(f"{unavailable} satellite{'s' if unavailable != 1 else ''} unavailable")
    elif unavailable:
        reasons.append(f"{unavailable} satellite{'s' if unavailable != 1 else ''} unavailable (allowed)")
    if code == 0:
        reasons.insert(0, "clean" if not gating else f"{len(gating)} findings below fail_on = {threshold}")
    return code, f"{' · '.join(reasons)} → exit {code}"


# ----- the pipeline ------------------------------------------------------------------------------------------------


def _status(resolved: _Resolved, *, is_main: bool) -> RepoStatus:
    repo = resolved.repo
    return RepoStatus(
        name=resolved.spec.name,
        is_main=is_main,
        source=resolved.spec.source,
        resolved=str(repo.root) if repo else None,
        rev=resolved.spec.rev,
        head=repo.head() if repo else None,
        status="ok" if repo else "unavailable",
        error=resolved.error,
        tags=tuple(resolved.spec.tags),
        identity=resolved.identity,
    )


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run(
    config: Config,
    *,
    options: RunOptions | None = None,
    registry: Registry | None = None,
    progress: Progress | None = None,
) -> Report:
    """Run the whole pipeline and build the :class:`~sistent.model.Report`.

    Raises :class:`RunFailure` (exit 2) when nothing can be compared: main unavailable, or a filter naming nothing.
    """
    options = options or RunOptions()
    registry = registry or default_registry()
    aspects = select_aspects(config, registry, options.aspects)
    satellite_specs = select_satellites(config, options.repos, options.tags)
    jobs = max(1, options.jobs or config.jobs)

    resolved = resolve_repos(
        config, [config.main_spec, *satellite_specs], fetch=options.fetch, jobs=jobs, progress=progress
    )
    main, satellites = resolved[0], resolved[1:]
    if not main.ok:
        raise MainUnavailable(f"main repository {main.spec.name!r} is unavailable: {main.error}")

    extracted = extract_all(aspects, main, satellites, stale=config.stale, jobs=jobs, progress=progress)
    available_names = [r.spec.name for r in satellites if r.ok]
    compared, compare_errors = compare_all(aspects, main.spec.name, available_names, extracted)

    findings: list[Finding] = [*extracted.findings, *compared]
    if config.stale:
        findings.extend(stale_findings(aspects, extracted.snapshots))
    findings = _order(findings, [main.spec.name, *available_names], [a.name for a in aspects])
    findings = suppress(findings, config, aspects)

    baseline_stale: list[str] = []
    baseline_path = options.baseline or config.baseline
    generated_at = now_iso()
    if baseline_path is not None:
        baseline: Baseline | None
        if options.update_baseline:
            baseline = write_baseline(baseline_path, findings, generated_at=generated_at)
        elif baseline_path.exists():
            baseline = read_baseline(baseline_path)
        else:
            baseline = None
        findings, baseline_stale = apply_baseline(findings, baseline)

    errors = [*extracted.errors, *compare_errors]
    fail_on = _parse_fail_on(options.fail_on, config)
    unavailable = [r for r in satellites if not r.ok]
    code, reason = compute_exit(
        findings, errors, fail_on=fail_on, unavailable=len(unavailable), allow_unavailable=options.allow_unavailable
    )

    totals: dict[str, int] = {}
    aspect_infos: list[AspectInfo] = []
    for aspect in aspects:
        targets = tuple(r.spec.name for r in satellites if r.ok and _targets(aspect, r.spec))
        totals[aspect.name] = len(targets)
        aspect_infos.append(
            AspectInfo(name=aspect.name, type=aspect.type_name, options=aspect.options_dict(), targets=targets)
        )

    return Report(
        sistent_version=__version__,
        generated_at=generated_at,
        config_path=str(config.path),
        main=_status(main, is_main=True),
        repos=tuple(_status(r, is_main=False) for r in satellites),
        aspects=tuple(aspect_infos),
        findings=tuple(findings),
        candidates=aggregate_candidates(findings, totals),
        errors=tuple(errors),
        fail_on=fail_on,
        exit_code=code,
        exit_reason=reason,
        snapshots=extracted.snapshots,
        baseline_stale=tuple(baseline_stale),
    )


def _order(findings: Sequence[Finding], repo_order: Sequence[str], aspect_order: Sequence[str]) -> list[Finding]:
    repo_rank = {name: i for i, name in enumerate(repo_order)}
    aspect_rank = {name: i for i, name in enumerate(aspect_order)}
    indexed = list(enumerate(findings))
    indexed.sort(
        key=lambda item: (
            repo_rank.get(item[1].repo, len(repo_rank)),
            aspect_rank.get(item[1].aspect, len(aspect_rank)),
            item[0],
        )
    )
    return [f for _, f in indexed]


# ----- helpers for the other commands ------------------------------------------------------------------------------


def extract_snapshots(
    config: Config,
    *,
    repos: Sequence[str],
    aspects: Sequence[str] = (),
    registry: Registry | None = None,
    fetch: bool = True,
    jobs: int | None = None,
    progress: Progress | None = None,
) -> tuple[dict[tuple[str, str], Snapshot], list[RunError]]:
    """Snapshots of the named repos (main allowed) for ``snapshot`` / ``diff``; stale detection uses all repos."""
    registry = registry or default_registry()
    built = select_aspects(config, registry, aspects)
    specs: list[RepoSpec] = []
    for name in repos:
        if name not in config.repos:
            raise SelectionError(f"unknown repo {name!r} (known: {', '.join(config.repos)})")
        specs.append(config.repos[name])
    resolved = resolve_repos(config, specs, fetch=fetch, jobs=jobs, progress=progress)
    errors: list[RunError] = []
    snapshots: dict[tuple[str, str], Snapshot] = {}
    identities = {r.spec.name: r.identity for r in resolved if r.identity is not None}
    for r in resolved:
        if not r.ok:
            errors.append(RunError(stage="resolve", repo=r.spec.name, aspect=None, message=r.error or "unavailable"))
            continue
        foreign = [i for name, i in identities.items() if name != r.spec.name]
        for aspect in built:
            if not _targets(aspect, r.spec):
                continue
            ctx = make_context(r, foreign=foreign, is_main=r.spec.name == config.main, stale=config.stale)
            try:
                snapshots[(r.spec.name, aspect.name)] = aspect.run_extract(ctx)
            except Exception as exc:
                errors.append(
                    RunError(
                        stage="extract", repo=r.spec.name, aspect=aspect.name, message=str(exc), traceback=_tb(exc)
                    )
                )
    return snapshots, errors


def repo_statuses(
    config: Config, *, fetch: bool = False, jobs: int | None = None, progress: Progress | None = None
) -> list[RepoStatus]:
    """Status of every configured repo (``repos`` / ``fetch`` commands)."""
    resolved = resolve_repos(config, list(config.repos.values()), fetch=fetch, jobs=jobs, progress=progress)
    return [_status(r, is_main=r.spec.name == config.main) for r in resolved]


def snapshot_text(snapshot: Snapshot, *, explain: bool = False) -> str:
    """Human-readable rendering of a snapshot for ``snapshot --format text`` and ``diff``."""
    import json

    return json.dumps(snapshot.to_dict(explain=explain), indent=2, ensure_ascii=False, sort_keys=True, default=str)


__all__: list[str] = [
    "MainUnavailable",
    "RunFailure",
    "RunOptions",
    "SelectionError",
    "compare_all",
    "compute_exit",
    "extract_all",
    "extract_snapshots",
    "make_context",
    "repo_statuses",
    "resolve_repos",
    "run",
    "select_aspects",
    "select_satellites",
    "snapshot_text",
    "stale_findings",
    "suppress",
]
