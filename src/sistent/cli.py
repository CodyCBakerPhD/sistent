"""Command-line interface: a thin click layer over :mod:`sistent.api`, :mod:`sistent.config` and the renderers.

Progress and diagnostics go to stderr; stdout carries only the report (or the requested data).
"""

from __future__ import annotations

import difflib
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import click

from sistent import __version__, api
from sistent.baseline import BaselineError
from sistent.config import Config, ConfigError, default_config_text, dump_config, find_config, load_config
from sistent.model import Severity, Snapshot
from sistent.options import describe_options
from sistent.registry import Registry, RegistryError, default_registry
from sistent.render import RenderOptions, render
from sistent.repository import Repository

EXIT_USAGE = 2
EXIT_PARTIAL = 3


@dataclass
class State:
    config_path: Path | None
    quiet: bool
    verbose: int
    color: bool

    def progress(self, message: str) -> None:
        if not self.quiet:
            click.echo(message, err=True)


def _fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    click.echo(f"error: {message}", err=True)
    sys.exit(code)


def _load(state: State, *, registry: Registry | None = None, config_path: Path | None = None) -> Config:
    try:
        path = find_config(config_path or state.config_path)
        return load_config(path, registry=registry)
    except (ConfigError, RegistryError) as exc:
        _fail(str(exc))


def _registry() -> Registry:
    try:
        return default_registry()
    except RegistryError as exc:
        _fail(str(exc))


def _want_color(no_color: bool) -> bool:
    if no_color or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


_CONFIG_HELP = "Config file (default: $SISTENT_CONFIG, else sistent.toml searched upward from the current directory)."


def config_option(command: Any) -> Any:
    """``-c/--config`` on a sub-command (the group accepts it too)."""
    return click.option(
        "-c",
        "--config",
        "config_path",
        type=click.Path(path_type=Path, dir_okay=False),
        default=None,
        help=_CONFIG_HELP,
    )(command)


# ----- group -------------------------------------------------------------------------------------------------------


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="sistent")
@click.option(
    "-c",
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=_CONFIG_HELP,
)
@click.option("-q", "--quiet", is_flag=True, help="No progress output on stderr.")
@click.option("-v", "--verbose", count=True, help="-v cites the option behind each finding; -vv adds tracebacks.")
@click.option("--no-color", is_flag=True, help="Disable ANSI colours (also honours $NO_COLOR).")
@click.pass_context
def main(ctx: click.Context, config_path: Path | None, quiet: bool, verbose: int, no_color: bool) -> None:
    """Keep a fleet of repositories consistent with one main repository."""
    ctx.obj = State(config_path=config_path, quiet=quiet, verbose=verbose, color=_want_color(no_color))


# ----- check -------------------------------------------------------------------------------------------------------


