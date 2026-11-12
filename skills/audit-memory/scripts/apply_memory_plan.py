#!/usr/bin/env python3
"""Transactional patcher, line-offset rebaser, and backup/restore manager for audit-memory."""

from __future__ import annotations

import argparse
import difflib
import errno
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any

from memory_common import (
    MARKDOWN_LINK_RE,
    find_direct_lines,
    is_within_root,
    parse_frontmatter,
    sha256_text,
)

DEFAULT_BACKUP_ROOT = Path.home() / ".cache" / "audit-memory" / "backups"
HEADING_RE = re.compile(r"^(#+)\s+(.*)$")


class PlanVerificationError(Exception):
    """Raised when root containment or post-write frontmatter verification fails."""


def _allocate_backup_dir(backup_root: Path) -> Path:
    """Creates and returns a unique `<YYYYMMDD-HHMMSS>` snapshot directory under `backup_root`."""
    backup_root.mkdir(parents=True, exist_ok=True)
    base_id = time.strftime("%Y%m%d-%H%M%S")
    candidate = backup_root / base_id
    suffix = 1
    while candidate.exists():
        candidate = backup_root / f"{base_id}-{suffix:02d}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def _prune_old_backups(backup_root: Path, max_backups: int) -> list[str]:
    """Removes oldest backup directories under `backup_root` exceeding `max_backups`."""
    if max_backups <= 0 or not backup_root.is_dir():
        return []
    snapshots = sorted(
        d
        for d in backup_root.iterdir()
        if d.is_dir() and (d / "manifest.json").is_file()
    )
    pruned: list[str] = []
    while len(snapshots) > max_backups:
        oldest = snapshots.pop(0)
        shutil.rmtree(oldest, ignore_errors=True)
        pruned.append(oldest.name)
    return pruned


def _add_if_relpath(rels: set[str], val: Any) -> None:
    """Adds `val` to `rels` if it is a non-empty string."""
    if isinstance(val, str) and val.strip():
        rels.add(val.strip())


def _collect_incoming_link_sources(
    plan: dict[str, Any],
    approved_ids: set[Any],
    retired_rels: set[str],
) -> set[str]:
    """Collects relative paths of files containing incoming links to retired files."""
    rels: set[str] = set()
    for rw in plan.get("link_rewrites") or []:
        if (
            rw.get("retired_finding_id") in approved_ids
            or rw.get("retired_file") in retired_rels
        ):
            _add_if_relpath(rels, rw.get("source_file"))
    files_map = plan.get("files") or {}
    for retired_rel in retired_rels:
        for link in files_map.get(retired_rel, {}).get("incoming_links") or []:
            _add_if_relpath(rels, link.get("source_file"))
    return rels


def _collect_snapshot_memory_rels(
    plan: dict[str, Any], approved_findings: list[dict[str, Any]]
) -> list[str]:
    """Collects sorted relative paths of all memory files touched by `approved_findings`."""
    rels: set[str] = set()
    approved_ids = {f.get("id") for f in approved_findings}
    retired_rels: set[str] = set()
    for finding in approved_findings:
        _add_if_relpath(rels, finding.get("file"))
        _add_if_relpath(rels, finding.get("direct_migration_target"))
        if finding.get("action") == "retire" and isinstance(
            finding.get("file"), str
        ):
            retired_rels.add(finding["file"])
    rels.update(_collect_incoming_link_sources(plan, approved_ids, retired_rels))
    return sorted(rels)


def _collect_snapshot_promote_targets(
    approved_findings: list[dict[str, Any]],
) -> list[Path]:
    """Collects unique resolved promotion target paths from `approved_findings`."""
    seen: set[str] = set()
    targets: list[Path] = []
    for finding in approved_findings:
        if finding.get("verdict") == "promote:script":
            continue
        raw = finding.get("promote_target")
        if not isinstance(raw, str) or not raw.strip():
            continue
        if not finding.get("promote_lines"):
            continue
        resolved = Path(raw).expanduser().resolve()
        key = str(resolved)
        if key not in seen:
            seen.add(key)
            targets.append(resolved)
    return targets


