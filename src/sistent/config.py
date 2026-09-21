"""``sistent.toml``: discovery, strict loading, default-aspect merging and fully resolved TOML output.

Loading is strict: unknown tables and keys, wrong value types, unknown aspect types and unknown aspect options are
:class:`ConfigError` naming the location (``[aspects.readme].simlarity_threshold``) with a "did you mean" hint and
the valid keys. Relative paths resolve against the configuration file's directory, never the working directory.
"""

from __future__ import annotations

import difflib
import os
import re
import tomllib
import typing
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sistent.aspects import DEFAULT_ASPECT_NAMES, DEFAULT_CONFIG
from sistent.model import Severity
from sistent.options import BaseOptions, OptionsError, check_type, parse_options
from sistent.registry import Registry, RegistryError, default_registry
from sistent.repository import RepoHints

CONFIG_FILENAME = "sistent.toml"
ENV_CONFIG = "SISTENT_CONFIG"
FAIL_ON_VALUES: tuple[str, ...] = ("info", "warning", "error", "never")

DEFAULT_FAIL_ON = "error"
DEFAULT_GIT_TIMEOUT = 120
DEFAULT_JOBS = 8

_TOP_LEVEL_TABLES: tuple[str, ...] = ("sistent", "repos", "aspects")
_SISTENT_SCHEMA: dict[str, Any] = {
    "main": str,
    "cache_dir": str,
    "fail_on": str,
    "github": str,
    "git_timeout": int,
    "jobs": int,
    "defaults": bool,
    "stale": bool,
    "baseline": str,
}
_REPO_SCHEMA: dict[str, Any] = {
    "path": str,
    "url": str,
    "rev": str,
    "root": str,
    "tags": list[str],
    "skip_aspects": list[str],
    "ignore": list[str],
    "aliases": list[str],
    "vars": dict[str, str],
    "substitute": bool,
}
_GITHUB_SHORTHAND = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_INLINE = 100


class ConfigError(Exception):
    """Invalid or missing configuration; the message names the offending location."""


# ----- resolved configuration --------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class RepoSpec:
    """One ``[repos.<name>]`` table, resolved."""

    name: str
    path: Path | None = None
    """Local checkout, absolute (relative values were joined to the config directory). Never fetched."""
    url: str | None = None
    """Git URL (shorthand and ``[sistent].github`` already expanded)."""
    rev: str | None = None
    root: str = "."
    tags: tuple[str, ...] = ()
    skip_aspects: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    vars: dict[str, str] = field(default_factory=dict)
    substitute: bool = True

    @property
    def source(self) -> str:
        """The path or url, for display."""
        return self.url if self.url is not None else str(self.path)

    @property
    def hints(self) -> RepoHints:
        """What identity probing needs to know about this repo."""
        return RepoHints(name=self.name, url=self.url, rev=self.rev, aliases=self.aliases, vars=dict(self.vars))


@dataclass(frozen=True, kw_only=True)
class AspectSpec:
    """One resolved aspect instance: a ``[aspects.<name>]`` table merged over the built-in default of that name."""

    name: str
    type: str
    options: BaseOptions
    table: dict[str, Any]
    """The fully resolved raw table, including ``type``."""
    inherited: frozenset[str]
    """Keys whose values came from the built-in default rather than from the user's file."""
    is_default: bool
    """Whether ``name`` is one of the built-in default instances."""


@dataclass(frozen=True, kw_only=True)
class Config:
    """A fully resolved ``sistent.toml``."""

    path: Path
    directory: Path
    main: str
    cache_dir: Path
    fail_on: Severity | None
    """``None`` means ``"never"``."""
    github: str | None
    git_timeout: int
    jobs: int
    defaults: bool
    stale: bool
    baseline: Path | None
    repos: dict[str, RepoSpec]
    """Config order."""
    aspects: tuple[AspectSpec, ...]
    """Defaults first (in :data:`~sistent.aspects.DEFAULT_ASPECT_NAMES` order), then user-defined in config order."""

    @property
    def main_spec(self) -> RepoSpec:
        return self.repos[self.main]

    @property
    def satellites(self) -> list[RepoSpec]:
        return [spec for name, spec in self.repos.items() if name != self.main]

    @property
    def aspect_names(self) -> list[str]:
        return [spec.name for spec in self.aspects]

    def aspect(self, name: str) -> AspectSpec:
        for spec in self.aspects:
            if spec.name == name:
                return spec
        raise ConfigError(
            f"unknown aspect {name!r}{_suggest(name, self.aspect_names)}. "
            f"Configured aspects: {', '.join(self.aspect_names) or '(none)'}"
        )


