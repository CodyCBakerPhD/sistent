"""``workflows`` aspect type: GitHub Actions workflows — file set, triggers, jobs, matrices and action versions.

Extraction reduces every ``*.yml``/``*.yaml`` file under the workflow directory to its structure: the workflow
name, the trigger names (``on:`` as a string, list or mapping; a ``schedule`` is reduced to its presence), and per
job its name, ``runs-on``, matrix axes, the actions its steps ``use`` and a reusable-workflow reference. ``run:``
bodies, ``env``, ``if``, step names, ``permissions``, ``concurrency`` and ``paths``/``branches`` filters are ignored
in v1. Action versions are aggregated across all workflows because that is the robust cross-layout signal: a fleet
that splits its CI differently still wants ``actions/checkout`` pinned to the same version everywhere.

A workflow that does not parse is recorded under ``data["unparseable"]`` instead of failing the whole aspect.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from sistent.aspects.base import Aspect
from sistent.compare.findings import format_value
from sistent.compare.sequences import match_by_keys
from sistent.compare.sets import diff_sets
from sistent.compare.text import normalize_text
from sistent.model import Direction, Finding, Kind, Severity, Snapshot, Subject, locator
from sistent.options import BaseOptions
from sistent.parsers import ParseError, yaml_
from sistent.repository import RepoContext

Workflow = dict[str, Any]
Job = dict[str, Any]
JobItem = tuple[str, Job]

HEURISTIC_THRESHOLD = 0.5
"""Minimum Jaccard similarity of trigger set + job ids for two differently named workflows to be paired."""

_MATRIX_META = frozenset({"include", "exclude"})


@dataclass(frozen=True, kw_only=True)
class WorkflowsOptions(BaseOptions):
    """Options of the ``workflows`` aspect type."""

    dir: str = field(default=".github/workflows", metadata={"help": "Directory holding the workflow files."})
    ignore_files: list[str] = field(
        default_factory=list,
        metadata={"help": "Filename globs (fnmatch, case-insensitive) of workflows to skip in every repository."},
    )
    compare_matrix: bool = field(
        default=True,
        metadata={"help": "Extract and compare the strategy.matrix axes of matched jobs."},
    )


class WorkflowsAspect(Aspect):
    """Compare GitHub Actions workflows; see the module docstring."""

    type_name: ClassVar[str] = "workflows"
    options_cls: ClassVar[type[BaseOptions]] = WorkflowsOptions
    description: ClassVar[str] = "GitHub Actions workflows: file set, triggers, jobs, matrices and action versions."
    default_severity: ClassVar[dict[str, str]] = {"missing.file": "warning"}

    def __init__(self, name: str, options: BaseOptions) -> None:
        if not isinstance(options, WorkflowsOptions):
            raise TypeError(f"{type(self).__name__} requires WorkflowsOptions, got {type(options).__name__}")
        super().__init__(name, options)
        self.opts: WorkflowsOptions = options

    @property
    def directory(self) -> str:
        """The workflow directory without a trailing slash (locator prefix)."""
        return self.opts.dir.strip("/") or "."

    # ----- extraction -----------------------------------------------------------------------------------------

    def extract(self, ctx: RepoContext) -> dict[str, Any]:
        """``{"present", "workflows": [...], "actions": {action: [versions]}, "unparseable": {file: reason}}``."""
        directory = self.directory
        workflows: list[Workflow] = []
        unparseable: dict[str, str] = {}
        versions: dict[str, set[str]] = {}
        files = sorted(set(ctx.glob("*.yml", dirs=(directory,))) | set(ctx.glob("*.yaml", dirs=(directory,))))
        for rel in files:
            name = rel.rsplit("/", 1)[-1]
            if self._ignored_file(name):
                ctx.ignored.append(f"{rel} (ignore_files)")
                continue
            try:
                doc = yaml_.load(ctx.read_text(rel), source=rel)
            except ParseError as exc:
                unparseable[name] = exc.reason
                continue
            if doc is None:
                unparseable[name] = "empty document"
                continue
            if not isinstance(doc, Mapping):
                unparseable[name] = "top level is not a mapping"
                continue
            workflow = self._workflow(doc, name, ctx, where=locator(directory, name, sep=":"))
            workflows.append(workflow)
            for job in workflow["jobs"].values():
                for use in job["uses"]:
                    versions.setdefault(use["action"], set())
                    if use["version"] is not None:
                        versions[use["action"]].add(use["version"])
                if job["reusable"] is not None:
                    action, version = split_uses(job["reusable"])
                    versions.setdefault(action, set())
                    if version is not None:
                        versions[action].add(version)
        return {
            "present": ctx.exists(directory),
            "workflows": workflows,
            "actions": {action: sorted(found) for action, found in sorted(versions.items())},
            "unparseable": unparseable,
        }

    def _ignored_file(self, name: str) -> bool:
        folded = name.casefold()
        return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in self.opts.ignore_files)

    def _workflow(self, doc: Mapping[str, Any], file: str, ctx: RepoContext, *, where: str) -> Workflow:
        raw_name = doc.get("name")
        name = ctx.substitute(raw_name, where=where) if isinstance(raw_name, str) else None
        jobs: dict[str, Job] = {}
        raw_jobs = doc.get("jobs")
        if isinstance(raw_jobs, Mapping):
            for raw_id, raw_job in raw_jobs.items():
                job_id = ctx.substitute(str(raw_id), where=where)
                job_where = f"{where}:jobs.{job_id}"
                jobs[job_id] = self._job(raw_job if isinstance(raw_job, Mapping) else {}, ctx, where=job_where)
        return {"file": file, "name": name, "triggers": trigger_names(doc.get("on")), "jobs": jobs}

    def _job(self, job: Mapping[str, Any], ctx: RepoContext, *, where: str) -> Job:
        raw_name = job.get("name")
        name = ctx.substitute(raw_name, where=where) if isinstance(raw_name, str) else None
        runs_on = [ctx.substitute(text, where=where) for text in as_str_list(job.get("runs-on"))]
        matrix: dict[str, list[str]] = {}
        if self.opts.compare_matrix:
            for axis, values in matrix_axes(job.get("strategy")).items():
                substituted = {ctx.substitute(value, where=where) for value in values}
                matrix[ctx.substitute(axis, where=where)] = sorted(substituted)
        uses: list[dict[str, Any]] = []
        steps = job.get("steps")
        if isinstance(steps, list):
            for index, step in enumerate(steps):
                if not isinstance(step, Mapping) or not isinstance(step.get("uses"), str):
                    continue
                action, version = split_uses(step["uses"])
                uses.append({"action": ctx.substitute(action, where=where), "version": version, "step": index})
        raw_reusable = job.get("uses")
        reusable = ctx.substitute(raw_reusable.strip(), where=where) if isinstance(raw_reusable, str) else None
        return {"name": name, "runs_on": runs_on, "matrix": matrix, "uses": uses, "reusable": reusable}

    # ----- comparison -----------------------------------------------------------------------------------------

    def compare(self, main: Snapshot, other: Snapshot) -> list[Finding]:
        """Findings for ``other`` relative to ``main``: workflow set, per-workflow structure, action versions."""
        repo = other.repo
        directory = self.directory
        out: list[Finding] = []
        if not other.data.get("present"):
            if main.data.get("present"):
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MISSING,
                        subject=Subject.FILE,
                        locator=locator(directory),
                        message=f"directory missing: {directory}",
                    )
                )
            return out
        if not main.data.get("present"):
            return out

        main_bad: Mapping[str, str] = main.data.get("unparseable", {})
        other_bad: Mapping[str, str] = other.data.get("unparseable", {})
        for file, reason in sorted(other_bad.items()):
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.UNPARSEABLE,
                    subject=Subject.FILE,
                    locator=locator(directory, file, sep=":"),
                    message=f"cannot parse workflow {file}: {reason}",
                    detail=reason,
                    detail_kind="text",
                )
            )
        # A file that is unparseable on one side is neither missing nor extra on the other.
        main_workflows: list[Workflow] = [w for w in main.data.get("workflows", []) if w["file"] not in other_bad]
        other_workflows: list[Workflow] = [w for w in other.data.get("workflows", []) if w["file"] not in main_bad]

        by_file: Callable[[Workflow], Hashable | None] = lambda w: w["file"]  # noqa: E731
        by_name: Callable[[Workflow], Hashable | None] = lambda w: normalised_name(w.get("name"))  # noqa: E731
        matches = match_by_keys(main_workflows, other_workflows, keys=[by_file, by_name])
        pairs: list[tuple[Workflow, Workflow, int]] = list(matches.pairs)
        left_main = list(matches.unmatched_main)
        left_other = list(matches.unmatched_other)
        for main_workflow, other_workflow in heuristic_pairs(left_main, left_other):
            pairs.append((main_workflow, other_workflow, 2))
            left_main.remove(main_workflow)
            left_other.remove(other_workflow)

        for workflow in left_main:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.WORKFLOW,
                    locator=locator(directory, workflow["file"], sep=":"),
                    message=f"workflow missing: {workflow['file']}",
                    content_key=workflow["file"],
                )
            )
        for workflow in left_other:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.WORKFLOW,
                    locator=locator(directory, workflow["file"], sep=":"),
                    message=f"workflow only in repo: {workflow['file']}",
                    content_key=workflow["file"],
                )
            )
        main_actions: Mapping[str, list[str]] = main.data.get("actions", {})
        other_actions: Mapping[str, list[str]] = other.data.get("actions", {})
        for main_workflow, other_workflow, key_index in sorted(pairs, key=lambda p: p[0]["file"]):
            if key_index > 0:
                label = main_workflow.get("name") or main_workflow["file"]
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MOVED,
                        subject=Subject.WORKFLOW,
                        locator=locator(directory, main_workflow["file"], sep=":"),
                        message=(
                            f"workflow '{label}' is {main_workflow['file']} in main, {other_workflow['file']} in repo"
                        ),
                        content_key=main_workflow["file"],
                    )
                )
            out.extend(
                self._compare_workflow(
                    repo, main_workflow, other_workflow, main_all=set(main_actions), other_all=set(other_actions)
                )
            )
        out.extend(self._compare_actions(repo, main_actions, other_actions))
        return out

    def _compare_workflow(
        self,
        repo: str,
        main_workflow: Workflow,
        other_workflow: Workflow,
        *,
        main_all: set[str],
        other_all: set[str],
    ) -> list[Finding]:
        base = locator(self.directory, main_workflow["file"], sep=":")
        out: list[Finding] = []
        triggers = diff_sets(list(main_workflow["triggers"]), list(other_workflow["triggers"]))
        for trigger in triggers.missing:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.KEY,
                    locator=f"{base}:on.{trigger}",
                    message=f"trigger missing: {trigger}",
                    content_key=trigger,
                )
            )
        for trigger in triggers.extra:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.KEY,
                    locator=f"{base}:on.{trigger}",
                    message=f"trigger only in repo: {trigger}",
                    content_key=trigger,
                )
            )

        main_jobs: list[JobItem] = list(main_workflow["jobs"].items())
        other_jobs: list[JobItem] = list(other_workflow["jobs"].items())
        by_id: Callable[[JobItem], Hashable | None] = lambda item: item[0]  # noqa: E731
        by_name: Callable[[JobItem], Hashable | None] = lambda item: normalised_name(item[1].get("name"))  # noqa: E731
        matches = match_by_keys(main_jobs, other_jobs, keys=[by_id, by_name])
        for job_id, _job in matches.unmatched_main:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.JOB,
                    locator=f"{base}:jobs.{job_id}",
                    message=f"job missing: {job_id}",
                    content_key=job_id,
                )
            )
        for job_id, _job in matches.unmatched_other:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.JOB,
                    locator=f"{base}:jobs.{job_id}",
                    message=f"job only in repo: {job_id}",
                    content_key=job_id,
                )
            )
        for (main_id, main_job), (other_id, other_job), key_index in matches.pairs:
            job_base = f"{base}:jobs.{main_id}"
            if key_index > 0:
                label = main_job.get("name") or main_id
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MOVED,
                        subject=Subject.JOB,
                        locator=job_base,
                        message=f"job '{label}' is '{main_id}' in main, '{other_id}' in repo",
                        content_key=main_id,
                    )
                )
            out.extend(self._compare_job(repo, job_base, main_job, other_job, main_all=main_all, other_all=other_all))
        return out

    def _compare_job(
        self,
        repo: str,
        base: str,
        main_job: Job,
        other_job: Job,
        *,
        main_all: set[str],
        other_all: set[str],
    ) -> list[Finding]:
        out: list[Finding] = []
        if self.opts.compare_matrix:
            main_matrix: Mapping[str, list[str]] = main_job.get("matrix", {})
            other_matrix: Mapping[str, list[str]] = other_job.get("matrix", {})
            option = self.option_ref("compare_matrix")
            axes = diff_sets(list(main_matrix), list(other_matrix))
            for axis in axes.missing:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.MISSING,
                        subject=Subject.VALUE,
                        locator=f"{base}.strategy.matrix.{axis}",
                        message=f"matrix axis missing: {axis}",
                        content_key=axis,
                        severity=Severity.INFO,
                        option=option,
                    )
                )
            for axis in axes.extra:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.EXTRA,
                        subject=Subject.VALUE,
                        locator=f"{base}.strategy.matrix.{axis}",
                        message=f"matrix axis only in repo: {axis}",
                        content_key=axis,
                        option=option,
                    )
                )
            for axis, _axis in axes.common:
                main_values = sorted(set(main_matrix[axis]))
                other_values = sorted(set(other_matrix[axis]))
                if main_values != other_values:
                    out.append(
                        self.finding(
                            repo=repo,
                            kind=Kind.DIFFERS,
                            subject=Subject.VALUE,
                            locator=f"{base}.strategy.matrix.{axis}",
                            message=f"{axis}: main {', '.join(main_values)} ; repo {', '.join(other_values)}",
                            content_key=axis,
                            direction=Direction.DOWNSTREAM,
                            severity=Severity.INFO,
                            option=option,
                        )
                    )
        # Actions absent from the other repository altogether are reported once, at the aggregate level.
        actions = diff_sets(job_actions(main_job), job_actions(other_job))
        for action in actions.missing:
            if action not in other_all:
                continue
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.ACTION,
                    locator=f"{base}.steps",
                    message=f"action missing: {action}",
                    content_key=action,
                    severity=Severity.INFO,
                )
            )
        for action in actions.extra:
            if action not in main_all:
                continue
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.ACTION,
                    locator=f"{base}.steps",
                    message=f"action only in repo: {action}",
                    content_key=action,
                )
            )
        return out

    def _compare_actions(
        self, repo: str, main_actions: Mapping[str, list[str]], other_actions: Mapping[str, list[str]]
    ) -> list[Finding]:
        directory = self.directory
        out: list[Finding] = []
        diff = diff_sets(list(main_actions), list(other_actions))
        for action in diff.missing:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.MISSING,
                    subject=Subject.ACTION,
                    locator=locator(directory, action, sep=":"),
                    message=f"action missing: {action}",
                    content_key=action,
                    severity=Severity.INFO,
                )
            )
        for action in diff.extra:
            out.append(
                self.finding(
                    repo=repo,
                    kind=Kind.EXTRA,
                    subject=Subject.ACTION,
                    locator=locator(directory, action, sep=":"),
                    message=f"action only in repo: {action}",
                    content_key=action,
                )
            )
        for action, _action in diff.common:
            main_versions = sorted(set(main_actions[action]))
            other_versions = sorted(set(other_actions[action]))
            if main_versions != other_versions:
                out.append(
                    self.finding(
                        repo=repo,
                        kind=Kind.DIFFERS,
                        subject=Subject.ACTION,
                        locator=locator(directory, action, sep=":"),
                        message=f"{action}: main {_versions(main_versions)} ; repo {_versions(other_versions)}",
                        content_key=action,
                        direction=Direction.DOWNSTREAM,
                        severity=Severity.WARNING,
                    )
                )
        return out


# ----- helpers -----------------------------------------------------------------------------------------------------


def trigger_names(on: Any) -> list[str]:
    """Sorted, unique trigger names of an ``on:`` value given as a string, a list or a mapping."""
    if isinstance(on, str):
        names = [on]
    elif isinstance(on, list):
        names = [scalar_text(item) for item in on if item is not None]
    elif isinstance(on, Mapping):
        names = [str(key) for key in on]
    else:
        names = []
    return sorted(set(names))


def scalar_text(value: Any) -> str:
    """A string for any YAML value: strings as-is, everything else as compact JSON (``3.1``, ``true``)."""
    return value if isinstance(value, str) else format_value(value)


def as_str_list(value: Any) -> list[str]:
    """A string as a one-element list, a list element-wise, ``None`` as empty, anything else as one element."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [scalar_text(item) for item in value]
    return [scalar_text(value)]


