#!/usr/bin/env python3
"""Classifies changed files as code or as a document type for code-review.

The code-review skill uses this to decide which files get the code review
mandate and which get the document review mandate, and which rubric applies.

Only prose files (.md, .rst, .adoc, .txt) and JSON Schema files (*.schema.json,
or .json files whose "$schema" names a json-schema.org meta-schema) can be
documents. Everything else is "code". For eligible files, ranked rules are
checked in order and the first match wins. Each result names the rule that
matched so the caller can override a wrong call.

Usage:
  classify_artifacts.py [--root DIR] [--no-content] PATH...
  git diff --name-only main...HEAD | classify_artifacts.py [--root DIR] -

Prints a JSON list of {"path", "type", "rule", "rubric"} objects.
Exits 0 on success and 2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import NamedTuple

DOC_TYPES = ("normative-spec", "design", "blueprint", "reference-doc")
PROSE_EXTENSIONS = (".md", ".rst", ".adoc", ".txt")
SNIFF_BYTES = 8192

RUBRICS = {
    "normative-spec": "references/rubrics/normative_specs.md",
    "design": "references/rubrics/design_docs.md",
    "blueprint": "references/rubrics/blueprints.md",
    "reference-doc": "references/rubrics/reference_docs.md",
}

_FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
_BLUEPRINT_TYPE = re.compile(
    r"^type:\s*['\"]?(module|feature|codebase)['\"]?\s*$", re.MULTILINE
)
# A JSON Schema document declares a json-schema.org meta-schema. Config files
# that only point at a validation schema (tsconfig, schemastore) do not.
_JSON_SCHEMA_META = re.compile(r'"\$schema"\s*:\s*"https?://json-schema\.org/')
_FENCED_BLOCK = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.DOTALL | re.MULTILINE)
_DESIGN_HEADING = re.compile(
    r"^(?:#{1,6}\s*(?:alternatives?(?:\s+considered)?|non-goals|decision"
    r"|drawbacks|unresolved questions|consequences)\b"
    r"|\**status\**:)",
    re.IGNORECASE | re.MULTILINE,
)
# RFC 2119 / RFC 8174: only the uppercase forms are normative.
_RFC2119 = re.compile(r"\b(?:MUST|SHALL|SHOULD|REQUIRED|RECOMMENDED|MAY|OPTIONAL)\b")

_DESIGN_DIRS = frozenset({"rfcs", "rfc", "adr", "adrs", "proposals"})
_SPEC_DIRS = frozenset({"spec", "specs", "specification", "specifications"})


class Rule(NamedTuple):
    """One ranked classifier rule."""

    name: str
    doc_type: str
    matches: Callable[[PurePosixPath, str], bool]


def _frontmatter(content: str) -> str:
    match = _FRONTMATTER.match(content)
    return match.group(1) if match else ""


def _is_blueprint(path: PurePosixPath, content: str) -> bool:
    if path.name.endswith(".blueprint.md"):
        return True
    return bool(_BLUEPRINT_TYPE.search(_frontmatter(content)))


def _prose_only(content: str) -> str:
    """Removes fenced code blocks so quoted code and prompts don't count."""
    return _FENCED_BLOCK.sub("", content)


def _is_schema(path: PurePosixPath, content: str) -> bool:
    return path.suffix.lower() == ".json"


def _in_dirs(path: PurePosixPath, dirs: frozenset[str]) -> bool:
    return any(part.lower() in dirs for part in path.parts[:-1])


def _is_design(path: PurePosixPath, content: str) -> bool:
    if _in_dirs(path, _DESIGN_DIRS):
        return True
    return len(_DESIGN_HEADING.findall(_prose_only(content))) >= 2


def _is_normative_prose(path: PurePosixPath, content: str) -> bool:
    if _in_dirs(path, _SPEC_DIRS):
        return True
    lines = _prose_only(content).splitlines()
    keyword_lines = sum(1 for line in lines if _RFC2119.search(line))
    return keyword_lines >= 3


