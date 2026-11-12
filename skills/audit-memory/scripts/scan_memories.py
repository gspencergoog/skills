#!/usr/bin/env python3
"""Discovers cross-harness memory files, link graphs, promotion targets, and shards."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from memory_common import (
    count_claims,
    extract_relative_links,
    find_direct_lines,
    is_protected_file,
    is_within_root,
    parse_frontmatter,
    sha256_text,
)

MFS_MOUNT_RE = re.compile(
    r"^\s*(?P<group>[A-Za-z0-9_.-]+)\s+\((?P<mode>rw|ro)\)\s+.*?entries:\s*(?P<entries>[\d,]+)",
    re.IGNORECASE,
)


def _probe_mfs_skipped_mounts(memory_parent: Path) -> list[Path]:
    """Detects read-only or large FUSE mounts (such as `~/memory/skills`) to skip."""
    skipped: list[Path] = []
    skills_mount = memory_parent / "skills"
    if skills_mount.exists():
        skipped.append(skills_mount)
    try:
        proc = subprocess.run(
            ["mfs", "status"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return skipped
    for line in proc.stdout.splitlines():
        match = MFS_MOUNT_RE.search(line)
        if not match:
            continue
        group = match.group("group")
        mode = match.group("mode").lower()
        entries = int(match.group("entries").replace(",", ""))
        mount_path = memory_parent / group
        if (group == "skills" or (mode == "ro" and entries > 10000)) and (
            mount_path not in skipped
        ):
            skipped.append(mount_path)
    return skipped


def discover_memory_root(
    explicit_dir: str | None = None,
    home_dir: Path | None = None,
    cwd: Path | None = None,
) -> tuple[Path, list[Path]]:
    """Discovers the active memory root directory and any skipped FUSE mounts."""
    home = home_dir or Path.home()
    work = cwd or Path.cwd()
    skipped = _probe_mfs_skipped_mounts(home / "memory")
    if explicit_dir:
        target = Path(explicit_dir).expanduser().resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"Memory directory not found: {target}")
        return target, skipped

    candidates: list[Path] = [
        home / "memory" / "default",
        home / ".gemini" / "memory",
        work / ".agents" / "memory",
        work / ".claude" / "memory",
    ]
    claude_projects = home / ".claude" / "projects"
    if claude_projects.is_dir():
        candidates.extend(sorted(claude_projects.glob("*/memory")))

    for candidate in candidates:
        if candidate.is_dir() and not any(
            candidate.resolve().is_relative_to(s.resolve())
            for s in skipped
            if s.exists()
        ):
            return candidate.resolve(), skipped
    raise FileNotFoundError(
        "No memory directory found; pass --memory-dir <path>."
    )


def _is_writable_file(path: Path) -> bool:
    """Whether `path` exists as a regular file and is writable by the user."""
    try:
        real = path.resolve()
        return real.is_file() and os.access(real, os.W_OK)
    except OSError:
        return False


def _is_writable_skill_root(path: Path, skipped_mounts: list[Path]) -> bool:
    """Whether `path` is a writable skill directory outside skipped FUSE mounts."""
    try:
        real = path.resolve()
    except OSError:
        return False
    if not real.is_dir() or not os.access(real, os.W_OK):
        return False
    return not any(
        real.is_relative_to(s.resolve()) for s in skipped_mounts if s.exists()
    )


def discover_promotion_targets(
    workspace: Path | None = None,
    home_dir: Path | None = None,
    skipped_mounts: list[Path] | None = None,
) -> dict[str, Any]:
    """Discovers writable instruction files and user skill directories."""
    home = home_dir or Path.home()
    work = (workspace or Path.cwd()).resolve()
    skipped = skipped_mounts or []

    workspace_instr: str | None = None
    for rel in ("AGENTS.md", "CLAUDE.md", ".gemini/GEMINI.md", "GEMINI.md"):
        candidate = work / rel
        if _is_writable_file(candidate):
            workspace_instr = str(candidate.resolve())
            break

    global_instr: str | None = None
    for candidate in (
        work / "agents" / "agents.md",
        home / ".gemini" / "GEMINI.md",
        home / ".gemini" / "AGENTS.md",
        home / ".claude" / "CLAUDE.md",
    ):
        if _is_writable_file(candidate):
            global_instr = str(candidate.resolve())
            break

    skill_roots: list[str] = []
    for root in (
        work / "agents" / "skills",
        work / "skills",
        work / ".agents" / "skills",
        home / ".gemini" / "config" / "skills",
        home / ".claude" / "skills",
    ):
        if _is_writable_skill_root(root, skipped):
            resolved_str = str(root.resolve())
            if resolved_str not in skill_roots:
                skill_roots.append(resolved_str)

    return {
        "global_instructions": global_instr,
        "workspace_instructions": workspace_instr,
        "writable_skill_dirs": skill_roots,
    }


def _matches_scope(relpath: str, scope: str) -> bool:
    """Whether `relpath` matches the requested audit scope filter."""
    if scope in ("all", "*"):
        return True
    if scope in ("projects", "topics", "areas"):
        return relpath.startswith(f"{scope}/")
    if scope == "preferences":
        return Path(relpath).name in (
            "preferences.md",
            "communication_guidelines.md",
            "user.md",
        )
    return Path(relpath).match(scope) or scope in relpath


def _collect_memory_files(
    memory_root: Path, skipped_mounts: list[Path], scope: str
) -> list[Path]:
    """Collects markdown files under `memory_root` matching `scope`."""
    files: list[Path] = []
    for path in sorted(memory_root.rglob("*.md")):
        if not path.is_file():
            continue
        if not is_within_root(path, memory_root, skipped_mounts):
            continue
        rel = path.resolve().relative_to(memory_root.resolve()).as_posix()
        if _matches_scope(rel, scope):
            files.append(path.resolve())
    return files


def _build_shards(
    files_meta: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Partitions files into balanced audit shards (`ceil(files / 3)`, max 10)."""
    total_files = len(files_meta)
    if total_files == 0:
        return [], 0
    recommended = min(10, max(1, math.ceil(total_files / 3)))
    ordered = sorted(
        files_meta.items(),
        key=lambda item: (-item[1]["claim_count"], item[0]),
    )
    buckets: list[dict[str, Any]] = [
        {"shard_id": f"aud-{idx + 1}", "files": [], "claim_count": 0}
        for idx in range(recommended)
    ]
    for rel, meta in ordered:
        lightest = min(buckets, key=lambda b: (b["claim_count"], len(b["files"])))
        lightest["files"].append(rel)
        lightest["claim_count"] += meta["claim_count"]
    for bucket in buckets:
        bucket["files"].sort()
    non_empty = [b for b in buckets if b["files"]]
    return non_empty, len(non_empty)


