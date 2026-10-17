"""Tiny aspect types used by the api/cli tests so they do not depend on the built-in aspects."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sistent.aspects.base import Aspect, UnparseableFile
from sistent.compare.findings import findings_from_setdiff
from sistent.compare.sets import diff_sets
from sistent.model import Finding, Kind, Snapshot, Subject, locator
from sistent.options import BaseOptions
from sistent.repository import RepoContext


@dataclass(frozen=True, kw_only=True)
class LinesOptions(BaseOptions):
    file: str = field(default="RULES.txt", metadata={"help": "File whose non-blank lines are compared as a set."})


class LinesAspect(Aspect):
    """Compare the non-blank lines of one file as an unordered set of rules."""

    type_name = "lines"
    options_cls = LinesOptions
    description = "Test aspect: the non-blank lines of one file as a set."

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        file = self.options.file  # type: ignore[attr-defined]
        if not ctx.exists(file):
            return {"present": False, "lines": []}
        text = ctx.read_text(file)
        lines = [ctx.substitute(line.strip(), where=file) for line in text.splitlines() if line.strip()]
        return {"present": True, "lines": lines}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        file = self.options.file  # type: ignore[attr-defined]
        if main.data["present"] and not other.data["present"]:
            return [
                self.finding(
                    repo=other.repo,
                    kind=Kind.MISSING,
                    subject=Subject.FILE,
                    locator=file,
                    message=f"{file} missing",
                    content_key=file,
                )
            ]
        if not main.data["present"]:
            return []
        diff = diff_sets(main.data["lines"], other.data["lines"])
        return findings_from_setdiff(
            diff,
            aspect=self,
            repo=other.repo,
            subject=Subject.RULE,
            locator=lambda line: locator(file, line),
            describe=lambda line: line,
            content_key=lambda line: line,
        )


class BoomAspect(Aspect):
    """Always fails during extraction (exercises RunError handling)."""

    type_name = "boom"
    description = "Test aspect: raises during extraction."

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        raise RuntimeError("boom")

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        return []


class UnparseableAspect(Aspect):
    """Raises UnparseableFile for every repo whose ``BROKEN`` file exists."""

    type_name = "unparseable"
    description = "Test aspect: reports BROKEN as unparseable."

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        if ctx.exists("BROKEN"):
            raise UnparseableFile("BROKEN", "cannot parse")
        return {}

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        return []
