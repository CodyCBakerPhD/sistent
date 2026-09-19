"""sistent: keep a fleet of repositories consistent with one main repository."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from sistent.model import Candidate, Direction, Finding, Kind, Report, Severity, Snapshot, Subject

try:
    __version__ = version("sistent")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without installation
    __version__ = "0.0.0"

__all__ = [
    "Candidate",
    "Direction",
    "Finding",
    "Kind",
    "Report",
    "Severity",
    "Snapshot",
    "Subject",
    "__version__",
]