def _snapshot_single_file(
    abs_path: Path, backup_dir: Path, backup_relpath: str, kind: str
) -> dict[str, Any]:
    """Copies `abs_path` into `backup_dir / backup_relpath` and returns its manifest record."""
    dest = backup_dir / backup_relpath
    if abs_path.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, dest)
        text = abs_path.read_text(encoding="utf-8", errors="replace")
        return {
            "kind": kind,
            "path": str(abs_path),
            "backup_relpath": backup_relpath,
            "pre_apply_sha256": sha256_text(text),
            "existed_before": True,
        }
    return {
        "kind": kind,
        "path": str(abs_path),
        "backup_relpath": backup_relpath,
        "pre_apply_sha256": None,
        "existed_before": False,
    }


def create_backup_snapshot(
    plan: dict[str, Any],
    approved_findings: list[dict[str, Any]],
    backup_root: Path,
    max_backups: int = 20,
) -> Path:
    """Snapshots touched memory and promotion target files into `<backup_root>/<YYYYMMDD-HHMMSS>/`."""
    memory_root = Path(plan["memory_root"]).resolve()
    backup_dir = _allocate_backup_dir(backup_root.expanduser().resolve())
    manifest_files: list[dict[str, Any]] = []

    for rel in _collect_snapshot_memory_rels(plan, approved_findings):
        abs_mem = (memory_root / rel).resolve()
        manifest_files.append(
            _snapshot_single_file(abs_mem, backup_dir, f"memory/{rel}", "memory")
        )

    used_target_names: set[str] = set()
    for target_path in _collect_snapshot_promote_targets(approved_findings):
        name = target_path.name
        if name not in used_target_names:
            rel_dest = f"targets/{name}"
            used_target_names.add(name)
        else:
            rel_dest = f"targets/{target_path.parent.name}_{name}"
        manifest_files.append(
            _snapshot_single_file(
                target_path, backup_dir, rel_dest, "promotion_target"
            )
        )

    manifest = {
        "schema_version": 1,
        "backup_id": backup_dir.name,
        "memory_root": str(memory_root),
        "files": manifest_files,
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    _prune_old_backups(backup_dir.parent, max_backups)
    return backup_dir


def restore_backup_snapshot(backup_dir: Path) -> dict[str, Any]:
    """Restores all `memory` and `promotion_target` files recorded in `backup_dir/manifest.json`."""
    resolved_dir = backup_dir.expanduser().resolve()
    manifest_path = resolved_dir / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Backup manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored_paths: list[str] = []

    for entry in manifest.get("files") or []:
        dest_path = Path(entry["path"])
        src_path = resolved_dir / entry["backup_relpath"]
        if entry.get("existed_before", True):
            if not src_path.is_file():
                raise FileNotFoundError(
                    f"Backup payload missing for {dest_path}: {src_path}"
                )
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dest_path)
            restored_paths.append(str(dest_path))
        elif dest_path.exists():
            dest_path.unlink()
            restored_paths.append(str(dest_path))

    return {
        "restored": True,
        "backup_id": manifest.get("backup_id", resolved_dir.name),
        "backup_dir": str(resolved_dir),
        "restored_files": restored_paths,
    }


def _is_allowed_promote_target(
    raw_target: str, plan: dict[str, Any], skipped_mounts: list[Path]
) -> bool:
    """Whether `raw_target` resolves inside `plan['target_files']` or `plan['promotion_targets']`."""
    target_path = Path(raw_target).expanduser().resolve()
    target_files = plan.get("target_files") or {}
    if str(target_path) in target_files:
        return True
    promo_cfg = plan.get("promotion_targets") or {}
    for key in ("global_instructions", "workspace_instructions"):
        val = promo_cfg.get(key)
        if isinstance(val, str) and val.strip() and Path(val).resolve() == target_path:
            return True
    for raw_dir in promo_cfg.get("writable_skill_dirs") or []:
        if (
            isinstance(raw_dir, str)
            and raw_dir.strip()
            and target_path.name == "SKILL.md"
            and is_within_root(target_path, Path(raw_dir).resolve(), skipped_mounts)
        ):
            return True
    return False


