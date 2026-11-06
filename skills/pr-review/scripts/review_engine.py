#!/usr/bin/env python3
"""
review_engine.py - Orchestrates external PR fetching, ephemeral workspace setup,
diff generation, and review payload construction.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PR_REVIEW_SCRIPTS = Path(__file__).resolve().parent
_CODE_REVIEW_SCRIPTS = Path(__file__).resolve().parents[2] / "code-review" / "scripts"
for _script_dir in (_PR_REVIEW_SCRIPTS, _CODE_REVIEW_SCRIPTS):
    if str(_script_dir) not in sys.path:
        sys.path.insert(0, str(_script_dir))

import diff_parser
import lint_review

ALIGNMENT_HEADINGS: dict[str, str] = {
    "MATCHES": "Matches Stated Purpose",
    "PARTIAL": "Partial Divergence",
    "DIVERGES": "Diverges from Stated Purpose",
    "UNSTATED": "Unstated Scope",
}

_VALID_CALLOUT_KINDS = frozenset({"NOTE", "IMPORTANT", "WARNING"})
_SECTION_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_FENCE_LINE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_TOP_LEVEL_BULLET_RE = re.compile(r"^-\s+")


@dataclass(frozen=True)
class SectionBudget:
    """Per-section length budget for Stage 1 blind_description.md."""

    sentences: int | None = None
    bullets: int | None = None
    lines_when_clean: int | None = None


DESCRIPTION_BUDGETS: dict[str, SectionBudget] = {
    "Summary": SectionBudget(sentences=3),
    "Quality Assessment": SectionBudget(sentences=1),
    "Changes": SectionBudget(bullets=6),
    "Reviewer Focus & Risks": SectionBudget(bullets=3),
    "Security & Execution Gate": SectionBudget(lines_when_clean=1),
}

_FALLBACK_FIX_HINTS: dict[str, str] = dict(getattr(lint_review, "_FIX_HINTS", {}))

_HARDENED_GIT_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("diff.external", ""),
)


def _apply_hardened_git_env(env: dict[str, str]) -> None:
    """Appends safe git config overrides to env via GIT_CONFIG_COUNT/KEY/VALUE."""
    try:
        base_count = max(0, int(env.get("GIT_CONFIG_COUNT", "0")))
    except ValueError:
        base_count = 0

    for offset, (key, value) in enumerate(_HARDENED_GIT_CONFIG):
        idx = base_count + offset
        env[f"GIT_CONFIG_KEY_{idx}"] = key
        env[f"GIT_CONFIG_VALUE_{idx}"] = value
    env["GIT_CONFIG_COUNT"] = str(base_count + len(_HARDENED_GIT_CONFIG))


def run_command(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Runs a subprocess command and returns (exit_code, stdout, stderr)."""
    env = os.environ.copy()
    # Unset GITHUB_TOKEN override to match user environment policies
    env.pop("GITHUB_TOKEN", None)
    _apply_hardened_git_env(env)
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
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def parse_repo_from_url(pr_url: str) -> tuple[str, str]:
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


def _resolve_owner_and_repo(pr_url: str, repo_arg: str | None) -> tuple[str, str]:
    """Resolves (owner, repo) from a PR URL or fallback 'owner/repo' string."""
    owner, repo_name = parse_repo_from_url(pr_url)
    if (not owner or not repo_name) and repo_arg and "/" in repo_arg:
        owner, repo_name = repo_arg.split("/", 1)
    return owner, repo_name


def _build_pr_metadata(
    raw: dict[str, Any], owner: str, repo: str, pull_number: int | None = None
) -> dict[str, Any]:
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
    pr_identifier: str, repo: str | None = None
) -> dict[str, Any]:
    """Fetches non-narrative PR coordinates without exposing title, body, or branch name.

    Queries `gh pr view` for number, base ref, commit SHA, URL, and state
    so Stage 1 can clone and diff the PR without priming the reviewer on the
    author's stated intent or branch naming.
    """
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_identifier),
        "--json",
        "number,baseRefName,headRefOid,url,state",
    ]
    if repo:
        cmd.extend(["--repo", repo])

    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(
            f"Failed to fetch PR coordinates for '{pr_identifier}': {stderr}"
        )

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


def resolve_pr_metadata(pr_identifier: str, repo: str | None = None) -> dict[str, Any]:
    """Fetches PR metadata from GitHub CLI (`gh pr view`).

    Returns a dictionary with owner, repo, pull_number, title, author, base_ref,
    head_ref, head_sha, pr_url, and state.
    """
    cmd = [
        "gh",
        "pr",
        "view",
        str(pr_identifier),
        "--json",
        "number,title,author,baseRefName,headRefName,headRefOid,url,state",
    ]
    if repo:
        cmd.extend(["--repo", repo])

    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(
            f"Failed to fetch PR metadata for '{pr_identifier}': {stderr}"
        )

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


def _format_comment_nodes(nodes: list[Any] | None) -> list[dict[str, str]]:
    """Normalizes a list of GraphQL or CLI comment nodes."""
    formatted: list[dict[str, str]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        body = (node.get("body") or "").strip()
        if not body:
            continue
        formatted.append(
            {
                "author": _extract_author_login(node.get("author")),
                "body": body,
                "created_at": str(node.get("createdAt") or ""),
                "url": str(node.get("url") or ""),
            }
        )
    return formatted


def _format_review_nodes(nodes: list[Any] | None) -> list[dict[str, str]]:
    """Normalizes a list of GraphQL or CLI review nodes."""
    formatted: list[dict[str, str]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        body = (node.get("body") or "").strip()
        state = str(node.get("state") or "")
        if not body and state in ("", "PENDING", "COMMENTED"):
            continue
        formatted.append(
            {
                "author": _extract_author_login(node.get("author")),
                "body": body,
                "state": state,
                "submitted_at": str(node.get("submittedAt") or ""),
                "url": str(node.get("url") or ""),
            }
        )
    return formatted


def _format_thread_nodes(nodes: list[Any] | None) -> list[dict[str, Any]]:
    """Normalizes a list of GraphQL reviewThread nodes."""
    threads: list[dict[str, Any]] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        comments_container = node.get("comments") or {}
        raw_comments = (
            comments_container.get("nodes")
            if isinstance(comments_container, dict)
            else []
        )
        comments = _format_comment_nodes(raw_comments)
        if not comments:
            continue
        threads.append(
            {
                "path": str(node.get("path") or ""),
                "line": node.get("line"),
                "is_resolved": bool(node.get("isResolved", False)),
                "is_outdated": bool(node.get("isOutdated", False)),
                "comments": comments,
            }
        )
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
    pr_data: dict[str, Any], owner: str, repo: str, pull_number: int
) -> tuple[dict[str, Any], dict[str, Any]]:
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetches PR metadata and narrative context using `gh pr view` as a fallback."""
    full_repo = f"{owner}/{repo}"
    cmd = [
        "gh",
        "pr",
        "view",
        str(pull_number),
        "--repo",
        full_repo,
        "--json",
        "number,title,body,author,baseRefName,headRefName,headRefOid,url,state,comments,reviews",
    ]
    ret, stdout, stderr = run_command(cmd)
    if ret != 0:
        raise RuntimeError(
            f"Failed to fetch PR context for {full_repo}#{pull_number}: {stderr}"
        )

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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fetches full PR metadata and narrative context (title, body, comments, reviews, threads).

    Queries GitHub's GraphQL API first so inline review threads are included,
    and falls back to `gh pr view --json` if GraphQL fails.
    """
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={PR_CONTEXT_GRAPHQL_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"repo={repo}",
        "-F",
        f"pr={pull_number}",
    ]
    ret, stdout, _ = run_command(cmd)
    if ret == 0 and stdout:
        try:
            payload = json.loads(stdout)
            pr_data = ((payload.get("data") or {}).get("repository") or {}).get(
                "pullRequest"
            )
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


def _parse_stack_entry(node: Any, pull_number: int) -> dict[str, Any] | None:
    """Parses one GraphQL stack entry node into a normalized dict."""
    if not isinstance(node, dict):
        return None
    pr = node.get("pullRequest")
    if not isinstance(pr, dict) or pr.get("number") is None:
        return None
    number = int(pr["number"])
    return {
        "number": number,
        "title": str(pr.get("title") or ""),
        "state": str(pr.get("state") or ""),
        "is_draft": bool(pr.get("isDraft", False)),
        "head_ref": str(pr.get("headRefName") or ""),
        "base_ref": str(pr.get("baseRefName") or ""),
        "url": str(pr.get("url") or ""),
        "is_target": number == pull_number,
        "needs_rebase": None,
    }


def _parse_stack(stack_data: Any, pull_number: int) -> dict[str, Any] | None:
    """Normalizes a GraphQL `pullRequest.stack` node into the stack dict used by both stages.

    Entries keep GitHub's order (bottom of the stack, closest to the trunk,
    first). `position` is 1-based from the bottom. Returns None when the PR is
    not in a stack.
    """
    if not isinstance(stack_data, dict):
        return None
    raw_entries = stack_data.get("entries")
    entries_container = raw_entries if isinstance(raw_entries, dict) else {}
    nodes = entries_container.get("nodes") or []
    entries: list[dict[str, Any]] = []
    for node in nodes:
        parsed = _parse_stack_entry(node, pull_number)
        if parsed is not None:
            entries.append(parsed)
    position = next((i + 1 for i, e in enumerate(entries) if e["is_target"]), None)
    total = entries_container.get("totalCount")
    incomplete = isinstance(total, int) and total > len(entries)
    return {
        "number": stack_data.get("number"),
        "size": stack_data.get("size") or len(entries),
        "trunk": str(stack_data.get("baseRefName") or ""),
        "position": position,
        "entries_complete": not incomplete,
        "entries": entries,
    }


def fetch_pr_stack(owner: str, repo: str, pull_number: int) -> dict[str, Any] | None:
    """Returns the GitHub stack the PR belongs to, or None if it is not stacked.

    Uses the GraphQL `pullRequest.stack` field, so it works without the
    `gh stack` extension and without local stack metadata. A failed query is
    reported on stderr and treated as "not stacked".
    """
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={STACK_GRAPHQL_QUERY}",
        "-f",
        f"owner={owner}",
        "-f",
        f"repo={repo}",
        "-F",
        f"pr={pull_number}",
    ]
    ret, stdout, stderr = run_command(cmd)
    if ret != 0 or not stdout:
        print(
            f"Warning: could not query stack membership for #{pull_number}: {stderr}",
            file=sys.stderr,
        )
        return None
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    pr_data = ((payload.get("data") or {}).get("repository") or {}).get(
        "pullRequest"
    ) or {}
    return _parse_stack(pr_data.get("stack"), pull_number)


