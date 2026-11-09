#!/usr/bin/env python3
"""Prepares deterministic context for /design-review runs.

Classifies target inputs (design-doc, code-architecture, or conformance),
evaluates One-Way vs. Two-Way Door reversibility, stages Markdown files via
context-indexer with file:// and repo-root link post-resolution, and collects
module architecture metrics (import cycles, cognitive complexity hotspots, and
read-only packrat churn).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

VALID_MODES = ("design-doc", "code-architecture", "conformance")
PROSE_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".adoc", ".txt"})
CODE_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".js", ".jsx", ".dart", ".swift", ".kt", ".cc", ".cpp", ".h"}
)
SKIP_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        ".dart_tool",
    }
)
PUBLIC_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pubspec.yaml",
        "pyproject.toml",
        "Cargo.toml",
        "public_skills.json",
    }
)

ONE_WAY_DOOR_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        "persisted-schema",
        r"\b(?:CREATE\s+TABLE|ALTER\s+TABLE|sqlite3?|postgres|spanner|migration\s+schema)\b",
    ),
    (
        "wire-protocol",
        r"\b(?:protobuf|json-rpc|json\s+schema|wire\s+(?:format|envelope|protocol)|websocket|sse)\b",
    ),
    (
        "public-contract",
        r"\b(?:public\s+api|breaking\s+change|public_skills\.json|exported\s+interface|cli\s+contract)\b",
    ),
    (
        "trust-boundary",
        r"\b(?:trust\s+boundary|authentication|authorization|credential|sandbox|csrf|csp)\b",
    ),
    (
        "cross-repo-split",
        r"\b(?:cross-repo|companion\s+pr|multi-repo|package\s+split)\b",
    ),
)

_COMPILED_DOOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE)) for name, pat in ONE_WAY_DOOR_PATTERNS
)
_DECISION_LINE_RE = re.compile(
    r"^\s*(?:[-*+]|\d+\.)\s+\*\*(?P<id>[DQ]\d+[^*]*)\*\*:?\s*(?P<rest>.+)$"
)


@dataclass(frozen=True)
class DoorClassification:
    """Reversibility classification and recommended review depth flags.

    Attributes:
        overall: Either 'one-way' or 'two-way'.
        signals: Matched signal identifiers explaining the classification.
        recommended_flags: Suggested review flags (['--panel'] for one-way).
    """

    overall: str
    signals: list[str]
    recommended_flags: list[str]


def _is_json_schema_file(path: Path) -> bool:
    """Returns whether path is a JSON Schema document."""
    if path.name.endswith(".schema.json"):
        return True
    if path.suffix.lower() != ".json" or not path.is_file():
        return False
    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:4096]
    except OSError:
        return False
    return '"$schema"' in head and "json-schema.org" in head


def _is_prose_or_schema(path: Path) -> bool:
    """Returns whether path is a prose document or JSON Schema file."""
    return path.suffix.lower() in PROSE_SUFFIXES or _is_json_schema_file(path)


def classify_target_mode(targets: Sequence[Path], against_spec: Path | None) -> str:
    """Classifies the review mode from target paths and optional governing spec.

    Args:
        targets: Target files or directories under review.
        against_spec: Optional governing specification path for conformance checks.

    Returns:
        One of 'design-doc', 'code-architecture', or 'conformance'.
    """
    if against_spec is not None:
        return "conformance"
    if all(t.is_file() and _is_prose_or_schema(t) for t in targets):
        return "design-doc"
    return "code-architecture"


def _walk_directory_files(directory: Path) -> list[Path]:
    """Collects non-ignored files recursively under directory."""
    collected: list[Path] = []
    for entry in sorted(directory.rglob("*")):
        if entry.is_file() and not any(part in SKIP_DIRS for part in entry.parts):
            collected.append(entry)
    return collected


def _collect_target_files(targets: Sequence[Path]) -> list[Path]:
    """Expands file and directory targets into a deduplicated file list."""
    seen: set[Path] = set()
    files: list[Path] = []
    for target in targets:
        candidates = [target] if target.is_file() else _walk_directory_files(target)
        for item in candidates:
            resolved = item.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(resolved)
    return files


def _is_test_file(path: Path) -> bool:
    """Returns whether path follows standard test naming conventions."""
    stem = path.stem.lower()
    return stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))


def _count_file_substantive_lines(path: Path) -> float:
    """Computes weighted non-blank lines for a single file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0.0
    if _is_prose_or_schema(path):
        return float(sum(1 for ln in lines if ln.strip()))
    if path.suffix.lower() not in CODE_SUFFIXES:
        return 0.0
    code_lines = sum(
        1 for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", "//"))
    )
    weight = 0.25 if _is_test_file(path) else 1.0
    return code_lines * weight


