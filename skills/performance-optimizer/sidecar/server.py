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

import http.server
import json
import os
import urllib.parse

class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def get_workspace_path(self):
        try:
            with open("workspace_path.txt", "r") as f:
                return f.read().strip()
        except Exception:
            return ""

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        
        if parsed_url.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            with open("dashboard.html", "rb") as f:
                self.wfile.write(f.read())
                
        elif parsed_url.path == "/api/history":
            workspace = self.get_workspace_path()
            if not workspace:
                self.send_error_json(400, "Workspace path not configured in sidecar.")
                return
                
            history_path = os.path.join(workspace, "optimization_history.json")
            if not os.path.exists(history_path):
                # Return empty list if optimization loop has not run yet
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps([]).encode("utf-8"))
                return
                
            try:
                with open(history_path, "r") as f:
                    history_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(history_data.encode("utf-8"))
            except Exception as e:
                self.send_error_json(500, f"Error reading optimization history: {str(e)}")
                
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def send_error_json(self, status, message):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode("utf-8"))

def run():
    port_str = os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT")
    port = int(port_str) if port_str else 8080
    
    server_address = ("", port)
    httpd = http.server.HTTPServer(server_address, DashboardHandler)
    print(f"Starting Performance Optimizer Dashboard server on port {port}...")
    httpd.serve_forever()

if __name__ == "__main__":
    run()