# ----- discovery ---------------------------------------------------------------------------------------------------


def find_config(explicit: Path | str | None = None, *, start: Path | None = None) -> Path:
    """Locate the configuration file: ``explicit``, else ``$SISTENT_CONFIG``, else ``sistent.toml`` upward from
    ``start`` (the working directory by default)."""
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate}")
        return candidate.absolute()
    env = os.environ.get(ENV_CONFIG)
    if env:
        candidate = Path(env).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"config file not found: {candidate} (from ${ENV_CONFIG})")
        return candidate.absolute()
    base = Path(start).absolute() if start is not None else Path.cwd()
    for directory in (base, *base.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"no {CONFIG_FILENAME} found in {base} or any parent directory (use -c/--config or set ${ENV_CONFIG})"
    )


# ----- loading -----------------------------------------------------------------------------------------------------


def load_config(path: Path | str, *, registry: Registry | None = None) -> Config:
    """Read and resolve the configuration file at ``path``."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc.strerror or exc}") from None
    return loads_config(text, path=path, registry=registry)


def loads_config(text: str, *, path: Path | str, registry: Registry | None = None) -> Config:
    """Resolve configuration from TOML ``text``; ``path`` locates it (relative paths resolve against its directory)."""
    path = Path(path).expanduser().absolute()
    directory = path.parent
    if registry is None:
        registry = default_registry()
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None

    _check_top_level(doc)
    sistent = _table(doc, "sistent", required=True)
    repos_table = _table(doc, "repos", required=False)
    aspects_table = _table(doc, "aspects", required=False)

    # ----- [sistent] ---------------------------------------------------------------------------------------------
    _check_keys(sistent, _SISTENT_SCHEMA, where="[sistent]")
    main = sistent.get("main")
    if main is None:
        raise ConfigError("[sistent].main: required (the name of the [repos.<name>] table that is the source of truth)")
    fail_on_text = sistent.get("fail_on", DEFAULT_FAIL_ON)
    if fail_on_text not in FAIL_ON_VALUES:
        raise ConfigError(f"[sistent].fail_on: expected one of {', '.join(FAIL_ON_VALUES)}; got {fail_on_text!r}")
    fail_on = None if fail_on_text == "never" else Severity.parse(fail_on_text)
    github = sistent.get("github")
    if github is not None and not github.strip():
        raise ConfigError("[sistent].github: must not be empty")
    git_timeout = sistent.get("git_timeout", DEFAULT_GIT_TIMEOUT)
    jobs = sistent.get("jobs", DEFAULT_JOBS)
    for key, value in (("git_timeout", git_timeout), ("jobs", jobs)):
        if value < 1:
            raise ConfigError(f"[sistent].{key}: must be a positive integer, got {value}")
    defaults = sistent.get("defaults", True)
    stale = sistent.get("stale", True)
    cache_dir = _resolve_path(sistent["cache_dir"], directory) if "cache_dir" in sistent else default_cache_dir()
    baseline = _resolve_path(sistent["baseline"], directory) if "baseline" in sistent else None

    # ----- [repos.X] ---------------------------------------------------------------------------------------------
    repos: dict[str, RepoSpec] = {}
    for name, table in repos_table.items():
        repos[name] = _parse_repo(name, table, directory=directory, github=github)
    if not repos:
        raise ConfigError("no repositories configured: add at least a [repos.<name>] table for main")
    if main not in repos:
        raise ConfigError(
            f"[sistent].main: {main!r} is not a configured repo{_suggest(main, repos)}. "
            f"Configured repos: {', '.join(repos)}"
        )

    # ----- [aspects.X] -------------------------------------------------------------------------------------------
    for name, table in aspects_table.items():
        if not isinstance(table, dict):
            raise ConfigError(f"[aspects.{name}]: expected a table, got {type(table).__name__} ({table!r})")
    merged, inherited = merge_aspect_tables(aspects_table, defaults=defaults)
    aspects = tuple(
        _parse_aspect(
            name,
            table,
            inherited=inherited[name],
            is_default=defaults and name in DEFAULT_ASPECT_NAMES,
            registry=registry,
        )
        for name, table in merged.items()
    )
    aspect_names = [spec.name for spec in aspects]
    for spec in repos.values():
        for skipped in spec.skip_aspects:
            if skipped not in aspect_names:
                raise ConfigError(
                    f"[repos.{spec.name}].skip_aspects: unknown aspect {skipped!r}{_suggest(skipped, aspect_names)}. "
                    f"Configured aspects: {', '.join(aspect_names) or '(none)'}"
                )

    return Config(
        path=path,
        directory=directory,
        main=main,
        cache_dir=cache_dir,
        fail_on=fail_on,
        github=github,
        git_timeout=git_timeout,
        jobs=jobs,
        defaults=defaults,
        stale=stale,
        baseline=baseline,
        repos=repos,
        aspects=aspects,
    )


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/sistent``, else ``~/.cache/sistent``."""
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    base = Path(_expand(xdg)) if xdg else Path.home() / ".cache"
    return base / "sistent"


