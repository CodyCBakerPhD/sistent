# Catalogue of inconsistencies sistent catches

This is the developer reference for *what* `sistent check` reports, case by case, and which part of `sistent.toml`
controls each case. Every entry names the aspect type that detects it, the finding it produces (`kind`/`subject`,
direction, default severity, locator shape) and the options that tune or silence it. The user-facing overview lives
in `README.md`; the design rationale in `AGENTS.md`.

Conventions used below:

- **downstream** = main has it, the satellite lacks it or does it differently → the satellite should adopt it. Gates
  CI through `fail_on`.
- **upstream** = the satellite has it, main lacks it → a *candidate for main*, aggregated across satellites, always
  `info`, never fails the build.
- **none** = neither side is authoritative (content differs both ways, stale reference, self-inconsistency).
- Severities can be overridden per aspect with `severity = { "<kind>.<subject>" = "...", "<kind>" = "..." }`.
- Any finding can be silenced with `ignore = ["<aspect>:<locator-glob>"]` on a repo or `ignore = ["<locator-glob>"]`
  on an aspect, or accepted once with a baseline (`--update-baseline`).

Every string is passed through identity substitution before comparison: each repo's package name, import name, repo
slug, GitHub org and default branch become `{{name}}`, `{{org}}` and `{{branch}}`. A sentence, URL or path that only
differs by those names is therefore *not* an inconsistency. Add spellings that the probes cannot discover with
`[repos.X] aliases = [...]`, and extra placeholders with `vars = { company = "Acme" }`.

## 1. Agent-instruction files (`agent_instructions`, type `markdown`)

Files considered, in order: `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `.cursorrules`, `.windsurfrules`, `.clinerules`,
`.github/copilot-instructions.md` (option `files`). With `merge = true` all present files fold into one logical
document per repo. A `CLAUDE.md` that is a symlink to `AGENTS.md` or contains only `@AGENTS.md` counts as an *alias*
of that file, not as separate content.

| Case | Example | Finding | Tune with |
|---|---|---|---|
| A satellite has no instruction file at all while main has one | main has `AGENTS.md`; `repo-3` has nothing | `missing`/`file`, downstream, **error**, locator `AGENTS.md` | `files`, `severity = { "missing.file" = "warning" }` |
| The satellite uses a different file name for the same role | main: `AGENTS.md` (+ `CLAUDE.md` → `AGENTS.md` alias); `repo-1`: only `CLAUDE.md` | `missing`/`file` for the role main uses (`AGENTS.md`, warning, message says what the repo uses instead) and `extra`/`file` for `CLAUDE.md` (upstream); when the sets are disjoint this collapses to one `differs`/`name` (info, none) | `files`; per-repo `ignore = ["agent_instructions:AGENTS.md"]` if the naming is intentional |
| A satellite has an instruction file main lacks | `repo-2` has `.github/copilot-instructions.md`, main does not | `extra`/`file`, upstream (info) | `files` |
| `AGENTS.md` and `CLAUDE.md` in one repo have drifted apart | both exist as full documents with different content for the same heading | `differs`/`section`, none, warning, locator `AGENTS.md+CLAUDE.md#<Heading>` (self-check, also reported for main) | keep one file and alias the other |
| Document titles differ | `# Instructions for automated agents` vs `# Custom instructions for GitHub Copilot in the **{{name}}** repository` | `differs`/`title`, none, info | `title = false` |
| A section of main is missing | main has `## Development > Testing`; the satellite has no matching heading | `missing`/`section`, downstream, warning, locator `AGENTS.md#Development > Testing`; the detail says how many subsections and rules are missing with it; children of a missing section are never reported separately | `ignore_sections`, `sections = { testing = "presence" }` |
| The satellite has a section main lacks | `## Release process` only in the satellite | `extra`/`section`, upstream (info), aggregated as *k of N satellites have it* | `ignore_sections` |
| A section exists but sits under a different parent | main: `Usage > Example`; satellite: `Example` at the top level | `moved`/`section`, downstream, info (matched by heading suffix, or by leaf heading when the bodies are similar) | `heading_aliases` when the headings are synonyms |
| Headings are the same instruction under different names | `Installation` vs `Getting started` | matched as the same section when listed in `heading_aliases`; otherwise `missing` + `extra` | `heading_aliases = { installation = ["getting started", "quick start"] }` |
| A rule (bullet/numbered item) of main is missing | main: `- Run pytest before committing`; the satellite's section lacks it | `missing`/`rule`, downstream, warning; `content_key` is the normalised rule text | `default_mode`/`sections` (`presence` or `similar` ignore rules), `paragraph_rules` |
| A rule was reworded | `Run pytest before committing` vs `run pytest tests/ before committing` (similarity ≥ `rule_match_cutoff`) | `differs`/`rule`, none, warning, both texts in the detail | `rule_match_cutoff` |
| A rule exists but in another section | main has it under *Testing*, the satellite under *Development* | `moved`/`rule`, downstream, info | – |
| The satellite has rules main lacks | `- Keep the test suite fast and offline.` only in the satellite | `extra`/`rule`, upstream (info) | – |
| Rules are the same but in a different order (unordered lists) | – | **not a finding** (rules are compared as sets) | – |
| A numbered procedure has its steps reordered | `1. commit 2. push` vs `1. push 2. commit` | `reordered`/`rule`, downstream, info, list in the detail | – |
| Prose of a matched section only gained lines in the satellite | an extra paragraph | `extra`/`prose`, upstream (info), inserted lines in the detail | `similarity_threshold` |
| Prose of a matched section lost lines | a paragraph of main is absent | `missing`/`prose`, downstream, warning, deleted lines in the detail | – |
| Prose was rewritten | word-shingle similarity below `similarity_threshold` (default 0.6) with lines changed on both sides | `differs`/`prose`, none, warning, unified diff in the detail, `option` cites the threshold | `similarity_threshold`, `sections = { x = "identical" }` for verbatim sections |
| Minor wording changes above the threshold | – | **not a finding** | lower `similarity_threshold` to be stricter |
| Code blocks differ | commands in fenced blocks | ignored unless `compare_code = true` (then treated like prose under subject `code`) | `compare_code` |
| Top-level sections appear in a different order | – | `reordered`/`section`, downstream, info, locator `AGENTS.md#(order)`; off by default for instructions | `heading_order` |
| A "Skills" section exists | `## Skills` in any file | **ignored by default** (skills are managed by APM); listed under *Ignored* in the footer and in `sistent snapshot --explain` | `ignore_sections` (regexes, full match, case-insensitive) |
| Headings differ only by numbering, emoji, markup or the package name | `## 1 Environment awareness` vs `## Environment awareness`; `## 🚀 Quick start`; `## Citing **NeuroConv**` | **not a finding** (heading normalisation + identity substitution) | – |
| Documents use `#` for every section in one repo and `##` in another | – | **not a finding** (levels are rebased after the title) | – |
| A file cannot be parsed | binary or absurdly large file, `.rst` instruction file | `unparseable`/`file`, none, warning (info for `.rst`, unsupported in v1); the aspect is skipped for that satellite; a failure in **main** disables the aspect for the run (a run error) | – |
| A file mentions another configured repo's name | roiextractors' `AGENTS.md` says "see the neuroconv docs" | `stale`/`reference`, none, warning, excerpt in the detail; reported for main too | `[sistent] stale = false`, `[repos.X] substitute = false`, `aliases` |

