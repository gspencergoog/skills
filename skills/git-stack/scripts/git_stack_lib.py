#!/usr/bin/env python3
"""git_stack_lib.py - Core library for GitHub stack inspection, validation, and cascade rebasing.

Provides structured models and operations to manage GitHub PR stacks across
linked git worktrees without manual merge-base calculations.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class StackEntry:
    """Represents a single pull request or branch in a stack."""

    branch: str
    base_ref: str
    pr_number: Optional[int] = None
    title: str = ""
    state: str = "OPEN"
    is_draft: bool = False
    url: str = ""
    worktree_path: Optional[Path] = None
    local_sha: Optional[str] = None
    remote_sha: Optional[str] = None
    parent_sha: Optional[str] = None
    is_ancestor: bool = True
    needs_rebase: bool = False
    is_dirty: bool = False


@dataclass
class StackModel:
    """Represents an ordered stack of branches/PRs from base integration branch to tip."""

    root_base: str
    entries: List[StackEntry] = field(default_factory=list)
    repo_owner: str = ""
    repo_name: str = ""
    remote: str = "upstream"

    def find_entry(self, branch: str) -> Optional[StackEntry]:
        for entry in self.entries:
            if entry.branch == branch:
                return entry
        return None


def run_cmd(
    cmd: Sequence[str],
    cwd: Optional[Path] = None,
    env_override: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Runs a subprocess command with environment sanitization."""
    env = dict(os.environ)
    # Clear GITHUB_TOKEN overrides unless explicitly provided
    if "GITHUB_TOKEN" in env and (not env_override or "GITHUB_TOKEN" not in env_override):
        del env["GITHUB_TOKEN"]
    if env_override:
        env.update(env_override)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except OSError as e:
        return 1, "", str(e)


def find_git_root(cwd: Optional[Path] = None) -> Path:
    """Resolves the root directory of the current git repository or worktree."""
    start_dir = cwd or Path.cwd()
    code, stdout, _ = run_cmd(["git", "rev-parse", "--show-toplevel"], cwd=start_dir)
    if code == 0 and stdout:
        return Path(stdout).resolve()
    return start_dir.resolve()


def get_git_common_dir(repo_dir: Path) -> Path:
    """Resolves the primary .git directory across linked worktrees."""
    code, stdout, _ = run_cmd(["git", "rev-parse", "--git-common-dir"], cwd=repo_dir)
    if code == 0 and stdout:
        common_path = Path(stdout)
        if not common_path.is_absolute():
            common_path = repo_dir / common_path
        return common_path.resolve()
    dot_git = repo_dir / ".git"
    return dot_git.resolve()


def get_worktree_map(repo_dir: Path) -> Dict[str, Path]:
    """Parses `git worktree list --porcelain` to map branch names to worktree paths."""
    code, stdout, _ = run_cmd(["git", "worktree", "list", "--porcelain"], cwd=repo_dir)
    if code != 0 or not stdout:
        return {}

    worktrees: Dict[str, Path] = {}
    current_worktree: Optional[Path] = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            current_worktree = None
            continue
        if line.startswith("worktree "):
            current_worktree = Path(line[9:].strip()).resolve()
        elif line.startswith("branch ") and current_worktree:
            branch_ref = line[7:].strip()
            # branch refs/heads/feature-name -> feature-name
            branch_name = branch_ref.replace("refs/heads/", "")
            worktrees[branch_name] = current_worktree

    return worktrees


def is_worktree_dirty(worktree_path: Path) -> bool:
    """Checks whether a worktree has uncommitted or untracked changes."""
    code, stdout, _ = run_cmd(["git", "status", "--porcelain"], cwd=worktree_path)
    return code == 0 and bool(stdout.strip())


