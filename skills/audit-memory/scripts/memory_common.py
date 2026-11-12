#!/usr/bin/env python3
"""Shared YAML frontmatter, link-graph, and path-containment helpers for audit-memory."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
from typing import Any

PROTECTED_FILENAMES: frozenset[str] = frozenset({
    "preferences.md",
    "communication_guidelines.md",
    "user.md",
})

FENCE_RE = re.compile(r"^\s*(?:`{3,}|~{3,})")
DIRECT_TAG_RE = re.compile(r"(?:^|\s)#direct(?:\b|$)")
CLAIM_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\S")
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)\)")
KEY_VALUE_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$")
EXTERNAL_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
BLOCK_SCALAR_MARKERS = frozenset({">", ">-", "|", "|-"})


@dataclass(frozen=True)
class FrontmatterInfo:
    """Parsed metadata from a memory file's YAML frontmatter block."""

    valid: bool
    end_line: int
    keys: dict[str, str]
    description: str
    is_pinned: bool
    error: str | None = None


def sha256_text(text: str) -> str:
    """Returns the hexadecimal SHA-256 digest of a UTF-8 string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _find_closing_delimiter(lines: list[str], max_scan_lines: int) -> int:
    """Returns the 1-based line number of the closing '---', or 0 if not found."""
    limit = min(len(lines), max_scan_lines)
    for idx in range(1, limit):
        if lines[idx].strip() == "---":
            return idx + 1
    return 0


def _is_quoted(raw_val: str) -> bool:
    """Whether a raw YAML scalar value is wrapped in matching single or double quotes."""
    if len(raw_val) < 2:
        return False
    return (raw_val[0] == '"' and raw_val[-1] == '"') or (
        raw_val[0] == "'" and raw_val[-1] == "'"
    )


def _unquote_scalar(raw_val: str) -> tuple[str, str | None]:
    """Unquotes a single-line YAML scalar and validates unquoted colons or comments."""
    if _is_quoted(raw_val):
        inner = raw_val[1:-1]
        if raw_val[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        else:
            inner = inner.replace("''", "'")
        return inner.strip(), None
    if ": " in raw_val or " #" in raw_val or raw_val.startswith("#"):
        return (
            raw_val,
            "Unquoted frontmatter value contains ': ' or ' #'; wrap in double quotes.",
        )
    return raw_val.strip(), None


def _parse_frontmatter_body(
    block_lines: list[str],
) -> tuple[dict[str, str], str | None]:
    """Parses key-value pairs and block scalars inside a YAML frontmatter block."""
    keys: dict[str, str] = {}
    idx = 0
    while idx < len(block_lines):
        raw = block_lines[idx]
        if not raw.strip() or raw.lstrip().startswith("#"):
            idx += 1
            continue
        match = KEY_VALUE_RE.match(raw)
        if not match:
            return keys, f"Malformed frontmatter line: {raw.strip()}"
        key, raw_val = match.group(1), match.group(2).strip()
        if raw_val in BLOCK_SCALAR_MARKERS:
            parts, idx = _collect_block_scalar(block_lines, idx + 1)
            keys[key] = " ".join(parts).strip()
            continue
        val, err = _unquote_scalar(raw_val)
        if err:
            return keys, f"Key '{key}': {err}"
        keys[key] = val
        idx += 1
    return keys, None


def _collect_block_scalar(
    block_lines: list[str], start_idx: int
) -> tuple[list[str], int]:
    """Collects indented continuation lines for a YAML block scalar."""
    parts: list[str] = []
    idx = start_idx
    while idx < len(block_lines):
        line = block_lines[idx]
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.strip():
            parts.append(line.strip())
        idx += 1
    return parts, idx


def parse_frontmatter(
    lines: list[str], max_scan_lines: int = 30
) -> FrontmatterInfo:
    """Parses a memory file's YAML frontmatter block up to `max_scan_lines`.

    Preserves all frontmatter keys (`description`, `visibility`, `confidence`,
    `sourced_date`) and flags unquoted colons or missing `description` fields.
    """
    if not lines or lines[0].strip() != "---":
        return FrontmatterInfo(
            valid=False,
            end_line=0,
            keys={},
            description="",
            is_pinned=False,
            error="Missing opening '---' frontmatter delimiter on line 1.",
        )
    end_line = _find_closing_delimiter(lines, max_scan_lines)
    if end_line == 0:
        return FrontmatterInfo(
            valid=False,
            end_line=0,
            keys={},
            description="",
            is_pinned=False,
            error=f"Missing closing '---' delimiter within first {max_scan_lines} lines.",
        )
    keys, err = _parse_frontmatter_body(lines[1 : end_line - 1])
    desc = keys.get("description", "").strip()
    is_pinned = keys.get("visibility", "").strip().upper() == "PINNED"
    if err:
        return FrontmatterInfo(False, end_line, keys, desc, is_pinned, err)
    if not desc and not is_pinned:
        return FrontmatterInfo(
            False,
            end_line,
            keys,
            desc,
            is_pinned,
            "Missing or empty 'description' in frontmatter.",
        )
    return FrontmatterInfo(True, end_line, keys, desc, is_pinned, None)


def _escape_double_quoted(val: str) -> str:
    """Escapes backslashes and double quotes for a YAML double-quoted scalar."""
    collapsed = " ".join(val.splitlines()).strip()
    escaped = collapsed.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def format_frontmatter(keys: dict[str, str]) -> list[str]:
    """Serializes frontmatter keys into a `---`-delimited YAML block.

    Always double-quotes `description` on a single line while preserving all
    other keys (`visibility`, `confidence`, `sourced_date`).
    """
    out = ["---"]
    ordered_keys = ["description"] if "description" in keys else []
    ordered_keys.extend(k for k in keys if k != "description")
    for key in ordered_keys:
        val = str(keys[key])
        if key == "description" or ": " in val or " #" in val or not val:
            out.append(f"{key}: {_escape_double_quoted(val)}")
        else:
            out.append(f"{key}: {val}")
    out.append("---")
    return out


def iter_non_fenced_lines(
    lines: list[str], start_after_line: int = 0
) -> Iterator[tuple[int, str]]:
    """Yields `(1_based_line_number, line_text)` outside fenced code blocks."""
    in_fence = False
    for idx, line in enumerate(lines, start=1):
        if idx <= start_after_line:
            continue
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            yield idx, line


def find_direct_lines(lines: list[str], start_after_line: int = 0) -> list[int]:
    """Returns 1-based line numbers containing `#direct` outside code fences."""
    return [
        line_num
        for line_num, text in iter_non_fenced_lines(lines, start_after_line)
        if DIRECT_TAG_RE.search(text)
    ]


def count_claims(lines: list[str], start_after_line: int = 0) -> int:
    """Counts list-bullet claims outside fenced code blocks after frontmatter."""
    count = sum(
        1
        for _, text in iter_non_fenced_lines(lines, start_after_line)
        if CLAIM_BULLET_RE.match(text)
    )
    return max(count, 1) if len(lines) > start_after_line else 0


def _resolve_relative_target(
    raw_target: str, source_relpath: str, memory_root: Path
) -> tuple[str, bool] | None:
    """Resolves a relative Markdown link target within `memory_root`."""
    if not raw_target or raw_target.startswith(("#", "/")):
        return None
    if EXTERNAL_SCHEME_RE.match(raw_target):
        return None
    path_part = raw_target.split("#", 1)[0].split("?", 1)[0].strip()
    if not path_part or not path_part.endswith(".md"):
        return None
    source_dir = (memory_root / source_relpath).parent
    candidate = (source_dir / path_part).resolve()
    real_root = memory_root.resolve()
    if not candidate.is_relative_to(real_root):
        return None
    rel_target = candidate.relative_to(real_root).as_posix()
    return rel_target, candidate.is_file()


def extract_relative_links(
    lines: list[str],
    source_relpath: str,
    memory_root: Path,
    start_after_line: int = 0,
) -> list[dict[str, Any]]:
    """Extracts relative `.md` links outside code fences within `memory_root`."""
    links: list[dict[str, Any]] = []
    for line_num, text in iter_non_fenced_lines(lines, start_after_line):
        for match in MARKDOWN_LINK_RE.finditer(text):
            raw_target = match.group(1).strip()
            resolved = _resolve_relative_target(
                raw_target, source_relpath, memory_root
            )
            if resolved is not None:
                rel_target, exists = resolved
                links.append({
                    "line": line_num,
                    "raw_target": raw_target,
                    "target": rel_target,
                    "exists": exists,
                })
    return links


def is_within_root(
    candidate: Path, root: Path, skipped_mounts: list[Path] | None = None
) -> bool:
    """Whether `candidate` resolves inside `root` and outside `skipped_mounts`."""
    try:
        real_candidate = candidate.resolve()
        real_root = root.resolve()
    except OSError:
        return False
    if not real_candidate.is_relative_to(real_root):
        return False
    for mount in skipped_mounts or []:
        try:
            if real_candidate.is_relative_to(mount.resolve()):
                return False
        except OSError:
            continue
    return True


def is_protected_file(relpath: str, frontmatter: FrontmatterInfo) -> bool:
    """Whether a memory file is protected from whole-file retirement."""
    name = Path(relpath).name
    return (
        name in PROTECTED_FILENAMES
        or relpath in PROTECTED_FILENAMES
        or frontmatter.is_pinned
    )
