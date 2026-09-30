#!/usr/bin/env python3
"""
test_launch_dashboard.py - Unit tests for launch_dashboard.py.
"""

import http.client
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from launch_dashboard import (
    ReviewDashboardHandler,
    find_free_port,
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
                "pull_number": 42,
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
            "<html><body>/* __INITIAL_DATA__ */ null</body></html>",
            encoding="utf-8",
        )

        self.port = find_free_port()
        ReviewDashboardHandler.findings_file = self.findings_file
        ReviewDashboardHandler.output_file = self.output_file
        ReviewDashboardHandler.html_template_path = self.template_file

        self.server = http.server.HTTPServer(("127.0.0.1", self.port), ReviewDashboardHandler)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp_dir.cleanup()

    def test_find_free_port(self):
        port = find_free_port()
        self.assertIsInstance(port, int)
        self.assertGreater(port, 1024)

    def test_get_root_injects_findings(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        body = resp.read().decode("utf-8")
        conn.close()

        # Check injected data
        self.assertIn('"pull_number": 42', body)
        self.assertIn('"Unchecked null"', body)

    def test_get_api_findings(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", "/api/findings")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(data["pr_metadata"]["pull_number"], 42)
        self.assertEqual(len(data["findings"]), 1)

    def test_post_api_save(self):
        decisions_payload = {
            "pull_number": 42,
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

        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        headers = {"Content-Type": "application/json"}
        conn.request("POST", "/api/save", json.dumps(decisions_payload), headers)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        res_data = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(res_data["status"], "ok")
        self.assertTrue(self.output_file.is_file())

        saved = json.loads(self.output_file.read_text(encoding="utf-8"))
        self.assertEqual(saved["verdict"], "COMMENT")
        self.assertEqual(saved["findings"][0]["draft_comment"], "Please fix null check")

    def test_post_api_abort(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/api/abort")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        res_data = json.loads(resp.read().decode("utf-8"))
        conn.close()

        self.assertEqual(res_data["status"], "aborted")


if __name__ == "__main__":
    unittest.main()
