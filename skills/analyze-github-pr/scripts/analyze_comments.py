#!/usr/bin/env python3
import json
import subprocess
import re
import sys
import os
import argparse

from utils import run_cmd

def get_repo_info():
    for remote in ["upstream", "origin"]:
        try:
            url = run_cmd(["git", "remote", "get-url", remote])
            m = re.search(r'(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+)', url)
            if m:
                owner = m.group(1)
                repo = m.group(2).strip()
                if repo.endswith(".git"):
                    repo = repo[:-4]
                return owner, repo
        except Exception:
            continue
    raise Exception("Could not determine repository owner and name from git remotes.")

def get_pr_number():
    try:
        pr_num = run_cmd(["gh", "pr", "view", "--json", "number", "--jq", ".number"])
        return int(pr_num)
    except Exception:
        raise Exception("Could not find an active PR for the current branch.")

def fetch_pr_data(owner, repo, pr_number):
    query = """
    fragment threadFields on PullRequestReviewThread {
      id
      isResolved
      isOutdated
      path
      line
      originalLine
      subjectType
      comments(first: 50) {
        nodes {
          id
          body
          isMinimized
          minimizedReason
          author {
            login
          }
          createdAt
        }
      }
    }
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) {
          body
          headRefName
          baseRefName
          reviewThreads(first: 100) {
            nodes {
              ...threadFields
            }
          }
        }
      }
    }
    """
    
    cmd = [
        "gh", "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"repo={repo}",
        "-F", f"pr={pr_number}"
    ]
    
    output = run_cmd(cmd)
    data = json.loads(output)
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    pr_data = data["data"]["repository"]["pullRequest"]
    nodes = pr_data["reviewThreads"]["nodes"]
        
    seen = set()
    unique_nodes = []
    for node in nodes:
        if node["id"] not in seen:
            seen.add(node["id"])
            unique_nodes.append(node)
            
    return pr_data.get("body", ""), pr_data.get("headRefName", ""), pr_data.get("baseRefName", ""), unique_nodes


def parse_suggestion(body):
    pattern = r"```suggestion\s*(.*?)\s*```"
    match = re.search(pattern, body, re.DOTALL)
    if match:
        return match.group(1)
    return None

def check_if_addressed(path, line, suggestion):
    if not os.path.exists(path):
        return "File not found"
    if not line:
        return "Pending review"
    
    try:
        with open(path, "r") as f:
            lines = f.readlines()
    except Exception:
        return "Unreadable file"
    
    idx = line - 1
    if idx < 0 or idx >= len(lines):
        return "Line index out of bounds"
    
    if suggestion:
        suggested_lines = [l.strip() for l in suggestion.strip().split("\n")]
        file_slice = [l.strip() for l in lines[idx : idx + len(suggested_lines)]]
        if file_slice == suggested_lines:
            return "Addressed (matches suggestion)"
        else:
            return "Unaddressed (does not match suggestion)"
    
    return "Pending review"

def get_modified_lines(base_branch="main"):
    candidates = []
    if base_branch:
        if "/" in base_branch:
            candidates.append(base_branch)
        else:
            candidates.extend([f"origin/{base_branch}", f"upstream/{base_branch}", base_branch])
    candidates.extend(["origin/main", "origin/master", "main", "master"])
    
    diff_output = None
    for candidate in candidates:
        try:
            merge_base = run_cmd(["git", "merge-base", candidate, "HEAD"])
            diff_output = run_cmd(["git", "diff", f"{merge_base}..HEAD", "-U0"])
            break
        except Exception:
            continue
            
    if diff_output is None:
        try:
            diff_output = run_cmd(["git", "diff", "HEAD", "-U0"])
        except Exception:
            return {}
            
    modified_lines = {}
    current_file = None
    
    for line in diff_output.split("\n"):
        if line.startswith("diff --git"):
            match = re.match(r"diff --git a/(.*) b/(.*)", line)
            if match:
                current_file = match.group(2)
                modified_lines[current_file] = set()
        elif line.startswith("@@") and current_file:
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                new_start = int(match.group(1))
                new_len = int(match.group(2)) if match.group(2) else 1
                for l in range(new_start, new_start + new_len):
                    modified_lines[current_file].add(l)
                    
    return modified_lines

def truncate_log(log_text):
    if not log_text:
        return ""
    lines = log_text.splitlines()
    N = len(lines)
    if N <= 100:
        return log_text
        
    keep_indices = set()
    
    # Keep first 15 lines
    for i in range(min(15, N)):
        keep_indices.add(i)
        
    # Keep last 85 lines
    for i in range(max(0, N - 85), N):
        keep_indices.add(i)
        
    # Scan for keywords
    keywords = ["fail", "error", "unexpected", "deprecated", "warning"]
    for i in range(N):
        line_lower = lines[i].lower()
        if any(kw in line_lower for kw in keywords):
            start = max(0, i - 30)
            end = min(N - 1, i + 10)
            for j in range(start, end + 1):
                keep_indices.add(j)
                
    sorted_keep = sorted(list(keep_indices))
    if len(sorted_keep) == N:
        return log_text
        
    truncated_lines = []
    for idx, line_idx in enumerate(sorted_keep):
        if idx > 0 and line_idx > sorted_keep[idx - 1] + 1:
            elided_count = line_idx - sorted_keep[idx - 1] - 1
            truncated_lines.append(f"... [ELIDED {elided_count} LINES] ...")
        truncated_lines.append(lines[line_idx])
        
    return "\n".join(truncated_lines)

