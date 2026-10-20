#!/usr/bin/env python3
"""Stages Markdown files into relational JSONL tables and optional section chunks.

Layer 2 companion to skills/duckdb/scripts/markdown_macros.sql for code-review
Document Mode. Parses ATX and Setext headings, blockquote and nested code fences,
multi-line YAML frontmatter, GFM pipe tables, and inline/reference links.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_FENCE_RE = re.compile(r"^ {0,3}(?:>[ \t]*)*(`{3,}|~{3,})[ \t]*([^`~\s]*).*$")
_BQ_PREFIX_RE = re.compile(r"^ {0,3}(?:>[ \t]?)+")
_ATX_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*$")
_SETEXT_RE = re.compile(r"^ {0,3}(={3,}|-{3,})[ \t]*$")
_TABLE_SEP_RE = re.compile(
    r"^ {0,3}\|?[ \t]*:?-+:?[ \t]*(?:\|[ \t]*:?-+:?[ \t]*)+\|?[ \t]*$"
)
_REF_DEF_RE = re.compile(r"^ {0,3}\[([^\]]+)\]:[ \t]*<?([^\s>]+)>?")
_INLINE_CODE_RE = re.compile(r"(`+)(.+?)\1")
_INLINE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]+)\)")
_REF_LINK_RE = re.compile(r"\[([^\]]+)\]\[([^\]]*)\]")
_TAG_RE = re.compile(r"(?:^|(?<=\s))#[a-zA-Z_][\w:.-]*")
_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")
_SLUG_SPACE_RE = re.compile(r"[\s]+")

_PIPE_PLACEHOLDER = "\x00PIPE\x00"


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


@dataclass
class ParsedDoc:
    file_path: str
    lines: list[LineRecord] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)
    sections: list[dict[str, Any]] = field(default_factory=list)
    code_blocks: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)


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


def _parse_yaml_scalar(val: str) -> Any:
    """Parses a single-line YAML scalar or inline list value."""
    if not val:
        return ""
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
        return val[1:-1]
    if val.startswith("[") and val.endswith("]"):
        parts = val[1:-1].split(",")
        return [p.strip().strip("\"'") for p in parts if p.strip()]
    low = val.lower()
    if low in ("true", "false"):
        return low == "true"
    return val


def _finalize_fm_block(mode: str, buf: list[str]) -> Any:
    """Finalizes a buffered multi-line YAML frontmatter block."""
    if mode == "list":
        return list(buf)
    if mode == "|":
        return "\n".join(buf).strip()
    return " ".join(buf).strip()


def _append_fm_continuation(stripped: str, mode: str, buf: list[str]) -> str:
    """Appends an indented continuation line to the active frontmatter buffer."""
    if stripped.startswith("- "):
        buf.append(stripped[2:].strip().strip("\"'"))
        return "list"
    buf.append(stripped)
    return mode


def _assign_fm_key_value(raw: str, attrs: dict[str, Any]) -> tuple[str | None, str]:
    """Parses a top-level YAML key:value line and returns (pending_key, mode)."""
    key, sep, rest = raw.partition(":")
    if not sep:
        return None, ""
    key, val = key.strip(), rest.strip()
    if val in (">-", ">", "|", ""):
        return key, ("list" if val == "" else val)
    attrs[key] = _parse_yaml_scalar(val)
    return None, ""


def _parse_frontmatter_lines(fm_lines: list[str]) -> dict[str, Any]:
    """Parses simple YAML frontmatter including block scalars and lists."""
    attrs: dict[str, Any] = {}
    cur_key: str | None = None
    cur_mode = ""
    buf: list[str] = []

    for raw in fm_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw.startswith((" ", "\t")) and cur_key is not None:
            cur_mode = _append_fm_continuation(stripped, cur_mode, buf)
            continue
        if cur_key is not None:
            attrs[cur_key] = _finalize_fm_block(cur_mode, buf)
            buf = []
        cur_key, cur_mode = _assign_fm_key_value(raw, attrs)

    if cur_key is not None:
        attrs[cur_key] = _finalize_fm_block(cur_mode, buf)
    return attrs


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


def _build_code_blocks(
    file_path: str, records: list[LineRecord], sec_headings: dict[int, str]
) -> list[dict[str, Any]]:
    """Extracts fenced code blocks with clean inner code lines."""
    grouped: dict[int, list[LineRecord]] = {}
    for rec in records:
        if rec.in_code_block and rec.code_block_id is not None:
            grouped.setdefault(rec.code_block_id, []).append(rec)

    blocks: list[dict[str, Any]] = []
    for block_id, group in sorted(grouped.items()):
        first = group[0]
        body_lines = [
            _BQ_PREFIX_RE.sub("", r.raw) for r in group if not r.is_fence_delim
        ]
        blocks.append(
            {
                "file_path": file_path,
                "block_id": block_id,
                "section_id": first.section_id,
                "section_heading": sec_headings.get(first.section_id, "(preamble)"),
                "lang": first.fence_lang,
                "line_start": group[0].line_num,
                "line_end": group[-1].line_num,
                "code_lines": len(body_lines),
                "code": "\n".join(body_lines),
            }
        )
    return blocks


def _split_table_row(line: str) -> list[str]:
    """Splits a Markdown pipe table row while preserving pipes in inline code."""
    masked = _INLINE_CODE_RE.sub(
        lambda m: m.group(0).replace("|", _PIPE_PLACEHOLDER), line
    )
    masked = masked.replace(r"\|", _PIPE_PLACEHOLDER)
    stripped = masked.strip()
    stripped = stripped.removeprefix("|")
    stripped = stripped.removesuffix("|")
    return [
        cell.strip().replace(_PIPE_PLACEHOLDER, "|") for cell in stripped.split("|")
    ]


def _is_table_header_pair(prose: list[LineRecord], idx: int) -> bool:
    """Returns True if prose[idx] and prose[idx + 1] form a pipe table header."""
    if idx + 1 >= len(prose):
        return False
    cur, nxt = prose[idx], prose[idx + 1]
    if "|" not in cur.raw or nxt.line_num != cur.line_num + 1:
        return False
    return bool(_TABLE_SEP_RE.match(nxt.raw))


def _is_table_data_row(rec: LineRecord) -> bool:
    """Returns True if a prose LineRecord is a pipe table data row."""
    if "|" not in rec.raw or rec.heading_level is not None:
        return False
    return not _TABLE_SEP_RE.match(rec.raw)


def _zip_table_cells(headers: list[str], vals: list[str]) -> dict[str, str]:
    """Zips table header names with row values, padding missing trailing cells."""
    cells: dict[str, str] = {}
    for col, name in enumerate(headers):
        cells[name] = vals[col] if col < len(vals) else ""
    return cells


def _extract_tables(
    file_path: str, records: list[LineRecord], sec_headings: dict[int, str]
) -> list[dict[str, Any]]:
    """Extracts GFM pipe tables into keyed JSON row objects."""
    prose = [r for r in records if not r.in_frontmatter and not r.in_code_block]
    rows: list[dict[str, Any]] = []
    table_id = 0
    idx = 0

    while idx < len(prose):
        if not _is_table_header_pair(prose, idx):
            idx += 1
            continue
        table_id += 1
        headers = _split_table_row(prose[idx].raw)
        idx += 2
        row_index = 0
        while idx < len(prose) and _is_table_data_row(prose[idx]):
            row_index += 1
            rec = prose[idx]
            vals = _split_table_row(rec.raw)
            rows.append(
                {
                    "file_path": file_path,
                    "table_id": table_id,
                    "section_id": rec.section_id,
                    "section_heading": sec_headings.get(rec.section_id, "(preamble)"),
                    "row_index": row_index,
                    "line_num": rec.line_num,
                    "headers": headers,
                    "cells": _zip_table_cells(headers, vals),
                }
            )
            idx += 1
    return rows


def _resolve_link_target(
    root: Path, file_path: str, raw_target: str
) -> tuple[str, str, bool, bool]:
    """Resolves a link target into (target_path, anchor, is_external, target_exists)."""
    target = raw_target.strip().split()[0].strip("<>")
    if target.startswith(("http://", "https://", "mailto:", "conversation://")):
        return target, "", True, True

    path_part, _, anchor = target.partition("#")
    if not path_part:
        resolved_rel = file_path
    else:
        parent = (root / file_path).parent
        candidate = (parent / path_part).resolve()
        try:
            resolved_rel = str(candidate.relative_to(root.resolve()))
        except ValueError:
            resolved_rel = str(candidate)

    check_path = (
        root / resolved_rel if not os.path.isabs(resolved_rel) else Path(resolved_rel)
    )
    return resolved_rel, anchor, False, check_path.exists()


def _collect_ref_defs(prose: list[LineRecord]) -> dict[str, str]:
    """Collects reference-style link definitions across prose lines."""
    ref_defs: dict[str, str] = {}
    for rec in prose:
        m = _REF_DEF_RE.match(rec.raw)
        if m:
            ref_defs[m.group(1).strip().lower()] = m.group(2).strip()
    return ref_defs


def _find_links_on_line(raw: str, ref_defs: dict[str, str]) -> list[tuple[str, str]]:
    """Extracts (label, raw_target) pairs from a single prose line."""
    if _REF_DEF_RE.match(raw):
        return []
    masked = _INLINE_CODE_RE.sub("", raw)
    found = [(m.group(1), m.group(2)) for m in _INLINE_LINK_RE.finditer(masked)]
    for m in _REF_LINK_RE.finditer(masked):
        label = m.group(1)
        ref_key = (m.group(2) or label).strip().lower()
        if ref_key in ref_defs:
            found.append((label, ref_defs[ref_key]))
    return found


def _extract_links(
    root: Path,
    file_path: str,
    records: list[LineRecord],
    sec_headings: dict[int, str],
) -> list[dict[str, Any]]:
    """Extracts inline and reference-style links outside code blocks."""
    prose = [r for r in records if not r.in_frontmatter and not r.in_code_block]
    ref_defs = _collect_ref_defs(prose)
    links: list[dict[str, Any]] = []

    for rec in prose:
        for label, raw_target in _find_links_on_line(rec.raw, ref_defs):
            t_path, anchor, is_ext, exists = _resolve_link_target(
                root, file_path, raw_target
            )
            links.append(
                {
                    "file_path": file_path,
                    "line_num": rec.line_num,
                    "section_id": rec.section_id,
                    "section_heading": sec_headings.get(rec.section_id, "(preamble)"),
                    "label": label,
                    "raw_target": raw_target,
                    "target_path": t_path,
                    "anchor": anchor,
                    "is_external": is_ext,
                    "target_exists": exists,
                }
            )
    return links


def _extract_tags(records: list[LineRecord]) -> list[str]:
    """Extracts inline #tags outside frontmatter, headings, and code blocks."""
    tags: set[str] = set()
    for rec in records:
        if rec.in_frontmatter or rec.in_code_block or rec.heading_level is not None:
            continue
        masked = _INLINE_CODE_RE.sub("", rec.raw)
        for m in _TAG_RE.finditer(masked):
            tags.add(m.group(0).rstrip(".:"))
    return sorted(tags)


