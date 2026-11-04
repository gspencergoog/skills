"""In-memory AST code symbol and Markdown section parsers for packrat.

Provides self-contained, zero-external-dependency parsing of raw file blobs
retrieved from git commits, removing any dependency on external skills.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Markdown Heading, Section, and Breadcrumb Parsing
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^ {0,3}(?:>[ \t]*)*(`{3,}|~{3,})[ \t]*([^`~\s]*).*$")
_BQ_PREFIX_RE = re.compile(r"^ {0,3}(?:>[ \t]?)+")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(={3,}|-{3,})[ \t]*$")
_TABLE_SEP_RE = re.compile(
    r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$"
)
_REF_DEF_RE = re.compile(r"^ {0,3}\[([^\]]+)\]:[ \t]*<?([^\s>]+)>?")
_INLINE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")
_SLUG_SPACE_RE = re.compile(r"[\s]+")


@dataclass
class LineRecord:
    line_num: int
    raw: str
    in_frontmatter: bool = False
    is_fence_delim: bool = False
    in_code_block: bool = False
    code_block_id: int | None = None
    fence_lang: str = ""
    heading_level: int | None = None
    heading_text: str | None = None
    is_setext_underline: bool = False
    section_id: int = 0


def _strip_inline_formatting(text: str) -> str:
    """Strips inline links and code backticks for heading slug generation."""
    cleaned = _INLINE_LINK_RE.sub(r"\1", text)
    return cleaned.replace("`", "").replace("*", "").replace("_", "")


def _slugify_heading(heading: str, seen_counts: dict[str, int]) -> str:
    """Computes a GitHub/g3doc-compatible heading anchor slug with deduplication."""
    base = _strip_inline_formatting(heading).strip().lower()
    base = _SLUG_STRIP_RE.sub("", base)
    base = _SLUG_SPACE_RE.sub("-", base).strip("-") or "section"
    count = seen_counts.get(base, 0)
    seen_counts[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def _mark_frontmatter(records: list[LineRecord]) -> list[str]:
    """Marks frontmatter lines at the top of the file and returns inner lines."""
    if not records or records[0].raw != "---":
        return []
    end_idx = None
    for idx in range(1, len(records)):
        if records[idx].raw in ("---", "..."):
            end_idx = idx
            break
    if end_idx is None:
        return []
    for idx in range(end_idx + 1):
        records[idx].in_frontmatter = True
    return [records[idx].raw for idx in range(1, end_idx)]


def _step_fence_record(
    rec: LineRecord, active_char: str, active_len: int, block_counter: int
) -> tuple[str, int, int]:
    """Updates fence state for one non-frontmatter line."""
    match = _FENCE_RE.match(rec.raw)
    if not match:
        if active_char:
            rec.in_code_block = True
            rec.code_block_id = block_counter
        return active_char, active_len, block_counter

    marker, lang = match.group(1), match.group(2)
    char, length = marker[0], len(marker)
    if not active_char:
        block_counter += 1
        rec.is_fence_delim = True
        rec.in_code_block = True
        rec.code_block_id = block_counter
        rec.fence_lang = lang
        return char, length, block_counter

    rec.in_code_block = True
    rec.code_block_id = block_counter
    if char == active_char and length >= active_len and not lang:
        rec.is_fence_delim = True
        return "", 0, block_counter
    return active_char, active_len, block_counter


def _mark_fences(records: list[LineRecord]) -> None:
    """Marks fenced code blocks, supporting nested and blockquote-indented fences."""
    active_char = ""
    active_len = 0
    block_counter = 0
    for rec in records:
        if not rec.in_frontmatter:
            active_char, active_len, block_counter = _step_fence_record(
                rec, active_char, active_len, block_counter
            )


def _can_be_setext_title(rec: LineRecord) -> bool:
    """Returns True if a line can serve as the title above a Setext underline."""
    if rec.in_frontmatter or rec.in_code_block or rec.heading_level is not None:
        return False
    text = rec.raw.strip()
    if not text or _TABLE_SEP_RE.match(rec.raw) or _REF_DEF_RE.match(rec.raw):
        return False
    return not text.startswith(("-", "*", "+", ">", "|", "#"))


def _detect_heading_on_line(records: list[LineRecord], idx: int) -> None:
    """Marks ATX or Setext heading metadata on records[idx]."""
    rec = records[idx]
    if rec.in_frontmatter or rec.in_code_block:
        return
    atx = _ATX_RE.match(rec.raw)
    if atx:
        rec.heading_level = len(atx.group(1))
        rec.heading_text = atx.group(2).strip()
        return
    setext = _SETEXT_RE.match(rec.raw)
    if setext and idx > 0 and _can_be_setext_title(records[idx - 1]):
        prev = records[idx - 1]
        prev.heading_level = 1 if setext.group(1)[0] == "=" else 2
        prev.heading_text = prev.raw.strip()
        rec.is_setext_underline = True


def _mark_headings_and_sections(records: list[LineRecord]) -> None:
    """Detects ATX and Setext headings and assigns sequential section_id values."""
    for idx in range(len(records)):
        _detect_heading_on_line(records, idx)

    sec_id = 0
    for rec in records:
        if not rec.in_frontmatter:
            if rec.heading_level is not None:
                sec_id += 1
            rec.section_id = sec_id


def _compute_subtree_end(
    sections: list[dict[str, Any]], idx: int, max_line: int
) -> int:
    """Finds the last line of a section's subtree before the next <= level heading."""
    cur_level = sections[idx]["level"]
    if cur_level == 0:
        return sections[idx]["line_end"]
    for nxt in sections[idx + 1 :]:
        if 1 <= nxt["level"] <= cur_level:
            return nxt["line_start"] - 1
    return max_line