def count_substantive_lines(files: Sequence[Path]) -> int:
    """Returns total rounded substantive lines across files."""
    return round(sum(_count_file_substantive_lines(f) for f in files))


def _has_public_surface_file(files: Sequence[Path]) -> bool:
    """Returns whether files include package manifests or schema definitions."""
    for path in files:
        if (
            path.name in PUBLIC_MANIFEST_NAMES
            or path.suffix.lower() == ".proto"
            or _is_json_schema_file(path)
        ):
            return True
    return False


def classify_door_reversibility(
    text: str,
    substantive_lines: int,
    is_code_public_surface: bool,
) -> DoorClassification:
    """Classifies architectural reversibility into one-way or two-way doors.

    Args:
        text: Combined text of the target specification or module interfaces.
        substantive_lines: Weighted line count of the target.
        is_code_public_surface: Whether target files include public manifests/schemas.

    Returns:
        A DoorClassification with matched signals and recommended flags.
    """
    signals = [name for name, regex in _COMPILED_DOOR_PATTERNS if regex.search(text)]
    if is_code_public_surface and "public-contract" not in signals:
        signals.append("public-contract")
    if substantive_lines > 400:
        signals.append("large-spec")
    if signals:
        return DoorClassification(
            overall="one-way", signals=signals, recommended_flags=["--panel"]
        )
    return DoorClassification(overall="two-way", signals=[], recommended_flags=[])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Reads newline-delimited JSON objects from path if present."""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.strip():
            rows.append(json.loads(raw))
    return rows


def _broken_entry(row: dict[str, Any], reason: str) -> dict[str, Any]:
    """Formats a broken link dictionary from a staged link row."""
    return {
        "file_path": row.get("file_path", ""),
        "line_num": row.get("line_num", 0),
        "label": row.get("label", ""),
        "raw_target": row.get("raw_target", ""),
        "reason": reason,
    }


def _check_file_uri_link(row: dict[str, Any]) -> dict[str, Any] | None:
    """Validates a file:// link target directly on disk, stripping #L... anchors."""
    raw_target = str(row.get("raw_target", ""))
    parsed = urlparse(raw_target)
    fs_path = Path(unquote(parsed.path))
    if fs_path.exists():
        return None
    return _broken_entry(row, "missing_file")


def _is_outside_root(file_path_str: str, repo_root: Path) -> bool:
    """Returns whether file_path_str refers to an absolute path outside repo_root."""
    candidate = Path(file_path_str)
    if not candidate.is_absolute():
        return False
    try:
        candidate.resolve().relative_to(repo_root.resolve())
        return False
    except ValueError:
        return True


def _recheck_external_relative_link(
    row: dict[str, Any], repo_root: Path
) -> dict[str, Any] | None:
    """Re-evaluates a broken relative link from an external artifact against repo_root."""
    raw_target = str(row.get("raw_target", ""))
    path_part = raw_target.split("#", 1)[0].strip()
    if path_part and (repo_root / path_part).exists():
        return None
    return _broken_entry(row, str(row.get("reason") or "missing_file"))


