#!/usr/bin/env python3
"""Deterministic 7-rule plan linter for audit-memory findings."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

from memory_common import (
    DIRECT_TAG_RE,
    MARKDOWN_LINK_RE,
    find_direct_lines,
    is_within_root,
    parse_frontmatter,
    sha256_text,
)

VALID_ACTIONS: frozenset[str] = frozenset({"update", "delete", "retire"})
PROMOTE_APPEND_VERDICTS: frozenset[str] = frozenset({
    "promote:agents_md",
    "promote:skill",
})
INSTRUCTION_FILENAMES: frozenset[str] = frozenset({
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "agents.md",
    "SKILL.md",
})

META_TARGET_PREFIX_RE = re.compile(
    r"^\s*(?:(?:[-*+]|\d+\.)\s+)?"
    r"(?:update|remove|delete|change|replace|fix|edit|drop|omit|keep|retain|"
    r"move|merge|consolidate|rewrite)\s+"
    r"(?:line|lines|bullet|bullets|claim|claims|section|sections|entry|entries|"
    r"file|record|this\b|"
    r"the\s+(?:claim|claims|bullet|bullets|line|lines|section|sections|"
    r"entry|entries|following|above|below|text|path|reference|mention|"
    r"statement|sentence|paragraph|note))\b",
    re.IGNORECASE,
)
META_LINE_NUMBER_RE = re.compile(
    r"^\s*(?:(?:[-*+]|\d+\.)\s+)?line(?:s)?\s+\d+(?:\s*(?:[-–..]|to)\s*\d+)?\s*:",
    re.IGNORECASE,
)
META_CHANGE_TO_RE = re.compile(
    r"^\s*(?:(?:[-*+]|\d+\.)\s+)?(?:change|replace)\s+\S.*?\s+(?:to|with)\s+\S",
    re.IGNORECASE,
)
UNBULLETED_IMPERATIVE_RE = re.compile(
    r"^\s*(?:update|remove|delete|change|replace|fix|edit|drop|omit|keep|"
    r"retain|move|merge|consolidate|rewrite)(?:\s|$|:)",
    re.IGNORECASE,
)
LIST_BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d+\.)\s+\S")
INDENTED_BULLET_RE = re.compile(r"^(\s+)(?:[-*+]|\d+\.)\s+\S")
DESCRIPTION_LINE_RE = re.compile(r"^\s*description\s*:\s*(.*)$")


def _finding_label(finding: dict[str, Any], index: int) -> str:
    """Returns a stable identifier label for error messages."""
    raw_id = finding.get("id")
    if isinstance(raw_id, str) and raw_id.strip():
        return raw_id.strip()
    return f"finding-{index + 1}"


def _is_valid_memory_relpath(
    relpath: Any,
    memory_root: Path,
    skipped_mounts: list[Path],
    files_map: dict[str, Any],
) -> bool:
    """Whether `relpath` is a relative path inside `memory_root` and present in `files_map`."""
    if not isinstance(relpath, str) or not relpath.strip():
        return False
    if Path(relpath).is_absolute() or relpath not in files_map:
        return False
    return is_within_root(memory_root / relpath, memory_root, skipped_mounts)


def _check_root_containment(
    finding: dict[str, Any],
    label: str,
    memory_root: Path,
    skipped_mounts: list[Path],
    files_map: dict[str, Any],
    retired_files: set[str],
) -> list[str]:
    """Rule 1 (`root-containment`): checks `action`, `file`, and migration/link targets."""
    errors: list[str] = []
    action = finding.get("action")
    if action not in VALID_ACTIONS:
        errors.append(
            f"[{label}] root-containment: invalid action {action!r}; "
            f"expected one of {sorted(VALID_ACTIONS)}."
        )
    rel_file = finding.get("file")
    if not _is_valid_memory_relpath(
        rel_file, memory_root, skipped_mounts, files_map
    ):
        errors.append(
            f"[{label}] root-containment: file {rel_file!r} is outside memory_root "
            "or missing from inventory."
        )
    for field in ("direct_migration_target", "link_rewrite_target"):
        target = finding.get(field)
        if target is None:
            continue
        if not _is_valid_memory_relpath(
            target, memory_root, skipped_mounts, files_map
        ):
            errors.append(
                f"[{label}] root-containment: {field} {target!r} is outside "
                "memory_root or missing from inventory."
            )
        elif target in retired_files:
            errors.append(
                f"[{label}] root-containment: {field} {target!r} cannot target "
                "a file marked action='retire' in the same plan."
            )
    return errors


def _is_positive_int(val: Any) -> bool:
    """Whether `val` is a positive integer (excluding booleans)."""
    return isinstance(val, int) and not isinstance(val, bool) and val >= 1


def _check_single_line_range(
    finding: dict[str, Any], label: str, file_meta: dict[str, Any]
) -> list[str]:
    """Validates `1 <= start_line <= end_line <= line_count` for a single finding."""
    start = finding.get("start_line")
    end = finding.get("end_line")
    line_count = int(
        file_meta.get("line_count", len(file_meta.get("original_lines", [])))
    )
    if not (_is_positive_int(start) and _is_positive_int(end)):
        return [
            f"[{label}] line-range-valid: start_line ({start!r}) and "
            f"end_line ({end!r}) must be positive integers."
        ]
    if start > end or end > line_count:
        return [
            f"[{label}] line-range-valid: span {start}..{end} out of bounds "
            f"for {finding.get('file')} (1..{line_count})."
        ]
    if finding.get("action") == "retire" and (start != 1 or end != line_count):
        return [
            f"[{label}] line-range-valid: action='retire' must cover full file "
            f"1..{line_count}, got {start}..{end}."
        ]
    return []


def _check_excerpt_match(
    finding: dict[str, Any], label: str, file_meta: dict[str, Any]
) -> list[str]:
    """Rule 3 (`excerpt-match`): checks `expected_excerpt` is a substring of `start_line..end_line`."""
    excerpt = finding.get("expected_excerpt")
    if not isinstance(excerpt, str) or not excerpt.strip():
        return [
            f"[{label}] excerpt-match: expected_excerpt must be a non-empty string."
        ]
    start = int(finding["start_line"])
    end = int(finding["end_line"])
    orig_lines = file_meta.get("original_lines", [])
    span_text = "\n".join(orig_lines[start - 1 : end])
    if excerpt not in span_text:
        return [
            f"[{label}] excerpt-match: expected_excerpt {excerpt!r} not found "
            f"in {finding.get('file')}:{start}..{end}."
        ]
    return []


def _is_meta_instruction_line(line: str) -> bool:
    """Whether a replacement or promoted line matches a meta-instruction pattern."""
    if not line.strip():
        return False
    if META_TARGET_PREFIX_RE.search(line):
        return True
    if META_LINE_NUMBER_RE.search(line):
        return True
    if META_CHANGE_TO_RE.search(line):
        return True
    return bool(UNBULLETED_IMPERATIVE_RE.search(line))


def _check_lines_for_meta(
    field_name: str, lines: list[Any], label: str
) -> list[str]:
    """Checks each line in `lines` against imperative meta-instruction patterns."""
    errors: list[str] = []
    for line in lines:
        if not isinstance(line, str) or _is_meta_instruction_line(line):
            errors.append(
                f"[{label}] no-meta-instruction: {field_name} contains "
                f"imperative instruction text: {line!r}."
            )
    return errors


def _check_bullet_preservation(
    finding: dict[str, Any], label: str, file_meta: dict[str, Any]
) -> list[str]:
    """Checks that replacing a list bullet preserves bullet structure and indentation."""
    rep_lines = finding.get("replacement_lines") or []
    if finding.get("action") != "update" or not rep_lines:
        return []
    first_rep = rep_lines[0]
    if not isinstance(first_rep, str):
        return []
    orig_lines = file_meta.get("original_lines", [])
    start_idx = int(finding["start_line"]) - 1
    orig_first = orig_lines[start_idx] if 0 <= start_idx < len(orig_lines) else ""
    if INDENTED_BULLET_RE.match(orig_first) and not INDENTED_BULLET_RE.match(
        first_rep
    ):
        return [
            f"[{label}] no-meta-instruction: replacement_lines drops indentation "
            f"or bullet marker for indented bullet {orig_first!r}."
        ]
    if LIST_BULLET_RE.match(orig_first) and not LIST_BULLET_RE.match(first_rep):
        return [
            f"[{label}] no-meta-instruction: replacement_lines replaces list "
            f"bullet {orig_first!r} with non-bullet prose."
        ]
    return []


def _check_no_meta_instruction(
    finding: dict[str, Any], label: str, file_meta: dict[str, Any]
) -> list[str]:
    """Rule 4 (`no-meta-instruction`): rejects imperative instruction text and lost bullet indentation."""
    errors: list[str] = []
    rep_lines = finding.get("replacement_lines") or []
    promo_lines = finding.get("promote_lines") or []
    if finding.get("action") == "update" and not rep_lines:
        errors.append(
            f"[{label}] no-meta-instruction: action='update' requires non-empty replacement_lines."
        )
    errors.extend(_check_lines_for_meta("replacement_lines", rep_lines, label))
    errors.extend(_check_lines_for_meta("promote_lines", promo_lines, label))
    errors.extend(_check_bullet_preservation(finding, label, file_meta))
    return errors


def _has_replacement_direct(finding: dict[str, Any]) -> bool:
    """Whether `finding` is an update whose `replacement_lines` retain `#direct`."""
    if finding.get("action") != "update":
        return False
    rep_lines = finding.get("replacement_lines") or []
    return any(isinstance(l, str) and DIRECT_TAG_RE.search(l) for l in rep_lines)


def _has_valid_promotion(finding: dict[str, Any]) -> bool:
    """Whether `finding` specifies a promotion target with lines or `promote:script`."""
    target = finding.get("promote_target")
    if not isinstance(target, str) or not target.strip():
        return False
    if finding.get("verdict") == "promote:script":
        return True
    return bool(finding.get("promote_lines"))


def _has_valid_direct_migration(
    finding: dict[str, Any],
    retired_files: set[str],
    files_map: dict[str, Any],
) -> bool:
    """Whether `finding` names a surviving `direct_migration_target` in `files_map`."""
    mig = finding.get("direct_migration_target")
    if not isinstance(mig, str) or not mig.strip():
        return False
    return (
        mig in files_map
        and mig not in retired_files
        and mig != finding.get("file")
    )


def _finding_conserves_direct(
    finding: dict[str, Any],
    retired_files: set[str],
    files_map: dict[str, Any],
) -> bool:
    """Whether `finding` preserves, promotes, migrates, or explicitly drops `#direct`."""
    if finding.get("allow_drop_direct") is True:
        return True
    if _has_replacement_direct(finding) or _has_valid_promotion(finding):
        return True
    return _has_valid_direct_migration(finding, retired_files, files_map)


def _get_file_direct_lines(file_meta: dict[str, Any]) -> set[int]:
    """Returns the set of 1-based `#direct` line numbers for `file_meta`."""
    if "direct_lines" in file_meta and file_meta["direct_lines"] is not None:
        return {int(x) for x in file_meta["direct_lines"]}
    orig_lines = file_meta.get("original_lines", [])
    fm_end = int(file_meta.get("frontmatter_end_line", 0))
    return set(find_direct_lines(orig_lines, start_after_line=fm_end))


def _check_direct_and_pinned(
    finding: dict[str, Any],
    label: str,
    file_meta: dict[str, Any],
    routed_direct_lines: set[int],
    retired_files: set[str],
    files_map: dict[str, Any],
) -> list[str]:
    """Rule 5 (`direct-and-pinned-conservation`): protects pinned files and `#direct` claims."""
    action = finding.get("action")
    if action == "retire" and file_meta.get("protected_file"):
        return [
            f"[{label}] direct-and-pinned-conservation: cannot retire protected "
            f"or visibility:PINNED file {finding.get('file')!r}."
        ]
    direct_lines = _get_file_direct_lines(file_meta)
    if not direct_lines:
        return []
    if action == "retire":
        unrouted = sorted(ln for ln in direct_lines if ln not in routed_direct_lines)
        if unrouted and not _finding_conserves_direct(
            finding, retired_files, files_map
        ):
            return [
                f"[{label}] direct-and-pinned-conservation: retiring "
                f"{finding.get('file')!r} loses unrouted #direct lines {unrouted}."
            ]
        return []

    start = int(finding["start_line"])
    end = int(finding["end_line"])
    touched = sorted(ln for ln in direct_lines if start <= ln <= end)
    if touched and not _finding_conserves_direct(finding, retired_files, files_map):
        return [
            f"[{label}] direct-and-pinned-conservation: finding on "
            f"{finding.get('file')}:{start}..{end} drops #direct lines {touched} "
            "without replacement #direct, allow_drop_direct, promote_target, or direct_migration_target."
        ]
    return []


def _section_exists_in_file(target_path: Path, section: str) -> bool:
    """Whether `section` matches a Markdown heading in `target_path`."""
    try:
        lines = target_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    clean = section.strip()
    clean_no_hash = clean.lstrip("#").strip()
    for line in lines:
        stripped = line.strip()
        if stripped == clean:
            return True
        if stripped.startswith("#") and stripped.lstrip("#").strip() == clean_no_hash:
            return True
    return False


def _resolve_promote_path(raw_target: str, workspace: Path) -> Path:
    """Resolves `raw_target` against `workspace` when relative."""
    cand = Path(raw_target).expanduser()
    if cand.is_absolute():
        return cand.resolve()
    return (workspace / cand).resolve()


def _allowed_instruction_paths(promotion_targets: dict[str, Any]) -> set[Path]:
    """Returns resolved instruction paths from `promotion_targets`."""
    paths: set[Path] = set()
    for key in ("global_instructions", "workspace_instructions"):
        val = promotion_targets.get(key)
        if isinstance(val, str) and val.strip():
            paths.add(Path(val).resolve())
    return paths


def _is_allowed_skill_file(
    target_path: Path,
    promotion_targets: dict[str, Any],
    skipped_mounts: list[Path],
) -> bool:
    """Whether `target_path` is an existing `SKILL.md` inside `writable_skill_dirs`."""
    if target_path.name != "SKILL.md" or not target_path.is_file():
        return False
    raw_dirs = promotion_targets.get("writable_skill_dirs") or []
    for raw_dir in raw_dirs:
        if isinstance(raw_dir, str) and raw_dir.strip():
            if is_within_root(target_path, Path(raw_dir).resolve(), skipped_mounts):
                return True
    return False


def _validate_promote_path(
    verdict: str,
    target_path: Path,
    promotion_targets: dict[str, Any],
    workspace: Path,
    skipped_mounts: list[Path],
) -> str | None:
    """Checks whether `target_path` is permitted for `verdict` under `promotion_targets`."""
    allowed_instr = _allowed_instruction_paths(promotion_targets)
    if verdict == "promote:agents_md":
        if target_path in allowed_instr and target_path.is_file():
            return None
        return (
            f"promote_target {str(target_path)!r} does not match "
            "global_instructions or workspace_instructions."
        )
    if verdict == "promote:skill":
        if _is_allowed_skill_file(target_path, promotion_targets, skipped_mounts):
            return None
        return (
            f"promote_target {str(target_path)!r} must resolve to an existing "
            "SKILL.md inside writable_skill_dirs."
        )
    if verdict == "promote:script":
        if is_within_root(target_path, workspace, skipped_mounts):
            return None
        return (
            f"promote_target {str(target_path)!r} for promote:script "
            f"must resolve inside workspace {str(workspace)!r}."
        )
    if target_path in allowed_instr and target_path.is_file():
        return None
    if _is_allowed_skill_file(target_path, promotion_targets, skipped_mounts):
        return None
    return f"promote_target {str(target_path)!r} is not in the promotion_targets allowlist."


def _check_promote_content(
    finding: dict[str, Any], label: str, verdict: str, target_path: Path
) -> list[str]:
    """Validates `promote_lines` and `promote_section` for instruction and skill promotions."""
    needs_lines = verdict in PROMOTE_APPEND_VERDICTS or (
        verdict != "promote:script" and target_path.name in INSTRUCTION_FILENAMES
    )
    if not needs_lines:
        return []
    promo_lines = finding.get("promote_lines") or []
    has_non_empty = any(isinstance(l, str) and l.strip() for l in promo_lines)
    if not has_non_empty:
        return [
            f"[{label}] promotion-target-allowlist: {verdict or 'promotion'} "
            "requires non-empty promote_lines."
        ]
    section = finding.get("promote_section")
    if isinstance(section, str) and section.strip():
        if not _section_exists_in_file(target_path, section):
            return [
                f"[{label}] promotion-target-allowlist: promote_section "
                f"{section!r} not found in {str(target_path)!r}."
            ]
    return []


def _check_promotion_allowlist(
    finding: dict[str, Any],
    label: str,
    promotion_targets: dict[str, Any],
    workspace: Path,
    skipped_mounts: list[Path],
) -> list[str]:
    """Rule 6 (`promotion-target-allowlist`): validates `promote_target`, `promote_section`, and `promote_lines`."""
    verdict = str(finding.get("verdict") or "")
    raw_target = finding.get("promote_target")
    if not verdict.startswith("promote:") and raw_target is None:
        return []
    if not isinstance(raw_target, str) or not raw_target.strip():
        return [
            f"[{label}] promotion-target-allowlist: verdict {verdict!r} requires a non-empty promote_target."
        ]
    target_path = _resolve_promote_path(raw_target, workspace)
    path_err = _validate_promote_path(
        verdict, target_path, promotion_targets, workspace, skipped_mounts
    )
    if path_err:
        return [f"[{label}] promotion-target-allowlist: {path_err}"]
    return _check_promote_content(finding, label, verdict, target_path)


def _is_description_double_quoted(lines: list[str], end_line: int) -> bool:
    """Whether the `description:` line in `lines[1 : end_line - 1]` is a single-line double-quoted string."""
    for line in lines[1 : max(1, end_line - 1)]:
        match = DESCRIPTION_LINE_RE.match(line)
        if match:
            raw_val = match.group(1).strip()
            return len(raw_val) >= 2 and raw_val[0] == '"' and raw_val[-1] == '"'
    return True


def _check_frontmatter_safe(
    finding: dict[str, Any], label: str, file_meta: dict[str, Any]
) -> list[str]:
    """Rule 7 (`frontmatter-safe`): verifies edits inside `1..frontmatter_end_line` preserve YAML keys and double-quote `description`."""
    if finding.get("action") == "retire":
        return []
    orig_lines = list(file_meta.get("original_lines", []))
    fm_end = int(file_meta.get("frontmatter_end_line", 0))
    if fm_end <= 0:
        fm_end = parse_frontmatter(orig_lines).end_line
    start = int(finding["start_line"])
    if fm_end <= 0 or start > fm_end:
        return []

    end = int(finding["end_line"])
    replacement = (
        list(finding.get("replacement_lines") or [])
        if finding.get("action") == "update"
        else []
    )
    patched = orig_lines[: start - 1] + replacement + orig_lines[end:]
    new_fm = parse_frontmatter(patched)
    if not new_fm.valid:
        return [
            f"[{label}] frontmatter-safe: frontmatter invalid after edit: {new_fm.error}"
        ]
    old_keys = set((file_meta.get("frontmatter_keys") or {}).keys())
    missing_keys = sorted(old_keys - set(new_fm.keys.keys()))
    if missing_keys:
        return [
            f"[{label}] frontmatter-safe: edit drops existing frontmatter keys {missing_keys}."
        ]
    if "description" in new_fm.keys and not _is_description_double_quoted(
        patched, new_fm.end_line
    ):
        return [
            f"[{label}] frontmatter-safe: 'description:' in frontmatter must be a single-line double-quoted string."
        ]
    return []


def _collect_routed_direct_map(
    findings: list[dict[str, Any]],
    invalid_indices: set[int],
    retired_files: set[str],
    files_map: dict[str, Any],
) -> dict[str, set[int]]:
    """Collects line numbers covered by valid per-line `delete` routing records per file."""
    routed: dict[str, set[int]] = {}
    for idx, finding in enumerate(findings):
        if idx in invalid_indices or finding.get("action") != "delete":
            continue
        if not _finding_conserves_direct(finding, retired_files, files_map):
            continue
        rel_file = finding.get("file")
        start = finding.get("start_line")
        end = finding.get("end_line")
        if (
            isinstance(rel_file, str)
            and _is_positive_int(start)
            and _is_positive_int(end)
        ):
            routed.setdefault(rel_file, set()).update(
                range(int(start), int(end) + 1)
            )
    return routed


def _record_non_retire_span(
    rel_file: str,
    start: int,
    end: int,
    idx: int,
    label: str,
    action: str,
    per_file_spans: dict[str, list[tuple[int, int, int, str, str]]],
    invalid_indices: set[int],
) -> list[str]:
    """Checks a non-retire span against prior spans in `rel_file` and records it."""
    errors: list[str] = []
    for o_start, o_end, o_idx, o_label, _ in per_file_spans.get(rel_file, []):
        if start <= o_end and o_start <= end:
            errors.append(
                f"[{label}] line-range-valid: span {start}..{end} in {rel_file} "
                f"overlaps [{o_label}] ({o_start}..{o_end})."
            )
            invalid_indices.update({idx, o_idx})
    per_file_spans.setdefault(rel_file, []).append(
        (start, end, idx, label, action)
    )
    return errors


def _check_retire_coexistence(
    per_file_retire: dict[str, tuple[int, str]],
    per_file_spans: dict[str, list[tuple[int, int, int, str, str]]],
    invalid_indices: set[int],
) -> list[str]:
    """Ensures only `action='delete'` routing spans coexist with a whole-file `retire`."""
    errors: list[str] = []
    for rel_file, (ret_idx, ret_label) in per_file_retire.items():
        for start, end, s_idx, s_label, s_action in per_file_spans.get(
            rel_file, []
        ):
            if s_action != "delete":
                errors.append(
                    f"[{s_label}] line-range-valid: {s_action!r} on "
                    f"{rel_file}:{start}..{end} cannot coexist with whole-file "
                    f"retire [{ret_label}]; only action='delete' routing records may coexist."
                )
                invalid_indices.update({ret_idx, s_idx})
    return errors


def _check_single_rewrite_link(
    idx: int,
    label: str,
    src_file: str,
    ln: int,
    rewrite_target: str,
    per_file_spans: dict[str, list[tuple[int, int, int, str, str]]],
    synthetic_by_line: dict[tuple[str, int], tuple[int, str, str]],
    invalid_indices: set[int],
) -> list[str]:
    """Checks one incoming link rewrite on `(src_file, ln)` for collisions."""
    errors: list[str] = []
    for start, end, span_idx, span_label, _ in per_file_spans.get(src_file, []):
        if start <= ln <= end:
            errors.append(
                f"[{label}] line-range-valid: incoming link rewrite in "
                f"{src_file}:{ln} collides with [{span_label}] ({start}..{end})."
            )
            invalid_indices.update({idx, span_idx})
    key = (src_file, ln)
    if key not in synthetic_by_line:
        synthetic_by_line[key] = (idx, label, rewrite_target)
        return errors
    prev_idx, prev_label, prev_target = synthetic_by_line[key]
    if prev_target != rewrite_target:
        errors.append(
            f"[{label}] line-range-valid: incoming link rewrite on "
            f"{src_file}:{ln} collides with [{prev_label}]."
        )
        invalid_indices.update({idx, prev_idx})
    return errors


def _iter_retired_incoming_links(
    finding: dict[str, Any],
    files_map: dict[str, Any],
    retired_files: set[str],
) -> list[tuple[str, int, str]]:
    """Returns `(source_file, line_num, rewrite_target)` for a retired file's incoming links."""
    if finding.get("action") != "retire":
        return []
    rewrite_target = finding.get("link_rewrite_target")
    if not isinstance(rewrite_target, str) or not rewrite_target.strip():
        return []
    rel_file = str(finding["file"])
    incoming = files_map.get(rel_file, {}).get("incoming_links") or []
    results: list[tuple[str, int, str]] = []
    for link in incoming:
        src_file = link.get("source_file")
        line_num = link.get("line")
        if (
            isinstance(src_file, str)
            and src_file not in retired_files
            and src_file in files_map
            and _is_positive_int(line_num)
        ):
            results.append((src_file, int(line_num), rewrite_target))
    return results


