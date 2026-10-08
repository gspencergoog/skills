#!/usr/bin/env python3
"""
apply_review.py - Submits approved PR review decisions to GitHub as an atomic batch.

Splits approved comments into inline diff comments (for lines inside diff hunks)
and broader context comments (appended to top-level review body to prevent GitHub
422 Unprocessable Entity errors). Submits atomically via GitHub Pull Request Reviews API.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


AGENT_ATTRIBUTION_BADGE = (
    "<sub><!-- agent-generated --><kbd>🤖 Agent-generated</kbd></sub>"
)


def append_agent_attribution(text: str) -> str:
    """Appends the agent-generated <kbd> badge footer if not already present."""
    stripped = text.strip()
    if not stripped:
        return stripped
    if (
        "<!-- agent-generated -->" in stripped
        or "<kbd>🤖 Agent-generated</kbd>" in stripped
    ):
        return stripped
    return f"{stripped}\n\n{AGENT_ATTRIBUTION_BADGE}"


def _extract_finding_coords(f: Dict[str, Any]) -> tuple[str, int, int, str]:
    """Extracts and normalizes (file_path, start_line, end_line, side) from a finding."""
    file_path = str(
        f.get("file_path") or f.get("file") or f.get("path") or ""
    ).strip().removeprefix("./")
    raw_start = f.get("line_start") or f.get("start_line") or f.get("line") or 1
    raw_end = f.get("line_end") or f.get("end_line") or f.get("line") or raw_start
    start_line = int(raw_start)
    end_line = int(raw_end)
    if start_line > end_line:
        start_line, end_line = end_line, start_line
    side = str(f.get("side") or "RIGHT").upper()
    return file_path, start_line, end_line, side


def _partition_approved_findings(
    findings: List[Dict[str, Any]]
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Splits approved findings into inline diff comments and out-of-diff items."""
    inline_comments: List[Dict[str, Any]] = []
    broader_context_items: List[Dict[str, Any]] = []

    for f in findings:
        if not f.get("approved", False):
            continue
        comment_body = str(
            f.get("draft_comment") or f.get("comment_body") or f.get("body") or ""
        ).strip()
        if not comment_body:
            continue

        file_path, start_line, end_line, side = _extract_finding_coords(f)

        if f.get("is_diff_hunk", True) and file_path:
            comment_obj: Dict[str, Any] = {
                "path": file_path,
                "line": end_line,
                "side": side,
                "body": append_agent_attribution(comment_body),
            }
            if start_line != end_line:
                comment_obj["start_line"] = start_line
                comment_obj["start_side"] = side
            inline_comments.append(comment_obj)
        else:
            broader_context_items.append({
                "file_path": file_path or "(general)",
                "line_start": start_line,
                "line_end": end_line,
                "description": str(f.get("description") or f.get("title") or ""),
                "body": comment_body,
            })

    return inline_comments, broader_context_items


def _format_broader_context_section(
    broader_context_items: List[Dict[str, Any]]
) -> List[str]:
    """Formats out-of-diff findings as a Markdown section for the review summary."""
    parts = [
        "\n### Broader Context & Non-Diff Observations\n",
        "_The following observations relate to surrounding or referenced code outside the active diff hunks:_\n",
    ]
    for item in broader_context_items:
        path_line = (
            f"`{item['file_path']}:{item['line_start']}`"
            if item["line_start"] == item["line_end"]
            else f"`{item['file_path']}:{item['line_start']}-{item['line_end']}`"
        )
        description = item["description"].strip()
        already_in_body = (
            bool(description)
            and description.rstrip(".").casefold() in item["body"].casefold()
        )
        desc_part = f" — {description}" if description and not already_in_body else ""
        parts.append(f"- **{path_line}**{desc_part}")
        quoted_body = "\n".join(f"  > {line}" for line in item["body"].splitlines())
        parts.append(f"{quoted_body}\n")
    return parts


