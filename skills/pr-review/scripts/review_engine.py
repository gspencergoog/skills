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


def parse_repo_from_url(pr_url: str) -> Tuple[str, str]:
    """Extracts the repository owner and name from a GitHub pull request URL."""
    url_match = re.match(r"https?://[^/]+/([^/]+)/([^/]+)/pull/(\d+)", pr_url or "")
    if url_match:
        return url_match.group(1), url_match.group(2)
    return "", ""


def _extract_author_login(author_field: Any) -> str:
    """Extracts the login string from a GitHub CLI or GraphQL author field."""
    if isinstance(author_field, dict):
        return str(author_field.get("login") or "")
    return str(author_field or "")


def _resolve_owner_and_repo(pr_url: str, repo_arg: Optional[str]) -> Tuple[str, str]:
    """Resolves (owner, repo) from a PR URL or fallback 'owner/repo' string."""
    owner, repo_name = parse_repo_from_url(pr_url)
    if (not owner or not repo_name) and repo_arg and "/" in repo_arg:
        owner, repo_name = repo_arg.split("/", 1)
    return owner, repo_name


def _build_pr_metadata(
    raw: Dict[str, Any], owner: str, repo: str, pull_number: Optional[int] = None
) -> Dict[str, Any]:
    """Constructs a canonical pr_metadata dict from GitHub CLI or GraphQL fields."""
    pr_num = raw.get("number") or pull_number
    default_url = (
        f"https://github.com/{owner}/{repo}/pull/{pr_num}"
        if owner and repo and pr_num
        else ""
    )
    return {
        "owner": owner,
        "repo": repo,
        "pull_number": pr_num,
        "title": str(raw.get("title") or ""),
        "author": _extract_author_login(raw.get("author")),
        "base_ref": str(raw.get("baseRefName") or "main"),
        "head_ref": str(raw.get("headRefName") or ""),
        "head_sha": str(raw.get("headRefOid") or ""),
        "pr_url": str(raw.get("url") or default_url),
        "state": str(raw.get("state") or "OPEN"),
    }


def resolve_pr_coordinates(
    pr_identifier: str, repo: Optional[str] = None
) -> Dict[str, Any]:
    """Fetches non-narrative PR coordinates without exposing title, body, or branch name.

    Queries `gh pr view` for number, base ref, commit SHA, URL, and state
    so Stage 1 can clone and diff the PR without priming the reviewer on the
    author's stated intent or branch naming.
    """
    cmd = [
        "gh", "pr", "view", str(pr_identifier),
        "--json", "number,baseRefName,headRefOid,url,state",
    ]
    if repo:
        cmd.extend(["--repo", repo])

    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(f"Failed to fetch PR coordinates for '{pr_identifier}': {stderr}")

    raw = json.loads(stdout)
    pr_url = str(raw.get("url") or "")
    owner, repo_name = _resolve_owner_and_repo(pr_url, repo)

    return {
        "owner": owner,
        "repo": repo_name,
        "pull_number": raw.get("number"),
        "base_ref": str(raw.get("baseRefName") or "main"),
        "head_sha": str(raw.get("headRefOid") or ""),
        "pr_url": pr_url,
        "state": str(raw.get("state") or "OPEN"),
    }


def resolve_pr_metadata(pr_identifier: str, repo: Optional[str] = None) -> Dict[str, Any]:
    """Fetches PR metadata from GitHub CLI (`gh pr view`).

    Returns a dictionary with owner, repo, pull_number, title, author, base_ref,
    head_ref, head_sha, pr_url, and state.
    """
    cmd = [
        "gh", "pr", "view", str(pr_identifier),
        "--json", "number,title,author,baseRefName,headRefName,headRefOid,url,state",
    ]
    if repo:
        cmd.extend(["--repo", repo])

    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(f"Failed to fetch PR metadata for '{pr_identifier}': {stderr}")

    raw = json.loads(stdout)
    owner, repo_name = _resolve_owner_and_repo(str(raw.get("url") or ""), repo)
    return _build_pr_metadata(raw, owner, repo_name)