def _check_link_rewrite_collisions(
    findings: list[dict[str, Any]],
    files_map: dict[str, Any],
    retired_files: set[str],
    per_file_spans: dict[str, list[tuple[int, int, int, str, str]]],
    invalid_indices: set[int],
) -> list[str]:
    """Checks synthetic line edits from `link_rewrite_target` against explicit finding spans."""
    errors: list[str] = []
    synthetic_by_line: dict[tuple[str, int], tuple[int, str, str]] = {}
    for idx, finding in enumerate(findings):
        if idx in invalid_indices:
            continue
        label = _finding_label(finding, idx)
        for src_file, ln, rewrite_target in _iter_retired_incoming_links(
            finding, files_map, retired_files
        ):
            errors.extend(
                _check_single_rewrite_link(
                    idx,
                    label,
                    src_file,
                    ln,
                    rewrite_target,
                    per_file_spans,
                    synthetic_by_line,
                    invalid_indices,
                )
            )
    return errors


def _check_cross_finding_spans(
    findings: list[dict[str, Any]],
    files_map: dict[str, Any],
    retired_files: set[str],
    invalid_indices: set[int],
) -> list[str]:
    """Rule 2 cross-finding check: detects overlapping line ranges and incoming link-rewrite collisions."""
    errors: list[str] = []
    per_file_retire: dict[str, tuple[int, str]] = {}
    per_file_spans: dict[str, list[tuple[int, int, int, str, str]]] = {}

    for idx, finding in enumerate(findings):
        if idx in invalid_indices:
            continue
        label = _finding_label(finding, idx)
        rel_file = str(finding["file"])
        action = str(finding["action"])
        if action == "retire":
            if rel_file in per_file_retire:
                prev_idx, prev_label = per_file_retire[rel_file]
                errors.append(
                    f"[{label}] line-range-valid: duplicate action='retire' on "
                    f"{rel_file} (collides with [{prev_label}])."
                )
                invalid_indices.update({idx, prev_idx})
            else:
                per_file_retire[rel_file] = (idx, label)
            continue
        errors.extend(
            _record_non_retire_span(
                rel_file,
                int(finding["start_line"]),
                int(finding["end_line"]),
                idx,
                label,
                action,
                per_file_spans,
                invalid_indices,
            )
        )

    errors.extend(
        _check_retire_coexistence(
            per_file_retire, per_file_spans, invalid_indices
        )
    )
    errors.extend(
        _check_link_rewrite_collisions(
            findings, files_map, retired_files, per_file_spans, invalid_indices
        )
    )
    return errors


