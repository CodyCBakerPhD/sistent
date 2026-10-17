"""JSON report renderer: the documented report shape (``schema_version`` 1), never filtered."""

from __future__ import annotations

import json

from sistent.model import Report
from sistent.render import RenderOptions

__all__ = ["render_json"]


def render_json(report: Report, options: RenderOptions | None = None) -> str:
    """Pretty-print :meth:`Report.to_dict` with a trailing newline.

    ``options`` is accepted for a uniform renderer signature but ignored: the JSON report always carries every
    finding (including suppressed and baselined ones), every candidate and every error, in documented field order.
    """
    del options
    return json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n"
