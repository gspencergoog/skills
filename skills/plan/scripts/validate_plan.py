#!/usr/bin/env python3
"""validate_plan.py - Implementation Plan Linter and Validator

Validates implementation plan markdown files for structural completeness,
actionable details, placeholder-free content, and file path integrity
against a target workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


@dataclass
class ValidationIssue:
    is_error: bool
    message: str
    line_num: Optional[int] = None
    file_path: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
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


class PlanValidator:
    # Required top-level or secondary section patterns (case-insensitive regex)
    REQUIRED_SECTION_PATTERNS = [
        (r"^#\s+.+", "Top-level title (e.g. '# Feature Name')"),
        (
            r"^##\s+.*(user review|open questions|review required|context)",
            "User Review / Questions / Context section",
        ),
        (
            r"^##\s+.*(proposed changes|phased implementation|implementation steps|phases)",
            "Proposed Changes / Phased Implementation section",
        ),
        (
            r"^##\s+.*(verification plan|verification|testing)",
            "Verification Plan section",
        ),
    ]

    # File action markers: [NEW], [MODIFY], [DELETE], [RENAME]
    FILE_ACTION_RE = re.compile(
        r"\[(NEW|MODIFY|DELETE|RENAME)\]\s+(?:\[(?P<label>[^\]]+)\]\((?P<link>[^\)]+)\)|`?(?P<path>[^\s`\)]+)`?)",
        re.IGNORECASE,
    )

    # Placeholders that indicate incomplete plans
    SUSPICIOUS_PLACEHOLDERS = [
        re.compile(r"\bTODO\b", re.IGNORECASE),
        re.compile(r"\bTBD\b", re.IGNORECASE),
        re.compile(r"\bimplement (?:rest|here|later)\b", re.IGNORECASE),
        re.compile(r"\badd code here\b", re.IGNORECASE),
        re.compile(r"\binsert code here\b", re.IGNORECASE),
    ]

    def __init__(
        self,
        content: str,
        workspace_dir: Optional[Path] = None,
        strict: bool = False,
    ):
        self.content = content
        self.lines = content.splitlines()
        self.workspace_dir = workspace_dir
        self.strict = strict
        self.issues: List[ValidationIssue] = []

    def validate(self) -> List[ValidationIssue]:
        self.issues.clear()
        self._check_sections()
        self._check_code_blocks()
        self._check_placeholders()
        self._check_file_actions()
        return self.issues

    def _check_sections(self) -> None:
        """Verifies that all required structural sections exist."""
        for pattern, description in self.REQUIRED_SECTION_PATTERNS:
            regex = re.compile(pattern, re.IGNORECASE)
            found = any(regex.search(line) for line in self.lines)
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
            if stripped.startswith("```") or stripped.startswith("~~~"):
                current_char = stripped[0]
                current_len = len(re.match(r"^[`~]+", stripped).group(0))  # type: ignore

                if not in_code_block:
                    in_code_block = True
                    fence_char = current_char
                    fence_len = current_len
                    block_start_line = i
                else:
                    if current_char == fence_char and current_len >= fence_len:
                        in_code_block = False

        if in_code_block:
            self.issues.append(
                ValidationIssue(
                    is_error=True,
                    message=f"Unclosed code fence starting at line {block_start_line}",
                    line_num=block_start_line,
                )
            )

    def _check_placeholders(self) -> None:
        """Flags unresolved placeholders (TODO, TBD, implement rest)."""
        in_code_block = False

        for i, line in enumerate(self.lines, start=1):
            stripped = line.strip()
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_code_block = not in_code_block
                continue

            for pattern in self.SUSPICIOUS_PLACEHOLDERS:
                if pattern.search(line):
                    self.issues.append(
                        ValidationIssue(
                            is_error=self.strict,
                            message=f"Unresolved placeholder found: '{line.strip()}'",
                            line_num=i,
                        )
                    )

    def _extract_target_path(self, match: re.Match) -> Tuple[str, str]:
        """Extracts the action and resolved path string from a match."""
        action = match.group(1).upper()
        link = match.group("link")
        raw_path = match.group("path")
        label = match.group("label")

        path_str = ""
        if link:
            # Handle file:/// or relative URLs
            if link.startswith("file://"):
                path_str = link[7:]
            else:
                path_str = link
        elif raw_path:
            path_str = raw_path
        elif label:
            path_str = label

        # Strip anchor links like #L1-L10
        if "#" in path_str:
            path_str = path_str.split("#")[0]

        return action, path_str.strip()

    def _check_file_actions(self) -> None:
        """Verifies files marked [NEW], [MODIFY], [DELETE] against disk if workspace is set."""
        seen_actions: Set[Tuple[str, str]] = set()

        for i, line in enumerate(self.lines, start=1):
            matches = list(self.FILE_ACTION_RE.finditer(line))
            for m in matches:
                action, path_str = self._extract_target_path(m)
                if not path_str or path_str.startswith("http://") or path_str.startswith("https://"):
                    continue

                if (action, path_str) in seen_actions:
                    continue
                seen_actions.add((action, path_str))

                if self.workspace_dir:
                    resolved_file: Path
                    if os.path.isabs(path_str):
                        resolved_file = Path(path_str)
                    else:
                        resolved_file = self.workspace_dir / path_str

                    if action in ("MODIFY", "DELETE"):
                        if not resolved_file.exists():
                            self.issues.append(
                                ValidationIssue(
                                    is_error=True,
                                    message=(
                                        f"File marked [{action}] does not exist on disk: '{path_str}' "
                                        f"(resolved to {resolved_file})"
                                    ),
                                    line_num=i,
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
                                    line_num=i,
                                    file_path=path_str,
                                )
                            )
                        # Check parent directory
                        if not resolved_file.parent.exists() and not any(
                            act == "NEW" and Path(p) in resolved_file.parents
                            for act, p in seen_actions
                        ):
                            self.issues.append(
                                ValidationIssue(
                                    is_error=False,
                                    message=(
                                        f"Parent directory for new file does not exist yet: "
                                        f"'{resolved_file.parent}'"
                                    ),
                                    line_num=i,
                                    file_path=path_str,
                                )
                            )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
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


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    content: str
    if args.plan_file:
        plan_path = Path(args.plan_file)
        if not plan_path.exists():
            print(f"Error: Plan file not found: {plan_path}", file=sys.stderr)
            return 2
        content = plan_path.read_text(encoding="utf-8")
    else:
        if sys.stdin.isatty():
            print("Error: No plan file provided and stdin is empty.", file=sys.stderr)
            return 2
        content = sys.stdin.read()

    workspace_dir = Path(args.workspace).resolve() if args.workspace else None
    if workspace_dir and not workspace_dir.is_dir():
        print(
            f"Error: Workspace directory does not exist: {workspace_dir}",
            file=sys.stderr,
        )
        return 2

    validator = PlanValidator(
        content=content,
        workspace_dir=workspace_dir,
        strict=args.strict,
    )
    issues = validator.validate()

    errors = [issue for issue in issues if issue.is_error]
    warnings = [issue for issue in issues if not issue.is_error]

    if args.json:
        output_payload = {
            "valid": len(errors) == 0 and (not args.strict or len(warnings) == 0),
            "errors": [err.to_dict() for err in errors],
            "warnings": [warn.to_dict() for warn in warnings],
            "total_issues": len(issues),
        }
        print(json.dumps(output_payload, indent=2))
    else:
        if issues and not args.quiet:
            for issue in issues:
                print(str(issue))

        if not errors and not (args.strict and warnings):
            if not args.quiet:
                print(
                    f"✓ Plan passed validation ({len(warnings)} warning(s))."
                )
        else:
            print(
                f"✗ Plan validation failed: {len(errors)} error(s), {len(warnings)} warning(s).",
                file=sys.stderr,
            )

    has_fatal_failure = len(errors) > 0 or (args.strict and len(warnings) > 0)
    return 1 if has_fatal_failure else 0


if __name__ == "__main__":
    sys.exit(main())