def _deduplicate_promotions(
    valid_findings: list[dict[str, Any]], workspace: Path
) -> list[dict[str, Any]]:
    """Deduplicates identical `(promote_target, promote_section, tuple(promote_lines))` across findings."""
    seen: set[tuple[str, str, tuple[str, ...]]] = set()
    out: list[dict[str, Any]] = []
    for finding in valid_findings:
        item = dict(finding)
        raw_target = item.get("promote_target")
        promo_lines = [str(x) for x in (item.get("promote_lines") or [])]
        if isinstance(raw_target, str) and raw_target.strip() and promo_lines:
            resolved = str(_resolve_promote_path(raw_target, workspace))
            section = str(item.get("promote_section") or "").strip()
            sig = (resolved, section, tuple(promo_lines))
            if sig in seen:
                item["promote_lines"] = []
                item["promotion_deduplicated"] = True
            else:
                seen.add(sig)
        out.append(item)
    return out


def _lint_single_finding(
    finding: dict[str, Any],
    label: str,
    memory_root: Path,
    skipped_mounts: list[Path],
    files_map: dict[str, Any],
    retired_files: set[str],
    promotion_targets: dict[str, Any],
    workspace: Path,
) -> list[str]:
    """Runs per-finding validation rules 1, 2 (bounds), 3, 4, 6, and 7."""
    root_errors = _check_root_containment(
        finding, label, memory_root, skipped_mounts, files_map, retired_files
    )
    if root_errors:
        return root_errors
    file_meta = files_map[finding["file"]]
    range_errors = _check_single_line_range(finding, label, file_meta)
    if range_errors:
        return range_errors
    errors: list[str] = []
    errors.extend(_check_excerpt_match(finding, label, file_meta))
    errors.extend(_check_no_meta_instruction(finding, label, file_meta))
    errors.extend(
        _check_promotion_allowlist(
            finding, label, promotion_targets, workspace, skipped_mounts
        )
    )
    errors.extend(_check_frontmatter_safe(finding, label, file_meta))
    return errors


