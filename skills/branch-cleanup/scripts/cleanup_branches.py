#!/usr/bin/env python3
"""
Python script to clean up merged local branches and their tracking remote branches.
"""

import argparse
import subprocess
import sys
import os

def run_cmd(args, cwd=None):
    result = subprocess.run(args, capture_output=True, text=True, cwd=cwd)
    return result.returncode, result.stdout, result.stderr

def is_git_repo(path):
    code, _, _ = run_cmd(['git', 'rev-parse', '--is-inside-work-tree'], cwd=path)
    return code == 0

def fetch_all(path):
    print("Fetching updates from remotes...")
    code, _, err = run_cmd(['git', 'fetch', '--all', '--prune'], cwd=path)
    if code != 0:
        print(f"Warning: git fetch failed: {err.strip()}", file=sys.stderr)

def get_branches(path):
    # Formats: local name, upstream short, upstream tracking status, worktreepath
    fmt = "%(refname:short)|%(upstream:short)|%(upstream:track)|%(worktreepath)"
    code, out, err = run_cmd(['git', 'branch', f'--format={fmt}'], cwd=path)
    if code != 0:
        print(f"Error listing branches: {err.strip()}", file=sys.stderr)
        sys.exit(1)
    
    branches = []
    for line in out.strip().split('\n'):
        if not line:
            continue
        parts = line.split('|')
        if len(parts) < 4:
            continue
        branches.append({
            'name': parts[0],
            'upstream': parts[1],
            'track': parts[2],
            'worktree': parts[3]
        })
    return branches

def get_current_branch(path):
    code, out, err = run_cmd(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=path)
    if code != 0:
        return None
    return out.strip()

def is_merged(branch, main_branch, path):
    # Check using git merge-tree (robust for squash merges with multiple commits)
    code, out, err = run_cmd(['git', 'merge-tree', main_branch, branch], cwd=path)
    if code == 0:
        lines = out.strip().split('\n')
        if lines:
            tree_hash = lines[0].strip()
            # Verify it looks like a valid sha1 hash
            if len(tree_hash) == 40 and all(c in '0123456789abcdefABCDEF' for c in tree_hash):
                code_diff, out_diff, _ = run_cmd(['git', 'diff', main_branch, tree_hash], cwd=path)
                if code_diff == 0 and not out_diff.strip():
                    return True

    # Fallback 1: Check git cherry
    code_cherry, out_cherry, _ = run_cmd(['git', 'cherry', main_branch, branch], cwd=path)
    if code_cherry == 0:
        has_new_commits = False
        for line in out_cherry.strip().split('\n'):
            if line.strip().startswith('+'):
                has_new_commits = True
                break
        if not has_new_commits:
            return True
            
    # Fallback 2: Check git branch --merged
    code2, out2, _ = run_cmd(['git', 'branch', '--merged', main_branch], cwd=path)
    if code2 == 0:
        merged_branches = [b.strip().replace('*', '').strip() for b in out2.strip().split('\n')]
        return branch in merged_branches
        
    return False

def delete_local_branch(branch, path, dry_run=True):
    if dry_run:
        print(f"[Dry-Run] Would delete local branch: {branch}")
        return True
    
    # Try deleting with -d first, fallback to -D if needed (since squash merges need -D)
    code, _, err = run_cmd(['git', 'branch', '-d', branch], cwd=path)
    if code != 0:
        print(f"Standard delete failed for local branch '{branch}' (might be squash-merged). Forcing delete...")
        code, _, err = run_cmd(['git', 'branch', '-D', branch], cwd=path)
        
    if code == 0:
        print(f"Deleted local branch: {branch}")
        return True
    else:
        print(f"Error: Failed to delete local branch '{branch}': {err.strip()}", file=sys.stderr)
        return False

def delete_remote_branch(remote, remote_branch, path, dry_run=True):
    if dry_run:
        print(f"[Dry-Run] Would delete remote branch: {remote}/{remote_branch}")
        return True
    
    code, _, err = run_cmd(['git', 'push', remote, '--delete', remote_branch], cwd=path)
    if code == 0:
        print(f"Deleted remote branch: {remote}/{remote_branch}")
        return True
    else:
        print(f"Warning: Failed to delete remote branch '{remote}/{remote_branch}': {err.strip()}", file=sys.stderr)
        return False