## 2. README layout (`readme`, type `markdown`)

Same machinery as above with different defaults: `merge = false`, `default_mode = "presence"` (sections are only
required to exist), `heading_order = true`, `ignore_sections = ["table of contents", "contents", "toc"]`, and per-section
modes `installation = "similar"`, `documentation = "similar"`, `license = "similar"`, `contributing = "identical"`.

| Case | Finding | Tune with |
|---|---|---|
| A README section of main is absent | `missing`/`section`, downstream, warning | `sections`, `ignore_sections` |
| The satellite has a README section main lacks (e.g. *Funding*) | `extra`/`section`, upstream (info); candidate for main | per-repo `ignore = ["readme:README.md#Funding*"]` when intentional |
| Sections appear in a different order | `reordered`/`section`, downstream, info, locator `README.md#(order)` | `heading_order = false` |
| *Installation*/*Documentation*/*License* prose diverges beyond wording (after `{{name}}` substitution) | `differs`/`prose` (none, warning) or `missing`/`extra` prose | `sections`, `similarity_threshold` |
| *Contributing* text is not verbatim main's | `differs`/`prose`, downstream, warning, diff in the detail | `sections = { contributing = "similar" }` |
| Package-specific sections (*About*, *Features*, *Usage*) differ in content | **not a finding** (presence only) | `sections = { usage = "similar" }` to compare them |
| README is `README.rst` | `unparseable`/`file`, info (RST sections are not parsed in v1; badges are) | `files` |

## 3. README badges (`readme_badges`, type `badges`)

Badges are recognised by host (shields.io, badge.fury.io, codecov, coveralls, readthedocs, zenodo, pre-commit.ci,
mybinder, pepy, anaconda, GitHub Actions `badge.svg`, ...) and classified into kinds after identity substitution:
`pypi-version`, `pypi-license`, `pypi-pyversions`, `pypi-downloads`, `conda-version`, `ci:<workflow-file>`, `coverage`,
`docs`, `doi`, `license`, `pre-commit`, `binder`, `code-style:<tool>`, `static:<label>[:<message>]`,
`social:<platform>`, `other:<host>/<path>`.