def expand_url(url: str) -> str:
    """Expand the GitHub shorthand ``"org/repo"`` to ``https://github.com/org/repo``; other URLs are unchanged."""
    url = url.strip()
    if _GITHUB_SHORTHAND.match(url):
        return f"https://github.com/{url}"
    return url


def _parse_repo(name: str, table: Any, *, directory: Path, github: str | None) -> RepoSpec:
    where = f"[repos.{name}]"
    if not name.strip():
        raise ConfigError("[repos]: repository names must not be empty")
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: expected a table, got {type(table).__name__} ({table!r})")
    _check_keys(table, _REPO_SCHEMA, where=where)
    path_text: str | None = table.get("path")
    url: str | None = table.get("url")
    if path_text is not None and url is not None:
        raise ConfigError(f"{where}: give either 'path' or 'url', not both")
    path: Path | None = None
    if path_text is not None:
        if not path_text.strip():
            raise ConfigError(f"{where}.path: must not be empty")
        path = _resolve_path(path_text, directory)
    elif url is not None:
        if not url.strip():
            raise ConfigError(f"{where}.url: must not be empty")
        url = expand_url(url)
    elif github is not None:
        url = f"https://github.com/{github}/{name}"
    else:
        raise ConfigError(
            f"{where}: needs 'path' or 'url' (or set [sistent].github to derive https://github.com/<owner>/{name})"
        )
    rev: str | None = table.get("rev")
    if rev is not None and path is not None:
        raise ConfigError(f"{where}.rev: only applies to 'url' repos (a local 'path' is never fetched)")
    root: str = table.get("root", ".")
    if not root.strip():
        root = "."
    if Path(root).is_absolute():
        raise ConfigError(f"{where}.root: must be a sub-directory relative to the checkout, got {root!r}")
    return RepoSpec(
        name=name,
        path=path,
        url=url,
        rev=rev,
        root=root,
        tags=tuple(table.get("tags", [])),
        skip_aspects=tuple(table.get("skip_aspects", [])),
        ignore=tuple(table.get("ignore", [])),
        aliases=tuple(table.get("aliases", [])),
        vars=dict(table.get("vars", {})),
        substitute=table.get("substitute", True),
    )


def _parse_aspect(
    name: str, table: Mapping[str, Any], *, inherited: frozenset[str], is_default: bool, registry: Registry
) -> AspectSpec:
    where = f"[aspects.{name}]"
    type_name = table["type"]
    try:
        cls = registry.get(type_name)
    except RegistryError as exc:
        raise ConfigError(f"{where}.type: {exc}") from None
    option_table = {key: value for key, value in table.items() if key != "type"}
    try:
        options = parse_options(cls.options_cls, option_table, where=where)
    except OptionsError as exc:
        message = str(exc).replace("unknown option", f"unknown option for type '{type_name}'")
        raise ConfigError(message) from None
    return AspectSpec(
        name=name,
        type=type_name,
        options=options,
        table=_copy(dict(table)),
        inherited=inherited,
        is_default=is_default,
    )


# ----- defaults ----------------------------------------------------------------------------------------------------


def default_aspect_tables() -> dict[str, dict[str, Any]]:
    """The ``[aspects.*]`` tables of :data:`~sistent.aspects.DEFAULT_CONFIG`, in default order (fresh copies)."""
    parsed = tomllib.loads(DEFAULT_CONFIG).get("aspects", {})
    ordered: dict[str, dict[str, Any]] = {name: parsed[name] for name in DEFAULT_ASPECT_NAMES if name in parsed}
    for name, table in parsed.items():
        ordered.setdefault(name, table)
    return ordered


