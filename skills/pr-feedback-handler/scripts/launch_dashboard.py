#!/usr/bin/env python3
import http.server
import json
import os
import sys
import webbrowser
import urllib.parse
import threading
import socket
import time
import re
import subprocess
import select

from utils import run_git

# Global flags for shutdown and status
server_should_shutdown = False
exit_status = 0

def wait_for_git_changes(head_path, index_path, check_shutdown):
    if hasattr(select, "kqueue"):
        kq = select.kqueue()
        fds = []
        events = []
        try:
            for path in [head_path, index_path]:
                if path and os.path.exists(path):
                    try:
                        fd = os.open(path, os.O_RDONLY)
                        fds.append(fd)
                        ev = select.kevent(
                            fd,
                            filter=select.KQ_FILTER_VNODE,
                            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_ATTRIB
                        )
                        events.append(ev)
                    except Exception:
                        pass
                    
            if events:
                kq.control(events, 0)
                while not check_shutdown():
                    triggered = kq.control(None, 1, 1.0)
                    if triggered:
                        return True
        finally:
            try:
                kq.close()
            except Exception:
                pass
            for fd in fds:
                try:
                    os.close(fd)
                except Exception:
                    pass
        return False

    # Fallback to mtime polling
    last_head = os.path.getmtime(head_path) if head_path and os.path.exists(head_path) else 0
    last_index = os.path.getmtime(index_path) if index_path and os.path.exists(index_path) else 0
    
    while not check_shutdown():
        time.sleep(1)
        curr_head = os.path.getmtime(head_path) if head_path and os.path.exists(head_path) else 0
        curr_index = os.path.getmtime(index_path) if index_path and os.path.exists(index_path) else 0
        if curr_head != last_head or curr_index != last_index:
            return True
    return False