| Case | Finding | Tune with |
|---|---|---|
| A badge kind of main is absent (no CI badge, no license badge, ...) | `missing`/`badge`, downstream, warning, locator `README.md#badges`, `content_key` = kind | `ignore_kinds` |
| The satellite shows a badge main lacks (e.g. downloads) | `extra`/`badge`, upstream (info); candidate for main | `ignore_kinds` |
| Same kind, different provider or parameters (codecov vs coveralls, `pypi/dm` vs `pypi/dw`) | `differs`/`badge`, downstream, info | – |
| Same badges in a different order | `reordered`/`badge`, downstream, info, both orders in the detail | `order = false` |
| Badge URLs differ only by package/org/branch name, style, logo or colour | **not a finding** | – |
| A badge still points at another configured repo (`pypi/l/pynwb` in roiextractors) | `stale`/`reference`, none, warning | `stale`, `aliases` |
| No README in the satellite while main has one | `missing`/`file`, downstream, error | `severity` |

## 4. Folder layout (`layout`, type `tree`)

Directories up to `depth = 2`, files at the root (`file_depth = 1`) plus `include = [".github/**", "docs/*"]`, with
build artefacts, caches, virtualenvs and skills directories ignored (`ignore`). Path components are identity-substituted
(`src/neuroconv/` → `src/{{name}}/`). Only tracked or untracked-but-not-gitignored paths count.

| Case | Finding | Tune with |
|---|---|---|
| A directory of main is absent (`docs/`, `tests/`, `.github/workflows/`) | `missing`/`path`, downstream, warning; nothing inside a missing directory is reported separately | `depth`, `ignore`, per-repo `ignore = ["layout:docs/"]` |
| A root file of main is absent (`CHANGELOG.md`, `.pre-commit-config.yaml`, `pyproject.toml`) | `missing`/`path`, downstream, warning | `file_depth`, `include` |
| The satellite has directories/files main lacks | `extra`/`path`, upstream (info) | – |
| Flat layout vs `src/` layout | `moved`/`path`, downstream, info (`main: src/{{name}}/ ; repo: {{name}}/`) | – |
| The package directory is named after the repo | **not a finding** (substituted to `{{name}}`) | `aliases` when the import name is unrelated to the repo name |
| `.claude/skills/`, `skills/`, `SKILL.md` | **ignored by default** | `ignore` |

## 5. Community and policy files (`community_files`, type `files`)

Entries are any-of globs searched in `search_dirs = [".", ".github", "docs"]`: `LICENSE*|COPYING*|license.*`,
`CONTRIBUTING.*`, `CODE_OF_CONDUCT.*`, `SECURITY.*`, `.pre-commit-config.yaml`, `.gitignore`, `.codespellrc|.codespell*`,
`CITATION.cff`. `required = "from-main"`: only files main has are required.

| Case | Finding | Tune with |
|---|---|---|
| Main has a file the satellite lacks (no `CODE_OF_CONDUCT`, no `CITATION.cff`) | `missing`/`file`, downstream, **error**, message names main's location | `files`, `severity = { "missing.file" = "warning" }` |
| The satellite has one main lacks | `extra`/`file`, upstream (info) | – |
| Same file in a different place or spelling (`docs/CONTRIBUTING.rst` vs `CONTRIBUTING.md`, `license.txt` vs `LICENSE`) | `moved`/`file`, downstream, info (any-of globs across `search_dirs` make these the same entry) | `search_dirs` |
| `CODE_OF_CONDUCT` text differs from main (after years, names and whitespace are normalised) | `differs`/`file`, downstream, warning, diff in the detail | `identical`, `ignore_patterns` |
| A file every repo must have regardless of main | `required = "all"`: `missing`/`file` for every satellite, and a `none` warning for main itself | `required` |

## 6. `pyproject.toml` (`pyproject`, type `toml`)

Keys are compared with a mode each (`[aspects.pyproject.keys]`): `exact` (`build-system`, `project.requires-python`,
`tool.ruff`, `tool.pytest.ini_options`, `tool.codespell`, `tool.coverage`, `tool.mypy`), `set` (`project.classifiers`),
`keys` (`project.urls`), `requirements` (`project.optional-dependencies.test|dev|docs`), `present`. Keys absent in main
are never required.