def parse_markdown_file(root: Path, file_path: str) -> ParsedDoc:
    """Parses a single Markdown file into structured tables."""
    full_path = root / file_path if not os.path.isabs(file_path) else Path(file_path)
    raw_text = full_path.read_text(encoding="utf-8", errors="replace")
    records = [
        LineRecord(line_num=i, raw=line)
        for i, line in enumerate(raw_text.splitlines(), start=1)
    ]

    fm_lines = _mark_frontmatter(records)
    _mark_fences(records)
    _mark_headings_and_sections(records)

    sections = _build_sections(file_path, records)
    sec_headings = {s["section_id"]: s["heading"] for s in sections}
    code_blocks = _build_code_blocks(file_path, records, sec_headings)
    tables = _extract_tables(file_path, records, sec_headings)
    links = _extract_links(root, file_path, records, sec_headings)

    fm_end = max((r.line_num for r in records if r.in_frontmatter), default=0)
    frontmatter = {
        "file_path": file_path,
        "has_frontmatter": fm_end > 0,
        "line_end": fm_end,
        "attributes": _parse_frontmatter_lines(fm_lines),
        "tags": _extract_tags(records),
    }

    return ParsedDoc(
        file_path=file_path,
        lines=records,
        frontmatter=frontmatter,
        sections=sections,
        code_blocks=code_blocks,
        tables=tables,
        links=links,
    )