@main.command()
@config_option
@click.option("--format", "fmt", type=click.Choice(["text", "markdown", "json"]), default="text", show_default=True)
@click.option("-o", "--output", type=click.Path(path_type=Path, dir_okay=False), default=None, help="Write to FILE.")
@click.option("--aspect", "aspects", multiple=True, help="Only these aspects (glob, repeatable).")
@click.option("--repo", "repos", multiple=True, help="Only these satellites (glob, repeatable).")
@click.option("--tag", "tags", multiple=True, help="Only satellites with any of these tags (repeatable).")
@click.option(
    "--fail-on", type=click.Choice(["info", "warning", "error", "never"]), default=None, help="Override the config."
)
@click.option(
    "--min-severity",
    type=click.Choice(["info", "warning", "error"]),
    default="info",
    show_default=True,
    help="Display filter.",
)
@click.option(
    "--direction", type=click.Choice(["downstream", "upstream", "both"]), default="downstream", show_default=True
)
@click.option("--kind", "kinds", multiple=True, help="Display only these finding kinds (repeatable).")
@click.option("--diff", "full_detail", is_flag=True, help="Show full diffs/lists instead of truncated details.")
@click.option("--summary", "summary_only", is_flag=True, help="Summary table only.")
@click.option("--group-by", type=click.Choice(["repo", "aspect"]), default="repo", show_default=True)
@click.option("--sort", type=click.Choice(["config", "severity"]), default="config", show_default=True)
@click.option("--no-fetch", is_flag=True, envvar="SISTENT_NO_FETCH", help="Use cached clones only.")
@click.option("--no-cache", is_flag=True, hidden=True, help="Reserved.")
@click.option("--jobs", type=int, default=None, help="Parallel workers.")
@click.option("--baseline", type=click.Path(path_type=Path, dir_okay=False), default=None, help="Baseline file.")
@click.option("--update-baseline", is_flag=True, help="Rewrite the baseline with the current findings.")
@click.option("--show-baseline", is_flag=True, help="Show baselined findings.")
@click.option("--show-suppressed", is_flag=True, help="Show suppressed findings.")
@click.option("--allow-unavailable", is_flag=True, help="Do not exit 3 when a satellite is unavailable.")
@click.option("--no-candidates", is_flag=True, help="Omit the 'Candidates for main' section.")
@click.pass_obj
def check(
    state: State,
    config_path: Path | None,
    fmt: str,
    output: Path | None,
    aspects: tuple[str, ...],
    repos: tuple[str, ...],
    tags: tuple[str, ...],
    fail_on: str | None,
    min_severity: str,
    direction: str,
    kinds: tuple[str, ...],
    full_detail: bool,
    summary_only: bool,
    group_by: str,
    sort: str,
    no_fetch: bool,
    no_cache: bool,
    jobs: int | None,
    baseline: Path | None,
    update_baseline: bool,
    show_baseline: bool,
    show_suppressed: bool,
    allow_unavailable: bool,
    no_candidates: bool,
) -> None:
    """Compare every satellite against main and print the report.

    Exit codes: 0 clean, 1 findings at/above fail_on or run errors, 2 usage/config error or main unavailable,
    3 one or more satellites unavailable (others are still reported).
    """
    registry = _registry()
    config = _load(state, registry=registry, config_path=config_path)
    options = api.RunOptions(
        fetch=not no_fetch,
        jobs=jobs,
        aspects=aspects,
        repos=repos,
        tags=tags,
        fail_on=fail_on,
        baseline=baseline,
        update_baseline=update_baseline,
        allow_unavailable=allow_unavailable,
    )
    try:
        report = api.run(config, options=options, registry=registry, progress=state.progress)
    except (api.RunFailure, BaselineError, RegistryError) as exc:
        _fail(str(exc))
    if update_baseline:
        target = baseline or config.baseline
        state.progress(f"baseline written: {target} ({sum(1 for f in report.findings if f.baselined)} findings)")
    render_options = RenderOptions(
        direction=direction,  # type: ignore[arg-type]
        min_severity=Severity.parse(min_severity),
        kinds=frozenset(kinds),
        full_detail=full_detail,
        summary_only=summary_only,
        group_by=group_by,  # type: ignore[arg-type]
        sort=sort,  # type: ignore[arg-type]
        show_baseline=show_baseline,
        show_suppressed=show_suppressed,
        verbose=state.verbose,
        color=state.color and output is None and fmt == "text",
        hide_candidates=no_candidates,
    )
    text = render(report, fmt, render_options)  # type: ignore[arg-type]
    _emit(text, output)
    sys.exit(report.exit_code)


def _emit(text: str, output: Path | None) -> None:
    if output is None:
        click.echo(text, nl=not text.endswith("\n"))
    else:
        output.write_text(text, encoding="utf-8")


# ----- snapshot / diff ---------------------------------------------------------------------------------------------


@main.command()
@config_option
@click.argument("repo")
@click.option("--aspect", "aspects", multiple=True, help="Only these aspects (glob, repeatable).")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="json", show_default=True)
@click.option("--explain", is_flag=True, help="Include applied aliases, ignored sections and stale hits.")
@click.option("--no-fetch", is_flag=True, envvar="SISTENT_NO_FETCH", help="Use cached clones only.")
@click.pass_obj
def snapshot(
    state: State,
    config_path: Path | None,
    repo: str,
    aspects: tuple[str, ...],
    fmt: str,
    explain: bool,
    no_fetch: bool,
) -> None:
    """Print the normalised snapshot(s) extracted from REPO (what the comparison actually sees)."""
    registry = _registry()
    config = _load(state, registry=registry, config_path=config_path)
    try:
        snapshots, errors = api.extract_snapshots(
            config, repos=[repo], aspects=aspects, registry=registry, fetch=not no_fetch, progress=state.progress
        )
    except api.RunFailure as exc:
        _fail(str(exc))
    for error in errors:
        click.echo(f"error: {error.stage} {error.aspect or ''}: {error.message}", err=True)
    ordered = [snapshots[key] for key in sorted(snapshots, key=lambda k: _aspect_rank(config, k[1]))]
    if fmt == "json":
        payload: Any = [s.to_dict(explain=explain) for s in ordered]
        if len(payload) == 1:
            payload = payload[0]
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        for snap in ordered:
            click.echo(f"== {snap.repo} / {snap.aspect} (sources: {', '.join(snap.sources) or 'none'})")
            click.echo(api.snapshot_text(snap, explain=explain))
    if errors and not snapshots:
        sys.exit(1)


def _aspect_rank(config: Config, name: str) -> int:
    for i, spec in enumerate(config.aspects):
        if spec.name == name:
            return i
    return len(config.aspects)


