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


# Pattern matching git diff file headers (quoted or unquoted): diff --git a/path b/path
DIFF_HEADER_RE = re.compile(
    r'^diff --git (?:"a/((?:[^"\\]|\\.)+)"|a/(\S+)) (?:"b/((?:[^"\\]|\\.)+)"|b/(\S+))$'
)
# Pattern matching +++ b/path headers (quoted or unquoted)
PLUS_HEADER_RE = re.compile(r'^\+\+\+ (?:"b/((?:[^"\\]|\\.)+)"|b/([^\t\n]+))')
# Pattern matching hunk header: @@ -old_start,old_count +new_start,new_count @@
HUNK_HEADER_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)
OCTAL_ESCAPE_RE = re.compile(r"(?:\\[0-3][0-7]{2})+")


def _unescape_git_path(raw_path: str) -> str:
    """Unescapes C/octal-escaped paths emitted by git diff when quoting filenames."""
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


def _parse_file_header(line: str) -> Optional[tuple[bool, Optional[str]]]:
    """Checks if `line` is a diff file header; returns (matched, new_current_file)."""
    if line.startswith("diff --git "):
        header_match = DIFF_HEADER_RE.match(line)
        if not header_match:
            return True, None
        quoted_b, plain_b = header_match.group(3), header_match.group(4)
        file_path = _unescape_git_path(quoted_b) if quoted_b is not None else plain_b
        return True, file_path

    if line.startswith("+++ /dev/null"):
        return True, None

    plus_match = PLUS_HEADER_RE.match(line)
    if plus_match:
        quoted_b, plain_b = plus_match.group(1), plus_match.group(2)
        file_path = (
            _unescape_git_path(quoted_b)
            if quoted_b is not None
            else plain_b.strip()
        )
        return True, file_path

    return None


def _parse_hunk_header(line: str, current_file: str) -> Optional[DiffHunk]:
    """Parses an `@@ ... @@` hunk header line for `current_file`."""
    hunk_match = HUNK_HEADER_RE.match(line)
    if not hunk_match:
        return None
    old_start = int(hunk_match.group(1))
    old_count = int(hunk_match.group(2)) if hunk_match.group(2) is not None else 1
    new_start = int(hunk_match.group(3))
    new_count = int(hunk_match.group(4)) if hunk_match.group(4) is not None else 1
    header = hunk_match.group(5).strip()
    return DiffHunk(
        file_path=current_file,
        old_start=old_start,
        old_count=old_count,
        new_start=new_start,
        new_count=new_count,
        header=header,
    )


def parse_diff_hunks(diff_text: str) -> Dict[str, List[DiffHunk]]:
    """Parses a unified diff string into a mapping of file paths to DiffHunk lists."""
    hunks_by_file: Dict[str, List[DiffHunk]] = {}
    current_file: Optional[str] = None

    for line in diff_text.splitlines():
        header_result = _parse_file_header(line)
        if header_result is not None:
            _, current_file = header_result
            if current_file and current_file not in hunks_by_file:
                hunks_by_file[current_file] = []
            continue

        if current_file:
            hunk = _parse_hunk_header(line, current_file)
            if hunk is not None:
                hunks_by_file[current_file].append(hunk)

    return hunks_by_file


def is_line_in_diff(
    hunks_by_file: Dict[str, List[DiffHunk]],
    file_path: str,
    line: int,
    side: str = "RIGHT",
) -> bool:
    """Checks whether a line falls within any diff hunk for the given file."""
    normalized_path = file_path.removeprefix("./")
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
    normalized_path = file_path.removeprefix("./")
    hunks = hunks_by_file.get(normalized_path, [])
    return any(hunk.contains_range(start_line, end_line, side) for hunk in hunks)


def resolve_repo_dir(repo_dir: Optional[str]) -> Optional[str]:
    """Resolves a repository checkout path from a directory path, text file, or coords JSON."""
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
            resolved = str(data.get("workspace_dir") or "").strip() if isinstance(data, dict) else ""
            return resolved or None
        except ValueError:
            return None
    first_line = raw.splitlines()[0].strip() if raw else ""
    return first_line or None


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
    clean_rel = file_path.removeprefix("./")
    if not clean_rel:
        return ""
    resolved_dir = resolve_repo_dir(repo_dir)
    full_path = Path(resolved_dir) / clean_rel if resolved_dir else Path(clean_rel)

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
    return "".join(extracted).rstrip("\r\n")


def normalize_finding_entry(
    raw: Dict[str, Any], index: int, force_out_of_diff: bool = False
) -> Dict[str, Any]:
    """Normalizes a single finding dictionary from canonical or alias keys."""
    item = dict(raw)
    file_path = str(
        item.get("file_path") or item.get("file") or item.get("path") or ""
    ).strip().removeprefix("./")

    raw_start = item.get("line_start") or item.get("start_line") or item.get("line") or 1
    raw_end = item.get("line_end") or item.get("end_line") or item.get("line") or raw_start
    start_line = int(raw_start)
    end_line = int(raw_end)
    if start_line > end_line:
        start_line, end_line = end_line, start_line

    description = str(item.get("description") or item.get("title") or "").strip()
    draft_comment = str(
        item.get("draft_comment")
        or item.get("comment_body")
        or item.get("body")
        or item.get("comment")
        or ""
    ).strip()

    item["id"] = str(item.get("id") or f"f{index}")
    item["file_path"] = file_path
    item["line_start"] = start_line
    item["line_end"] = end_line
    item["side"] = str(item.get("side") or "RIGHT").upper()
    item["severity"] = str(item.get("severity") or "medium").lower()
    if "category" in item and item["category"]:
        item["category"] = str(item["category"]).lower()
    item["description"] = description
    item["draft_comment"] = draft_comment
    if force_out_of_diff:
        item["is_diff_hunk"] = False
    return item


