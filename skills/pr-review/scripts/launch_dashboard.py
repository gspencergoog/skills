#!/usr/bin/env python3
"""
launch_dashboard.py - Launches a local HTTP server serving the PR review dashboard.

Blocks until the user saves approved review decisions or aborts the session,
then exits with status 0 (saved), 1 (aborted by user), or 2 (startup/validation error).
"""

import argparse
import http.server
import json
import re
import secrets
import socket
import sys
import urllib.parse
import webbrowser
from pathlib import Path
from typing import List, Optional, Tuple

from diff_parser import normalize_findings_payload

# Global shutdown flags
server_should_shutdown = False
exit_status = 0


def find_free_port() -> int:
    """Finds an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Metadata fields the dashboard needs to render the header and submit a review.
REQUIRED_META_FIELDS = ("pull_number", "owner", "repo", "head_sha", "title")
PINNED_DECISION_FIELDS = ("pull_number", "owner", "repo", "head_sha")

ALIGNMENT_HEADING_RE = re.compile(
    r"^\s*#{1,4}\s*Intent Alignment\s*[:\-—]\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_alignment_status(markdown_text: str) -> str:
    """Extracts the alignment token from an 'Intent Alignment:' heading.

    Returns 'MATCHES', 'PARTIAL', 'DIVERGES', 'UNSTATED', or '' when no
    recognized alignment heading is present.
    """
    match = ALIGNMENT_HEADING_RE.search(markdown_text or "")
    if not match:
        return ""
    heading_val = match.group(1).casefold()
    if "partial" in heading_val:
        return "PARTIAL"
    if any(
        k in heading_val
        for k in ("diverge", "conflict", "mismatch", "not match", "n't match", "n\u2019t match")
    ):
        return "DIVERGES"
    if "unstated" in heading_val:
        return "UNSTATED"
    if "match" in heading_val:
        return "MATCHES"
    return ""


def _normalize_pr_description(
    raw_desc: object, description_markdown: Optional[str] = None
) -> dict:
    """Normalizes the reviewer-only PR description and alignment status."""
    md = ""
    align = ""
    if isinstance(raw_desc, dict):
        md = str(raw_desc.get("markdown") or "").strip()
        align = str(raw_desc.get("alignment") or "").strip().upper()
    elif isinstance(raw_desc, str):
        md = raw_desc.strip()

    if description_markdown is not None and description_markdown.strip():
        md = description_markdown.strip()
        align = parse_alignment_status(md) or align
    elif md and not align:
        align = parse_alignment_status(md)

    return {"markdown": md, "alignment": align}


def _comment_mentions(comment: str, description: str) -> bool:
    """Returns True if `comment` already contains the text of `description`."""
    needle = description.strip().rstrip(".").casefold()
    return bool(needle) and needle in comment.casefold()


def _normalize_pr_metadata(data: dict, meta: Optional[dict]) -> dict:
    """Merges `pr_meta`, `pr_metadata`, top-level `head_sha`, and external `meta`."""
    pr_metadata = dict(data.get("pr_meta") or {})
    pr_metadata.update(data.get("pr_metadata") or {})
    if not pr_metadata.get("head_sha") and data.get("head_sha"):
        pr_metadata["head_sha"] = data["head_sha"]
    for key, value in (meta or {}).items():
        if not pr_metadata.get(key) and value:
            pr_metadata[key] = value
    return pr_metadata


def _normalize_finding(finding: dict) -> dict:
    """Normalizes a single finding dict and resets `approved` to False."""
    item = dict(finding)
    # Approval is the user's decision in the dashboard, not the agent's.
    item["approved"] = False
    description = (item.get("description") or "").strip()
    comment = (item.get("draft_comment") or item.get("comment_body") or "").strip()
    if description and not _comment_mentions(comment, description):
        comment = f"**{description}**\n\n{comment}" if comment else f"**{description}**"
    item["draft_comment"] = comment
    return item


def normalize_review_data(
    data: dict,
    meta: Optional[dict] = None,
    description_markdown: Optional[str] = None,
) -> Tuple[dict, List[str]]:
    """Converts findings JSON into the shape the dashboard reads.

    Accepts the canonical schema (see references/findings_schema.md) and the
    aliases agents have written in practice: `pr_meta` for `pr_metadata`, a
    top-level `head_sha`, `summary_comment` / `summary` / `verdict` for
    `review_summary`, and `inline_findings` / `general_findings` / `file` /
    `start_line` / `line` / `title` / `body` for `findings`.

    Args:
      data: Parsed contents of review_findings.json.
      meta: Optional parsed pr_meta.json. Its values fill fields missing from
        the findings file; they never override values already present.
      description_markdown: Optional Markdown string from `--description-file`
        containing the blind-first PR description and intent annotations.

    Returns:
      A (normalized, warnings) tuple. `normalized` has `pr_metadata`,
      `review_summary`, `pr_description`, and `findings`. Every finding starts
      with `approved: False`, and its `draft_comment` opens with the finding's
      `description` so the editor shows the full comment that gets posted.
      `warnings` names each required metadata field that is still missing.
    """
    canonical_input = normalize_findings_payload(data)
    normalized = dict(canonical_input)
    pr_metadata = _normalize_pr_metadata(canonical_input, meta)
    normalized["pr_metadata"] = pr_metadata
    normalized.pop("pr_meta", None)

    summary = dict(canonical_input.get("review_summary") or {})
    if not summary.get("overview") and canonical_input.get("summary_comment"):
        summary["overview"] = canonical_input["summary_comment"]
    if not summary.get("verdict") and canonical_input.get("verdict"):
        summary["verdict"] = canonical_input["verdict"]
    normalized["review_summary"] = summary
    normalized.pop("summary_comment", None)
    normalized.pop("verdict", None)

    normalized["pr_description"] = _normalize_pr_description(
        canonical_input.get("pr_description"), description_markdown
    )
    normalized["findings"] = [
        _normalize_finding(finding)
        for finding in (canonical_input.get("findings") or [])
    ]

    warnings = [
        f"pr_metadata.{field} is missing"
        for field in REQUIRED_META_FIELDS
        if not pr_metadata.get(field)
    ]
    return normalized, warnings


def load_review_data(
    findings_file: Path,
    meta_file: Optional[Path] = None,
    description_file: Optional[Path] = None,
) -> Tuple[dict, List[str]]:
    """Reads the findings file (and optional meta/description files) and normalizes them."""
    data = json.loads(findings_file.read_text(encoding="utf-8"))
    meta = None
    if meta_file is not None and meta_file.is_file():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    description_md = None
    if description_file is not None and description_file.is_file():
        description_md = description_file.read_text(encoding="utf-8")
    return normalize_review_data(data, meta, description_md)


def script_safe_json(data: dict) -> str:
    """Serializes `data` for embedding inside an inline <script> element.

    Escapes `</` so PR content such as `</script>` in a comment or code
    snippet can't end the script element early.
    """
    return json.dumps(data).replace("</", "<\\/")


def _is_allowed_origin(origin: str, server_port: int) -> bool:
    """Returns True if `origin` is empty or matches http://127.0.0.1:<port> or http://localhost:<port>."""
    if not origin:
        return True
    parsed = urllib.parse.urlparse(origin)
    return (
        parsed.scheme == "http"
        and (parsed.hostname or "").lower() in ("127.0.0.1", "localhost")
        and parsed.port in (None, server_port)
    )