def build_inventory(
    memory_root: Path,
    skipped_mounts: list[Path],
    promotion_targets: dict[str, Any],
    scope: str = "all",
) -> dict[str, Any]:
    """Scans `memory_root` and builds the `memory_inventory.json` data structure."""
    real_root = memory_root.resolve()
    md_files = _collect_memory_files(real_root, skipped_mounts, scope)
    files_meta: dict[str, dict[str, Any]] = {}
    incoming_map: dict[str, list[dict[str, Any]]] = {}

    for path in md_files:
        rel = path.relative_to(real_root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        stat = path.stat()
        fm = parse_frontmatter(lines)
        direct_lines = find_direct_lines(lines, start_after_line=fm.end_line)
        outgoing = extract_relative_links(
            lines, rel, real_root, start_after_line=fm.end_line
        )
        for link in outgoing:
            incoming_map.setdefault(link["target"], []).append(
                {"source_file": rel, "line": link["line"], "raw_target": link["raw_target"]}
            )
        files_meta[rel] = {
            "sha256": sha256_text(text),
            "mtime_ns": stat.st_mtime_ns,
            "line_count": len(lines),
            "claim_count": count_claims(lines, start_after_line=fm.end_line),
            "original_lines": lines,
            "protected_file": is_protected_file(rel, fm),
            "frontmatter_valid": fm.valid,
            "frontmatter_end_line": fm.end_line,
            "frontmatter_keys": fm.keys,
            "frontmatter_description": fm.description,
            "frontmatter_error": fm.error,
            "direct_lines": direct_lines,
            "outgoing_links": outgoing,
            "incoming_links": [],
        }

    for rel, meta in files_meta.items():
        meta["incoming_links"] = incoming_map.get(rel, [])

    shards, recommended_workers = _build_shards(files_meta)
    return {
        "schema_version": 1,
        "memory_root": str(real_root),
        "skipped_mounts": [str(s) for s in skipped_mounts],
        "promotion_targets": promotion_targets,
        "summary": {
            "total_files": len(files_meta),
            "total_lines": sum(m["line_count"] for m in files_meta.values()),
            "total_claims": sum(m["claim_count"] for m in files_meta.values()),
            "direct_claims": sum(len(m["direct_lines"]) for m in files_meta.values()),
            "recommended_workers": recommended_workers,
            "capped_workers": min(2, recommended_workers),
        },
        "shards": shards,
        "files": files_meta,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments for `scan_memories.py`."""
    parser = argparse.ArgumentParser(
        description="Scan agent memory files, link graphs, and promotion targets."
    )
    parser.add_argument("--memory-dir", help="Explicit memory directory path.")
    parser.add_argument("--workspace", help="Active repository workspace path.")
    parser.add_argument(
        "--scope",
        default="all",
        help="Scope filter: all, projects, topics, preferences, or a file glob.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Output JSON path for memory_inventory.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `scan_memories.py`."""
    args = _parse_args(argv)
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else Path.cwd()
    try:
        memory_root, skipped = discover_memory_root(
            explicit_dir=args.memory_dir, cwd=workspace
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    targets = discover_promotion_targets(
        workspace=workspace, skipped_mounts=skipped
    )
    inventory = build_inventory(
        memory_root=memory_root,
        skipped_mounts=skipped,
        promotion_targets=targets,
        scope=args.scope,
    )
    out_path = Path(args.output).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    summary = inventory["summary"]
    print(
        f"Scanned {summary['total_files']} file(s) ({summary['total_claims']} claims, "
        f"{summary['direct_claims']} #direct) -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