def check_git_state(project_dir, expected_branch, expected_repo, head_ref_oid=None):
    cwd = os.path.abspath(os.path.expanduser(project_dir))
    
    # 1. Check if it's a git repo and resolve toplevel path
    toplevel = run_git(["rev-parse", "--show-toplevel"], cwd)
    if not toplevel:
        return {
            "isGit": False,
            "error": f"Path '{cwd}' is not inside a git repository."
        }
        
    # 2. Check branch
    active_branch = run_git(["symbolic-ref", "--short", "HEAD"], cwd)
    if not active_branch:
        # Might be detached HEAD, get OID
        active_branch = run_git(["rev-parse", "--short", "HEAD"], cwd)
        
    is_correct_branch = bool(expected_branch) and (active_branch == expected_branch)
    
    # 3. Check dirty
    status_porcelain = run_git(["status", "--porcelain", "-uno"], cwd)
    is_dirty = len(status_porcelain) > 0
    
    # 4. Check unpushed commits
    unpushed_commits = []
    has_unpushed = False
    if head_ref_oid:
        current_sha = run_git(["rev-parse", "HEAD"], cwd)
        if current_sha and current_sha != head_ref_oid:
            log_out = run_git(["log", f"{head_ref_oid}..HEAD", "--oneline"], cwd)
            if log_out:
                unpushed_commits = [line for line in log_out.splitlines() if line]
                has_unpushed = len(unpushed_commits) > 0
            else:
                cherry_out = run_git(["cherry", "-v"], cwd)
                has_unpushed = len(cherry_out) > 0
                unpushed_commits = [line for line in cherry_out.splitlines() if line]
        else:
            has_unpushed = False
            unpushed_commits = []
    else:
        cherry_out = run_git(["cherry", "-v"], cwd)
        has_unpushed = len(cherry_out) > 0
        unpushed_commits = [line for line in cherry_out.splitlines() if line]
    
    # 5. Check repository URL
    active_repo = ""
    is_correct_repo = False
    
    remotes_output = run_git(["remote"], cwd)
    remotes = [r.strip() for r in remotes_output.splitlines() if r.strip()]
    if not remotes:
        remotes = ["upstream", "origin"]
        
    for remote in remotes:
        remote_url = run_git(["remote", "get-url", remote], cwd)
        if remote_url:
            m = re.search(r'(?:git@github\.com:|https://github\.com/)([^/]+)/([^/]+)', remote_url)
            if m:
                owner = m.group(1)
                repo = m.group(2).strip()
                if repo.endswith(".git"):
                    repo = repo[:-4]
                active_repo = f"{owner}/{repo}"
                if expected_repo and active_repo.lower() == expected_repo.lower():
                    is_correct_repo = True
                    break
                    
    # 6. Check if worktree
    git_dir = run_git(["rev-parse", "--git-dir"], cwd)
    git_common_dir = run_git(["rev-parse", "--git-common-dir"], cwd)
    is_worktree = (git_dir != git_common_dir)
    
    return {
        "isGit": True,
        "isCorrectBranch": is_correct_branch,
        "activeBranch": active_branch,
        "expectedBranch": expected_branch,
        "isDirty": is_dirty,
        "hasUnpushed": has_unpushed,
        "unpushedCommits": unpushed_commits,
        "isCorrectRepo": is_correct_repo,
        "activeRepo": active_repo,
        "expectedRepo": expected_repo,
        "isWorktree": is_worktree,
        "worktreePath": toplevel
    }


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    data_dir = os.path.expanduser("~/.gemini/jetski/scratch")
    project_dir = "."
    git_dir = None

    def log_message(self, format, *args):
        # Suppress logging to keep stdout clean
        pass

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        
        if parsed_url.path == "/" or parsed_url.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            html_path = os.path.join(os.path.dirname(__file__), "../assets/pr_feedback.html")
            with open(html_path, "rb") as f:
                self.wfile.write(f.read())
                
        elif parsed_url.path == "/api/comments":
            try:
                comments_path = os.path.join(self.data_dir, "pr_comments.json")
                if os.path.exists(comments_path):
                    with open(comments_path, "r", encoding="utf-8") as f:
                        report_data = json.load(f)
                    
                    # Merge proposals.json overlay if present
                    proposals_path = os.path.join(self.data_dir, "proposals.json")
                    if os.path.exists(proposals_path):
                        try:
                            with open(proposals_path, "r", encoding="utf-8") as pf:
                                proposals_data = json.load(pf)
                            proposals_map = {}
                            if isinstance(proposals_data, dict):
                                if "proposals" in proposals_data and isinstance(proposals_data["proposals"], dict):
                                    proposals_map = proposals_data["proposals"]
                                else:
                                    proposals_map = proposals_data
                            elif isinstance(proposals_data, list):
                                for item in proposals_data:
                                    if isinstance(item, dict) and "threadId" in item:
                                        proposals_map[item["threadId"]] = item

                            for thread in report_data.get("threads", []):
                                tid = thread.get("id")
                                if tid and tid in proposals_map:
                                    proposal = proposals_map[tid]
                                    if isinstance(proposal, dict):
                                        if "proposedFix" in proposal:
                                            thread["proposedFix"] = proposal["proposedFix"]
                                        if "draftReply" in proposal:
                                            thread["draftReply"] = proposal["draftReply"]
                        except Exception:
                            pass

                    expected_branch = report_data.get("headRefName", "")
                    expected_repo = report_data.get("repo", "")
                    head_ref_oid = report_data.get("headRefOid", "")
                    git_state = check_git_state(self.project_dir, expected_branch, expected_repo, head_ref_oid)
                    
                    report_data["gitState"] = git_state
                    
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(report_data).encode("utf-8"))
                else:
                    self.send_error_json(404, f"PR comments data not found ({comments_path}).")
            except Exception as e:
                self.send_error_json(500, f"Server error: {str(e)}")
                
        elif parsed_url.path == "/api/git-events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            # Read expected state from pr_comments.json
            expected_branch = ""
            expected_repo = ""
            head_ref_oid = ""
            comments_path = os.path.join(self.data_dir, "pr_comments.json")
            if os.path.exists(comments_path):
                try:
                    with open(comments_path, "r", encoding="utf-8") as f:
                        report_data = json.load(f)
                    expected_branch = report_data.get("headRefName") or report_data.get("pr", {}).get("headRef") or report_data.get("pr", {}).get("headRefName", "")
                    head_ref_oid = report_data.get("headRefOid", "")
                    expected_repo = report_data.get("repo", "")
                    if not expected_repo and isinstance(report_data.get("pr"), dict):
                        pr_url = report_data["pr"].get("url", "")
                        m = re.search(r'github\.com/([^/]+/[^/]+)/pull', pr_url)
                        if m:
                            expected_repo = m.group(1)
                except Exception:
                    pass

            # Initial state send
            git_state = check_git_state(self.project_dir, expected_branch, expected_repo, head_ref_oid)
            try:
                self.wfile.write(f"data: {json.dumps(git_state)}\n\n".encode("utf-8"))
                self.wfile.flush()
            except Exception:
                return

            head_path = None
            index_path = None
            
            if self.git_dir:
                head_path = os.path.join(self.git_dir, "HEAD")
                index_path = os.path.join(self.git_dir, "index")

            try:
                while not server_should_shutdown:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    
                    has_changes = wait_for_git_changes(head_path, index_path, lambda: server_should_shutdown)
                    if has_changes:
                        # Debounce/settling period for filesystem updates
                        time.sleep(0.25)
                        git_state = check_git_state(self.project_dir, expected_branch, expected_repo, head_ref_oid)
                        self.wfile.write(f"data: {json.dumps(git_state)}\n\n".encode("utf-8"))
                        self.wfile.flush()
            except (ConnectionResetError, BrokenPipeError):
                pass
            except Exception:
                pass
            return
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        global exit_status, server_should_shutdown
        parsed_url = urllib.parse.urlparse(self.path)
        
        if parsed_url.path == "/api/save":
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            
            try:
                data = json.loads(post_data.decode("utf-8"))
                
                # Write to feedback_state.json in data_dir
                state_file_path = os.path.join(self.data_dir, "feedback_state.json")
                with open(state_file_path, "w") as f:
                    json.dump(data, f, indent=2)
                    
                response_bytes = json.dumps({"status": "success"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_bytes)))
                self.end_headers()
                self.wfile.write(response_bytes)
                self.wfile.flush()
                
                exit_status = 0
                server_should_shutdown = True
                
            except Exception as e:
                self.send_error_json(500, f"Failed to save state: {str(e)}")
                
        elif parsed_url.path == "/api/abort":
            response_bytes = json.dumps({"status": "aborted"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_bytes)))
            self.end_headers()
            self.wfile.write(response_bytes)
            self.wfile.flush()
            
            exit_status = 1
            server_should_shutdown = True
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def send_error_json(self, status, message):
        response_bytes = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