def _verify_plan_containment(
    plan: dict[str, Any], approved_findings: list[dict[str, Any]]
) -> list[str]:
    """Verifies root containment on all memory and promotion target paths before applying."""
    memory_root = Path(plan["memory_root"]).resolve()
    skipped_mounts = [
        Path(p).resolve() for p in (plan.get("skipped_mounts") or [])
    ]
    errors: list[str] = []

    for finding in approved_findings:
        fid = finding.get("id", "unknown")
        for field in ("file", "direct_migration_target", "link_rewrite_target"):
            rel = finding.get(field)
            if rel is None:
                continue
            if (
                not isinstance(rel, str)
                or Path(rel).is_absolute()
                or not is_within_root(memory_root / rel, memory_root, skipped_mounts)
            ):
                errors.append(
                    f"[{fid}] root-containment: {field} {rel!r} resolves outside memory_root."
                )
        raw_target = finding.get("promote_target")
        if (
            raw_target is not None
            and finding.get("verdict") != "promote:script"
            and not _is_allowed_promote_target(str(raw_target), plan, skipped_mounts)
        ):
            errors.append(
                f"[{fid}] promotion-target-allowlist: {raw_target!r} is outside allowed targets."
            )
    return errors


def _rebase_span_via_opcodes(
    start_line: int,
    end_line: int,
    action: str,
    opcodes: list[tuple[str, int, int, int, int]],
) -> tuple[int, int] | None:
    """Rebases 1-based `start_line..end_line` using `SequenceMatcher` opcodes, or returns `None` on conflict."""
    if action == "retire":
        if len(opcodes) == 1 and opcodes[0][0] == "equal":
            return start_line, end_line
        return None
    orig_start = start_line - 1
    orig_end = end_line
    for tag, i1, i2, j1, _ in opcodes:
        if tag == "equal" and i1 <= orig_start and orig_end <= i2:
            delta = j1 - i1
            return start_line + delta, end_line + delta
    return None


def _drop_retire_on_conflict(
    active: list[dict[str, Any]], conflicts: list[str]
) -> list[dict[str, Any]]:
    """Removes any `action='retire'` finding from `active` when coexisting findings conflicted."""
    if not conflicts:
        return active
    kept: list[dict[str, Any]] = []
    for item in active:
        if item.get("action") == "retire":
            fid = str(item.get("id"))
            if fid not in conflicts:
                conflicts.append(fid)
        else:
            kept.append(item)
    return kept


