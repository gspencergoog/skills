#!/usr/bin/env python3
import json
import subprocess
import re
import sys
import os
import argparse

def run_cmd(args):
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()

def get_repo_info():
    for remote in ["upstream", "origin"]:
        try:
            url = run_cmd(["git", "remote", "get-url", remote])
            m = re.search(r'(?:git@github\.com:|https://github\.com/)([^/]+)/([^/.]+)(?:\.git)?', url)
            if m:
                return m.group(1), m.group(2)
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
    pr_data = data["data"]["repository"]["pullRequest"]
    nodes = pr_data["reviewThreads"]["nodes"]
        
    seen = set()
    unique_nodes = []
    for node in nodes:
        if node["id"] not in seen:
            seen.add(node["id"])
            unique_nodes.append(node)
            
    return pr_data.get("body", ""), unique_nodes

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

def get_modified_lines(base_branch="origin/main"):
    try:
        merge_base = run_cmd(["git", "merge-base", base_branch, "HEAD"])
        diff_output = run_cmd(["git", "diff", f"{merge_base}..HEAD", "-U0"])
    except Exception:
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

def analyze(include_all=False):
    owner, repo = get_repo_info()
    pr_number = get_pr_number()
    pr_description, threads = fetch_pr_data(owner, repo, pr_number)
    modified = get_modified_lines()
    
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
        
    return {
        "repo": f"{owner}/{repo}",
        "pr": pr_number,
        "prDescription": pr_description,
        "threads": output_threads
    }

def main():
    parser = argparse.ArgumentParser(description="Analyze PR review comments.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    parser.add_argument("--all", action="store_true", help="Include resolved and hidden/minimized threads.")
    args = parser.parse_args()
    
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
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