def _is_allowed_host(host: str) -> bool:
    """Returns True if `host` is empty or targets 127.0.0.1 or localhost."""
    if not host:
        return True
    hostname = host.split(":", 1)[0].strip().lower()
    return hostname in ("127.0.0.1", "localhost")


class ReviewDashboardHandler(http.server.BaseHTTPRequestHandler):
    findings_file: Path
    output_file: Path
    html_template_path: Path
    meta_file: Optional[Path] = None
    description_file: Optional[Path] = None
    review_token: str = ""

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

    def _check_request_security(self, require_token: bool) -> bool:
        """Validates Host, Origin, and optional X-Review-Token headers against DNS rebinding and CSRF."""
        if not _is_allowed_host(self.headers.get("Host", "")):
            self.send_json(403, {"error": "Forbidden Host header"})
            return False

        if not _is_allowed_origin(self.headers.get("Origin", ""), self.server.server_port):
            self.send_json(403, {"error": "Cross-origin request rejected"})
            return False

        if require_token and self.review_token:
            provided = self.headers.get("X-Review-Token", "")
            if not secrets.compare_digest(provided, self.review_token):
                self.send_json(403, {"error": "Missing or invalid X-Review-Token"})
                return False

        return True

    def _serve_dashboard_html(self) -> None:
        if not self.html_template_path.is_file():
            self.send_error(404, "Dashboard HTML template not found")
            return
        try:
            html_content = self.html_template_path.read_text(encoding="utf-8")
            injected_html = html_content.replace(
                '/* __REVIEW_TOKEN__ */ ""',
                json.dumps(self.review_token),
            )
            if self.findings_file.is_file():
                data, _ = load_review_data(
                    self.findings_file, self.meta_file, self.description_file
                )
                injected_html = injected_html.replace(
                    "/* __INITIAL_DATA__ */ null",
                    script_safe_json(data),
                )

            body = injected_html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, f"Error rendering dashboard: {e}")

    def _serve_findings_json(self) -> None:
        if not self.findings_file.is_file():
            self.send_json(404, {"error": "Findings file not found"})
            return
        try:
            data, _ = load_review_data(
                self.findings_file, self.meta_file, self.description_file
            )
            self.send_json(200, data)
        except Exception as e:
            self.send_json(500, {"error": str(e)})

    def _handle_save(self, body: str) -> None:
        global server_should_shutdown, exit_status
        try:
            decisions_data = json.loads(body)
            if self.findings_file.is_file():
                pinned_data, _ = load_review_data(
                    self.findings_file, self.meta_file, self.description_file
                )
                pinned_meta = pinned_data.get("pr_metadata") or {}
                for field in PINNED_DECISION_FIELDS:
                    if pinned_meta.get(field):
                        decisions_data[field] = pinned_meta[field]

            self.output_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.output_file, "w", encoding="utf-8") as f:
                json.dump(decisions_data, f, indent=2)

            self.send_json(200, {"status": "ok", "saved_to": str(self.output_file)})
            exit_status = 0
            server_should_shutdown = True
        except Exception as e:
            self.send_json(400, {"error": f"Invalid payload: {e}"})

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path

        if path in ("/", "/index.html"):
            if not self._check_request_security(require_token=False):
                return
            self._serve_dashboard_html()
            return

        if path == "/api/findings":
            if not self._check_request_security(require_token=True):
                return
            self._serve_findings_json()
            return

        self.send_error(404, "Not found")

    def do_POST(self) -> None:
        global server_should_shutdown, exit_status
        if not self._check_request_security(require_token=True):
            return

        path = urllib.parse.urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""

        if path == "/api/save":
            self._handle_save(body)
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
    meta_file: Optional[Path] = None,
    description_file: Optional[Path] = None,
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
    handler.meta_file = meta_file
    handler.description_file = description_file
    handler.review_token = secrets.token_urlsafe(32)

    # Report missing metadata before serving; the page also shows a banner.
    normalized, warnings = load_review_data(findings_file, meta_file, description_file)
    for warning in warnings:
        print(
            f"Warning: {warning}; the dashboard header or submission will be incomplete.",
            file=sys.stderr,
        )
    sys.stderr.flush()

    server = http.server.HTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}/"

    print(f"PR Review Dashboard running at: {url}")
    print(f"Loaded {len(normalized.get('findings', []))} findings from: {findings_file}")
    if meta_file is not None:
        print(f"Reading PR metadata from: {meta_file}")
    if description_file is not None:
        print(f"Reading PR description from: {description_file}")
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Launch interactive PR review dashboard.")
    parser.add_argument("--findings-file", required=True, help="Path to review_findings.json.")
    parser.add_argument("--output-file", required=True, help="Path to save review_decisions.json.")
    parser.add_argument(
        "--meta-file",
        help="Path to pr_meta.json from review_engine.py; fills PR metadata missing from the findings file.",
    )
    parser.add_argument(
        "--description-file",
        help="Path to pr_description.md containing the blind-first PR description and intent annotations.",
    )
    parser.add_argument("--port", type=int, default=0, help="Port to bind (default: random free port).")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open browser.")
    args = parser.parse_args(argv)

    findings_file = Path(args.findings_file)
    output_file = Path(args.output_file)
    meta_file = Path(args.meta_file) if args.meta_file else None
    description_file = Path(args.description_file) if args.description_file else None

    script_dir = Path(__file__).resolve().parent
    html_template_path = script_dir.parent / "assets" / "review_dashboard.html"

    if not html_template_path.is_file():
        print(f"Error: Dashboard HTML template does not exist: {html_template_path}", file=sys.stderr)
        return 2
    if not findings_file.is_file():
        print(f"Error: Findings file does not exist: {findings_file}", file=sys.stderr)
        return 2
    if meta_file is not None and not meta_file.is_file():
        print(f"Error: Meta file does not exist: {meta_file}", file=sys.stderr)
        return 2
    if description_file is not None and not description_file.is_file():
        print(f"Error: Description file does not exist: {description_file}", file=sys.stderr)
        return 2
    try:
        load_review_data(findings_file, meta_file, description_file)
    except (OSError, ValueError) as e:
        print(f"Error: Could not read review data: {e}", file=sys.stderr)
        return 2

    return run_dashboard_server(
        findings_file=findings_file,
        output_file=output_file,
        html_template_path=html_template_path,
        port=args.port,
        open_browser=not args.no_browser,
        meta_file=meta_file,
        description_file=description_file,
    )


if __name__ == "__main__":
    sys.exit(main())


