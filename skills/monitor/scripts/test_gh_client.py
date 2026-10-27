"""Unit tests for gh_client.py."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from gh_client import (
    GhError,
    RateLimitBackoff,
    SubprocessGh,
    build_query,
    fetch_prs,
)
from testing import FakeGh, load_fixture


class TestGhClient(unittest.TestCase):
    def test_build_query_aliases_literal_numbers_and_mixed_repos(self) -> None:
        query = build_query(["acme/widget#3339", "other/repo#42"])
        self.assertIn('pr0: repository(owner: "acme", name: "widget")', query)
        self.assertIn("pullRequest(number: 3339)", query)
        self.assertIn("isRequired(pullRequestNumber: 3339)", query)
        self.assertIn('pr1: repository(owner: "other", name: "repo")', query)
        self.assertIn("pullRequest(number: 42)", query)
        self.assertIn("isRequired(pullRequestNumber: 42)", query)
        self.assertIn("latestOpinionatedReviews", query)
        with self.assertRaises(ValueError):
            build_query([])

    def test_fetch_prs_chunks_at_ten_and_merges(self) -> None:
        keys = [f"acme/widget#{100 + i}" for i in range(12)]
        chunk1 = {
            "data": {
                "viewer": {"login": "alice"},
                **{f"pr{i}": {"pullRequest": {"number": 100 + i}} for i in range(10)},
            }
        }
        chunk2 = {
            "data": {
                "viewer": {"login": "alice"},
                **{f"pr{i}": {"pullRequest": {"number": 110 + i}} for i in range(2)},
            }
        }
        fake = FakeGh(
            [
                ("graphql", 0, json.dumps(chunk1), ""),
                ("graphql", 0, json.dumps(chunk2), ""),
            ]
        )
        viewer, raw = fetch_prs(fake, keys)
        self.assertEqual(viewer, "alice")
        self.assertEqual(len(raw), 12)
        self.assertEqual(raw["acme/widget#111"]["number"], 111)
        self.assertEqual(len(fake.calls), 2)

    def test_fetch_prs_errors_on_missing_pr_or_graphql_error(self) -> None:
        fake = FakeGh([("graphql", 0, load_fixture("pr_open_ci_running.json"), "")])
        viewer, raw = fetch_prs(fake, ["acme/widget#3339"])
        self.assertEqual(viewer, "alice")
        self.assertEqual(raw["acme/widget#3339"]["number"], 3339)

        missing = FakeGh(
            [
                (
                    "graphql",
                    0,
                    json.dumps({"data": {"viewer": {"login": "a"}, "pr0": {"pullRequest": None}}}),
                    "",
                )
            ]
        )
        with self.assertRaises(GhError):
            fetch_prs(missing, ["acme/widget#9999"])

        gql_err = FakeGh(
            [("graphql", 0, json.dumps({"errors": [{"message": "rate limited"}]}), "")]
        )
        with self.assertRaises(GhError):
            fetch_prs(gql_err, ["acme/widget#3339"])

        exit_err = FakeGh([("graphql", 1, "", "HTTP 502")])
        with self.assertRaises(GhError):
            fetch_prs(exit_err, ["acme/widget#3339"])

    def test_subprocess_gh_wraps_timeout_and_missing_binary(self) -> None:
        runner = SubprocessGh(timeout_s=5)
        with patch(
            "gh_client.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=5)
        ):
            with self.assertRaises(GhError) as ctx:
                runner(["api", "user"])
            self.assertEqual(ctx.exception.code, 124)

        with patch("gh_client.subprocess.run", side_effect=FileNotFoundError("no gh")):
            with self.assertRaises(GhError) as ctx:
                runner(["api", "user"])
            self.assertEqual(ctx.exception.code, 127)

    def test_rate_limit_backoff_sequence_and_headers(self) -> None:
        backoff = RateLimitBackoff(min_delay_s=30, max_delay_s=300)
        self.assertEqual(backoff.next_delay(), 30)
        self.assertEqual(backoff.next_delay(), 60)
        self.assertEqual(backoff.next_delay(), 120)
        backoff.reset()
        self.assertEqual(backoff.failures, 0)
        self.assertEqual(backoff.next_delay(headers={"Retry-After": "90"}), 90)
        self.assertEqual(
            backoff.next_delay(
                status=429,
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"},
                now_epoch=1000.0,
            ),
            102,
        )


if __name__ == "__main__":
    unittest.main()
