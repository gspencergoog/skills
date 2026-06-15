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

# Global flags for shutdown and status
server_should_shutdown = False
exit_status = 0

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    data_dir = os.path.expanduser("~/.gemini/jetski/scratch")

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
                    with open(comments_path, "rb") as f:
                        data = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error_json(404, f"PR comments data not found ({comments_path}).")
            except Exception as e:
                self.send_error_json(500, f"Server error: {str(e)}")
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
    args = parser.parse_args()
    
    # Resolve path
    resolved_data_dir = os.path.abspath(os.path.expanduser(args.data_dir))
    os.makedirs(resolved_data_dir, exist_ok=True)
    
    # Pass to handler
    DashboardHandler.data_dir = resolved_data_dir
    
    # Find a free port
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    
    server_address = ("", port)
    httpd = http.server.HTTPServer(server_address, DashboardHandler)
    
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
