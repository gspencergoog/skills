"""Unit tests for the budgeted `gh api -i` transport."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from gh_transport import (
    BudgetDeferred,
    GhApiError,
    GhResponse,
    GhTransport,
    RateLimited,
)
from github_sync import GhCommandResult, _classify_gh_failure
from rate_budget import Priority, RateBudget

_RESET = int(time.time()) + 3600


def _http(
    status_line: str,
    body: str = "",
    *,
    resource: str = "core",
    remaining: int = 4013,
    limit: int = 5000,
    extra: Sequence[str] = (),
) -> str:
    """Builds `gh api -i` style output with rate-limit headers."""
    headers = [
        status_line,
        "Content-Type: application/json; charset=utf-8",
        f"X-Ratelimit-Limit: {limit}",
        f"X-Ratelimit-Remaining: {remaining}",
        f"X-Ratelimit-Reset: {_RESET}",
        f"X-Ratelimit-Resource: {resource}",
        *extra,
    ]
    return "\r\n".join(headers) + "\r\n\r\n" + body


class FakeRunner:
    """Scripted GhRunner that records every invocation."""

    def __init__(self, *results: GhCommandResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], int]] = []

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        self.calls.append((list(args), timeout))
        return self.results.pop(0)


class FakeStore:
    """ApiCallStore that keeps rows in memory."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record_api_call(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


def _ok(stdout: str) -> GhCommandResult:
    return GhCommandResult(returncode=0, stdout=stdout, stderr="")


def _fail(stdout: str, stderr: str) -> GhCommandResult:
    return GhCommandResult(returncode=1, stdout=stdout, stderr=stderr)