def merge_aspect_tables(
    user: Mapping[str, Mapping[str, Any]], *, defaults: bool
) -> tuple[dict[str, dict[str, Any]], dict[str, frozenset[str]]]:
    """Merge user ``[aspects.*]`` tables over the built-in defaults.

    A user table with a default's name shallow-merges over it (lists and dicts replace as a whole); ``type`` may not
    change for a default name. Other names are new instances and must give ``type``. ``enabled = false`` removes an
    instance. Returns the resolved tables (defaults first, then new instances in config order) and, per aspect, the
    keys inherited from the default.
    """
    base = default_aspect_tables() if defaults else {}
    resolved: dict[str, dict[str, Any]] = {}
    inherited: dict[str, frozenset[str]] = {}
    for name, table in base.items():
        resolved[name] = dict(table)
        inherited[name] = frozenset(table)
    for name, user_table in user.items():
        where = f"[aspects.{name}]"
        if not isinstance(user_table, Mapping):
            raise ConfigError(f"{where}: expected a table, got {type(user_table).__name__} ({user_table!r})")
        table = dict(user_table)
        if "type" in table and not isinstance(table["type"], str):
            raise ConfigError(f"{where}.type: expected str, got {type(table['type']).__name__} ({table['type']!r})")
        if name in base:
            default_type = base[name]["type"]
            if "type" in table and table["type"] != default_type:
                raise ConfigError(
                    f"{where}.type: the type of built-in aspect '{name}' is fixed to '{default_type}' "
                    f"(got {table['type']!r}); define a new aspect under another name instead"
                )
            merged = {**base[name], **table}
            keys = frozenset(key for key in base[name] if key not in table)
        else:
            if "type" not in table:
                raise ConfigError(
                    f"{where}.type: required for a new aspect (built-in defaults: {', '.join(DEFAULT_ASPECT_NAMES)})"
                )
            merged = table
            keys = frozenset()
        enabled = merged.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"{where}.enabled: expected bool, got {type(enabled).__name__} ({enabled!r})")
        if not enabled:
            resolved.pop(name, None)
            inherited.pop(name, None)
            continue
        resolved[name] = merged
        inherited[name] = keys
    return resolved, inherited


# ----- output ------------------------------------------------------------------------------------------------------


def dump_config(config: Config) -> str:
    """The fully resolved configuration as TOML; ``# default`` follows every value inherited from the defaults.

    Loading the output again yields the same configuration: a built-in default the user disabled is written as
    ``[aspects.<name>] enabled = false`` so the defaults do not resurrect it.
    """
    lines: list[str] = []
    sistent: dict[str, Any] = {
        "main": config.main,
        "cache_dir": str(config.cache_dir),
        "fail_on": config.fail_on.value if config.fail_on is not None else "never",
    }
    if config.github is not None:
        sistent["github"] = config.github
    sistent["git_timeout"] = config.git_timeout
    sistent["jobs"] = config.jobs
    sistent["defaults"] = config.defaults
    sistent["stale"] = config.stale
    if config.baseline is not None:
        sistent["baseline"] = str(config.baseline)
    _emit_table(lines, "sistent", sistent)

    for spec in config.repos.values():
        table: dict[str, Any] = {}
        if spec.path is not None:
            table["path"] = str(spec.path)
        if spec.url is not None:
            table["url"] = spec.url
        if spec.rev is not None:
            table["rev"] = spec.rev
        table["root"] = spec.root
        table["tags"] = list(spec.tags)
        table["skip_aspects"] = list(spec.skip_aspects)
        table["ignore"] = list(spec.ignore)
        table["aliases"] = list(spec.aliases)
        table["vars"] = dict(spec.vars)
        table["substitute"] = spec.substitute
        lines.append("")
        _emit_table(lines, f"repos.{_fmt_key(spec.name)}", table)

    by_name = {aspect.name: aspect for aspect in config.aspects}
    disabled = [name for name in DEFAULT_ASPECT_NAMES if name not in by_name] if config.defaults else []
    for name in _aspect_order(list(by_name), disabled):
        lines.append("")
        aspect = by_name.get(name)
        if aspect is None:
            _emit_table(lines, f"aspects.{_fmt_key(name)}", {"enabled": False})
            continue
        table = {"type": aspect.type}
        table.update((key, value) for key, value in aspect.table.items() if key != "type")
        _emit_table(lines, f"aspects.{_fmt_key(name)}", table, inherited=aspect.inherited)
    return "\n".join(lines) + "\n"