def matrix_axes(strategy: Any) -> dict[str, list[str]]:
    """``{axis: sorted unique values}`` of ``strategy.matrix``; ``include``/``exclude`` are not axes."""
    matrix = strategy.get("matrix") if isinstance(strategy, Mapping) else None
    if not isinstance(matrix, Mapping):
        return {}
    return {str(axis): sorted(set(as_str_list(values))) for axis, values in matrix.items() if axis not in _MATRIX_META}


def split_uses(uses: str) -> tuple[str, str | None]:
    """``"actions/checkout@v4"`` -> ``("actions/checkout", "v4")``; no ``@`` (local, docker) -> ``(uses, None)``."""
    uses = uses.strip()
    if uses.startswith("docker://"):
        return uses, None
    action, sep, version = uses.rpartition("@")
    if not sep or not action:
        return uses, None
    return action, version


def normalised_name(name: Any) -> str | None:
    """Casefolded, whitespace-collapsed workflow/job name; ``None`` when absent or empty (unmatchable)."""
    if not isinstance(name, str):
        return None
    return normalize_text(name) or None


def job_actions(job: Job) -> list[str]:
    """Unique action names a job uses (step ``uses`` plus a reusable workflow reference), first-seen order."""
    names = [str(use["action"]) for use in job.get("uses", [])]
    reusable = job.get("reusable")
    if isinstance(reusable, str):
        names.append(split_uses(reusable)[0])
    return list(dict.fromkeys(names))