| Case | Finding | Tune with |
|---|---|---|
| No `pyproject.toml` in the satellite while main has one | `missing`/`file`, downstream, error | `required`, `severity` |
| A configured key of main is absent (`tool.mypy`, `tool.ruff.lint`) | `missing`/`key`, downstream, warning, locator `pyproject.toml:tool.ruff.lint` | `keys` |
| A scalar or list differs under `exact` (`tool.ruff.line-length` 120 vs 100, `requires-python`, `build-system.requires`) | `differs`/`value`, downstream, warning, `main: 120 ; repo: 100` | `keys` (use `present` to only require existence) |
| A key exists only in the satellite | `extra`/`key`, upstream (info) | – |
| Classifier missing/extra (order ignored) | `missing`/`value` (warning) or `extra`/`value` (info) | `keys` |
| `project.urls` has different keys (values are package-specific and ignored) | `missing`/`key` or `extra`/`key` | `keys` |
| A test/dev/docs requirement is missing | `missing`/`value`, downstream, warning (`requirement missing: mypy>=1.10`) | `keys` |
| A shared requirement is pinned differently (`pytest>=8` vs `pytest>=7`) | `differs`/`value`, downstream, info | – |
| Values that embed the package name (`packages = ["src/neuroconv"]`) | **not a finding** (substituted) | `aliases` |
| The file cannot be parsed | `unparseable`/`file`, none, warning | – |

## 7. GitHub Actions (`workflows`, type `workflows`)

| Case | Finding | Tune with |
|---|---|---|
| The satellite has no `.github/workflows` while main has one | `missing`/`file`, downstream, warning | `dir` |
| A workflow of main is absent (matched by file name, then `name`, then triggers + jobs) | `missing`/`workflow`, downstream, warning, locator `.github/workflows:ci.yml` | `ignore_files` |
| The satellite has a workflow main lacks | `extra`/`workflow`, upstream (info) | – |
| Same workflow under another file name | `moved`/`workflow`, downstream, info | – |
| A trigger differs (`on: pull_request` missing, no `schedule`) | `missing`/`extra` subject `key`, locator `...:ci.yml:on.pull_request` | – |
| A job is missing/extra/renamed | `missing`/`extra`/`moved` subject `job`, locator `...:ci.yml:jobs.test` | – |
| A matrix axis differs (Python versions, OS) | `missing`/`extra`/`differs` subject `value`, info, locator `...:jobs.test.strategy.matrix.python-version` | `compare_matrix = false` |
| An action is used in main but not in the satellite (per job) | `missing`/`action`, info | – |
| An action is pinned to different versions across the fleet (`actions/checkout@v4` vs `@v3`) | `differs`/`action`, downstream, warning, locator `.github/workflows:actions/checkout` (aggregated across all workflows) | – |
| `run:` scripts, `env`, `if`, permissions, path filters differ | **not compared** in v1 | – |
| A workflow file cannot be parsed | `unparseable`/`file` for that file only | – |

## 8. pre-commit hooks (`pre_commit`, type `precommit`)

| Case | Finding | Tune with |
|---|---|---|
| No `.pre-commit-config.yaml` while main has one | `missing`/`file`, downstream, warning (pre-commit is optional tooling) | `severity` |
| A hook repository of main is absent (`astral-sh/ruff-pre-commit`) | `missing`/`key`, downstream, warning, locator `.pre-commit-config.yaml:astral-sh/ruff-pre-commit` | – |
| The satellite uses a hook repository main lacks | `extra`/`key`, upstream (info) | – |
| A hook id is missing/extra within a shared repository (`ruff-format`) | `missing`/`extra` subject `hook` | – |
| A shared repository is pinned to a different `rev` | `differs`/`value`, downstream, warning (`main v0.6.9 ; repo v0.5.0`) | `compare_rev = false` |

## 9. Cross-cutting cases

| Case | Behaviour |
|---|---|
| A satellite cannot be resolved (path missing, clone failed, `--no-fetch` without cache) | reported as `unavailable` in the summary; the others are still checked; exit code 3 unless `--allow-unavailable` |
| Main cannot be resolved | nothing to compare against: exit code 2 |
| An aspect crashes or main's file is unparseable | a run error (counts as `error` for the exit code, even with `fail_on = never`); the aspect is skipped for the run |
| A repo does not apply to an aspect | `[repos.X] skip_aspects`, or aspect `tags` vs repo `tags`: no findings, listed as a non-target |
| An intentional, permanent difference | `[repos.X] ignore = ["<aspect>:<locator-glob>"]` → the finding stays in the JSON report as `suppressed: true` |
| Accepted drift that should not fail CI until it changes | `--update-baseline` records finding ids; stale baseline entries are reported in the footer |
| Improvements that several satellites share | *Candidates for main* section: `2/3 agent_instructions AGENTS.md#Release process (roiextractors, spikeinterface)` |

## Adding a case

1. Decide the aspect type (or add one: see `AGENTS.md`, "Writing an aspect type").
2. Emit the finding through `Aspect.finding(...)` with the `kind`/`subject` pair from `sistent.model` that fits; the
   direction and default severity follow from the kind.
3. Add a test asserting the `(kind, subject, direction, locator)` tuple, and a row in this catalogue. The test in
   `tests/test_docs.py` checks that every default aspect and every finding kind is mentioned here.