def parse_repo_slug(repo_dir: Path, remote: str = "upstream") -> Tuple[str, str]:
    """Extracts GitHub owner and repository name from the remote URL."""
    code, stdout, _ = run_cmd(["git", "remote", "get-url", remote], cwd=repo_dir)
    if code != 0 or not stdout:
        # Fallback to origin if upstream not found
        code, stdout, _ = run_cmd(["git", "remote", "get-url", "origin"], cwd=repo_dir)
        if code != 0 or not stdout:
            return "", ""

    url = stdout.strip()
    # Matches: git@github.com:owner/repo.git or https://github.com/owner/repo.git
    m = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)(?:\.git)?$", url)
    if m:
        return m.group("owner"), m.group("repo")
    return "", ""


def verify_ancestry(repo_dir: Path, base_ref: str, head_ref: str) -> bool:
    """Asserts whether head_ref contains base_ref (git merge-base --is-ancestor)."""
    code, _, _ = run_cmd(["git", "merge-base", "--is-ancestor", base_ref, head_ref], cwd=repo_dir)
    return code == 0


def compute_merge_base(repo_dir: Path, ref_a: str, ref_b: str) -> Optional[str]:
    """Calculates git merge-base between two references."""
    code, stdout, _ = run_cmd(["git", "merge-base", ref_a, ref_b], cwd=repo_dir)
    if code == 0 and stdout:
        return stdout.strip()
    return None


def get_commit_sha(repo_dir: Path, ref: str) -> Optional[str]:
    """Resolves commit SHA for a reference."""
    code, stdout, _ = run_cmd(["git", "rev-parse", "--verify", ref], cwd=repo_dir)
    if code == 0 and stdout:
        return stdout.strip()
    return None