def _aspect_order(active: Sequence[str], disabled: Sequence[str]) -> list[str]:
    """Output order: built-in names in default order (whether active or disabled), then the other active names."""
    builtin = [name for name in DEFAULT_ASPECT_NAMES if name in active or name in disabled]
    return [*builtin, *(name for name in active if name not in DEFAULT_ASPECT_NAMES)]


def default_config_text(
    *,
    main: str = "my-main-repo",
    repos: Sequence[str] = ("another-repo",),
    github: str | None = None,
    paths: Mapping[str, str] | None = None,
) -> str:
    """The commented starter configuration written by ``sistent init``.

    ``[sistent]`` and one ``[repos.*]`` table per name are followed by the built-in default aspects spelled out in
    full, so the defaults (including the skills-ignore rules) live in the file the user owns. ``paths`` gives an
    explicit ``path`` per repo name; other repos get a commented url hint (with ``github``) or a ``../<name>`` path.
    """
    paths = dict(paths or {})
    lines = [
        "# sistent configuration. Every relative path is resolved against this file's directory.",
        "# Run `sistent config` to print the resolved configuration and `sistent aspects` to list every option.",
        "",
        "[sistent]",
        f"main = {_fmt_str(main)}  # source-of-truth repo (a [repos.*] key); satellites are compared against it",
    ]
    if github is not None:
        lines.append(f"github = {_fmt_str(github)}  # default GitHub owner for [repos.X] tables without path/url")
    else:
        lines.append('# github = "my-org"  # default GitHub owner for [repos.X] tables without path/url')
    lines += [
        '# cache_dir = "~/.cache/sistent"  # where url repos are cloned ($XDG_CACHE_HOME/sistent by default)',
        f"fail_on = {_fmt_str(DEFAULT_FAIL_ON)}  # info | warning | error | never (upstream candidates never count)",
        f"git_timeout = {DEFAULT_GIT_TIMEOUT}  # seconds per git command",
        f"jobs = {DEFAULT_JOBS}  # parallel workers for fetching and extracting",
        "defaults = true  # keep the built-in aspects listed below; edit them by name or disable with enabled = false",
        "stale = true  # report copy-paste leftovers that name another configured repo",
        '# baseline = ".sistent-baseline.json"  # hide findings recorded in this file (--update-baseline writes it)',
        "",
    ]
    for name in dict.fromkeys((main, *repos)):
        lines.append(f"[repos.{_fmt_key(name)}]")
        if name in paths:
            lines.append(f"path = {_fmt_str(paths[name])}  # relative to this file")
        elif github is not None:
            lines.append(f"# url derived from [sistent].github: https://github.com/{github}/{name}")
        else:
            lines.append(f'path = {_fmt_str("../" + name)}  # relative to this file; or url = "org/repo" (+ rev)')
        lines += [
            '# tags = ["python"]  # free labels; aspects with tags apply only to repos sharing one of them',
            '# skip_aspects = ["pre_commit"]  # aspect instances not run for this repo',
            '# ignore = ["readme:README.md#Funding*"]  # "<aspect>:<locator-glob>" suppressions',
            "",
        ]
    lines += [
        "# ---- aspects: the built-in defaults, spelled out. Edit freely; `enabled = false` removes one; ----",
        "# ---- a table with a new name and a `type` adds an instance. ----",
        DEFAULT_CONFIG.strip(),
    ]
    return "\n".join(lines) + "\n"


# ----- helpers -----------------------------------------------------------------------------------------------------


def _suggest(name: str, candidates: Iterable[str]) -> str:
    close = difflib.get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


def _check_top_level(doc: Mapping[str, Any]) -> None:
    unknown = [key for key in doc if key not in _TOP_LEVEL_TABLES]
    if unknown:
        parts = [f"[{key}]: unknown top-level table{_suggest(key, _TOP_LEVEL_TABLES)}" for key in unknown]
        raise ConfigError(f"{'; '.join(parts)}. Valid tables: {', '.join(_TOP_LEVEL_TABLES)}")


def _table(doc: Mapping[str, Any], name: str, *, required: bool) -> dict[str, Any]:
    value = doc.get(name)
    if value is None:
        if required:
            raise ConfigError(f"[{name}]: table is required")
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}]: expected a table, got {type(value).__name__} ({value!r})")
    return value