def _emit_single_chunk(
    output_dir: Path, chunk_idx: int, cur_secs: list[dict[str, Any]], cur_lines: int
) -> dict[str, Any]:
    """Writes one md_chunk_NN.md file and returns its manifest entry."""
    chunk_name = f"md_chunk_{chunk_idx:02d}.md"
    outline = ["<!-- Document Section Outline -->"]
    files_set: set[str] = set()
    sec_ids: list[int] = []
    bodies: list[str] = []

    for sec in cur_secs:
        sec["chunk_name"] = chunk_name
        files_set.add(sec["file_path"])
        sec_ids.append(sec["section_id"])
        bodies.append(sec["content"])
        outline.append(
            f"<!-- {sec['file_path']}:{sec['line_start']}-{sec['line_end']} [{sec['breadcrumb']}] -->"
        )

    outline.append("")
    body = "\n".join(outline) + "\n".join(bodies) + "\n"
    (output_dir / chunk_name).write_text(body, encoding="utf-8")
    return {
        "chunk_name": chunk_name,
        "files": sorted(files_set),
        "section_ids": sec_ids,
        "lines": cur_lines,
        "bytes": len(body.encode("utf-8")),
    }


def _write_chunks_for_docs(
    docs: list[ParsedDoc],
    output_dir: Path,
    max_lines: int,
    max_bytes: int,
) -> list[dict[str, Any]]:
    """Packs document sections into size-bounded md_chunk_NN.md files."""
    all_secs = [sec for doc in docs for sec in doc.sections]
    manifest_chunks: list[dict[str, Any]] = []
    cur_secs: list[dict[str, Any]] = []
    cur_lines = 0
    cur_bytes = 0

    for sec in all_secs:
        sec_bytes = len(sec["content"].encode("utf-8"))
        sec_lines = sec["own_lines"]
        exceeds = cur_lines + sec_lines > max_lines or cur_bytes + sec_bytes > max_bytes
        if cur_secs and exceeds:
            manifest_chunks.append(
                _emit_single_chunk(
                    output_dir, len(manifest_chunks) + 1, cur_secs, cur_lines
                )
            )
            cur_secs, cur_lines, cur_bytes = [], 0, 0
        cur_secs.append(sec)
        cur_lines += sec_lines
        cur_bytes += sec_bytes

    if cur_secs:
        manifest_chunks.append(
            _emit_single_chunk(
                output_dir, len(manifest_chunks) + 1, cur_secs, cur_lines
            )
        )
    return manifest_chunks


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Writes a list of dicts to a newline-delimited JSON file."""
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def _to_repo_rel(root: Path, path_str: str) -> str:
    """Formats a file path relative to root when inside root."""
    p = Path(path_str)
    try:
        return str(p.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(p)


def _expand_single_pattern(root: Path, item: str) -> list[str]:
    """Expands one path or glob pattern into matching file paths."""
    pattern = item if os.path.isabs(item) else str(root / item)
    matches = sorted(glob.glob(pattern, recursive=True))
    if not matches and Path(pattern).is_file():
        matches = [pattern]
    return [_to_repo_rel(root, m) for m in matches if Path(m).is_file()]


def _expand_inputs(root: Path, inputs: list[str]) -> list[str]:
    """Expands file paths or glob patterns into sorted repo-relative paths."""
    if inputs == ["-"]:
        raw_items = [line.strip() for line in sys.stdin if line.strip()]
    else:
        raw_items = inputs
    resolved: list[str] = []
    for item in raw_items:
        resolved.extend(_expand_single_pattern(root, item))
    return sorted(dict.fromkeys(resolved))


def _write_all_tables(out_dir: Path, docs: list[ParsedDoc]) -> None:
    """Writes the five staged Markdown JSONL tables to out_dir."""
    sections: list[dict[str, Any]] = []
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    frontmatter: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    for doc in docs:
        sections.extend(doc.sections)
        blocks.extend(doc.code_blocks)
        tables.extend(doc.tables)
        frontmatter.append(doc.frontmatter)
        links.extend(doc.links)
    _write_jsonl(out_dir / "md_sections.jsonl", sections)
    _write_jsonl(out_dir / "md_code_blocks.jsonl", blocks)
    _write_jsonl(out_dir / "md_tables.jsonl", tables)
    _write_jsonl(out_dir / "md_frontmatter.jsonl", frontmatter)
    _write_jsonl(out_dir / "md_links.jsonl", links)


def main(argv: list[str] | None = None) -> int:
    """Runs the CLI. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Stage Markdown files into JSONL tables for DuckDB querying."
    )
    parser.add_argument("--root", default=".", help="Repository root directory.")
    parser.add_argument(
        "--output-dir", required=True, help="Directory for staged JSONL outputs."
    )
    parser.add_argument(
        "--split-chunks",
        action="store_true",
        help="Pack sections into size-bounded md_chunk_NN.md files.",
    )
    parser.add_argument(
        "--max-lines", type=int, default=700, help="Max lines per chunk file."
    )
    parser.add_argument(
        "--max-bytes", type=int, default=35000, help="Max bytes per chunk file."
    )
    parser.add_argument(
        "paths", nargs="+", help="Markdown file paths, globs, or - for stdin."
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rel_paths = _expand_inputs(root, args.paths)
    docs = [parse_markdown_file(root, p) for p in rel_paths]

    if args.split_chunks:
        chunks = _write_chunks_for_docs(docs, out_dir, args.max_lines, args.max_bytes)
        manifest = {"files": rel_paths, "chunks": chunks}
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )

    _write_all_tables(out_dir, docs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
