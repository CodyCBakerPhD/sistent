# Instructions for automated agents

`sistent` ("consistent") checks that a fleet of repositories stays consistent with one *main* repository. Read
`README.md` for the concepts (main, satellites, aspects, snapshots, findings, downstream/upstream) before changing
behaviour.

## Layout

- `src/sistent/model.py` — data model. Imports nothing else from the package; everything depends on it.
- `src/sistent/compare/` — pure comparison primitives (sets, sequences, text similarity, mappings) and finding builders.
- `src/sistent/parsers/` — text-to-structure parsers (Markdown, badges, TOML, YAML). No knowledge of findings.
- `src/sistent/aspects/` — one module per aspect type; `base.py` holds the contract; `__init__.py` holds the built-in
  type map and the default aspect instances (`DEFAULT_CONFIG`).
- `src/sistent/repository.py` — read-only repository access, identity probes, `RepoContext`.
- `src/sistent/config.py`, `registry.py`, `sources/` — configuration loading, aspect-type registry, git materialisation.
- `src/sistent/api.py` — the pipeline; `cli.py` — the click layer; `render/` — text/markdown/json reports.
- `tests/` mirrors `src/`; `tests/fixtures/real/` holds README/AGENTS files of public scientific Python repos.

## Development

- Python >= 3.11, `hatchling`, src layout. Create the environment with `uv venv && uv pip install -e ".[dev]"`.
- Before committing run `ruff check .`, `ruff format .`, `mypy` (strict) and `pytest -q`. All four must pass.
- Keep the test suite fast and offline: build fixture repositories with the `make_repo` fixture, never clone from the
  network; git-dependent tests use the `requires_git` marker.
- Every public function has a docstring and full type annotations; use `from __future__ import annotations`.
- Do not add dependencies beyond `click` and `pyyaml` without discussing it in the pull request.

## Writing an aspect type

- Subclass `sistent.aspects.base.Aspect`; declare `type_name`, `options_cls` (a `BaseOptions` dataclass with a
  `help` metadata entry per field) and `description`.
- Read files only through the `RepoContext` (`ctx.read_text`, `ctx.glob`, `ctx.iter_files`).
- Pass every string that ends up in a snapshot through `ctx.substitute(text, where=locator)` so package, org and
  branch names become placeholders before comparison.
- Emit findings only through `self.finding(...)`; never construct `Finding` directly. Report `missing`/`extra` for
  the highest unmatched node only.
- Snapshots must be JSON-serialisable and comparison must be a pure function of two snapshots.
- Register built-in types in `sistent.aspects.BUILTIN` and in the `sistent.aspects` entry-point group in
  `pyproject.toml`; add a default instance to `DEFAULT_CONFIG` only when it is useful for most Python repositories.

## Testing an aspect type

- Test `extract()` on a `make_repo` fixture by asserting the snapshot data literally.
- Test `compare()` on hand-built snapshots by asserting the set of `(kind, subject, direction, locator)` tuples,
  never message text.
- Add a noise regression on the real fixtures when the aspect touches READMEs or agent-instruction files.

## Skills

Skills (`SKILL.md`, `.claude/skills/`, "Skills" sections) are managed by APM and ignored by default; keep it that way
unless the configuration explicitly opts in.
