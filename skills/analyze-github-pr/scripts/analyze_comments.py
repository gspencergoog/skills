#!/usr/bin/env python3
import json
import subprocess
import re
import sys
import os
import argparse

from utils import run_cmd

def get_repo_info(pr_input=None):
    if pr_input:
        m = _PR_URL_REGEX.search(str(pr_input))
        if m:
            return m.group(1), m.group(2).strip()

    if os.environ.get("GH_REPO"):
        parts = os.environ["GH_REPO"].split("/")
        if len(parts) == 2:
            return parts[0], parts[1].strip()

    # Query all git remotes
    try:
        remotes_out = run_cmd(["git", "remote"])
        remotes = [r.strip() for r in remotes_out.splitlines() if r.strip()]
    except Exception:
        remotes = []

    for remote in remotes:
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

    try:
        repo_out = run_cmd(["gh", "repo", "view", "--json", "owner,name"])
        repo_json = json.loads(repo_out)
        return repo_json["owner"]["login"], repo_json["name"]
    except Exception:
        pass

    raise Exception("Could not determine repository owner and name from git remotes.")

_PR_URL_REGEX = re.compile(r'github\.com/([^/]+)/([^/]+)/pull/(\d+)')
_DIGITS_ONLY = re.compile(r'^\d+$')

def get_pr_number(pr_input=None):
    if pr_input is not None:
        input_str = str(pr_input)
        m = _PR_URL_REGEX.search(input_str)
        if m:
            return int(m.group(3))
        if _DIGITS_ONLY.match(input_str):
            return int(input_str)
        raise Exception(f"Invalid PR argument '{pr_input}'. Please provide a PR number or a GitHub PR URL.")

    if os.environ.get("PR_NUMBER"):
        return int(os.environ["PR_NUMBER"])

    branch = ""
    try:
        branch = run_cmd(["git", "symbolic-ref", "--short", "HEAD"]).strip()
    except Exception:
        try:
            branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except Exception:
            branch = ""

    if not branch or branch in ("HEAD", "main", "master"):
        raise Exception(
            f"Active branch is {repr(branch) if branch else 'detached HEAD'}. "
            "Please specify a target PR number or URL using --pr."
        )

    try:
        list_output = run_cmd(["gh", "pr", "list", "--head", branch, "--json", "number,url"])
        pr_list = json.loads(list_output)
    except Exception:
        pr_list = []

    if not pr_list:
        try:
            pr_num = run_cmd(["gh", "pr", "view", "--json", "number", "--jq", ".number"])
            return int(pr_num)
        except Exception:
            raise Exception(f"No open PR found for branch '{branch}'. Please specify a target PR number or URL using --pr.")

    if len(pr_list) > 1:
        raise Exception(f"Multiple open PRs found for branch '{branch}'. Please specify which PR number or URL to target using --pr.")

    return int(pr_list[0]["number"])

