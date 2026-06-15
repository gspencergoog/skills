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

def check_git_state(project_dir, expected_branch, expected_repo):
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
        
    is_correct_branch = (active_branch == expected_branch)
    
    # 3. Check dirty
    status_porcelain = run_git(["status", "--porcelain"], cwd)
    is_dirty = len(status_porcelain) > 0
    
    # 4. Check unpushed commits
    cherry_out = run_git(["cherry", "-v"], cwd)
    has_unpushed = len(cherry_out) > 0
    unpushed_commits = [line for line in cherry_out.splitlines() if line]
    
    # 5. Check repository URL
    active_repo = ""
    is_correct_repo = False
    for remote in ["upstream", "origin"]:
        remote_url = run_git(["remote", "get-url", remote], cwd)
        if remote_url:
            m = re.search(r'(?:git@github\.com:|https://github\.com/)([^/]+)/([^/.]+)(?:\.git)?', remote_url)
            if m:
                active_repo = f"{m.group(1)}/{m.group(2)}"
                if active_repo.lower() == expected_repo.lower():
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
                    
                    expected_branch = report_data.get("headRefName", "")
                    expected_repo = report_data.get("repo", "")
                    git_state = check_git_state(self.project_dir, expected_branch, expected_repo)
                    
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
            comments_path = os.path.join(self.data_dir, "pr_comments.json")
            if os.path.exists(comments_path):
                try:
                    with open(comments_path, "r", encoding="utf-8") as f:
                        report_data = json.load(f)
                    expected_branch = report_data.get("headRefName", "")
                    expected_repo = report_data.get("repo", "")
                except Exception:
                    pass

            # Initial state send
            git_state = check_git_state(self.project_dir, expected_branch, expected_repo)
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
                        git_state = check_git_state(self.project_dir, expected_branch, expected_repo)
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
                    
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": "success"}).encode("utf-8"))
                
                exit_status = 0
                server_should_shutdown = True
                
            except Exception as e:
                self.send_error_json(500, f"Failed to save state: {str(e)}")
                
        elif parsed_url.path == "/api/abort":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "aborted"}).encode("utf-8"))
            
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
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode("utf-8"))

def run_server(server):
    server.serve_forever()

def main():
    global server_should_shutdown
    
    import argparse
    parser = argparse.ArgumentParser(description="Launch PR feedback dashboard.")
    parser.add_argument("--data-dir", default="~/.gemini/jetski/scratch", help="Directory to read/write comments and decisions.")
    parser.add_argument("--project-dir", default=".", help="Path to the target codebase/repository directory.")
    args = parser.parse_args()
    
    # Resolve path
    resolved_data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    os.makedirs(resolved_data_dir, exist_ok=True)
    
    # Pass to handler
    DashboardHandler.data_dir = resolved_data_dir
    DashboardHandler.project_dir = os.path.abspath(os.path.expanduser(args.project_dir))
    
    # Resolve git_dir
    raw_git_dir = run_git(["rev-parse", "--git-dir"], DashboardHandler.project_dir)
    if raw_git_dir:
        DashboardHandler.git_dir = os.path.abspath(os.path.join(DashboardHandler.project_dir, raw_git_dir))
    
    # Bind directly to port 0 for race-condition-free port assignment
    server_address = ("", 0)
    httpd = http.server.ThreadingHTTPServer(server_address, DashboardHandler)
    port = httpd.server_port
    
    server_thread = threading.Thread(target=run_server, args=(httpd,))
    server_thread.daemon = True
    server_thread.start()
    
    url = f"http://localhost:{port}/"
    print(f"Starting dashboard on {url}", flush=True)
    print("Waiting for user decisions in the browser...", flush=True)
    
    # Open browser
    webbrowser.open(url)
    
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