PR_CONTEXT_GRAPHQL_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      number
      title
      body
      state
      url
      baseRefName
      headRefName
      headRefOid
      author { login }
      comments(last: 50) {
        nodes {
          author { login }
          body
          createdAt
          url
        }
      }
      reviews(last: 50) {
        nodes {
          author { login }
          body
          state
          submittedAt
          url
        }
      }
      reviewThreads(last: 100) {
        totalCount
        nodes {
          path
          line
          isResolved
          isOutdated
          comments(last: 50) {
            totalCount
            nodes {
              author { login }
              body
              createdAt
              url
            }
          }
        }
      }
    }
  }
}
"""


def _format_comment_nodes(nodes: Optional[List[Any]]) -> List[Dict[str, str]]:
    """Normalizes a list of GraphQL or CLI comment nodes."""
    formatted: List[Dict[str, str]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        body = (node.get("body") or "").strip()
        if not body:
            continue
        formatted.append({
            "author": _extract_author_login(node.get("author")),
            "body": body,
            "created_at": str(node.get("createdAt") or ""),
            "url": str(node.get("url") or ""),
        })
    return formatted


def _format_review_nodes(nodes: Optional[List[Any]]) -> List[Dict[str, str]]:
    """Normalizes a list of GraphQL or CLI review nodes."""
    formatted: List[Dict[str, str]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        body = (node.get("body") or "").strip()
        state = str(node.get("state") or "")
        if not body and state in ("", "PENDING", "COMMENTED"):
            continue
        formatted.append({
            "author": _extract_author_login(node.get("author")),
            "body": body,
            "state": state,
            "submitted_at": str(node.get("submittedAt") or ""),
            "url": str(node.get("url") or ""),
        })
    return formatted


def _format_thread_nodes(nodes: Optional[List[Any]]) -> List[Dict[str, Any]]:
    """Normalizes a list of GraphQL reviewThread nodes."""
    threads: List[Dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        comments_container = node.get("comments") or {}
        raw_comments = comments_container.get("nodes") if isinstance(comments_container, dict) else []
        comments = _format_comment_nodes(raw_comments)
        if not comments:
            continue
        threads.append({
            "path": str(node.get("path") or ""),
            "line": node.get("line"),
            "is_resolved": bool(node.get("isResolved", False)),
            "is_outdated": bool(node.get("isOutdated", False)),
            "comments": comments,
        })
    return threads


def _are_threads_complete(review_threads_obj: Any) -> bool:
    """Returns True when all GraphQL review threads and comments were fetched without truncation."""
    if not isinstance(review_threads_obj, dict):
        return True
    nodes = review_threads_obj.get("nodes") or []
    total_threads = review_threads_obj.get("totalCount")
    if isinstance(total_threads, int) and total_threads > len(nodes):
        return False
    for node in nodes:
        if not isinstance(node, dict):
            continue
        comments_obj = node.get("comments")
        if isinstance(comments_obj, dict):
            c_nodes = comments_obj.get("nodes") or []
            c_total = comments_obj.get("totalCount")
            if isinstance(c_total, int) and c_total > len(c_nodes):
                return False
    return True


def _parse_graphql_context(
    pr_data: Dict[str, Any], owner: str, repo: str, pull_number: int
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Transforms a GraphQL pullRequest node into (pr_metadata, pr_context)."""
    pr_metadata = _build_pr_metadata(pr_data, owner, repo, pull_number)
    threads_container = pr_data.get("reviewThreads") or {}
    pr_context = {
        "owner": owner,
        "repo": repo,
        "pull_number": pr_metadata["pull_number"],
        "title": pr_metadata["title"],
        "body": str(pr_data.get("body") or ""),
        "author": pr_metadata["author"],
        "comments": _format_comment_nodes((pr_data.get("comments") or {}).get("nodes")),
        "reviews": _format_review_nodes((pr_data.get("reviews") or {}).get("nodes")),
        "threads": _format_thread_nodes(threads_container.get("nodes")),
        "threads_complete": _are_threads_complete(threads_container),
    }
    return pr_metadata, pr_context


