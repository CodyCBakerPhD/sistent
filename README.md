# sistent

[![CI](https://github.com/CodyCBakerPhD/sistent/actions/workflows/ci.yml/badge.svg)](https://github.com/CodyCBakerPhD/sistent/actions/workflows/ci.yml)
[![PyPI version](https://img.shields.io/pypi/v/sistent.svg)](https://pypi.org/project/sistent/)
[![Python versions](https://img.shields.io/pypi/pyversions/sistent.svg)](https://pypi.org/project/sistent/)
[![License](https://img.shields.io/pypi/l/sistent.svg)](https://github.com/CodyCBakerPhD/sistent/blob/main/LICENSE)

Keep a fleet of repositories consistent with one **main** repository.

`sistent` reads one central `sistent.toml`, materialises every listed repository (local path or shallow git clone),
extracts a normalised **snapshot** of each configured **aspect** from every repository, and compares each
**satellite** against **main**. Aspects are generic and configurable: agent-instruction files (`AGENTS.md`,
`CLAUDE.md`, `.cursorrules`, ...), README section layout and badges, folder structure, community files, `pyproject.toml`
sections, GitHub Actions workflows, pre-commit hooks, or anything you add through a plugin.

Two kinds of results come out:

- **Drift** (`downstream`): main has something a satellite lacks or does differently. The satellite should adopt it.
  Drift gates CI through `fail_on`.
- **Candidates for main** (`upstream`): a satellite has something main lacks. These are improvements that may
  percolate *into* main (and from there to every other repo). They are aggregated across satellites
  ("2 of 3 satellites have a *Release process* section") and never fail the build.

Skills (`SKILL.md`, `.claude/skills/`, "Skills" sections) are ignored by default because they are managed by APM.

## Installation

```bash
pip install sistent
# or
uv tool install sistent
```

Python >= 3.11. Runtime dependencies: `click`, `pyyaml`. `git` must be on `PATH` for `url` repositories.

## Quick start

```toml
# sistent.toml
[sistent]
main = "neuroconv"
github = "catalystneuro"   # default owner for repos without path/url

[repos.neuroconv]
path = "../neuroconv"      # a local checkout (relative to this file) ...

[repos.roiextractors]      # ... or https://github.com/catalystneuro/roiextractors, cloned into ~/.cache/sistent

[repos.spikeinterface]
url = "SpikeInterface/spikeinterface"
```

```console
$ sistent check
sistent 0.1.0 · main neuroconv @3f9a2c1 · 2 satellites · 8 aspects · fail_on = error

repo             status  error  warn  info  candidates
neuroconv (main) ok          0     0     0           –
roiextractors    ok          0     3     2           1
spikeinterface   ok          1     2     4           2
TOTAL                        1     5     6           3

roiextractors
  agent_instructions
    warn  missing    AGENTS.md#Development > Testing     rule missing: 'Run pytest before committing'
  readme
    warn  differs    README.md#License                   similarity 0.41 (+2 −5 lines; --diff to show)
  pyproject
    warn  differs    pyproject.toml:tool.ruff.line-length  main: 120 ; repo: 100

spikeinterface
  agent_instructions
    error missing    AGENTS.md                           no agent-instruction file (main has AGENTS.md)
  ...

Candidates for main (upstream, never fail the build)
  2/2  agent_instructions  AGENTS.md#Release process     section not in main   (roiextractors, spikeinterface)
  1/2  readme_badges       README.md#badges              badge only in repo: pypi-downloads (roiextractors)

1 finding at or above fail_on = error (worst: error) → exit 1
```

Write a fully commented starter configuration (every default visible) with `sistent init`, optionally pointing at
your main checkout and its siblings:

```bash
sistent init --from ../neuroconv --repos '../*/'
```

The complete, case-by-case list of what is reported (with the finding each case produces and the option that tunes
it) is in [`docs/inconsistencies.md`](docs/inconsistencies.md).

## Concepts

| Term | Meaning |
|---|---|
| **main** | The repository named by `[sistent] main`. It is the source of truth; satellites are expected to look like it. |
| **satellite** | Every other configured repository. Each is compared against main independently. |
| **aspect** | One configured comparison over one facet of a repository. An aspect is a named *instance* of a generic aspect *type*; the same type can be instantiated several times (`markdown` serves both `agent_instructions` and `readme`). |
| **snapshot** | The normalised, JSON-serialisable representation an aspect extracts from one repository. All noise removal happens at extraction; comparison is a pure function of two snapshots. `sistent snapshot REPO` prints them. |
| **finding** | One observed inconsistency: kind (`missing`, `extra`, `differs`, `moved`, `reordered`, `stale`, `unparseable`), subject (`file`, `section`, `rule`, `prose`, `badge`, `key`, `value`, `path`, ...), severity, direction, locator, message. |
| **identity** | Each repo's package name, import name, repo slug, GitHub org and default branch are replaced by `{{name}}`, `{{org}}`, `{{branch}}` placeholders before anything is compared, so templated sentences and URLs are not reported as drift. |
| **stale** | A repo that still mentions *another* configured repo's name (a `pypi/l/pynwb` badge in `roiextractors`) — usually a copy-paste leftover. |

## Configuration

`sistent.toml` is found through `-c/--config`, `$SISTENT_CONFIG`, or by searching upward from the current directory.
Relative paths resolve against the config file's directory. Loading is strict: unknown tables, keys or option names
are errors with a "did you mean" hint.

### `[sistent]`

| Key | Default | Meaning |
|---|---|---|
| `main` | required | Name of the `[repos.X]` table that is the source of truth. |
| `github` | – | Default GitHub owner; a repo with neither `path` nor `url` resolves to `https://github.com/<github>/<name>`. |
| `cache_dir` | `$XDG_CACHE_HOME/sistent` | Where `url` repos are cloned (shallow, one directory per URL and revision). |
| `fail_on` | `"error"` | `info`, `warning`, `error` or `never`: exit 1 when any non-suppressed downstream finding reaches this severity. |
| `git_timeout` | `120` | Seconds per git command. |
| `jobs` | `8` | Parallel workers for cloning and extraction. |
| `defaults` | `true` | Keep the built-in default aspects (override them by name; `false` = only your own). |
| `stale` | `true` | Report mentions of other configured repos' names. |
| `baseline` | – | Baseline file for accepted findings (see below). |

### `[repos.<name>]`

| Key | Meaning |
|---|---|
| `path` | Local checkout. Never fetched. |
| `url` | Git URL; `"org/repo"` expands to GitHub. Cloned shallowly into `cache_dir`. |
| `rev` | Branch, tag or SHA to check out (`url` only; default: the remote HEAD). |
| `root` | Sub-directory treated as the repository root (monorepos). |
| `tags` | Free labels; aspects with `tags` only apply to repos sharing one. |
| `skip_aspects` | Aspect instances not run for this repo. |
| `ignore` | Suppression globs `"<aspect>:<locator-glob>"` (the aspect part is optional), e.g. `"readme:README.md#Funding*"`. Suppressed findings stay in the JSON report with `suppressed: true`. |
| `aliases` | Extra identity aliases (display names, old names). |
| `vars` | Identity overrides (`name`, `org`, `branch`) or extra placeholders (`{ company = "Acme" }` turns "Acme" into `{{company}}`). |
| `substitute` | `false` disables identity substitution for this repo. |

### `[aspects.<name>]`

Every aspect table takes `type` (a registered type name or a dotted `"pkg.module:Class"` path), `enabled`, `tags`,
`ignore` (locator globs) and `severity` (overrides keyed by `"<kind>"` or `"<kind>.<subject>"`), plus the options of its
type. A table whose name is one of the defaults *overrides* that default key by key (lists and dicts replace as a
whole); `enabled = false` removes it. `sistent aspects` lists every type with its options; `sistent config` prints the
fully resolved configuration with `# default` after every inherited key.

The default aspects:

| Name | Type | What it compares |
|---|---|---|
| `agent_instructions` | `markdown` | `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`, `.github/copilot-instructions.md`, merged into one logical document: instruction files present, sections, bullet-level **rules** (as sets, so reordering is not drift), prose similarity. "Skills" sections are ignored. A satellite without any instruction file is an `error`. |
| `readme` | `markdown` | `README.md` section presence and order; *Installation*, *Documentation* and *License* sections compared for similarity, *Contributing* for identity; "Table of contents" ignored. |
| `readme_badges` | `badges` | Badge kinds (`pypi-version`, `ci:<workflow>`, `coverage`, `docs`, `doi`, `license`, ...), provider and order. Package/org names in badge URLs are normalised away. |
| `layout` | `tree` | Directories two levels deep and root files, plus `.github/**` and `docs/*`; `src/<pkg>` becomes `src/{{name}}`. Build artefacts, caches and skills directories are ignored. |
| `community_files` | `files` | Presence of `LICENSE`, `CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY`, `.pre-commit-config.yaml`, `.gitignore`, codespell config, `CITATION.cff` (required when main has them); `CODE_OF_CONDUCT` must match main (years and names normalised). |
| `pyproject` | `toml` | `build-system`, `requires-python`, classifiers (as a set), `project.urls` (keys only), `test`/`dev`/`docs` extras (as requirements: names, then specifiers), `tool.ruff`, `tool.pytest.ini_options`, `tool.codespell`, `tool.coverage`, `tool.mypy` (exact). |
| `workflows` | `workflows` | `.github/workflows`: workflow set (matched by file name, then `name`), triggers, jobs, matrices, and `uses:` action versions across all workflows. |
| `pre_commit` | `precommit` | `.pre-commit-config.yaml` repos, hooks and pinned revisions. |

Example overrides and additions:

```toml
[aspects.agent_instructions]
similarity_threshold = 0.5
severity = { "missing.file" = "error", missing = "warning" }

[aspects.readme]
tags = ["python"]
sections = { installation = "similar", license = "similar", contributing = "identical", usage = "presence" }

[aspects.workflows]
enabled = false

[aspects.changelog]           # a new instance of a built-in type
type = "markdown"
files = ["CHANGELOG.md"]
default_mode = "presence"

[aspects.docs_conf]           # your own aspect type
type = "mytools.sistent_aspects:SphinxConfAspect"
file = "docs/conf.py"
```

### Markdown comparison modes

The `markdown` type compares documents section by section. Sections are matched by normalised heading path (numbering,
emoji, markup and package names removed; headings rebased so `#` vs `##` conventions do not matter), then by heading
suffix, then by leaf heading when the bodies are similar. Each section has a *mode*, chosen by `sections` (per heading
or path glob) or `default_mode`:

| Mode | Checks |
|---|---|
| `presence` | The section exists (and, with `heading_order`, keeps its position). |
| `identical` | Normalised body lines are equal; main is authoritative. |
| `similar` | Word-shingle similarity of the prose; lines only added in the satellite are upstream candidates, lines only removed are drift, a rewrite below `similarity_threshold` is `differs` with a diff. |
| `rules` | Bullet/numbered items are compared as sets of normalised rules (reworded rules are paired by similarity; rules moved to another section are `moved`). |
| `full` | `rules` for list items plus `similar` for the remaining prose (the default for agent instructions). |

## Command line

| Command | Purpose |
|---|---|
| `sistent check` | Run everything and print the report (`--format text\|markdown\|json`, `--aspect`, `--repo`, `--tag`, `--fail-on`, `--min-severity`, `--direction`, `--kind`, `--diff`, `--summary`, `--group-by`, `--sort`, `--no-fetch`, `--jobs`, `--baseline`, `--update-baseline`, `--allow-unavailable`, `-o FILE`). |
| `sistent snapshot REPO` | Print the normalised snapshot(s) of one repo (`--aspect`, `--explain` adds applied aliases and ignored sections). |
| `sistent diff REPO [REPO2]` | Unified diff of main's snapshot against REPO's (`--raw` diffs the source files). |
| `sistent repos` | Table of configured repos, sources, cache status and identity aliases. |
| `sistent fetch` | Clone or update the `url` repos without checking. |
| `sistent aspects` | List aspect types and their options. |
| `sistent config` | Print the fully resolved configuration. |
| `sistent init [PATH]` | Write a commented starter configuration (`--from`, `--repos`, `--github`). |

Exit codes: `0` clean, `1` findings at or above `fail_on` or run errors, `2` usage/config error or main unavailable,
`3` one or more satellites unavailable (the others are still reported; `--allow-unavailable` maps it to 0/1).

The markdown format is meant for GitHub issues and `$GITHUB_STEP_SUMMARY`; `--repo X --format markdown` gives a
single-repo issue body with task-list items. The JSON format is stable (`schema_version`) and carries every finding,
including suppressed and baselined ones.

### Baselines

Accepted drift can be recorded so only *new* findings are reported:

```bash
sistent check --update-baseline            # writes .sistent-baseline.json (or [sistent] baseline)
sistent check                              # baselined findings are hidden and never fail the build
```

Finding ids are stable across runs (hash of aspect, repo, kind, subject, locator and normalised content), and stale
baseline entries are reported in the footer.

## Library use

```python
from sistent.api import RunOptions, run
from sistent.config import load_config

report = run(load_config("sistent.toml"), options=RunOptions(fetch=False))
for finding in report.visible_findings:
    print(finding.repo, finding.severity, finding.locator, finding.message)
print(report.candidates)  # upstream findings aggregated across satellites
```

## Writing your own aspect type

```python
from sistent.aspects.base import Aspect
from sistent.model import Kind, Subject, locator
from sistent.options import BaseOptions
from dataclasses import dataclass, field


@dataclass(frozen=True, kw_only=True)
class SphinxOptions(BaseOptions):
    file: str = field(default="docs/conf.py", metadata={"help": "Sphinx configuration file."})


class SphinxConfAspect(Aspect):
    type_name = "sphinx"
    options_cls = SphinxOptions
    description = "Sphinx extensions and theme."

    def extract(self, ctx):
        text = ctx.read_text(self.options.file) if ctx.exists(self.options.file) else ""
        return {"present": bool(text), "extensions": sorted(_extensions(ctx.substitute(text)))}

    def compare(self, main, other):
        missing = set(main.data["extensions"]) - set(other.data["extensions"])
        return [
            self.finding(
                repo=other.repo,
                kind=Kind.MISSING,
                subject=Subject.VALUE,
                locator=locator(self.options.file, "extensions", sep=":"),
                message=f"extension missing: {ext}",
                content_key=ext,
            )
            for ext in sorted(missing)
        ]
```

Reference it as `type = "mypkg.aspects:SphinxConfAspect"` or publish it under the `sistent.aspects` entry-point group.
Snapshots must be JSON-serialisable; read files only through the context; pass strings through `ctx.substitute`;
build findings only with `self.finding` so direction, severity and ids are derived consistently.

## License

BSD-3-Clause.