def is_remote_or_headless():
    """Detects whether execution is in a remote or headless environment."""
    # 1. SSH session active -> browser will open on remote host, not local client
    if any(k in os.environ for k in ("SSH_CLIENT", "SSH_TTY", "SSH_CONNECTION")):
        return True

    # 2. Known remote container / cloud environments
    remote_keys = (
        "JETSKI_HUB",
        "JETSKI_REMOTE",
        "CLOUDTOP_ENVIRONMENT",
        "CODESPACES",
        "GITPOD_WORKSPACE_ID",
        "REMOTE_CONTAINERS",
        "DEVPOD",
    )
    if any(os.environ.get(k) for k in remote_keys):
        return True

    # 3. Linux without graphical display server
    if sys.platform.startswith("linux"):
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return True

    # 4. Explicit non-interactive / CI / headless flags
    if os.environ.get("CI") == "true" or os.environ.get("HEADLESS") == "true":
        return True

    return False

def can_open_local_browser():
    return not is_remote_or_headless()

def generate_artifact_report(data_dir, project_dir):
    comments_path = os.path.join(data_dir, "pr_comments.json")
    with open(comments_path, "r", encoding="utf-8") as f:
        report_data = json.load(f)

    # Proposals overlay
    proposals_path = os.path.join(data_dir, "proposals.json")
    proposals_map = {}
    if os.path.exists(proposals_path):
        try:
            with open(proposals_path, "r", encoding="utf-8") as pf:
                pdata = json.load(pf)
            if isinstance(pdata, dict):
                proposals_map = pdata.get("proposals", pdata)
            elif isinstance(pdata, list):
                for item in pdata:
                    if isinstance(item, dict) and "threadId" in item:
                        proposals_map[item["threadId"]] = item
        except Exception:
            pass

    expected_branch = report_data.get("headRefName", "")
    expected_repo = report_data.get("repo", "")
    head_ref_oid = report_data.get("headRefOid", "")
    git_state = check_git_state(project_dir, expected_branch, expected_repo, head_ref_oid)

    pr_num = report_data.get("pr", "N/A")
    repo = report_data.get("repo", "N/A")
    pr_url = report_data.get("prUrl") or f"https://github.com/{repo}/pull/{pr_num}"
    sync_status = report_data.get("syncStatus", {})
    sync_state = sync_status.get("syncState", "unknown")
    sync_icon = "✅" if sync_status.get("isSynced") else "⚠️"

    lines = []
    lines.append(f"# PR Triage Report: #{pr_num} ({repo})")
    lines.append("")
    lines.append(f"**URL**: [{repo}#{pr_num}]({pr_url})")
    lines.append(f"**Branch**: `{expected_branch}`")
    lines.append(f"**Remote Commit**: `{head_ref_oid}`")
    lines.append(f"**Sync Status**: `{sync_state}` {sync_icon}")
    if sync_status.get("warning"):
        lines.append(f"> [!WARNING]\n> {sync_status['warning']}\n")

    threads = report_data.get("threads", [])
    lines.append(f"## Unresolved Review Comments ({len(threads)})")
    lines.append("")

    if not threads:
        lines.append("No unresolved review comments found. 🎉\n")
    else:
        for i, t in enumerate(threads):
            tid = t.get("id", "")
            prop = proposals_map.get(tid, {})
            proposed_fix = prop.get("proposedFix") or t.get("proposedFix", "")
            draft_reply = prop.get("draftReply") or t.get("draftReply", "")

            lines.append(f"### Comment #{i+1} (Thread `{tid}`): `{t.get('path')}` (Line {t.get('line') or t.get('originalLine') or 'File'})")
            lines.append(f"- **Local Status**: `{t.get('localStatus', 'Pending review')}`")
            if proposed_fix:
                lines.append(f"- **Proposed Fix**: {proposed_fix}")
            if draft_reply:
                lines.append(f"- **Draft Reply**: {draft_reply}")

            for c in t.get("comments", []):
                lines.append(f"\n> **@{c.get('author')}** ({c.get('createdAt')}):")
                lines.append(f"> {c.get('body', '').replace(chr(10), chr(10) + '> ')}")
            lines.append("\n---\n")

    reviews = report_data.get("reviews", [])
    if reviews:
        lines.append(f"## Top-Level Reviews ({len(reviews)})\n")
        for r in reviews:
            lines.append(f"- **@{r.get('author')}** ({r.get('state')}): {r.get('body')}")
        lines.append("")

    comments = report_data.get("comments", [])
    if comments:
        lines.append(f"## Conversation Comments ({len(comments)})\n")
        for c in comments:
            lines.append(f"- **@{c.get('author')}** ({c.get('createdAt')}): {c.get('body')}")
        lines.append("")

    checks = report_data.get("checks", [])
    lines.append(f"## Failed Status Checks ({len(checks)})\n")
    if not checks:
        lines.append("All checks passing! ✅\n")
    else:
        for c in checks:
            lines.append(f"### ❌ {c.get('name')}")
            lines.append(f"Link: {c.get('link')}")
            lines.append("```text")
            lines.append(c.get("logs", "No logs available."))
            lines.append("```\n")

    pending = report_data.get("pendingChecks", [])
    if pending:
        lines.append(f"## Active/Pending Checks ({len(pending)}) ⏳\n")
        for p in pending:
            lines.append(f"- ⏳ **{p.get('name')}**: [{p.get('link')}]({p.get('link')})")
        lines.append("")

    artifact_content = "\n".join(lines)
    artifact_path = os.path.join(data_dir, "pr_triage_report.md")
    with open(artifact_path, "w", encoding="utf-8") as f:
        f.write(artifact_content)
    return artifact_path

