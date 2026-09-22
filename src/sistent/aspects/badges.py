"""README badge set, providers and order (aspect type ``badges``).

Every image whose URL :func:`~sistent.parsers.badges.is_badge` accepts is normalised, **after identity substitution**,
to a *kind* (what the badge says: ``pypi-version``, ``ci:<workflow>``, ``coverage``, ...), a *provider* (the host that
renders it) and *params* (the meaningful path segments left after removing the ``{{org}}``/``{{name}}``/``{{branch}}``
placeholders, per-repository identifiers and cosmetics such as query strings). Kinds are compared as a set
(``missing``/``extra``), shared kinds by provider and params (``differs``) and by relative order (``reordered``).

Classification happens on the substituted URL so that ``badge.fury.io/py/neuroconv.svg`` and
``badge.fury.io/py/roiextractors.svg`` are the same ``pypi-version`` badge, while a package name that is *not* the
repository's own (``pypi/l/pynwb`` in another project) stays visible in ``params`` and is caught by the
:class:`~sistent.compare.text.Substituter`'s stale scan.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from sistent.aspects.base import Aspect, UnparseableFile
from sistent.compare.sequences import diff_sequences
from sistent.compare.sets import diff_sets
from sistent.compare.text import BRANCH, NAME, ORG
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject, locator
from sistent.options import BaseOptions
from sistent.parsers import ParseError
from sistent.parsers.markdown import parse_document
from sistent.repository import RepoContext

# ----- options -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class BadgesOptions(BaseOptions):
    """Options of the ``badges`` aspect type."""

    file: str = field(
        default="README.md",
        metadata={"help": "File whose badges are compared (Markdown, HTML and reStructuredText image syntaxes)."},
    )
    order: bool = field(
        default=True,
        metadata={"help": "Report when the badges both repos share appear in a different order."},
    )
    ignore_kinds: list[str] = field(
        default_factory=list,
        metadata={"help": "Badge kinds to drop at extraction; fnmatch globs such as 'static:*' or 'social:*'."},
    )


# ----- classification ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Classified:
    """The normalised identity of one badge."""

    kind: str
    """What the badge says, e.g. ``pypi-version``, ``ci:tests.yml``, ``code-style:black``, ``static:python``."""
    provider: str
    """Host that renders the badge (``img.shields.io``, ``codecov.io``, ...); ``""`` for relative URLs."""
    params: tuple[str, ...]
    """Meaningful path segments after removing identity placeholders, per-repo identifiers and cosmetics."""


_PLACEHOLDERS: frozenset[str] = frozenset({NAME, ORG, BRANCH})
_RE_IMAGE_EXT = re.compile(r"\.(?:svg|png|gif|jpe?g|webp|json)$", re.IGNORECASE)

_PYPI_KINDS: dict[str, str] = {
    "v": "pypi-version",
    "l": "pypi-license",
    "pyversions": "pypi-pyversions",
    "dm": "pypi-downloads",
    "dw": "pypi-downloads",
    "dd": "pypi-downloads",
}
_CONDA_KINDS: dict[str, str] = {
    "vn": "conda-version",
    "v": "conda-version",
    "dn": "conda-downloads",
    "d": "conda-downloads",
}
_ANACONDA_KINDS: dict[str, str] = {
    "version": "conda-version",
    "downloads": "conda-downloads",
    "license": "license",
}
_SOCIAL_PLATFORMS: frozenset[str] = frozenset(
    {
        "twitter",
        "mastodon",
        "discord",
        "slack",
        "gitter",
        "youtube",
        "linkedin",
        "reddit",
        "bluesky",
        "zulip",
        "matrix",
        "telegram",
    }
)
_SHIELDS_SOCIAL: frozenset[str] = frozenset({"twitter", "discord", "mastodon", "reddit", "youtube", "gitter", "matrix"})
_GITHUB_SOCIAL: frozenset[str] = frozenset({"stars", "forks", "watchers"})
_STYLE_LABELS: frozenset[str] = frozenset(
    {"code style", "code-style", "codestyle", "style", "linting", "linter", "formatting", "formatter"}
)
_PRE_COMMIT_LABELS: frozenset[str] = frozenset({"pre-commit", "pre-commit.ci"})

_Match = tuple[str, list[str]]


def _host_is(host: str, *listed: str) -> bool:
    return any(host == name or host.endswith("." + name) for name in listed)


def _free(segments: Iterable[str]) -> list[str]:
    """``segments`` without the identity placeholders."""
    return [s for s in segments if s not in _PLACEHOLDERS]


def _segments(path: str) -> list[str]:
    """Percent-decoded path segments with the image extension stripped from the last one."""
    segments = [unquote(s) for s in path.split("/") if s]
    if segments:
        last = _RE_IMAGE_EXT.sub("", segments[-1])
        if last:
            segments[-1] = last
        else:
            segments.pop()
    return segments


def _query(raw: str) -> dict[str, str]:
    """First value of every query parameter, keys lower-cased."""
    return {key.lower(): values[0] for key, values in parse_qs(raw, keep_blank_values=True).items() if values}


def _fold(text: str) -> str:
    return " ".join(text.split()).casefold()


def _split_static(content: str) -> tuple[str, str]:
    """``(label, message)`` of a shields ``badge/<label>-<message>-<color>`` path segment.

    Shields escapes: ``--`` is a literal dash, ``__`` a literal underscore, ``_`` a space. With fewer than three
    dash-separated parts the badge has no label (``badge/<message>-<color>``).
    """
    escaped = content.replace("--", "\x00").replace("__", "\x01")
    parts = escaped.split("-")
    if len(parts) >= 3:
        label, message = parts[0], "-".join(parts[1:-1])
    else:
        label, message = "", parts[0]

    def restore(text: str) -> str:
        return _fold(text.replace("_", " ").replace("\x00", "-").replace("\x01", "_"))

    return restore(label), restore(message)


def _social_platform(logo: str, label: str, message: str) -> str | None:
    logo = logo.casefold()
    if logo == "x":
        return "twitter"
    if logo in _SOCIAL_PLATFORMS:
        return logo
    for word in re.findall(r"[a-z]+", f"{label} {message}"):
        if word in _SOCIAL_PLATFORMS:
            return str(word)
    return None


def _match_static(label: str, message: str, query: dict[str, str]) -> _Match | None:
    """Kind of a shields static badge from its casefolded label and message."""
    platform = _social_platform(query.get("logo", ""), label, message)
    if platform:
        return f"social:{platform}", ["badge"]
    if label == "doi":
        return "doi", ["badge"]
    if label == "license":
        return "license", ["badge", *([message] if message else [])]
    if label in _PRE_COMMIT_LABELS:
        return "pre-commit", ["badge"]
    if label in _STYLE_LABELS and message:
        return f"code-style:{message}", ["badge"]
    if label:
        return f"static:{label}", ["badge", *([message] if message else [])]
    if message:
        return f"static:{message}", ["badge"]
    return None


def _match_shields_github(rest: list[str]) -> _Match | None:
    if rest[:3] == ["actions", "workflow", "status"] and len(rest) >= 6:
        return f"ci:{rest[5]}", ["github", "actions", "workflow", "status", *_free(rest[3:5])]
    if rest[:2] == ["workflow", "status"] and len(rest) >= 5:
        return f"ci:{rest[4]}", ["github", "workflow", "status", *_free(rest[2:4])]
    if rest[:1] == ["license"]:
        return "license", ["github", "license", *_free(rest[1:])]
    if rest and rest[0].lower() in _GITHUB_SOCIAL:
        metric = rest[0].lower()
        return f"social:github-{metric}", ["github", metric, *_free(rest[1:])]
    return None


def _match_shields(host: str, segments: list[str], query: dict[str, str]) -> _Match | None:
    if not segments:
        return None
    head, rest = segments[0].lower(), segments[1:]
    if head == "pypi" and len(rest) >= 2:
        kind = _PYPI_KINDS.get(rest[0].lower())
        return (kind, ["pypi", rest[0].lower(), *_free(rest[1:])]) if kind else None
    if head == "conda" and len(rest) >= 2:
        kind = _CONDA_KINDS.get(rest[0].lower())
        return (kind, ["conda", rest[0].lower(), *_free(rest[1:])]) if kind else None
    if head == "github":
        return _match_shields_github(rest)
    if head == "codecov" and rest[:1] == ["c"]:
        return "coverage", ["codecov", "c", *_free(rest[1:])]
    if head == "coveralls":
        return "coverage", ["coveralls", *_free(rest)]
    if head == "readthedocs":
        return "docs", ["readthedocs", *_free(rest)]
    if head in _SHIELDS_SOCIAL:
        return f"social:{head}", [head, *rest[:-1]]  # the trailing handle / server id is per repository
    if head == "badge" and rest:
        label, message = _split_static(rest[0])
        return _match_static(label, message, query)
    if head == "static" and rest[:1] == ["v1"]:
        return _match_static(_fold(query.get("label", "")), _fold(query.get("message", "")), query)
    if head == "endpoint":
        url = query.get("url", "")
        if "astral-sh/ruff" in url:
            return "code-style:ruff", ["endpoint"]
        return f"other:{host}/endpoint", [url] if url else []
    return None


def _match_github(segments: list[str]) -> _Match | None:
    if len(segments) < 3:
        return None
    owner = _free(segments[:2])
    rest = segments[2:]
    if rest[:2] == ["actions", "workflows"] and len(rest) >= 3:
        return f"ci:{rest[2]}", [*owner, "actions", "workflows"]
    if rest[:1] == ["workflows"] and len(rest) >= 2:
        return f"ci:{rest[1]}", [*owner, "workflows"]
    return None


def _match(host: str, segments: list[str], query: dict[str, str]) -> _Match | None:
    if _host_is(host, "img.shields.io", "shields.io"):
        return _match_shields(host, segments, query)
    if _host_is(host, "badge.fury.io"):
        return ("pypi-version", ["py", *_free(segments[1:])]) if segments[:1] == ["py"] else None
    if _host_is(host, "pepy.tech"):
        if segments and segments[0] in ("badge", "personalized-badge"):
            params = [segments[0], *_free(segments[1:])]
            if query.get("period"):
                params.append(query["period"])
            return "pypi-downloads", params
        return None
    if _host_is(host, "anaconda.org"):
        if len(segments) >= 4 and segments[2] == "badges":
            what = segments[3].lower()
            kind = _ANACONDA_KINDS.get(what)
            return (kind, [*_free(segments[:2]), "badges", what]) if kind else None
        return None
    if _host_is(host, "github.com"):
        return _match_github(segments)
    if _host_is(host, "codecov.io"):
        return "coverage", _free(segments[1:3])  # github|gh/<org>/<repo>/...: only a foreign org/repo matters
    if _host_is(host, "coveralls.io"):
        return "coverage", _free(s for s in segments if s not in ("repos", "github", "r", "badge"))
    if _host_is(host, "readthedocs.org"):
        return "docs", _free(segments[1:2]) if segments[:1] == ["projects"] else []
    if _host_is(host, "readthedocs.io"):
        return "docs", []
    if _host_is(host, "zenodo.org"):
        return ("doi", ["badge"]) if segments[:1] == ["badge"] else None
    if _host_is(host, "results.pre-commit.ci"):
        return "pre-commit", _free(segments[2:4])  # badge/github/<org>/<repo>/<branch>
    if _host_is(host, "mybinder.org"):
        return "binder", []
    return None


def classify(img: str) -> Classified:
    """Normalise an (identity-substituted) badge image URL to ``(kind, provider, params)``.

    Query strings are dropped except where they carry the badge's meaning (shields ``static/v1``, ``endpoint``,
    ``logo`` hints for social badges, pepy ``period``). Kinds:

    * ``pypi-version`` / ``pypi-license`` / ``pypi-pyversions`` / ``pypi-downloads`` (shields, badge.fury.io, pepy),
    * ``conda-version`` / ``conda-downloads`` (shields ``conda/``, anaconda.org),
    * ``ci:<workflow>`` (GitHub Actions badge, shields ``github/actions/workflow/status``),
    * ``coverage`` (codecov, coveralls, shields), ``docs`` (readthedocs, shields), ``doi`` (zenodo, static ``doi``),
    * ``license``, ``pre-commit``, ``binder``, ``code-style:<tool>``,
    * ``social:<platform>`` (shields ``twitter/``, ``discord/``, ..., static badges with a social logo or word,
      ``github/stars|forks|watchers`` as ``social:github-<metric>``),
    * ``static:<label>`` (message in ``params``) or ``static:<message>`` when the static badge has no label,
    * ``other:<host>/<path>`` for everything else (placeholders and purely numeric segments dropped from the path).
    """
    try:
        parts = urlsplit(img.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return Classified(kind=f"other:{img.strip()}", provider="", params=())
    segments = _segments(parts.path)
    matched = _match(host, segments, _query(parts.query))
    if matched is None:
        path = "/".join(s for s in segments if s not in _PLACEHOLDERS and not s.isdigit())
        matched = (f"other:{host}/{path}", [])
    kind, params = matched
    return Classified(kind=kind, provider=host, params=tuple(params))


# ----- aspect ------------------------------------------------------------------------------------------------------


def _ignore_pattern(kind: str, patterns: Sequence[str]) -> str | None:
    folded = kind.casefold()
    for pattern in patterns:
        if fnmatch.fnmatchcase(folded, pattern.casefold()):
            return pattern
    return None


def _badges(snap: Snapshot) -> list[dict[str, Any]]:
    badges = snap.data.get("badges")
    return list(badges) if isinstance(badges, list) else []


def _kind(badge: dict[str, Any]) -> str:
    return str(badge.get("kind", ""))


def _signature(badge: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    return str(badge.get("provider", "")), tuple(str(p) for p in badge.get("params", ()))


def _describe(badge: dict[str, Any]) -> str:
    provider, params = _signature(badge)
    return f"{provider} {'/'.join(params)}".strip()


def _count(badges: Sequence[dict[str, Any]]) -> str:
    return "1 badge" if len(badges) == 1 else f"{len(badges)} badges"


class BadgesAspect(Aspect):
    """Compare the badges of one file: which kinds exist, who renders them and in which order."""

    type_name = "badges"
    options_cls = BadgesOptions
    description = "README badge set: badge kinds (pypi-version, ci:<workflow>, coverage, ...), providers and order."
    default_severity = {"missing.file": "warning"}
    """A missing README is the markdown aspect's error; the badge aspect only warns about it."""

    @property
    def badge_options(self) -> BadgesOptions:
        options = self.options
        if not isinstance(options, BadgesOptions):
            raise TypeError(f"{self.name}: expected BadgesOptions, got {type(options).__name__}")
        return options

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """``{"present": bool, "badges": [{kind, provider, params, alt, img, href, line, syntax}, ...]}``.

        Badges are in document order; ``img``/``href``/``alt`` are identity-substituted before classification.
        Kinds matching ``ignore_kinds`` are dropped and listed in ``ctx.ignored``.
        """
        options = self.badge_options
        file = options.file
        if not ctx.exists(file):
            return {"present": False, "badges": []}
        text = ctx.read_text(file)
        try:
            document = parse_document(text, source=file)
        except ParseError as exc:
            raise UnparseableFile(file, exc.reason) from exc
        where = locator(file, "badges")
        records: list[dict[str, Any]] = []
        for image in document.badges:
            if not image.badge:
                continue
            img = ctx.substitute(image.img, where=where)
            href = ctx.substitute(image.href, where=where) if image.href else None
            alt = ctx.substitute(image.alt, where=where)
            classified = classify(img)
            pattern = _ignore_pattern(classified.kind, options.ignore_kinds)
            if pattern is not None:
                ctx.ignored.append(f"{where}: badge {classified.kind} (matched ignore_kinds {pattern!r})")
                continue
            records.append(
                {
                    "kind": classified.kind,
                    "provider": classified.provider,
                    "params": list(classified.params),
                    "alt": alt,
                    "img": img,
                    "href": href,
                    "line": image.line,
                    "syntax": image.syntax,
                }
            )
        return {"present": True, "badges": records}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Findings for ``other`` relative to ``main`` (see the module docstring for the rules)."""
        options = self.badge_options
        file = options.file
        where = locator(file, "badges")
        repo = other.repo
        main_badges = _badges(main)
        other_badges = _badges(other)
        if not main.data.get("present"):
            if not other.data.get("present"):
                return []
            return [
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.FILE,
                    locator=file,
                    message=f"{file} only in repo ({_count(other_badges)})",
                    content_key=file,
                )
            ]
        if not other.data.get("present"):
            return [
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.FILE,
                    locator=file,
                    message=f"{file} missing (main has {_count(main_badges)})",
                    content_key=file,
                )
            ]

        findings: list[Finding] = []
        diff = diff_sets(main_badges, other_badges, key=_kind)
        for badge in diff.missing:
            findings.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.BADGE,
                    locator=where,
                    message=f"badge missing: {_kind(badge)} (main: {_describe(badge)})",
                    content_key=_kind(badge),
                )
            )
        for badge in diff.extra:
            findings.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.BADGE,
                    locator=where,
                    message=f"badge only in repo: {_kind(badge)} (repo: {_describe(badge)})",
                    content_key=_kind(badge),
                )
            )
        for mine, theirs in diff.common:
            if _signature(mine) == _signature(theirs):
                continue
            findings.append(
                self.finding(
                    repo=repo,
                    kind=Kind.DIFFERS,
                    subject=Subject.BADGE,
                    locator=where,
                    message=f"{_kind(mine)}: main uses {_describe(mine)} ; repo uses {_describe(theirs)}",
                    detail=f"main: {mine.get('img', '')}\nrepo: {theirs.get('img', '')}",
                    detail_kind="text",
                    content_key=_kind(mine),
                    direction=Direction.DOWNSTREAM,
                    severity=Severity.INFO,
                )
            )
        if options.order:
            shared = {_kind(mine) for mine, _ in diff.common}
            main_order = [k for k in dict.fromkeys(map(_kind, main_badges)) if k in shared]
            other_order = [k for k in dict.fromkeys(map(_kind, other_badges)) if k in shared]
            if diff_sequences(main_order, other_order).reordered:
                findings.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.REORDERED,
                        subject=Subject.BADGE,
                        locator=where,
                        message=f"badge order differs ({len(shared)} shared badges)",
                        detail=f"main: {', '.join(main_order)}\nrepo: {', '.join(other_order)}",
                        detail_kind="list",
                        content_key="order",
                        severity=Severity.INFO,
                        option=self.option_ref("order"),
                    )
                )
        return findings


__all__ = ["BadgesAspect", "BadgesOptions", "Classified", "classify"]