def _load_staged_slugs(md_links_path: Path) -> dict[str, set[str]]:
    """Loads {file_path: {anchor_slug, ...}} from sibling md_sections.jsonl."""
    slugs: dict[str, set[str]] = {}
    for row in _read_jsonl(md_links_path.parent / "md_sections.jsonl"):
        fpath = str(row.get("file_path", ""))
        slug = str(row.get("anchor_slug", ""))
        slugs.setdefault(fpath, set())
        if slug:
            slugs[fpath].add(slug)
    return slugs


def _resolve_single_link_row(
    row: dict[str, Any], repo_root: Path, slugs_by_file: dict[str, set[str]]
) -> dict[str, Any] | None:
    """Resolves one md_links.jsonl entry, returning a broken record or None."""
    raw_target = str(row.get("raw_target", ""))
    if raw_target.startswith("file://"):
        return _check_file_uri_link(row)
    if row.get("is_external"):
        return None
    is_missing = row.get("target_exists") is False or bool(row.get("broken"))
    if is_missing:
        if _is_outside_root(str(row.get("file_path", "")), repo_root):
            return _recheck_external_relative_link(row, repo_root)
        return _broken_entry(row, str(row.get("reason") or "missing_file"))
    anchor = str(row.get("anchor", ""))
    t_path = str(row.get("target_path", ""))
    if anchor and t_path in slugs_by_file and anchor not in slugs_by_file[t_path]:
        return _broken_entry(row, "broken_anchor")
    return None


def resolve_staged_links(md_links_path: Path, repo_root: Path) -> list[dict[str, Any]]:
    """Post-processes md_links.jsonl for file:// URIs, external artifacts, and anchors.

    Args:
        md_links_path: Path to md_links.jsonl produced by stage_markdown.py.
        repo_root: Repository root directory for workspace-relative fallback checks.

    Returns:
        List of genuinely broken link records.
    """
    slugs_by_file = _load_staged_slugs(md_links_path)
    broken: list[dict[str, Any]] = []
    for row in _read_jsonl(md_links_path):
        issue = _resolve_single_link_row(row, repo_root, slugs_by_file)
        if issue is not None:
            broken.append(issue)
    return broken


def _extract_settled_decisions(md_files: Sequence[Path]) -> list[str]:
    """Extracts recorded D<n> or Q<n> decision lines from Markdown files."""
    decisions: list[str] = []
    for path in md_files:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _DECISION_LINE_RE.match(line)
            if match:
                decisions.append(
                    f"{match.group('id').strip()}: {match.group('rest').strip()}"
                )
    return decisions


def _format_stage_input(path: Path, repo_root: Path) -> str:
    """Formats path as repo-relative when inside repo_root, else absolute."""
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _stage_markdown_script() -> Path:
    """Returns the sibling context-indexer stage_markdown.py path."""
    return (
        Path(__file__).resolve().parents[2]
        / "context-indexer"
        / "scripts"
        / "stage_markdown.py"
    )


