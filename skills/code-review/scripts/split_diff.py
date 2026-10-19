#!/usr/bin/env python3
"""Splits unified diffs into review chunks and structured JSONL tables."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

DIFF_HEADER_RE = re.compile(
    r'^diff --git (?:"a/((?:[^"\\]|\\.)+)"|a/(\S+)) (?:"b/((?:[^"\\]|\\.)+)"|b/(\S+))$'
)
FALLBACK_GIT_RE = re.compile(
    r'^diff --git (?:a/([^\s"]+)|"a/((?:[^"\\]|\\.)+)")',
    re.MULTILINE,
)
PLUS_HEADER_RE = re.compile(
    r'^\+\+\+ (?:"b/((?:[^"\\]|\\.)+)"|b/([^\t\n]+))', re.MULTILINE
)
MINUS_HEADER_RE = re.compile(
    r'^--- (?:"a/((?:[^"\\]|\\.)+)"|a/([^\t\n]+))', re.MULTILINE
)
HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)
OCTAL_ESCAPE_RE = re.compile(r"(?:\\[0-3][0-7]{2})+")
FILE_META_PREFIXES = (
    "diff --git ",
    "index ",
    "--- ",
    "+++ ",
    "new file ",
    "deleted file ",
    "similarity index ",
    "rename from ",
    "rename to ",
    "old mode ",
    "new mode ",
    "Binary files ",
)

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

SegmentTuple = tuple[str, list[dict[str, Any]], list[dict[str, Any]]]


def _unescape_git_path(raw_path: str) -> str:
    """Unescapes C and octal-escaped paths emitted by git diff."""

    def _decode_octal_run(match: re.Match[str]) -> str:
        octals = match.group(0).split("\\")[1:]
        raw_bytes = bytes(int(o, 8) for o in octals)
        return raw_bytes.decode("utf-8", errors="replace")

    unescaped = OCTAL_ESCAPE_RE.sub(_decode_octal_run, raw_path)
    return (
        unescaped.replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\\t", "\t")
        .replace("\\n", "\n")
    )


def extract_diff_from_json(json_data: Any, key: str | None = None) -> str:
    """Extracts a unified diff string from parsed JSON data."""
    if key:
        return json_data.get(key, "")
    if isinstance(json_data, str):
        return json_data
    for k in ["diff", "patch", "content"]:
        if k in json_data:
            return json_data[k]
    raise ValueError("Could not find diff in JSON data")


def _split_into_file_diffs(diff_content: str) -> list[str]:
    """Splits a unified diff string into per-file diff blocks."""
    files = re.split(r"^(?=diff --git )", diff_content, flags=re.MULTILINE)
    if len(files) <= 1:
        files = re.split(r"^(?=--- )", diff_content, flags=re.MULTILINE)
    return [f for f in files if f.strip()]


def _match_header_path(pattern: re.Pattern[str], text: str) -> str | None:
    """Matches a two-group quoted/unquoted header regex and unescapes the path."""
    match = pattern.search(text)
    if not match:
        return None
    raw = (match.group(1) or match.group(2) or "").strip()
    if not raw or raw == "/dev/null":
        return None
    return _unescape_git_path(raw)


def _extract_git_header_path(file_diff: str) -> str | None:
    """Extracts a file path from a diff --git header line."""
    first_line = file_diff.splitlines()[0] if file_diff else ""
    git_match = DIFF_HEADER_RE.match(first_line)
    if git_match:
        chosen = (
            git_match.group(3)
            or git_match.group(4)
            or git_match.group(1)
            or git_match.group(2)
        )
        if chosen:
            return _unescape_git_path(chosen.strip())
    return _match_header_path(FALLBACK_GIT_RE, file_diff)


def _extract_file_name(file_diff: str, index: int) -> str:
    """Extracts and unescapes the target file path from a file diff block."""
    return (
        _match_header_path(PLUS_HEADER_RE, file_diff)
        or _extract_git_header_path(file_diff)
        or _match_header_path(MINUS_HEADER_RE, file_diff)
        or f"chunk_{index}.diff"
    )


def _byte_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _fits_limits(text: str, max_bytes: int, max_lines: int) -> bool:
    return _byte_len(text) <= max_bytes and _line_count(text) <= max_lines


def _parse_hunk_header_meta(
    header_line: str, file_path: str, hunk_id: int
) -> dict[str, Any]:
    """Parses an @@ hunk header line into a hunk record dictionary."""
    match = HUNK_HEADER_RE.match(header_line.rstrip("\r\n"))
    if not match:
        return {
            "file_path": file_path,
            "chunk_name": "",
            "hunk_id": hunk_id,
            "old_start": 1,
            "old_count": 0,
            "new_start": 1,
            "new_count": 0,
            "func_context": "",
            "added_lines": 0,
            "deleted_lines": 0,
        }
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    func_context = match.group(5).strip()
    return {
        "file_path": file_path,
        "chunk_name": "",
        "hunk_id": hunk_id,
        "old_start": old_start,
        "old_count": old_count,
        "new_start": new_start,
        "new_count": new_count,
        "func_context": func_context,
        "added_lines": 0,
        "deleted_lines": 0,
    }


def _build_diff_line_record(
    raw_line: str,
    hunk_meta: dict[str, Any],
    cur_old: int,
    cur_new: int,
) -> tuple[dict[str, Any] | None, int, int]:
    """Creates a line record for a single diff body line and advances line counters."""
    if not raw_line or raw_line.startswith(FILE_META_PREFIXES):
        return None, cur_old, cur_new
    op = raw_line[0]
    if op not in ("+", "-", " "):
        return None, cur_old, cur_new

    content = raw_line[1:].rstrip("\r\n")
    old_line = cur_old if op in ("-", " ") else None
    new_line = cur_new if op in ("+", " ") else None
    if op == "+":
        hunk_meta["added_lines"] += 1
        cur_new += 1
    elif op == "-":
        hunk_meta["deleted_lines"] += 1
        cur_old += 1
    else:
        cur_old += 1
        cur_new += 1

    record = {
        "file_path": hunk_meta["file_path"],
        "chunk_name": "",
        "hunk_id": hunk_meta["hunk_id"],
        "func_context": hunk_meta["func_context"],
        "op": op,
        "old_line": old_line,
        "new_line": new_line,
        "content": content,
    }
    return record, cur_old, cur_new


def _annotate_hunk_lines(
    hunk_text: str, file_path: str, hunk_id: int
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]]]:
    """Annotates each raw line of a hunk with its pre-computed line record."""
    lines = hunk_text.splitlines(keepends=True)
    if not lines:
        meta = _parse_hunk_header_meta("", file_path, hunk_id)
        return meta, []

    first_is_hunk = lines[0].startswith("@@ ")
    meta = _parse_hunk_header_meta(
        lines[0] if first_is_hunk else "", file_path, hunk_id
    )
    cur_old = meta["old_start"]
    cur_new = meta["new_start"]
    annotated: list[tuple[str, dict[str, Any] | None]] = []

    for idx, line in enumerate(lines):
        if idx == 0 and first_is_hunk:
            annotated.append((line, None))
            continue
        rec, cur_old, cur_new = _build_diff_line_record(
            line, meta, cur_old, cur_new
        )
        annotated.append((line, rec))

    if not first_is_hunk:
        meta["old_count"] = cur_old - meta["old_start"]
        meta["new_count"] = cur_new - meta["new_start"]
    return meta, annotated


def parse_diff_records(
    diff_content: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parses a unified diff into (hunk_records, line_records) with 1-indexed line numbers."""
    hunk_records: list[dict[str, Any]] = []
    line_records: list[dict[str, Any]] = []
    hunk_id = 0

    for i, file_diff in enumerate(_split_into_file_diffs(diff_content)):
        file_name = _extract_file_name(file_diff, i)
        parts = re.split(r"^(?=@@ )", file_diff, flags=re.MULTILINE)
        hunk_texts = parts[1:] if len(parts) > 1 and parts[0].strip() else [file_diff]
        for h_text in hunk_texts:
            hunk_id += 1
            meta, annotated = _annotate_hunk_lines(h_text, file_name, hunk_id)
            recs = [rec for _, rec in annotated if rec is not None]
            if recs or h_text.startswith("@@ "):
                hunk_records.append(meta)
                line_records.extend(recs)

    return hunk_records, line_records