def _fetch_context_via_pr_view(
    owner: str, repo: str, pull_number: int
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Fetches PR metadata and narrative context using `gh pr view` as a fallback."""
    full_repo = f"{owner}/{repo}"
    cmd = [
        "gh", "pr", "view", str(pull_number),
        "--repo", full_repo,
        "--json", "number,title,body,author,baseRefName,headRefName,headRefOid,url,state,comments,reviews",
    ]
    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(f"Failed to fetch PR context for {full_repo}#{pull_number}: {stderr}")

    raw = json.loads(stdout)
    pr_metadata = _build_pr_metadata(raw, owner, repo, pull_number)
    pr_context = {
        "owner": owner,
        "repo": repo,
        "pull_number": pr_metadata["pull_number"],
        "title": pr_metadata["title"],
        "body": str(raw.get("body") or ""),
        "author": pr_metadata["author"],
        "comments": _format_comment_nodes(raw.get("comments")),
        "reviews": _format_review_nodes(raw.get("reviews")),
        "threads": [],
        "threads_complete": False,
    }
    return pr_metadata, pr_context


def fetch_pr_context(
    owner: str, repo: str, pull_number: int
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Fetches full PR metadata and narrative context (title, body, comments, reviews, threads).

    Queries GitHub's GraphQL API first so inline review threads are included,
    and falls back to `gh pr view --json` if GraphQL fails.
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={PR_CONTEXT_GRAPHQL_QUERY}",
        "-f", f"owner={owner}",
        "-f", f"repo={repo}",
        "-F", f"pr={pull_number}",
    ]
    ret, stdout, _ = run_command(cmd)
    if ret == 0 and stdout:
        try:
            payload = json.loads(stdout)
            pr_data = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest")
            if isinstance(pr_data, dict):
                return _parse_graphql_context(pr_data, owner, repo, pull_number)
        except ValueError:
            pass
    return _fetch_context_via_pr_view(owner, repo, pull_number)


STACK_GRAPHQL_QUERY = """
query($owner: String!, $repo: String!, $pr: Int!) {
  repository(owner: $owner, name: $repo) {
    pullRequest(number: $pr) {
      stack {
        number
        size
        baseRefName
        entries(first: 50) {
          totalCount
          nodes {
            pullRequest {
              number
              title
              state
              isDraft
              headRefName
              baseRefName
              url
            }
          }
        }
      }
    }
  }
}
"""

# Keys of a stack entry that name or describe the change (branch names, titles).
# Stage 1 output must not carry them; see `blind_stack_summary`.
_NARRATIVE_STACK_ENTRY_KEYS = ("title", "head_ref", "base_ref", "url")


def _parse_stack(stack_data: Any, pull_number: int) -> Optional[Dict[str, Any]]:
    """Normalizes a GraphQL `pullRequest.stack` node into the stack dict used by both stages.

    Entries keep GitHub's order (bottom of the stack, closest to the trunk,
    first). `position` is 1-based from the bottom. Returns None when the PR is
    not in a stack.
    """
    if not isinstance(stack_data, dict):
        return None
    entries_container = stack_data.get("entries") or {}
    nodes = entries_container.get("nodes") if isinstance(entries_container, dict) else []
    entries: List[Dict[str, Any]] = []
    for node in nodes or []:
        pr = (node or {}).get("pullRequest") if isinstance(node, dict) else None
        if not isinstance(pr, dict) or pr.get("number") is None:
            continue
        number = int(pr["number"])
        entries.append({
            "number": number,
            "title": str(pr.get("title") or ""),
            "state": str(pr.get("state") or ""),
            "is_draft": bool(pr.get("isDraft", False)),
            "head_ref": str(pr.get("headRefName") or ""),
            "base_ref": str(pr.get("baseRefName") or ""),
            "url": str(pr.get("url") or ""),
            "is_target": number == pull_number,
            "needs_rebase": None,
        })
    position = next((i + 1 for i, e in enumerate(entries) if e["is_target"]), None)
    total = entries_container.get("totalCount") if isinstance(entries_container, dict) else None
    return {
        "number": stack_data.get("number"),
        "size": stack_data.get("size") or len(entries),
        "trunk": str(stack_data.get("baseRefName") or ""),
        "position": position,
        "entries_complete": not (isinstance(total, int) and total > len(entries)),
        "entries": entries,
    }


def fetch_pr_stack(owner: str, repo: str, pull_number: int) -> Optional[Dict[str, Any]]:
    """Returns the GitHub stack the PR belongs to, or None if it is not stacked.

    Uses the GraphQL `pullRequest.stack` field, so it works without the
    `gh stack` extension and without local stack metadata. A failed query is
    reported on stderr and treated as "not stacked".
    """
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={STACK_GRAPHQL_QUERY}",
        "-f", f"owner={owner}",
        "-f", f"repo={repo}",
        "-F", f"pr={pull_number}",
    ]
    ret, stdout, stderr = run_command(cmd)
    if ret != 0 or not stdout:
        print(f"Warning: could not query stack membership for #{pull_number}: {stderr}", file=sys.stderr)
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    pr_data = ((payload.get("data") or {}).get("repository") or {}).get("pullRequest") or {}
    return _parse_stack(pr_data.get("stack"), pull_number)


def structural_stack(stack: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Drops PR titles and URLs from a stack dict, keeping branch names for git checks.

    This is what Stage 1 writes to `--output-stack`: enough for Stage 2 to
    merge rebase status by PR number, but no narrative text.
    """
    if not stack:
        return None
    result = dict(stack)
    result["entries"] = [
        {k: v for k, v in entry.items() if k not in ("title", "url")}
        for entry in stack.get("entries", [])
    ]
    return result


