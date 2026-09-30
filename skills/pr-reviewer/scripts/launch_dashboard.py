#!/usr/bin/env python3
"""
launch_dashboard.py - Launches a local HTTP server serving the PR review dashboard.

Blocks until the user saves approved review decisions or aborts the session,
then exits with status 0 (saved) or 1 (aborted).
"""

import argparse
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Optional

# Global shutdown flags
server_should_shutdown = False
exit_status = 0


def find_free_port() -> int:
    """Finds an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ReviewDashboardHandler(http.server.BaseHTTPRequestHandler):
    findings_file: Path
    output_file: Path
    html_template_path: Path

    def log_message(self, format: str, *args: object) -> None:
        # Suppress noisy standard request logging to keep agent output clean
        pass

    def send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url_parts = urllib.parse.urlparse(self.path)
        path = url_parts.path

        if path in ("/", "/index.html"):
            if not self.html_template_path.is_file():
                self.send_error(404, "Dashboard HTML template not found")
                return

            try:
                html_content = self.html_template_path.read_text(encoding="utf-8")
                # Inject findings data directly into HTML template
                if self.findings_file.is_file():
                    findings_data = self.findings_file.read_text(encoding="utf-8")
                    # Replace placeholder
                    injected_html = html_content.replace(
                        "/* __INITIAL_DATA__ */ null",
                        findings_data
                    )
                else:
                    injected_html = html_content

                body = injected_html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_error(500, f"Error rendering dashboard: {e}")
            return

        if path == "/api/findings":
            if not self.findings_file.is_file():
                self.send_json(404, {"error": "Findings file not found"})
                return
            try:
                with open(self.findings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        global server_should_shutdown, exit_status
        url_parts = urllib.parse.urlparse(self.path)
        path = url_parts.path

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

        if path == "/api/save":
            try:
                decisions_data = json.loads(body)
                # Ensure parent directory exists
                self.output_file.parent.mkdir(parents=True, exist_ok=True)
                with open(self.output_file, "w", encoding="utf-8") as f:
                    json.dump(decisions_data, f, indent=2)

                self.send_json(200, {"status": "ok", "saved_to": str(self.output_file)})
                exit_status = 0
                server_should_shutdown = True
            except Exception as e:
                self.send_json(400, {"error": f"Invalid payload: {e}"})
            return

        if path == "/api/abort":
            self.send_json(200, {"status": "aborted"})
            exit_status = 1
            server_should_shutdown = True
            return

        self.send_error(404, "Not found")


def run_dashboard_server(
    findings_file: Path,
    output_file: Path,
    html_template_path: Path,
    port: int = 0,
    open_browser: bool = True,
) -> int:
    global server_should_shutdown, exit_status
    server_should_shutdown = False
    exit_status = 0

    if port <= 0:
        port = find_free_port()

    # Configure handler class
    handler = ReviewDashboardHandler
    handler.findings_file = findings_file
    handler.output_file = output_file
    handler.html_template_path = html_template_path

    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"

    print(f"PR Review Dashboard running at: {url}")
    print(f"Reading findings from: {findings_file}")
    print(f"Decisions will be saved to: {output_file}")
    sys.stdout.flush()

    if open_browser:
        webbrowser.open(url)

    # Server loop with non-blocking check
    server.timeout = 0.5
    while not server_should_shutdown:
        try:
            server.handle_request()
        except KeyboardInterrupt:
            print("\nDashboard interrupted by user.")
            exit_status = 1
            break

    server.server_close()
    return exit_status


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch interactive PR review dashboard.")
    parser.add_argument("--findings-file", required=True, help="Path to review_findings.json.")
    parser.add_argument("--output-file", required=True, help="Path to save review_decisions.json.")
    parser.add_argument("--port", type=int, default=0, help="Port to bind (default: random free port).")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser.")
    args = parser.parse_args()

    findings_file = Path(args.findings_file)
    output_file = Path(args.output_file)

    script_dir = Path(__file__).resolve().parent
    html_template_path = script_dir.parent / "assets" / "review_dashboard.html"

    if not findings_file.is_file():
        print(f"Error: Findings file does not exist: {findings_file}", file=sys.stderr)
        return 1

    return run_dashboard_server(
        findings_file=findings_file,
        output_file=output_file,
        html_template_path=html_template_path,
        port=args.port,
        open_browser=not args.no_browser,
    )


if __name__ == "__main__":
    sys.exit(main())
