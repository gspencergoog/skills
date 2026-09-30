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


def build_review_payload(decisions: Dict[str, Any]) -> Dict[str, Any]:
    """
    Constructs the GitHub Pull Request Review API payload.
    Separates inline hunk comments from out-of-hunk context comments.
    """
    head_sha = decisions.get("head_sha", "")
    verdict = decisions.get("verdict", "COMMENT").upper()
    if verdict not in ("COMMENT", "REQUEST_CHANGES", "APPROVE"):
        verdict = "COMMENT"

    base_summary = decisions.get("summary_comment", "").strip()
    findings = decisions.get("findings", [])

    inline_comments: List[Dict[str, Any]] = []
    broader_context_items: List[Dict[str, Any]] = []

    for f in findings:
        if not f.get("approved", True):
            continue

        comment_body = (f.get("draft_comment") or f.get("comment_body") or "").strip()
        if not comment_body:
            continue

        is_in_diff = f.get("is_diff_hunk", True)
        file_path = f.get("file_path", "")
        start_line = f.get("line_start", f.get("line", 1))
        end_line = f.get("line_end", start_line)
        side = f.get("side", "RIGHT").upper()

        if is_in_diff:
            # Inline diff comment
            comment_obj: Dict[str, Any] = {
                "path": file_path.lstrip("./"),
                "line": end_line,
                "side": side,
                "body": comment_body,
            }
            if start_line != end_line:
                comment_obj["start_line"] = start_line
                comment_obj["start_side"] = side
            inline_comments.append(comment_obj)
        else:
            # Out of diff - collect for top-level summary
            broader_context_items.append({
                "file_path": file_path,
                "line_start": start_line,
                "line_end": end_line,
                "description": f.get("description", ""),
                "body": comment_body,
            })

    # Assemble synthesized top-level body
    summary_parts = []
    if base_summary:
        summary_parts.append(base_summary)

    if broader_context_items:
        summary_parts.append("\n### Broader Context & Non-Diff Observations\n")
        summary_parts.append(
            "_The following observations relate to surrounding or referenced code outside the active diff hunks:_\n"
        )
        for item in broader_context_items:
            path_line = (
                f"`{item['file_path']}:{item['line_start']}`"
                if item["line_start"] == item["line_end"]
                else f"`{item['file_path']}:{item['line_start']}-{item['line_end']}`"
            )
            desc_part = f" — {item['description']}" if item["description"] else ""
            summary_parts.append(f"- **{path_line}**{desc_part}")
            # Indent comment block
            quoted_body = "\n".join(f"  > {line}" for line in item["body"].splitlines())
            summary_parts.append(f"{quoted_body}\n")

    synthesized_body = "\n".join(summary_parts).strip()
    if not synthesized_body and not inline_comments:
        synthesized_body = "Reviewed and verified."

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

    review_url = resp.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{pull_number}"
    print(f"\n✓ Successfully submitted review to GitHub!")
    print(f"Review URL: {review_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