def fetch_failed_checks_logs(pr_number):
    try:
        checks_output = run_cmd(["gh", "pr", "checks", str(pr_number), "--json", "name,state,bucket,link,workflow"])
        checks = json.loads(checks_output)
    except Exception as e:
        if "no checks reported" in str(e).lower():
            return []
        return []
        
    failed_checks = [c for c in checks if c.get("bucket") == "fail"]
    output_checks = []
    
    for check in failed_checks:
        name = check.get("name", "Unknown Check")
        link = check.get("link", "")
        state = check.get("state", "FAILED")
        workflow = check.get("workflow", "")
        
        logs = ""
        match = re.search(r'/actions/runs/(\d+)', link)
        if match:
            run_id = match.group(1)
            try:
                raw_logs = run_cmd(["gh", "run", "view", run_id, "--log-failed"])
                logs = truncate_log(raw_logs)
            except Exception as e:
                logs = f"Failed to fetch logs: {e}"
        else:
            logs = f"Non-GitHub Actions run. Inspect details at: {link}"
            
        output_checks.append({
            "name": name,
            "link": link,
            "state": state,
            "workflow": workflow,
            "logs": logs
        })
        
    return output_checks

def analyze(include_all=False):
    owner, repo = get_repo_info()
    pr_number = get_pr_number()
    pr_description, head_ref_name, base_ref_name, threads = fetch_pr_data(owner, repo, pr_number)
    modified = get_modified_lines(base_ref_name)
    
    output_threads = []
    
    for thread in threads:
        is_resolved = thread.get("isResolved", False)
        comments = thread["comments"]["nodes"]
        is_hidden = all(c.get("isMinimized", False) for c in comments) if comments else False
        
        if (is_resolved or is_hidden) and not include_all:
            continue
            
        path = thread["path"]
        line = thread["line"] or thread["originalLine"]
        is_outdated = thread["isOutdated"]
        
        # Parse last comment's suggestion
        last_body = comments[-1]["body"]
        suggestion = parse_suggestion(last_body)
        
        # Check local state
        local_status = check_if_addressed(path, thread["line"], suggestion)
        
        # If pending review, check if local git diff has modifications around that line
        if local_status == "Pending review" and path in modified:
            if line is None or line in modified[path]:
                local_status = "Modified locally"
            
        output_threads.append({
            "id": thread["id"],
            "path": path,
            "line": thread["line"],
            "originalLine": thread["originalLine"],
            "subjectType": thread.get("subjectType", "FILE" if line is None else "LINE"),
            "isOutdated": is_outdated,
            "isResolved": is_resolved,
            "isHidden": is_hidden,
            "localStatus": local_status,
            "suggestion": suggestion,
            "comments": [{
                "id": c["id"],
                "body": c["body"],
                "author": c["author"]["login"] if c["author"] else "unknown",
                "createdAt": c["createdAt"],
                "isMinimized": c.get("isMinimized", False),
                "minimizedReason": c.get("minimizedReason", "")
            } for c in comments]
        })
        
    failed_checks = fetch_failed_checks_logs(pr_number)
        
    return {
        "repo": f"{owner}/{repo}",
        "pr": pr_number,
        "prDescription": pr_description,
        "headRefName": head_ref_name,
        "baseRefName": base_ref_name,
        "threads": output_threads,
        "checks": failed_checks
    }

def main():
    parser = argparse.ArgumentParser(description="Analyze PR review comments.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    parser.add_argument("--all", action="store_true", help="Include resolved and hidden/minimized threads.")
    parser.add_argument("--dir", default=".", help="Directory to run git/gh commands from.")
    args = parser.parse_args()
    
    target_dir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.exists(target_dir):
        print(f"Error: Directory '{target_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)
    os.chdir(target_dir)
    
    try:
        report = analyze(include_all=args.all)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"Repo: {report['repo']}, PR: #{report['pr']}")
            print("="*80)
            print("PR Description:")
            print(report["prDescription"])
            print("="*80)
            for t in report["threads"]:
                print(f"Thread: {t['id']}")
                line_info = f"Line: {t['line'] or t['originalLine']}" if (t['line'] or t['originalLine']) else "File-level"
                print(f"File: {t['path']} ({line_info})")
                print(f"Outdated (pushed): {t['isOutdated']}")
                print(f"Resolved: {t['isResolved']}")
                print(f"Hidden: {t['isHidden']}")
                print(f"Local Status: {t['localStatus']}")
                for i, c in enumerate(t["comments"]):
                    min_str = f" [Hidden: {c['minimizedReason']}]" if c['isMinimized'] else ""
                    print(f"  [{i+1}] @{c['author']} ({c['createdAt']}){min_str}:")
                    for line in c["body"].split("\n"):
                        print(f"      {line}")
                if t["suggestion"]:
                    print("  Suggestion:")
                    for line in t["suggestion"].split("\n"):
                        print(f"    + {line}")
                print("="*80)
            print(f"Total threads displayed: {len(report['threads'])}")
            
            print("\n" + "="*80)
            print(f"Failed Status Checks ({len(report['checks'])}):")
            print("="*80)
            for check in report["checks"]:
                print(f"Check: {check['name']}")
                print(f"Workflow: {check['workflow']}")
                print(f"Link: {check['link']}")
                print("Logs:")
                print(check["logs"])
                print("-"*80)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