def _split_annotated_by_lines(
    header: str,
    annotated_lines: list[tuple[str, dict[str, Any] | None]],
    hunk_meta: dict[str, Any] | None,
    max_bytes: int,
    max_lines: int,
) -> list[SegmentTuple]:
    """Splits an oversized annotated hunk body by lines while preserving line records."""
    segments: list[SegmentTuple] = []
    cur_strs: list[str] = []
    cur_recs: list[dict[str, Any]] = []
    hunk_list = [hunk_meta] if hunk_meta is not None else []

    for line_str, rec in annotated_lines:
        candidate = header + "".join(cur_strs) + line_str
        if cur_strs and not _fits_limits(candidate, max_bytes, max_lines):
            segments.append((header + "".join(cur_strs), cur_recs, hunk_list))
            cur_strs = [line_str]
            cur_recs = [rec] if rec is not None else []
        else:
            cur_strs.append(line_str)
            if rec is not None:
                cur_recs.append(rec)

    if cur_strs:
        segments.append((header + "".join(cur_strs), cur_recs, hunk_list))
    return segments


def _split_by_lines(
    header: str, body_text: str, max_bytes: int, max_lines: int
) -> list[str]:
    """Splits body_text into line-bounded string segments prefixed with header."""
    annotated = [(line, None) for line in body_text.splitlines(keepends=True)]
    return [
        seg_text
        for seg_text, _, _ in _split_annotated_by_lines(
            header, annotated, None, max_bytes, max_lines
        )
    ]


