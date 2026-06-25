#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import subprocess
import json
import sys
import argparse
import os
import re
import shutil
from collections import defaultdict
from datetime import datetime

def run_cmd(args, cwd=None):
    try:
        res = subprocess.run(args, capture_output=True, text=True, check=True, cwd=cwd)
        return res.stdout.strip()
    except subprocess.CalledProcessError as e:
        return ""

def get_repo_info(cwd):
    # Try using gh repo view first as it handles forks/upstream correctly
    if shutil.which("gh"):
        repo_json = run_cmd(["gh", "repo", "view", "--json", "owner,name"], cwd=cwd)
        if repo_json:
            try:
                data = json.loads(repo_json)
                return data["owner"]["login"], data["name"]
            except Exception:
                pass
                
    # Fallback to git remote url parsing
    remote_url = run_cmd(["git", "remote", "get-url", "origin"], cwd=cwd)
    if not remote_url:
        return None, None
    
    match = re.search(r"github\.com[:/]([^/]+)/([^.]+)(?:\.git)?", remote_url)
    if match:
        return match.group(1), match.group(2)
    return None, None

def get_default_branch(cwd):
    origin_head = run_cmd(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=cwd)
    if origin_head:
        return origin_head.split("/")[-1]
    
    branches = run_cmd(["git", "branch", "-r"], cwd=cwd)
    if "origin/main" in branches:
        return "main"
    if "origin/master" in branches:
        return "master"
    return "main"

def get_changed_files_from_range(commit_range, cwd):
    files_str = run_cmd(["git", "diff", "--name-only", commit_range], cwd=cwd)
    if not files_str:
        return []
    return [f.strip() for f in files_str.splitlines() if f.strip()]

def get_pr_details(pr_number, repo, cwd):
    if not shutil.which("gh"):
        return None
    
    cmd = ["gh", "pr", "view", str(pr_number), "--json", "author,files,title,baseRefName"]
    if repo:
        cmd.extend(["--repo", repo])
    
    pr_json = run_cmd(cmd, cwd=cwd)
    if not pr_json:
        return None
    
    try:
        return json.loads(pr_json)
    except json.JSONDecodeError:
        return None

def get_collaborators(repo, cwd):
    if not shutil.which("gh") or not repo:
        return []
    
    collabs_json = run_cmd(["gh", "api", f"repos/{repo}/assignees", "--paginate"], cwd=cwd)
    if not collabs_json:
        return []
    
    try:
        data = json.loads(collabs_json)
        if isinstance(data, list):
            return [c["login"] for c in data if "login" in c]
    except Exception:
        pass
    
    logins = re.findall(r'"login":\s*"([^"]+)"', collabs_json)
    return list(set(logins))

def get_file_contributors(filepath, cwd):
    log_str = run_cmd(["git", "log", "-n", "50", "--format=%H|%ae|%an|%at", "--", filepath], cwd=cwd)
    if not log_str:
        return []
    
    contributors = []
    for line in log_str.splitlines():
        parts = line.strip().split("|")
        if len(parts) == 4:
            sha, email, name, timestamp = parts
            contributors.append({
                "sha": sha.strip(),
                "email": email.strip(),
                "name": name.strip(),
                "timestamp": int(timestamp)
            })
    return contributors

def map_git_to_github(git_email, git_name, github_logins):
    email_prefix = git_email.split("@")[0].lower()
    email_norm = re.sub(r"[._-]", "", email_prefix)
    name_clean = git_name.lower().replace(" ", "")
    name_norm = re.sub(r"[._-]", "", name_clean)
    
    # Try exact match after normalization
    for login in github_logins:
        login_lower = login.lower()
        login_norm = re.sub(r"[._-]", "", login_lower)
        if login_norm == email_norm or login_norm == name_norm:
            return login
            
    # Try substring match after normalization
    for login in github_logins:
        login_lower = login.lower()
        login_norm = re.sub(r"[._-]", "", login_lower)
        if login_norm in email_norm or email_norm in login_norm:
            return login
        if login_norm in name_norm or name_norm in login_norm:
            return login
            
    if "noreply" not in git_email and "github" not in git_email:
        return email_prefix
        
    return None