@main.command()
@config_option
@click.argument("repo")
@click.argument("repo2", required=False)
@click.option("--aspect", "aspects", multiple=True, help="Only these aspects (glob, repeatable).")
@click.option("--raw", is_flag=True, help="Diff the source files instead of the normalised snapshots.")
@click.option("--no-fetch", is_flag=True, envvar="SISTENT_NO_FETCH", help="Use cached clones only.")
@click.pass_obj
def diff(
    state: State,
    config_path: Path | None,
    repo: str,
    repo2: str | None,
    aspects: tuple[str, ...],
    raw: bool,
    no_fetch: bool,
) -> None:
    """Unified diff of main's normalised snapshot against REPO's (or REPO against REPO2)."""
    registry = _registry()
    config = _load(state, registry=registry, config_path=config_path)
    left, right = (repo, repo2) if repo2 else (config.main, repo)
    try:
        snapshots, errors = api.extract_snapshots(
            config, repos=[left, right], aspects=aspects, registry=registry, fetch=not no_fetch, progress=state.progress
        )
    except api.RunFailure as exc:
        _fail(str(exc))
    for error in errors:
        click.echo(f"error: {error.stage} {error.repo or ''} {error.aspect or ''}: {error.message}", err=True)
    names = [spec.name for spec in config.aspects if (left, spec.name) in snapshots or (right, spec.name) in snapshots]
    any_output = False
    for name in names:
        a = snapshots.get((left, name))
        b = snapshots.get((right, name))
        if raw:
            lines = _raw_diff(config, left, right, a, b, no_fetch=no_fetch, progress=state.progress)
        else:
            a_text = api.snapshot_text(a).splitlines() if a else []
            b_text = api.snapshot_text(b).splitlines() if b else []
            lines = list(
                difflib.unified_diff(a_text, b_text, fromfile=f"{left}/{name}", tofile=f"{right}/{name}", lineterm="")
            )
        if lines:
            any_output = True
            click.echo("\n".join(lines))
    if errors and not any_output:
        sys.exit(1)


def _raw_diff(
    config: Config,
    left: str,
    right: str,
    a: Snapshot | None,
    b: Snapshot | None,
    *,
    no_fetch: bool,
    progress: Callable[[str], None],
) -> list[str]:
    from sistent.api import resolve_repos

    resolved = {
        r.spec.name: r
        for r in resolve_repos(config, [config.repos[left], config.repos[right]], fetch=not no_fetch, progress=progress)
    }
    sources = sorted({*(a.sources if a else ()), *(b.sources if b else ())})
    out: list[str] = []
    for rel in sources:
        left_lines = _read_lines(resolved[left].repo, rel)
        right_lines = _read_lines(resolved[right].repo, rel)
        out.extend(
            difflib.unified_diff(
                left_lines, right_lines, fromfile=f"{left}/{rel}", tofile=f"{right}/{rel}", lineterm=""
            )
        )
    return out


def _read_lines(repo: Repository | None, rel: str) -> list[str]:
    if repo is None or not repo.exists(rel):
        return []
    return repo.read_text(rel).splitlines()


# ----- repos / fetch -----------------------------------------------------------------------------------------------


@main.command()
@config_option
@click.option("--fetch", is_flag=True, help="Materialise url repos before listing (default: cache only).")
@click.pass_obj
def repos(state: State, config_path: Path | None, fetch: bool) -> None:
    """List configured repositories, their sources, resolved paths, revisions and identity aliases."""
    config = _load(state, registry=_registry(), config_path=config_path)
    statuses = api.repo_statuses(config, fetch=fetch, progress=state.progress)
    rows = [("repo", "source", "status", "head", "tags", "aliases")]
    for s in statuses:
        name = f"{s.name} (main)" if s.is_main else s.name
        head = (s.head or "")[:7]
        aliases = ", ".join(s.identity.aliases) if s.identity else ""
        status = s.status if s.status == "ok" else f"{s.status}: {s.error}"
        rows.append((name, s.source, status, head, ",".join(s.tags), aliases))
    click.echo(_table(rows))
    if any(s.status != "ok" for s in statuses):
        sys.exit(EXIT_PARTIAL)