def get_patch_id(repo_dir: Path, sha: str) -> Optional[str]:
    """Computes stable git patch-id for a commit."""
    p_show = subprocess.Popen(
        ["git", "show", sha],
        cwd=str(repo_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    p_patch = subprocess.Popen(
        ["git", "patch-id", "--stable"],
        cwd=str(repo_dir),
        stdin=p_show.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if p_show.stdout:
        p_show.stdout.close()
    out, _ = p_patch.communicate()
    if p_patch.returncode == 0 and out:
        parts = out.decode("utf-8").strip().split()
        if parts:
            return parts[0]
    return None


def fetch_stack_graphql(
    repo_dir: Path,
    pr_number: int,
    owner: str,
    repo: str,
) -> Optional[Dict[str, Any]]:
    """Fetches GitHub pullRequest.stack information via GraphQL."""
    query = """
    query($owner: String!, $name: String!, $pr: Int!) {
      repository(owner: $owner, name: $name) {
        pullRequest(number: $pr) {
          number
          title
          state
          isDraft
          headRefName
          baseRefName
          url
          stack {
            number
            baseRefName
            entries(first: 30) {
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
    cmd = [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={repo}",
        "-F",
        f"pr={pr_number}",
    ]
    code, stdout, _ = run_cmd(cmd, cwd=repo_dir)
    if code != 0 or not stdout:
        return None

    try:
        data = json.loads(stdout)
        return data.get("data", {}).get("repository", {}).get("pullRequest")
    except json.JSONDecodeError:
        return None


def get_current_branch(repo_dir: Path) -> Optional[str]:
    """Returns the name of the currently checked out branch."""
    code, stdout, _ = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir)
    if code == 0 and stdout and stdout != "HEAD":
        return stdout.strip()
    return None


def find_pr_for_branch(repo_dir: Path, branch: str, remote: str = "upstream") -> Optional[int]:
    """Finds open PR number associated with a branch name."""
    cmd = [
        "gh",
        "pr",
        "list",
        "--head",
        branch,
        "--json",
        "number,state",
        "--limit",
        "1",
    ]
    code, stdout, _ = run_cmd(cmd, cwd=repo_dir)
    if code == 0 and stdout:
        try:
            prs = json.loads(stdout)
            if prs and isinstance(prs, list):
                return prs[0].get("number")
        except json.JSONDecodeError:
            pass
    return None


def discover_stack(
    repo_dir: Path,
    target_branch_or_pr: Optional[str] = None,
    remote: str = "upstream",
) -> Optional[StackModel]:
    """Discovers the stack hierarchy for a branch or PR."""
    owner, repo_name = parse_repo_slug(repo_dir, remote=remote)
    worktree_map = get_worktree_map(repo_dir)

    target = target_branch_or_pr or get_current_branch(repo_dir)
    if not target:
        return None

    pr_num: Optional[int] = None
    if target.isdigit():
        pr_num = int(target)
    else:
        pr_num = find_pr_for_branch(repo_dir, target, remote=remote)

    if not pr_num:
        return None

    pr_data = fetch_stack_graphql(repo_dir, pr_num, owner=owner, repo=repo_name)
    if not pr_data:
        return None

    stack_obj = pr_data.get("stack")
    entries_list: List[Dict[str, Any]] = []

    if stack_obj and stack_obj.get("entries"):
        root_base = stack_obj.get("baseRefName", "main")
        for node in stack_obj["entries"].get("nodes", []):
            pr_node = node.get("pullRequest")
            if pr_node:
                entries_list.append(pr_node)
    else:
        # Fallback for manual single PR / unlinked stacks
        root_base = pr_data.get("baseRefName", "main")
        entries_list.append(pr_data)

    model = StackModel(
        root_base=root_base,
        repo_owner=owner,
        repo_name=repo_name,
        remote=remote,
    )

    prev_ref = root_base
    for item in entries_list:
        branch = item.get("headRefName", "")
        base_ref = item.get("baseRefName", prev_ref)
        prev_ref = branch

        wt_path = worktree_map.get(branch)
        is_dirty = is_worktree_dirty(wt_path) if wt_path else False

        local_sha = get_commit_sha(repo_dir, branch)
        remote_sha = get_commit_sha(repo_dir, f"{remote}/{branch}")

        # Ancestry validation against base_ref
        parent_candidate = f"{remote}/{base_ref}" if get_commit_sha(repo_dir, f"{remote}/{base_ref}") else base_ref
        head_candidate = f"{remote}/{branch}" if get_commit_sha(repo_dir, f"{remote}/{branch}") else branch

        is_anc = True
        needs_reb = False
        if get_commit_sha(repo_dir, parent_candidate) and get_commit_sha(repo_dir, head_candidate):
            is_anc = verify_ancestry(repo_dir, parent_candidate, head_candidate)
            needs_reb = not is_anc

        entry = StackEntry(
            branch=branch,
            base_ref=base_ref,
            pr_number=item.get("number"),
            title=item.get("title", ""),
            state=item.get("state", "OPEN"),
            is_draft=item.get("isDraft", False),
            url=item.get("url", ""),
            worktree_path=wt_path,
            local_sha=local_sha,
            remote_sha=remote_sha,
            is_ancestor=is_anc,
            needs_rebase=needs_reb,
            is_dirty=is_dirty,
        )
        model.entries.append(entry)

    return model


def cascade_rebase(
    repo_dir: Path,
    model: StackModel,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Performs bottom-up cascade rebasing across linked worktrees or temporary worktree."""
    results: List[Dict[str, Any]] = []
    parent_branch = model.root_base

    # Pre-capture old merge bases before any modification
    old_bases: Dict[str, str] = {}
    for i, entry in enumerate(model.entries):
        p_ref = model.root_base if i == 0 else model.entries[i - 1].branch
        parent_ref = f"{model.remote}/{p_ref}" if get_commit_sha(repo_dir, f"{model.remote}/{p_ref}") else p_ref
        child_ref = f"{model.remote}/{entry.branch}" if get_commit_sha(repo_dir, f"{model.remote}/{entry.branch}") else entry.branch

        mb = compute_merge_base(repo_dir, parent_ref, child_ref)
        if mb:
            old_bases[entry.branch] = mb

    # Bottom-up rebase execution
    temp_worktree: Optional[Path] = None
    common_dir = get_git_common_dir(repo_dir)

    try:
        for entry in model.entries:
            if not entry.needs_rebase:
                parent_branch = entry.branch
                results.append({
                    "branch": entry.branch,
                    "status": "SKIPPED",
                    "reason": "Already up to date with base",
                })
                continue

            old_base = old_bases.get(entry.branch)
            if not old_base:
                results.append({
                    "branch": entry.branch,
                    "status": "ERROR",
                    "reason": f"Could not determine merge-base with {parent_branch}",
                })
                break

            # Locate execution directory (existing worktree or temporary detached worktree)
            target_worktree = entry.worktree_path
            created_temp = False

            rebase_cmd = ["git", "rebase", "--onto", parent_branch, old_base]
            if dry_run:
                target_display = str(target_worktree) if target_worktree else f"(detached temp worktree for {entry.branch})"
                results.append({
                    "branch": entry.branch,
                    "status": "DRY_RUN",
                    "command": " ".join(rebase_cmd),
                    "worktree": target_display,
                })
                parent_branch = entry.branch
                continue

            if not target_worktree or not target_worktree.exists():
                temp_worktree = common_dir / "git-stack-temp-worktree"
                if temp_worktree.exists():
                    run_cmd(["git", "worktree", "remove", "--force", str(temp_worktree)], cwd=repo_dir)

                code, _, err = run_cmd(
                    ["git", "worktree", "add", "--detach", str(temp_worktree), entry.branch],
                    cwd=repo_dir,
                )
                if code != 0:
                    results.append({
                        "branch": entry.branch,
                        "status": "ERROR",
                        "reason": f"Failed to spawn temporary worktree: {err}",
                    })
                    break
                target_worktree = temp_worktree
                created_temp = True

            if is_worktree_dirty(target_worktree):
                results.append({
                    "branch": entry.branch,
                    "status": "ERROR",
                    "reason": f"Worktree at {target_worktree} has uncommitted changes",
                })
                break

            code, out, err = run_cmd(rebase_cmd, cwd=target_worktree)
            if code != 0:
                results.append({
                    "branch": entry.branch,
                    "status": "CONFLICT",
                    "reason": err or out,
                    "worktree": str(target_worktree),
                })
                # Halt cascade on conflict to let user/agent resolve
                break

            # Clean up temporary worktree if spawned
            if created_temp and temp_worktree and temp_worktree.exists():
                run_cmd(["git", "worktree", "remove", "--force", str(temp_worktree)], cwd=repo_dir)
                temp_worktree = None

            results.append({
                "branch": entry.branch,
                "status": "SUCCESS",
                "rebased_onto": parent_branch,
            })
            parent_branch = entry.branch

    finally:
        if temp_worktree and temp_worktree.exists():
            run_cmd(["git", "worktree", "remove", "--force", str(temp_worktree)], cwd=repo_dir)

    return results


def sync_stack_remotes(
    repo_dir: Path,
    model: StackModel,
    dry_run: bool = False,
) -> List[Dict[str, Any]]:
    """Pushes rebased stack branches with force-with-lease and updates PR base branches."""
    results: List[Dict[str, Any]] = []

    for entry in model.entries:
        push_cmd = ["git", "push", "--force-with-lease", model.remote, entry.branch]
        if dry_run:
            results.append({
                "branch": entry.branch,
                "action": "PUSH",
                "command": " ".join(push_cmd),
            })
        else:
            code, out, err = run_cmd(push_cmd, cwd=repo_dir)
            if code != 0:
                results.append({
                    "branch": entry.branch,
                    "action": "PUSH",
                    "status": "ERROR",
                    "reason": err or out,
                })
                continue
            results.append({
                "branch": entry.branch,
                "action": "PUSH",
                "status": "SUCCESS",
            })

        # Check if PR baseRefName needs retargeting on GitHub
        if entry.pr_number:
            edit_cmd = ["gh", "pr", "edit", str(entry.pr_number), "--base", entry.base_ref]
            if dry_run:
                results.append({
                    "pr": entry.pr_number,
                    "action": "EDIT_BASE",
                    "command": " ".join(edit_cmd),
                })
            else:
                code, out, err = run_cmd(edit_cmd, cwd=repo_dir)
                results.append({
                    "pr": entry.pr_number,
                    "action": "EDIT_BASE",
                    "status": "SUCCESS" if code == 0 else "ERROR",
                    "reason": err or out if code != 0 else "",
                })

    return results