def run_server(server):
    server.serve_forever()

def main():
    global server_should_shutdown
    
    import argparse
    parser = argparse.ArgumentParser(description="Launch PR feedback dashboard.")
    parser.add_argument("--data-dir", default="~/.gemini/jetski/scratch", help="Directory to read/write comments and decisions.")
    parser.add_argument("--project-dir", default=".", help="Path to the target codebase/repository directory.")
    parser.add_argument("--allow-dirty", action="store_true", help="Allow launching dashboard even if workspace has uncommitted changes to tracked files.")
    parser.add_argument("--mode", choices=["auto", "web", "artifact"], default="auto", help="Execution mode: auto (default), web, or artifact.")
    args = parser.parse_args()
    
    # Resolve path
    resolved_data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    os.makedirs(resolved_data_dir, exist_ok=True)
    
    # Pre-flight check: Ensure pr_comments.json exists and is valid JSON
    comments_path = os.path.join(resolved_data_dir, "pr_comments.json")
    if not os.path.exists(comments_path):
        print(f"Error: PR comments data file not found at '{comments_path}'.", file=sys.stderr)
        print("Please generate pr_comments.json before launching the dashboard.", file=sys.stderr)
        sys.exit(1)
        
    try:
        with open(comments_path, "r", encoding="utf-8") as f:
            json.load(f)
    except Exception as e:
        print(f"Error: Failed to parse '{comments_path}' as valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Artifact Mode Check
    if args.mode == "artifact":
        report_file = generate_artifact_report(resolved_data_dir, args.project_dir)
        print(f"Generated PR triage report artifact at: {report_file}", flush=True)
        sys.exit(0)

    # Pass to handler
    DashboardHandler.data_dir = resolved_data_dir
    DashboardHandler.project_dir = os.path.abspath(os.path.expanduser(args.project_dir))
    
    # Pre-flight check: Ensure workspace files are clean of premature edits
    if not args.allow_dirty:
        status_porcelain = run_git(["status", "--porcelain", "-uno"], DashboardHandler.project_dir)
        if status_porcelain:
            print("Error: Tracked workspace files have uncommitted changes.", file=sys.stderr)
            print("You MUST NOT modify workspace files before reviewing proposed fixes on the dashboard.", file=sys.stderr)
            print("Please revert workspace changes and launch the dashboard first.", file=sys.stderr)
            sys.exit(1)
    
    # Resolve git_dir (robust against worktrees)
    raw_git_dir = run_git(["rev-parse", "--git-dir"], DashboardHandler.project_dir)
    if raw_git_dir:
        DashboardHandler.git_dir = os.path.abspath(os.path.join(DashboardHandler.project_dir, raw_git_dir))
    
    # Bind directly to port 0 for race-condition-free port assignment
    server_address = ("127.0.0.1", 0)
    httpd = http.server.ThreadingHTTPServer(server_address, DashboardHandler)
    port = httpd.server_port
    
    server_thread = threading.Thread(target=run_server, args=(httpd,))
    server_thread.daemon = True
    server_thread.start()
    
    url = f"http://localhost:{port}/"
    print(f"Starting dashboard on {url}", flush=True)
    
    # Open browser if running locally and mode != artifact
    if can_open_local_browser() and args.mode != "artifact":
        print("Opening browser...", flush=True)
        webbrowser.open(url)
    else:
        print("[Remote/SSH session detected] Skipping automatic browser launch.", flush=True)
        print(f"Please open {url} in your local browser (or via SSH port forwarding).", flush=True)
        
    print("Waiting for user decisions in the browser...", flush=True)
    
    # Monitor shutdown flag
    try:
        while not server_should_shutdown:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nAborted by user (Ctrl+C)", flush=True)
        httpd.shutdown()
        sys.exit(1)
        
    httpd.shutdown()
    
    if exit_status == 0:
        print("Plan saved successfully.", flush=True)
        sys.exit(0)
    else:
        print("Review aborted by user.", flush=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