def _update_breadcrumb(
    active_headings: dict[int, str], level: int, heading: str
) -> str:
    """Updates active heading levels and returns the hierarchical breadcrumb."""
    if level <= 0:
        return "(preamble)"
    active_headings[level] = heading
    stale = [k for k in active_headings if k > level]
    for k in stale:
        del active_headings[k]
    ordered = [active_headings[k] for k in sorted(active_headings)]
    return " > ".join(ordered)


def _make_section_dict(
    file_path: str,
    sec_id: int,
    group: list[LineRecord],
    active_headings: dict[int, str],
    seen_slugs: dict[str, int],
) -> dict[str, Any]:
    """Builds a single section dictionary from its grouped LineRecords."""
    first = group[0]
    level = first.heading_level or 0
    heading = first.heading_text or "(preamble)"
    breadcrumb = _update_breadcrumb(active_headings, level, heading)
    slug = _slugify_heading(heading, seen_slugs) if level > 0 else ""
    line_start, line_end = group[0].line_num, group[-1].line_num
    block_ids = {r.code_block_id for r in group if r.code_block_id is not None}
    raw_lines = [r.raw for r in group]
    return {
        "file_path": file_path,
        "section_id": sec_id,
        "level": level,
        "heading": heading,
        "breadcrumb": breadcrumb,
        "anchor_slug": slug,
        "chunk_name": None,
        "line_start": line_start,
        "line_end": line_end,
        "subtree_line_end": line_end,
        "own_lines": line_end - line_start + 1,
        "subtree_lines": line_end - line_start + 1,
        "own_code_blocks": len(block_ids),
        "content": "\n".join(raw_lines),
    }


def _build_sections(file_path: str, records: list[LineRecord]) -> list[dict[str, Any]]:
    """Aggregates LineRecords into section dicts with breadcrumbs and slugs."""
    grouped: dict[int, list[LineRecord]] = {}
    max_line = 0
    for rec in records:
        if not rec.in_frontmatter:
            grouped.setdefault(rec.section_id, []).append(rec)
            max_line = rec.line_num
    if not grouped:
        return []

    seen_slugs: dict[str, int] = {}
    active_headings: dict[int, str] = {}
    sections: list[dict[str, Any]] = []
    for sec_id in sorted(grouped):
        sections.append(
            _make_section_dict(
                file_path, sec_id, grouped[sec_id], active_headings, seen_slugs
            )
        )

    for idx, sec in enumerate(sections):
        sub_end = _compute_subtree_end(sections, idx, max_line)
        sec["subtree_line_end"] = sub_end
        sec["subtree_lines"] = sub_end - sec["line_start"] + 1
    return sections


def parse_markdown_sections(
    file_path: str, raw_lines: list[str]
) -> list[dict[str, Any]]:
    """Parses raw lines of Markdown into structured heading sections and breadcrumbs."""
    records = [LineRecord(line_num=i + 1, raw=ln) for i, ln in enumerate(raw_lines)]
    _mark_frontmatter(records)
    _mark_fences(records)
    _mark_headings_and_sections(records)
    return _build_sections(file_path, records)


# ---------------------------------------------------------------------------
# AST & Regex Code Symbol Extraction
# ---------------------------------------------------------------------------