def _is_changelog(path: PurePosixPath, content: str) -> bool:
    del content  # Path-only rule.
    return path.name.upper().startswith("CHANGELOG")


RULES: tuple[Rule, ...] = (
    Rule("blueprint-frontmatter-or-name", "blueprint", _is_blueprint),
    Rule("json-schema", "normative-spec", _is_schema),
    Rule("design-headings-or-dir", "design", _is_design),
    Rule("rfc2119-keywords-or-spec-dir", "normative-spec", _is_normative_prose),
    Rule("changelog", "reference-doc", _is_changelog),
    Rule("prose-default", "reference-doc", lambda _path, _content: True),
)


def is_document_eligible(path: str, content: str | None = None) -> bool:
    """Returns True if the file can be classified as a document.

    Prose files are always eligible. JSON files are eligible only when they
    are JSON Schema documents: named *.schema.json, or declaring a
    json-schema.org meta-schema in their "$schema" key.
    """
    name = PurePosixPath(path).name.lower()
    if name.endswith(PROSE_EXTENSIONS) or name.endswith(".schema.json"):
        return True
    return name.endswith(".json") and bool(_JSON_SCHEMA_META.search(content or ""))


# ---------------------------------------------------------------------------
# Generalized Reuse Ring Classification (Blast Radius & Review Depth)
# ---------------------------------------------------------------------------

RING_LABELS: dict[int, str] = {
    0: "Ring 0 (Private / Local)",
    1: "Ring 1 (Internal Tooling & Repo Plumbing)",
    2: "Ring 2 (Application / Service Logic)",
    3: "Ring 3 (Shared Libraries & Public Contracts)",
    4: "Ring 4 (Foundational Runtimes & Core Engines)",
}

RING_LEVELS: dict[int, tuple[str, list[str]]] = {
    0: ("fast", ["--fast"]),
    1: ("standard", []),
    2: ("standard", []),
    3: ("panel", ["--panel"]),
    4: ("max", ["--max"]),
}

_CI_DIRS = frozenset({".github", ".gitlab", ".circleci", ".buildkite", "jenkins"})
_CI_FILES = frozenset(
    {
        "jenkinsfile",
        ".gitlab-ci.yml",
        ".travis.yml",
        ".cirrus.yml",
    }
)

_TOOL_CONFIG_FILES = frozenset(
    {
        "makefile",
        "cmakelists.txt",
        "dockerfile",
        "containerfile",
        "taskfile.yml",
        "taskfile.yaml",
        "justfile",
        "vagrantfile",
    }
)

_TOOL_CONFIG_PREFIXES = (
    ".eslintrc",
    "eslint.config.",
    "tsconfig",
    ".prettierrc",
    "analysis_options",
    "ruff.",
    ".flake8",
    ".rubocop",
    ".golangci",
)

_TOOL_SCRIPT_DIRS = frozenset({"scripts", "tools", "tool", "tasks", "dev"})

_TEST_DIRS = frozenset(
    {
        "test",
        "tests",
        "__tests__",
        "testing",
        "fixtures",
        "mocks",
        "testdata",
    }
)

_REPO_META_FILES = frozenset(
    {
        "readme.md",
        "contributing.md",
        "license",
        "license.md",
        "license.txt",
        "code_of_conduct.md",
        "agents.md",
        "gemini.md",
    }
)

_SCRATCH_DIRS = frozenset(
    {
        "scratch",
        "tmp",
        "temp",
        "sandbox",
        "experiments",
        "playground",
        "drafts",
        "notes",
        "personal",
    }
)

_DOTFILE_DIRS = frozenset({".config", ".local", "dotfiles", "cheats"})

_DOTFILE_NAMES = frozenset(
    {
        "bashrc",
        ".bashrc",
        "bash_profile",
        ".bash_profile",
        "zshrc",
        ".zshrc",
        "cheats",
        ".cheats",
    }
)