def _check_keys(table: Mapping[str, Any], schema: Mapping[str, Any], *, where: str) -> None:
    unknown = [key for key in table if key not in schema]
    if unknown:
        parts = [f"{where}.{key}: unknown key{_suggest(key, schema)}" for key in unknown]
        raise ConfigError(f"{'; '.join(parts)}. Valid keys: {', '.join(sorted(schema))}")
    for key, value in table.items():
        hint = schema[key]
        if not check_type(value, hint):
            raise ConfigError(f"{where}.{key}: expected {_describe(hint)}, got {type(value).__name__} ({value!r})")


def _describe(hint: Any) -> str:
    origin, args = typing.get_origin(hint), typing.get_args(hint)
    if origin is list:
        return f"list[{_describe(args[0])}]" if args else "list"
    if origin is dict:
        return f"dict[{_describe(args[0])}, {_describe(args[1])}]" if args else "dict"
    return getattr(hint, "__name__", str(hint))


def _expand(text: str) -> str:
    return os.path.expanduser(os.path.expandvars(text))


def _resolve_path(text: str, directory: Path) -> Path:
    """``text`` with ``~`` and environment variables expanded, joined to ``directory`` when relative."""
    candidate = Path(_expand(text))
    return candidate if candidate.is_absolute() else directory / candidate


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


# ----- tiny TOML emitter -------------------------------------------------------------------------------------------


def _emit_table(
    lines: list[str],
    header: str,
    table: Mapping[str, Any],
    *,
    inherited: frozenset[str] = frozenset(),
    marker: bool = False,
) -> None:
    lines.append(f"[{header}]" + ("  # default" if marker else ""))
    deferred: list[tuple[str, Mapping[str, Any], bool]] = []
    for key, value in table.items():
        is_inherited = marker or key in inherited
        if isinstance(value, Mapping) and value and len(_fmt_inline_table(value)) > _MAX_INLINE:
            deferred.append((key, value, is_inherited))
            continue
        mark = "  # default" if key in inherited else ""
        lines.append(f"{_fmt_key(key)} = {_fmt_value(value, inline=False)}{mark}")
    for key, value, is_inherited in deferred:
        lines.append("")
        _emit_table(lines, f"{header}.{_fmt_key(key)}", value, marker=is_inherited)


def _fmt_value(value: Any, *, inline: bool) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _fmt_float(value)
    if isinstance(value, str):
        return _fmt_str(value)
    if isinstance(value, Path):
        return _fmt_str(str(value))
    if isinstance(value, (list, tuple)):
        return _fmt_list(list(value), inline=inline)
    if isinstance(value, Mapping):
        return _fmt_inline_table(value)
    raise TypeError(f"cannot write {type(value).__name__} ({value!r}) as TOML")


def _fmt_float(value: float) -> str:
    text = repr(value)
    if text in ("inf", "-inf", "nan"):
        return text
    if "." not in text and "e" not in text:
        text += ".0"
    return text


def _fmt_str(value: str) -> str:
    out = ['"']
    for char in value:
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif char == "\r":
            out.append("\\r")
        elif ord(char) < 0x20 or char == "\x7f":
            out.append(f"\\u{ord(char):04X}")
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


def _fmt_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else _fmt_str(key)


def _fmt_list(items: list[Any], *, inline: bool) -> str:
    single = "[" + ", ".join(_fmt_value(item, inline=True) for item in items) + "]"
    if inline or not items or len(single) <= _MAX_INLINE:
        return single
    body = "".join(f"    {_fmt_value(item, inline=True)},\n" for item in items)
    return f"[\n{body}]"


def _fmt_inline_table(table: Mapping[str, Any]) -> str:
    if not table:
        return "{}"
    items = ", ".join(f"{_fmt_key(key)} = {_fmt_value(value, inline=True)}" for key, value in table.items())
    return f"{{ {items} }}"


__all__ = [
    "CONFIG_FILENAME",
    "ENV_CONFIG",
    "FAIL_ON_VALUES",
    "AspectSpec",
    "Config",
    "ConfigError",
    "RepoSpec",
    "default_aspect_tables",
    "default_cache_dir",
    "default_config_text",
    "dump_config",
    "expand_url",
    "find_config",
    "load_config",
    "loads_config",
    "merge_aspect_tables",
]