def _collect_raw_findings_list(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extracts and normalizes findings from canonical `findings` or alias lists."""
    collected: List[Dict[str, Any]] = []
    for key, out_of_diff in (
        ("findings", False),
        ("inline_findings", False),
        ("general_findings", True),
        ("comments", False),
    ):
        entries = data.get(key)
        if isinstance(entries, list):
            for raw in entries:
                if isinstance(raw, dict):
                    collected.append(
                        normalize_finding_entry(
                            raw, len(collected) + 1, force_out_of_diff=out_of_diff
                        )
                    )
    return collected


def normalize_findings_payload(data: Any) -> Dict[str, Any]:
    """Normalizes a findings JSON payload (canonical or alias keys) in-place or as a dict."""
    if isinstance(data, list):
        return {
            "findings": [
                normalize_finding_entry(item, i + 1)
                for i, item in enumerate(data)
                if isinstance(item, dict)
            ]
        }
    if not isinstance(data, dict):
        return {"findings": []}

    normalized = dict(data)
    normalized["findings"] = _collect_raw_findings_list(normalized)
    for alias_key in ("inline_findings", "general_findings", "comments"):
        normalized.pop(alias_key, None)

    raw_summary = normalized.get("review_summary")
    summary_dict = dict(raw_summary) if isinstance(raw_summary, dict) else {}
    overview = str(
        summary_dict.get("overview")
        or normalized.get("summary_comment")
        or normalized.get("summary")
        or normalized.get("overview")
        or ""
    ).strip()
    verdict = str(
        summary_dict.get("verdict") or normalized.get("verdict") or "COMMENT"
    ).upper()
    if overview or "review_summary" in normalized or "summary" in normalized:
        normalized["review_summary"] = {"overview": overview, "verdict": verdict}
    normalized.pop("summary", None)
    return normalized


def tag_findings_with_hunk_status(
    findings: List[Dict[str, Any]],
    hunks_by_file: Dict[str, List[DiffHunk]],
    repo_dir: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Updates each finding in-place with `is_diff_hunk` boolean and enriches
    `original_code` from the repository checkout if not already populated.
    """
    resolved_dir = resolve_repo_dir(repo_dir)
    for i, raw_finding in enumerate(findings):
        normalized = normalize_finding_entry(raw_finding, i + 1)
        raw_finding.update(normalized)
        file_path = raw_finding["file_path"]
        start_line = raw_finding["line_start"]
        end_line = raw_finding["line_end"]
        side = raw_finding["side"]

        if not file_path:
            raw_finding["is_diff_hunk"] = False
        else:
            raw_finding["is_diff_hunk"] = is_range_in_diff(
                hunks_by_file, file_path, start_line, end_line, side
            )

        if not raw_finding.get("original_code") and resolved_dir and file_path:
            raw_finding["original_code"] = extract_source_lines(
                file_path, start_line, end_line, resolved_dir
            )

    return findings


def _run_tag_findings(
    findings_arg: str,
    hunks: Dict[str, List[DiffHunk]],
    repo_dir: Optional[str],
    output_arg: Optional[str],
) -> int:
    """Loads, normalizes, validates, and tags a findings JSON file."""
    findings_path = Path(findings_arg)
    if not findings_path.is_file():
        print(f"Error: Findings file not found: {findings_arg}", file=sys.stderr)
        return 1
    with open(findings_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)

    if isinstance(raw_data, list):
        findings_list = [
            normalize_finding_entry(item, i + 1)
            for i, item in enumerate(raw_data)
            if isinstance(item, dict)
        ]
        data: Any = findings_list
    else:
        data = normalize_findings_payload(raw_data)
        findings_list = data["findings"]

    for idx, item in enumerate(findings_list):
        if not item.get("draft_comment") and not item.get("description"):
            print(
                f"Error: Finding #{idx + 1} in {findings_arg} has no draft_comment or description.",
                file=sys.stderr,
            )
            return 2

    tag_findings_with_hunk_status(findings_list, hunks, repo_dir)
    inline_count = sum(1 for f in findings_list if f.get("is_diff_hunk"))
    out_count = len(findings_list) - inline_count
    print(
        f"Tagged {len(findings_list)} findings ({inline_count} inline, {out_count} out-of-diff).",
        file=sys.stderr,
    )

    output_text = json.dumps(data, indent=2)
    if output_arg:
        out_path = Path(output_arg)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_text, encoding="utf-8")
    else:
        print(output_text)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse diffs and validate hunk lines.")
    parser.add_argument("--diff", required=True, help="Path to unified diff file.")
    parser.add_argument("--findings", help="Optional path to review findings JSON to tag.")
    parser.add_argument(
        "--repo-dir",
        help="Path to repository checkout (or workspace_dir.txt / pr_coords.json) for source line extraction.",
    )
    parser.add_argument("--output", help="Optional output path for tagged findings JSON.")
    args = parser.parse_args()

    diff_path = Path(args.diff)
    if not diff_path.is_file():
        print(f"Error: Diff file not found: {args.diff}", file=sys.stderr)
        return 1

    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    hunks = parse_diff_hunks(diff_text)

    if args.findings:
        return _run_tag_findings(args.findings, hunks, args.repo_dir, args.output)

    summary = {
        file_path: [asdict(hunk) for hunk in file_hunks]
        for file_path, file_hunks in hunks.items()
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