@main.command()
@config_option
@click.option("--repo", "names", multiple=True, help="Only these repos (repeatable).")
@click.pass_obj
def fetch(state: State, config_path: Path | None, names: tuple[str, ...]) -> None:
    """Clone or update the url repositories into the cache without checking anything."""
    config = _load(state, registry=_registry(), config_path=config_path)
    specs = [s for s in config.repos.values() if s.url and (not names or s.name in names)]
    unknown = [n for n in names if n not in config.repos]
    if unknown:
        _fail(f"unknown repo(s): {', '.join(unknown)}")
    if not specs:
        click.echo("nothing to fetch (no url repos selected)", err=True)
        return
    resolved = api.resolve_repos(config, specs, fetch=True, progress=state.progress)
    failed = 0
    for r in resolved:
        if r.ok:
            assert r.repo is not None
            click.echo(f"{r.spec.name}: {r.repo.root} @{(r.repo.head() or '')[:7]}")
        else:
            failed += 1
            click.echo(f"{r.spec.name}: FAILED {r.error}", err=True)
    if failed:
        sys.exit(EXIT_PARTIAL)


def _table(rows: Sequence[Sequence[str]]) -> str:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = [str(c).ljust(widths[i]) for i, c in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


# ----- aspects / config / init -------------------------------------------------------------------------------------


@main.command("aspects")
@click.option("--type", "type_name", default=None, help="Show one aspect type in detail.")
@click.pass_obj
def aspects_cmd(state: State, type_name: str | None) -> None:
    """List aspect types (built-in and plugins) with their options and the default aspect instances."""
    registry = _registry()
    from sistent.aspects import DEFAULT_ASPECT_NAMES, DEFAULT_CONFIG

    names = [type_name] if type_name else registry.names()
    for name in names:
        try:
            cls = registry.get(name)
        except RegistryError as exc:
            _fail(str(exc))
        click.echo(f"{name}: {cls.description or cls.__doc__ or ''}".rstrip())
        for info in describe_options(cls.options_cls):
            default = json.dumps(info.default, default=str) if info.default != "(required)" else "(required)"
            click.echo(f"  {info.name:<22} {info.type:<24} default {default}")
            if info.help:
                click.echo(f"  {'':<22} {info.help}")
        if cls.default_severity:
            click.echo(f"  severity defaults: {json.dumps(cls.default_severity)}")
        click.echo()
    if not type_name:
        click.echo(f"Default aspect instances: {', '.join(DEFAULT_ASPECT_NAMES)}")
        click.echo("Run `sistent config` to see them fully resolved, or `sistent init` to write them to a file.")
        if state.verbose:
            click.echo(DEFAULT_CONFIG)


@main.command("config")
@config_option
@click.pass_obj
def config_cmd(state: State, config_path: Path | None) -> None:
    """Print the fully resolved configuration as TOML (`# default` marks inherited values)."""
    config = _load(state, registry=_registry(), config_path=config_path)
    click.echo(dump_config(config), nl=False)


@main.command()
@click.argument("path", type=click.Path(path_type=Path, dir_okay=False), default=Path("sistent.toml"))
@click.option(
    "--main", "main_name", default=None, help="Name of the main repo (default: derived from --from or 'my-main-repo')."
)
@click.option(
    "--from",
    "from_path",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Local checkout to use as main; sets its path.",
)
@click.option(
    "--repos", "repo_globs", multiple=True, help="Glob of sibling checkouts to add as satellites, e.g. '../*/'."
)
@click.option("--github", default=None, help="Default GitHub owner for repos without path/url.")
@click.option("--force", is_flag=True, help="Overwrite an existing file.")
@click.pass_obj
def init(
    state: State,
    path: Path,
    main_name: str | None,
    from_path: Path | None,
    repo_globs: tuple[str, ...],
    github: str | None,
    force: bool,
) -> None:
    """Write a fully commented starter configuration (all defaults visible)."""
    if path.exists() and not force:
        _fail(f"{path} exists (use --force to overwrite)")
    repos: dict[str, Path | None] = {}
    main = main_name
    if from_path is not None:
        main = main or from_path.resolve().name
        repos[main] = from_path
    main = main or "my-main-repo"
    for pattern in repo_globs:
        base = Path(pattern).expanduser()
        for candidate in sorted(Path(base.parent).glob(base.name or "*")):
            if candidate.is_dir() and (candidate / ".git").exists():
                name = candidate.resolve().name
                if name not in repos:
                    repos[name] = candidate
    if not repos:
        repos = {main: None, "another-repo": None}
    text = default_config_text(main=main, repos=list(repos), github=github, paths=_relative_paths(path, repos))
    path.write_text(text, encoding="utf-8")
    state.progress(f"wrote {path} (main = {main}, {len(repos)} repos)")


def _relative_paths(config_path: Path, repos: dict[str, Path | None]) -> dict[str, str]:
    base = config_path.resolve().parent
    out: dict[str, str] = {}
    for name, p in repos.items():
        if p is None:
            continue
        try:
            out[name] = os.path.relpath(p.resolve(), base)
        except ValueError:
            out[name] = str(p.resolve())
    return out


if __name__ == "__main__":  # pragma: no cover
    main()
