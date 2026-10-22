#!/usr/bin/env python3
"""validate_plan.py - Implementation Plan Linter and Validator

Validates implementation plan markdown files for structural completeness,
actionable details, placeholder-free content, parallel wave file disjointness,
and file path integrity against a target workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ValidationIssue:
    is_error: bool
    message: str
    line_num: int | None = None
    file_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": "ERROR" if self.is_error else "WARNING",
            "message": self.message,
            "line_num": self.line_num,
            "file_path": self.file_path,
        }

    def __str__(self) -> str:
        level = "ERROR" if self.is_error else "WARNING"
        location = f":{self.line_num}" if self.line_num is not None else ""
        return f"[{level}]{location} {self.message}"


@dataclass
class ParallelTrackSpan:
    wave_id: str
    track_id: str
    heading_line: int
    heading_level: int
    file_actions: list[tuple[str, str, str, int]] = field(default_factory=list)
    has_verification: bool = False


class PlanValidator:
    # Required top-level or secondary section patterns (case-insensitive regex)
    REQUIRED_SECTION_PATTERNS = (
        (r"^#\s+.+", "Top-level title (e.g. '# Feature Name')"),
        (
            r"^##\s+.*(user review|open questions|review required|context)",
            "User Review / Questions / Context section",
        ),
        (
            r"^##\s+.*(proposed changes|phased implementation|implementation steps|phases|waves)",
            "Proposed Changes / Phased Implementation section",
        ),
        (
            r"^##\s+.*(verification plan|verification|testing)",
            "Verification Plan section",
        ),
    )

    # File action markers: [NEW], [MODIFY], [DELETE], [RENAME]
    FILE_ACTION_RE = re.compile(
        r"\[(NEW|MODIFY|DELETE|RENAME)\]\s+(?:\[(?P<label>[^\]]+)\]\((?P<link>[^\)]+)\)|`?(?P<path>[^\s`\)]+)`?)",
        re.IGNORECASE,
    )

    # Task list items with inline heading hashes (e.g. "- [ ] #### [MODIFY] ...")
    CHECKBOX_HEADING_RE = re.compile(
        r"^\s*(?:(?:[-*+]|\d+\.)\s+)?\[[ xX]\]\s+#{1,6}\s+"
    )

    # Markdown headings
    HEADING_RE = re.compile(r"^\s*(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")

    # Wave heading: e.g. "### Wave 2: Core Implementation [Parallel]"
    WAVE_HEADING_RE = re.compile(
        r"^Wave\s+(?P<wave_id>[\w.-]+)\b(?P<rest>.*)$",
        re.IGNORECASE,
    )

    # Track subheading inside a wave: e.g. "#### Track 2A: Linter"
    TRACK_HEADING_RE = re.compile(
        r"^(?P<label>Track\s+[\w.-]+)\b",
        re.IGNORECASE,
    )

    # Inline parallel wave tag on a Phase or Track heading:
    # e.g. "### Phase 2A: Core [Parallel: Wave 2]"
    INLINE_PARALLEL_WAVE_RE = re.compile(
        r"^(?P<label>(?:Phase|Track)\s+[\w.-]+)\b.*?\[Parallel:\s*(?:Wave\s*)?(?P<wave_id>[\w.-]+)\]",
        re.IGNORECASE,
    )

    # Track or phase verification marker inside a parallel track
    TRACK_VERIFICATION_RE = re.compile(
        r"(?:\*\*|[-*+]\s+\*\*|^#+\s*)(?:track|phase)\b[^\n*]*verification\b|"
        r"^\s*[-*+]?\s*\*\*verification(?:\s+command)?\*\*",
        re.IGNORECASE,
    )

    # Placeholders that indicate incomplete plans
    SUSPICIOUS_PLACEHOLDERS = (
        re.compile(r"\bTODO\b", re.IGNORECASE),
        re.compile(r"\bTBD\b", re.IGNORECASE),
        re.compile(r"\bimplement (?:rest|here|later)\b", re.IGNORECASE),
        re.compile(r"\badd code here\b", re.IGNORECASE),
        re.compile(r"\binsert code here\b", re.IGNORECASE),
    )

    def __init__(
        self,
        content: str,
        workspace_dir: Path | None = None,
        strict: bool = False,
    ):
        self.content = content
        self.lines = content.splitlines()
        self.workspace_dir = workspace_dir
        self.strict = strict
        self.issues: list[ValidationIssue] = []

    def validate(self) -> list[ValidationIssue]:
        self.issues.clear()
        self._check_sections()
        self._check_code_blocks()
        self._check_task_list_headings()
        self._check_placeholders()
        self._check_file_actions()
        self._check_parallel_waves()
        return self.issues

    def _iter_non_fenced_lines(self) -> Iterator[tuple[int, str]]:
        """Yields (1-based line_num, line) for lines outside fenced code blocks."""
        in_code_block = False
        fence_char = ""
        fence_len = 0

        for i, line in enumerate(self.lines, start=1):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                current_char = stripped[0]
                current_len = len(stripped) - len(stripped.lstrip(current_char))
                if not in_code_block:
                    in_code_block = True
                    fence_char = current_char
                    fence_len = current_len
                elif current_char == fence_char and current_len >= fence_len:
                    in_code_block = False
                continue

            if not in_code_block:
                yield i, line

    def _check_sections(self) -> None:
        """Verifies that all required structural sections exist."""
        for pattern, description in self.REQUIRED_SECTION_PATTERNS:
            regex = re.compile(pattern, re.IGNORECASE)
            found = any(regex.search(line) for _, line in self._iter_non_fenced_lines())
            if not found:
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=f"Missing required section: {description}",
                    )
                )

    def _check_code_blocks(self) -> None:
        """Verifies that markdown code blocks are properly opened and closed."""
        in_code_block = False
        fence_char = ""
        fence_len = 0
        block_start_line = 0

        for i, line in enumerate(self.lines, start=1):
            stripped = line.strip()
            if stripped.startswith(("```", "~~~")):
                current_char = stripped[0]
                current_len = len(stripped) - len(stripped.lstrip(current_char))
                if not in_code_block:
                    in_code_block = True
                    fence_char = current_char
                    fence_len = current_len
                    block_start_line = i
                elif current_char == fence_char and current_len >= fence_len:
                    in_code_block = False

        if in_code_block:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=f"Unclosed code fence starting at line {block_start_line}",
                    line_num=block_start_line,
                )
            )

    def _check_task_list_headings(self) -> None:
        """Flags task list items that contain inline heading hashes (e.g. '- [ ] #### Item')."""
        for i, line in self._iter_non_fenced_lines():
            if self.CHECKBOX_HEADING_RE.search(line):
                self.issues.append(
                    ValidationIssue(
                        is_error=True,
                        message=(
                            f"Invalid Markdown: task list item contains heading hashes: '{line.strip()}'"
                        ),
                        line_num=i,
                    )
                )

    def _check_placeholders(self) -> None:
        """Flags unresolved placeholders (TODO, TBD, implement rest)."""
        for i, line in self._iter_non_fenced_lines():
            for pattern in self.SUSPICIOUS_PLACEHOLDERS:
                if pattern.search(line):
                    self.issues.append(
                        ValidationIssue(
                            is_error=self.strict,
                            message=f"Unresolved placeholder found: '{line.strip()}'",
                            line_num=i,
                        )
                    )

    def _extract_target_path(self, match: re.Match) -> tuple[str, str]:
        """Extracts the action and resolved path string from a match."""
        action = match.group(1).upper()
        link = match.group("link")
        raw_path = match.group("path")
        label = match.group("label")

        path_str = ""
        if link:
            path_str = link.removeprefix("file://")
        elif raw_path:
            path_str = raw_path
        elif label:
            path_str = label

        if "#" in path_str:
            path_str = path_str.split("#")[0]

        return action, path_str.strip()

    def _normalize_action_path(self, path_str: str) -> str:
        """Normalizes a file path so relative and file:// links compare equal."""
        if self.workspace_dir:
            ws_resolved = self.workspace_dir.resolve()
            candidate = (
                Path(path_str).resolve()
                if os.path.isabs(path_str)
                else (ws_resolved / path_str).resolve()
            )
            try:
                return candidate.relative_to(ws_resolved).as_posix()
            except ValueError:
                return candidate.as_posix().lstrip("/")

        cleaned = Path(path_str).as_posix()
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned.lstrip("/")

    def _validate_workspace_action(
        self,
        action: str,
        path_str: str,
        line_num: int,
        seen_actions: set[tuple[str, str]],
    ) -> None:
        """Validates a single file action against disk when workspace_dir is configured."""
        if not self.workspace_dir:
            return

        resolved_file = (
            Path(path_str) if os.path.isabs(path_str) else self.workspace_dir / path_str
        )

        if action in ("MODIFY", "DELETE") and not resolved_file.exists():
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=(
                        f"File marked [{action}] does not exist on disk: '{path_str}' "
                        f"(resolved to {resolved_file})"
                    ),
                    line_num=line_num,
                    file_path=path_str,
                )
            )
        elif action == "NEW":
            if resolved_file.exists():
                self.issues.append(
                    ValidationIssue(
                        is_error=False,
                        message=(
                            f"File marked [NEW] already exists on disk: '{path_str}'. "
                            "Should this be [MODIFY] instead?"
                        ),
                        line_num=line_num,
                        file_path=path_str,
                    )
                )
            has_planned_parent = any(
                act == "NEW" and Path(p) in resolved_file.parents
                for act, p in seen_actions
            )
            if not resolved_file.parent.exists() and not has_planned_parent:
                self.issues.append(
                    ValidationIssue(
                        is_error=False,
                        message=(
                            f"Parent directory for new file does not exist yet: "
                            f"'{resolved_file.parent}'"
                        ),
                        line_num=line_num,
                        file_path=path_str,
                    )
                )

    def _check_file_actions(self) -> None:
        """Verifies files marked [NEW], [MODIFY], [DELETE] against disk if workspace is set."""
        seen_actions: set[tuple[str, str]] = set()

        for i, line in self._iter_non_fenced_lines():
            for m in self.FILE_ACTION_RE.finditer(line):
                action, path_str = self._extract_target_path(m)
                if not path_str or path_str.startswith(("http://", "https://")):
                    continue
                if (action, path_str) in seen_actions:
                    continue
                seen_actions.add((action, path_str))
                self._validate_workspace_action(action, path_str, i, seen_actions)

    def _handle_track_heading(
        self,
        line_num: int,
        level: int,
        text: str,
        current_wave_id: str | None,
        current_wave_level: int,
        current_track: ParallelTrackSpan | None,
        waves: dict[str, list[ParallelTrackSpan]],
    ) -> tuple[str | None, int, ParallelTrackSpan | None]:
        """Updates wave and track state when a Markdown heading is encountered."""
        if current_track and level <= current_track.heading_level:
            current_track = None
        if current_wave_id and level <= current_wave_level:
            current_wave_id = None
            current_wave_level = 0

        inline_match = self.INLINE_PARALLEL_WAVE_RE.match(text)
        if inline_match:
            wave_id = inline_match.group("wave_id")
            track = ParallelTrackSpan(
                wave_id=wave_id,
                track_id=inline_match.group("label"),
                heading_line=line_num,
                heading_level=level,
            )
            waves.setdefault(wave_id, []).append(track)
            return current_wave_id, current_wave_level, track

        wave_match = self.WAVE_HEADING_RE.match(text)
        if wave_match:
            rest = wave_match.group("rest") or ""
            if "sequential" in rest.lower():
                return None, 0, None
            return wave_match.group("wave_id"), level, None

        track_match = self.TRACK_HEADING_RE.match(text)
        if track_match and current_wave_id:
            track = ParallelTrackSpan(
                wave_id=current_wave_id,
                track_id=track_match.group("label"),
                heading_line=line_num,
                heading_level=level,
            )
            waves.setdefault(current_wave_id, []).append(track)
            return current_wave_id, current_wave_level, track

        return current_wave_id, current_wave_level, current_track

    def _parse_parallel_tracks(self) -> dict[str, list[ParallelTrackSpan]]:
        """Groups parallel tracks by wave_id across non-fenced Markdown lines."""
        waves: dict[str, list[ParallelTrackSpan]] = {}
        current_wave_id: str | None = None
        current_wave_level = 0
        current_track: ParallelTrackSpan | None = None

        for i, line in self._iter_non_fenced_lines():
            heading_match = self.HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group("hashes"))
                text = heading_match.group("text")
                current_wave_id, current_wave_level, current_track = (
                    self._handle_track_heading(
                        i,
                        level,
                        text,
                        current_wave_id,
                        current_wave_level,
                        current_track,
                        waves,
                    )
                )
                continue

            if not current_track:
                continue

            if self.TRACK_VERIFICATION_RE.search(line):
                current_track.has_verification = True

            for m in self.FILE_ACTION_RE.finditer(line):
                action, path_str = self._extract_target_path(m)
                if not path_str or path_str.startswith(("http://", "https://")):
                    continue
                norm_path = self._normalize_action_path(path_str)
                current_track.file_actions.append((action, path_str, norm_path, i))

        return waves

    def _check_wave_collisions(
        self, wave_id: str, tracks: list[ParallelTrackSpan]
    ) -> None:
        """Flags file paths modified by multiple parallel tracks in the same wave."""
        first_seen: dict[str, tuple[str, int, str]] = {}

        for track in tracks:
            seen_in_track: set[str] = set()
            for _, raw_path, norm_path, line_num in track.file_actions:
                if norm_path in seen_in_track:
                    continue
                seen_in_track.add(norm_path)

                if norm_path in first_seen:
                    prev_track, prev_line, _ = first_seen[norm_path]
                    self.issues.append(
                        ValidationIssue(
                            is_error=True,
                            message=(
                                f"Parallel file collision in Wave {wave_id}: '{raw_path}' "
                                f"is touched by both {prev_track} (line {prev_line}) and "
                                f"{track.track_id} (line {line_num}). Isolate shared "
                                "chokepoint files into a sequential wave."
                            ),
                            line_num=line_num,
                            file_path=raw_path,
                        )
                    )
                else:
                    first_seen[norm_path] = (track.track_id, line_num, raw_path)

    def _check_track_verifications(self, tracks: list[ParallelTrackSpan]) -> None:
        """Warns (or errors in strict mode) when a parallel track lacks verification."""
        for track in tracks:
            if track.file_actions and not track.has_verification:
                self.issues.append(
                    ValidationIssue(
                        is_error=self.strict,
                        message=(
                            f"Parallel track '{track.track_id}' in Wave {track.wave_id} "
                            f"is missing a track verification command (e.g. '**{track.track_id} Verification**:')."
                        ),
                        line_num=track.heading_line,
                    )
                )

    def _check_parallel_waves(self) -> None:
        """Validates intra-wave file disjointness and per-track verification."""
        waves = self._parse_parallel_tracks()
        for wave_id, tracks in waves.items():
            if len(tracks) < 2:
                continue
            self._check_wave_collisions(wave_id, tracks)
            self._check_track_verifications(tracks)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate implementation plan markdown documents."
    )
    parser.add_argument(
        "plan_file",
        nargs="?",
        type=str,
        help="Path to the implementation plan markdown file (reads stdin if omitted).",
    )
    parser.add_argument(
        "--workspace",
        type=str,
        default=None,
        help="Path to the repository root to verify file paths against disk.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output validation results in JSON format.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Only output on validation errors.",
    )
    return parser.parse_args(argv)