def blind_stack_summary(stack: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Reduces a stack dict to numbers and states for `pr_coords.json`.

    Branch names, titles, and URLs are removed so Stage 1 stays blind to the
    author's framing while still knowing that the PR is part N of M.
    """
    if not stack:
        return None
    target = next((e for e in stack.get("entries", []) if e.get("is_target")), None)
    return {
        "number": stack.get("number"),
        "size": stack.get("size"),
        "position": stack.get("position"),
        "needs_rebase": target.get("needs_rebase") if target else None,
        "source": stack.get("source"),
        "entries": [
            {k: v for k, v in entry.items() if k not in _NARRATIVE_STACK_ENTRY_KEYS}
            for entry in stack.get("entries", [])
        ],
    }


def capture_stack_view(workspace_dir: str, pull_number: int) -> Optional[Dict[str, Any]]:
    """Imports the PR's stack into the checkout and returns `gh stack view --json`.

    `gh stack checkout <pr>` pulls every member branch, records the stack in
    `.git/gh-stack`, and switches to the PR's branch. The checkout is moved
    back to the commit it started on (detached) afterwards, so `HEAD` is
    unchanged for the rest of Stage 1. Returns None when the `gh stack`
    extension is missing, the PR is not stacked, or either command fails.
    """
    ret, start_sha, _ = run_command(["git", "rev-parse", "HEAD"], cwd=workspace_dir)
    if ret != 0 or not start_sha:
        return None

    view: Optional[Dict[str, Any]] = None
    ret, _, stderr = run_command(["gh", "stack", "checkout", str(pull_number)], cwd=workspace_dir)
    if ret == 0:
        ret, stdout, stderr = run_command(["gh", "stack", "view", "--json"], cwd=workspace_dir)
        if ret == 0 and stdout:
            try:
                parsed = json.loads(stdout)
                view = parsed if isinstance(parsed, dict) else None
            except ValueError:
                view = None
    if view is None:
        print(f"Note: `gh stack view` unavailable for #{pull_number}: {stderr}", file=sys.stderr)

    run_command(["git", "checkout", "--quiet", "--detach", start_sha], cwd=workspace_dir)
    return view


def _needs_rebase_from_git(workspace_dir: str, base_ref: str, head_ref: str) -> Optional[bool]:
    """Returns True when origin/<head_ref> does not contain origin/<base_ref>."""
    if not base_ref or not head_ref:
        return None
    ret, _, _ = run_command(
        ["git", "merge-base", "--is-ancestor", f"origin/{base_ref}", f"origin/{head_ref}"],
        cwd=workspace_dir,
    )
    if ret == 0:
        return False
    if ret == 1:
        return True
    return None


def annotate_stack_status(
    stack: Dict[str, Any],
    stack_view: Optional[Dict[str, Any]],
    workspace_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Fills `needs_rebase` (and merge/queue flags) on each stack entry in place.

    Prefers the `gh stack view --json` output, matched to entries by PR
    number. Without it, falls back to git ancestry in the checkout:
    a branch needs a rebase when it does not contain its base's tip.
    """
    by_number: Dict[int, Dict[str, Any]] = {}
    for branch in (stack_view or {}).get("branches") or []:
        pr = branch.get("pr") if isinstance(branch, dict) else None
        if isinstance(pr, dict) and pr.get("number") is not None:
            by_number[int(pr["number"])] = branch

    if by_number:
        stack["source"] = "gh stack view"
        for entry in stack.get("entries", []):
            branch = by_number.get(entry["number"])
            if branch is None:
                continue
            entry["needs_rebase"] = bool(branch.get("needsRebase", False))
            entry["is_merged"] = bool(branch.get("isMerged", False))
            entry["is_queued"] = bool(branch.get("isQueued", False))
        return stack

    if workspace_dir:
        stack["source"] = "git ancestry"
        for entry in stack.get("entries", []):
            entry["needs_rebase"] = _needs_rebase_from_git(
                workspace_dir, entry.get("base_ref", ""), entry.get("head_ref", "")
            )
    return stack


def merge_stack_status(
    fresh: Optional[Dict[str, Any]], recorded: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Copies Stage 1 rebase/merge flags onto a freshly fetched Stage 2 stack by PR number."""
    if not fresh:
        return fresh
    if not recorded:
        return fresh
    recorded_entries = {
        e.get("number"): e for e in recorded.get("entries", []) if e.get("number") is not None
    }
    for entry in fresh.get("entries", []):
        old = recorded_entries.get(entry.get("number"))
        if not old:
            continue
        for key in ("needs_rebase", "is_merged", "is_queued"):
            if key in old:
                entry[key] = old[key]
    if recorded.get("source"):
        fresh["source"] = recorded["source"]
    return fresh


def prepare_workspace(
    pr_metadata: Dict[str, Any],
    reference_dir: Optional[str] = None,
) -> Tuple[str, bool]:
    """Prepares a clean local workspace for analyzing the PR.

    If reference_dir exists on disk, clones with --reference into a temporary
    directory to ensure isolation and clean up after. Returns (workspace_dir,
    is_temporary).
    """
    owner = pr_metadata["owner"]
    repo = pr_metadata["repo"]
    pull_number = pr_metadata["pull_number"]
    full_repo = f"{owner}/{repo}" if owner and repo else repo

    temp_dir = tempfile.mkdtemp(prefix="pr-review-")

    clone_cmd = ["gh", "repo", "clone", full_repo, temp_dir]
    resolved_ref = resolve_workspace_path(reference_dir)
    if resolved_ref and Path(resolved_ref).is_dir():
        code, out, _ = run_command(["git", "rev-parse", "--is-inside-work-tree"], cwd=resolved_ref)
        if code == 0 and out == "true":
            repo_url = f"https://github.com/{full_repo}.git"
            clone_cmd = ["git", "clone", "--reference", resolved_ref, repo_url, temp_dir]

    ret, _, err = run_command(clone_cmd)
    if ret != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir = tempfile.mkdtemp(prefix="pr-review-")
        ret, _, err = run_command(["gh", "repo", "clone", full_repo, temp_dir])
        if ret != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to clone repository '{full_repo}' into temporary directory: {err}")

    ret, _, err = run_command(["gh", "pr", "checkout", str(pull_number), "--detach"], cwd=temp_dir)
    if ret != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(f"Failed to check out PR #{pull_number} in temporary directory: {err}")

    return temp_dir, True


def resolve_workspace_path(path_or_file: Optional[str]) -> str:
    """Resolves a workspace directory path from a directory path, text file, or coords JSON."""
    if not path_or_file:
        return ""
    candidate = path_or_file.strip()
    if not os.path.isfile(candidate):
        return candidate
    try:
        raw = Path(candidate).read_text(encoding="utf-8").strip()
    except OSError:
        return candidate
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return str(data.get("workspace_dir") or "").strip() if isinstance(data, dict) else ""
        except ValueError:
            return ""
    return raw.splitlines()[0].strip() if raw else ""


def cleanup_workspace(workspace_dir: str) -> bool:
    """Safely removes a temporary review workspace directory.

    Accepts a directory path directly, a text file containing the path
    (e.g. workspace_dir.txt), or a pr_coords.json file. Validates that the
    resolved path is a direct child of the system temporary directory and
    starts with the 'pr-review-' prefix.
    """
    resolved_dir = resolve_workspace_path(workspace_dir)
    if not resolved_dir or not os.path.isdir(resolved_dir):
        return False

    abs_path = os.path.realpath(resolved_dir)
    temp_root = os.path.realpath(tempfile.gettempdir())
    dir_name = os.path.basename(abs_path)

    if not dir_name.startswith("pr-review-") or os.path.dirname(abs_path) != temp_root:
        return False

    try:
        shutil.rmtree(abs_path)
        return True
    except Exception:
        return False


def get_pr_diff(
    workspace_dir: str,
    base_ref: str = "main",
    pull_number: Optional[int] = None,
) -> str:
    """Extracts unified diff (-U0) comparing base_ref to HEAD."""
    ret, diff_out, _ = run_command(
        ["git", "-c", "core.quotePath=false", "diff", "-U0", f"origin/{base_ref}...HEAD"],
        cwd=workspace_dir,
    )
    if ret == 0 and diff_out:
        return diff_out

    ret, diff_out, _ = run_command(
        ["git", "-c", "core.quotePath=false", "diff", "-U0", f"{base_ref}...HEAD"],
        cwd=workspace_dir,
    )
    if ret == 0 and diff_out:
        return diff_out

    gh_cmd = ["gh", "pr", "diff"] + ([str(pull_number)] if pull_number else [])
    ret, diff_out, _ = run_command(gh_cmd, cwd=workspace_dir)
    if ret == 0:
        return diff_out

    return ""


def _write_json_file(path_str: str, data: Dict[str, Any]) -> None:
    """Writes a dictionary as formatted JSON, creating parent directories if needed."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_prepare_workspace(args: argparse.Namespace) -> int:
    """Executes Stage 1: blind coordinate resolution, workspace checkout, and diff extraction."""
    if not args.pr:
        print("Error: --prepare-workspace requires --pr.", file=sys.stderr)
        return 1

    try:
        coords = resolve_pr_coordinates(args.pr, repo=args.repo)
        stack = fetch_pr_stack(coords["owner"], coords["repo"], int(coords["pull_number"]))
        workspace_dir, _ = prepare_workspace(coords, reference_dir=args.repo_dir)
        coords["workspace_dir"] = workspace_dir
        if stack:
            stack_view = capture_stack_view(workspace_dir, int(coords["pull_number"]))
            annotate_stack_status(stack, stack_view, workspace_dir)
        coords["stack"] = blind_stack_summary(stack)
        sha_ret, actual_sha, _ = run_command(["git", "rev-parse", "HEAD"], cwd=workspace_dir)
        if sha_ret == 0 and actual_sha:
            coords["head_sha"] = actual_sha
        diff_text = get_pr_diff(
            workspace_dir,
            base_ref=coords.get("base_ref", "main"),
            pull_number=coords.get("pull_number"),
        )
    except Exception as e:
        print(f"Error preparing blind review workspace: {e}", file=sys.stderr)
        return 1

    if not diff_text.strip():
        cleanup_workspace(workspace_dir)
        print("Error: Extracted PR diff is empty; aborting workspace setup.", file=sys.stderr)
        return 1

    if args.output_coords:
        _write_json_file(args.output_coords, coords)
    if args.output_stack and stack:
        _write_json_file(args.output_stack, structural_stack(stack))
    if args.output_diff:
        diff_path = Path(args.output_diff)
        diff_path.parent.mkdir(parents=True, exist_ok=True)
        diff_path.write_text(diff_text, encoding="utf-8")
    if args.output_workspace:
        ws_path = Path(args.output_workspace)
        ws_path.parent.mkdir(parents=True, exist_ok=True)
        ws_path.write_text(workspace_dir, encoding="utf-8")

    print(workspace_dir)
    return 0


def _validate_blind_description(path_str: Optional[str]) -> bool:
    """Checks that the Stage 1 blind description file was provided, exists, and is non-empty."""
    if not path_str:
        print(
            "Error: --require-blind-description is required before fetching PR metadata or context.",
            file=sys.stderr,
        )
        return False
    path = Path(path_str)
    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
        print(
            f"Error: Blind diff description is missing or empty at '{path_str}'. "
            "Draft the blind description from the diff before fetching PR context.",
            file=sys.stderr,
        )
        return False
    return True


def _resolve_target_from_args(args: argparse.Namespace) -> Tuple[str, str, int, str]:
    """Resolves (owner, repo, pull_number, stage1_sha) from --input-coords or --pr."""
    stage1_sha = ""
    if args.input_coords:
        coords = json.loads(Path(args.input_coords).read_text(encoding="utf-8"))
        owner = str(coords.get("owner") or "")
        repo = str(coords.get("repo") or "")
        pull_number = int(coords.get("pull_number") or 0)
        stage1_sha = str(coords.get("head_sha") or "")
        if owner and repo and pull_number:
            return owner, repo, pull_number, stage1_sha
        if not args.pr:
            raise ValueError(
                f"Incomplete coordinates in '{args.input_coords}': "
                "owner, repo, and pull_number are required."
            )

    if not args.pr:
        raise ValueError("Either --input-coords or --pr is required for --fetch-context.")

    coords = resolve_pr_coordinates(args.pr, repo=args.repo)
    return (
        str(coords["owner"]),
        str(coords["repo"]),
        int(coords["pull_number"]),
        stage1_sha or str(coords.get("head_sha") or ""),
    )


def _run_fetch_context(args: argparse.Namespace) -> int:
    """Executes Stage 2: fetches PR title, description, reviews, and comments."""
    if not _validate_blind_description(args.require_blind_description):
        return 1

    try:
        owner, repo, pull_number, stage1_sha = _resolve_target_from_args(args)
        metadata, context = fetch_pr_context(owner, repo, pull_number)
        recorded_stack = None
        if args.input_stack and Path(args.input_stack).is_file():
            recorded_stack = json.loads(Path(args.input_stack).read_text(encoding="utf-8"))
        stack = merge_stack_status(fetch_pr_stack(owner, repo, pull_number), recorded_stack)
    except Exception as e:
        print(f"Error fetching PR context: {e}", file=sys.stderr)
        return 1

    context["stack"] = stack
    if stack:
        metadata["stack"] = {
            "number": stack.get("number"),
            "size": stack.get("size"),
            "position": stack.get("position"),
        }

    if stage1_sha and metadata.get("head_sha") and metadata.get("head_sha") != stage1_sha:
        print(
            f"Warning: PR head moved from {stage1_sha} to {metadata['head_sha']} since Stage 1; "
            f"pinning head_sha to Stage 1 checkout ({stage1_sha}).",
            file=sys.stderr,
        )
        metadata["latest_head_sha"] = metadata["head_sha"]
        metadata["head_sha"] = stage1_sha

    if args.output_meta:
        _write_json_file(args.output_meta, metadata)
        print(f"Wrote PR metadata to {args.output_meta}")
    if args.output_context:
        _write_json_file(args.output_context, context)
        print(f"Wrote PR context to {args.output_context}")

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    """Constructs the argument parser for review_engine.py."""
    parser = argparse.ArgumentParser(description="Prepare workspace and extract PR diff and context.")
    parser.add_argument("--pr", help="PR number, URL, or branch name.")
    parser.add_argument("--repo", help="Optional owner/repo if not inferred from directory.")
    parser.add_argument(
        "--reference",
        "--repo-dir",
        dest="repo_dir",
        help="Optional local directory to use as clone reference.",
    )
    parser.add_argument(
        "--prepare-workspace",
        action="store_true",
        help="Stage 1: clone PR into a temporary workspace and extract diff without fetching PR prose.",
    )
    parser.add_argument(
        "--output-coords",
        help="Output path for Stage 1 blind PR coordinates JSON (no title, body, or comments).",
    )
    parser.add_argument("--output-workspace", help="Optional file path to record the temporary workspace directory.")
    parser.add_argument(
        "--output-stack",
        help=(
            "Stage 1: output path for the PR's GitHub stack (member PR numbers, states, branch names, "
            "and rebase status from `gh stack view --json`, no titles). Written only when the PR is stacked. "
            "Stage 2 material: do not read it before blind_description.md is written."
        ),
    )
    parser.add_argument(
        "--fetch-context",
        action="store_true",
        help="Stage 2: fetch PR title, description, reviews, and comments for reconciliation.",
    )
    parser.add_argument("--input-coords", help="Path to Stage 1 pr_coords.json for --fetch-context.")
    parser.add_argument(
        "--input-stack",
        help="Path to the Stage 1 --output-stack file; its rebase status is merged into pr_context.json's `stack`.",
    )
    parser.add_argument(
        "--require-blind-description",
        help="Path to blind_description.md that must exist and be non-empty before --fetch-context runs.",
    )
    parser.add_argument("--output-meta", help="Output path for resolved PR metadata JSON.")
    parser.add_argument("--output-context", help="Output path for PR narrative context JSON (title, body, comments).")
    parser.add_argument("--output-diff", help="Output path for extracted unified diff.")
    parser.add_argument("--cleanup", help="Directory path to safely clean up and exit.")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Entrypoint for review_engine CLI."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.cleanup:
        if cleanup_workspace(args.cleanup):
            print(f"Successfully cleaned up temporary directory: {args.cleanup}")
            return 0
        print(f"Failed or refused to clean up directory: {args.cleanup}", file=sys.stderr)
        return 1

    if args.prepare_workspace:
        return _run_prepare_workspace(args)

    if args.fetch_context:
        return _run_fetch_context(args)

    if not args.pr:
        parser.error("the following arguments are required: --pr")

    if not _validate_blind_description(args.require_blind_description):
        return 1

    try:
        metadata = resolve_pr_metadata(args.pr, repo=args.repo)
    except Exception as e:
        print(f"Error resolving PR: {e}", file=sys.stderr)
        return 1

    if args.output_meta:
        _write_json_file(args.output_meta, metadata)
        print(f"Wrote PR metadata to {args.output_meta}")

    return 0


if __name__ == "__main__":
    sys.exit(main())


