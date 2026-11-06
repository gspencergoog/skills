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
  classify_artifacts.py [--root DIR] [--ring | --combined] [--diff-file DIFF] [PATH...]
  git diff --name-only main...HEAD | classify_artifacts.py [--root DIR] -

Prints a JSON list of {"path", "type", "rule", "rubric"} objects (or a JSON
object under --ring or --combined).
Exits 0 on success and 2 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import NamedTuple

_CONTEXT_INDEXER_SCRIPTS = str(
    Path(__file__).resolve().parents[2] / "context-indexer" / "scripts"
)
if _CONTEXT_INDEXER_SCRIPTS not in sys.path:
    sys.path.insert(0, _CONTEXT_INDEXER_SCRIPTS)

import diff_weight
from stage_diffs import _extract_file_name, _split_into_file_diffs

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

PANEL_MIN_LINES = 225
FOCUS_MIN_LINES = 400

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


def _score_ring_files(
    paths: list[str],
) -> tuple[list[dict], int, str | None, str | None]:
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
    return scored_files, max(0, max_ring), highest_file, highest_rule


def classify_ring(
    paths: list[str],
    root: Path | str | None = None,
    substantive_lines: float | None = None,
    panel_min_lines: int = PANEL_MIN_LINES,
) -> dict:
    """Classifies a changeset into an effective ring and default review level.

    When ring == 3 and substantive_lines <= panel_min_lines, returns
    review_level "standard", flags [], api_mandate True, and a gate_reason
    string, instead of review_level "panel" and flags ["--panel"].
    """
    del root  # Reserved for future manifest inspection.
    if not paths:
        level, flags = RING_LEVELS[2]
        return {
            "ring": 2,
            "label": RING_LABELS[2],
            "review_level": level,
            "flags": list(flags),
            "api_mandate": False,
            "gate_reason": None,
            "summary": (
                f"{RING_LABELS[2]} -> Review Level: {level.capitalize()} (1 reviewer)"
            ),
            "highest_file": None,
            "highest_rule": "default-fallback",
            "files": [],
        }

    scored_files, effective_ring, highest_file, highest_rule = _score_ring_files(paths)
    level, default_flags = RING_LEVELS[effective_ring]
    flags = list(default_flags)
    api_mandate = False
    gate_reason: str | None = None

    if (
        effective_ring == 3
        and substantive_lines is not None
        and substantive_lines <= panel_min_lines
    ):
        level = "standard"
        flags = []
        api_mandate = True
        gate_reason = (
            f"substantive_lines ({substantive_lines:g}) <= "
            f"panel_min_lines ({panel_min_lines})"
        )

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
        "api_mandate": api_mandate,
        "gate_reason": gate_reason,
        "summary": summary,
        "highest_file": highest_file,
        "highest_rule": highest_rule,
        "files": scored_files,
    }


def _extract_paths_from_diff(diff_text: str) -> list[str]:
    """Extracts unique repo-relative file paths from a unified diff in order."""
    seen: set[str] = set()
    paths: list[str] = []
    for i, block in enumerate(_split_into_file_diffs(diff_text)):
        path = _extract_file_name(block, i)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def classify_combined(
    paths: list[str],
    root: Path | str = Path("."),
    diff_text: str | None = None,
    *,
    no_content: bool = False,
) -> dict:
    """Returns {"ring": ..., "artifacts": [...], "substantive": asdict(stats),
    "focus_dedicated_allowed": substantive_lines is None or > FOCUS_MIN_LINES}.
    """
    root_path = Path(root)
    if diff_text is not None:
        stats = diff_weight.compute_substantive_diff_stats(diff_text)
        substantive_lines: float | None = stats.substantive_lines
        if not paths:
            paths = _extract_paths_from_diff(diff_text)
    else:
        stats = diff_weight.SubstantiveDiffStats()
        substantive_lines = None

    ring_result = classify_ring(paths, root_path, substantive_lines=substantive_lines)
    artifacts = [
        classify(path, None if no_content else _read_head(root_path, path))
        for path in paths
    ]
    return {
        "ring": ring_result,
        "artifacts": artifacts,
        "substantive": asdict(stats),
        "focus_dedicated_allowed": (
            substantive_lines is None or substantive_lines > FOCUS_MIN_LINES
        ),
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


def _read_diff_file(diff_file: str) -> str:
    if diff_file == "-":
        return sys.stdin.read()
    with open(diff_file, encoding="utf-8", errors="replace") as handle:
        return handle.read()


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
    parser.add_argument(
        "--combined",
        action="store_true",
        help="Output combined ring, artifact, and substantive diff classification.",
    )
    parser.add_argument(
        "--diff-file",
        default=None,
        help="Unified diff file for substantive line counting and path extraction.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        help="File paths, or - to read stdin (optional when --diff-file is set).",
    )
    return parser.parse_args(argv)


def _resolve_paths_and_diff(
    args: argparse.Namespace,
) -> tuple[list[str], str | None]:
    if args.diff_file == "-" and args.paths == ["-"]:
        raise argparse.ArgumentError(
            None,
            "cannot read both --diff-file and paths from stdin (-); omit - so paths come from --diff-file -",
        )
    diff_text = _read_diff_file(args.diff_file) if args.diff_file else None
    paths = list(args.paths)
    if paths == ["-"]:
        paths = [line.strip() for line in sys.stdin if line.strip()]
    elif not paths and diff_text is not None:
        paths = _extract_paths_from_diff(diff_text)
    return paths, diff_text


def _write_json(payload: object) -> int:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def main(argv: list[str]) -> int:
    """Runs the CLI. Returns the process exit code."""
    try:
        args = _parse_args(argv)
        if not args.paths and not args.diff_file:
            raise argparse.ArgumentError(None, "paths or --diff-file required")
        paths, diff_text = _resolve_paths_and_diff(args)
    except (argparse.ArgumentError, OSError, SystemExit):
        print(__doc__, file=sys.stderr)
        return 2

    root = Path(args.root)
    if args.combined:
        return _write_json(
            classify_combined(paths, root, diff_text, no_content=args.no_content)
        )
    if args.ring:
        substantive_lines = (
            diff_weight.compute_substantive_diff_stats(diff_text).substantive_lines
            if diff_text is not None
            else None
        )
        return _write_json(
            classify_ring(paths, root, substantive_lines=substantive_lines)
        )
    results = [
        classify(path, None if args.no_content else _read_head(root, path))
        for path in paths
    ]
    return _write_json(results)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