def _split_headerless_file_diff(
    file_diff: str,
    file_name: str,
    hunk_id: int,
    max_bytes: int,
    max_lines: int,
) -> list[SegmentTuple]:
    """Splits a file diff that contains no @@ hunk headers."""
    meta, annotated = _annotate_hunk_lines(file_diff, file_name, hunk_id)
    recs = [r for _, r in annotated if r is not None]
    active_meta = meta if recs else None
    if _fits_limits(file_diff, max_bytes, max_lines):
        hunks = [meta] if active_meta else []
        return [(file_diff, recs, hunks)]
    return _split_annotated_by_lines(
        "", annotated, active_meta, max_bytes, max_lines
    )


def _split_file_diff_annotated(
    file_diff: str,
    file_name: str,
    start_hunk_id: int,
    max_bytes: int,
    max_lines: int,
) -> tuple[list[SegmentTuple], int]:
    """Splits a file diff into size-bounded segments with attached line and hunk records."""
    parts = re.split(r"^(?=@@ )", file_diff, flags=re.MULTILINE)
    if len(parts) <= 1 or not parts[0].strip():
        hunk_id = start_hunk_id + 1
        return (
            _split_headerless_file_diff(
                file_diff, file_name, hunk_id, max_bytes, max_lines
            ),
            hunk_id,
        )

    header = parts[0]
    segments: list[SegmentTuple] = []
    cur_hunk_strs: list[str] = []
    cur_recs: list[dict[str, Any]] = []
    cur_hunks: list[dict[str, Any]] = []
    hunk_id = start_hunk_id

    for hunk in parts[1:]:
        hunk_id += 1
        meta, annotated = _annotate_hunk_lines(hunk, file_name, hunk_id)
        hunk_recs = [r for _, r in annotated if r is not None]
        candidate = header + "".join(cur_hunk_strs) + hunk

        if cur_hunk_strs and not _fits_limits(candidate, max_bytes, max_lines):
            segments.append(
                (header + "".join(cur_hunk_strs), cur_recs, cur_hunks)
            )
            cur_hunk_strs, cur_recs, cur_hunks = [], [], []

        if _fits_limits(header + hunk, max_bytes, max_lines):
            cur_hunk_strs.append(hunk)
            cur_recs.extend(hunk_recs)
            cur_hunks.append(meta)
        else:
            segments.extend(
                _split_annotated_by_lines(
                    header, annotated, meta, max_bytes, max_lines
                )
            )

    if cur_hunk_strs:
        segments.append((header + "".join(cur_hunk_strs), cur_recs, cur_hunks))
    return segments, hunk_id


