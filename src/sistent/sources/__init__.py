"""Repository sources: local paths and git URLs materialised into a cache.

:func:`resolve` turns a :class:`~sistent.config.RepoSpec` into a :class:`~sistent.repository.Repository`. ``path``
repos are used in place (never fetched); ``url`` repos are shallow-cloned into ``cache_dir`` by
:func:`~sistent.sources.git.materialise`. Every failure is a :class:`SourceError` naming the repo and the cause.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from sistent.repository import Repository
from sistent.sources.git import SourceError, cache_key, git_available, materialise

if TYPE_CHECKING:
    from sistent.config import RepoSpec

__all__ = ["SourceError", "cache_key", "git_available", "materialise", "resolve"]


def resolve(spec: RepoSpec, *, cache_dir: Path, fetch: bool = True, timeout: int = 120) -> Repository:
    """Materialise ``spec`` and return a :class:`Repository` rooted at ``checkout / spec.root``.

    A ``path`` repo must exist and be a directory. A ``url`` repo is materialised under ``cache_dir`` (``fetch``
    false only uses an existing cache entry). ``timeout`` bounds every git call.
    """
    if spec.path is not None:
        checkout = Path(spec.path)
        if not checkout.is_dir():
            raise SourceError(f"{spec.name}: path {checkout} does not exist or is not a directory")
    elif spec.url is not None:
        try:
            checkout = materialise(spec.url, spec.rev, cache_dir, fetch=fetch, timeout=timeout)
        except SourceError as exc:
            raise SourceError(f"{spec.name}: {exc}") from exc
    else:  # pragma: no cover - the config loader guarantees one of path/url
        raise SourceError(f"{spec.name}: neither path nor url configured")
    root = checkout if spec.root in ("", ".") else checkout / spec.root
    if not root.is_dir():
        raise SourceError(f"{spec.name}: root {spec.root!r} is not a directory inside {checkout}")
    return Repository(spec.name, root, checkout=checkout)
