#!/usr/bin/env python3
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

        self.server = http.server.HTTPServer(("127.0.0.1", self.port), ReviewDashboardHandler)
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
        self.assertFalse(data["findings"][0]["approved"])

    def test_get_api_findings_normalizes_aliases_and_meta_file(self):
        self.findings_file.write_text(
            json.dumps(
                {
                    "pr_meta": {"pull_number": 2907, "owner": "a2ui-project"},
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
            json.dumps({"repo": "a2ui", "title": "fix: thing", "pull_number": 1}),
            encoding="utf-8",
        )
        ReviewDashboardHandler.meta_file = meta_file

        status, body = self._get("/api/findings")
        self.assertEqual(status, 200)
        data = json.loads(body)
        meta = data["pr_metadata"]
        self.assertEqual(meta["pull_number"], 2907)  # findings file wins
        self.assertEqual(meta["repo"], "a2ui")  # filled from meta file
        self.assertEqual(meta["title"], "fix: thing")
        self.assertEqual(meta["head_sha"], "010d93e")
        self.assertEqual(
            data["review_summary"], {"overview": "Overall fine.", "verdict": "REQUEST_CHANGES"}
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
        conn.request("POST", "/api/save", json.dumps(decisions_payload), {"Content-Type": "application/json"})
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
        conn.request("POST", "/api/abort", headers={"X-Review-Token": "test-secret-token"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        res_data = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(res_data["status"], "aborted")

    def test_main_returns_exit_code_2_on_missing_files(self):
        rc = main([
            "--findings-file", str(self.tmp_path / "nonexistent.json"),
            "--output-file", str(self.output_file),
        ])
        self.assertEqual(rc, 2)


class TestNormalizeReviewData(unittest.TestCase):

    FULL_META = {
        "pull_number": 7,
        "owner": "o",
        "repo": "r",
        "head_sha": "abc123",
        "title": "T",
    }

    def test_forces_unapproved(self):
        data, _ = normalize_review_data(
            {"pr_metadata": self.FULL_META, "findings": [{"approved": True}, {}]}
        )
        self.assertEqual([f["approved"] for f in data["findings"]], [False, False])

    def test_prefixes_description(self):
        data, _ = normalize_review_data(
            {"findings": [{"description": "Promise not awaited.", "draft_comment": "Add await."}]}
        )
        self.assertEqual(
            data["findings"][0]["draft_comment"], "**Promise not awaited.**\n\nAdd await."
        )

    def test_no_duplicate_description(self):
        comment = "The promise is not awaited, so errors are lost. Add await."
        data, _ = normalize_review_data(
            {"findings": [{"description": "Promise is NOT awaited.", "draft_comment": comment}]}
        )
        self.assertEqual(data["findings"][0]["draft_comment"], comment)

    def test_empty_description_leaves_comment(self):
        data, _ = normalize_review_data({"findings": [{"draft_comment": "Add await."}]})
        self.assertEqual(data["findings"][0]["draft_comment"], "Add await.")

    def test_description_only(self):
        data, _ = normalize_review_data({"findings": [{"description": "Unused import"}]})
        self.assertEqual(data["findings"][0]["draft_comment"], "**Unused import**")

    def test_pr_metadata_wins_over_pr_meta(self):
        data, _ = normalize_review_data(
            {"pr_metadata": {"title": "canonical"}, "pr_meta": {"title": "alias", "repo": "r"}}
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
        _, warnings = normalize_review_data({"pr_metadata": {"title": "T"}, "findings": []})
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
        raw = {"findings": [{"approved": True, "description": "d", "draft_comment": "c"}]}
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
            parse_alignment_status("## Intent Alignment - Diverges from PR Description"),
            "DIVERGES",
        )
        self.assertEqual(
            parse_alignment_status("## Intent Alignment: Does Not Match Stated Purpose"),
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
            {"markdown": "## Intent Alignment: Matches\n\nBody text.", "alignment": "MATCHES"},
        )

        # description_markdown parameter overrides inline findings JSON
        data_override, _ = normalize_review_data(
            {"pr_description": {"markdown": "Old", "alignment": "MATCHES"}},
            description_markdown="## Intent Alignment: Unstated Scope\n\nNew body.",
        )
        self.assertEqual(
            data_override["pr_description"],
            {"markdown": "## Intent Alignment: Unstated Scope\n\nNew body.", "alignment": "UNSTATED"},
        )

    def test_normalizes_inline_findings_schema_in_launcher(self):
        data, _ = normalize_review_data({
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
        })
        self.assertEqual(data["review_summary"]["overview"], "Overall assessment.")
        self.assertEqual(len(data["findings"]), 1)
        f = data["findings"][0]
        self.assertEqual(f["file_path"], "src/app.py")
        self.assertEqual(f["line_start"], 10)
        self.assertEqual(f["line_end"], 12)
        self.assertIn("**Check bounds**", f["draft_comment"])

    @unittest.skipUnless(shutil.which("node"), "node is required for JS Markdown XSS tests")
    def test_dashboard_markdown_renderer_xss_safety(self):
        html_path = Path(__file__).resolve().parent.parent / "assets" / "review_dashboard.html"
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
  "```js\\"><img src=x>\\nconst x = 1 < 2;\\n```"
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


if __name__ == "__main__":
    unittest.main()


