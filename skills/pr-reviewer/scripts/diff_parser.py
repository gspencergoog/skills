#!/usr/bin/env python3
"""
diff_parser.py - Unified diff parser and diff hunk validator.

Parses git unified diffs to map modified line ranges (diff hunks) and validates
whether target review comment lines fall within diff boundaries accepted by the
GitHub Pull Request Reviews API.
"""

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class DiffHunk:
    """Represents a single unified diff hunk."""
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str

    def contains_line(self, line: int, side: str = "RIGHT") -> bool:
        """Checks if a 1-indexed line number is within this hunk."""
        if side.upper() == "RIGHT":
            if self.new_count == 0:
                return False
            return self.new_start <= line < (self.new_start + self.new_count)
        elif side.upper() == "LEFT":
            if self.old_count == 0:
                return False
            return self.old_start <= line < (self.old_start + self.old_count)
        return False

    def contains_range(self, start_line: int, end_line: int, side: str = "RIGHT") -> bool:
        """Checks if a 1-indexed line range [start_line, end_line] is fully within this hunk."""
        if start_line > end_line:
            start_line, end_line = end_line, start_line
        return self.contains_line(start_line, side) and self.contains_line(end_line, side)


# Pattern matching git diff file headers: diff --git a/path b/path
DIFF_HEADER_RE = re.compile(r"^diff --git a/(.*?) b/(.*)$")
# Pattern matching hunk header: @@ -old_start,old_count +new_start,new_count @@
HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


def parse_diff_hunks(diff_text: str) -> Dict[str, List[DiffHunk]]:
    """
    Parses a unified diff string and returns a dictionary mapping normalized
    file paths to lists of DiffHunk instances.
    """
    hunks_by_file: Dict[str, List[DiffHunk]] = {}
    current_file: Optional[str] = None

    for line in diff_text.splitlines():
        # Match diff --git header
        header_match = DIFF_HEADER_RE.match(line)
        if header_match:
            # b/ path represents the target file
            current_file = header_match.group(2)
            if current_file not in hunks_by_file:
                hunks_by_file[current_file] = []
            continue

        # Match +++ b/path in case diff was not formatted with diff --git
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            if current_file not in hunks_by_file:
                hunks_by_file[current_file] = []
            continue

        if line.startswith("+++ /dev/null"):
            # Deleted file
            continue

        # Match @@ hunk header
        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match and current_file:
            old_start = int(hunk_match.group(1))
            old_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
            new_start = int(hunk_match.group(3))
            new_count = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
            header = hunk_match.group(5).strip()

            hunk = DiffHunk(
                file_path=current_file,
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                header=header,
            )
            hunks_by_file[current_file].append(hunk)

    return hunks_by_file


def is_line_in_diff(
    hunks_by_file: Dict[str, List[DiffHunk]],
    file_path: str,
    line: int,
    side: str = "RIGHT",
) -> bool:
    """Checks whether a line falls within any diff hunk for the given file."""
    normalized_path = file_path.lstrip("./")
    hunks = hunks_by_file.get(normalized_path, [])
    return any(hunk.contains_line(line, side) for hunk in hunks)


def is_range_in_diff(
    hunks_by_file: Dict[str, List[DiffHunk]],
    file_path: str,
    start_line: int,
    end_line: int,
    side: str = "RIGHT",
) -> bool:
    """Checks whether a line range [start_line, end_line] falls within a single diff hunk."""
    normalized_path = file_path.lstrip("./")
    hunks = hunks_by_file.get(normalized_path, [])
    return any(hunk.contains_range(start_line, end_line, side) for hunk in hunks)


def extract_source_lines(
    file_path: str,
    start_line: int,
    end_line: int,
    repo_dir: Optional[str] = None,
) -> str:
    """
    Extracts 1-indexed lines [start_line, end_line] inclusive from a file on disk.
    Returns the lines joined with newlines.
    """
    if repo_dir:
        full_path = Path(repo_dir) / file_path.lstrip("./")
    else:
        full_path = Path(file_path)

    if not full_path.is_file():
        return ""

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return ""

    if start_line > end_line:
        start_line, end_line = end_line, start_line

    start_idx = max(0, start_line - 1)
    end_idx = min(len(lines), end_line)

    if start_idx >= len(lines):
        return ""

    extracted = lines[start_idx:end_idx]
    # Strip single trailing newline from extracted block for cleaner formatting
    return "".join(extracted).rstrip("\r\n")


def tag_findings_with_hunk_status(
    findings: List[Dict[str, Any]],
    hunks_by_file: Dict[str, List[DiffHunk]],
    repo_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Updates each finding in-place with `is_diff_hunk` boolean and enriches
    `original_code` from the repository checkout if not already populated.
    """
    for finding in findings:
        file_path = finding.get("file_path", "")
        start_line = finding.get("line_start", finding.get("line", 1))
        end_line = finding.get("line_end", start_line)
        side = finding.get("side", "RIGHT")

        in_hunk = is_range_in_diff(hunks_by_file, file_path, start_line, end_line, side)
        finding["is_diff_hunk"] = in_hunk

        # Populate original_code if empty and repo_dir provided
        if not finding.get("original_code") and repo_dir:
            finding["original_code"] = extract_source_lines(
                file_path, start_line, end_line, repo_dir
            )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse diffs and validate hunk lines.")
    parser.add_argument("--diff", required=True, help="Path to unified diff file.")
    parser.add_argument("--findings", help="Optional path to review findings JSON to tag.")
    parser.add_argument("--repo-dir", help="Path to repository checkout for source line extraction.")
    parser.add_argument("--output", help="Optional output path for tagged findings JSON.")
    args = parser.parse_args()

    diff_path = Path(args.diff)
    if not diff_path.is_file():
        print(f"Error: Diff file not found: {args.diff}", file=sys.stderr)
        return 1

    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    hunks = parse_diff_hunks(diff_text)

    if args.findings:
        findings_path = Path(args.findings)
        if not findings_path.is_file():
            print(f"Error: Findings file not found: {args.findings}", file=sys.stderr)
            return 1
        with open(findings_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        findings_list = data if isinstance(data, list) else data.get("findings", [])
        tag_findings_with_hunk_status(findings_list, hunks, args.repo_dir)

        output_text = json.dumps(data, indent=2)
        if args.output:
            Path(args.output).write_text(output_text, encoding="utf-8")
        else:
            print(output_text)
    else:
        # Output summary of parsed hunks
        summary = {
            file_path: [asdict(hunk) for hunk in file_hunks]
            for file_path, file_hunks in hunks.items()
        }
        print(json.dumps(summary, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