def resolve_username(sha, email, name, repo, cwd, github_logins, cache):
    """Resolves a git author's email to a GitHub username using Commit API or local fallback."""
    if email in cache:
        return cache[email]
        
    # 1. Try to resolve via GitHub Commit API (100% reliable)
    if sha and repo and shutil.which("gh"):
        username = run_cmd(["gh", "api", f"repos/{repo}/commits/{sha}", "-q", ".author.login"], cwd=cwd)
        if username and not username.startswith("error") and "not found" not in username.lower() and username.strip():
            resolved = username.strip()
            cache[email] = resolved
            return resolved
            
    # 2. Fallback to local heuristic matching
    username = map_git_to_github(email, name, github_logins)
    if username:
        cache[email] = username
        return username
        
    return None

def main():
    parser = argparse.ArgumentParser(description="Recommend PR reviewers based on git history and ownership.")
    parser.add_argument("--pr", type=int, help="GitHub Pull Request number to analyze.")
    parser.add_argument("--branch", help="Local or remote branch name to compare against main.")
    parser.add_argument("--compare", help="Custom git commit range (e.g. main...feature).")
    parser.add_argument("--repo", help="Target GitHub repository (owner/repo).")
    parser.add_argument("--dir", default=".", help="Path to local git repository.")
    args = parser.parse_args()
    
    cwd = os.path.abspath(args.dir)
    # Check if the directory is a git repository (supporting both standard and worktree setups)
    if not run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd) == "true":
        print(f"Error: {cwd} is not a git repository.", file=sys.stderr)
        sys.exit(1)
        
    owner, repo_name = get_repo_info(cwd)
    repo = args.repo or (f"{owner}/{repo_name}" if owner and repo_name else None)
    
    changed_files = []
    pr_author = None
    pr_title = ""
    base_branch = get_default_branch(cwd)
    
    if args.pr:
        print(f"Fetching details for PR #{args.pr} from {repo}...", file=sys.stderr)
        pr_details = get_pr_details(args.pr, repo, cwd)
        if pr_details:
            changed_files = [f["path"] for f in pr_details.get("files", [])]
            pr_author = pr_details.get("author", {}).get("login")
            pr_title = pr_details.get("title", "")
            base_branch = pr_details.get("baseRefName", base_branch)
            print(f"PR Title: {pr_title}", file=sys.stderr)
            print(f"PR Author: {pr_author}", file=sys.stderr)
        else:
            print(f"Warning: Could not fetch PR #{args.pr} details. Falling back to local git analysis.", file=sys.stderr)
            
    if not changed_files:
        if args.compare:
            print(f"Analyzing changes in range: {args.compare}...", file=sys.stderr)
            changed_files = get_changed_files_from_range(args.compare, cwd)
        elif args.branch:
            print(f"Analyzing changes in branch: {args.branch} against origin/{base_branch}...", file=sys.stderr)
            changed_files = get_changed_files_from_range(f"origin/{base_branch}...{args.branch}", cwd)
        else:
            current_branch = run_cmd(["git", "branch", "--show-current"], cwd=cwd)
            if current_branch and current_branch != base_branch:
                print(f"Auto-detected branch: {current_branch}. Comparing with origin/{base_branch}...", file=sys.stderr)
                changed_files = get_changed_files_from_range(f"origin/{base_branch}...{current_branch}", cwd)
            else:
                print(f"Analyzing uncommitted changes and last commit on {base_branch}...", file=sys.stderr)
                changed_files = get_changed_files_from_range("HEAD~1", cwd)
                
    if not changed_files:
        print("No changed files found to analyze.", file=sys.stderr)
        sys.exit(0)
        
    print(f"Found {len(changed_files)} changed files.", file=sys.stderr)
    
    github_logins = get_collaborators(repo, cwd)
    print(f"Fetched {len(github_logins)} repository contributors/collaborators.", file=sys.stderr)
    
    file_ownership = {}
    candidate_scores = defaultdict(lambda: {"commits": 0, "recency": 0, "files": set(), "emails": set(), "names": set()})
    email_to_username_cache = {}
    
    now = int(datetime.now().timestamp())
    
    for filepath in changed_files:
        print(f"Analyzing history of {filepath}...", file=sys.stderr)
        contributors = get_file_contributors(filepath, cwd)
        file_ownership[filepath] = contributors
        
        for idx, contrib in enumerate(contributors):
            sha = contrib.get("sha")
            email = contrib["email"]
            name = contrib["name"]
            timestamp = contrib["timestamp"]
            
            gh_username = resolve_username(sha, email, name, repo, cwd, github_logins, email_to_username_cache)
            if not gh_username:
                continue
                
            if pr_author and gh_username.lower() == pr_author.lower():
                continue
                
            days_ago = (now - timestamp) / 86400
            recency_score = max(0, 10 - (days_ago / 30))
            
            cand = candidate_scores[gh_username]
            cand["commits"] += 1
            cand["recency"] = max(cand["recency"], recency_score)
            cand["files"].add(filepath)
            cand["emails"].add(email)
            cand["names"].add(name)
            
    dir_ownership = defaultdict(list)
    for filepath in changed_files:
        dirpath = os.path.dirname(filepath)
        if not dirpath:
            continue
            
        dir_contributors = get_file_contributors(dirpath, cwd)
        for contrib in dir_contributors:
            sha = contrib.get("sha")
            email = contrib["email"]
            name = contrib["name"]
            
            gh_username = resolve_username(sha, email, name, repo, cwd, github_logins, email_to_username_cache)
            if gh_username and (not pr_author or gh_username.lower() != pr_author.lower()):
                cand = candidate_scores[gh_username]
                if filepath not in cand["files"]:
                    cand["commits"] += 0.2
                    cand["files"].add(f"{dirpath}/*")
                    cand["emails"].add(email)
                    cand["names"].add(name)

    ranked_candidates = []
    for username, data in candidate_scores.items():
        direct_files = {f for f in data["files"] if not f.endswith("/*")}
        dir_files = data["files"] - direct_files
        
        score = (len(direct_files) * 10) + (data["commits"] * 2) + data["recency"] + (len(dir_files) * 2)
        
        ranked_candidates.append({
            "username": username,
            "score": score,
            "direct_files_count": len(direct_files),
            "total_commits": data["commits"],
            "recency": data["recency"],
            "files": sorted(list(data["files"])),
            "display_name": next(iter(data["names"])) if data["names"] else username,
            "email": next(iter(data["emails"])) if data["emails"] else ""
        })
        
    ranked_candidates.sort(key=lambda x: x["score"], reverse=True)
    
    print("\n## Reviewer Recommendation Report\n")
    
    if pr_title:
        print(f"**PR Target**: {pr_title} (by @{pr_author})\n")
    
    print("### Changed Files")
    for f in changed_files:
        print(f"- `{f}`")
    print()
    
    primary = []
    secondary = []
    
    for cand in ranked_candidates:
        if cand["direct_files_count"] > 0:
            primary.append(cand)
        else:
            secondary.append(cand)
            
    print("### Primary Reviewer Suggestions")
    if not primary:
        print("No primary candidates identified (no historical commits found directly on the changed files).")
    else:
        print("These users have directly modified the changed files recently:")
        print("| Reviewer | Score | Changed Files Matched | Historical Commits | Contact |")
        print("| --- | --- | --- | --- | --- |")
        for p in primary[:4]:
            matched_files_desc = ", ".join([f"`{os.path.basename(f)}`" for f in p["files"] if not f.endswith("/*")])
            print(f"| **@{p['username']}** ({p['display_name']}) | {p['score']:.1f} | {matched_files_desc} | {int(p['total_commits'])} | `{p['email']}` |")
            
    print("\n### Secondary Reviewer Suggestions")
    if not secondary and len(primary) <= 4:
        print("No secondary candidates identified.")
    else:
        sec_candidates = secondary[:4] + primary[4:8]
        sec_candidates.sort(key=lambda x: x["score"], reverse=True)
        
        if not sec_candidates:
            print("No secondary candidates found.")
        else:
            print("These users are active in the same directories or have relevant historical contributions:")
            print("| Reviewer | Score | Relevant Directories / Files | Commits | Contact |")
            print("| --- | --- | --- | --- | --- |")
            for s in sec_candidates:
                matched_files_desc = ", ".join([f"`{f}`" for f in s["files"][:2]])
                if len(s["files"]) > 2:
                    matched_files_desc += "..."
                print(f"| **@{s['username']}** ({s['display_name']}) | {s['score']:.1f} | {matched_files_desc} | {s['total_commits']:.1f} | `{s['email']}` |")
                
    all_suggested = [p['username'] for p in primary[:2]] + [s['username'] for s in secondary[:1]]
    all_suggested = [u for u in all_suggested if u]
    
    if all_suggested and shutil.which("gh"):
        suggested_str = ",".join(all_suggested)
        pr_arg = str(args.pr) if args.pr else "<pr-number>"
        print(f"\n### Suggested Command to Assign Reviewers\n")
        print(f"```bash")
        print(f"gh pr edit {pr_arg} --add-reviewer {suggested_str}")
        print(f"```")

if __name__ == "__main__":
    main()
