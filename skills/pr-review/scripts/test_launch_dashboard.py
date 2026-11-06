"""
test_launch_dashboard.py - Unit tests for launch_dashboard.py.
"""

import http.client
import http.server
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar

from launch_dashboard import (
    ReviewDashboardHandler,
    find_free_port,
    main,
    normalize_review_data,
    parse_alignment_status,
)


class TestLaunchDashboard(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

        self.findings_file = self.tmp_path / "findings.json"
        self.output_file = self.tmp_path / "decisions.json"
        self.template_file = self.tmp_path / "dashboard.html"

        # Write dummy findings
        self.test_findings = {
            "pr_metadata": {
                "owner": "octocat",
                "repo": "hello-world",
                "pull_number": 42,
                "head_sha": "pinned_sha_123",
                "title": "Add test feature",
                "author": "octocat",
            },
            "findings": [
                {
                    "id": "f1",
                    "file_path": "lib/foo.dart",
                    "line_start": 10,
                    "line_end": 12,
                    "description": "Unchecked null",
                    "approved": True,
                    "draft_comment": "Check null",
                }
            ],
        }
        self.findings_file.write_text(json.dumps(self.test_findings), encoding="utf-8")
        self.template_file.write_text(
            '<html><body>const REVIEW_TOKEN = /* __REVIEW_TOKEN__ */ ""; /* __INITIAL_DATA__ */ null</body></html>',
            encoding="utf-8",
        )

        self.port = find_free_port()
        ReviewDashboardHandler.findings_file = self.findings_file
        ReviewDashboardHandler.output_file = self.output_file
        ReviewDashboardHandler.html_template_path = self.template_file
        ReviewDashboardHandler.meta_file = None
        ReviewDashboardHandler.description_file = None
        ReviewDashboardHandler.review_token = "test-secret-token"

        self.server = http.server.HTTPServer(
            ("127.0.0.1", self.port), ReviewDashboardHandler
        )
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        ReviewDashboardHandler.meta_file = None
        ReviewDashboardHandler.description_file = None
        ReviewDashboardHandler.review_token = ""
        self.temp_dir.cleanup()

    def _get(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        req_headers = {"X-Review-Token": "test-secret-token"}
        if headers is not None:
            req_headers.update(headers)
        conn.request("GET", path, headers=req_headers)
        resp = conn.getresponse()
        status, body = resp.status, resp.read().decode("utf-8")
        conn.close()
        return status, body

    def test_find_free_port(self):
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)

    def test_get_root_injects_findings_and_token(self):
        status, body = self._get("/", headers={"X-Review-Token": ""})
        self.assertEqual(status, 200)

        self.assertIn('"pull_number": 42', body)
        self.assertIn("Unchecked null", body)
        self.assertIn('"test-secret-token"', body)

    def test_get_root_escapes_script_close(self):
        self.test_findings["findings"][0]["draft_comment"] = "Avoid </script><b>x</b>"
        self.findings_file.write_text(json.dumps(self.test_findings), encoding="utf-8")
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertNotIn("</script>", body)
        self.assertIn("<\\/script>", body)

    def test_get_api_findings(self):
        status, body = self._get("/api/findings")
        self.assertEqual(status, 200)
        data = json.loads(body)

        self.assertEqual(data["pr_metadata"]["pull_number"], 42)
        self.assertEqual(len(data["findings"]), 1)
        self.assertTrue(data["findings"][0]["approved"])

    def test_get_api_findings_normalizes_aliases_and_meta_file(self):
        self.findings_file.write_text(
            json.dumps(
                {
                    "pr_meta": {"pull_number": 2907, "owner": "octo-org"},
                    "head_sha": "010d93e",
                    "verdict": "REQUEST_CHANGES",
                    "summary_comment": "Overall fine.",
                    "findings": [],
                }
            ),
            encoding="utf-8",
        )
        meta_file = self.tmp_path / "pr_meta.json"
        meta_file.write_text(
            json.dumps({"repo": "widgets", "title": "fix: thing", "pull_number": 1}),
            encoding="utf-8",
        )
        ReviewDashboardHandler.meta_file = meta_file

        status, body = self._get("/api/findings")
        self.assertEqual(status, 200)
        data = json.loads(body)
        meta = data["pr_metadata"]
        self.assertEqual(meta["pull_number"], 2907)  # findings file wins
        self.assertEqual(meta["repo"], "widgets")  # filled from meta file
        self.assertEqual(meta["title"], "fix: thing")
        self.assertEqual(meta["head_sha"], "010d93e")
        self.assertEqual(
            data["review_summary"],
            {"overview": "Overall fine.", "verdict": "REQUEST_CHANGES"},
        )
        self.assertNotIn("pr_meta", data)

    def test_get_api_findings_loads_description_file(self):
        desc_file = self.tmp_path / "pr_description.md"
        desc_file.write_text(
            "## Intent Alignment: ⚠️ Partial Divergence\n\n### Summary\nAvoid </script> tags.",
            encoding="utf-8",
        )
        ReviewDashboardHandler.description_file = desc_file

        status, body = self._get("/api/findings")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["pr_description"]["alignment"], "PARTIAL")
        self.assertIn("### Summary", data["pr_description"]["markdown"])

        # Also verify root HTML escapes </script> inside pr_description
        root_status, root_body = self._get("/")
        self.assertEqual(root_status, 200)
        self.assertNotIn("</script>", root_body)
        self.assertIn("<\\/script>", root_body)

    def test_post_api_save_pins_metadata_and_checks_csrf(self):
        decisions_payload = {
            "owner": "attacker-org",
            "repo": "evil-repo",
            "pull_number": 9999,
            "head_sha": "spoofed_sha",
            "verdict": "COMMENT",
            "summary_comment": "LGTM with minor nits",
            "findings": [
                {
                    "id": "f1",
                    "approved": True,
                    "draft_comment": "Please fix null check",
                }
            ],
        }

        # 1. Rejects missing X-Review-Token (M3)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST",
            "/api/save",
            json.dumps(decisions_payload),
            {"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        conn.close()

        # 2. Rejects cross-origin Origin header (M3)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST",
            "/api/save",
            json.dumps(decisions_payload),
            {
                "Content-Type": "application/json",
                "X-Review-Token": "test-secret-token",
                "Origin": "https://evil.example",
            },
        )
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        conn.close()

        # 3. Rejects DNS-rebinding Host header (M3)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST",
            "/api/save",
            json.dumps(decisions_payload),
            {
                "Content-Type": "application/json",
                "X-Review-Token": "test-secret-token",
                "Host": "attacker.example:8080",
            },
        )
        resp = conn.getresponse()
        resp.read()
        self.assertEqual(resp.status, 403)
        conn.close()

        # 4. Succeeds with valid token and pins server-side PR coordinates (M3)
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {
            "Content-Type": "application/json",
            "X-Review-Token": "test-secret-token",
        }
        conn.request("POST", "/api/save", json.dumps(decisions_payload), headers)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        res_data = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(res_data["status"], "ok")
        self.assertTrue(self.output_file.is_file())

        saved = json.loads(self.output_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["verdict"], "COMMENT")
        self.assertEqual(saved["owner"], "octocat")
        self.assertEqual(saved["repo"], "hello-world")
        self.assertEqual(saved["pull_number"], 42)
        self.assertEqual(saved["head_sha"], "pinned_sha_123")
        self.assertEqual(saved["findings"][0]["draft_comment"], "Please fix null check")

    def test_post_api_abort(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request(
            "POST", "/api/abort", headers={"X-Review-Token": "test-secret-token"}
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        res_data = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(res_data["status"], "aborted")

    def test_main_returns_exit_code_2_on_missing_files(self):
        rc = main(
            [
                "--findings-file",
                str(self.tmp_path / "nonexistent.json"),
                "--output-file",
                str(self.output_file),
            ]
        )
        self.assertEqual(rc, 2)

    def test_main_dump_json_prints_normalized_payload(self):
        meta_file = self.tmp_path / "pr_meta.json"
        meta_file.write_text(
            json.dumps(
                {
                    "owner": "octocat",
                    "repo": "hello-world",
                    "pull_number": 42,
                    "head_sha": "pinned_sha_123",
                    "title": "Fix bug",
                }
            ),
            encoding="utf-8",
        )
        desc_file = self.tmp_path / "pr_description.md"
        desc_file.write_text(
            "## Intent Alignment: Matches Stated Purpose\n\nBody.", encoding="utf-8"
        )

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(
                [
                    "--dump-json",
                    "--findings-file",
                    str(self.findings_file),
                    "--meta-file",
                    str(meta_file),
                    "--description-file",
                    str(desc_file),
                    "--output-file",
                    str(self.output_file),
                ]
            )
        self.assertEqual(rc, 0)
        dumped = json.loads(buf.getvalue())
        self.assertEqual(dumped["pr_metadata"]["owner"], "octocat")
        self.assertEqual(dumped["pr_description"]["alignment"], "MATCHES")
        self.assertEqual(dumped["_warnings"], [])
        self.assertEqual(len(dumped["findings"]), 1)

    def test_main_dump_json_returns_2_on_missing_findings(self):
        rc = main(
            [
                "--dump-json",
                "--findings-file",
                str(self.tmp_path / "missing.json"),
            ]
        )
        self.assertEqual(rc, 2)


class TestNormalizeReviewData(unittest.TestCase):
    FULL_META: ClassVar[dict[str, object]] = {
        "pull_number": 7,
        "owner": "o",
        "repo": "r",
        "head_sha": "abc123",
        "title": "T",
    }

    def test_defaults_findings_to_approved(self):
        data, _ = normalize_review_data(
            {"pr_metadata": self.FULL_META, "findings": [{"approved": False}, {}]}
        )
        self.assertEqual([f["approved"] for f in data["findings"]], [True, True])

    def test_preserves_draft_comment_without_prefixing_description(self):
        data, _ = normalize_review_data(
            {
                "findings": [
                    {
                        "description": "Promise not awaited.",
                        "draft_comment": "Add await.",
                    }
                ]
            }
        )
        self.assertEqual(data["findings"][0]["draft_comment"], "Add await.")

    def test_no_duplicate_description(self):
        comment = "The promise is not awaited, so errors are lost. Add await."
        data, _ = normalize_review_data(
            {
                "findings": [
                    {"description": "Promise is NOT awaited.", "draft_comment": comment}
                ]
            }
        )
        self.assertEqual(data["findings"][0]["draft_comment"], comment)

    def test_empty_description_leaves_comment(self):
        data, _ = normalize_review_data({"findings": [{"draft_comment": "Add await."}]})
        self.assertEqual(data["findings"][0]["draft_comment"], "Add await.")

    def test_description_only(self):
        data, _ = normalize_review_data(
            {"findings": [{"description": "Unused import"}]}
        )
        self.assertEqual(data["findings"][0]["draft_comment"], "Unused import")

    def test_pr_metadata_wins_over_pr_meta(self):
        data, _ = normalize_review_data(
            {
                "pr_metadata": {"title": "canonical"},
                "pr_meta": {"title": "alias", "repo": "r"},
            }
        )
        self.assertEqual(data["pr_metadata"]["title"], "canonical")
        self.assertEqual(data["pr_metadata"]["repo"], "r")

    def test_meta_fills_gaps_only(self):
        data, warnings = normalize_review_data(
            {"pr_metadata": {"title": "keep"}}, meta=dict(self.FULL_META, title="other")
        )
        self.assertEqual(data["pr_metadata"]["title"], "keep")
        self.assertEqual(data["pr_metadata"]["head_sha"], "abc123")
        self.assertEqual(warnings, [])

    def test_warns_on_missing_metadata(self):
        _, warnings = normalize_review_data(
            {"pr_metadata": {"title": "T"}, "findings": []}
        )
        self.assertEqual(
            warnings,
            [
                "pr_metadata.pull_number is missing",
                "pr_metadata.owner is missing",
                "pr_metadata.repo is missing",
                "pr_metadata.head_sha is missing",
            ],
        )

    def test_does_not_mutate_input(self):
        raw = {
            "findings": [{"approved": True, "description": "d", "draft_comment": "c"}]
        }
        normalize_review_data(raw)
        self.assertTrue(raw["findings"][0]["approved"])
        self.assertEqual(raw["findings"][0]["draft_comment"], "c")

    def test_parse_alignment_status(self):
        self.assertEqual(
            parse_alignment_status("## Intent Alignment: ✅ Matches Stated Purpose"),
            "MATCHES",
        )
        self.assertEqual(
            parse_alignment_status("### Intent Alignment: ⚠️ Partial Divergence"),
            "PARTIAL",
        )
        self.assertEqual(
            parse_alignment_status(
                "## Intent Alignment - Diverges from PR Description"
            ),
            "DIVERGES",
        )
        self.assertEqual(
            parse_alignment_status(
                "## Intent Alignment: Does Not Match Stated Purpose"
            ),
            "DIVERGES",
        )
        self.assertEqual(
            parse_alignment_status("## Intent Alignment: Doesn't Match Stated Purpose"),
            "DIVERGES",
        )
        self.assertEqual(
            parse_alignment_status("## Intent Alignment: Unstated Scope"),
            "UNSTATED",
        )
        self.assertEqual(parse_alignment_status("## Summary\nNo alignment header."), "")

    def test_normalizes_pr_description_variants(self):
        # String alias in findings JSON
        data_str, _ = normalize_review_data(
            {"pr_description": "## Intent Alignment: Matches\n\nBody text."}
        )
        self.assertEqual(
            data_str["pr_description"],
            {
                "markdown": "## Intent Alignment: Matches\n\nBody text.",
                "alignment": "MATCHES",
            },
        )

        # description_markdown parameter overrides inline findings JSON
        data_override, _ = normalize_review_data(
            {"pr_description": {"markdown": "Old", "alignment": "MATCHES"}},
            description_markdown="## Intent Alignment: Unstated Scope\n\nNew body.",
        )
        self.assertEqual(
            data_override["pr_description"],
            {
                "markdown": "## Intent Alignment: Unstated Scope\n\nNew body.",
                "alignment": "UNSTATED",
            },
        )

    def test_normalizes_inline_findings_schema_in_launcher(self):
        data, _ = normalize_review_data(
            {
                "summary": "Overall assessment.",
                "verdict": "COMMENT",
                "inline_findings": [
                    {
                        "file": "src/app.py",
                        "start_line": 10,
                        "line": 12,
                        "severity": "MEDIUM",
                        "title": "Check bounds",
                        "body": "Bounds are not checked.",
                    }
                ],
            }
        )
        self.assertEqual(data["review_summary"]["overview"], "Overall assessment.")
        self.assertEqual(len(data["findings"]), 1)
        f = data["findings"][0]
        self.assertEqual(f["file_path"], "src/app.py")
        self.assertEqual(f["line_start"], 10)
        self.assertEqual(f["line_end"], 12)
        self.assertEqual(f["description"], "Check bounds")
        self.assertEqual(f["draft_comment"], "Bounds are not checked.")

    @unittest.skipUnless(
        shutil.which("node"), "node is required for JS Markdown XSS tests"
    )
    def test_dashboard_markdown_renderer_xss_safety(self):
        html_path = (
            Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
        )
        html = html_path.read_text(encoding="utf-8")
        script_match = re.search(r"<script>([\s\S]*?)</script>", html)
        self.assertIsNotNone(script_match)
        js_code = script_match.group(1)
        # Stub window.addEventListener so script loads cleanly in node
        harness = (
            "const window = { addEventListener: () => {} };\n"
            "const document = {};\n"
            + js_code
            + "\n"
            + """
const payloads = [
  "# Heading <img src=x onerror=alert(1)>",
  "Click [here](javascript:alert(1)) or [ok](https://example.com/a\\"onmouseover=\\"alert(1))",
  "> [!WARNING] <script>alert(1)</script>\\n> Body `<b>code</b>` **bold**",
  "```js\\"><img src=x>\\nconst x = 1 < 2;\\n```",
  "| Col <script>alert(1)</script> | Val |\\n| --- | --- |\\n| <img src=x onerror=alert(1)> | $\\\\text{<script>evil()</script>}$ |",
  "```mermaid\\nflowchart LR\\n  A[\\"<img src=x onerror=alert(1)>\\"] -->|<script>alert(1)</script>| B\\n```"
];
const out = payloads.map(p => renderMarkdownSafe(p)).join("\\n---\\n");
console.log(out);
"""
        )
        proc = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = proc.stdout
        self.assertNotIn("<img", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn('href="javascript:', rendered)
        self.assertNotIn('onmouseover="alert', rendered)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", rendered)
        self.assertIn("callout-warning", rendered)
        self.assertIn("<code>&lt;b&gt;code&lt;/b&gt;</code>", rendered)
        self.assertIn('<table class="md-table">', rendered)
        self.assertIn("<mtext>&lt;script&gt;evil()&lt;/script&gt;</mtext>", rendered)
        self.assertIn('<svg class="md-mermaid-svg"', rendered)

    @unittest.skipUnless(
        shutil.which("node"), "node is required for JS Markdown table tests"
    )
    def test_dashboard_markdown_tables_rendering(self):
        html_path = (
            Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
        )
        html = html_path.read_text(encoding="utf-8")
        script_match = re.search(r"<script>([\s\S]*?)</script>", html)
        self.assertIsNotNone(script_match)
        js_code = script_match.group(1)
        harness = (
            "const window = { addEventListener: () => {} };\n"
            "const document = {};\n"
            + js_code
            + "\n"
            + r"""
const md = [
  "Intro paragraph before table",
  "| Metric | Formula | Complexity | Notes |",
  "| :--- | :---: | ---: | --- |",
  "| Latency | $|x| + \\|y\\| \\le 1$ | `a | b` | Escaped \\| pipe and **bold** |",
  "",
  "> [!NOTE]",
  "> | Inside | Callout |",
  "> | --- | --- |",
  "> | Yes | `ok` |"
].join("\n");
console.log(renderMarkdownSafe(md));
"""
        )
        proc = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = proc.stdout
        self.assertIn('<div class="md-table-wrap"><table class="md-table">', rendered)
        self.assertIn('<th style="text-align:left">Metric</th>', rendered)
        self.assertIn('<th style="text-align:center">Formula</th>', rendered)
        self.assertIn('<th style="text-align:right">Complexity</th>', rendered)
        self.assertIn("<code>a | b</code>", rendered)
        self.assertIn("Escaped | pipe and <strong>bold</strong>", rendered)
        self.assertIn("<mo>‖</mo>", rendered)
        self.assertIn("callout-note", rendered)
        self.assertEqual(rendered.count('<table class="md-table">'), 2)

    @unittest.skipUnless(
        shutil.which("node"), "node is required for JS LaTeX math tests"
    )
    def test_dashboard_markdown_latex_math_rendering(self):
        html_path = (
            Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
        )
        html = html_path.read_text(encoding="utf-8")
        script_match = re.search(r"<script>([\s\S]*?)</script>", html)
        self.assertIsNotNone(script_match)
        js_code = script_match.group(1)
        harness = (
            "const window = { addEventListener: () => {} };\n"
            "const document = {};\n"
            + js_code
            + "\n"
            + r"""
const md = [
  "Inline $\\frac{\\alpha_1 + \\beta^2}{\\sqrt[3]{x}}$ and \\(\\mathcal{O}(n \\log n) \\in \\mathbb{R}\\).",
  "Price is \\$100 to \\$200 and shell var is `$HOME` or `$1`.",
  "",
  "$$",
  "A = \\begin{pmatrix} 1 & 2 \\\\ 3 & 4 \\end{pmatrix}, \\quad \\hat{y} = \\mathbf{W}x",
  "$$",
  "",
  "\\[",
  "\\sum_{i=1}^{n} x_i \\ge 0",
  "\\]"
].join("\n");
console.log(renderMarkdownSafe(md));
"""
        )
        proc = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = proc.stdout
        self.assertIn(
            '<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline">',
            rendered,
        )
        self.assertIn(
            '<div class="md-math-block"><math xmlns="http://www.w3.org/1998/Math/MathML" display="block">',
            rendered,
        )
        self.assertIn("<mfrac>", rendered)
        self.assertIn("<mroot>", rendered)
        self.assertIn("<mtable>", rendered)
        self.assertIn("<mover>", rendered)
        self.assertIn("𝒪", rendered)
        self.assertIn("ℝ", rendered)
        self.assertIn("Price is $100 to $200", rendered)
        self.assertIn("<code>$HOME</code>", rendered)

    @unittest.skipUnless(shutil.which("node"), "node is required for JS Mermaid tests")
    def test_dashboard_markdown_mermaid_rendering(self):
        html_path = (
            Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
        )
        html = html_path.read_text(encoding="utf-8")
        script_match = re.search(r"<script>([\s\S]*?)</script>", html)
        self.assertIsNotNone(script_match)
        js_code = script_match.group(1)
        harness = (
            "const window = { addEventListener: () => {} };\n"
            "const document = {};\n"
            + js_code
            + "\n"
            + r"""
const md = [
  "```mermaid",
  "flowchart LR",
  "  A[Client] -->|HTTP| B(Server)",
  "  B --> C{Valid?}",
  "```",
  "",
  "```mermaid",
  "stateDiagram-v2",
  "  [*] --> Idle",
  "  Idle --> Active : start",
  "  Active --> [*]",
  "```",
  "",
  "```mermaid",
  "sequenceDiagram",
  "  participant U as User",
  "  participant S as Service",
  "  U->>S: Request",
  "  S-->>U: Response",
  "```",
  "",
  "```mermaid",
  "pie showData title Latency Budget",
  "  \"Parse\" : 30",
  "  \"Render\" : 70",
  "```",
  "",
  "```mermaid",
  "xychart-beta",
  "  title \"Throughput\"",
  "  x-axis [\"Q1\", \"Q2\"]",
  "  y-axis \"req/s\" 0 --> 100",
  "  bar [40, 85]",
  "  line [35, 80]",
  "```",
  "",
  "> [!NOTE]",
  "> ```mermaid",
  "> graph TD",
  ">   X --> Y",
  "> ```",
  "",
  "```mermaid",
  "gantt",
  "  title Unsupported Diagram",
  "```"
].join("\n");
console.log(renderMarkdownSafe(md));
"""
        )
        proc = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        rendered = proc.stdout
        self.assertEqual(rendered.count('<svg class="md-mermaid-svg"'), 6)
        self.assertIn("md-mermaid-node", rendered)
        self.assertIn("md-mermaid-actor", rendered)
        self.assertIn('aria-label="Latency Budget"', rendered)
        self.assertIn('aria-label="Throughput"', rendered)
        self.assertIn(
            '<pre class="code-box md-mermaid-fallback" data-lang="mermaid">',
            rendered,
        )

    def test_normalize_review_data_preserves_enriched_fields(self):
        data = {
            "pr_metadata": {
                "owner": "octocat",
                "repo": "hello-world",
                "pull_number": 42,
                "head_sha": "010d93e",
            },
            "review_summary": {
                "overview": "Summary overview.",
                "verdict": "REQUEST_CHANGES",
                "checked_clean": ["Memory leak: Executed - clean"],
                "structural_assessment": {"implementer": "Clean"},
            },
            "findings": [
                {
                    "id": "H1",
                    "file_path": "src/parser.ts",
                    "line_start": 10,
                    "line_end": 12,
                    "severity": "high",
                    "evidence_tier": "Read",
                    "evidence_detail": "parser.ts:10",
                    "why": "Silent drop",
                    "description": "Unawaited",
                    "draft_comment": "Await this.",
                }
            ],
        }
        normalized, _warnings = normalize_review_data(data)
        summary = normalized["review_summary"]
        self.assertEqual(summary["checked_clean"], ["Memory leak: Executed - clean"])
        self.assertEqual(summary["structural_assessment"], {"implementer": "Clean"})
        finding = normalized["findings"][0]
        self.assertEqual(finding["evidence_tier"], "Read")
        self.assertEqual(finding["why"], "Silent drop")

    def test_normalize_review_data_does_not_duplicate_questions(self):
        """Top-level `questions` survive the real two-pass path exactly once.

        diff_parser.normalize_findings_payload runs first (Step 3.4), then
        launch_dashboard.normalize_review_data runs it again. Without the
        `questions` pop in diff_parser, the second pass re-collects them.
        """
        from diff_parser import normalize_findings_payload

        raw = {
            "pr_metadata": {
                "owner": "octocat",
                "repo": "hello-world",
                "pull_number": 42,
                "head_sha": "010d93e",
            },
            "review_summary": {
                "overview": "Summary overview.",
                "verdict": "REQUEST_CHANGES",
            },
            "findings": [
                {
                    "id": "H1",
                    "file_path": "src/parser.ts",
                    "line_start": 10,
                    "line_end": 12,
                    "severity": "high",
                    "draft_comment": "Fix this.",
                },
            ],
            "questions": [
                {
                    "id": "Q1",
                    "file_path": "src/parser.ts",
                    "line_start": 20,
                    "line_end": 20,
                    "severity": "question",
                    "draft_comment": "**Question**: Is this needed?",
                },
            ],
        }
        pass1 = normalize_findings_payload(raw)
        normalized, _warnings = normalize_review_data(pass1)
        self.assertEqual([f["id"] for f in normalized["findings"]], ["H1", "Q1"])
        self.assertNotIn("questions", normalized)

    @unittest.skipUnless(shutil.which("node"), "node is required for JS tab tests")
    def test_dashboard_write_preview_tabs_and_submission(self):
        html_path = (
            Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
        )
        html = html_path.read_text(encoding="utf-8")
        script_match = re.search(r"<script>([\s\S]*?)</script>", html)
        self.assertIsNotNone(script_match)
        js_code = script_match.group(1)

        harness = (
            """
const elements = {};
function makeEl(id, initial = {}) {
  const classes = new Set(initial.classes || []);
  const attrs = { ...(initial.attrs || {}) };
  return {
    id,
    value: initial.value || '',
    innerHTML: '',
    textContent: '',
    style: { display: initial.display || 'block', height: '' },
    scrollHeight: 100,
    clientHeight: 100,
    classList: {
      toggle(cls, force) {
        if (force === undefined) {
          if (classes.has(cls)) classes.delete(cls);
          else classes.add(cls);
        } else if (force) {
          classes.add(cls);
        } else {
          classes.delete(cls);
        }
      },
      add(cls) { classes.add(cls); },
      remove(cls) { classes.delete(cls); },
      contains(cls) { return classes.has(cls); }
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k]; }
  };
}
elements['summary-tab-write'] = makeEl('summary-tab-write', { classes: ['active'], attrs: { 'aria-selected': 'true' } });
elements['summary-tab-preview'] = makeEl('summary-tab-preview', { classes: [], attrs: { 'aria-selected': 'false' } });
elements['review-summary-text'] = makeEl('review-summary-text', { value: 'Summary with [`fn`](file:///tmp/pr-review-123/a.ts#L1).' });
elements['review-summary-preview'] = makeEl('review-summary-preview', { display: 'none' });
elements['comment-tab-write-0'] = makeEl('comment-tab-write-0', { classes: ['active'], attrs: { 'aria-selected': 'true' } });
elements['comment-tab-preview-0'] = makeEl('comment-tab-preview-0', { classes: [], attrs: { 'aria-selected': 'false' } });
elements['comment-editor-0'] = makeEl('comment-editor-0', { value: 'Initial comment' });
elements['comment-preview-0'] = makeEl('comment-preview-0', { display: 'none' });

let savedPayload = null;
const window = {
  addEventListener: () => {},
  __dashboardHost: {
    kind: 'mcp-app',
    save: async (p) => { savedPayload = p; }
  }
};
const document = {
  getElementById: (id) => elements[id] || null,
  querySelector: (sel) => sel === 'input[name="verdict"]:checked' ? { value: 'APPROVE' } : null,
  querySelectorAll: () => [],
  createElement: () => ({ style: {}, appendChild: () => {} }),
  body: { appendChild: () => {} }
};
"""
            + js_code
            + """
reviewData = {
  pr_metadata: { pull_number: 10, owner: 'o', repo: 'r', head_sha: 'abc1234' },
  pr_description: { markdown: 'Appended **desc**' },
  findings: [
    {
      id: 'L1',
      file_path: 'a.ts',
      line_start: 1,
      line_end: 1,
      draft_comment: 'Initial comment',
      _initial_comment: 'Initial comment',
      original_code: 'const a = 1;',
      approved: true
    }
  ]
};

// 1. Empty preview renders placeholder
const emptyHtml = renderCommentPreviewHtml('   ');
if (!emptyHtml.includes('Nothing to preview')) throw new Error('Expected empty placeholder: ' + emptyHtml);

// 2. Switch summary to preview -> strips file:// link and renders markdown
switchSummaryTab('preview');
if (elements['review-summary-text'].style.display !== 'none') throw new Error('Summary textarea should be hidden');
if (elements['review-summary-preview'].style.display !== 'block') throw new Error('Summary preview should be visible');
if (!elements['summary-tab-preview'].classList.contains('active')) throw new Error('Summary preview tab should be active');
if (elements['review-summary-preview'].innerHTML.includes('file://')) throw new Error('file:// link leaked in preview');
if (!elements['review-summary-preview'].innerHTML.includes('<code>fn</code>')) throw new Error('Expected inline code in preview');

// 3. Insert description while summary is in preview mode updates preview in place
insertDescriptionIntoSummary();
if (!elements['review-summary-preview'].innerHTML.includes('<strong>desc</strong>')) {
  throw new Error('Expected inserted description in summary preview');
}

// 4. Switch finding 0 to preview, insert suggestion, verify suggestion header in preview
switchCommentTab(0, 'preview');
if (reviewData.findings[0]._tab !== 'preview') throw new Error('Expected _tab=preview');
insertSuggestion(0);
if (!elements['comment-preview-0'].innerHTML.includes('suggestion-preview-header')) {
  throw new Error('Expected Suggested change header in comment preview');
}

// 5. Submit while in preview mode preserves edited text and strips _tab / _initial_comment
submitReview().then(() => {
  console.log(JSON.stringify(savedPayload));
});
"""
        )
        proc = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        saved = json.loads(proc.stdout.strip())
        self.assertEqual(saved["verdict"], "APPROVE")
        self.assertIn("Appended **desc**", saved["summary_comment"])
        self.assertIn(
            "```suggestion\nconst a = 1;\n```", saved["findings"][0]["draft_comment"]
        )
        self.assertNotIn("_tab", saved["findings"][0])
        self.assertNotIn("_initial_comment", saved["findings"][0])


def _md(*lines: str) -> str:
    """Joins Markdown source lines with newlines."""
    return "\n".join(lines)


@unittest.skipUnless(shutil.which("node"), "node is required for JS details tests")
class TestDashboardDetailsRendering(unittest.TestCase):
    """Exercises the <details> fold grammar and collapsed description sections."""

    js_code: ClassVar[str] = ""

    @classmethod
    def setUpClass(cls):
        html_path = (
            Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
        )
        html = html_path.read_text(encoding="utf-8")
        scripts = re.findall(r"<script>([\s\S]*?)</script>", html)
        if len(scripts) != 1:
            raise AssertionError(f"expected one inline <script>, found {len(scripts)}")
        cls.js_code = scripts[0]

    def _run_js(self, body: str, inputs) -> object:
        """Runs `body` after the dashboard script with `inputs` bound as INPUTS.

        `body` must print one JSON value to stdout, which is returned decoded.
        """
        harness = (
            "const elements = {};\n"
            "const window = { addEventListener: () => {} };\n"
            "const document = { getElementById: (id) => elements[id] || null };\n"
            + self.js_code
            + "\nconst INPUTS = "
            + json.dumps(inputs)
            + ";\n"
            + body
        )
        proc = subprocess.run(
            ["node", "-e", harness], capture_output=True, text=True, check=True
        )
        return json.loads(proc.stdout)

    def _render_all(self, payloads: list[str]) -> list[str]:
        return self._run_js(
            "console.log(JSON.stringify(INPUTS.map(p => renderMarkdownSafe(p))));",
            payloads,
        )

    def test_fold_renders_summary_and_fenced_code_inside(self):
        md = _md(
            "The bound is off by one.",
            "",
            "<details>",
            "<summary>Repro for **items**</summary>",
            "",
            "```python",
            "assert f([1]) == '<b>'",
            "```",
            "",
            "</details>",
        )
        (rendered,) = self._render_all([md])
        self.assertIn("<p>The bound is off by one.</p>", rendered)
        self.assertIn(
            '<details class="md-details"><summary>Repro for <strong>items</strong></summary>',
            rendered,
        )
        self.assertIn(
            '<pre class="code-box" data-lang="python"><code>assert f([1]) == '
            "&#039;&lt;b&gt;&#039;</code></pre></details>",
            rendered,
        )
        self.assertNotIn("&lt;details&gt;", rendered)
        self.assertNotIn("\u0000", rendered)

    def test_fold_without_summary_uses_default_label(self):
        (rendered,) = self._render_all(["<details>\n\nBody text\n\n</details>"])
        self.assertEqual(
            rendered,
            '<details class="md-details"><summary>Details</summary><p>Body text</p></details>',
        )

    def test_hostile_details_forms_stay_escaped_or_inert(self):
        payloads = [
            "<details open>\nopened\n</details>",
            "<DETAILS>\nupper\n</DETAILS>",
            "<details>\n<summary onclick=alert(1)>hi</summary>\nbody\n</details>",
            (
                "<details>\n<summary>a</summary><img src=x onerror=alert(1)></summary>\n"
                "<script>alert(1)</script>\n</details>"
            ),
            "Intro\n<details>\nnever closed",
            "<details>\n```\n</details>\n<script>alert(1)</script>\n```\n",
        ]
        rendered = self._render_all(payloads)
        joined = "\n".join(rendered)
        self.assertNotIn("<script", joined)
        self.assertNotIn("<img", joined)
        self.assertNotIn("<summary onclick", joined)
        self.assertNotIn("<details open", joined)

        # Attributes or uppercase tags never open a fold.
        self.assertEqual(
            rendered[0], "<p>&lt;details open&gt;<br>opened<br>&lt;/details&gt;</p>"
        )
        self.assertNotIn("<details", rendered[1])
        self.assertIn("&lt;DETAILS&gt;", rendered[1])
        # A summary line with attributes stays body text under the default label.
        self.assertIn("<summary>Details</summary>", rendered[2])
        self.assertIn("&lt;summary onclick=alert(1)&gt;hi&lt;/summary&gt;", rendered[2])
        # Summary text is escaped, never copied into a tag.
        self.assertIn(
            "<summary>a&lt;/summary&gt;&lt;img src=x onerror=alert(1)&gt;</summary>",
            rendered[3],
        )
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", rendered[3])
        # An unclosed fold renders as escaped text.
        self.assertEqual(
            rendered[4], "<p>Intro</p>\n<p>&lt;details&gt;<br>never closed</p>"
        )
        # A close tag inside a fence cannot close the fold, so it stays unclosed.
        self.assertNotIn("<details", rendered[5])
        self.assertIn("&lt;details&gt;", rendered[5])
        self.assertIn(
            "<code>&lt;/details&gt;\n&lt;script&gt;alert(1)&lt;/script&gt;</code>",
            rendered[5],
        )

    def test_nested_folds_pair_by_depth_and_depth_four_is_escaped(self):
        two_levels = _md(
            "<details>",
            "<summary>Outer</summary>",
            "<details>",
            "<summary>Inner</summary>",
            "inner body",
            "</details>",
            "outer tail",
            "</details>",
            "after",
        )
        four_levels = "\n".join(["<details>"] * 4 + ["deep"] + ["</details>"] * 4)
        rendered = self._render_all([two_levels, four_levels])
        self.assertEqual(
            rendered[0],
            '<details class="md-details"><summary>Outer</summary>'
            '<details class="md-details"><summary>Inner</summary><p>inner body</p></details>\n'
            "<p>outer tail</p></details>\n<p>after</p>",
        )
        self.assertEqual(rendered[1].count('<details class="md-details">'), 3)
        self.assertEqual(rendered[1].count("</details>"), 3)
        self.assertIn("<p>&lt;details&gt;<br>deep<br>&lt;/details&gt;</p>", rendered[1])

    def test_blockquote_nesting_counts_toward_depth_cap(self):
        md = _md(
            "<details>",
            "<details>",
            "<details>",
            "> <details>",
            "> quoted",
            "> </details>",
            "</details>",
            "</details>",
            "</details>",
        )
        (rendered,) = self._render_all([md])
        self.assertEqual(rendered.count('<details class="md-details">'), 3)
        self.assertIn("&lt;details&gt;", rendered)

    def test_description_sections_collapse_and_warning_renders_open(self):
        md = _md(
            "Preamble line.",
            "## Summary",
            "Adds a cache.",
            "## Intent Alignment: Matches Stated Purpose",
            "Aligned.",
            "## Changes",
            "- Adds `cache.py`",
            "```markdown",
            "## Not A Section",
            "```",
            "## Security & Execution Gate",
            "> [!WARNING]",
            "> Runs a postinstall script.",
            "## Stack Context <img src=x onerror=alert(1)>",
            "Base PR #1.",
            "## Prior Review Resolution",
            "All resolved.",
        )
        rendered = self._run_js(
            "console.log(JSON.stringify(renderDescriptionHtml(INPUTS)));", md
        )
        self.assertIn("<p>Preamble line.</p>", rendered)
        self.assertIn("<h2>Summary</h2>", rendered)
        self.assertIn("<h2>Intent Alignment: Matches Stated Purpose</h2>", rendered)
        self.assertIn(
            '<details class="md-details desc-section"><summary>Changes</summary>'
            "<ul><li>Adds <code>cache.py</code></li></ul>",
            rendered,
        )
        # A heading inside a fence stays in the code block and does not split.
        self.assertIn(
            '<pre class="code-box" data-lang="markdown"><code>## Not A Section</code></pre></details>',
            rendered,
        )
        self.assertNotIn("<summary>Not A Section", rendered)
        self.assertIn(
            '<details class="md-details desc-section" open>'
            "<summary>Security &amp; Execution Gate</summary>"
            '<div class="md-callout callout-warning">',
            rendered,
        )
        self.assertIn(
            '<details class="md-details desc-section">'
            "<summary>Stack Context &lt;img src=x onerror=alert(1)&gt;</summary>",
            rendered,
        )
        self.assertIn(
            '<details class="md-details desc-section">'
            "<summary>Prior Review Resolution</summary><p>All resolved.</p></details>",
            rendered,
        )
        self.assertNotIn("<img", rendered)
        self.assertEqual(rendered.count(" open>"), 1)

    def test_render_pr_description_uses_collapsed_sections(self):
        body = """
function el() {
  return { style: {}, innerHTML: '', textContent: '', className: '' };
}
['pr-description-card', 'pr-description-rendered', 'pr-description-raw',
 'pr-alignment-badge'].forEach(id => { elements[id] = el(); });
reviewData = { pr_description: { markdown: INPUTS, alignment: 'MATCHES' } };
renderPRDescription();
console.log(JSON.stringify({
  rendered: elements['pr-description-rendered'].innerHTML,
  raw: elements['pr-description-raw'].textContent
}));
"""
        md = "## Summary\nShort.\n\n## Changes\n- One"
        out = self._run_js(body, md)
        self.assertEqual(out["raw"], md)
        self.assertIn("<h2>Summary</h2>", out["rendered"])
        self.assertIn(
            '<details class="md-details desc-section"><summary>Changes</summary>',
            out["rendered"],
        )

    def test_insert_suggestion_places_fence_before_trailing_fold(self):
        body = """
function editor(value) {
  return { value, style: {}, classList: { toggle() {}, add() {}, remove() {} } };
}
INPUTS.forEach((comment, idx) => {
  elements[`comment-editor-${idx}`] = editor(comment);
});
reviewData = {
  findings: INPUTS.map(comment => ({ draft_comment: comment, original_code: 'x = 1' }))
};
INPUTS.forEach((_, idx) => insertSuggestion(idx));
console.log(JSON.stringify(reviewData.findings.map(f => f.draft_comment)));
"""
        fold = "<details>\n<summary>Details</summary>\n\n```\n</details>\n```\n\n</details>"
        comments = [
            f"The bound is off by one.\n\n{fold}\n",
            fold,
            "Plain comment.",
            "Ends with a fence.\n```\n</details>\n```",
        ]
        out = self._run_js(body, comments)
        fence = "```suggestion\nx = 1\n```"
        self.assertEqual(out[0], f"The bound is off by one.\n\n{fence}\n\n{fold}")
        self.assertEqual(out[1], f"{fence}\n\n{fold}")
        self.assertEqual(out[2], f"Plain comment.\n\n{fence}")
        self.assertEqual(out[3], f"{comments[3]}\n\n{fence}")


if __name__ == "__main__":
    unittest.main()