def delete_worktree(worktree_path, repo_path, dry_run=True):
    if dry_run:
        print(f"[Dry-Run] Would remove git worktree at: {worktree_path}")
        return True
    
    code, _, err = run_cmd(['git', 'worktree', 'remove', worktree_path], cwd=repo_path)
    if code == 0:
        print(f"Removed git worktree at: {worktree_path}")
        return True
    else:
        print(f"Error: Failed to remove git worktree at '{worktree_path}': {err.strip()}", file=sys.stderr)
        return False

def main():
    parser = argparse.ArgumentParser(description="Clean up merged git branches.")
    parser.add_argument("--repo-dir", default=os.getcwd(), help="Path to the git repository (default: current directory)")
    parser.add_argument("--main-branch", default="main", help="The name of the main branch (default: main)")
    parser.add_argument("--delete", action="store_true", help="Perform actual deletion (default is dry-run)")
    
    args = parser.parse_args()
    
    repo_path = os.path.abspath(args.repo_dir)
    if not is_git_repo(repo_path):
        print(f"Error: '{repo_path}' is not a valid git repository.", file=sys.stderr)
        sys.exit(1)
        
    dry_run = not args.delete
    if dry_run:
        print("--- RUNNING IN DRY-RUN MODE (no changes will be made) ---")
        print("Use --delete flag to perform actual branch deletion.\n")
        
    fetch_all(repo_path)
    
    current_branch = get_current_branch(repo_path)
    branches = get_branches(repo_path)
    
    merged_local_count = 0
    merged_worktree_count = 0
    merged_remote_count = 0
    
    to_delete = []
    
    for b in branches:
        name = b['name']
        if name in [args.main_branch, 'master']:
            continue
            
        # Determine if merged
        merged = is_merged(name, args.main_branch, repo_path)
        if not merged:
            continue
            
        # Check if active in any worktree
        worktree = None
        if b['worktree']:
            if b['worktree'] == repo_path:
                print(f"Skipping merged branch '{name}' because it is checked out in the main worktree: {b['worktree']}")
                continue
            else:
                worktree = b['worktree']
            
        # Determine remote tracking info
        upstream = b['upstream']
        track = b['track']
        
        remote = None
        remote_branch = None
        
        if upstream:
            # Check if upstream is '[gone]'
            if '[gone]' in track:
                pass
            else:
                parts = upstream.split('/', 1)
                if len(parts) == 2:
                    remote = parts[0]
                    remote_branch = parts[1]
                    
        to_delete.append({
            'local': name,
            'remote': remote,
            'remote_branch': remote_branch,
            'worktree': worktree
        })
        
    if not to_delete:
        print("No merged branches found to clean up.")
        return
        
    print("\nFound the following merged branches to clean up:")
    for item in to_delete:
        local_str = item['local']
        remote_str = f"remote: {item['remote']}/{item['remote_branch']}" if item['remote'] else "remote: None"
        worktree_str = f"worktree: {item['worktree']}" if item['worktree'] else "worktree: None"
        print(f" - Local: {local_str:<35} | {remote_str:<40} | {worktree_str}")
        
    print()
    for item in to_delete:
        success = True
        # If there's a linked worktree, delete it first
        if item['worktree']:
            success = delete_worktree(item['worktree'], repo_path, dry_run)
            if success:
                merged_worktree_count += 1
            
        if success:
            # 1. Delete local branch
            success = delete_local_branch(item['local'], repo_path, dry_run)
            if success:
                merged_local_count += 1
                
            # 2. Delete remote branch (if tracking remote exists and was not already gone)
            if success and item['remote'] and item['remote_branch']:
                rem_success = delete_remote_branch(item['remote'], item['remote_branch'], repo_path, dry_run)
                if rem_success:
                    merged_remote_count += 1
                
    print("\n--- Summary ---")
    if dry_run:
        print(f"Would delete {merged_local_count} local branches, {merged_worktree_count} worktrees, and {merged_remote_count} remote branches.")
    else:
        print(f"Successfully deleted {merged_local_count} local branches, {merged_worktree_count} worktrees, and {merged_remote_count} remote branches.")

if __name__ == '__main__':
    main()