def stage_design_markdown(
    md_files: Sequence[Path],
    repo_root: Path,
    scratch_dir: Path,
) -> dict[str, Any]:
    """Stages Markdown documents via stage_markdown.py and resolves links.

    Args:
        md_files: Markdown files to stage.
        repo_root: Target repository root directory.
        scratch_dir: Output scratch directory.

    Returns:
        Dictionary summarizing staged chunks, sections, links, Mermaid count, and decisions.
    """
    if not md_files:
        return {
            "staged_dir": None,
            "chunks": [],
            "section_count": 0,
            "broken_links": [],
            "mermaid_count": 0,
            "settled_decisions": [],
        }
    staged_dir = scratch_dir / "staged_md"
    staged_dir.mkdir(parents=True, exist_ok=True)
    total_lines = sum(
        len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
        for p in md_files
    )
    cmd = [
        sys.executable,
        str(_stage_markdown_script()),
        "--root",
        str(repo_root.resolve()),
        "--output-dir",
        str(staged_dir),
    ]
    if total_lines > 500:
        cmd.append("--split-chunks")
    cmd.extend(_format_stage_input(p, repo_root) for p in md_files)
    subprocess.run(cmd, check=True, capture_output=True, text=True)

    sections = _read_jsonl(staged_dir / "md_sections.jsonl")
    non_empty_inputs = any(
        p.read_text(encoding="utf-8", errors="ignore").strip() for p in md_files
    )
    if non_empty_inputs and not sections:
        raise RuntimeError(
            "stage_markdown.py produced zero sections for non-empty Markdown input."
        )
    code_blocks = _read_jsonl(staged_dir / "md_code_blocks.jsonl")
    mermaid_count = sum(
        1
        for b in code_blocks
        if str(b.get("lang", b.get("language", ""))).lower() == "mermaid"
    )
    chunks = sorted(p.name for p in staged_dir.glob("md_chunk_*.md"))
    return {
        "staged_dir": str(staged_dir),
        "chunks": chunks,
        "section_count": len(sections),
        "broken_links": resolve_staged_links(staged_dir / "md_links.jsonl", repo_root),
        "mermaid_count": mermaid_count,
        "settled_decisions": _extract_settled_decisions(md_files),
    }