def _run_direct_conservation_pass(
    findings: list[dict[str, Any]],
    invalid_indices: set[int],
    files_map: dict[str, Any],
) -> list[str]:
    """Runs Rule 5 (`direct-and-pinned-conservation`) across all remaining valid findings."""
    active_retired = {
        str(findings[idx]["file"])
        for idx in range(len(findings))
        if idx not in invalid_indices and findings[idx].get("action") == "retire"
    }
    routed_direct_map = _collect_routed_direct_map(
        findings, invalid_indices, active_retired, files_map
    )
    errors: list[str] = []
    for idx, finding in enumerate(findings):
        if idx in invalid_indices:
            continue
        rel_file = str(finding["file"])
        direct_errors = _check_direct_and_pinned(
            finding,
            _finding_label(finding, idx),
            files_map[rel_file],
            routed_direct_map.get(rel_file, set()),
            active_retired,
            files_map,
        )
        if direct_errors:
            errors.extend(direct_errors)
            invalid_indices.add(idx)
    return errors


def lint_findings(
    inventory: dict[str, Any],
    findings: list[dict[str, Any]],
    workspace: Path | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Validates `findings` against the 7 deterministic rules and returns `(errors, valid_findings)`."""
    eff_workspace = (workspace or Path.cwd()).resolve()
    memory_root = Path(inventory["memory_root"]).resolve()
    skipped_mounts = [
        Path(p).resolve() for p in (inventory.get("skipped_mounts") or [])
    ]
    files_map: dict[str, Any] = inventory.get("files") or {}
    promotion_targets: dict[str, Any] = inventory.get("promotion_targets") or {}
    retired_files = {
        str(f.get("file"))
        for f in findings
        if f.get("action") == "retire" and isinstance(f.get("file"), str)
    }

    errors: list[str] = []
    invalid_indices: set[int] = set()
    for idx, finding in enumerate(findings):
        f_errors = _lint_single_finding(
            finding,
            _finding_label(finding, idx),
            memory_root,
            skipped_mounts,
            files_map,
            retired_files,
            promotion_targets,
            eff_workspace,
        )
        if f_errors:
            errors.extend(f_errors)
            invalid_indices.add(idx)

    errors.extend(
        _check_cross_finding_spans(
            findings, files_map, retired_files, invalid_indices
        )
    )
    errors.extend(
        _run_direct_conservation_pass(findings, invalid_indices, files_map)
    )

    survivors = [
        findings[idx] for idx in range(len(findings)) if idx not in invalid_indices
    ]
    return errors, _deduplicate_promotions(survivors, eff_workspace)


def _rewrite_markdown_links_in_line(
    line_text: str,
    source_relpath: str,
    retired_relpath: str,
    new_target_relpath: str,
    memory_root: Path,
) -> str:
    """Rewrites relative Markdown links in `line_text` pointing to `retired_relpath`."""
    source_dir = (memory_root / source_relpath).parent
    retired_abs = (memory_root / retired_relpath).resolve()
    new_target_abs = (memory_root / new_target_relpath).resolve()
    rel_replacement = os.path.relpath(new_target_abs, source_dir).replace(
        os.sep, "/"
    )

    def _replace(match: re.Match[str]) -> str:
        full = match.group(0)
        raw_target = match.group(1).strip()
        if raw_target.startswith(("#", "/")):
            return full
        path_part, sep, frag = raw_target.partition("#")
        path_part = path_part.split("?", 1)[0].strip()
        if not path_part.endswith(".md"):
            return full
        if (source_dir / path_part).resolve() != retired_abs:
            return full
        new_url = f"{rel_replacement}#{frag}" if sep else rel_replacement
        bracket_end = full.rfind("](")
        return f"{full[: bracket_end + 2]}{new_url})"

    return MARKDOWN_LINK_RE.sub(_replace, line_text)


def _build_link_rewrites(
    valid_findings: list[dict[str, Any]],
    files_map: dict[str, Any],
    memory_root: Path,
) -> list[dict[str, Any]]:
    """Expands `link_rewrite_target` on retired files into synthetic line-rewrite entries."""
    retired_files = {
        str(f["file"]) for f in valid_findings if f.get("action") == "retire"
    }
    rewrites: list[dict[str, Any]] = []
    for finding in valid_findings:
        retired_rel = str(finding.get("file") or "")
        for src_rel, ln, rewrite_target in _iter_retired_incoming_links(
            finding, files_map, retired_files
        ):
            src_lines = files_map[src_rel].get("original_lines") or []
            if ln > len(src_lines):
                continue
            orig_line = src_lines[ln - 1]
            new_line = _rewrite_markdown_links_in_line(
                orig_line, src_rel, retired_rel, rewrite_target, memory_root
            )
            rewrites.append({
                "id": f"link-rewrite:{finding.get('id', retired_rel)}:{src_rel}:L{ln}",
                "retired_finding_id": finding.get("id"),
                "retired_file": retired_rel,
                "source_file": src_rel,
                "line": ln,
                "old_target": retired_rel,
                "new_target": rewrite_target,
                "original_line": orig_line,
                "replacement_line": new_line,
            })
    return rewrites


def _collect_touched_memory_rels(
    valid_findings: list[dict[str, Any]],
    link_rewrites: list[dict[str, Any]],
    files_map: dict[str, Any],
) -> set[str]:
    """Collects all memory file relative paths touched by findings or link rewrites."""
    touched: set[str] = set()
    for finding in valid_findings:
        for key in ("file", "direct_migration_target", "link_rewrite_target"):
            val = finding.get(key)
            if isinstance(val, str) and val in files_map:
                touched.add(val)
    for rw in link_rewrites:
        src = rw.get("source_file")
        if isinstance(src, str) and src in files_map:
            touched.add(src)
    return touched


def _build_target_files_map(
    valid_findings: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Snapshots current SHA-256 and lines for each touched `promote_target` file."""
    target_files: dict[str, dict[str, Any]] = {}
    for finding in valid_findings:
        raw_target = finding.get("promote_target")
        if not isinstance(raw_target, str) or not raw_target.strip():
            continue
        target_path = Path(raw_target).expanduser().resolve()
        abs_str = str(target_path)
        if abs_str in target_files or not target_path.is_file():
            continue
        text = target_path.read_text(encoding="utf-8", errors="replace")
        t_lines = text.splitlines()
        target_files[abs_str] = {
            "sha256": sha256_text(text),
            "line_count": len(t_lines),
            "original_lines": t_lines,
        }
    return target_files


def build_verified_plan(
    inventory: dict[str, Any],
    valid_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Builds the `verified_plan.json` (`schema_version: 1`) structure from validated findings."""
    memory_root = Path(inventory["memory_root"]).resolve()
    files_map: dict[str, Any] = inventory.get("files") or {}
    link_rewrites = _build_link_rewrites(valid_findings, files_map, memory_root)
    touched_rels = _collect_touched_memory_rels(
        valid_findings, link_rewrites, files_map
    )

    plan_files: dict[str, dict[str, Any]] = {}
    for rel in sorted(touched_rels):
        meta = files_map[rel]
        orig_lines = list(meta.get("original_lines") or [])
        plan_files[rel] = {
            "sha256": meta.get("sha256") or sha256_text("\n".join(orig_lines)),
            "line_count": int(meta.get("line_count", len(orig_lines))),
            "original_lines": orig_lines,
            "frontmatter_end_line": int(meta.get("frontmatter_end_line", 0)),
            "frontmatter_keys": dict(meta.get("frontmatter_keys") or {}),
            "protected_file": bool(meta.get("protected_file", False)),
            "direct_lines": list(meta.get("direct_lines") or []),
            "incoming_links": list(meta.get("incoming_links") or []),
        }

    return {
        "schema_version": 1,
        "memory_root": str(memory_root),
        "skipped_mounts": list(inventory.get("skipped_mounts") or []),
        "promotion_targets": dict(inventory.get("promotion_targets") or {}),
        "findings": valid_findings,
        "link_rewrites": link_rewrites,
        "files": plan_files,
        "target_files": _build_target_files_map(valid_findings),
    }


def _parse_jsonl_lines(
    path: Path, text: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parses newline-delimited JSON objects from `text`."""
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for line_num, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            errors.append(f"{path}:{line_num}: invalid JSONL: {exc}")
            continue
        if isinstance(obj, dict):
            records.append(obj)
        else:
            errors.append(f"{path}:{line_num}: expected JSON object.")
    return records, errors


def _load_single_findings_file(
    raw_path: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Loads findings from a single JSONL or JSON array file."""
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        return [], [f"Findings file not found: {path}"]
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return [], []
    if text.startswith("["):
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            return [], [f"{path}: invalid JSON array: {exc}"]
        if isinstance(loaded, list):
            return [x for x in loaded if isinstance(x, dict)], []
    return _parse_jsonl_lines(path, text)


def _load_findings_files(
    paths: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Loads finding records from one or more JSONL or JSON array files."""
    records: list[dict[str, Any]] = []
    parse_errors: list[str] = []
    for raw_path in paths:
        file_records, file_errors = _load_single_findings_file(raw_path)
        records.extend(file_records)
        parse_errors.extend(file_errors)
    return records, parse_errors


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parses command-line arguments for `lint_memory_plan.py`."""
    parser = argparse.ArgumentParser(
        description="Validate audit-memory findings JSONL and emit verified_plan.json."
    )
    parser.add_argument(
        "--inventory",
        required=True,
        help="Path to memory_inventory.json produced by scan_memories.py.",
    )
    parser.add_argument(
        "--findings",
        nargs="+",
        required=True,
        help="One or more findings-*.jsonl files.",
    )
    parser.add_argument(
        "--workspace",
        help="Active repository workspace path for promote:script containment.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Emit verified_plan.json with valid findings while warning on invalid records.",
    )
    parser.add_argument(
        "--output-plan",
        required=True,
        help="Output path for verified_plan.json.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for `lint_memory_plan.py`."""
    args = _parse_args(argv)
    inv_path = Path(args.inventory).expanduser().resolve()
    if not inv_path.is_file():
        print(f"Error: inventory file not found: {inv_path}", file=sys.stderr)
        return 1
    try:
        inventory = json.loads(inv_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"Error: invalid inventory JSON: {exc}", file=sys.stderr)
        return 1

    findings, parse_errors = _load_findings_files(args.findings)
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else Path.cwd().resolve()
    )
    lint_errors, valid_findings = lint_findings(
        inventory, findings, workspace=workspace
    )
    all_errors = parse_errors + lint_errors

    if all_errors and not args.allow_partial:
        for err in all_errors:
            print(f"LINT ERROR: {err}", file=sys.stderr)
        return 1

    if all_errors and args.allow_partial:
        for err in all_errors:
            print(f"LINT WARNING (dropped): {err}", file=sys.stderr)

    plan = build_verified_plan(inventory, valid_findings)
    out_path = Path(args.output_plan).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(
        f"Verified {len(valid_findings)} finding(s) "
        f"({len(all_errors)} error(s)) -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