def build_review_payload(decisions: Dict[str, Any]) -> Dict[str, Any]:
    """Constructs the GitHub Pull Request Review API payload.

    Separates inline hunk comments from out-of-hunk context comments. Ignores
    `pr_description` so reviewer-only blind diff descriptions and annotations
    are never automatically posted to GitHub.
    """
    head_sha = decisions.get("head_sha", "")
    verdict = decisions.get("verdict", "COMMENT").upper()
    if verdict not in ("COMMENT", "REQUEST_CHANGES", "APPROVE"):
        verdict = "COMMENT"

    base_summary = decisions.get("summary_comment", "").strip()
    inline_comments, broader_context_items = _partition_approved_findings(
        decisions.get("findings", [])
    )

    summary_parts = [base_summary] if base_summary else []
    if broader_context_items:
        summary_parts.extend(_format_broader_context_section(broader_context_items))

    synthesized_body = "\n".join(summary_parts).strip()
    if not synthesized_body:
        if not inline_comments:
            synthesized_body = "Reviewed and verified."
        elif verdict == "REQUEST_CHANGES":
            synthesized_body = "Please see the inline comments for requested changes."
    if synthesized_body:
        synthesized_body = append_agent_attribution(synthesized_body)

    payload: Dict[str, Any] = {
        "body": synthesized_body,
        "event": verdict,
        "comments": inline_comments,
    }
    if head_sha:
        payload["commit_id"] = head_sha

    return payload


def submit_review(
    owner: str,
    repo: str,
    pull_number: int,
    payload: Dict[str, Any],
    dry_run: bool = False,
) -> Tuple[int, Dict[str, Any], str]:
    """
    Submits the batched review payload to GitHub using `gh api`.
    Returns (exit_code, response_json, error_message).
    """
    endpoint = f"repos/{owner}/{repo}/pulls/{pull_number}/reviews"
    payload_str = json.dumps(payload, indent=2)

    if dry_run:
        print("[DRY RUN] Would submit POST to endpoint:", endpoint)
        print(payload_str)
        return 0, {"dry_run": True, "html_url": f"https://github.com/{owner}/{repo}/pull/{pull_number}"}, ""

    env = os.environ.copy()
    env.pop("GITHUB_TOKEN", None)

    cmd = ["gh", "api", "--method", "POST", endpoint, "--input", "-"]
    try:
        proc = subprocess.run(
            cmd,
            input=payload_str,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode, {}, proc.stderr.strip()

        resp_json = json.loads(proc.stdout) if proc.stdout.strip() else {}
        return 0, resp_json, ""
    except Exception as e:
        return 1, {}, str(e)


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit approved review decisions to GitHub.")
    parser.add_argument("--decisions-file", required=True, help="Path to review_decisions.json.")
    parser.add_argument("--owner", help="GitHub repository owner (if not in decisions file).")
    parser.add_argument("--repo", help="GitHub repository name (if not in decisions file).")
    parser.add_argument("--pull-number", type=int, help="PR number (if not in decisions file).")
    parser.add_argument("--dry-run", action="store_true", help="Print payload without posting to GitHub.")
    args = parser.parse_args()

    decisions_path = Path(args.decisions_file)
    if not decisions_path.is_file():
        print(f"Error: Decisions file not found: {decisions_path}", file=sys.stderr)
        return 1

    try:
        with open(decisions_path, "r", encoding="utf-8") as f:
            decisions = json.load(f)
    except Exception as e:
        print(f"Error reading decisions file: {e}", file=sys.stderr)
        return 1

    owner = args.owner or decisions.get("owner")
    repo = args.repo or decisions.get("repo")
    pull_number = args.pull_number or decisions.get("pull_number")

    if not owner or not repo or not pull_number:
        print("Error: Missing repository owner, repo name, or pull_number.", file=sys.stderr)
        return 1

    payload = build_review_payload(decisions)
    inline_count = len(payload.get("comments", []))
    print(f"Submitting batched PR review for {owner}/{repo}#{pull_number}...")
    print(f"Verdict: {payload['event']} | Inline comments: {inline_count}")

    code, resp, err = submit_review(owner, repo, pull_number, payload, dry_run=args.dry_run)
    if code != 0:
        print(f"Error submitting review to GitHub: {err}", file=sys.stderr)
        return code

    if args.dry_run:
        print("\nDry run complete. Nothing was posted to GitHub.")
        return 0

    review_url = resp.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{pull_number}"
    print(f"\n✓ Successfully submitted review to GitHub!")
    print(f"Review URL: {review_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