def structural_stack(stack: dict[str, Any] | None) -> dict[str, Any] | None:
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


def blind_stack_summary(stack: dict[str, Any] | None) -> dict[str, Any] | None:
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


def capture_stack_view(workspace_dir: str, pull_number: int) -> dict[str, Any] | None:
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

    view: dict[str, Any] | None = None
    ret, _, stderr = run_command(
        ["gh", "stack", "checkout", str(pull_number)], cwd=workspace_dir
    )
    if ret == 0:
        ret, stdout, stderr = run_command(
            ["gh", "stack", "view", "--json"], cwd=workspace_dir
        )
        if ret == 0 and stdout:
            try:
                parsed = json.loads(stdout)
                view = parsed if isinstance(parsed, dict) else None
            except ValueError:
                view = None
    if view is None:
        print(
            f"Note: `gh stack view` unavailable for #{pull_number}: {stderr}",
            file=sys.stderr,
        )

    run_command(
        ["git", "checkout", "--quiet", "--detach", start_sha], cwd=workspace_dir
    )
    return view


def _needs_rebase_from_git(
    workspace_dir: str, base_ref: str, head_ref: str
) -> bool | None:
    """Returns True when origin/<head_ref> does not contain origin/<base_ref>."""
    if not base_ref or not head_ref:
        return None
    ret, _, _ = run_command(
        [
            "git",
            "merge-base",
            "--is-ancestor",
            f"origin/{base_ref}",
            f"origin/{head_ref}",
        ],
        cwd=workspace_dir,
    )
    if ret == 0:
        return False
    if ret == 1:
        return True
    return None


def _index_stack_branches(
    stack_view: dict[str, Any] | None,
) -> dict[int, dict[str, Any]]:
    """Indexes `gh stack view --json` branches by pull request number."""
    if not isinstance(stack_view, dict):
        return {}
    by_number: dict[int, dict[str, Any]] = {}
    for branch in stack_view.get("branches") or []:
        if not isinstance(branch, dict):
            continue
        pr = branch.get("pr")
        if isinstance(pr, dict) and pr.get("number") is not None:
            by_number[int(pr["number"])] = branch
    return by_number