_ENGINE_DIRS = frozenset(
    {
        "engine",
        "runtime",
        "vm",
        "compiler",
        "kernel",
        "allocator",
        "gc",
        "skwasm",
        "wimp",
    }
)

_NATIVE_DIRS = frozenset({"native", "ffi", "cbits", "jni"})

_SCHEMA_DIRS = frozenset({"protos", "proto", "schemas", "schema"})

_PUBLIC_EXPORT_DIRS = frozenset({"api", "public", "exports", "include"})

_PACKAGE_MANIFESTS = frozenset(
    {
        "package.json",
        "pubspec.yaml",
        "cargo.toml",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "pom.xml",
        "go.mod",
        "api.txt",
    }
)


def classify_file_ring(path: str) -> tuple[int, str]:
    """Classifies a single file into a reuse ring (0 to 4) and reason rule."""
    pure = PurePosixPath(path.replace("\\", "/"))
    parts_lower = [p.lower() for p in pure.parts]
    name_lower = pure.name.lower()

    # 1. Scratch / Private / Personal (Ring 0)
    if any(part in _SCRATCH_DIRS for part in parts_lower[:-1]):
        return 0, "scratch-or-sandbox"
    if (
        ".scratch." in name_lower
        or ".local." in name_lower
        or ".private." in name_lower
    ):
        return 0, "scratch-or-private-file"
    if (
        any(part in _DOTFILE_DIRS for part in parts_lower[:-1])
        or name_lower in _DOTFILE_NAMES
    ):
        return 0, "personal-dotfiles"
    if (
        name_lower.startswith(".")
        and not any(part in _CI_DIRS for part in parts_lower)
        and not any(name_lower.startswith(p) for p in _TOOL_CONFIG_PREFIXES)
    ):
        return 0, "dotfile"

    # 2. Core Engine / Runtimes / Compilers (Ring 4)
    if any(part in _ENGINE_DIRS for part in parts_lower[:-1]):
        return 4, "core-engine-or-runtime"
    if any(part in _NATIVE_DIRS for part in parts_lower[:-1]):
        return 4, "native-or-ffi-plumbing"
    if name_lower.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs")) and any(
        part in ("engine", "native", "runtime", "vm", "core") for part in parts_lower
    ):
        return 4, "native-core-code"

    # 3. Shared Libraries, SDKs & Public Contracts (Ring 3)
    if any(part in _SCHEMA_DIRS for part in parts_lower[:-1]) or name_lower.endswith(
        (".proto", ".schema.json")
    ):
        return 3, "schema-or-protocol-contract"
    if name_lower in _PACKAGE_MANIFESTS:
        return 3, "package-manifest-or-api-surface"
    if any(part in _PUBLIC_EXPORT_DIRS for part in parts_lower[:-1]):
        return 3, "public-export-directory"
    if (
        len(pure.parts) == 2
        and pure.parts[0].lower() == "lib"
        and name_lower.endswith(".dart")
    ):
        return 3, "dart-library-entrypoint"
    if (
        name_lower in ("index.ts", "index.js", "public-api.ts", "mod.ts")
        and len(pure.parts) <= 2
    ):
        return 3, "package-root-entrypoint"

    # 4. Tooling, CI, Tests & Meta (Ring 1)
    if any(part in _CI_DIRS for part in parts_lower[:-1]) or name_lower in _CI_FILES:
        return 1, "ci-workflow-or-pipeline"
    if name_lower in _TOOL_CONFIG_FILES or any(
        name_lower.startswith(p) for p in _TOOL_CONFIG_PREFIXES
    ):
        return 1, "build-or-linter-config"
    if any(part in _TOOL_SCRIPT_DIRS for part in parts_lower[:-1]):
        return 1, "repo-tooling-or-scripts"
    if (
        any(part in _TEST_DIRS for part in parts_lower[:-1])
        or name_lower.startswith("test_")
        or name_lower.endswith(
            (
                "_test.dart",
                "_test.py",
                "_test.go",
                ".test.ts",
                ".test.js",
                ".spec.ts",
                ".spec.js",
            )
        )
    ):
        return 1, "test-suite-or-fixture"
    if name_lower in _REPO_META_FILES:
        return 1, "repo-documentation"

    # 5. Application / Service Business Logic (Ring 2 - Default baseline)
    return 2, "application-logic"


