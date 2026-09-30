#!/usr/bin/env python3
"""git_stack.py - CLI tool for inspecting, checking, and cascade-rebasing GitHub PR stacks.

Usage:
  git-stack view   [--repo-dir <path>] [--target <branch|pr>] [--remote <name>] [--json]
  git-stack check  [--repo-dir <path>] [--target <branch|pr>] [--remote <name>]
  git-stack rebase [--repo-dir <path>] [--target <branch|pr>] [--remote <name>] [--dry-run]
  git-stack sync   [--repo-dir <path>] [--target <branch|pr>] [--remote <name>] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# Add parent directory to sys.path to load git_stack_lib
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import git_stack_lib as lib


def format_table(model: lib.StackModel) -> str:
    """Renders a readable formatted overview of the stack."""
    lines: List[str] = []
    lines.append(f"GitHub Stack: {model.repo_owner}/{model.repo_name} (Base: {model.root_base})")
    lines.append("=" * 70)

    for i, entry in enumerate(model.entries, start=1):
        pr_str = f"#{entry.pr_number}" if entry.pr_number else "Local Branch"
        draft_str = " [Draft]" if entry.is_draft else ""
        wt_str = f" (worktree: {entry.worktree_path})" if entry.worktree_path else ""

        if entry.is_dirty:
            status = "⊘ DIRTY WORKTREE"
        elif entry.needs_rebase:
            status = "⚠ NEEDS REBASE"
        else:
            status = "✓ UP TO DATE"

        lines.append(f"\n{i}. [{status}] {pr_str}{draft_str}: {entry.branch}")
        lines.append(f"   Base: {entry.base_ref} | State: {entry.state}")
        if entry.title:
            lines.append(f"   Title: {entry.title}")
        if entry.url:
            lines.append(f"   URL: {entry.url}")
        if wt_str:
            lines.append(f"  {wt_str}")

    lines.append("\n" + "=" * 70)
    return "\n".join(lines)


def cmd_view(args: argparse.Namespace) -> int:
    repo_dir = lib.find_git_root(Path(args.repo_dir) if args.repo_dir else None)
    model = lib.discover_stack(repo_dir, target_branch_or_pr=args.target, remote=args.remote)
    if not model:
        print(f"Error: Could not discover GitHub stack in {repo_dir}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "root_base": model.root_base,
            "owner": model.repo_owner,
            "repo": model.repo_name,
            "remote": model.remote,
            "entries": [
                {
                    "pr_number": e.pr_number,
                    "branch": e.branch,
                    "base_ref": e.base_ref,
                    "title": e.title,
                    "state": e.state,
                    "is_draft": e.is_draft,
                    "url": e.url,
                    "worktree": str(e.worktree_path) if e.worktree_path else None,
                    "needs_rebase": e.needs_rebase,
                    "is_dirty": e.is_dirty,
                }
                for e in model.entries
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(format_table(model))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    repo_dir = lib.find_git_root(Path(args.repo_dir) if args.repo_dir else None)
    model = lib.discover_stack(repo_dir, target_branch_or_pr=args.target, remote=args.remote)
    if not model:
        print(f"Error: Could not discover GitHub stack in {repo_dir}", file=sys.stderr)
        return 2

    has_issue = False
    for entry in model.entries:
        if entry.is_dirty:
            print(f"⊘ Worktree for {entry.branch} has uncommitted changes: {entry.worktree_path}")
            has_issue = True
        elif entry.needs_rebase:
            print(f"⚠ Branch {entry.branch} needs rebase onto base {entry.base_ref}")
            has_issue = True

    if has_issue:
        return 1

    if not args.quiet:
        print(f"✓ All {len(model.entries)} branches in the stack are clean and up to date.")
    return 0


def cmd_rebase(args: argparse.Namespace) -> int:
    repo_dir = lib.find_git_root(Path(args.repo_dir) if args.repo_dir else None)
    model = lib.discover_stack(repo_dir, target_branch_or_pr=args.target, remote=args.remote)
    if not model:
        print(f"Error: Could not discover GitHub stack in {repo_dir}", file=sys.stderr)
        return 2

    print(f"Starting cascade rebase for {len(model.entries)} branches in stack...")
    results = lib.cascade_rebase(repo_dir, model, dry_run=args.dry_run)

    has_error = False
    for res in results:
        b = res.get("branch")
        st = res.get("status")
        if st == "SUCCESS":
            print(f"✓ [{b}] Rebased successfully onto {res.get('rebased_onto')}")
        elif st == "SKIPPED":
            print(f"- [{b}] Skipped: {res.get('reason')}")
        elif st == "DRY_RUN":
            print(f"[DRY_RUN] [{b}] {res.get('command')} (in {res.get('worktree')})")
        elif st == "CONFLICT":
            print(f"✗ [{b}] CONFLICT during rebase in worktree: {res.get('worktree')}")
            print(f"    Details: {res.get('reason')}")
            print(f"    Please resolve conflicts in {res.get('worktree')} and run: git rebase --continue")
            has_error = True
            break
        elif st == "ERROR":
            print(f"✗ [{b}] Error: {res.get('reason')}")
            has_error = True
            break

    return 1 if has_error else 0


def cmd_sync(args: argparse.Namespace) -> int:
    repo_dir = lib.find_git_root(Path(args.repo_dir) if args.repo_dir else None)
    model = lib.discover_stack(repo_dir, target_branch_or_pr=args.target, remote=args.remote)
    if not model:
        print(f"Error: Could not discover GitHub stack in {repo_dir}", file=sys.stderr)
        return 2

    print(f"Synchronizing stack to remote '{model.remote}'...")
    results = lib.sync_stack_remotes(repo_dir, model, dry_run=args.dry_run)

    has_error = False
    for res in results:
        act = res.get("action")
        st = res.get("status")
        if act == "PUSH":
            b = res.get("branch")
            if args.dry_run:
                print(f"[DRY_RUN] Push: {res.get('command')}")
            elif st == "SUCCESS":
                print(f"✓ Pushed {b} to {model.remote}")
            else:
                print(f"✗ Push failed for {b}: {res.get('reason')}")
                has_error = True
        elif act == "EDIT_BASE":
            pr = res.get("pr")
            if args.dry_run:
                print(f"[DRY_RUN] Update PR #{pr} base: {res.get('command')}")
            elif st == "SUCCESS":
                print(f"✓ Updated PR #{pr} baseRefName on GitHub")
            else:
                print(f"✗ Failed to update PR #{pr} baseRefName: {res.get('reason')}")

    return 1 if has_error else 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="git-stack",
        description="Inspect, validate ancestry, and cascade-rebase GitHub PR stacks across worktrees.",
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    # view
    p_view = subparsers.add_parser("view", help="Inspect and display stack hierarchy.")
    p_view.add_argument("--repo-dir", help="Target git repository or worktree root.")
    p_view.add_argument("--target", help="Branch name or PR number in the stack.")
    p_view.add_argument("--remote", default="upstream", help="Git remote name (default: upstream).")
    p_view.add_argument("--json", action="store_true", help="Output machine-readable JSON.")
    p_view.set_defaults(func=cmd_view)

    # check
    p_check = subparsers.add_parser("check", help="Verify ancestry and clean worktrees.")
    p_check.add_argument("--repo-dir", help="Target git repository or worktree root.")
    p_check.add_argument("--target", help="Branch name or PR number in the stack.")
    p_check.add_argument("--remote", default="upstream", help="Git remote name (default: upstream).")
    p_check.add_argument("-q", "--quiet", action="store_true", help="Quiet output on success.")
    p_check.set_defaults(func=cmd_check)

    # rebase
    p_rebase = subparsers.add_parser("rebase", help="Cascade-rebase stack bottom-up.")
    p_rebase.add_argument("--repo-dir", help="Target git repository or worktree root.")
    p_rebase.add_argument("--target", help="Branch name or PR number in the stack.")
    p_rebase.add_argument("--remote", default="upstream", help="Git remote name (default: upstream).")
    p_rebase.add_argument("--dry-run", action="store_true", help="Simulate rebase commands.")
    p_rebase.set_defaults(func=cmd_rebase)

    # sync
    p_sync = subparsers.add_parser("sync", help="Push rebased branches and align PR bases.")
    p_sync.add_argument("--repo-dir", help="Target git repository or worktree root.")
    p_sync.add_argument("--target", help="Branch name or PR number in the stack.")
    p_sync.add_argument("--remote", default="upstream", help="Git remote name (default: upstream).")
    p_sync.add_argument("--dry-run", action="store_true", help="Simulate push and edit commands.")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
