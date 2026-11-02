"""Unit tests for packrat conditional /events ETag polling and GraphQL delta sync."""

from __future__ import annotations

import json
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from db_store import PackratStore
from github_sync import (
    GhCommandResult,
    extract_issue_links,
    needs_safety_sweep,
    poll_repo_events,
    sync_github_metadata,
)
from models import BACKFILL_STREAMS, RepoConfig


def _mark_backfill_complete(store: PackratStore, repo_id: int) -> None:
    """Marks every stream's history as fetched so syncs exercise incremental paging only."""
    for stream in BACKFILL_STREAMS:
        store.set_backfill_state(repo_id, stream, cursor=None, done_at="2026-10-01T00:00:00Z")


class TestGithubSync(unittest.TestCase):
    """Tests HTTP 304 short-circuit, safety sweep, GraphQL delta paging, and error handling."""

    def test_http_304_not_modified_short_circuits_graphql(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                store.register_repo("octo-org", "widgets")
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                now_iso = "2026-10-30T23:00:00Z"
                store.update_repo_sync_state(
                    repo.repo_id,
                    events_etag='W/"etag-1"',
                    events_last_modified="Fri, 30 Oct 2026 22:55:00 GMT",
                    last_full_sweep_at="2026-10-30T22:55:00Z",
                )
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                self.assertFalse(needs_safety_sweep(repo, now_iso=now_iso))

                calls: list[Sequence[str]] = []

                def fake_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
                    calls.append(args)
                    return GhCommandResult(
                        returncode=0,
                        stdout=(
                            'HTTP/2.0 304 Not Modified\r\n'
                            'ETag: W/"etag-1"\r\n'
                            'X-Poll-Interval: 60\r\n\r\n'
                        ),
                        stderr="",
                    )

                poll = poll_repo_events(repo, runner=fake_runner)
                self.assertTrue(poll.not_modified)
                self.assertEqual(poll.status_code, 304)
                self.assertEqual(len(calls), 1)
                self.assertIn('If-None-Match: W/"etag-1"', calls[0])
            finally:
                store.close()

    def test_graphql_delta_sync_page_halving_and_issue_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                store.register_repo("octo-org", "widgets")
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                store.update_repo_sync_state(
                    repo.repo_id,
                    issues_watermark="2026-10-20T00:00:00Z",
                    prs_watermark="2026-10-20T00:00:00Z",
                    discussions_watermark="2026-10-20T00:00:00Z",
                )
                _mark_backfill_complete(store, repo.repo_id)
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None

                pr_attempts = 0

                def fake_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
                    nonlocal pr_attempts
                    cmd = " ".join(args)
                    if "repos/octo-org/widgets/events" in cmd:
                        return GhCommandResult(
                            returncode=0,
                            stdout=(
                                'HTTP/2.0 200 OK\r\n'
                                'ETag: W/"etag-2"\r\n'
                                'Last-Modified: Fri, 30 Oct 2026 23:00:00 GMT\r\n'
                                'X-Poll-Interval: 45\r\n\r\n'
                                '[{"id":"1","type":"PushEvent"}]'
                            ),
                            stderr="",
                        )
                    if "issues(first:" in cmd:
                        payload = {
                            "data": {
                                "repository": {
                                    "issues": {
                                        "pageInfo": {"hasNextPage": True, "endCursor": "cur-1"},
                                        "nodes": [
                                            {
                                                "id": "I_101",
                                                "number": 101,
                                                "title": "Focus ring missing on button",
                                                "state": "OPEN",
                                                "updatedAt": "2026-10-28T12:00:00Z",
                                                "createdAt": "2026-10-27T12:00:00Z",
                                                "body": "Keyboard navigation does not show focus ring.",
                                                "author": {"login": "alice"},
                                                "authorAssociation": "CONTRIBUTOR",
                                                "labels": {"nodes": [{"name": "a11y"}]},
                                                "assignees": {"nodes": [{"login": "bob"}]},
                                                "comments": {
                                                    "nodes": [
                                                        {
                                                            "id": "IC_1",
                                                            "body": "Confirmed on Chrome.",
                                                            "createdAt": "2026-10-28T11:00:00Z",
                                                            "updatedAt": "2026-10-28T11:00:00Z",
                                                            "author": {"login": "bob"},
                                                        }
                                                    ]
                                                },
                                            },
                                            {
                                                "id": "I_99",
                                                "number": 99,
                                                "title": "Old issue before watermark",
                                                "state": "CLOSED",
                                                "updatedAt": "2026-10-15T00:00:00Z",
                                                "body": "Should stop at watermark",
                                            },
                                        ],
                                    }
                                }
                            }
                        }
                        return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

                    if "pullRequests(first:" in cmd:
                        pr_attempts += 1
                        if "pageSize=25" in cmd:
                            return GhCommandResult(
                                returncode=1,
                                stdout="",
                                stderr="HTTP 502 Bad Gateway",
                            )
                        payload = {
                            "data": {
                                "repository": {
                                    "pullRequests": {
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "nodes": [
                                            {
                                                "id": "PR_202",
                                                "number": 202,
                                                "title": "Add focus ring to SurfaceButton",
                                                "state": "MERGED",
                                                "isDraft": False,
                                                "updatedAt": "2026-10-29T15:00:00Z",
                                                "createdAt": "2026-10-28T14:00:00Z",
                                                "mergedAt": "2026-10-29T15:00:00Z",
                                                "body": "Fixes #101 and references #88.",
                                                "baseRefName": "main",
                                                "headRefName": "fix-focus",
                                                "headRefOid": "abc1234",
                                                "mergeCommit": {"oid": "def5678"},
                                                "reviewDecision": "APPROVED",
                                                "mergeable": "MERGEABLE",
                                                "additions": 20,
                                                "deletions": 4,
                                                "changedFiles": 1,
                                                "author": {"login": "bob"},
                                                "authorAssociation": "MEMBER",
                                                "labels": {"nodes": [{"name": "ui"}]},
                                                "closingIssuesReferences": {"nodes": [{"number": 101}]},
                                                "files": {
                                                    "nodes": [
                                                        {
                                                            "path": "src/button.ts",
                                                            "changeType": "MODIFIED",
                                                            "additions": 20,
                                                            "deletions": 4,
                                                        }
                                                    ]
                                                },
                                                "reviews": {
                                                    "nodes": [
                                                        {
                                                            "id": "REV_1",
                                                            "state": "APPROVED",
                                                            "body": "LGTM",
                                                            "submittedAt": "2026-10-29T14:00:00Z",
                                                            "author": {"login": "carol"},
                                                        }
                                                    ]
                                                },
                                                "reviewThreads": {
                                                    "nodes": [
                                                        {
                                                            "id": "RT_1",
                                                            "path": "src/button.ts",
                                                            "line": 42,
                                                            "startLine": 40,
                                                            "isResolved": True,
                                                            "isOutdated": False,
                                                            "comments": {
                                                                "nodes": [
                                                                    {
                                                                        "id": "RTC_1",
                                                                        "body": "Use --focus-ring token here.",
                                                                        "createdAt": "2026-10-29T13:00:00Z",
                                                                        "updatedAt": "2026-10-29T13:00:00Z",
                                                                        "author": {"login": "carol"},
                                                                    }
                                                                ]
                                                            },
                                                        }
                                                    ]
                                                },
                                                "comments": {"nodes": []},
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                        return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

                    if "discussions(first:" in cmd:
                        payload = {
                            "data": {
                                "repository": {
                                    "discussions": {
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "nodes": [
                                            {
                                                "id": "D_301",
                                                "number": 301,
                                                "title": "Custom catalog schema proposal",
                                                "updatedAt": "2026-10-29T18:00:00Z",
                                                "body": "How should custom catalog components register?",
                                                "author": {"login": "dave"},
                                                "category": {"name": "Ideas"},
                                                "answer": {"body": "Use registerCatalogComponent."},
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                        return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")

                    return GhCommandResult(returncode=1, stdout="", stderr="unexpected")

                stats = sync_github_metadata(store, repo, runner=fake_runner)
                self.assertEqual(stats.issues_upserted, 1)
                self.assertEqual(stats.prs_upserted, 1)
                self.assertEqual(stats.discussions_upserted, 1)
                self.assertGreaterEqual(pr_attempts, 2)  # Verified pageSize=25 -> pageSize=12 halving

                updated_repo = store.get_repo("octo-org/widgets")
                assert updated_repo is not None
                self.assertEqual(updated_repo.events_etag, 'W/"etag-2"')
                self.assertEqual(updated_repo.issues_watermark, "2026-10-28T12:00:00Z")
                self.assertEqual(updated_repo.prs_watermark, "2026-10-29T15:00:00Z")
                self.assertEqual(updated_repo.discussions_watermark, "2026-10-29T18:00:00Z")

                counts = store.table_counts(repo.repo_id)
                self.assertEqual(counts["issues"], 1)
                self.assertEqual(counts["pull_requests"], 1)
                self.assertEqual(counts["review_threads"], 1)
                self.assertEqual(counts["discussions"], 1)

                links = extract_issue_links(repo.repo_id, 202, "Fixes #101 and references #88.", [101])
                self.assertEqual(len(links), 2)
            finally:
                store.close()

    def test_locked_keyring_or_offline_preserves_watermarks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                store.register_repo("octo-org", "widgets")
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                store.update_repo_sync_state(
                    repo.repo_id,
                    events_etag='W/"keep-me"',
                    issues_watermark="2026-10-25T00:00:00Z",
                )
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None

                def timeout_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
                    return GhCommandResult(
                        returncode=124,
                        stdout="",
                        stderr="gh command timed out (possible locked keyring or network stall)",
                        timed_out=True,
                    )

                stats = sync_github_metadata(store, repo, runner=timeout_runner)
                self.assertIsNotNone(stats.skipped_reason)
                after = store.get_repo("octo-org/widgets")
                assert after is not None
                self.assertEqual(after.events_etag, 'W/"keep-me"')
                self.assertEqual(after.issues_watermark, "2026-10-25T00:00:00Z")
            finally:
                store.close()

    def test_max_pages_truncation_preserves_watermark_and_force_clears_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                store.register_repo("octo-org", "widgets")
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None
                store.update_repo_sync_state(
                    repo.repo_id,
                    issues_watermark="2026-10-20T00:00:00Z",
                )
                _mark_backfill_complete(store, repo.repo_id)
                repo = store.get_repo("octo-org/widgets")
                assert repo is not None

                def truncated_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
                    cmd = " ".join(args)
                    if "repos/octo-org/widgets/events" in cmd:
                        return GhCommandResult(
                            returncode=0,
                            stdout='HTTP/2.0 200 OK\r\nETag: "etag-trunc"\r\n\r\n[{"id":"1"}]',
                            stderr="",
                        )
                    if "issues(first:" in cmd:
                        payload = {
                            "data": {
                                "repository": {
                                    "issues": {
                                        "pageInfo": {"hasNextPage": True, "endCursor": "CUR_2"},
                                        "nodes": [
                                            {
                                                "id": "I_500",
                                                "number": 500,
                                                "title": "Newest issue on page 1",
                                                "state": "OPEN",
                                                "updatedAt": "2026-10-30T00:00:00Z",
                                                "createdAt": "2026-10-30T00:00:00Z",
                                                "body": "Body",
                                                "author": {"login": "alice"},
                                                "labels": {"nodes": []},
                                                "assignees": {"nodes": []},
                                                "comments": {"nodes": []},
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                        return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")
                    return GhCommandResult(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        "pullRequests": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                                        "discussions": {"pageInfo": {"hasNextPage": False}, "nodes": []},
                                    }
                                }
                            }
                        ),
                        stderr="",
                    )

                # max_pages=1 while hasNextPage=True must NOT advance issues_watermark (H3)
                sync_github_metadata(store, repo, max_pages=1, runner=truncated_runner)
                after_trunc = store.get_repo("octo-org/widgets")
                assert after_trunc is not None
                self.assertEqual(after_trunc.issues_watermark, "2026-10-20T00:00:00Z")

                # force=True must re-index items even when their updatedAt <= stored watermark (H4)
                store.update_repo_sync_state(
                    repo.repo_id,
                    issues_watermark="2026-10-31T00:00:00Z",
                )
                repo_high_wm = store.get_repo("octo-org/widgets")
                assert repo_high_wm is not None

                def complete_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
                    cmd = " ".join(args)
                    if "repos/octo-org/widgets/events" in cmd:
                        return GhCommandResult(
                            returncode=0,
                            stdout='HTTP/2.0 304 Not Modified\r\nETag: "etag-trunc"\r\n\r\n',
                            stderr="",
                        )
                    if "issues(first:" in cmd:
                        payload = {
                            "data": {
                                "repository": {
                                    "issues": {
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "nodes": [
                                            {
                                                "id": "I_400",
                                                "number": 400,
                                                "title": "Older issue below previous watermark",
                                                "state": "OPEN",
                                                "updatedAt": "2026-10-25T00:00:00Z",
                                                "createdAt": "2026-10-25T00:00:00Z",
                                                "body": "Body",
                                                "author": {"login": "alice"},
                                                "labels": {"nodes": []},
                                                "assignees": {"nodes": []},
                                                "comments": {"nodes": []},
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                        return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")
                    return truncated_runner(args, timeout)

                stats_force = sync_github_metadata(
                    store, repo_high_wm, force=True, max_pages=5, runner=complete_runner
                )
                self.assertEqual(stats_force.issues_upserted, 1)
            finally:
                store.close()


def _issue_node(number: int, updated_at: str) -> dict[str, object]:
    """Builds a minimal GraphQL issue node."""
    return {
        "id": f"I_{number}",
        "number": number,
        "title": f"Issue {number}",
        "state": "OPEN",
        "updatedAt": updated_at,
        "createdAt": updated_at,
        "body": "Body",
        "author": {"login": "alice"},
        "labels": {"nodes": []},
        "assignees": {"nodes": []},
        "comments": {"nodes": []},
    }


class PagedIssuesRunner:
    """Fake gh runner that serves issue pages keyed by request cursor.

    `pages` maps a request cursor (None for the first page) to
    (issue numbers, hasNextPage, endCursor). Issue N gets updatedAt day N of
    October 2026, so higher numbers are newer. Cursors listed in `failing`
    answer with HTTP 502 at every page size. PR and discussion connections are
    always empty.
    """

    def __init__(
        self,
        pages: dict[str | None, tuple[list[int], bool, str | None]],
        *,
        failing: frozenset[str] = frozenset(),
        events_status: int = 200,
    ) -> None:
        self.pages = pages
        self.failing = failing
        self.events_status = events_status
        self.issue_cursors: list[str | None] = []

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        cmd = " ".join(args)
        if "/events" in cmd:
            status = "304 Not Modified" if self.events_status == 304 else "200 OK"
            body = "" if self.events_status == 304 else "[]"
            return GhCommandResult(returncode=0, stdout=f'HTTP/2.0 {status}\r\nETag: "e1"\r\n\r\n{body}', stderr="")
        if "issues(first:" not in cmd:
            empty = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
            payload = {"data": {"repository": {"pullRequests": empty, "discussions": empty}}}
            return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")
        cursor = next((a.split("=", 1)[1] for a in args if a.startswith("cursor=")), None)
        self.issue_cursors.append(cursor)
        if cursor in self.failing:
            return GhCommandResult(returncode=1, stdout="", stderr="HTTP 502 Bad Gateway")
        numbers, has_next, end_cursor = self.pages[cursor]
        nodes = [_issue_node(n, f"2026-10-{n:02d}T00:00:00Z") for n in numbers]
        conn = {"pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor}, "nodes": nodes}
        return GhCommandResult(returncode=0, stdout=json.dumps({"data": {"repository": {"issues": conn}}}), stderr="")


THREE_ISSUE_PAGES: dict[str | None, tuple[list[int], bool, str | None]] = {
    None: ([30, 29], True, "C1"),
    "C1": ([20, 19], True, "C2"),
    "C2": ([10, 9], False, "C3"),
}


class TestGithubBackfill(unittest.TestCase):
    """Tests full-history backfill paging, resume cursors, and safety stops."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PackratStore(Path(self.tmp.name) / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _reload(self) -> RepoConfig:
        repo = self.store.get_repo_by_id(self.repo.repo_id)
        assert repo is not None
        return repo

    def test_first_sync_pages_until_has_next_page_false(self) -> None:
        runner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        stats = sync_github_metadata(self.store, self.repo, max_pages=1, runner=runner)

        self.assertEqual(stats.issues_upserted, 6)
        self.assertEqual(stats.backfill_incomplete, [])
        repo = self._reload()
        self.assertTrue(repo.backfill("issues").complete)
        self.assertIsNone(repo.backfill("issues").cursor)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")
        self.assertTrue(all(repo.backfill(s).complete for s in BACKFILL_STREAMS))

    def test_backfill_cap_saves_cursor_and_next_sync_resumes(self) -> None:
        runner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        stats = sync_github_metadata(self.store, self.repo, backfill_max_pages=2, runner=runner)

        self.assertEqual(stats.issues_upserted, 4)
        self.assertEqual(stats.backfill_incomplete, ["issues"])
        repo = self._reload()
        self.assertEqual(repo.backfill("issues").cursor, "C2")
        self.assertFalse(repo.backfill("issues").complete)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")

        # A quiet repository (HTTP 304) must still resume the pending backfill.
        resume_runner = PagedIssuesRunner(THREE_ISSUE_PAGES, events_status=304)
        stats2 = sync_github_metadata(self.store, repo, backfill_max_pages=2, runner=resume_runner)

        self.assertFalse(stats2.events_not_modified)
        self.assertEqual(resume_runner.issue_cursors, [None, "C2"])  # incremental pass, then resume
        self.assertEqual(stats2.backfill_incomplete, [])
        self.assertEqual(self.store.table_counts(repo.repo_id)["issues"], 6)
        repo = self._reload()
        self.assertTrue(repo.backfill("issues").complete)
        self.assertIsNone(repo.backfill("issues").cursor)

        # Once every stream is complete, HTTP 304 skips GraphQL again.
        quiet_runner = PagedIssuesRunner(THREE_ISSUE_PAGES, events_status=304)
        stats3 = sync_github_metadata(self.store, repo, runner=quiet_runner)
        self.assertTrue(stats3.events_not_modified)
        self.assertEqual(quiet_runner.issue_cursors, [])

    def test_backfill_error_keeps_saved_pages_and_cursor(self) -> None:
        runner = PagedIssuesRunner(THREE_ISSUE_PAGES, failing=frozenset({"C1"}))
        stats = sync_github_metadata(self.store, self.repo, runner=runner)

        self.assertEqual(stats.issues_upserted, 2)
        self.assertEqual(stats.backfill_incomplete, ["issues"])
        repo = self._reload()
        self.assertEqual(repo.backfill("issues").cursor, "C1")
        self.assertIsNone(repo.backfill("issues").done_at)
        self.assertEqual(self.store.table_counts(repo.repo_id)["issues"], 2)

    def test_repeated_end_cursor_stops_walk(self) -> None:
        runner = PagedIssuesRunner({None: ([30], True, "C1"), "C1": ([29], True, "C1")})
        stats = sync_github_metadata(self.store, self.repo, runner=runner)

        self.assertEqual(runner.issue_cursors, [None, "C1"])
        self.assertEqual(stats.backfill_incomplete, ["issues"])
        self.assertEqual(self._reload().backfill("issues").cursor, "C1")

    def test_force_restarts_backfill_from_newest_item(self) -> None:
        sync_github_metadata(self.store, self.repo, runner=PagedIssuesRunner(THREE_ISSUE_PAGES))
        self.assertTrue(self._reload().backfill("issues").complete)

        runner = PagedIssuesRunner(THREE_ISSUE_PAGES, events_status=304)
        stats = sync_github_metadata(self.store, self._reload(), force=True, runner=runner)

        self.assertEqual(runner.issue_cursors, [None, "C1", "C2"])
        self.assertEqual(stats.issues_upserted, 6)
        self.assertTrue(self._reload().backfill("issues").complete)

    def test_existing_watermark_without_backfill_record_backfills_from_newest_item(self) -> None:
        self.store.update_repo_sync_state(self.repo.repo_id, issues_watermark="2026-10-25T00:00:00Z")
        runner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        stats = sync_github_metadata(self.store, self._reload(), runner=runner)

        self.assertEqual(runner.issue_cursors, [None, "C1", "C2"])
        self.assertEqual(stats.issues_upserted, 6)
        repo = self._reload()
        self.assertTrue(repo.backfill("issues").complete)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")

    def test_total_count_progress_halving_warnings_and_sibling_stream_isolation(self) -> None:
        events_recorded: list[tuple[str, object]] = []

        class RecordingReporter:
            def start_phase(self, repo: str, phase: str, total: int | None = None) -> None:
                events_recorded.append(("start", (repo, phase, total)))

            def advance(self, completed: int, total: int | None = None, *, detail: str = "") -> None:
                events_recorded.append(("advance", (completed, total, detail)))

            def finish_phase(self, summary: str = "") -> None:
                events_recorded.append(("finish", summary))

            def warn(self, repo: str, message: str) -> None:
                events_recorded.append(("warn", (repo, message)))

            def error(self, repo: str, message: str) -> None:
                events_recorded.append(("error", (repo, message)))

            def clear_active_line(self) -> None:
                pass

        def fake_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            cmd = " ".join(args)
            if "/events" in cmd:
                return GhCommandResult(returncode=0, stdout='HTTP/2.0 200 OK\r\nETag: "e9"\r\n\r\n[]', stderr="")
            if "issues(first:" in cmd:
                if "pageSize=25" in cmd:
                    return GhCommandResult(returncode=1, stdout="", stderr="HTTP 502 Bad Gateway")
                conn = {
                    "totalCount": 42,
                    "pageInfo": {"hasNextPage": False, "endCursor": "I_END"},
                    "nodes": [_issue_node(10, "2026-10-10T00:00:00Z")],
                }
                return GhCommandResult(
                    returncode=0,
                    stdout=json.dumps({"data": {"repository": {"issues": conn}}}),
                    stderr="",
                )
            if "pullRequests(first:" in cmd:
                return GhCommandResult(returncode=1, stdout="", stderr="HTTP 401 Bad credentials")
            disc_conn = {
                "totalCount": 1,
                "pageInfo": {"hasNextPage": False, "endCursor": "D_END"},
                "nodes": [
                    {
                        "id": "D_1",
                        "number": 1,
                        "title": "Architecture Q&A",
                        "updatedAt": "2026-10-12T00:00:00Z",
                        "createdAt": "2026-10-12T00:00:00Z",
                        "url": "https://example.com/d/1",
                        "body": "Body",
                        "author": {"login": "carol"},
                        "category": {"name": "General"},
                        "answer": None,
                    }
                ],
            }
            return GhCommandResult(
                returncode=0,
                stdout=json.dumps({"data": {"repository": {"discussions": disc_conn}}}),
                stderr="",
            )

        reporter = RecordingReporter()
        stats = sync_github_metadata(self.store, self.repo, runner=fake_runner, reporter=reporter)

        self.assertEqual(stats.issues_upserted, 1)
        self.assertEqual(stats.discussions_upserted, 1)
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("github prs sync failed", stats.errors[0])
        self.assertIn("auth_error", stats.errors[0])
        self.assertIsNone(self._reload().last_full_sweep_at)

        warns = [payload for kind, payload in events_recorded if kind == "warn"]
        self.assertEqual(len(warns), 1)
        self.assertIn("pageSize=25", str(warns[0]))
        self.assertIn("pageSize=12", str(warns[0]))

        advances = [payload for kind, payload in events_recorded if kind == "advance"]
        self.assertIn((1, 42, "oldest 2026-10-10"), advances)


if __name__ == "__main__":
    unittest.main()
