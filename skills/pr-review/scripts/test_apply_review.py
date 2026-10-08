#!/usr/bin/env python3
"""
test_apply_review.py - Unit tests for apply_review.py.
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from apply_review import (
    AGENT_ATTRIBUTION_BADGE,
    append_agent_attribution,
    build_review_payload,
    submit_review,
)


class TestApplyReview(unittest.TestCase):

    def test_append_agent_attribution_idempotent(self):
        self.assertEqual(append_agent_attribution(""), "")
        self.assertEqual(append_agent_attribution("   "), "")
        stamped = append_agent_attribution("Missing null check.")
        self.assertEqual(stamped, f"Missing null check.\n\n{AGENT_ATTRIBUTION_BADGE}")
        # Calling again does not duplicate the badge.
        self.assertEqual(append_agent_attribution(stamped), stamped)

    def test_build_review_payload_inline_and_broader_context(self):
        decisions = {
            "head_sha": "abc123456789",
            "verdict": "REQUEST_CHANGES",
            "summary_comment": "Please address the following comments.",
            "findings": [
                {
                    "id": "1",
                    "file_path": "lib/foo.dart",
                    "line_start": 15,
                    "line_end": 15,
                    "side": "RIGHT",
                    "is_diff_hunk": True,
                    "approved": True,
                    "draft_comment": "Missing null check.",
                },
                {
                    "id": "2",
                    "file_path": "lib/bar.dart",
                    "line_start": 30,
                    "line_end": 35,
                    "side": "RIGHT",
                    "is_diff_hunk": True,
                    "approved": True,
                    "draft_comment": "```suggestion\nnew code\n```",
                },
                {
                    "id": "3",
                    "file_path": "lib/baz.dart",
                    "line_start": 5,
                    "line_end": 5,
                    "side": "RIGHT",
                    "is_diff_hunk": False,  # Outside diff hunk
                    "approved": True,
                    "description": "Unused private helper",
                    "draft_comment": "Consider deleting this helper as it has no callers.",
                },
                {
                    "id": "4",
                    "file_path": "lib/ignored.dart",
                    "line_start": 1,
                    "line_end": 1,
                    "side": "RIGHT",
                    "is_diff_hunk": True,
                    "approved": False,  # Not approved
                    "draft_comment": "Ignored comment",
                },
            ],
        }

        payload = build_review_payload(decisions)

        self.assertEqual(payload["commit_id"], "abc123456789")
        self.assertEqual(payload["event"], "REQUEST_CHANGES")

        # Check inline comments (should have findings 1 and 2, but NOT 3 or 4)
        comments = payload["comments"]
        self.assertEqual(len(comments), 2)

        # Finding 1
        self.assertEqual(comments[0]["path"], "lib/foo.dart")
        self.assertEqual(comments[0]["line"], 15)
        self.assertNotIn("start_line", comments[0])
        self.assertEqual(
            comments[0]["body"],
            f"Missing null check.\n\n{AGENT_ATTRIBUTION_BADGE}",
        )

        # Finding 2 (multiline)
        self.assertEqual(comments[1]["path"], "lib/bar.dart")
        self.assertEqual(comments[1]["start_line"], 30)
        self.assertEqual(comments[1]["line"], 35)
        self.assertTrue(comments[1]["body"].endswith(AGENT_ATTRIBUTION_BADGE))

        # Check broader context in top-level body
        body = payload["body"]
        self.assertIn("Please address the following comments.", body)
        self.assertIn("Broader Context & Non-Diff Observations", body)
        self.assertIn("`lib/baz.dart:5`", body)
        self.assertIn("Consider deleting this helper as it has no callers.", body)
        self.assertNotIn("Ignored comment", body)
        self.assertTrue(body.endswith(AGENT_ATTRIBUTION_BADGE))

    def test_build_review_payload_default_verdict(self):
        decisions = {
            "findings": [
                {
                    "file_path": "test.txt",
                    "line_start": 1,
                    "line_end": 1,
                    "is_diff_hunk": True,
                    "approved": True,
                    "draft_comment": "A comment",
                }
            ]
        }
        payload = build_review_payload(decisions)
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(len(payload["comments"]), 1)
        self.assertEqual(payload["body"], "")

    def test_missing_approved_is_not_posted(self):
        decisions = {
            "findings": [
                {
                    "file_path": "a.py",
                    "line_start": 1,
                    "line_end": 1,
                    "is_diff_hunk": True,
                    "draft_comment": "No approval field",
                },
                {
                    "file_path": "b.py",
                    "line_start": 2,
                    "line_end": 2,
                    "is_diff_hunk": False,
                    "draft_comment": "No approval field either",
                },
            ]
        }
        payload = build_review_payload(decisions)
        self.assertEqual(payload["comments"], [])
        self.assertNotIn("No approval field", payload["body"])
        self.assertEqual(
            payload["body"],
            f"Reviewed and verified.\n\n{AGENT_ATTRIBUTION_BADGE}",
        )

    def test_broader_context_no_duplicate_description(self):
        decisions = {
            "findings": [
                {
                    "file_path": "lib/baz.dart",
                    "line_start": 5,
                    "line_end": 5,
                    "is_diff_hunk": False,
                    "approved": True,
                    "description": "Unused private helper.",
                    "draft_comment": "**Unused private helper.**\n\nDelete it; it has no callers.",
                },
                {
                    "file_path": "lib/qux.dart",
                    "line_start": 9,
                    "line_end": 9,
                    "is_diff_hunk": False,
                    "approved": True,
                    "description": "Stale TODO",
                    "draft_comment": "Remove the comment.",
                },
            ]
        }
        body = build_review_payload(decisions)["body"]
        self.assertIn("- **`lib/baz.dart:5`**\n", body)
        self.assertNotIn("— Unused private helper", body)
        self.assertIn("- **`lib/qux.dart:9`** — Stale TODO", body)

    def test_submit_review_dry_run(self):
        payload = {"body": "Test", "event": "COMMENT", "comments": []}
        code, resp, err = submit_review("octocat", "hello-world", 100, payload, dry_run=True)
        self.assertEqual(code, 0)
        self.assertTrue(resp.get("dry_run"))
        self.assertEqual(err, "")

    @patch("subprocess.run")
    def test_submit_review_success(self, mock_run):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps({"id": 12345, "html_url": "https://github.com/o/r/pull/1#review-12345"})
        mock_proc.stderr = ""
        mock_run.return_value = mock_proc

        payload = {"body": "LGTM", "event": "APPROVE", "comments": []}
        code, resp, err = submit_review("o", "r", 1, payload, dry_run=False)

        self.assertEqual(code, 0)
        self.assertEqual(resp["id"], 12345)
        self.assertIn("review-12345", resp["html_url"])
        mock_run.assert_called_once()

    def test_pr_description_never_posted_to_github(self):
        decisions = {
            "head_sha": "abc123",
            "verdict": "COMMENT",
            "summary_comment": "Top-level summary.",
            "pr_description": {
                "markdown": "## Intent Alignment: Diverges\nSecret reviewer notes.",
                "alignment": "DIVERGES",
            },
            "findings": [],
        }
        payload = build_review_payload(decisions)
        self.assertNotIn("Secret reviewer notes", payload["body"])
        self.assertNotIn("pr_description", payload)

    def test_request_changes_empty_summary_gets_fallback_body(self):
        decisions = {
            "verdict": "REQUEST_CHANGES",
            "summary_comment": "",
            "findings": [
                {
                    "file_path": "a.py",
                    "line_start": 10,
                    "line_end": 10,
                    "is_diff_hunk": True,
                    "approved": True,
                    "draft_comment": "Please fix this bug.",
                }
            ],
        }
        payload = build_review_payload(decisions)
        self.assertEqual(payload["event"], "REQUEST_CHANGES")
        self.assertTrue(payload["body"].strip())
        self.assertIn("Please see the inline comments", payload["body"])
        self.assertTrue(payload["body"].endswith(AGENT_ATTRIBUTION_BADGE))

    def test_inverted_line_range_normalized_and_dotfile_preserved(self):
        decisions = {
            "verdict": "COMMENT",
            "findings": [
                {
                    "file_path": "./.github/workflows/ci.yml",
                    "line_start": 25,
                    "line_end": 20,
                    "is_diff_hunk": True,
                    "approved": True,
                    "draft_comment": "Fix range and preserve leading dot.",
                }
            ],
        }
        payload = build_review_payload(decisions)
        comment = payload["comments"][0]
        self.assertEqual(comment["path"], ".github/workflows/ci.yml")
        self.assertEqual(comment["start_line"], 20)
        self.assertEqual(comment["line"], 25)


if __name__ == "__main__":
    unittest.main()