CLASS_DECL_RE = re.compile(
    r"^\s*(?:export\s+|public\s+|abstract\s+|final\s+|sealed\s+)*"
    r"(?:class|struct|interface|protocol|enum|extension\s+type)\s+([A-Za-z0-9_]+)"
)
FUNC_DECL_RE = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|static\s+|async\s+|override\s+)*"
    r"(?:fun|func|function|fn|def)\s+([A-Za-z0-9_]+)\s*\(([^)]*)\)"
)
METHOD_DECL_RE = re.compile(
    r"^\s*(?:public\s+|private\s+|protected\s+|static\s+|async\s+|override\s+|"
    r"Future<[^>]+>\s+|Stream<[^>]+>\s+|[A-Za-z0-9_<>?\[\]]+\s+)+"
    r"([A-Za-z0-9_]+)\s*\(([^)]*)\)\s*(?::\s*[A-Za-z0-9_<>?|\[\]\s]+)?\s*(?:async\s*)?\{"
)
CONTROL_KEYWORDS = {"if", "for", "while", "switch", "catch", "when", "return"}


def _format_py_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Formats a Python function signature from its AST node."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_str = ast.unparse(node.args)
    ret_str = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    return f"{prefix} {node.name}({args_str}){ret_str}"


def _collect_py_method_ids(tree: ast.AST) -> set[int]:
    """Returns AST node object IDs for functions defined directly inside a class."""
    class_nodes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    return {
        id(child)
        for cls_node in class_nodes
        for child in cls_node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _py_node_to_entity(
    node: ast.AST, file_path: str, side: str, method_ids: set[int]
) -> dict[str, Any] | None:
    """Converts a Python ClassDef or FunctionDef AST node into an entity record."""
    if isinstance(node, ast.ClassDef):
        return {
            "file_path": file_path,
            "side": side,
            "kind": "class",
            "name": node.name,
            "signature": f"class {node.name}",
            "line_start": node.lineno,
            "line_end": node.end_lineno or node.lineno,
        }
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {
            "file_path": file_path,
            "side": side,
            "kind": "method" if id(node) in method_ids else "function",
            "name": node.name,
            "signature": _format_py_signature(node),
            "line_start": node.lineno,
            "line_end": node.end_lineno or node.lineno,
        }
    return None


def _extract_py_entities(
    file_path: str, side: str, content: str
) -> list[dict[str, Any]]:
    """Extracts Python class, method, and function spans using stdlib ast."""
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return []

    method_ids = _collect_py_method_ids(tree)
    entities = [
        ent
        for node in ast.walk(tree)
        if (ent := _py_node_to_entity(node, file_path, side, method_ids)) is not None
    ]
    entities.sort(key=lambda e: (e["line_start"], e["line_end"]))
    return entities


def _find_brace_end_line(lines: list[str], start_idx: int) -> int:
    """Finds the 1-indexed closing brace line for a declaration starting at start_idx."""
    depth = 0
    opened = False
    for idx in range(start_idx, len(lines)):
        opens = lines[idx].count("{")
        closes = lines[idx].count("}")
        if opens > 0:
            opened = True
        depth += opens - closes
        if opened and depth <= 0:
            return idx + 1
        if not opened and lines[idx].rstrip().endswith(";"):
            return idx + 1
    return start_idx + 1


def _match_brace_decl(line: str) -> tuple[str, str] | None:
    """Matches a single line against class, function, or method declaration regexes."""
    m_cls = CLASS_DECL_RE.match(line)
    if m_cls:
        return "class", m_cls.group(1)
    m_fn = FUNC_DECL_RE.match(line)
    if m_fn:
        return "function", m_fn.group(1)
    m_meth = METHOD_DECL_RE.match(line)
    if m_meth and m_meth.group(1) not in CONTROL_KEYWORDS:
        return "method", m_meth.group(1)
    return None


def _extract_brace_entities(
    file_path: str, side: str, content: str
) -> list[dict[str, Any]]:
    """Extracts symbol spans for brace-delimited languages."""
    lines = content.splitlines()
    entities: list[dict[str, Any]] = []
    for idx, line in enumerate(lines):
        matched = _match_brace_decl(line)
        if not matched:
            continue
        kind, name = matched
        sig = line.strip().rstrip("{").strip()
        entities.append(
            {
                "file_path": file_path,
                "side": side,
                "kind": kind,
                "name": name,
                "signature": sig,
                "line_start": idx + 1,
                "line_end": _find_brace_end_line(lines, idx),
            }
        )
    return entities


def extract_content_entities(
    rel_path: str,
    content_or_side: str,
    content: str | None = None,
    full_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Dispatches symbol extraction for a single file snapshot."""
    del full_path
    if content is None:
        actual_content = content_or_side
        side = "NEW"
    else:
        actual_content = content
        side = content_or_side
    if rel_path.endswith(".py"):
        return _extract_py_entities(rel_path, side, actual_content)
    return _extract_brace_entities(rel_path, side, actual_content)


# Alias for backward compatibility if callers reference the private helper name
_extract_content_entities = extract_content_entities
