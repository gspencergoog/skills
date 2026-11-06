#!/usr/bin/env python3
"""Computes weighted substantive line counts for a unified diff.

The counts drive review-depth gating (for example, whether a change is large
enough to justify a second reviewer). They never filter what reviewers see:
every file in the diff, including lockfiles and generated code, is still staged
and reviewed by the caller.

Usage:
    python3 diff_weight.py <diff-file>     # or '-' for stdin; prints JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from pathlib import PurePosixPath

LOW_CONTENT_KINDS = frozenset(
    {
        "lockfile",
        "generated",
        "snapshot_or_fixture",
        "asset_or_l10n",
        "docs",
        "pure_rename",
    }
)
TEST_LINE_WEIGHT = 0.25

_LOCKFILE_NAMES = frozenset(
    {
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pubspec.lock",
        "cargo.lock",
        "podfile.lock",
        "gemfile.lock",
        "poetry.lock",
        "uv.lock",
        "composer.lock",
        "go.sum",
        "flake.lock",
        "module.bazel.lock",
        "gradle.lockfile",
    }
)
_GENERATED_GLOBS = (
    "*.g.dart",
    "*.freezed.dart",
    "*.gr.dart",
    "*.pb.*",
    "*.pbjson.dart",
    "*.pbenum.dart",
    "*.pbserver.dart",
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*.generated.*",
    "*.min.js",
    "*.min.css",
    "*.bundle.js",
    "*.map",
)
_GENERATED_TOP_DIRS = frozenset({"dist", "build"})
_GENERATED_ANY_DIRS = frozenset({"vendor", "third_party", "node_modules"})
_SNAPSHOT_DIRS = frozenset(
    {"__snapshots__", "testdata", "fixtures", "__fixtures__", "golden", "goldens"}
)
_SNAPSHOT_GLOBS = ("*.snap", "*.golden.*", "*.golden")
_ASSET_SUFFIXES = frozenset(
    {
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".pdf",
        ".arb",
        ".po",
        ".pot",
        ".xlf",
        ".xliff",
    }
)
_DOC_SUFFIXES = frozenset({".rst", ".txt", ".adoc"})
_DOC_PREFIXES = ("changelog", "license", "notice")
# Markdown (skills, references, blueprints, design docs) counts like code.
# Every non-blank changed line counts, since Markdown headings and bullets
# would otherwise look like comments to is_logic_line().
_MARKDOWN_SUFFIXES = frozenset({".md", ".mdx"})
_TEST_DIRS = frozenset(
    {
        "test",
        "tests",
        "__tests__",
        "spec",
        "specs",
        "e2e",
        "integration_test",
        "javatests",
    }
)
_TEST_GLOBS = (
    "*_test.*",
    "test_*.*",
    "*.test.*",
    "*.spec.*",
    "*Test.java",
    "*Tests.java",
    "*Test.kt",
    "*Tests.kt",
)

_COMMENT_RE = re.compile(r"^(//|#|/\*|\*/|\*(\s|$)|<!--|-->|--(\s|$))")
_DELIMITER_RE = re.compile(r"^[{}\[\](),;]+$")
_IMPORT_RE = re.compile(
    r"^(import\s|from\s+\S+\s+import\s|export\s+\*\s+from\s|part\s|library\s|using\s)"
)
_WHITESPACE_RE = re.compile(r"\s+")
_RENAME_100_RE = re.compile(r"^similarity index 100%", re.MULTILINE)


def _classify_generated(path: PurePosixPath) -> bool:
    parts = path.parts
    if parts and parts[0] in _GENERATED_TOP_DIRS and len(parts) > 1:
        return True
    if any(p in _GENERATED_ANY_DIRS for p in parts[:-1]):
        return True
    return any(fnmatch(path.name, g) for g in _GENERATED_GLOBS)


def _classify_snapshot(path: PurePosixPath) -> bool:
    if any(p in _SNAPSHOT_DIRS for p in path.parts[:-1]):
        return True
    return any(fnmatch(path.name, g) for g in _SNAPSHOT_GLOBS)


def is_markdown_path(path: str) -> bool:
    """True for .md and .mdx files, which count every non-blank line."""
    return PurePosixPath(path).suffix.lower() in _MARKDOWN_SUFFIXES


def classify_low_content_path(path: str) -> str | None:
    """Returns a low-content kind for `path`, or None when its lines count."""
    p = PurePosixPath(path)
    name = p.name.lower()
    if name in _LOCKFILE_NAMES:
        return "lockfile"
    if _classify_generated(p):
        return "generated"
    if _classify_snapshot(p):
        return "snapshot_or_fixture"
    suffix = p.suffix.lower()
    if suffix in _ASSET_SUFFIXES:
        return "asset_or_l10n"
    if is_markdown_path(path):
        return None
    if suffix in _DOC_SUFFIXES or name.startswith(_DOC_PREFIXES):
        return "docs"
    return None


def is_test_path(path: str) -> bool:
    """True for files under test directories or with test-style names."""
    p = PurePosixPath(path)
    if any(part in _TEST_DIRS for part in p.parts[:-1]):
        return True
    return any(fnmatch(p.name, g) for g in _TEST_GLOBS)


def is_logic_line(content: str) -> bool:
    """False for blank, comment-only, bare delimiter, and import lines."""
    text = content.strip()
    if not text:
        return False
    if _COMMENT_RE.match(text) or _DELIMITER_RE.match(text):
        return False
    return not _IMPORT_RE.match(text)


def _normalize(content: str) -> str:
    return _WHITESPACE_RE.sub("", content).rstrip(",")


@dataclass(frozen=True)
class SubstantiveDiffStats:
    """Weighted line counts for a diff, used only for review-depth gating."""

    raw_lines: int = 0
    prod_logic_lines: int = 0
    test_logic_lines: int = 0
    substantive_lines: float = 0.0
    excluded_files: dict[str, str] = field(default_factory=dict)
    code_files: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _reflow_budget(lines: list[dict]) -> dict[str, Counter]:
    """Counts normalized texts present on both sides of one hunk."""
    removed = Counter(_normalize(r["content"]) for r in lines if r["op"] == "-")
    added = Counter(_normalize(r["content"]) for r in lines if r["op"] == "+")
    paired = removed & added
    return {"-": Counter(paired), "+": Counter(paired)}


def _count_hunk_logic(lines: list[dict], prose: bool = False) -> tuple[int, int]:
    """Returns (raw changed lines, logic lines) for one hunk's records.

    With `prose`, every non-blank line counts as logic (agent instructions).
    """
    budget = _reflow_budget(lines)
    raw = 0
    logic = 0
    for rec in lines:
        op = rec["op"]
        if op not in ("+", "-"):
            continue
        raw += 1
        key = _normalize(rec["content"])
        if budget[op][key] > 0:
            budget[op][key] -= 1
            continue
        counts = rec["content"].strip() if prose else is_logic_line(rec["content"])
        if counts:
            logic += 1
    return raw, logic


def _group_by_hunk(line_records: list[dict]) -> dict[tuple[str, int], list[dict]]:
    groups: dict[tuple[str, int], list[dict]] = {}
    for rec in line_records:
        groups.setdefault((rec["file_path"], rec["hunk_id"]), []).append(rec)
    return groups


def _pure_renames(diff_text: str) -> list[str]:
    """Returns target paths of file blocks that are 100% renames with no hunks."""
    from stage_diffs import _extract_file_name, _split_into_file_diffs

    renames = []
    for i, block in enumerate(_split_into_file_diffs(diff_text)):
        if _RENAME_100_RE.search(block) and "\n@@ " not in block:
            renames.append(_extract_file_name(block, i))
    return renames


def compute_substantive_diff_stats(diff_text: str) -> SubstantiveDiffStats:
    """Weights a diff's changed lines for review-depth gating.

    Low-content files count 0, blank/comment/import/delimiter lines count 0,
    whitespace-only reflow pairs in a hunk count 0, and test logic lines count
    TEST_LINE_WEIGHT. Markdown files (is_markdown_path) count every non-blank
    line as production logic. stage_diffs is imported
    lazily so it can import this module at top level without a cycle.
    """
    from stage_diffs import parse_diff_records

    _, line_records = parse_diff_records(diff_text)
    excluded = {p: "pure_rename" for p in _pure_renames(diff_text)}
    raw_total = prod = test = 0
    code_paths: set[str] = set()
    for (path, _), lines in _group_by_hunk(line_records).items():
        raw, logic = _count_hunk_logic(lines, prose=is_markdown_path(path))
        raw_total += raw
        kind = classify_low_content_path(path)
        if kind:
            excluded.setdefault(path, kind)
            continue
        code_paths.add(path)
        if is_test_path(path):
            test += logic
        else:
            prod += logic
    return SubstantiveDiffStats(
        raw_lines=raw_total,
        prod_logic_lines=prod,
        test_logic_lines=test,
        substantive_lines=round(prod + TEST_LINE_WEIGHT * test, 2),
        excluded_files=excluded,
        code_files=len(code_paths),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("diff", help="Unified diff file, or '-' for stdin.")
    args = parser.parse_args(argv)
    if args.diff == "-":
        text = sys.stdin.read()
    else:
        with open(args.diff, encoding="utf-8", errors="replace") as f:
            text = f.read()
    json.dump(compute_substantive_diff_stats(text).to_dict(), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