class _TransportTestCase(unittest.TestCase):
    """Builds a transport over a temp RateBudget, fake store, and fake runner."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.budget = RateBudget(Path(self._tmp.name) / "rate_budget.json")
        self.store = FakeStore()

    def transport(self, *results: GhCommandResult) -> tuple[GhTransport, FakeRunner]:
        runner = FakeRunner(*results)
        return GhTransport(self.budget, self.store, runner, timeout_sec=7), runner

    def exhaust(self, resource: str, remaining: int = 100) -> None:
        """Records a budget below the MIRROR reserve (and the floor for 100/5000)."""
        self.budget.record(
            {
                "x-ratelimit-resource": resource,
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": str(remaining),
                "x-ratelimit-reset": str(_RESET),
            },
            "test",
        )


class TestRestGet(_TransportTestCase):
    """REST conditional GETs."""

    def test_200_returns_body_and_records_row(self) -> None:
        body = json.dumps([{"id": "1"}])
        transport, runner = self.transport(
            _ok(_http("HTTP/2.0 200 OK", body, extra=["Etag: W/1"]))
        )
        resp = transport.rest_get(
            "repos/o/r/events", worker="w", priority=Priority.MIRROR
        )
        self.assertEqual(resp, GhResponse(status=200, headers=resp.headers, body=body))
        self.assertEqual(resp.headers["etag"], "W/1")
        self.assertEqual(runner.calls, [(["api", "-i", "repos/o/r/events"], 7)])
        self.assertEqual(
            self.store.rows,
            [
                {
                    "worker": "w",
                    "resource": "core",
                    "cost": 1,
                    "remaining": 4013,
                    "reset_at": _RESET,
                    "status": 200,
                }
            ],
        )
        snap = self.budget.current("core")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.remaining, 4013)  # type: ignore[union-attr]

    def test_conditional_headers_are_sent(self) -> None:
        transport, runner = self.transport(_ok(_http("HTTP/2.0 200 OK", "[]")))
        transport.rest_get(
            "repos/o/r/events",
            worker="w",
            priority=Priority.HOT,
            etag='W/"abc"',
            if_modified_since="Tue, 03 Nov 2026 00:00:00 GMT",
        )
        args = runner.calls[0][0]
        self.assertIn('If-None-Match: W/"abc"', args)
        self.assertIn("If-Modified-Since: Tue, 03 Nov 2026 00:00:00 GMT", args)

    def test_304_returns_empty_body_without_parsing(self) -> None:
        raw = _http("HTTP/2.0 304 Not Modified", "not json {{{")
        transport, _ = self.transport(_fail(raw, "gh: HTTP 304"))
        resp = transport.rest_get(
            "repos/o/r/events", worker="w", priority=Priority.MIRROR, etag="x"
        )
        self.assertEqual(resp.status, 304)
        self.assertEqual(resp.body, "")
        self.assertEqual(self.store.rows[0]["cost"], 0)
        self.assertEqual(self.store.rows[0]["status"], 304)

    def test_304_detected_from_stderr_without_status_line(self) -> None:
        transport, _ = self.transport(_fail("", "HTTP 304: 304 Not Modified"))
        resp = transport.rest_get(
            "repos/o/r/events", worker="w", priority=Priority.MIRROR
        )
        self.assertEqual(resp.status, 304)
        self.assertEqual(self.store.rows[0]["status"], 304)

    def test_403_rate_limit_raises_rate_limited(self) -> None:
        body = json.dumps({"message": "API rate limit exceeded for user ID 1."})
        raw = _http("HTTP/2.0 403 Forbidden", body, remaining=0)
        transport, _ = self.transport(
            _fail(raw, "gh: API rate limit exceeded (HTTP 403)")
        )
        with self.assertRaises(RateLimited) as ctx:
            transport.rest_get("repos/o/r/events", worker="w", priority=Priority.VIEWER)
        self.assertIsInstance(ctx.exception, BudgetDeferred)
        self.assertEqual(ctx.exception.resource, "core")
        self.assertEqual(ctx.exception.priority, Priority.VIEWER)
        self.assertEqual(self.store.rows[0]["status"], 403)
        self.assertEqual(self.store.rows[0]["remaining"], 0)

    def test_404_raises_gh_api_error(self) -> None:
        raw = _http("HTTP/2.0 404 Not Found", json.dumps({"message": "Not Found"}))
        transport, _ = self.transport(_fail(raw, "gh: Not Found (HTTP 404)"))
        with self.assertRaises(GhApiError) as ctx:
            transport.rest_get("repos/o/missing", worker="w", priority=Priority.MIRROR)
        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("Not Found", ctx.exception.detail)
        self.assertEqual(len(self.store.rows), 1)

    def test_remaining_4013_header_is_not_an_auth_or_rate_limit_error(self) -> None:
        raw = _http("HTTP/2.0 500 Internal Server Error", "oops", remaining=4013)
        transport, _ = self.transport(_fail(raw, "gh: HTTP 500"))
        with self.assertRaises(GhApiError) as ctx:
            transport.rest_get("repos/o/r", worker="w", priority=Priority.MIRROR)
        self.assertNotIsInstance(ctx.exception, RateLimited)
        self.assertEqual(ctx.exception.status, 500)

    def test_timeout_raises_gh_api_error_with_status_zero(self) -> None:
        res = GhCommandResult(
            returncode=124, stdout="", stderr="gh timed out", timed_out=True
        )
        transport, _ = self.transport(res)
        with self.assertRaises(GhApiError) as ctx:
            transport.rest_get("repos/o/r", worker="w", priority=Priority.MIRROR)
        self.assertEqual(ctx.exception.status, 0)
        self.assertEqual(self.store.rows[0]["status"], 0)
        self.assertIsNone(self.store.rows[0]["remaining"])

    def test_budget_denial_raises_before_any_gh_call(self) -> None:
        self.exhaust("core")
        transport, runner = self.transport()
        with self.assertRaises(BudgetDeferred) as ctx:
            transport.rest_get("repos/o/r", worker="w", priority=Priority.MIRROR)
        self.assertNotIsInstance(ctx.exception, RateLimited)
        self.assertIn("MIRROR", ctx.exception.detail)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.store.rows, [])


class TestGraphql(_TransportTestCase):
    """GraphQL queries."""

    def test_success_returns_payload_with_cost_and_typed_fields(self) -> None:
        payload = {"data": {"viewer": {"login": "me"}, "rateLimit": {"cost": 3}}}
        raw = _http(
            "HTTP/2.0 200 OK", json.dumps(payload), resource="graphql", remaining=4800
        )
        transport, runner = self.transport(_ok(raw))
        out = transport.graphql(
            "query { viewer { login } }",
            {"owner": "o", "pageSize": 50, "draft": False, "cursor": None},
            worker="mirror",
            priority=Priority.MIRROR,
        )
        self.assertEqual(out, payload)
        args = runner.calls[0][0]
        self.assertEqual(args[:3], ["api", "-i", "graphql"])
        for flag, field in (
            ("-f", "owner=o"),
            ("-F", "pageSize=50"),
            ("-F", "draft=false"),
            ("-F", "cursor=null"),
            ("-f", "query=query { viewer { login } }"),
        ):
            idx = args.index(field)
            self.assertEqual(args[idx - 1], flag, field)
        self.assertEqual(
            self.store.rows,
            [
                {
                    "worker": "mirror",
                    "resource": "graphql",
                    "cost": 3,
                    "remaining": 4800,
                    "reset_at": _RESET,
                    "status": 200,
                }
            ],
        )

    def test_missing_rate_limit_field_records_null_cost(self) -> None:
        raw = _http(
            "HTTP/2.0 200 OK", json.dumps({"data": {"x": 1}}), resource="graphql"
        )
        transport, _ = self.transport(_ok(raw))
        transport.graphql("q", {}, worker="w", priority=Priority.HOT)
        self.assertIsNone(self.store.rows[0]["cost"])

    def test_rate_limited_body_raises_rate_limited(self) -> None:
        body = json.dumps(
            {
                "data": None,
                "errors": [
                    {"type": "RATE_LIMITED", "message": "API rate limit exceeded"}
                ],
            }
        )
        raw = _http("HTTP/2.0 200 OK", body, resource="graphql", remaining=0)
        transport, _ = self.transport(_fail(raw, "gh: API rate limit exceeded"))
        with self.assertRaises(RateLimited) as ctx:
            transport.graphql("q", {}, worker="w", priority=Priority.BACKFILL)
        self.assertEqual(ctx.exception.resource, "graphql")
        self.assertEqual(self.store.rows[0]["remaining"], 0)

    def test_partial_data_with_errors_is_returned(self) -> None:
        payload = {"data": {"repository": None}, "errors": [{"type": "NOT_FOUND"}]}
        raw = _http("HTTP/2.0 200 OK", json.dumps(payload), resource="graphql")
        transport, _ = self.transport(
            _fail(raw, "gh: Could not resolve to a Repository")
        )
        self.assertEqual(
            transport.graphql("q", {}, worker="w", priority=Priority.HOT), payload
        )

    def test_null_data_raises_gh_api_error(self) -> None:
        payload = {"data": None, "errors": [{"type": "NOT_FOUND", "message": "nope"}]}
        raw = _http("HTTP/2.0 200 OK", json.dumps(payload), resource="graphql")
        transport, _ = self.transport(_fail(raw, ""))
        with self.assertRaises(GhApiError) as ctx:
            transport.graphql("q", {}, worker="w", priority=Priority.HOT)
        self.assertNotIsInstance(ctx.exception, RateLimited)
        self.assertIn("NOT_FOUND", ctx.exception.detail)

    def test_non_json_body_raises_gh_api_error(self) -> None:
        raw = _http(
            "HTTP/2.0 502 Bad Gateway", "<html>bad gateway</html>", resource="graphql"
        )
        transport, _ = self.transport(_fail(raw, "gh: HTTP 502"))
        with self.assertRaises(GhApiError) as ctx:
            transport.graphql("q", {}, worker="w", priority=Priority.HOT)
        self.assertEqual(ctx.exception.status, 502)
        self.assertIsNone(self.store.rows[0]["cost"])

    def test_401_unrelated_to_remaining_header(self) -> None:
        raw = _http("HTTP/2.0 200 OK", json.dumps({"data": {"x": 1}}), remaining=4013)
        transport, _ = self.transport(_ok(raw))
        self.assertEqual(
            transport.graphql("q", {}, worker="w", priority=Priority.HOT),
            {"data": {"x": 1}},
        )

    def test_budget_denial_raises_before_any_gh_call(self) -> None:
        self.exhaust("graphql")
        transport, runner = self.transport()
        with self.assertRaises(BudgetDeferred):
            transport.graphql("q", {}, worker="w", priority=Priority.VIEWER)
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.store.rows, [])

    def test_hot_priority_runs_even_when_exhausted(self) -> None:
        self.exhaust("graphql", remaining=0)
        raw = _http(
            "HTTP/2.0 200 OK", json.dumps({"data": {"x": 1}}), resource="graphql"
        )
        transport, runner = self.transport(_ok(raw))
        transport.graphql("q", {}, worker="w", priority=Priority.HOT)
        self.assertEqual(len(runner.calls), 1)

    def test_unsupported_variable_type_raises_type_error(self) -> None:
        transport, runner = self.transport()
        with self.assertRaises(TypeError):
            transport.graphql("q", {"x": 1.5}, worker="w", priority=Priority.HOT)
        self.assertEqual(runner.calls, [])


class TestAsRunner(_TransportTestCase):
    """The GhRunner adapter for existing github_sync code."""

    def test_denied_graphql_returns_deferral_without_running_gh(self) -> None:
        self.exhaust("graphql")
        transport, runner = self.transport()
        res = transport.as_runner("sync", Priority.BACKFILL)(
            ["api", "graphql", "-f", "q=1"], 20
        )
        self.assertEqual(res.returncode, 1)
        self.assertEqual(res.stdout, "")
        self.assertEqual(
            res.stderr,
            "packrat: deferred_rate_limit (graphql budget below BACKFILL threshold)",
        )
        self.assertEqual(res.error_kind, "deferred_rate_limit")
        self.assertEqual(_classify_gh_failure(res)[0], "rate_limited")
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.store.rows, [])

    def test_denied_rest_uses_core_resource(self) -> None:
        self.exhaust("core")
        transport, runner = self.transport()
        res = transport.as_runner("sync", Priority.MIRROR)(
            ["api", "repos/o/r/events"], 20
        )
        self.assertIn("core budget below MIRROR threshold", res.stderr)
        self.assertEqual(runner.calls, [])

    def test_passthrough_inserts_include_flag_and_records(self) -> None:
        payload = {"data": {"rateLimit": {"cost": 2}}}
        raw = _http(
            "HTTP/2.0 200 OK", json.dumps(payload), resource="graphql", remaining=4013
        )
        result = _ok(raw)
        transport, runner = self.transport(result)
        res = transport.as_runner("sync", Priority.MIRROR)(
            ["api", "graphql", "-f", "q=1"], 20
        )
        self.assertIs(res, result)
        self.assertEqual(runner.calls, [(["api", "-i", "graphql", "-f", "q=1"], 20)])
        self.assertEqual(self.store.rows[0]["worker"], "sync")
        self.assertEqual(self.store.rows[0]["cost"], 2)
        self.assertEqual(self.store.rows[0]["remaining"], 4013)
        self.assertEqual(self.budget.current("graphql").remaining, 4013)  # type: ignore[union-attr]

    def test_existing_include_flag_is_not_duplicated(self) -> None:
        raw = _http("HTTP/2.0 304 Not Modified")
        transport, runner = self.transport(_fail(raw, "gh: HTTP 304"))
        transport.as_runner("events", Priority.MIRROR)(
            ["api", "-i", "repos/o/r/events"], 20
        )
        self.assertEqual(runner.calls[0][0], ["api", "-i", "repos/o/r/events"])
        self.assertEqual(self.store.rows[0]["cost"], 0)
        self.assertEqual(self.store.rows[0]["status"], 304)

    def test_include_flag_before_graphql_still_counts_as_graphql(self) -> None:
        self.exhaust("graphql")
        transport, runner = self.transport()
        res = transport.as_runner("sync", Priority.MIRROR)(["api", "-i", "graphql"], 20)
        self.assertIn("graphql budget", res.stderr)
        self.assertEqual(runner.calls, [])

    def test_non_api_commands_pass_through_unbudgeted(self) -> None:
        self.exhaust("core", remaining=0)
        result = _ok("token\n")
        transport, runner = self.transport(result)
        res = transport.as_runner("sync", Priority.BACKFILL)(["auth", "token"], 5)
        self.assertIs(res, result)
        self.assertEqual(runner.calls, [(["auth", "token"], 5)])
        self.assertEqual(self.store.rows, [])

    def test_as_runner_sets_uses_gh_cli_and_tags_rate_limit_errors(self) -> None:
        live_transport = GhTransport(self.budget, self.store)
        live_runner = live_transport.as_runner("sync", Priority.MIRROR)
        self.assertTrue(getattr(live_runner, "_uses_gh_cli", False))

        fake_transport, _ = self.transport(
            _fail(
                _http(
                    "HTTP/2.0 403 Forbidden", '{"message": "API rate limit exceeded"}'
                ),
                "gh: HTTP 403",
            )
        )
        fake_runner = fake_transport.as_runner("sync", Priority.MIRROR)
        self.assertFalse(getattr(fake_runner, "_uses_gh_cli", False))
        res = fake_runner(["api", "repos/o/r/events"], 20)
        self.assertEqual(res.error_kind, "rate_limited")


if __name__ == "__main__":
    unittest.main()