def _read_plan_content(plan_file: str | None) -> str | None:
    """Reads plan markdown from a file path or stdin, returning None on error."""
    if plan_file:
        plan_path = Path(plan_file)
        if not plan_path.exists():
            print(f"Error: Plan file not found: {plan_path}", file=sys.stderr)
            return None
        return plan_path.read_text(encoding="utf-8")

    if sys.stdin.isatty():
        print("Error: No plan file provided and stdin is empty.", file=sys.stderr)
        return None
    return sys.stdin.read()


def _print_human_results(
    issues: list[ValidationIssue],
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
    strict: bool,
    quiet: bool,
) -> None:
    """Formats and prints human-readable validation results."""
    if issues and not quiet:
        for issue in issues:
            print(str(issue))

    if not errors and not (strict and warnings):
        if not quiet:
            print(f"✓ Plan passed validation ({len(warnings)} warning(s)).")
    else:
        print(
            f"✗ Plan validation failed: {len(errors)} error(s), {len(warnings)} warning(s).",
            file=sys.stderr,
        )


def _resolve_workspace_dir(workspace: str | None) -> tuple[Path | None, bool]:
    """Resolves and validates the workspace directory path."""
    if not workspace:
        return None, True
    workspace_dir = Path(workspace).resolve()
    if not workspace_dir.is_dir():
        print(
            f"Error: Workspace directory does not exist: {workspace_dir}",
            file=sys.stderr,
        )
        return None, False
    return workspace_dir, True


def _print_json_results(
    issues: list[ValidationIssue],
    errors: list[ValidationIssue],
    warnings: list[ValidationIssue],
    is_valid: bool,
) -> None:
    """Formats and prints JSON validation results."""
    output_payload = {
        "valid": is_valid,
        "errors": [err.to_dict() for err in errors],
        "warnings": [warn.to_dict() for warn in warnings],
        "total_issues": len(issues),
    }
    print(json.dumps(output_payload, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    content = _read_plan_content(args.plan_file)
    if content is None:
        return 2

    workspace_dir, valid_ws = _resolve_workspace_dir(args.workspace)
    if not valid_ws:
        return 2

    validator = PlanValidator(
        content=content,
        workspace_dir=workspace_dir,
        strict=args.strict,
    )
    issues = validator.validate()

    errors = [issue for issue in issues if issue.is_error]
    warnings = [issue for issue in issues if not issue.is_error]
    has_fatal_failure = bool(errors) or (args.strict and bool(warnings))

    if args.json:
        _print_json_results(issues, errors, warnings, not has_fatal_failure)
    else:
        _print_human_results(issues, errors, warnings, args.strict, args.quiet)

    return 1 if has_fatal_failure else 0


if __name__ == "__main__":
    sys.exit(main())