def annotate_stack_status(
    stack: dict[str, Any],
    stack_view: dict[str, Any] | None,
    workspace_dir: str | None = None,
) -> dict[str, Any]:
    """Fills `needs_rebase` (and merge/queue flags) on each stack entry in place.

    Prefers the `gh stack view --json` output, matched to entries by PR
    number. Without it, falls back to git ancestry in the checkout:
    a branch needs a rebase when it does not contain its base's tip.
    """
    by_number = _index_stack_branches(stack_view)
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
    fresh: dict[str, Any] | None, recorded: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Copies Stage 1 rebase/merge flags onto a freshly fetched Stage 2 stack by PR number."""
    if not fresh:
        return fresh
    if not recorded:
        return fresh
    recorded_entries = {
        e.get("number"): e
        for e in recorded.get("entries", [])
        if e.get("number") is not None
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
    pr_metadata: dict[str, Any],
    reference_dir: str | None = None,
) -> tuple[str, bool]:
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
        code, out, _ = run_command(
            ["git", "rev-parse", "--is-inside-work-tree"], cwd=resolved_ref
        )
        if code == 0 and out == "true":
            repo_url = f"https://github.com/{full_repo}.git"
            clone_cmd = [
                "git",
                "clone",
                "--reference",
                resolved_ref,
                repo_url,
                temp_dir,
            ]

    ret, _, err = run_command(clone_cmd)
    if ret != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir = tempfile.mkdtemp(prefix="pr-review-")
        ret, _, err = run_command(["gh", "repo", "clone", full_repo, temp_dir])
        if ret != 0:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise RuntimeError(
                f"Failed to clone repository '{full_repo}' into temporary directory: {err}"
            )

    ret, _, err = run_command(
        ["gh", "pr", "checkout", str(pull_number), "--detach"], cwd=temp_dir
    )
    if ret != 0:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise RuntimeError(
            f"Failed to check out PR #{pull_number} in temporary directory: {err}"
        )

    return temp_dir, True


def resolve_workspace_path(path_or_file: str | None) -> str:
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
            return (
                str(data.get("workspace_dir") or "").strip()
                if isinstance(data, dict)
                else ""
            )
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
    except OSError:
        return False


def get_pr_diff(
    workspace_dir: str,
    base_ref: str = "main",
    pull_number: int | None = None,
) -> str:
    """Extracts unified diff (-U0) comparing base_ref to HEAD."""
    diff_flags = [
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "-U0",
    ]
    ret, diff_out, _ = run_command(
        ["git", *diff_flags, f"origin/{base_ref}...HEAD"],
        cwd=workspace_dir,
    )
    if ret == 0 and diff_out:
        return diff_out

    ret, diff_out, _ = run_command(
        ["git", *diff_flags, f"{base_ref}...HEAD"],
        cwd=workspace_dir,
    )
    if ret == 0 and diff_out:
        return diff_out

    gh_cmd = ["gh", "pr", "diff"] + ([str(pull_number)] if pull_number else [])
    ret, diff_out, _ = run_command(gh_cmd, cwd=workspace_dir)
    if ret == 0:
        return diff_out

    return ""


def _fetch_prev_round_paths(
    workspace_dir: str, base_ref: str, prev_head_sha: str
) -> list[str]:
    """Returns paths touched between origin/<base_ref> and prev_head_sha, fetching if needed."""
    if not base_ref or not prev_head_sha:
        return []
    name_cmd = [
        "git",
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--name-only",
        f"origin/{base_ref}...{prev_head_sha}",
    ]
    ret, out, _ = run_command(name_cmd, cwd=workspace_dir)
    if ret != 0:
        fetch_ret, _, _ = run_command(
            ["git", "fetch", "--no-tags", "origin", prev_head_sha],
            cwd=workspace_dir,
        )
        if fetch_ret != 0:
            return []
        ret, out, _ = run_command(name_cmd, cwd=workspace_dir)
    if ret != 0:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _collect_pr_increment_paths(
    workspace_dir: str,
    base_ref: str,
    prev_head_sha: str,
    diff_text: str,
) -> list[str]:
    """Returns sorted unique file paths modified by the PR in Round 1 or Round 2."""
    paths: set[str] = set()
    for line in diff_text.splitlines():
        header_path = _extract_header_path(line)
        if header_path:
            paths.add(header_path)
    paths.update(_fetch_prev_round_paths(workspace_dir, base_ref, prev_head_sha))
    return sorted(paths)


def get_incremental_diff(
    workspace_dir: str,
    prev_head_sha: str,
    head_sha: str,
    pr_paths: Sequence[str] | None = None,
) -> str | None:
    """Extracts the unified diff between prev_head_sha and head_sha, restricted to pr_paths when given."""
    if not prev_head_sha or not head_sha or prev_head_sha == head_sha:
        return None
    diff_cmd = [
        "git",
        "-c",
        "core.quotePath=false",
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        f"{prev_head_sha}..{head_sha}",
    ]
    if pr_paths:
        diff_cmd.extend(["--", *pr_paths])
    ret, diff_out, _ = run_command(diff_cmd, cwd=workspace_dir)
    if ret == 0:
        return diff_out

    fetch_ret, _, _ = run_command(
        ["git", "fetch", "--no-tags", "origin", prev_head_sha],
        cwd=workspace_dir,
    )
    if fetch_ret != 0:
        return None
    ret, diff_out, _ = run_command(diff_cmd, cwd=workspace_dir)
    return diff_out if ret == 0 else None


_RISKY_CONFIG_EXACT = frozenset(
    {
        "package.json",
        ".npmrc",
        "setup.py",
        "setup.cfg",
        "pyproject.toml",
        "tox.ini",
        "noxfile.py",
        "makefile",
        "gnumakefile",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "cargo.toml",
        "build.rs",
        "package.swift",
        "workspace",
        "workspace.bazel",
        "module.bazel",
        ".bazelrc",
        "build",
        "build.bazel",
        "pubspec.yaml",
        "build.yaml",
        "analysis_options.yaml",
        "conftest.py",
        "pytest.ini",
        ".gitattributes",
        ".gitmodules",
        ".pre-commit-config.yaml",
        ".envrc",
        ".mise.toml",
        ".tool-versions",
    }
)

_RISKY_CONFIG_PREFIXES = (
    ".eslintrc",
    "eslint.config.",
    "tsconfig",
    ".yarnrc",
)

_RISKY_CONFIG_SUFFIXES = (
    ".mk",
    ".gradle",
    ".gradle.kts",
    ".bzl",
)

_RISKY_PATH_SEGMENTS = (
    ".githooks/",
    ".husky/",
    ".github/workflows/",
    ".cargo/config",
)

_DIFF_GIT_HEADER_RE = re.compile(r"^diff --git a/.+? b/(.+)$")
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_LINE_RISK_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "process_and_dynamic_execution",
        re.compile(
            r"\b(?:subprocess\b|os\.(?:system|popen|exec\w*|spawn\w*)|pty\.spawn|"
            r"eval\s*\(|exec\s*\(|compile\s*\(|__import__\s*\(|importlib\.import_module|"
            r"child_process|execSync|spawnSync|execFile\w*|new\s+Function\s*\(|"
            r"Process\.(?:run|runSync|start)|Isolate\.spawnUri|Command::new)\b"
        ),
        "Process spawn or dynamic code execution",
    ),
    (
        "network_and_exfiltration",
        re.compile(
            r"(?:\b(?:urllib\.request|requests\.(?:get|post|put|delete|request|Session)|"
            r"httpx\.|aiohttp\.|socket\.socket|fetch\s*\(|XMLHttpRequest|new\s+WebSocket|"
            r"http\.(?:get|post|request)|HttpClient|curl\s+|wget\s+)\b|"
            r"(?:~|\$HOME|/home/|/Users/)[/\w.-]*\.(?:ssh|aws|env|gemini|gnupg|netrc|npmrc|pypirc)\b|"
            r"\.config/gh\b|/etc/(?:passwd|shadow)\b|"
            r"\b(?:os\.environ|process\.env|Platform\.environment)\b)"
        ),
        "Network egress, sensitive path, or environment access",
    ),
    (
        "obfuscation_and_payloads",
        re.compile(
            r"(?:\b(?:base64\.b64decode|codecs\.decode|"
            r"Buffer\.from\s*\([^)]*['\"]base64['\"]|atob\s*\(|bytes\.fromhex)\b|"
            r"(?:\\x[0-9a-fA-F]{2}){8,})"
        ),
        "Encoded payload or obfuscation primitive",
    ),
)


def _check_config_file_risk(file_path: str) -> str | None:
    """Returns a risk reason when file_path is a build, linter plugin, or hook config."""
    norm = file_path.strip().replace("\\", "/")
    if not norm:
        return None
    lower_norm = norm.lower()
    base = lower_norm.rsplit("/", 1)[-1]
    if base in _RISKY_CONFIG_EXACT:
        return f"Modified build, hook, or tool config ({base})"
    if base.startswith(_RISKY_CONFIG_PREFIXES) or base.endswith(_RISKY_CONFIG_SUFFIXES):
        return f"Modified linter, compiler, or build config ({base})"
    for seg in _RISKY_PATH_SEGMENTS:
        if lower_norm.startswith(seg) or f"/{seg}" in lower_norm:
            return f"Modified hook or workflow path ({norm})"
    return None


def _extract_header_path(line: str) -> str | None:
    """Extracts the target file path from diff --git, rename to, or +++ b/ headers."""
    if line.startswith("diff --git "):
        m = _DIFF_GIT_HEADER_RE.match(line)
        return m.group(1).strip() if m else None
    if line.startswith("rename to "):
        return line[len("rename to ") :].strip() or None
    if line.startswith("+++ b/"):
        return line[6:].strip() or None
    return None


def _parse_hunk_new_line(line: str) -> int | None:
    """Extracts the starting new-file line number from a unified diff @@ header."""
    m = _HUNK_HEADER_RE.match(line)
    return int(m.group(1)) if m else None


def _scan_added_line(
    file_path: str, line_num: int, content: str
) -> list[dict[str, Any]]:
    """Matches an added diff line against execution, network, and obfuscation patterns."""
    matches: list[dict[str, Any]] = []
    snippet = content.strip()[:160]
    for category, pattern, reason in _LINE_RISK_PATTERNS:
        if pattern.search(content):
            matches.append(
                {
                    "category": category,
                    "file_path": file_path,
                    "line": line_num,
                    "reason": reason,
                    "snippet": snippet,
                }
            )
    return matches


def _record_signal(
    signal: dict[str, Any],
    signals: list[dict[str, Any]],
    categories: dict[str, int],
    flagged_files: list[str],
) -> None:
    """Appends a risk signal and updates category counts and ordered flagged_files."""
    signals.append(signal)
    cat = signal["category"]
    categories[cat] = categories.get(cat, 0) + 1
    fp = signal["file_path"]
    if fp and fp not in flagged_files:
        flagged_files.append(fp)


def _record_config_if_needed(
    current_file: str,
    raw_line: str,
    seen_config_files: set[str],
    signals: list[dict[str, Any]],
    categories: dict[str, int],
    flagged_files: list[str],
) -> None:
    """Records a build_and_hook_configs signal once per modified config file."""
    if current_file in seen_config_files:
        return
    config_reason = _check_config_file_risk(current_file)
    if not config_reason:
        return
    seen_config_files.add(current_file)
    _record_signal(
        {
            "category": "build_and_hook_configs",
            "file_path": current_file,
            "line": 1,
            "reason": config_reason,
            "snippet": raw_line[:160],
        },
        signals,
        categories,
        flagged_files,
    )


def _step_hunk_line(
    raw_line: str,
    current_file: str,
    new_line: int,
    signals: list[dict[str, Any]],
    categories: dict[str, int],
    flagged_files: list[str],
) -> int:
    """Processes one hunk body line and returns the updated new_line counter."""
    if raw_line.startswith(("+++", "-", "\\")):
        return new_line
    if raw_line.startswith("+"):
        for sig in _scan_added_line(current_file, new_line, raw_line[1:]):
            _record_signal(sig, signals, categories, flagged_files)
    return new_line + 1


def scan_diff_execution_risks(diff_text: str) -> dict[str, Any]:
    """Scans a unified diff for local code execution hazards before running local tools."""
    signals: list[dict[str, Any]] = []
    categories: dict[str, int] = {}
    flagged_files: list[str] = []
    seen_config_files: set[str] = set()
    current_file = ""
    new_line: int | None = None

    for raw_line in (diff_text or "").splitlines():
        header_path = _extract_header_path(raw_line)
        if header_path is not None:
            current_file = header_path
            if raw_line.startswith("diff --git "):
                new_line = None
            _record_config_if_needed(
                current_file,
                raw_line,
                seen_config_files,
                signals,
                categories,
                flagged_files,
            )
            continue

        hunk_start = _parse_hunk_new_line(raw_line)
        if hunk_start is not None:
            new_line = hunk_start
        elif new_line is not None and current_file:
            new_line = _step_hunk_line(
                raw_line, current_file, new_line, signals, categories, flagged_files
            )

    return {
        "status": "flagged" if signals else "clean",
        "risk_count": len(signals),
        "flagged_files": flagged_files,
        "categories": categories,
        "signals": signals,
    }


def _write_json_file(path_str: str, data: dict[str, Any]) -> None:
    """Writes a dictionary as formatted JSON, creating parent directories if needed."""
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _run_security_scan(args: argparse.Namespace) -> int:
    """Executes standalone static execution-risk scanning on a diff file or stdin."""
    try:
        if args.security_scan == "-":
            diff_text = sys.stdin.read()
        else:
            diff_text = Path(args.security_scan).read_text(encoding="utf-8")
    except OSError as e:
        print(f"Error reading diff for --security-scan: {e}", file=sys.stderr)
        return 1

    scan_result = scan_diff_execution_risks(diff_text)
    if args.output_security_scan:
        _write_json_file(args.output_security_scan, scan_result)
    print(json.dumps(scan_result, indent=2))
    return 0


def _load_matching_prior_coords(
    output_coords_path: str | None, new_coords: dict[str, Any]
) -> dict[str, Any] | None:
    """Loads existing coordinates when they belong to the same PR."""
    if not output_coords_path:
        return None
    coords_path = Path(output_coords_path)
    if not coords_path.is_file():
        return None
    try:
        old_coords = json.loads(coords_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(old_coords, dict):
        return None
    prior_key = (
        old_coords.get("owner"),
        old_coords.get("repo"),
        old_coords.get("pull_number"),
    )
    current_key = (
        new_coords.get("owner"),
        new_coords.get("repo"),
        new_coords.get("pull_number"),
    )
    return old_coords if prior_key == current_key else None


def _archive_prior_round_state(
    output_coords_path: str | None, new_coords: dict[str, Any]
) -> tuple[int, str]:
    """Archives prior round state when output_coords_path matches the same PR.

    Returns (review_round, prev_head_sha).
    """
    old_coords = _load_matching_prior_coords(output_coords_path, new_coords)
    if not old_coords or not output_coords_path:
        return 1, ""

    prev_head_sha = str(old_coords.get("head_sha") or "")
    review_round = int(old_coords.get("review_round") or 1) + 1
    scratch_dir = Path(output_coords_path).parent
    blind_path = scratch_dir / "blind_description.md"
    if blind_path.is_file():
        blind_path.replace(scratch_dir / "prev_blind_description.md")
    for artifact_name in ("review_results.json", "review_decisions.json"):
        src_path = scratch_dir / artifact_name
        if src_path.is_file():
            (scratch_dir / f"prev_{artifact_name}").write_text(
                src_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return review_round, prev_head_sha


def _write_incremental_diff_if_needed(
    workspace_dir: str,
    coords: dict[str, Any],
    prev_head_sha: str,
    output_diff: str | None,
    diff_text: str = "",
) -> None:
    """Writes pr_incremental.diff alongside output_diff when prev_head_sha moved."""
    if not prev_head_sha or not output_diff:
        return
    pr_paths = _collect_pr_increment_paths(
        workspace_dir,
        str(coords.get("base_ref") or "main"),
        prev_head_sha,
        diff_text,
    )
    inc_diff = get_incremental_diff(
        workspace_dir,
        prev_head_sha,
        str(coords.get("head_sha") or ""),
        pr_paths=pr_paths,
    )
    if inc_diff is None:
        return
    inc_path = Path(output_diff).parent / "pr_incremental.diff"
    inc_path.parent.mkdir(parents=True, exist_ok=True)
    inc_path.write_text(inc_diff, encoding="utf-8")
    coords["incremental_diff_file"] = str(inc_path)


def _run_prepare_workspace(args: argparse.Namespace) -> int:
    """Executes Stage 1: blind coordinate resolution, workspace checkout, and diff extraction."""
    if not args.pr:
        print("Error: --prepare-workspace requires --pr.", file=sys.stderr)
        return 1

    try:
        coords = resolve_pr_coordinates(args.pr, repo=args.repo)
        stack = fetch_pr_stack(
            coords["owner"], coords["repo"], int(coords["pull_number"])
        )
        workspace_dir, _ = prepare_workspace(coords, reference_dir=args.repo_dir)
        coords["workspace_dir"] = workspace_dir
        if stack:
            stack_view = capture_stack_view(workspace_dir, int(coords["pull_number"]))
            annotate_stack_status(stack, stack_view, workspace_dir)
        coords["stack"] = blind_stack_summary(stack)
        sha_ret, actual_sha, _ = run_command(
            ["git", "rev-parse", "HEAD"], cwd=workspace_dir
        )
        if sha_ret == 0 and actual_sha:
            coords["head_sha"] = actual_sha
        diff_text = get_pr_diff(
            workspace_dir,
            base_ref=coords.get("base_ref", "main"),
            pull_number=coords.get("pull_number"),
        )
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as e:
        print(f"Error preparing blind review workspace: {e}", file=sys.stderr)
        return 1

    if not diff_text.strip():
        cleanup_workspace(workspace_dir)
        print(
            "Error: Extracted PR diff is empty; aborting workspace setup.",
            file=sys.stderr,
        )
        return 1

    security_prescan = scan_diff_execution_risks(diff_text)
    coords["security_prescan"] = security_prescan
    review_round, prev_head_sha = _archive_prior_round_state(args.output_coords, coords)
    coords["review_round"] = review_round
    if prev_head_sha:
        coords["prev_head_sha"] = prev_head_sha
        _write_incremental_diff_if_needed(
            workspace_dir, coords, prev_head_sha, args.output_diff, diff_text
        )

    if args.output_coords:
        _write_json_file(args.output_coords, coords)
    if args.output_security_scan:
        _write_json_file(args.output_security_scan, security_prescan)
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


def _is_stale_re_review_blind_description(
    blind_path: Path, current_text: str, input_coords_path: str | None
) -> bool:
    """Returns True when a re-review reused the prior blind description after HEAD moved."""
    if not input_coords_path:
        return False
    coords_path = Path(input_coords_path)
    if not coords_path.is_file():
        return False
    try:
        coords = json.loads(coords_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(coords, dict) or int(coords.get("review_round") or 1) <= 1:
        return False
    prev_sha = str(coords.get("prev_head_sha") or "")
    head_sha = str(coords.get("head_sha") or "")
    if not prev_sha or not head_sha or prev_sha == head_sha:
        return False
    prev_blind = blind_path.parent / "prev_blind_description.md"
    if not prev_blind.is_file():
        return False
    if prev_blind.read_text(encoding="utf-8").strip() == current_text:
        print(
            f"Error: Blind diff description at '{blind_path}' is identical to the "
            f"previous round's description even though HEAD moved "
            f"({prev_sha} -> {head_sha}). "
            "Draft a fresh Stage 1 description from the updated pr.diff.",
            file=sys.stderr,
        )
        return True
    return False


def _validate_blind_description(
    path_str: str | None, input_coords_path: str | None = None
) -> bool:
    """Checks that the Stage 1 blind description file exists, is non-empty, and is fresh."""
    if not path_str:
        print(
            "Error: --require-blind-description is required before fetching "
            "PR metadata or context.",
            file=sys.stderr,
        )
        return False
    path = Path(path_str)
    if not path.is_file():
        print(
            f"Error: Blind diff description is missing or empty at '{path_str}'. "
            "Draft the blind description from the diff before fetching PR context.",
            file=sys.stderr,
        )
        return False
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        print(
            f"Error: Blind diff description is missing or empty at '{path_str}'. "
            "Draft the blind description from the diff before fetching PR context.",
            file=sys.stderr,
        )
        return False
    return not _is_stale_re_review_blind_description(path, content, input_coords_path)


def _resolve_target_from_args(args: argparse.Namespace) -> tuple[str, str, int, str]:
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
        raise ValueError(
            "Either --input-coords or --pr is required for --fetch-context."
        )

    coords = resolve_pr_coordinates(args.pr, repo=args.repo)
    return (
        str(coords["owner"]),
        str(coords["repo"]),
        int(coords["pull_number"]),
        stage1_sha or str(coords.get("head_sha") or ""),
    )


def _write_code_review_prep_if_requested(
    args: argparse.Namespace,
    metadata: dict[str, Any],
    context: dict[str, Any],
) -> None:
    """Populates code_review_prep in context and writes code_review_prep.json when requested."""
    if not getattr(args, "prepare_code_review", False):
        return
    prep = _prepare_code_review_from_args(args, metadata)
    context["code_review_prep"] = prep
    prep_out = getattr(args, "output_prep", None)
    if not prep_out and args.output_context:
        prep_out = str(Path(args.output_context).parent / "code_review_prep.json")
    if prep_out:
        _write_json_file(prep_out, prep)
        print(f"Wrote code review prep to {prep_out}")


def _run_fetch_context(args: argparse.Namespace) -> int:
    """Executes Stage 2: fetches PR title, description, reviews, and comments."""
    if not _validate_blind_description(
        args.require_blind_description, args.input_coords
    ):
        return 1

    try:
        owner, repo, pull_number, stage1_sha = _resolve_target_from_args(args)
        metadata, context = fetch_pr_context(owner, repo, pull_number)
        recorded_stack = None
        if args.input_stack and Path(args.input_stack).is_file():
            recorded_stack = json.loads(
                Path(args.input_stack).read_text(encoding="utf-8")
            )
        stack = merge_stack_status(
            fetch_pr_stack(owner, repo, pull_number), recorded_stack
        )
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as e:
        print(f"Error fetching PR context: {e}", file=sys.stderr)
        return 1

    blind_path = Path(args.require_blind_description)
    blind_text = blind_path.read_text(encoding="utf-8")
    prescan_clean = _is_prescan_clean(args.input_coords, blind_text)
    for line in check_description_budgets(blind_text, prescan_clean):
        print(line, file=sys.stderr)
    context["blind_sha256"] = _compute_file_sha256(blind_path)
    context["stack"] = stack
    if stack:
        metadata["stack"] = {
            "number": stack.get("number"),
            "size": stack.get("size"),
            "position": stack.get("position"),
        }

    if (
        stage1_sha
        and metadata.get("head_sha")
        and metadata.get("head_sha") != stage1_sha
    ):
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
    _write_code_review_prep_if_requested(args, metadata, context)
    if args.output_context:
        _write_json_file(args.output_context, context)
        print(f"Wrote PR context to {args.output_context}")

    return 0


_CONTEXT_INDEXER_SCRIPTS = (
    Path(__file__).resolve().parents[2] / "context-indexer" / "scripts"
)
_ENTITY_QUERY_LIMIT = 25


def _run_duckdb_macro(macro: str, diff_dir: str) -> list[dict[str, Any]] | None:
    """Runs a staged_diff_macros.sql table macro and returns rows, or None on failure."""
    duckdb = shutil.which("duckdb")
    if not duckdb:
        return None
    sql_file = _CONTEXT_INDEXER_SCRIPTS / "staged_diff_macros.sql"
    escaped_dir = diff_dir.replace("'", "''")
    query = f"SELECT * FROM {macro}('{escaped_dir}') LIMIT {_ENTITY_QUERY_LIMIT};"
    ret, stdout, _ = run_command(
        [duckdb, "-batch", "-json", "-init", str(sql_file), "-c", query]
    )
    if ret != 0:
        return None
    try:
        rows = json.loads(stdout or "[]")
    except ValueError:
        return None
    return rows if isinstance(rows, list) else None


def _run_packrat_context(
    paths: list[str], repo_dir: Path, pull_number: Any
) -> dict[str, Any] | None:
    """Runs code-review's packrat_context.py read-only; returns its JSON or None."""
    script = _CODE_REVIEW_SCRIPTS / "packrat_context.py"
    cmd = [sys.executable, str(script), "--root", str(repo_dir)]
    if pull_number:
        cmd.extend(["--pr", str(pull_number)])
    cmd.append("-")
    try:
        proc = subprocess.run(
            cmd,
            input="\n".join(paths),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        parsed = json.loads(proc.stdout or "{}")
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _stage_diff_for_review(
    diff_path: Path, repo_dir: Path, base_ref: str, diff_dir: str
) -> str | None:
    """Runs stage_diffs.py --grouped --with-entities; returns an error or None.

    The diff is passed as a positional input, so stage_diffs never writes to it.
    """
    cmd = [
        sys.executable,
        str(_CONTEXT_INDEXER_SCRIPTS / "stage_diffs.py"),
        str(diff_path),
        "--output-dir",
        diff_dir,
        "--grouped",
        "--with-entities",
        "--repo-dir",
        str(repo_dir),
    ]
    if base_ref:
        cmd.extend(["--base-ref", f"origin/{base_ref}"])
    ret, _, stderr = run_command(cmd)
    return None if ret == 0 else (stderr or f"stage_diffs.py exited {ret}")


def prepare_code_review(
    coords: dict[str, Any], diff_path: Path, repo_dir: Path, scratch: Path
) -> dict[str, Any]:
    """Bundles code-review pre-flight steps for pr-review Stage 2.

    Runs classify_artifacts.classify_combined(), stage_diffs.py, optional
    duckdb churn and drift queries, and packrat_context.py. Every optional
    step degrades to a `warnings` entry instead of failing. `scratch` is the
    conversation scratch directory, recorded for callers.
    """
    import classify_artifacts

    warnings: list[str] = []
    diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
    paths = classify_artifacts._extract_paths_from_diff(diff_text)
    classification = classify_artifacts.classify_combined(paths, repo_dir, diff_text)

    diff_dir: str | None = tempfile.mkdtemp(prefix="pr-review-diffs-")
    stage_error = _stage_diff_for_review(
        diff_path, repo_dir, str(coords.get("base_ref") or ""), diff_dir
    )
    churn = drift = None
    if stage_error:
        shutil.rmtree(diff_dir, ignore_errors=True)
        diff_dir = None
        warnings.append(f"stage_diffs.py failed: {stage_error}")
    elif not shutil.which("duckdb"):
        warnings.append("duckdb not installed; churn and drift skipped")
    else:
        churn = _run_duckdb_macro("diff_entity_churn", diff_dir)
        drift = _run_duckdb_macro("diff_signature_drift", diff_dir)
        if churn is None or drift is None:
            warnings.append("duckdb churn or drift query failed")

    packrat = _run_packrat_context(paths, repo_dir, coords.get("pull_number"))
    if packrat is None:
        warnings.append("packrat_context.py unavailable or failed")

    return {
        "classification": classification,
        "diff_dir": diff_dir,
        "scratch_dir": str(scratch),
        "entity_churn": churn,
        "signature_drift": drift,
        "packrat": packrat,
        "warnings": warnings,
    }


def _prepare_code_review_from_args(
    args: argparse.Namespace, metadata: dict[str, Any]
) -> dict[str, Any]:
    """Resolves pr.diff, the checkout, and scratch paths, then runs prepare_code_review()."""
    scratch = Path(args.input_coords).parent if args.input_coords else Path.cwd()
    diff_path = Path(args.diff) if args.diff else scratch / "pr.diff"
    repo_arg = args.repo_dir or str(scratch / "workspace_dir.txt")
    repo_dir = Path(resolve_workspace_path(repo_arg) or repo_arg)
    if not diff_path.is_file() or not repo_dir.is_dir():
        return {
            "warnings": [
                f"--prepare-code-review skipped: missing {diff_path} or checkout {repo_dir}"
            ]
        }
    return prepare_code_review(metadata, diff_path, repo_dir, scratch)


def _compute_file_sha256(path: Path) -> str:
    """Computes the lowercase hex SHA-256 digest of a file."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _resolve_alignment_heading(raw_alignment: Any) -> str:
    """Validates and maps an alignment key or label to its canonical heading text."""
    if not isinstance(raw_alignment, str) or not raw_alignment.strip():
        raise ValueError("Missing or empty 'alignment' in reconciliation JSON.")
    token = raw_alignment.strip()
    if token in ALIGNMENT_HEADINGS:
        return ALIGNMENT_HEADINGS[token]
    if token in ALIGNMENT_HEADINGS.values():
        return token
    raise ValueError(
        f"Unknown alignment '{raw_alignment}'. "
        f"Expected one of {sorted(ALIGNMENT_HEADINGS)}."
    )


def _format_optional_section(heading: str, raw_body: Any) -> str:
    """Formats an optional top-level markdown block with a single ## heading."""
    if raw_body is None:
        return ""
    if type(raw_body) is not str:
        raise ValueError(f"Expected string for '{heading}' markdown block.")
    text = raw_body.strip()
    if not text:
        return ""
    if re.match(rf"^##\s+{re.escape(heading)}\b", text, re.IGNORECASE):
        return text
    return f"## {heading}\n\n{text}"


def _update_fence_marker(fence_marker: str, line: str) -> str:
    """Tracks whether the markdown line opens or closes a fenced code block."""
    match = _FENCE_LINE_RE.match(line)
    if not match:
        return fence_marker
    token = match.group(1)
    if not fence_marker:
        return token
    if token[0] == fence_marker[0] and len(token) >= len(fence_marker):
        return ""
    return fence_marker


def _split_blind_sections(markdown_text: str) -> tuple[str, list[tuple[str, str]]]:
    """Splits blind_description.md into (preamble, [(section_name, verbatim_block), ...])."""
    preamble_lines: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fence_marker = ""

    for line in markdown_text.splitlines():
        was_fenced = bool(fence_marker)
        fence_marker = _update_fence_marker(fence_marker, line)
        heading_match = (
            None if (was_fenced or fence_marker) else _SECTION_HEADING_RE.match(line)
        )
        if heading_match:
            sections.append((heading_match.group(1).strip(), [line]))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble_lines.append(line)

    rendered = [(name, "\n".join(lines).rstrip()) for name, lines in sections]
    return "\n".join(preamble_lines).strip(), rendered


def _count_section_sentences(text: str) -> int:
    """Counts sentences in a markdown section body using analyze_prose's stdlib engine."""
    if not text.strip():
        return 0
    metrics = lint_review.analyze_prose("-", engine="stdlib", text=text)
    if not isinstance(metrics, dict) or "error" in metrics:
        return 0
    return int((metrics.get("summary") or {}).get("total_sentences", 0))


def _count_top_level_bullets(lines: list[str]) -> int:
    """Counts top-level '-' bullet lines outside fenced code blocks."""
    count = 0
    fence_marker = ""
    for line in lines:
        was_fenced = bool(fence_marker)
        fence_marker = _update_fence_marker(fence_marker, line)
        if not was_fenced and not fence_marker and _TOP_LEVEL_BULLET_RE.match(line):
            count += 1
    return count


def _has_safe_gate_verdict(markdown: str) -> bool:
    """Returns True when Security & Execution Gate records SAFE_FOR_LOCAL_EXECUTION."""
    _, sections = _split_blind_sections(markdown)
    for sec_name, block in sections:
        if sec_name == "Security & Execution Gate":
            body = "\n".join(block.splitlines()[1:])
            if "REQUIRES_USER_CONFIRMATION" in body or "BLOCKED_UNSAFE" in body:
                return False
            return "SAFE_FOR_LOCAL_EXECUTION" in body
    return False


def _is_prescan_clean(input_coords_path: str | None, markdown: str) -> bool:
    """Returns True when pr_coords.json has an empty security_prescan.signals list
    and the blind description's gate verdict is SAFE_FOR_LOCAL_EXECUTION.
    """
    if not input_coords_path:
        return False
    coords_path = Path(input_coords_path)
    if not coords_path.is_file():
        return False
    try:
        coords = json.loads(coords_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(coords, dict):
        return False
    prescan = coords.get("security_prescan")
    if not isinstance(prescan, dict):
        return False
    signals = prescan.get("signals")
    if not isinstance(signals, list) or len(signals) > 0:
        return False
    return _has_safe_gate_verdict(markdown)


def check_description_budgets(markdown: str, prescan_clean: bool) -> list[str]:
    """Checks each Stage 1 section against DESCRIPTION_BUDGETS.

    Returns 'Budget: <section>: <n> <unit> (max <m>)' strings for any overruns.
    """
    _, sections = _split_blind_sections(markdown)
    warnings: list[str] = []
    for sec_name, verbatim_block in sections:
        budget = DESCRIPTION_BUDGETS.get(sec_name)
        if budget is None:
            continue
        body_lines = verbatim_block.splitlines()[1:]
        body_text = "\n".join(body_lines).strip()
        if budget.sentences is not None:
            sent_count = _count_section_sentences(body_text)
            if sent_count > budget.sentences:
                warnings.append(
                    f"Budget: {sec_name}: {sent_count} sentences (max {budget.sentences})"
                )
        if budget.bullets is not None:
            bullet_count = _count_top_level_bullets(body_lines)
            if bullet_count > budget.bullets:
                warnings.append(
                    f"Budget: {sec_name}: {bullet_count} bullets (max {budget.bullets})"
                )
        if (
            budget.lines_when_clean is not None
            and prescan_clean
            and "REQUIRES_USER_CONFIRMATION" not in body_text
            and "BLOCKED_UNSAFE" not in body_text
        ):
            line_count = sum(1 for line in body_lines if line.strip())
            if line_count > budget.lines_when_clean:
                warnings.append(
                    f"Budget: {sec_name}: {line_count} lines (max {budget.lines_when_clean})"
                )
    return warnings


def _run_check_description_budgets(args: argparse.Namespace) -> int:
    """Executes CLI section-budget check on blind_description.md and always exits 0."""
    blind_path = Path(args.check_description_budgets)
    if not blind_path.is_file():
        return 0
    try:
        blind_text = blind_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    prescan_clean = _is_prescan_clean(args.input_coords, blind_text)
    for line in check_description_budgets(blind_text, prescan_clean):
        print(line)
    return 0


def _format_callout_block(kind: str, text: str) -> str:
    """Formats a GFM callout (> [!KIND]) from kind and body text."""
    stripped = text.strip()
    if stripped.startswith(f"> [!{kind}]"):
        return stripped
    body_lines = [
        f"> {line}" if line.strip() else ">" for line in stripped.splitlines()
    ]
    return "\n".join([f"> [!{kind}]", *body_lines])


def _validate_single_callout(entry: Any, valid_sections: set[str]) -> tuple[str, str]:
    """Validates one callout dict and returns (section_name, formatted_callout)."""
    if type(entry) is not dict:
        raise ValueError("Each entry in 'callouts' must be a JSON object.")
    raw_section = entry.get("section")
    if not isinstance(raw_section, str) or not raw_section.strip():
        raise ValueError("Callout 'section' must be a non-empty string.")
    sec_name = raw_section.strip().removeprefix("##").strip()
    if sec_name not in valid_sections:
        raise ValueError(
            f"Unknown section '{raw_section}' in callouts. "
            f"Available sections: {sorted(valid_sections)}"
        )
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind.strip() not in _VALID_CALLOUT_KINDS:
        raise ValueError(
            f"Unknown callout kind '{kind}'. "
            f"Expected one of {sorted(_VALID_CALLOUT_KINDS)}."
        )
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Callout 'text' must be a non-empty string.")
    return sec_name, _format_callout_block(kind.strip(), text)


def _parse_callouts_by_section(
    raw_callouts: Any, valid_sections: set[str]
) -> dict[str, list[str]]:
    """Validates callouts[] entries and groups formatted callout blocks by section name."""
    if raw_callouts is None:
        return {}
    if type(raw_callouts) is not list:
        raise ValueError("'callouts' in reconciliation JSON must be a list.")

    grouped: dict[str, list[str]] = {}
    for entry in raw_callouts:
        sec_name, formatted = _validate_single_callout(entry, valid_sections)
        grouped.setdefault(sec_name, []).append(formatted)
    return grouped


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Reads and validates a JSON object file, raising ValueError on failure."""
    if not path.is_file():
        raise ValueError(f"{label} file not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {label} file '{path}': {e}") from e
    if type(data) is not dict:
        raise ValueError(f"{label} JSON root must be an object.")
    return data


def reconcile_pr_description(
    blind_path: Path,
    reconciliation_path: Path,
    output_path: Path,
    expected_blind_sha256: str | None = None,
) -> None:
    """Writes `## Intent Alignment: <ALIGNMENT_HEADINGS[alignment]>`, then the
    optional `## Stack Context` and `## Prior Review Resolution` blocks, then
    the blind sections verbatim with callouts inserted under their sections.
    Raises ValueError on a hash mismatch, an unknown section, kind, or alignment.
    """
    blind_file = Path(blind_path)
    out_file = Path(output_path)

    if not blind_file.is_file():
        raise ValueError(f"Blind description file not found: {blind_file}")
    blind_text = blind_file.read_text(encoding="utf-8")
    if not blind_text.strip():
        raise ValueError(f"Blind description file is empty: {blind_file}")

    if expected_blind_sha256 is not None:
        actual_sha = _compute_file_sha256(blind_file)
        if actual_sha != expected_blind_sha256.strip().lower():
            raise ValueError(
                f"Blind description SHA-256 mismatch for '{blind_file}': "
                f"expected {expected_blind_sha256}, got {actual_sha}."
            )

    rec_data = _read_json_object(Path(reconciliation_path), "Reconciliation")
    alignment_heading = _resolve_alignment_heading(rec_data.get("alignment"))
    stack_block = _format_optional_section(
        "Stack Context", rec_data.get("stack_markdown")
    )
    prior_block = _format_optional_section(
        "Prior Review Resolution", rec_data.get("prior_resolution_markdown")
    )

    preamble, sections = _split_blind_sections(blind_text)
    valid_sections = {name for name, _ in sections}
    callouts_by_section = _parse_callouts_by_section(
        rec_data.get("callouts"), valid_sections
    )

    blocks: list[str] = [f"## Intent Alignment: {alignment_heading}"]
    for optional_block in (preamble, stack_block, prior_block):
        if optional_block:
            blocks.append(optional_block)
    for sec_name, sec_text in sections:
        sec_callouts = callouts_by_section.get(sec_name)
        blocks.append(
            sec_text + "\n\n" + "\n\n".join(sec_callouts) if sec_callouts else sec_text
        )

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _resolve_expected_blind_sha(args: argparse.Namespace) -> str | None:
    """Resolves expected blind_sha256 from --expected-blind-sha256 or --context."""
    if args.expected_blind_sha256 is not None:
        return args.expected_blind_sha256
    if not args.context:
        return None
    ctx_data = _read_json_object(Path(args.context), "PR context")
    sha = ctx_data.get("blind_sha256")
    if not isinstance(sha, str) or not sha.strip():
        raise ValueError(f"Missing 'blind_sha256' in context file '{args.context}'.")
    return sha.strip()


def _run_reconcile_description(args: argparse.Namespace) -> int:
    """Executes CLI reconciliation of blind_description.md and reconciliation JSON."""
    base_dir = Path(args.context).parent if args.context else None
    blind_arg = args.blind_description or (
        str(base_dir / "blind_description.md") if base_dir else None
    )
    rec_arg = args.reconciliation or (
        str(base_dir / "intent_reconciliation.json") if base_dir else None
    )
    out_arg = args.output_description or (
        str(base_dir / "pr_description.md") if base_dir else None
    )
    if not blind_arg or not rec_arg or not out_arg:
        print(
            "Error: --reconcile-description requires --blind-description, "
            "--reconciliation, and --output-description (or --context).",
            file=sys.stderr,
        )
        return 1

    try:
        expected_sha = _resolve_expected_blind_sha(args)
        reconcile_pr_description(
            Path(blind_arg),
            Path(rec_arg),
            Path(out_arg),
            expected_blind_sha256=expected_sha,
        )
    except (ValueError, OSError) as e:
        print(f"Error reconciling PR description: {e}", file=sys.stderr)
        return 1

    print(f"Wrote reconciled PR description to {out_arg}")
    return 0


def _print_lint_violations(violations: list[Any]) -> None:
    """Prints lint violations and their actionable Fix: hints to stdout."""
    module_hints = getattr(lint_review, "_FIX_HINTS", {})
    for v in violations:
        print(f"{v.line}: {v.rule}: {v.message}")
        hint = (
            getattr(v, "fix", "")
            or (module_hints.get(v.rule, "") if isinstance(module_hints, dict) else "")
            or _FALLBACK_FIX_HINTS.get(
                v.rule, "Update the report section to satisfy this lint rule."
            )
        )
        if "Fix:" not in str(v.message):
            print(f"Fix: {hint}")


def _merge_pr_metadata(
    payload: dict[str, Any], meta_path: Path | None
) -> dict[str, Any]:
    """Merges pr_meta.json fields into payload['pr_metadata']."""
    pr_metadata = dict(payload.get("pr_metadata") or {})
    if meta_path is not None and Path(meta_path).is_file():
        meta_data = _read_json_object(Path(meta_path), "PR metadata")
        for key, value in meta_data.items():
            if not pr_metadata.get(key) and value is not None and value != "":
                pr_metadata[key] = value
    payload["pr_metadata"] = pr_metadata
    return payload


def finalize_review(
    report_md: Path,
    diff_path: Path,
    meta_path: Path,
    repo_dir: Path | None,
    output_results: Path,
    output_findings: Path,
) -> int:
    """Runs parse_report() and check_report(). On violations prints each with
    its Fix: hint and returns 1. Otherwise exports review_results.json, merges
    pr_meta.json, tags hunks via normalize_findings_payload(), writes
    review_findings.json, and returns 0.
    """
    report_file = Path(report_md)
    diff_file = Path(diff_path)
    meta_file = Path(meta_path) if meta_path is not None else None
    results_file = Path(output_results)
    findings_file = Path(output_findings)

    if not report_file.is_file():
        print(f"Error: Report file not found: {report_file}", file=sys.stderr)
        return 1

    raw_report = report_file.read_text(encoding="utf-8")
    report = lint_review.parse_report(raw_report)
    violations = lint_review.check_report(report, profile="pr")
    if violations:
        _print_lint_violations(violations)
        return 1

    if not diff_file.is_file():
        print(f"Error: Diff file not found: {diff_file}", file=sys.stderr)
        return 1
    if meta_file is not None and not meta_file.is_file():
        print(f"Error: Metadata file not found: {meta_file}", file=sys.stderr)
        return 1

    try:
        results_payload = lint_review.export_report_to_json(report, results_file)
        findings_payload = diff_parser.normalize_findings_payload(results_payload)
        _merge_pr_metadata(findings_payload, meta_file)
        hunks = diff_parser.parse_diff_hunks(
            diff_file.read_text(encoding="utf-8", errors="replace")
        )
        repo_dir_str = str(repo_dir) if repo_dir is not None else None
        diff_parser.tag_findings_with_hunk_status(
            findings_payload["findings"], hunks, repo_dir=repo_dir_str
        )
        findings_payload = diff_parser.normalize_findings_payload(findings_payload)
        _write_json_file(str(findings_file), findings_payload)
    except (ValueError, OSError, KeyError, TypeError) as e:
        print(f"Error finalizing review artifacts: {e}", file=sys.stderr)
        return 1

    return 0


def _run_finalize_review(args: argparse.Namespace) -> int:
    """Executes CLI post-flight review finalization."""
    if not args.report_md:
        print("Error: --finalize-review requires --report-md.", file=sys.stderr)
        return 1
    base_dir = Path(args.report_md).parent
    diff_arg = Path(args.diff) if args.diff else base_dir / "pr.diff"
    meta_arg = Path(args.meta_file) if args.meta_file else base_dir / "pr_meta.json"
    results_arg = (
        Path(args.output_results)
        if args.output_results
        else base_dir / "review_results.json"
    )
    findings_arg = (
        Path(args.output_findings)
        if args.output_findings
        else base_dir / "review_findings.json"
    )
    repo_dir_arg: Path | None = None
    if args.repo_dir:
        repo_dir_arg = Path(args.repo_dir)
    elif (base_dir / "workspace_dir.txt").is_file():
        repo_dir_arg = base_dir / "workspace_dir.txt"

    return finalize_review(
        Path(args.report_md),
        diff_arg,
        meta_arg,
        repo_dir_arg,
        results_arg,
        findings_arg,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    """Constructs the argument parser for review_engine.py."""
    parser = argparse.ArgumentParser(
        description="Prepare workspace and extract PR diff and context."
    )
    parser.add_argument("--pr", help="PR number, URL, or branch name.")
    parser.add_argument(
        "--repo", help="Optional owner/repo if not inferred from directory."
    )
    parser.add_argument(
        "--reference",
        "--repo-dir",
        dest="repo_dir",
        help="Optional local directory to use as clone reference or checkout path.",
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
    parser.add_argument(
        "--output-workspace",
        help="Optional file path to record the temporary workspace directory.",
    )
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
    parser.add_argument(
        "--input-coords", help="Path to Stage 1 pr_coords.json for --fetch-context."
    )
    parser.add_argument(
        "--input-stack",
        help="Path to the Stage 1 --output-stack file; its rebase status is merged into pr_context.json's `stack`.",
    )
    parser.add_argument(
        "--require-blind-description",
        help="Path to blind_description.md that must exist and be non-empty before --fetch-context runs.",
    )
    parser.add_argument(
        "--prepare-code-review",
        action="store_true",
        help=(
            "With --fetch-context: also run classify_artifacts --combined, stage_diffs.py, "
            "optional duckdb churn/drift, and packrat_context.py, writing code_review_prep.json "
            "and storing the result under pr_context.json['code_review_prep']. Reads --diff "
            "(default <coords dir>/pr.diff) and --repo-dir (default <coords dir>/workspace_dir.txt)."
        ),
    )
    parser.add_argument(
        "--output-prep",
        help=(
            "Optional output path for standalone code_review_prep.json when --prepare-code-review "
            "is set (default: <output-context dir>/code_review_prep.json)."
        ),
    )
    parser.add_argument(
        "--output-meta", help="Output path for resolved PR metadata JSON."
    )
    parser.add_argument(
        "--output-context",
        help="Output path for PR narrative context JSON (title, body, comments).",
    )
    parser.add_argument("--output-diff", help="Output path for extracted unified diff.")
    parser.add_argument(
        "--reconcile-description",
        action="store_true",
        help="Stage 2: deterministically splice reconciliation callouts into blind_description.md.",
    )
    parser.add_argument(
        "--blind-description",
        help="Path to Stage 1 blind_description.md for --reconcile-description.",
    )
    parser.add_argument(
        "--reconciliation",
        help="Path to Stage 2 intent_reconciliation.json for --reconcile-description.",
    )
    parser.add_argument(
        "--output-description",
        help="Output path for reconciled pr_description.md.",
    )
    parser.add_argument(
        "--context",
        help="Path to pr_context.json carrying blind_sha256 for --reconcile-description.",
    )
    parser.add_argument(
        "--expected-blind-sha256",
        help="Optional explicit SHA-256 hex digest of blind_description.md.",
    )
    parser.add_argument(
        "--finalize-review",
        action="store_true",
        help="Post-flight: lint review_results.md, export JSON, merge pr_meta.json, and tag diff hunks.",
    )
    parser.add_argument(
        "--report-md",
        help="Path to synthesized review_results.md for --finalize-review.",
    )
    parser.add_argument(
        "--diff",
        help="Path to unified diff (pr.diff) for --finalize-review.",
    )
    parser.add_argument(
        "--meta-file",
        help="Path to pr_meta.json for --finalize-review.",
    )
    parser.add_argument(
        "--output-results",
        help="Output path for canonical review_results.json in --finalize-review.",
    )
    parser.add_argument(
        "--output-findings",
        help="Output path for hunk-tagged review_findings.json in --finalize-review.",
    )
    parser.add_argument(
        "--security-scan",
        help="Scan a unified diff file (or '-' for stdin) for local execution risks.",
    )
    parser.add_argument(
        "--output-security-scan",
        help="Optional output file path for the static execution-risk scan JSON.",
    )
    parser.add_argument(
        "--check-description-budgets",
        help="Stage 1: check section budgets on blind_description.md (with optional --input-coords) and exit 0.",
    )
    parser.add_argument("--cleanup", help="Directory path to safely clean up and exit.")
    return parser


def _run_cleanup(cleanup_target: str) -> int:
    """Runs workspace cleanup and prints status."""
    if cleanup_workspace(cleanup_target):
        print(f"Successfully cleaned up temporary directory: {cleanup_target}")
        return 0
    print(f"Failed or refused to clean up directory: {cleanup_target}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    """Entrypoint for review_engine CLI."""
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.cleanup:
        return _run_cleanup(args.cleanup)
    if args.check_description_budgets:
        return _run_check_description_budgets(args)
    if args.security_scan:
        return _run_security_scan(args)
    if args.prepare_workspace:
        return _run_prepare_workspace(args)
    if args.fetch_context:
        return _run_fetch_context(args)
    if args.reconcile_description:
        return _run_reconcile_description(args)
    if args.finalize_review:
        return _run_finalize_review(args)

    if not args.pr:
        parser.error("the following arguments are required: --pr")
    if not _validate_blind_description(args.require_blind_description):
        return 1

    try:
        metadata = resolve_pr_metadata(args.pr, repo=args.repo)
    except (RuntimeError, ValueError, OSError, KeyError, TypeError) as e:
        print(f"Error resolving PR: {e}", file=sys.stderr)
        return 1

    if args.output_meta:
        _write_json_file(args.output_meta, metadata)
        print(f"Wrote PR metadata to {args.output_meta}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
