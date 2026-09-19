"""Built-in aspect types and the default aspect instances.

``BUILTIN`` maps a type name to the dotted ``module:Class`` path of its implementation; the registry imports them
lazily so that an unavailable optional dependency only breaks the aspect that needs it.

``DEFAULT_CONFIG`` is TOML text holding the default aspect instances. It is loaded through the same strict loader as
user configuration, and ``sistent init`` / ``sistent config`` print it so every default (including the skills-ignore
rules) is visible in a file the user owns.
"""

from __future__ import annotations

BUILTIN: dict[str, str] = {
    "markdown": "sistent.aspects.markdown:MarkdownAspect",
    "badges": "sistent.aspects.badges:BadgesAspect",
    "tree": "sistent.aspects.tree:TreeAspect",
    "files": "sistent.aspects.files:FilesAspect",
    "toml": "sistent.aspects.toml:TomlAspect",
    "workflows": "sistent.aspects.workflows:WorkflowsAspect",
    "precommit": "sistent.aspects.precommit:PrecommitAspect",
}

DEFAULT_CONFIG = """
# Agent-instruction files (AGENTS.md and relatives). Skills are managed by APM, so "Skills" sections are ignored.
[aspects.agent_instructions]
type = "markdown"
files = [
    "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".cursorrules", ".windsurfrules", ".clinerules",
    ".github/copilot-instructions.md",
]
merge = true
ignore_sections = ["skills?", "skills? .*", ".* skills?", ".* skills? .*"]
default_mode = "full"
similarity_threshold = 0.6
heading_order = false
severity = { "missing.file" = "error" }

# README section layout. Package-specific sections are only checked for presence; templated ones for similarity.
[aspects.readme]
type = "markdown"
files = ["README.md"]
merge = false
ignore_sections = ["table of contents", "contents", "toc"]
default_mode = "presence"
sections = { installation = "similar", documentation = "similar", license = "similar", contributing = "identical" }
heading_aliases = { installation = ["install", "how to install", "getting started", "quick start", "quickstart"] }
heading_order = true

# README badges (kind, provider and order).
[aspects.readme_badges]
type = "badges"
file = "README.md"
order = true

# Folder layout: directories two levels deep, files at the root, plus .github/ and docs/.
[aspects.layout]
type = "tree"
depth = 2
file_depth = 1
include = [".github/**", "docs/*"]
ignore = [
    ".git", "__pycache__", "*.egg-info", ".venv", "venv", ".tox", ".nox", "build", "dist", "htmlcov",
    ".mypy_cache", ".ruff_cache", ".pytest_cache", ".idea", ".vscode", "node_modules",
    ".claude/skills", "skills", "SKILL.md",
]

# Community / policy files: required when main has them; CODE_OF_CONDUCT must match main.
[aspects.community_files]
type = "files"
files = [
    "LICENSE*|COPYING*|license.*", "CONTRIBUTING.*", "CODE_OF_CONDUCT.*", "SECURITY.*",
    ".pre-commit-config.yaml", ".gitignore", ".codespellrc|.codespell*", "CITATION.cff",
]
required = "from-main"
identical = ["CODE_OF_CONDUCT.*"]
search_dirs = [".", ".github", "docs"]
ignore_patterns = ["(?:19|20)\\\\d\\\\d(?:\\\\s*[-\\u2013]\\\\s*(?:19|20)\\\\d\\\\d)?"]

# pyproject.toml: build backend, tool configuration and test/dev/docs extras.
[aspects.pyproject]
type = "toml"
file = "pyproject.toml"
[aspects.pyproject.keys]
"build-system" = "exact"
"project.requires-python" = "exact"
"project.classifiers" = "set"
"project.urls" = "keys"
"project.optional-dependencies.test" = "requirements"
"project.optional-dependencies.dev" = "requirements"
"project.optional-dependencies.docs" = "requirements"
"tool.ruff" = "exact"
"tool.pytest.ini_options" = "exact"
"tool.codespell" = "exact"
"tool.coverage" = "exact"
"tool.mypy" = "exact"

# GitHub Actions workflows: file set, triggers, jobs, matrices and action versions.
[aspects.workflows]
type = "workflows"
dir = ".github/workflows"

# pre-commit hooks and their pinned revisions.
[aspects.pre_commit]
type = "precommit"
file = ".pre-commit-config.yaml"
"""

DEFAULT_ASPECT_NAMES: tuple[str, ...] = (
    "agent_instructions",
    "readme",
    "readme_badges",
    "layout",
    "community_files",
    "pyproject",
    "workflows",
    "pre_commit",
)

__all__ = ["BUILTIN", "DEFAULT_ASPECT_NAMES", "DEFAULT_CONFIG"]