def _rebase_file_findings(
    rel_file: str,
    file_findings: list[dict[str, Any]],
    file_plan: dict[str, Any],
    current_text: str,
    current_lines: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Checks SHA-256 and rebases `file_findings` against `current_lines`, returning `(active, rebased_ids, conflicts)`."""
    recorded_sha = file_plan.get("sha256")
    orig_lines = list(file_plan.get("original_lines") or [])
    if sha256_text(current_text) == recorded_sha or current_lines == orig_lines:
        return [dict(f) for f in file_findings], [], []

    opcodes = difflib.SequenceMatcher(None, orig_lines, current_lines).get_opcodes()
    active: list[dict[str, Any]] = []
    rebased_ids: list[str] = []
    conflicts: list[str] = []

    for finding in file_findings:
        fid = str(finding.get("id", rel_file))
        orig_span = (int(finding["start_line"]), int(finding["end_line"]))
        rebased = _rebase_span_via_opcodes(
            orig_span[0], orig_span[1], str(finding["action"]), opcodes
        )
        if rebased is None:
            conflicts.append(fid)
            continue
        item = dict(finding)
        item["start_line"], item["end_line"] = rebased
        if rebased != orig_span:
            rebased_ids.append(fid)
        active.append(item)

    return _drop_retire_on_conflict(active, conflicts), rebased_ids, conflicts


def _resolve_active_findings(
    plan: dict[str, Any], approved_findings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, list[str]], list[str], list[str]]:
    """Loads current disk lines and rebases or skips conflicting findings."""
    memory_root = Path(plan["memory_root"]).resolve()
    files_plan = plan.get("files") or {}
    by_file: dict[str, list[dict[str, Any]]] = {}
    for finding in approved_findings:
        by_file.setdefault(str(finding["file"]), []).append(finding)

    current_lines_map: dict[str, list[str]] = {}
    active_all: list[dict[str, Any]] = []
    rebased_all: list[str] = []
    conflicts_all: list[str] = []

    for rel_file, f_list in by_file.items():
        disk_path = (memory_root / rel_file).resolve()
        if not disk_path.is_file():
            conflicts_all.extend(str(f.get("id", rel_file)) for f in f_list)
            continue
        text = disk_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        current_lines_map[rel_file] = lines
        active, rebased, conflicts = _rebase_file_findings(
            rel_file, f_list, files_plan.get(rel_file, {}), text, lines
        )
        active_all.extend(active)
        rebased_all.extend(rebased)
        conflicts_all.extend(conflicts)

    return active_all, current_lines_map, rebased_all, conflicts_all


def _extract_finding_migration_lines(
    finding: dict[str, Any],
    src_lines: list[str],
    routed_lines: set[int],
) -> list[str]:
    """Extracts `#direct` claim lines from `src_lines` for a single migration finding."""
    action = finding.get("action")
    if action == "delete":
        start = int(finding["start_line"])
        end = int(finding["end_line"])
        return src_lines[start - 1 : end]
    if action == "retire":
        direct_nums = find_direct_lines(src_lines)
        return [src_lines[ln - 1] for ln in direct_nums if ln not in routed_lines]
    return []


def _collect_direct_migrations(
    active_findings: list[dict[str, Any]],
    current_lines_map: dict[str, list[str]],
) -> dict[str, list[str]]:
    """Extracts `#direct` claim lines to append to each `direct_migration_target`."""
    routed_by_file: dict[str, set[int]] = {}
    for finding in active_findings:
        if finding.get("action") == "delete":
            rel = str(finding["file"])
            routed_by_file.setdefault(rel, set()).update(
                range(int(finding["start_line"]), int(finding["end_line"]) + 1)
            )

    migrations: dict[str, list[str]] = {}
    for finding in active_findings:
        target = finding.get("direct_migration_target")
        if not isinstance(target, str) or not target.strip():
            continue
        rel = str(finding["file"])
        extracted = _extract_finding_migration_lines(
            finding,
            current_lines_map.get(rel, []),
            routed_by_file.get(rel, set()),
        )
        dest_list = migrations.setdefault(target, [])
        for line in extracted:
            if line not in dest_list:
                dest_list.append(line)
    return migrations


def _rewrite_link_match(
    match: re.Match[str],
    source_dir: Path,
    resolved_map: dict[Path, str],
) -> tuple[str, bool]:
    """Rewrites one regex match if its relative `.md` target is in `resolved_map`."""
    full = match.group(0)
    raw_target = match.group(1).strip()
    if raw_target.startswith(("#", "/")):
        return full, False
    path_part, sep, frag = raw_target.partition("#")
    path_part = path_part.split("?", 1)[0].strip()
    if not path_part.endswith(".md"):
        return full, False
    resolved = (source_dir / path_part).resolve()
    if resolved not in resolved_map:
        return full, False
    new_rel_url = resolved_map[resolved]
    new_url = f"{new_rel_url}#{frag}" if sep else new_rel_url
    bracket_end = full.rfind("](")
    return f"{full[: bracket_end + 2]}{new_url})", True


def _rewrite_links_in_lines(
    lines: list[str],
    source_relpath: str,
    retired_targets: dict[str, str],
    memory_root: Path,
) -> tuple[list[str], bool]:
    """Rewrites relative Markdown links in `lines` pointing to any retired file in `retired_targets`."""
    if not retired_targets:
        return lines, False
    source_dir = (memory_root / source_relpath).parent
    resolved_map = {
        (memory_root / old_rel).resolve(): os.path.relpath(
            (memory_root / new_rel).resolve(), source_dir
        ).replace(os.sep, "/")
        for old_rel, new_rel in retired_targets.items()
    }
    changed = False
    out_lines: list[str] = []

    for line in lines:
        def _sub(match: re.Match[str]) -> str:
            nonlocal changed
            new_str, did_change = _rewrite_link_match(
                match, source_dir, resolved_map
            )
            if did_change:
                changed = True
            return new_str

        out_lines.append(MARKDOWN_LINK_RE.sub(_sub, line))
    return out_lines, changed


def _apply_memory_edits_to_lines(
    base_lines: list[str],
    file_edits: list[dict[str, Any]],
    migrated_lines: list[str],
) -> list[str]:
    """Applies bottom-up span replacements/deletions and appends migrated `#direct` lines."""
    lines = list(base_lines)
    ordered = sorted(
        (f for f in file_edits if f.get("action") in ("update", "delete")),
        key=lambda e: int(e["start_line"]),
        reverse=True,
    )
    for edit in ordered:
        start = int(edit["start_line"])
        end = int(edit["end_line"])
        replacement = (
            [str(x) for x in (edit.get("replacement_lines") or [])]
            if edit.get("action") == "update"
            else []
        )
        lines[start - 1 : end] = replacement

    for m_line in migrated_lines:
        if m_line not in lines:
            lines.append(m_line)
    return lines


def _verify_written_frontmatter(
    disk_path: Path, expected_keys: set[str]
) -> None:
    """Re-reads `disk_path` and verifies `parse_frontmatter()` validity and key survival."""
    written_lines = disk_path.read_text(
        encoding="utf-8", errors="replace"
    ).splitlines()
    fm = parse_frontmatter(written_lines)
    if not fm.valid:
        raise PlanVerificationError(
            f"Frontmatter verification failed for {disk_path}: {fm.error}"
        )
    missing = sorted(expected_keys - set(fm.keys.keys()))
    if missing:
        raise PlanVerificationError(
            f"Frontmatter verification failed for {disk_path}: lost keys {missing}"
        )


def _trim_trailing_blanks(
    lines: list[str], insert_at: int, min_idx: int
) -> int:
    """Steps `insert_at` backward past trailing blank lines above `min_idx`."""
    while insert_at > min_idx and not lines[insert_at - 1].strip():
        insert_at -= 1
    return insert_at


def _find_section_insert_index(lines: list[str], section: str | None) -> int:
    """Finds the 0-based line index at the end of `section` in `lines`."""
    if not isinstance(section, str) or not section.strip():
        return len(lines)
    clean = section.strip()
    clean_no_hash = clean.lstrip("#").strip()
    sec_start = -1
    sec_level = 0
    for idx, line in enumerate(lines):
        stripped = line.strip()
        h_match = HEADING_RE.match(stripped)
        if not h_match:
            continue
        level = len(h_match.group(1))
        title = h_match.group(2).strip()
        if sec_start < 0 and (stripped == clean or title == clean_no_hash):
            sec_start = idx
            sec_level = level
        elif sec_start >= 0 and level <= sec_level:
            return _trim_trailing_blanks(lines, idx, sec_start + 1)

    if sec_start < 0:
        return len(lines)
    return _trim_trailing_blanks(lines, len(lines), sec_start + 1)


def _group_deduped_promotions(
    active_findings: list[dict[str, Any]],
) -> dict[Path, list[tuple[str | None, list[str]]]]:
    """Groups unique `(promote_section, promote_lines)` tuples by resolved `promote_target`."""
    seen_sigs: set[tuple[str, str, tuple[str, ...]]] = set()
    by_target: dict[Path, list[tuple[str | None, list[str]]]] = {}
    for finding in active_findings:
        if finding.get("verdict") == "promote:script":
            continue
        raw_target = finding.get("promote_target")
        promo_lines = [
            str(x) for x in (finding.get("promote_lines") or []) if str(x).strip()
        ]
        if not isinstance(raw_target, str) or not raw_target.strip() or not promo_lines:
            continue
        target_path = Path(raw_target).expanduser().resolve()
        sec = finding.get("promote_section")
        sig = (str(target_path), str(sec or "").strip(), tuple(promo_lines))
        if sig not in seen_sigs:
            seen_sigs.add(sig)
            by_target.setdefault(target_path, []).append((sec, promo_lines))
    return by_target


def _apply_promotions(
    active_findings: list[dict[str, Any]], dry_run: bool
) -> list[str]:
    """Appends deduplicated `promote_lines` under `promote_section` in each `promote_target`."""
    by_target = _group_deduped_promotions(active_findings)
    modified_targets: list[str] = []
    for target_path, entries in by_target.items():
        lines = target_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        for sec, promo_lines in entries:
            new_lines = [l for l in promo_lines if l not in lines]
            if new_lines:
                idx = _find_section_insert_index(lines, sec)
                lines[idx:idx] = new_lines
        if not dry_run:
            target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        modified_targets.append(str(target_path))
    return modified_targets


def _read_base_memory_lines(
    rel: str,
    current_lines_map: dict[str, list[str]],
    memory_root: Path,
) -> list[str] | None:
    """Returns current lines for `rel` from `current_lines_map` or disk."""
    if rel in current_lines_map:
        return current_lines_map[rel]
    disk_path = (memory_root / rel).resolve()
    if disk_path.is_file():
        return disk_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    return None


def _build_surviving_memory_updates(
    plan: dict[str, Any],
    active_findings: list[dict[str, Any]],
    current_lines_map: dict[str, list[str]],
) -> tuple[dict[str, list[str]], list[str]]:
    """Computes final line lists for surviving modified memory files and list of retired files."""
    memory_root = Path(plan["memory_root"]).resolve()
    retired_files = sorted({
        str(f["file"])
        for f in active_findings
        if f.get("action") == "retire"
    })
    retired_set = set(retired_files)
    retired_link_targets = {
        str(f["file"]): str(f["link_rewrite_target"])
        for f in active_findings
        if f.get("action") == "retire"
        and isinstance(f.get("link_rewrite_target"), str)
        and f["link_rewrite_target"].strip()
    }
    migrations = _collect_direct_migrations(active_findings, current_lines_map)
    candidate_rels = (
        set(current_lines_map.keys())
        | set(migrations.keys())
        | {str(k) for k in (plan.get("files") or {}).keys()}
    ) - retired_set

    edits_by_file: dict[str, list[dict[str, Any]]] = {}
    for finding in active_findings:
        rel = str(finding["file"])
        if rel not in retired_set:
            edits_by_file.setdefault(rel, []).append(finding)

    updated_files: dict[str, list[str]] = {}
    for rel in sorted(candidate_rels):
        base_lines = _read_base_memory_lines(rel, current_lines_map, memory_root)
        if base_lines is None:
            continue
        file_edits = edits_by_file.get(rel, [])
        mig_lines = migrations.get(rel, [])
        patched = _apply_memory_edits_to_lines(base_lines, file_edits, mig_lines)
        rewritten, link_changed = _rewrite_links_in_lines(
            patched, rel, retired_link_targets, memory_root
        )
        if file_edits or mig_lines or link_changed:
            updated_files[rel] = rewritten

    return updated_files, retired_files


def _execute_disk_transaction(
    plan: dict[str, Any],
    active_findings: list[dict[str, Any]],
    updated_memory_files: dict[str, list[str]],
    retired_files: list[str],
    dry_run: bool,
) -> list[str]:
    """Writes promotions, memory updates (with post-write frontmatter checks), and unlinks retired files."""
    memory_root = Path(plan["memory_root"]).resolve()
    files_plan = plan.get("files") or {}
    promoted_targets = _apply_promotions(active_findings, dry_run=dry_run)

    if dry_run:
        return promoted_targets

    for rel, new_lines in updated_memory_files.items():
        disk_path = (memory_root / rel).resolve()
        content = "\n".join(new_lines) + "\n" if new_lines else ""
        disk_path.write_text(content, encoding="utf-8")
        expected_keys = set(
            (files_plan.get(rel, {}).get("frontmatter_keys") or {}).keys()
        )
        _verify_written_frontmatter(disk_path, expected_keys)

    for rel in retired_files:
        if files_plan.get(rel, {}).get("protected_file"):
            raise PlanVerificationError(
                f"Refusing to unlink protected file: {rel}"
            )
        disk_path = (memory_root / rel).resolve()
        if disk_path.is_file():
            disk_path.unlink()

    return promoted_targets


def _format_rollback_result(
    exc: Exception,
    backup_dir: Path | None,
    dry_run: bool,
    rebased_ids: list[str],
    skipped_conflicts: list[str],
) -> dict[str, Any]:
    """Restores `backup_dir` if present and builds the `rolled_back` response payload."""
    if backup_dir is not None:
        restore_backup_snapshot(backup_dir)
    is_erofs = isinstance(exc, OSError) and exc.errno == errno.EROFS
    err_msg = (
        f"mfs is temporarily read-only (EROFS): {exc}" if is_erofs else str(exc)
    )
    return {
        "status": "rolled_back",
        "ok": False,
        "rolled_back": True,
        "dry_run": dry_run,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "backup_id": backup_dir.name if backup_dir else None,
        "applied_ids": [],
        "rebased_ids": rebased_ids,
        "skipped_conflicts": skipped_conflicts,
        "modified_memory_files": [],
        "retired_files": [],
        "promoted_targets": [],
        "errors": [err_msg],
    }


def apply_verified_plan(
    plan: dict[str, Any],
    approve_ids: set[str] | None,
    backup_root: Path,
    max_backups: int = 20,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Applies approved findings with pre-write rebasing, two-legged backup, and automatic rollback."""
    all_findings = list(plan.get("findings") or [])
    approved_set = set(approve_ids) if approve_ids is not None else None
    approved_findings = [
        f
        for f in all_findings
        if approved_set is None or str(f.get("id")) in approved_set
    ]

    containment_errors = _verify_plan_containment(plan, approved_findings)
    if containment_errors:
        return {
            "status": "error",
            "ok": False,
            "rolled_back": False,
            "dry_run": dry_run,
            "backup_dir": None,
            "backup_id": None,
            "applied_ids": [],
            "rebased_ids": [],
            "skipped_conflicts": [],
            "modified_memory_files": [],
            "retired_files": [],
            "promoted_targets": [],
            "errors": containment_errors,
        }

    active_findings, current_lines_map, rebased_ids, skipped_conflicts = (
        _resolve_active_findings(plan, approved_findings)
    )
    updated_memory_files, retired_files = _build_surviving_memory_updates(
        plan, active_findings, current_lines_map
    )

    backup_dir: Path | None = None
    if not dry_run and active_findings:
        backup_dir = create_backup_snapshot(
            plan, active_findings, backup_root, max_backups=max_backups
        )

    try:
        promoted_targets = _execute_disk_transaction(
            plan,
            active_findings,
            updated_memory_files,
            retired_files,
            dry_run=dry_run,
        )
    except (OSError, PlanVerificationError) as exc:
        return _format_rollback_result(
            exc, backup_dir, dry_run, rebased_ids, skipped_conflicts
        )

    return {
        "status": "dry_run" if dry_run else "ok",
        "ok": True,
        "rolled_back": False,
        "dry_run": dry_run,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "backup_id": backup_dir.name if backup_dir else None,
        "applied_ids": [str(f.get("id")) for f in active_findings],
        "rebased_ids": rebased_ids,
        "skipped_conflicts": skipped_conflicts,
        "modified_memory_files": sorted(updated_memory_files.keys()),
        "retired_files": retired_files,
        "promoted_targets": promoted_targets,
        "errors": [],
    }


def _list_backups(backup_root: Path) -> list[dict[str, Any]]:
    """Lists available backup snapshots under `backup_root` newest-first."""
    resolved = backup_root.expanduser().resolve()
    if not resolved.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for sub in sorted(resolved.iterdir(), reverse=True):
        manifest_path = sub / "manifest.json"
        if not sub.is_dir() or not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        items.append({
            "backup_id": manifest.get("backup_id", sub.name),
            "path": str(sub),
            "file_count": len(manifest.get("files") or []),
        })
    return items


def _resolve_restore_dir(raw_spec: str, backup_root: Path) -> Path:
    """Resolves a backup ID or directory path against `backup_root`."""
    cand = Path(raw_spec).expanduser()
    if cand.is_dir() and (cand / "manifest.json").is_file():
        return cand.resolve()
    under_root = (backup_root.expanduser().resolve() / raw_spec).resolve()
    if under_root.is_dir() and (under_root / "manifest.json").is_file():
        return under_root
    raise FileNotFoundError(
        f"Backup snapshot {raw_spec!r} not found under {backup_root}."
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments for `apply_memory_plan.py`."""
    parser = argparse.ArgumentParser(
        description="Apply verified_plan.json with snapshots, rebasing, and rollback."
    )
    parser.add_argument("--plan", help="Path to verified_plan.json.")
    parser.add_argument(
        "--approve-ids",
        help="Comma-separated finding IDs to apply.",
    )
    parser.add_argument(
        "--approve-all",
        action="store_true",
        help="Approve and apply all findings in verified_plan.json.",
    )
    parser.add_argument(
        "--backup-root",
        default=str(DEFAULT_BACKUP_ROOT),
        help="Directory for backup snapshots (default: ~/.cache/audit-memory/backups).",
    )
    parser.add_argument(
        "--max-backups",
        type=int,
        default=20,
        help="Maximum snapshots to retain (default: 20).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate applying findings without modifying disk.",
    )
    parser.add_argument(
        "--list-backups",
        action="store_true",
        help="List available backup snapshots and exit.",
    )
    parser.add_argument(
        "--restore",
        help="Restore a backup snapshot by ID or directory path.",
    )
    return parser.parse_args(argv)


def _handle_list_or_restore(
    args: argparse.Namespace, backup_root: Path
) -> int | None:
    """Handles `--list-backups` and `--restore` CLI modes if requested."""
    if args.list_backups:
        backups = _list_backups(backup_root)
        print(json.dumps({"backups": backups}, indent=2))
        return 0
    if args.restore:
        try:
            target_dir = _resolve_restore_dir(args.restore, backup_root)
            res = restore_backup_snapshot(target_dir)
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(res, indent=2))
        return 0
    return None


def _parse_approved_ids_arg(args: argparse.Namespace) -> set[str] | None:
    """Parses `--approve-all` and `--approve-ids` into a set of IDs or `None`."""
    if args.approve_all:
        return None
    if not args.approve_ids:
        return set()
    return {item.strip() for item in args.approve_ids.split(",") if item.strip()}


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `apply_memory_plan.py`."""
    args = _parse_args(argv)
    backup_root = Path(args.backup_root).expanduser().resolve()

    early_rc = _handle_list_or_restore(args, backup_root)
    if early_rc is not None:
        return early_rc

    if not args.plan:
        print(
            "Error: --plan is required unless --list-backups or --restore is passed.",
            file=sys.stderr,
        )
        return 2

    plan_path = Path(args.plan).expanduser().resolve()
    if not plan_path.is_file():
        print(f"Error: plan file not found: {plan_path}", file=sys.stderr)
        return 1

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    result = apply_verified_plan(
        plan=plan,
        approve_ids=_parse_approved_ids_arg(args),
        backup_root=backup_root,
        max_backups=args.max_backups,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    if not result.get("ok", False):
        for err in result.get("errors") or []:
            print(f"APPLY ERROR: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