def fetch_pr_sync_status(owner, repo, pr_number, remote_branch, remote_head_sha):
    local_branch = ""
    try:
        local_branch = run_cmd(["git", "symbolic-ref", "--short", "HEAD"]).strip()
    except Exception:
        try:
            local_branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        except Exception:
            local_branch = ""

    local_head_sha = ""
    try:
        local_head_sha = run_cmd(["git", "rev-parse", "HEAD"]).strip()
    except Exception:
        pass

    if local_branch and remote_branch and local_branch != remote_branch:
        return {
            "localBranch": local_branch,
            "remoteBranch": remote_branch,
            "localHeadSha": local_head_sha,
            "remoteHeadSha": remote_head_sha,
            "isSynced": False,
            "syncState": "branch_mismatch",
            "warning": f"Active local branch is '{local_branch}', but PR branch is '{remote_branch}'. Please checkout the correct branch."
        }

    if not local_head_sha or not remote_head_sha:
        return {
            "localBranch": local_branch,
            "remoteBranch": remote_branch,
            "localHeadSha": local_head_sha,
            "remoteHeadSha": remote_head_sha,
            "isSynced": False,
            "syncState": "unknown",
            "warning": "Could not determine local or remote commit SHA."
        }

    if local_head_sha == remote_head_sha:
        return {
            "localBranch": local_branch,
            "remoteBranch": remote_branch,
            "localHeadSha": local_head_sha,
            "remoteHeadSha": remote_head_sha,
            "isSynced": True,
            "syncState": "in_sync",
            "warning": None
        }

    remote_commit_exists = False
    try:
        run_cmd(["git", "cat-file", "-e", f"{remote_head_sha}^{{commit}}"])
        remote_commit_exists = True
    except Exception:
        remote_commit_exists = False

    if not remote_commit_exists:
        return {
            "localBranch": local_branch,
            "remoteBranch": remote_branch,
            "localHeadSha": local_head_sha,
            "remoteHeadSha": remote_head_sha,
            "isSynced": False,
            "syncState": "not_fetched",
            "warning": f"Remote PR commit ({remote_head_sha[:8]}) is not present locally. Run 'git fetch' to update your local repository."
        }

    is_local_ancestor = False
    try:
        run_cmd(["git", "merge-base", "--is-ancestor", local_head_sha, remote_head_sha])
        is_local_ancestor = True
    except Exception:
        is_local_ancestor = False

    if is_local_ancestor:
        return {
            "localBranch": local_branch,
            "remoteBranch": remote_branch,
            "localHeadSha": local_head_sha,
            "remoteHeadSha": remote_head_sha,
            "isSynced": False,
            "syncState": "behind_remote",
            "warning": f"Local branch is behind remote PR commit ({remote_head_sha[:8]}). Run 'git pull' before making edits."
        }

    is_remote_ancestor = False
    try:
        run_cmd(["git", "merge-base", "--is-ancestor", remote_head_sha, local_head_sha])
        is_remote_ancestor = True
    except Exception:
        is_remote_ancestor = False

    if is_remote_ancestor:
        return {
            "localBranch": local_branch,
            "remoteBranch": remote_branch,
            "localHeadSha": local_head_sha,
            "remoteHeadSha": remote_head_sha,
            "isSynced": False,
            "syncState": "ahead_of_remote",
            "warning": f"Local branch is ahead of remote PR commit ({remote_head_sha[:8]}). Push your commits to sync the PR."
        }

    return {
        "localBranch": local_branch,
        "remoteBranch": remote_branch,
        "localHeadSha": local_head_sha,
        "remoteHeadSha": remote_head_sha,
        "isSynced": False,
        "syncState": "diverged",
        "warning": f"Local branch and remote PR branch have diverged. Please sync local and remote branches."
    }

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
          databaseId
          body
          diffHunk
          isMinimized
          minimizedReason
          author {
            login
          }
          createdAt
          url
        }
      }
    }
    query($owner: String!, $repo: String!, $pr: Int!) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $pr) {
          body
          headRefName
          headRefOid
          baseRefName
          reviewDecision
          url
          comments(last: 100) {
            nodes {
              databaseId
              author {
                login
              }
              body
              createdAt
              url
            }
          }
          reviews(last: 100) {
            nodes {
              id
              databaseId
              author {
                login
              }
              body
              state
              submittedAt
              url
            }
          }
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
    nodes = pr_data.get("reviewThreads", {}).get("nodes", [])
        
    seen = set()
    unique_nodes = []
    for node in nodes:
        if node["id"] not in seen:
            seen.add(node["id"])
            unique_nodes.append(node)

    raw_reviews = pr_data.get("reviews", {}).get("nodes", [])
    reviews = []
    for r in raw_reviews:
        author = r["author"]["login"] if r.get("author") else "ghost"
        body = (r.get("body") or "").strip()
        reviews.append({
            "id": r.get("id", ""),
            "databaseId": str(r.get("databaseId", "")),
            "author": author,
            "body": body,
            "state": r.get("state", ""),
            "submittedAt": r.get("submittedAt", ""),
            "url": r.get("url", "")
        })

    raw_comments = pr_data.get("comments", {}).get("nodes", [])
    comments = []
    for c in raw_comments:
        author = c["author"]["login"] if c.get("author") else "ghost"
        body = (c.get("body") or "").strip()
        comments.append({
            "databaseId": str(c.get("databaseId", "")),
            "author": author,
            "body": body,
            "createdAt": c.get("createdAt", ""),
            "url": c.get("url", "")
        })
            
    return (
        pr_data.get("body", ""),
        pr_data.get("headRefName", ""),
        pr_data.get("headRefOid", ""),
        pr_data.get("baseRefName", ""),
        pr_data.get("reviewDecision", ""),
        pr_data.get("url", ""),
        unique_nodes,
        reviews,
        comments
    )


def parse_suggestion(body):
    pattern = r"```suggestion\s*(.*?)\s*```"
    match = re.search(pattern, body, re.DOTALL)
    if match:
        return match.group(1)
    return None

def parse_diff_hunk_right_ref(diff_hunk):
    if not diff_hunk:
        return []
        
    lines = diff_hunk.splitlines()
    if not lines:
        return []
        
    start_line = None
    header_match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", lines[0])
    if header_match:
        start_line = int(header_match.group(1))
        
    line_entries = []
    current_line = start_line
    
    for line in lines[1:]:
        if line.startswith("-"):
            continue
        elif line.startswith("+"):
            line_entries.append({
                "line": current_line,
                "content": line[1:]
            })
            if current_line is not None:
                current_line += 1
        elif line.startswith(" "):
            line_entries.append({
                "line": current_line,
                "content": line[1:]
            })
            if current_line is not None:
                current_line += 1
                
    return line_entries

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
    for i in range(min(15, N)):
        keep_indices.add(i)
    for i in range(max(0, N - 85), N):
        keep_indices.add(i)
        
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

def parse_run_id_from_link(link):
    if not link:
        return None
    m = re.search(r'/actions/runs/(\d+)', link)
    return m.group(1) if m else None

def parse_check_run_id_from_link(link):
    if not link:
        return None
    m = re.search(r'/check-runs/(\d+)', link)
    if m:
        return m.group(1)
    m = re.search(r'/job/(\d+)', link)
    if m:
        return m.group(1)
    m = re.search(r'/jobs/(\d+)', link)
    if m:
        return m.group(1)
    return None

def fetch_check_annotations(owner, repo, check_run_id):
    annotations = []
    try:
        endpoint = f"repos/{owner}/{repo}/check-runs/{check_run_id}/annotations"
        output = run_cmd(["gh", "api", endpoint])
        ann_list = json.loads(output)
        for ann in ann_list:
            path = ann.get("path", "")
            start_line = ann.get("start_line")
            message = ann.get("message", "")
            level = ann.get("annotation_level", "")
            title = ann.get("title", "")
            if message:
                line_str = f"{path}:{start_line} " if path else ""
                title_str = f"({title}): " if title else ""
                annotations.append(f"Annotation [{level}] {line_str}{title_str}{message}")
    except Exception:
        pass
    return annotations

def fetch_failed_job_logs(owner, repo, run_id):
    try:
        endpoint = f"repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
        output = run_cmd(["gh", "api", endpoint])
        jobs_data = json.loads(output)
        jobs_list = jobs_data.get("jobs", [])
        failed_jobs = [
            j for j in jobs_list
            if j.get("conclusion") in ("failure", "timed_out", "action_required")
        ]
        if not failed_jobs:
            return None

        log_buffers = []
        for job in failed_jobs:
            job_id = job.get("id")
            job_name = job.get("name", "Job")
            if job_id:
                try:
                    job_log = run_cmd(["gh", "api", "--allow-escape-sequences", f"repos/{owner}/{repo}/actions/jobs/{job_id}/logs"])
                    if job_log.strip():
                        log_buffers.append(f"--- Job: {job_name} (ID: {job_id}) ---\n{job_log}")
                except Exception:
                    pass

        if log_buffers:
            return "\n\n".join(log_buffers)
    except Exception:
        pass
    return None

def fetch_failed_checks_logs(owner, repo, pr_number):
    try:
        checks_output = run_cmd(["gh", "pr", "checks", str(pr_number), "--json", "name,state,bucket,link,workflow"])
        checks = json.loads(checks_output)
    except Exception as e:
        if "no checks reported" in str(e).lower():
            return [], []
        return [], []
        
    failed_checks = [
        c for c in checks
        if c.get("bucket") == "fail" or (c.get("state") or "").upper() in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED")
    ]
    pending_checks = [
        c for c in checks
        if c.get("bucket") == "pending" or (c.get("state") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING", "WAITING")
    ]

    output_failed = []
    for check in failed_checks:
        name = check.get("name", "Unknown Check")
        link = check.get("link", "")
        state = check.get("state", "FAILED")
        workflow = check.get("workflow", "")
        
        annotations = []
        check_run_id = parse_check_run_id_from_link(link)
        if check_run_id:
            annotations = fetch_check_annotations(owner, repo, check_run_id)

        logs = ""
        run_id = parse_run_id_from_link(link)
        if run_id:
            job_logs = fetch_failed_job_logs(owner, repo, run_id)
            if job_logs:
                logs = job_logs
            else:
                try:
                    raw_logs = run_cmd(["gh", "run", "view", run_id, "--log-failed"])
                    logs = raw_logs
                except Exception as e:
                    logs = f"Failed to fetch logs: {e}"
        else:
            logs = f"Non-GitHub Actions run. Inspect details at: {link}"

        if annotations:
            ann_block = "Check Annotations:\n" + "\n".join(annotations) + "\n\n"
            logs = ann_block + logs

        output_failed.append({
            "name": name,
            "link": link,
            "state": state,
            "workflow": workflow,
            "logs": truncate_log(logs),
            "annotations": annotations
        })

    output_pending = []
    for check in pending_checks:
        output_pending.append({
            "name": check.get("name", "Unknown Check"),
            "link": check.get("link", ""),
            "state": check.get("state", "PENDING"),
            "workflow": check.get("workflow", "")
        })
        
    return output_failed, output_pending

def analyze(include_all=False, pr_input=None):
    owner, repo = get_repo_info(pr_input=pr_input)
    pr_number = get_pr_number(pr_input=pr_input)
    (
        pr_description,
        head_ref_name,
        head_ref_oid,
        base_ref_name,
        review_decision,
        pr_url,
        threads,
        reviews,
        general_comments
    ) = fetch_pr_data(owner, repo, pr_number)

    sync_status = fetch_pr_sync_status(owner, repo, pr_number, head_ref_name, head_ref_oid)
    modified = get_modified_lines(base_ref_name)
    
    output_threads = []
    for thread in threads:
        is_resolved = thread.get("isResolved", False)
        comments = thread.get("comments", {}).get("nodes", [])
        is_hidden = all(c.get("isMinimized", False) for c in comments) if comments else False
        
        if (is_resolved or is_hidden) and not include_all:
            continue
            
        path = thread["path"]
        line = thread.get("line") or thread.get("originalLine")
        is_outdated = thread.get("isOutdated", False)
        
        last_body = comments[-1]["body"] if comments else ""
        suggestion = parse_suggestion(last_body)
        
        diff_hunk = comments[0].get("diffHunk") if comments else None
        right_ref_code_block = parse_diff_hunk_right_ref(diff_hunk)
        
        local_status = check_if_addressed(path, thread.get("line"), suggestion)
        if local_status == "Pending review" and path in modified:
            if line is None or line in modified[path]:
                local_status = "Modified locally"
            
        output_threads.append({
            "id": thread["id"],
            "path": path,
            "line": thread.get("line"),
            "originalLine": thread.get("originalLine"),
            "subjectType": thread.get("subjectType", "FILE" if line is None else "LINE"),
            "isOutdated": is_outdated,
            "isResolved": is_resolved,
            "isHidden": is_hidden,
            "localStatus": local_status,
            "suggestion": suggestion,
            "rightRefCodeBlock": right_ref_code_block,
            "comments": [{
                "id": c.get("id"),
                "databaseId": str(c.get("databaseId", "")),
                "body": c.get("body", ""),
                "author": c["author"]["login"] if c.get("author") else "unknown",
                "createdAt": c.get("createdAt", ""),
                "url": c.get("url", ""),
                "isMinimized": c.get("isMinimized", False),
                "minimizedReason": c.get("minimizedReason", "")
            } for c in comments]
        })
        
    failed_checks, pending_checks = fetch_failed_checks_logs(owner, repo, pr_number)
        
    return {
        "repo": f"{owner}/{repo}",
        "pr": pr_number,
        "prUrl": pr_url,
        "prDescription": pr_description,
        "headRefName": head_ref_name,
        "headRefOid": head_ref_oid,
        "baseRefName": base_ref_name,
        "reviewDecision": review_decision,
        "syncStatus": sync_status,
        "reviews": reviews,
        "comments": general_comments,
        "threads": output_threads,
        "checks": failed_checks,
        "pendingChecks": pending_checks
    }

def main():
    parser = argparse.ArgumentParser(description="Analyze PR review comments.")
    parser.add_argument("--json", action="store_true", help="Output raw JSON.")
    parser.add_argument("-o", "--output", help="Write JSON report to specified file path.")
    parser.add_argument("--all", action="store_true", help="Include resolved and hidden/minimized threads.")
    parser.add_argument("--dir", default=".", help="Directory to run git/gh commands from.")
    parser.add_argument("--pr", "-p", help="Target PR number or GitHub PR URL.")
    args = parser.parse_args()
    
    target_dir = os.path.abspath(os.path.expanduser(args.dir))
    if not os.path.exists(target_dir):
        print(f"Error: Directory '{target_dir}' does not exist.", file=sys.stderr)
        sys.exit(1)
    os.chdir(target_dir)
    
    try:
        report = analyze(include_all=args.all, pr_input=args.pr)
        if args.output:
            out_path = os.path.abspath(os.path.expanduser(args.output))
            parent = os.path.dirname(out_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
            if not args.json:
                print(f"Saved PR analysis to {out_path}")
        if args.json:
            print(json.dumps(report, indent=2))
        elif not args.output:
            print(f"Repo: {report['repo']}, PR: #{report['pr']}")
            if report.get("syncStatus") and report["syncStatus"].get("warning"):
                print(f"WARNING: {report['syncStatus']['warning']}")
            print("="*80)
            print("PR Description:")
            print(report["prDescription"])
            print("="*80)

            if report.get("reviews"):
                print(f"Top-Level Reviews ({len(report['reviews'])}):")
                for r in report["reviews"]:
                    print(f"  Review by @{r['author']} ({r['state']}): {r['body']}")
                print("="*80)

            if report.get("comments"):
                print(f"Conversation Comments ({len(report['comments'])}):")
                for c in report["comments"]:
                    print(f"  Comment by @{c['author']}: {c['body']}")
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

            if report.get("pendingChecks"):
                print(f"\nActive/Pending Checks ({len(report['pendingChecks'])}):")
                for p in report["pendingChecks"]:
                    print(f"  ⏳ {p['name']} ({p['state']}) - {p['link']}")
                print("="*80)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()

