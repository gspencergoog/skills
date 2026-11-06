"""Unit tests for gh_client.py."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from gh_client import (
    GhError,
    RateLimitBackoff,
    SubprocessGh,
    build_query,
    fetch_prs,
    graphql,
    search_review_keys,
    split_http_response,
)
from testing import FakeGh, load_fixture

RATE_HEADERS = {
    "X-RateLimit-Limit": "5000",
    "X-RateLimit-Remaining": "4990",
    "X-RateLimit-Used": "10",
    "X-RateLimit-Reset": "1793139300",
    "X-RateLimit-Resource": "graphql",
}


def http(body: str, status: str = "200 OK", headers: dict[str, str] | None = None) -> str:
    """Formats ``body`` as ``gh api -i`` output: status line, headers, blank line, body."""
    lines = [f"HTTP/2.0 {status}", "Content-Type: application/json; charset=utf-8"]
    lines.extend(f"{name}: {value}" for name, value in (headers or RATE_HEADERS).items())
    return "\r\n".join(lines) + "\r\n\r\n" + body


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
                ("graphql", 0, http(json.dumps(chunk1)), ""),
                ("graphql", 0, http(json.dumps(chunk2)), ""),
            ]
        )
        viewer, raw = fetch_prs(fake, keys)
        self.assertEqual(viewer, "alice")
        self.assertEqual(len(raw), 12)
        self.assertEqual(raw["acme/widget#111"]["number"], 111)
        self.assertEqual(len(fake.calls), 2)

    def test_fetch_prs_errors_on_missing_pr_or_graphql_error(self) -> None:
        fake = FakeGh([("graphql", 0, http(load_fixture("pr_open_ci_running.json")), "")])
        viewer, raw = fetch_prs(fake, ["acme/widget#3339"])
        self.assertEqual(viewer, "alice")
        self.assertEqual(raw["acme/widget#3339"]["number"], 3339)

        missing = FakeGh(
            [
                (
                    "graphql",
                    0,
                    http(
                        json.dumps(
                            {"data": {"viewer": {"login": "a"}, "pr0": {"pullRequest": None}}}
                        )
                    ),
                    "",
                )
            ]
        )
        with self.assertRaises(GhError):
            fetch_prs(missing, ["acme/widget#9999"])

        gql_err = FakeGh(
            [("graphql", 0, http(json.dumps({"errors": [{"message": "rate limited"}]})), "")]
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

    def test_build_query_selects_rate_limit_and_keeps_is_required(self) -> None:
        query = build_query(["acme/widget#3339"])
        self.assertIn("rateLimit {\n    cost\n    remaining\n    resetAt\n  }", query)
        self.assertIn("isRequired(pullRequestNumber: 3339)", query)

    def test_split_http_response_parses_headers_and_tolerates_bare_body(self) -> None:
        status, headers, body = split_http_response(http('{"data": {}}'))
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-ratelimit-remaining"], "4990")
        self.assertEqual(headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(body, '{"data": {}}')

        status, headers, body = split_http_response('{"data": {}}')
        self.assertIsNone(status)
        self.assertEqual(headers, {})
        self.assertEqual(body, '{"data": {}}')

    def test_graphql_passes_dash_i_and_returns_rate_limit_and_logs_cost(self) -> None:
        payload = {
            "data": {
                "viewer": {"login": "alice"},
                "rateLimit": {"cost": 3, "remaining": 4990, "resetAt": "2026-10-27T22:00:00Z"},
            }
        }
        fake = FakeGh([("graphql", 0, http(json.dumps(payload)), "")])
        logged: list[str] = []
        result = graphql(fake, "query { viewer { login } }", log=logged.append)
        args = fake.calls[0][0]
        self.assertEqual(args[:3], ["api", "-i", "graphql"])
        self.assertEqual(result.data["viewer"]["login"], "alice")
        self.assertEqual(result.headers["x-ratelimit-resource"], "graphql")
        rate = result.rate_limit
        self.assertEqual(
            (rate.resource, rate.limit, rate.remaining, rate.used, rate.reset_at, rate.cost),
            ("graphql", 5000, 4990, 10, 1793139300, 3),
        )
        self.assertEqual(len(logged), 1)
        self.assertIn("graphql cost=3 remaining=4990", logged[0])

    def test_fetch_prs_logs_cost_once_per_batch_to_stderr(self) -> None:
        keys = [f"acme/widget#{100 + i}" for i in range(12)]
        fake = FakeGh()
        for count in (10, 2):
            data = {
                "viewer": {"login": "alice"},
                "rateLimit": {"cost": 1, "remaining": 4990, "resetAt": "2026-10-27T22:00:00Z"},
                **{f"pr{i}": {"pullRequest": {"number": i}} for i in range(count)},
            }
            fake.add("graphql", 0, http(json.dumps({"data": data})))
        err = io.StringIO()
        with redirect_stderr(err):
            fetch_prs(fake, keys)
        self.assertEqual(err.getvalue().count("graphql cost=1"), 2)

    def test_graphql_failure_carries_status_and_headers_for_backoff(self) -> None:
        limited = {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1100",
            "X-RateLimit-Resource": "graphql",
        }
        body = json.dumps({"message": "API rate limit exceeded"})
        fake = FakeGh([("graphql", 1, http(body, "403 Forbidden", limited), "gh: HTTP 403")])
        with self.assertRaises(GhError) as ctx:
            graphql(fake, "query { viewer { login } }")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.headers["x-ratelimit-reset"], "1100")
        delay = RateLimitBackoff(min_delay_s=30, max_delay_s=300).next_delay(
            status=ctx.exception.status, headers=ctx.exception.headers, now_epoch=1000.0
        )
        self.assertEqual(delay, 102)

    def test_graphql_rate_limited_error_at_http_200_maps_to_403(self) -> None:
        # Real `gh api graphql` exits 1 when a 200 body carries `errors`.
        limited = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"}
        body = json.dumps({"errors": [{"type": "RATE_LIMITED", "message": "limit exceeded"}]})
        fake = FakeGh([("graphql", 1, http(body, headers=limited), "gh: limit exceeded")])
        with self.assertRaises(GhError) as ctx:
            graphql(fake, "query { viewer { login } }")
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(ctx.exception.headers["x-ratelimit-remaining"], "0")
        delay = RateLimitBackoff(min_delay_s=30, max_delay_s=300).next_delay(
            status=ctx.exception.status, headers=ctx.exception.headers, now_epoch=1000.0
        )
        self.assertEqual(delay, 102)

        exit_zero = FakeGh([("graphql", 0, http(body, headers=limited), "")])
        with self.assertRaises(GhError) as ctx:
            graphql(exit_zero, "query { viewer { login } }")
        self.assertEqual(ctx.exception.status, 403)

    def test_graphql_non_rate_limit_error_keeps_http_status(self) -> None:
        body = json.dumps({"errors": [{"type": "NOT_FOUND", "message": "no such repo"}]})
        fake = FakeGh([("graphql", 1, http(body), "gh: no such repo")])
        with self.assertRaises(GhError) as ctx:
            graphql(fake, "query { viewer { login } }")
        self.assertEqual(ctx.exception.status, 200)

    def test_graphql_without_rate_limit_node_does_not_log(self) -> None:
        fake = FakeGh([("graphql", 0, json.dumps({"data": {"viewer": {"login": "a"}}}), "")])
        logged: list[str] = []
        result = graphql(fake, "query { viewer { login } }", log=logged.append)
        self.assertEqual(logged, [])
        self.assertIsNone(result.rate_limit.cost)
        self.assertEqual(result.headers, {})

    def test_build_query_includes_reviews_fragment_with_commit_oid(self) -> None:
        query = build_query(["acme/widget#3339"])
        self.assertIn("reviews(last: 20)", query)
        self.assertIn("commit {\n        oid\n      }", query)

    def test_search_review_keys_deduplicates_and_handles_empty_and_repo_filter(self) -> None:
        payload_with_prs = {
            "data": {
                "viewer": {"login": "alice"},
                "rateLimit": {"cost": 1, "remaining": 4990, "resetAt": "2026-10-27T22:00:00Z"},
                "reviewRequested": {
                    "nodes": [
                        {"number": 10, "repository": {"nameWithOwner": "acme/widget"}},
                        {"number": 20, "repository": {"nameWithOwner": "other/repo"}},
                        {},
                    ]
                },
                "reviewed": {
                    "nodes": [
                        {"number": 20, "repository": {"nameWithOwner": "other/repo"}},
                        {"number": 30, "repository": {"nameWithOwner": "acme/widget"}},
                    ]
                },
            }
        }
        payload_empty = {
            "data": {
                "viewer": {"login": "alice"},
                "rateLimit": {"cost": 1, "remaining": 4989, "resetAt": "2026-10-27T22:00:00Z"},
                "reviewRequested": {"nodes": []},
                "reviewed": {"nodes": []},
            }
        }
        fake = FakeGh(
            [
                ("graphql", 0, http(json.dumps(payload_with_prs)), ""),
                ("graphql", 0, http(json.dumps(payload_empty)), ""),
            ]
        )
        backoff = RateLimitBackoff()
        backoff.next_delay()
        self.assertEqual(backoff.failures, 1)

        viewer, keys = search_review_keys(fake, backoff=backoff)
        self.assertEqual(viewer, "alice")
        self.assertEqual(keys, ["acme/widget#10", "other/repo#20", "acme/widget#30"])
        self.assertEqual(backoff.failures, 0)
        first_call = " ".join(fake.calls[0][0])
        self.assertIn("reqQuery=is:pr is:open review-requested:@me archived:false", first_call)
        self.assertIn(
            "revQuery=is:pr is:open reviewed-by:@me -author:@me archived:false", first_call
        )

        viewer_empty, keys_empty = search_review_keys(fake, repo="acme/widget")
        self.assertEqual(viewer_empty, "alice")
        self.assertEqual(keys_empty, [])
        second_call = " ".join(fake.calls[1][0])
        self.assertIn(
            "reqQuery=is:pr is:open review-requested:@me archived:false repo:acme/widget",
            second_call,
        )
        self.assertIn(
            "revQuery=is:pr is:open reviewed-by:@me -author:@me archived:false repo:acme/widget",
            second_call,
        )


if __name__ == "__main__":
    unittest.main()