def classify_ring(paths: list[str], root: Path | str | None = None) -> dict:
    """Classifies a changeset into an effective ring and default review level."""
    del root  # Reserved for future manifest inspection.
    if not paths:
        level, flags = RING_LEVELS[2]
        return {
            "ring": 2,
            "label": RING_LABELS[2],
            "review_level": level,
            "flags": flags,
            "summary": (
                f"{RING_LABELS[2]} -> Review Level: {level.capitalize()} (1 reviewer)"
            ),
            "highest_file": None,
            "highest_rule": "default-fallback",
            "files": [],
        }

    scored_files = []
    max_ring = -1
    highest_file = None
    highest_rule = None

    for path in paths:
        ring, rule = classify_file_ring(path)
        scored_files.append({"path": path, "ring": ring, "rule": rule})
        if ring > max_ring:
            max_ring = ring
            highest_file = path
            highest_rule = rule

    effective_ring = max(0, max_ring)
    level, flags = RING_LEVELS[effective_ring]
    summary_extra = f" ({' '.join(flags)})" if flags else " (1 reviewer)"
    summary = (
        f"{RING_LABELS[effective_ring]} -> Review Level:"
        f" {level.capitalize()}{summary_extra}"
    )

    return {
        "ring": effective_ring,
        "label": RING_LABELS[effective_ring],
        "review_level": level,
        "flags": flags,
        "summary": summary,
        "highest_file": highest_file,
        "highest_rule": highest_rule,
        "files": scored_files,
    }


def classify(path: str, content: str | None = None) -> dict:
    """Classifies one file.

    Args:
      path: Repo-relative path of the changed file.
      content: The file's leading text, or None to classify by path only
        (for example, a file the diff deletes).

    Returns:
      {"path", "type", "rule", "rubric"}. "type" is "code" or one of
      DOC_TYPES. "rubric" is the rubric reference path, or None for code.
    """
    if not is_document_eligible(path, content):
        return {"path": path, "type": "code", "rule": "not-prose", "rubric": None}
    pure = PurePosixPath(path.replace("\\", "/"))
    text = content or ""
    for rule in RULES:
        if rule.matches(pure, text):
            return {
                "path": path,
                "type": rule.doc_type,
                "rule": rule.name,
                "rubric": RUBRICS[rule.doc_type],
            }
    raise AssertionError("prose-default rule always matches")


def _read_head(root: Path, path: str) -> str | None:
    try:
        with open(root / path, "rb") as handle:
            return handle.read(SNIFF_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify changed files for code-review.",
        exit_on_error=False,
    )
    parser.add_argument("--root", default=".", help="Repo root for reading files.")
    parser.add_argument(
        "--no-content", action="store_true", help="Classify by path only."
    )
    parser.add_argument(
        "--ring",
        action="store_true",
        help="Classify changeset reuse ring (0-4) and default review level.",
    )
    parser.add_argument("paths", nargs="+", help="File paths, or - to read stdin.")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Runs the CLI. Returns the process exit code."""
    try:
        args = _parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        print(__doc__, file=sys.stderr)
        return 2
    paths = args.paths
    if paths == ["-"]:
        paths = [line.strip() for line in sys.stdin if line.strip()]
    root = Path(args.root)
    if args.ring:
        ring_result = classify_ring(paths, root)
        json.dump(ring_result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    results = []
    for path in paths:
        content = None if args.no_content else _read_head(root, path)
        results.append(classify(path, content))
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