def _split_oversized_file_diff(
    file_diff: str, max_bytes: int, max_lines: int
) -> list[str]:
    """Splits a single file diff along hunk or line boundaries to fit limits."""
    if _fits_limits(file_diff, max_bytes, max_lines):
        return [file_diff]
    segments, _ = _split_file_diff_annotated(
        file_diff, "chunk_0.diff", 0, max_bytes, max_lines
    )
    return [seg_text for seg_text, _, _ in segments]


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _write_grouped_chunk(
    output_dir: str,
    index: int,
    texts: list[str],
    files: list[str],
    line_recs: list[dict[str, Any]],
    hunk_recs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Writes a grouped diff chunk file and stamps chunk_name on its records."""
    chunk_name = f"diff_chunk_{index:02d}.diff"
    chunk_path = os.path.join(output_dir, chunk_name)
    content = "".join(texts)
    with open(chunk_path, "w", encoding="utf-8") as f:
        f.write(content)
    for rec in line_recs:
        rec["chunk_name"] = chunk_name
    for h_rec in hunk_recs:
        if not h_rec["chunk_name"]:
            h_rec["chunk_name"] = chunk_name
    return {
        "chunk_name": chunk_name,
        "chunk_file": os.path.abspath(chunk_path),
        "files": list(files),
        "lines": _line_count(content),
        "bytes": _byte_len(content),
    }


def _write_jsonl(path: str, records: list[dict[str, Any]]) -> str:
    """Writes a list of dictionaries to a newline-delimited JSON file."""
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return os.path.abspath(path)


def resolve_repo_dir(repo_dir: str | None) -> str | None:
    """Resolves a repository checkout path from a directory, text file, or coords JSON."""
    if not repo_dir:
        return None
    candidate = repo_dir.strip()
    if not os.path.isfile(candidate):
        return candidate or None
    try:
        raw = Path(candidate).read_text(encoding="utf-8").strip()
    except OSError:
        return candidate or None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            resolved = (
                str(data.get("workspace_dir") or "").strip()
                if isinstance(data, dict)
                else ""
            )
            return resolved or None
        except ValueError:
            return None
    return raw.splitlines()[0].strip() if raw else None


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
        if (ent := _py_node_to_entity(node, file_path, side, method_ids))
        is not None
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


def _try_sem_entities(
    rel_path: str, full_path: Path, side: str
) -> list[dict[str, Any]] | None:
    """Extracts entities via the sem CLI when available on PATH."""
    if side != "NEW" or not shutil.which("sem") or not full_path.is_file():
        return None
    res = subprocess.run(
        ["sem", "entities", str(full_path), "--format", "json"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    if res.returncode != 0:
        return None
    try:
        items = json.loads(res.stdout)
        return [
            {
                "file_path": rel_path,
                "side": side,
                "kind": str(it.get("type") or "symbol"),
                "name": str(it.get("name") or ""),
                "signature": str(it.get("signature") or it.get("name") or ""),
                "line_start": int(it.get("line_start") or 1),
                "line_end": int(
                    it.get("line_end") or it.get("line_start") or 1
                ),
            }
            for it in items
            if isinstance(it, dict) and it.get("name")
        ]
    except (ValueError, TypeError):
        return None


def _extract_content_entities(
    rel_path: str,
    side: str,
    content: str,
    full_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Dispatches symbol extraction for a single file snapshot."""
    if full_path is not None:
        sem_res = _try_sem_entities(rel_path, full_path, side)
        if sem_res:
            return sem_res
    if rel_path.endswith(".py"):
        return _extract_py_entities(rel_path, side, content)
    return _extract_brace_entities(rel_path, side, content)


def _read_git_blob(repo_dir: str, git_ref: str, rel_path: str) -> str | None:
    """Reads a file blob at git_ref:<rel_path> from repo_dir."""
    res = subprocess.run(
        ["git", "-C", repo_dir, "show", f"{git_ref}:{rel_path}"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    return res.stdout if res.returncode == 0 else None


def extract_diff_entities(
    file_paths: list[str],
    repo_dir: str,
    base_ref: str | None = None,
) -> list[dict[str, Any]]:
    """Extracts AST symbol spans for NEW (and optional OLD) versions of changed files."""
    resolved_dir = resolve_repo_dir(repo_dir)
    if not resolved_dir:
        return []

    root = Path(resolved_dir)
    entities: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for raw_path in file_paths:
        rel_path = raw_path.removeprefix("./")
        if not rel_path or rel_path in seen_paths:
            continue
        seen_paths.add(rel_path)

        full_path = root / rel_path
        if full_path.is_file():
            try:
                new_text = full_path.read_text(
                    encoding="utf-8", errors="replace"
                )
                entities.extend(
                    _extract_content_entities(
                        rel_path, "NEW", new_text, full_path
                    )
                )
            except OSError:
                pass

        if base_ref:
            old_text = _read_git_blob(resolved_dir, base_ref, rel_path)
            if old_text is not None:
                entities.extend(
                    _extract_content_entities(rel_path, "OLD", old_text)
                )

    return entities


def _collect_grouped_segments(
    file_diffs: list[str], max_bytes: int, max_lines: int
) -> tuple[
    list[tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]],
    list[str],
    list[dict[str, Any]],
]:
    """Splits all file diffs into segments and collects unique file paths and hunk records."""
    segments: list[
        tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]
    ] = []
    all_files: list[str] = []
    all_hunks: list[dict[str, Any]] = []
    seen_hunk_ids: set[int] = set()
    hunk_id = 0

    for i, file_diff in enumerate(file_diffs):
        file_name = _extract_file_name(file_diff, i)
        _append_unique(all_files, file_name)
        file_segs, hunk_id = _split_file_diff_annotated(
            file_diff, file_name, hunk_id, max_bytes, max_lines
        )
        for seg_text, seg_recs, seg_hunks in file_segs:
            norm_text = seg_text if seg_text.endswith("\n") else seg_text + "\n"
            segments.append((file_name, norm_text, seg_recs, seg_hunks))
            for h_meta in seg_hunks:
                if h_meta["hunk_id"] not in seen_hunk_ids:
                    seen_hunk_ids.add(h_meta["hunk_id"])
                    all_hunks.append(h_meta)

    return segments, all_files, all_hunks


def _pack_segments_into_chunks(
    segments: list[
        tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]
    ],
    output_dir: str,
    max_bytes: int,
    max_lines: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Packs annotated diff segments into chunk files and returns (chunks_meta, all_lines)."""
    chunks_meta: list[dict[str, Any]] = []
    all_lines: list[dict[str, Any]] = []
    cur_texts: list[str] = []
    cur_files: list[str] = []
    cur_recs: list[dict[str, Any]] = []
    cur_hunks: list[dict[str, Any]] = []

    for file_name, seg_text, seg_recs, seg_hunks in segments:
        candidate = "".join(cur_texts) + seg_text
        if cur_texts and not _fits_limits(candidate, max_bytes, max_lines):
            chunks_meta.append(
                _write_grouped_chunk(
                    output_dir,
                    len(chunks_meta) + 1,
                    cur_texts,
                    cur_files,
                    cur_recs,
                    cur_hunks,
                )
            )
            all_lines.extend(cur_recs)
            cur_texts, cur_files = [seg_text], [file_name]
            cur_recs, cur_hunks = list(seg_recs), list(seg_hunks)
        else:
            cur_texts.append(seg_text)
            _append_unique(cur_files, file_name)
            cur_recs.extend(seg_recs)
            cur_hunks.extend(seg_hunks)

    if cur_texts:
        chunks_meta.append(
            _write_grouped_chunk(
                output_dir,
                len(chunks_meta) + 1,
                cur_texts,
                cur_files,
                cur_recs,
                cur_hunks,
            )
        )
        all_lines.extend(cur_recs)

    return chunks_meta, all_lines


def split_diff_grouped(
    diff_content: str,
    output_dir: str,
    max_bytes: int = 35000,
    max_lines: int = 700,
    repo_dir: str | None = None,
    base_ref: str | None = None,
    with_entities: bool = False,
) -> list[dict[str, Any]]:
    """Packs per-file diffs into size-bounded chunk files and writes JSONL tables."""
    os.makedirs(output_dir, exist_ok=True)
    file_diffs = _split_into_file_diffs(diff_content)
    segments, all_files, all_hunks = _collect_grouped_segments(
        file_diffs, max_bytes, max_lines
    )
    chunks_meta, all_lines = _pack_segments_into_chunks(
        segments, output_dir, max_bytes, max_lines
    )

    lines_jsonl = _write_jsonl(
        os.path.join(output_dir, "diff_lines.jsonl"), all_lines
    )
    hunks_jsonl = _write_jsonl(
        os.path.join(output_dir, "diff_hunks.jsonl"), all_hunks
    )
    entity_recs = (
        extract_diff_entities(all_files, repo_dir or ".", base_ref)
        if (with_entities or repo_dir)
        else []
    )
    entities_jsonl = _write_jsonl(
        os.path.join(output_dir, "diff_entities.jsonl"), entity_recs
    )

    manifest = {
        "total_bytes": _byte_len(diff_content),
        "total_lines": _line_count(diff_content),
        "chunk_count": len(chunks_meta),
        "lines_jsonl": lines_jsonl,
        "hunks_jsonl": hunks_jsonl,
        "entities_jsonl": entities_jsonl,
        "chunks": chunks_meta,
    }
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)

    return chunks_meta


def split_diff(diff_content: str, output_dir: str) -> list[str]:
    """Splits a unified diff into one file per modified path in output_dir."""
    os.makedirs(output_dir, exist_ok=True)
    files = _split_into_file_diffs(diff_content)

    summary = []
    for i, file_diff in enumerate(files):
        file_name = _extract_file_name(file_diff, i)
        safe_name = (
            file_name.replace("/", "_")
            if file_name != f"chunk_{i}.diff"
            else file_name
        )

        file_path = os.path.join(output_dir, safe_name)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(file_diff)

        summary.append(f"- {file_name} -> {safe_name}")

    return summary


def _read_diff_input(args: argparse.Namespace) -> str:
    content = args.input.read()
    if not args.json:
        return content
    try:
        json_data = json.loads(content)
        return extract_diff_from_json(json_data, args.json_key)
    except json.JSONDecodeError:
        print("Error: Input is not valid JSON", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract and split diffs for code review."
    )
    parser.add_argument(
        "input",
        nargs="?",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help="Input file (default: stdin)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Input is JSON encoded"
    )
    parser.add_argument(
        "--json-key", help="Key in JSON containing the diff string"
    )
    parser.add_argument(
        "--output-dir", required=True, help="Directory to write chunks to"
    )
    parser.add_argument(
        "--grouped",
        action="store_true",
        help="Pack diffs into size-bounded diff_chunk_XX.diff files with manifest.json",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=35000,
        help="Maximum bytes per grouped chunk file (default: 35000)",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=700,
        help="Maximum lines per grouped chunk file (default: 700)",
    )
    parser.add_argument(
        "--repo-dir",
        help="Repository checkout path (or workspace_dir.txt / pr_coords.json) for AST entity extraction",
    )
    parser.add_argument(
        "--base-ref",
        help="Optional git base ref for extracting OLD-side AST entity spans",
    )
    parser.add_argument(
        "--with-entities",
        action="store_true",
        help="Extract AST symbol spans into diff_entities.jsonl",
    )

    args = parser.parse_args()
    diff_content = _read_diff_input(args)

    if args.grouped:
        chunks = split_diff_grouped(
            diff_content,
            args.output_dir,
            max_bytes=args.max_bytes,
            max_lines=args.max_lines,
            repo_dir=args.repo_dir,
            base_ref=args.base_ref,
            with_entities=args.with_entities,
        )
        print(
            f"Successfully split diff into {len(chunks)} grouped chunk(s) in {args.output_dir}"
        )
        for chunk in chunks:
            files_str = ", ".join(chunk["files"])
            print(
                f"- {chunk['chunk_file']} ({chunk['lines']} lines, {chunk['bytes']} bytes): {files_str}"
            )
        return

    summary = split_diff(diff_content, args.output_dir)
    print(f"Successfully split diff into {len(summary)} files in {args.output_dir}")
    print("\n".join(summary))


if __name__ == "__main__":
    main()