def _module_name_for_py(py_file: Path, base_root: Path) -> str:
    """Computes a dotted module identifier relative to base_root."""
    rel = py_file.resolve().relative_to(base_root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or py_file.stem


def _imported_module_names(node: ast.AST) -> list[str]:
    """Returns candidate dotted module names referenced by an import AST node."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
    return []


def _extract_import_targets(tree: ast.AST, known_modules: dict[str, str]) -> set[str]:
    """Extracts internal module dependencies from an AST."""
    deps: set[str] = set()
    for node in ast.walk(tree):
        for raw_name in _imported_module_names(node):
            if raw_name in known_modules:
                deps.add(known_modules[raw_name])
    return deps


def _find_graph_cycles(adj: dict[str, set[str]]) -> list[list[str]]:
    """Finds elementary import cycles across a small module adjacency graph."""
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node: str) -> None:
        visited.add(node)
        active.append(node)
        active_set.add(node)
        for nxt in sorted(adj.get(node, ())):
            if nxt in active_set:
                idx = active.index(nxt)
                cycles.append([*active[idx:], nxt])
            elif nxt not in visited:
                visit(nxt)
        active.pop()
        active_set.remove(node)

    for start in sorted(adj):
        if start not in visited:
            visit(start)
    return cycles


def find_python_import_cycles(
    code_files: Sequence[Path], repo_root: Path
) -> list[list[str]]:
    """Detects circular import chains among Python files in code_files."""
    py_files = [f for f in code_files if f.suffix == ".py" and not _is_test_file(f)]
    if len(py_files) < 2:
        return []
    known: dict[str, str] = {}
    file_labels: dict[Path, str] = {}
    for py_file in py_files:
        try:
            label = py_file.resolve().relative_to(repo_root.resolve()).as_posix()
            mod_name = _module_name_for_py(py_file, repo_root)
        except ValueError:
            label = py_file.name
            mod_name = py_file.stem
        file_labels[py_file] = label
        known[mod_name] = label
        known[py_file.stem] = label

    adj: dict[str, set[str]] = {}
    for py_file, label in file_labels.items():
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        deps = _extract_import_targets(tree, known)
        deps.discard(label)
        adj[label] = deps
    return _find_graph_cycles(adj)


def _probe_cognitive_complexity(
    code_files: Sequence[Path],
) -> tuple[list[dict[str, Any]], bool]:
    """Runs cognitive_complexity.py when available and returns functions > 15."""
    script = (
        Path(__file__).resolve().parents[2]
        / "cognitive-complexity"
        / "scripts"
        / "cognitive_complexity.py"
    )
    supported = [
        str(f)
        for f in code_files
        if f.suffix.lower()
        in {".py", ".ts", ".tsx", ".js", ".dart", ".swift", ".kt", ".cc", ".cpp"}
        and not _is_test_file(f)
    ]
    if not script.is_file() or not supported:
        return [], False
    res = subprocess.run(
        [sys.executable, str(script), "--format", "json", "-t", "15", *supported[:30]],
        capture_output=True,
        text=True,
        check=False,
    )
    if not res.stdout.strip():
        return [], True
    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError:
        return [], True
    hotspots = _extract_complexity_hotspots(payload)
    return hotspots, True


def _extract_complexity_hotspots(payload: Any) -> list[dict[str, Any]]:
    """Extracts functions exceeding score 15 from cognitive_complexity JSON output."""
    items = (
        payload.get("files", [])
        if isinstance(payload, dict)
        else (payload if isinstance(payload, list) else [])
    )
    hotspots: list[dict[str, Any]] = []
    for file_entry in items:
        if not isinstance(file_entry, dict):
            continue
        fpath = file_entry.get("file") or file_entry.get("path", "")
        for fn in file_entry.get("functions", []):
            score = int(fn.get("score", fn.get("complexity", 0)))
            if score > 15:
                hotspots.append(
                    {
                        "file": fpath,
                        "function": fn.get("name", ""),
                        "score": score,
                        "line": fn.get("line", 0),
                    }
                )
    return hotspots


def _probe_packrat_hotspots(
    code_files: Sequence[Path], repo_root: Path
) -> tuple[list[dict[str, Any]], str, bool]:
    """Queries packrat_context.py read-only for historical file churn hotspots."""
    packrat_dir = Path(__file__).resolve().parents[2] / "code-review" / "scripts"
    if not (packrat_dir / "packrat_context.py").is_file() or not code_files:
        return [], "", False
    if str(packrat_dir) not in sys.path:
        sys.path.insert(0, str(packrat_dir))
    import packrat_context  # type: ignore[import-not-found]

    rel_paths: list[str] = []
    for f in code_files:
        try:
            rel_paths.append(f.resolve().relative_to(repo_root.resolve()).as_posix())
        except ValueError:
            continue
    if not rel_paths:
        return [], "", True
    probe = packrat_context.probe_packrat(repo_root)
    if probe.status != "ready" or not probe.db_path or not probe.repo:
        return [], str(probe.user_note or ""), True
    ctx = packrat_context.query_changed_files_context(
        Path(probe.db_path),
        probe.repo,
        rel_paths[:40],
        probe=probe,
        root=repo_root,
    )
    return list(ctx.get("hotspots", []))[:10], str(probe.user_note or ""), True


def collect_code_metrics(code_files: Sequence[Path], repo_root: Path) -> dict[str, Any]:
    """Collects architectural metrics for code files with graceful tool fallback.

    Args:
        code_files: Non-prose files belonging to the target module(s).
        repo_root: Repository root directory.

    Returns:
        Dictionary of file count, import cycles, complexity hotspots, packrat hotspots, and tool status.
    """
    tools_used: list[str] = []
    skipped_probes: list[str] = []
    if shutil.which("sem"):
        tools_used.append("sem")
    else:
        skipped_probes.append("sem")

    hotspots, cc_ran = _probe_cognitive_complexity(code_files)
    (tools_used if cc_ran else skipped_probes).append("cognitive-complexity")
    packrat_hotspots, user_note, pk_ran = _probe_packrat_hotspots(code_files, repo_root)
    (tools_used if pk_ran else skipped_probes).append("packrat")

    return {
        "file_count": len(code_files),
        "high_complexity_functions": hotspots,
        "dependency_cycles": find_python_import_cycles(code_files, repo_root),
        "packrat_hotspots": packrat_hotspots,
        "tools_used": tools_used,
        "skipped_probes": skipped_probes,
        "_user_note": user_note,
    }


def _read_combined_text(files: Sequence[Path], max_bytes_per_file: int = 32768) -> str:
    """Reads bounded text snippets from files for door-signal scanning."""
    chunks: list[str] = []
    for path in files[:40]:
        try:
            chunks.append(
                path.read_text(encoding="utf-8", errors="ignore")[:max_bytes_per_file]
            )
        except OSError:
            continue
    return "\n".join(chunks)


def _resolve_one_path(raw: str | Path, repo_root: Path) -> Path:
    """Resolves raw against repo_root when relative."""
    p = Path(raw)
    return p.resolve() if p.is_absolute() else (repo_root / p).resolve()


def _resolve_cli_inputs(
    targets: Sequence[str], against_spec: Path | None, repo_root: Path
) -> tuple[list[Path], Path | None]:
    """Resolves and validates target paths and optional --against spec."""
    resolved_targets: list[Path] = []
    for raw in targets:
        resolved = _resolve_one_path(raw, repo_root)
        if not resolved.exists():
            raise FileNotFoundError(f"Target path does not exist: {raw}")
        resolved_targets.append(resolved)
    if against_spec is None:
        return resolved_targets, None
    resolved_against = _resolve_one_path(against_spec, repo_root)
    if not resolved_against.is_file():
        raise FileNotFoundError(f"--against spec file does not exist: {against_spec}")
    return resolved_targets, resolved_against


def build_design_context(
    targets: Sequence[str],
    repo_root: Path,
    output_dir: Path,
    against_spec: Path | None = None,
) -> dict[str, Any]:
    """Builds and writes design_context.json for a /design-review invocation.

    Args:
        targets: Target file or directory path strings.
        repo_root: Target repository root path.
        output_dir: Scratch directory where design_context.json is written.
        against_spec: Optional governing specification file for conformance mode.

    Returns:
        The serialized context dictionary written to design_context.json.

    Raises:
        FileNotFoundError: If any target or against_spec path does not exist.
    """
    resolved_targets, resolved_against = _resolve_cli_inputs(
        targets, against_spec, repo_root
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = classify_target_mode(resolved_targets, resolved_against)
    all_files = _collect_target_files(resolved_targets)
    md_files = [f for f in all_files if f.suffix.lower() in {".md", ".mdx"}]
    if (
        resolved_against is not None
        and resolved_against not in md_files
        and resolved_against.suffix.lower() in {".md", ".mdx"}
    ):
        md_files.append(resolved_against)
    code_files = [f for f in all_files if not _is_prose_or_schema(f)]

    subst_lines = count_substantive_lines(all_files)
    combined_text = _read_combined_text([*md_files, *code_files])
    door = classify_door_reversibility(
        combined_text, subst_lines, _has_public_surface_file(all_files)
    )
    md_info = stage_design_markdown(md_files, repo_root, output_dir)
    code_info = collect_code_metrics(code_files, repo_root)
    user_note = code_info.pop("_user_note", "")

    payload: dict[str, Any] = {
        "mode": mode,
        "targets": [str(t) for t in targets],
        "against_spec": str(against_spec) if against_spec is not None else None,
        "substantive_lines": subst_lines,
        "door_classification": asdict(door),
        "markdown": md_info,
        "code_metrics": code_info,
        "packrat_user_note": user_note,
    }
    (output_dir / "design_context.json").write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parses CLI arguments for prepare_design_context.py."""
    parser = argparse.ArgumentParser(
        description="Prepare staged context for /design-review."
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root directory (default: current directory).",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Scratch directory to write design_context.json.",
    )
    parser.add_argument(
        "--against",
        default=None,
        help="Optional governing design spec for conformance mode.",
    )
    parser.add_argument(
        "targets", nargs="+", help="Target files or directories to review."
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point returning 0 on success or 2 on usage/path errors."""
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    repo_root = Path(args.root).resolve()
    output_dir = Path(args.output_dir).resolve()
    against = Path(args.against) if args.against else None
    try:
        payload = build_design_context(
            args.targets, repo_root, output_dir, against_spec=against
        )
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
