# Instructions for automated agents

`example` is a small package used to exercise sistent.

## Development

- Python >= 3.11, `hatchling`, src layout. Create the environment with `uv venv && uv pip install -e ".[dev]"`.
- Before committing run `ruff check .` and `pytest -q`.
- Keep the test suite fast and offline.

## Release process

- Bump the version in `pyproject.toml` and tag the commit.

## Skills

- Use the `release` skill to publish.