def _signature(workflow: Workflow) -> frozenset[str]:
    return frozenset(f"on:{t}" for t in workflow["triggers"]) | frozenset(f"job:{j}" for j in workflow["jobs"])


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def heuristic_pairs(
    main: Sequence[Workflow],
    other: Sequence[Workflow],
    *,
    threshold: float = HEURISTIC_THRESHOLD,
) -> list[tuple[Workflow, Workflow]]:
    """Pair workflows by Jaccard similarity of trigger set + job ids, best first, one-to-one, unique best only.

    A workflow whose best score is shared by two candidates is ambiguous and stays unpaired.
    """
    scored: list[tuple[float, int, int]] = []
    for i, main_workflow in enumerate(main):
        for j, other_workflow in enumerate(other):
            score = _jaccard(_signature(main_workflow), _signature(other_workflow))
            if score >= threshold:
                scored.append((score, i, j))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_main: set[int] = set()
    used_other: set[int] = set()
    pairs: list[tuple[Workflow, Workflow]] = []
    for score, i, j in scored:
        if i in used_main or j in used_other:
            continue
        rivals = [
            (i2, j2)
            for score2, i2, j2 in scored
            if score2 == score and (i2, j2) != (i, j) and i2 not in used_main and j2 not in used_other
        ]
        ambiguous = False
        if any(i2 == i for i2, _ in rivals):
            used_main.add(i)
            ambiguous = True
        if any(j2 == j for _, j2 in rivals):
            used_other.add(j)
            ambiguous = True
        if ambiguous:
            continue
        used_main.add(i)
        used_other.add(j)
        pairs.append((main[i], other[j]))
    return pairs


def _versions(versions: Sequence[str]) -> str:
    return ", ".join(versions) if versions else "(unpinned)"


__all__ = [
    "HEURISTIC_THRESHOLD",
    "WorkflowsAspect",
    "WorkflowsOptions",
    "as_str_list",
    "heuristic_pairs",
    "job_actions",
    "matrix_axes",
    "normalised_name",
    "scalar_text",
    "split_uses",
    "trigger_names",
]
