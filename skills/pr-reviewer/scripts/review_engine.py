#!/usr/bin/env python3
"""
review_engine.py - Orchestrates external PR fetching, ephemeral workspace setup,
diff generation, and review payload construction.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from diff_parser import (
    parse_diff_hunks,
    tag_findings_with_hunk_status,
)


def run_command(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Runs a subprocess command and returns (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    # Unset GITHUB_TOKEN override to match user environment policies
    env.pop("GITHUB_TOKEN", None)
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def resolve_pr_metadata(pr_identifier: str, repo: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetches PR metadata from GitHub CLI (`gh pr view`).
    Returns dictionary with number, title, author, base_ref, head_ref, head_sha, url, etc.
    """
    cmd = [
        "gh", "pr", "view", pr_identifier,
        "--json", "number,title,author,baseRefName,headRefName,headRefOid,url,state"
    ]
    if repo:
        cmd.extend(["--repo", repo])

    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(f"Failed to fetch PR metadata for '{pr_identifier}': {stderr}")

    raw = json.loads(stdout)
    author_login = raw.get("author", {}).get("login", "") if isinstance(raw.get("author"), dict) else str(raw.get("author", ""))

    # Parse repo owner and name from PR URL (https://github.com/owner/repo/pull/123)
    pr_url = raw.get("url", "")
    url_match = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)", pr_url)
    owner, repo_name = ("", "")
    if url_match:
        owner = url_match.group(1)
        repo_name = url_match.group(2)

    return {
        "owner": owner,
        "repo": repo_name,
        "pull_number": raw.get("number"),
        "title": raw.get("title", ""),
        "author": author_login,
        "base_ref": raw.get("baseRefName", "main"),
        "head_ref": raw.get("headRefName", ""),
        "head_sha": raw.get("headRefOid", ""),
        "pr_url": pr_url,
        "state": raw.get("state", "OPEN"),
    }


def prepare_workspace(
    pr_metadata: Dict[str, Any],
    reference_dir: Optional[str] = None,
) -> Tuple[str, bool]:
    """
    Prepares a clean local workspace for analyzing the PR.
    If reference_dir exists on disk, clones with --reference into a temporary directory
    to ensure isolation and clean up after.
    Returns (workspace_dir, is_temporary).
    """
    owner = pr_metadata["owner"]
    repo = pr_metadata["repo"]
    pull_number = pr_metadata["pull_number"]
    full_repo = f"{owner}/{repo}" if owner and repo else repo

    # Create temporary directory
    temp_dir = tempfile.mkdtemp(prefix="pr-review-")

    # If reference_dir is a valid git repository of the same repo, use git clone --reference
    clone_cmd = ["gh", "repo", "clone", full_repo, temp_dir]
    if reference_dir and Path(reference_dir).is_dir():
        # Check if reference_dir is git repo
        code, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=reference_dir)
        if code == 0 and out == "true":
            # Clone with reference
            repo_url = f"https://github.com/{full_repo}.git"
            clone_cmd = ["git", "clone", "--reference", reference_dir, repo_url, temp_dir]

    ret, _, err = run_command(clone_cmd)
    if ret != 0:
        # Fall back to standard gh repo clone if git clone --reference failed
        ret, _, err = run_command(["gh", "repo", "clone", full_repo, temp_dir])
        if ret != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to clone repository '{full_repo}' into temporary directory: {err}")

    # Check out the PR branch in the temporary directory
    ret, _, err = run_command(["gh", "pr", "checkout", str(pull_number)], cwd=temp_dir)
    if ret != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to check out PR #{pull_number} in temporary directory: {err}")

    return temp_dir, True


def cleanup_workspace(workspace_dir: str) -> bool:
    """
    Safely removes a temporary review workspace directory.
    Validates that the path is non-empty and contains the 'pr-review-' prefix.
    """
    if not workspace_dir or not os.path.isdir(workspace_dir):
        return False

    abs_path = os.path.abspath(workspace_dir)
    dir_name = os.path.basename(abs_path)

    if "pr-review-" not in dir_name and "pr-review-" not in abs_path:
        # Safety abort: do not delete non-temporary directories
        return False

    try:
        shutil.rmtree(abs_path)
        return True
    except Exception:
        return False


def get_pr_diff(workspace_dir: str, base_ref: str = "main") -> str:
    """Extracts unified diff (-U0) comparing base_ref to HEAD."""
    # First try comparing against origin/<base_ref>
    ret, diff_out, _ = run_command(["git", "diff", "-U0", f"origin/{base_ref}...HEAD"], cwd=workspace_dir)
    if ret == 0 and diff_out:
        return diff_out

    # Fallback to base_ref directly
    ret, diff_out, _ = run_command(["git", "diff", "-U0", f"{base_ref}...HEAD"], cwd=workspace_dir)
    if ret == 0 and diff_out:
        return diff_out

    # Fallback to gh pr diff
    ret, diff_out, _ = run_command(["gh", "pr", "diff"], cwd=workspace_dir)
    if ret == 0:
        return diff_out

    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare workspace and extract PR diff.")
    parser.add_argument("--pr", required=True, help="PR number, URL, or branch name.")
    parser.add_argument("--repo", help="Optional owner/repo if not inferred from directory.")
    parser.add_argument("--repo-dir", help="Optional local directory to use as clone reference.")
    parser.add_argument("--mode", choices=["deep", "fast"], default="deep", help="Review depth mode.")
    parser.add_argument("--output-meta", help="Output path for resolved PR metadata JSON.")
    parser.add_argument("--output-diff", help="Output path for extracted unified diff.")
    parser.add_argument("--cleanup", help="Directory path to safely clean up and exit.")
    args = parser.parse_args()

    if args.cleanup:
        success = cleanup_workspace(args.cleanup)
        if success:
            print(f"Successfully cleaned up temporary directory: {args.cleanup}")
            return 0
        else:
            print(f"Failed or refused to clean up directory: {args.cleanup}", file=sys.stderr)
            return 1

    try:
        metadata = resolve_pr_metadata(args.pr, repo=args.repo)
    except Exception as e:
        print(f"Error resolving PR: {e}", file=sys.stderr)
        return 1

    if args.output_meta:
        Path(args.output_meta).write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        print(f"Wrote PR metadata to {args.output_meta}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
