"""Unit tests for packrat conditional /events ETag polling and GraphQL delta sync."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

from db_store import PackratStore
from github_sync import (
    OPEN_PRS_QUERY,
    PRS_DELTA_QUERY,
    GhAuthProbe,
    GhCommandResult,
    _classify_gh_failure,
    _parse_pr_node,
    _split_http_response,
    extract_issue_links,
    needs_safety_sweep,
    poll_repo_events,
    probe_gh_auth,
    refetch_open_prs,
    refetch_open_prs_key,
    run_gh_command,
    sync_backfill_only,
    sync_github_metadata,
)
from models import BACKFILL_STREAMS, RepoConfig
from rate_budget import RATE_BUDGET_FILENAME, Priority, RateBudget
from sync_runtime import BoundedCmdResult


def _mark_backfill_complete(store: PackratStore, repo_id: int) -> None:
    """Marks every stream's history as fetched so syncs exercise incremental paging only."""
    for stream in BACKFILL_STREAMS:
        store.set_backfill_state(
            repo_id, stream, cursor=None, done_at="2026-10-01T00:00:00Z"
        )


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
                            "HTTP/2.0 304 Not Modified\r\n"
                            'ETag: W/"etag-1"\r\n'
                            "X-Poll-Interval: 60\r\n\r\n"
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
                                "HTTP/2.0 200 OK\r\n"
                                'ETag: W/"etag-2"\r\n'
                                "Last-Modified: Fri, 30 Oct 2026 23:00:00 GMT\r\n"
                                "X-Poll-Interval: 45\r\n\r\n"
                                '[{"id":"1","type":"PushEvent"}]'
                            ),
                            stderr="",
                        )
                    if "issues(first:" in cmd:
                        payload = {
                            "data": {
                                "repository": {
                                    "issues": {
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "cur-1",
                                        },
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
                                                "assignees": {
                                                    "nodes": [{"login": "bob"}]
                                                },
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
                        return GhCommandResult(
                            returncode=0, stdout=json.dumps(payload), stderr=""
                        )

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
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
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
                                                "closingIssuesReferences": {
                                                    "nodes": [{"number": 101}]
                                                },
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
                                                            "author": {
                                                                "login": "carol"
                                                            },
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
                                                                        "author": {
                                                                            "login": "carol"
                                                                        },
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
                        return GhCommandResult(
                            returncode=0, stdout=json.dumps(payload), stderr=""
                        )

                    if "discussions(first:" in cmd:
                        payload = {
                            "data": {
                                "repository": {
                                    "discussions": {
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
                                        "nodes": [
                                            {
                                                "id": "D_301",
                                                "number": 301,
                                                "title": "Custom catalog schema proposal",
                                                "updatedAt": "2026-10-29T18:00:00Z",
                                                "body": "How should custom catalog components register?",
                                                "author": {"login": "dave"},
                                                "category": {"name": "Ideas"},
                                                "answer": {
                                                    "body": "Use registerCatalogComponent."
                                                },
                                            }
                                        ],
                                    }
                                }
                            }
                        }
                        return GhCommandResult(
                            returncode=0, stdout=json.dumps(payload), stderr=""
                        )

                    return GhCommandResult(returncode=1, stdout="", stderr="unexpected")

                stats = sync_github_metadata(store, repo, runner=fake_runner)
                self.assertEqual(stats.issues_upserted, 1)
                self.assertEqual(stats.prs_upserted, 1)
                self.assertEqual(stats.discussions_upserted, 1)
                self.assertGreaterEqual(
                    pr_attempts, 2
                )  # Verified pageSize=25 -> pageSize=12 halving

                updated_repo = store.get_repo("octo-org/widgets")
                assert updated_repo is not None
                self.assertEqual(updated_repo.events_etag, 'W/"etag-2"')
                self.assertEqual(updated_repo.issues_watermark, "2026-10-28T12:00:00Z")
                self.assertEqual(updated_repo.prs_watermark, "2026-10-29T15:00:00Z")
                self.assertEqual(
                    updated_repo.discussions_watermark, "2026-10-29T18:00:00Z"
                )

                counts = store.table_counts(repo.repo_id)
                self.assertEqual(counts["issues"], 1)
                self.assertEqual(counts["pull_requests"], 1)
                self.assertEqual(counts["review_threads"], 1)
                self.assertEqual(counts["discussions"], 1)

                links = extract_issue_links(
                    repo.repo_id, 202, "Fixes #101 and references #88.", [101]
                )
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

                def timeout_runner(
                    args: Sequence[str], timeout: int
                ) -> GhCommandResult:
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

    def test_max_pages_truncation_preserves_watermark_and_force_clears_watermark(
        self,
    ) -> None:
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

                def truncated_runner(
                    args: Sequence[str], timeout: int
                ) -> GhCommandResult:
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
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "CUR_2",
                                        },
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
                        return GhCommandResult(
                            returncode=0, stdout=json.dumps(payload), stderr=""
                        )
                    return GhCommandResult(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "data": {
                                    "repository": {
                                        "pullRequests": {
                                            "pageInfo": {"hasNextPage": False},
                                            "nodes": [],
                                        },
                                        "discussions": {
                                            "pageInfo": {"hasNextPage": False},
                                            "nodes": [],
                                        },
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

                def complete_runner(
                    args: Sequence[str], timeout: int
                ) -> GhCommandResult:
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
                                        "pageInfo": {
                                            "hasNextPage": False,
                                            "endCursor": None,
                                        },
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
                        return GhCommandResult(
                            returncode=0, stdout=json.dumps(payload), stderr=""
                        )
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
            return GhCommandResult(
                returncode=0,
                stdout=f'HTTP/2.0 {status}\r\nETag: "e1"\r\n\r\n{body}',
                stderr="",
            )
        if "issues(first:" not in cmd:
            empty = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
            payload = {
                "data": {"repository": {"pullRequests": empty, "discussions": empty}}
            }
            return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")
        cursor = next(
            (a.split("=", 1)[1] for a in args if a.startswith("cursor=")), None
        )
        self.issue_cursors.append(cursor)
        if cursor in self.failing:
            return GhCommandResult(
                returncode=1, stdout="", stderr="HTTP 502 Bad Gateway"
            )
        numbers, has_next, end_cursor = self.pages[cursor]
        nodes = [_issue_node(n, f"2026-10-{n:02d}T00:00:00Z") for n in numbers]
        conn = {
            "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
            "nodes": nodes,
        }
        return GhCommandResult(
            returncode=0,
            stdout=json.dumps({"data": {"repository": {"issues": conn}}}),
            stderr="",
        )


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
        stats = sync_github_metadata(
            self.store, self.repo, backfill_max_pages=2, runner=runner
        )

        self.assertEqual(stats.issues_upserted, 4)
        self.assertEqual(stats.backfill_incomplete, ["issues"])
        repo = self._reload()
        self.assertEqual(repo.backfill("issues").cursor, "C2")
        self.assertFalse(repo.backfill("issues").complete)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")

        # A quiet repository (HTTP 304) must still resume the pending backfill.
        resume_runner = PagedIssuesRunner(THREE_ISSUE_PAGES, events_status=304)
        stats2 = sync_github_metadata(
            self.store, repo, backfill_max_pages=2, runner=resume_runner
        )

        self.assertFalse(stats2.events_not_modified)
        self.assertEqual(
            resume_runner.issue_cursors, [None, "C2"]
        )  # incremental pass, then resume
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
        sync_github_metadata(
            self.store, self.repo, runner=PagedIssuesRunner(THREE_ISSUE_PAGES)
        )
        self.assertTrue(self._reload().backfill("issues").complete)

        runner = PagedIssuesRunner(THREE_ISSUE_PAGES, events_status=304)
        stats = sync_github_metadata(
            self.store, self._reload(), force=True, runner=runner
        )

        self.assertEqual(runner.issue_cursors, [None, "C1", "C2"])
        self.assertEqual(stats.issues_upserted, 6)
        self.assertTrue(self._reload().backfill("issues").complete)

    def test_existing_watermark_without_backfill_record_backfills_from_newest_item(
        self,
    ) -> None:
        self.store.update_repo_sync_state(
            self.repo.repo_id, issues_watermark="2026-10-25T00:00:00Z"
        )
        runner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        stats = sync_github_metadata(self.store, self._reload(), runner=runner)

        self.assertEqual(runner.issue_cursors, [None, "C1", "C2"])
        self.assertEqual(stats.issues_upserted, 6)
        repo = self._reload()
        self.assertTrue(repo.backfill("issues").complete)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")

    def test_total_count_progress_halving_warnings_and_sibling_stream_isolation(
        self,
    ) -> None:
        events_recorded: list[tuple[str, object]] = []

        class RecordingReporter:
            def start_phase(
                self, repo: str, phase: str, total: int | None = None
            ) -> None:
                events_recorded.append(("start", (repo, phase, total)))

            def advance(
                self, completed: int, total: int | None = None, *, detail: str = ""
            ) -> None:
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
                return GhCommandResult(
                    returncode=0,
                    stdout='HTTP/2.0 200 OK\r\nETag: "e9"\r\n\r\n[]',
                    stderr="",
                )
            if "issues(first:" in cmd:
                if "pageSize=25" in cmd:
                    return GhCommandResult(
                        returncode=1, stdout="", stderr="HTTP 502 Bad Gateway"
                    )
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
                return GhCommandResult(
                    returncode=1, stdout="", stderr="HTTP 401 Bad credentials"
                )
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
        stats = sync_github_metadata(
            self.store, self.repo, runner=fake_runner, reporter=reporter
        )

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


def _graphql_timeout_or_small_page(
    args: Sequence[str],
    recorded_sizes: list[str],
) -> GhCommandResult:
    """Fails with timeout on pageSize 25 and 12, then succeeds on pageSize 5."""
    cmd = " ".join(args)
    if "/events" in cmd:
        return GhCommandResult(
            returncode=0,
            stdout='HTTP/2.0 200 OK\r\nETag: "e-ok"\r\n\r\n[]',
            stderr="",
        )
    if "issues(first:" in cmd:
        size_arg = next((a for a in args if a.startswith("pageSize=")), "")
        recorded_sizes.append(size_arg)
        if size_arg in ("pageSize=25", "pageSize=12"):
            return GhCommandResult(
                returncode=124,
                stdout="",
                stderr="gh command timed out",
                timed_out=True,
            )
        conn = {
            "pageInfo": {"hasNextPage": False, "endCursor": "END"},
            "nodes": [_issue_node(7, "2026-10-07T00:00:00Z")],
        }
        body = json.dumps({"data": {"repository": {"issues": conn}}})
        return GhCommandResult(returncode=0, stdout=body, stderr="")
    empty = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    payload = {"data": {"repository": {"pullRequests": empty, "discussions": empty}}}
    return GhCommandResult(returncode=0, stdout=json.dumps(payload), stderr="")


class TestGhAuthProbeAndCircuitBreaker(unittest.TestCase):
    """Tests bounded gh execution, auth token probing, and keyring circuit breaking."""

    def test_run_gh_command_delegates_to_bounded_subprocess(self) -> None:
        ok_res = BoundedCmdResult(
            returncode=0, stdout="tok\n", stderr="", timed_out=False
        )
        with patch(
            "gh_transport.run_bounded_subprocess", return_value=ok_res
        ) as mocked:
            res = run_gh_command(["auth", "token"], timeout=5)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "tok\n")
            self.assertFalse(res.timed_out)
            cmd_arg = mocked.call_args.args[0]
            env_arg = mocked.call_args.kwargs["env"]
            self.assertEqual(cmd_arg, ["gh", "auth", "token"])
            self.assertEqual(env_arg["GH_PROMPT_DISABLED"], "1")
            self.assertEqual(env_arg["GIT_TERMINAL_PROMPT"], "0")
            self.assertEqual(env_arg["PAGER"], "cat")
            self.assertEqual(mocked.call_args.kwargs["timeout"], 5)

        to_res = BoundedCmdResult(
            returncode=124, stdout="", stderr="timed out", timed_out=True
        )
        with patch("gh_transport.run_bounded_subprocess", return_value=to_res):
            timed = run_gh_command(["api", "user"], timeout=3)
            self.assertTrue(timed.timed_out)
            self.assertEqual(timed.returncode, 124)
            self.assertIn("timed out", timed.stderr)

    def test_probe_gh_auth_unlocked_token(self) -> None:
        seen: list[tuple[list[str], int]] = []

        def fake_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            seen.append((list(args), timeout))
            return GhCommandResult(returncode=0, stdout="gho_valid_token\n", stderr="")

        probe = probe_gh_auth(runner=fake_runner, timeout=5)
        self.assertTrue(probe.ok)
        self.assertIsNone(probe.error_kind)
        self.assertEqual(probe.detail, "")
        self.assertEqual(seen, [(["auth", "token", "--no-refresh"], 5)])

    def test_probe_gh_auth_timeout_and_dbus_keyring_stderr(self) -> None:
        def timeout_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            return GhCommandResult(
                returncode=124, stdout="", stderr="timed out", timed_out=True
            )

        timed_probe = probe_gh_auth(runner=timeout_runner)
        self.assertFalse(timed_probe.ok)
        self.assertEqual(timed_probe.error_kind, "keyring_locked")

        for err_msg in (
            "failed to get token from keyring: collection is locked",
            "org.freedesktop.Secret.Service error over D-Bus",
            "secret service unavailable",
        ):
            res = GhCommandResult(returncode=1, stdout="", stderr=err_msg)
            probe = probe_gh_auth(runner=lambda _a, _t, r=res: r)
            self.assertFalse(probe.ok)
            self.assertEqual(probe.error_kind, "keyring_locked")
            self.assertIn(err_msg, probe.detail)

    def test_probe_gh_auth_logged_out_and_empty_token(self) -> None:
        logged_out = GhCommandResult(
            returncode=1,
            stdout="",
            stderr="You are not logged into any GitHub hosts. Run gh auth login",
        )
        probe = probe_gh_auth(runner=lambda _a, _t: logged_out)
        self.assertFalse(probe.ok)
        self.assertEqual(probe.error_kind, "auth_error")
        self.assertIn("not logged into any GitHub hosts", probe.detail)

        blank_ok = GhCommandResult(returncode=0, stdout="   \n", stderr="")
        blank_probe = probe_gh_auth(runner=lambda _a, _t: blank_ok)
        self.assertFalse(blank_probe.ok)
        self.assertEqual(blank_probe.error_kind, "auth_error")

    def test_probe_gh_auth_falls_back_when_no_refresh_rejected(self) -> None:
        calls: list[list[str]] = []

        def legacy_gh_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            calls.append(list(args))
            if "--no-refresh" in args:
                return GhCommandResult(
                    returncode=1,
                    stdout="",
                    stderr="unknown flag: --no-refresh",
                )
            return GhCommandResult(returncode=0, stdout="gho_legacy_tok\n", stderr="")

        probe = probe_gh_auth(runner=legacy_gh_runner, timeout=4)
        self.assertTrue(probe.ok)
        self.assertEqual(calls, [["auth", "token", "--no-refresh"], ["auth", "token"]])

    def test_mid_run_keyring_locked_aborts_halving_backfill_and_streams(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                store.update_repo_sync_state(
                    repo.repo_id,
                    issues_watermark="2026-10-20T00:00:00Z",
                )
                store.set_backfill_state(
                    repo.repo_id,
                    "issues",
                    cursor="C_RESUME",
                    done_at=None,
                )
                repo = store.get_repo_by_id(repo.repo_id)
                assert repo is not None

                graphql_calls: list[str] = []
                probe_calls = 0

                def timeout_graphql_runner(
                    args: Sequence[str], timeout: int
                ) -> GhCommandResult:
                    cmd = " ".join(args)
                    if "/events" in cmd:
                        return GhCommandResult(
                            returncode=0,
                            stdout='HTTP/2.0 200 OK\r\nETag: "e-lock"\r\n\r\n[]',
                            stderr="",
                        )
                    graphql_calls.append(cmd)
                    return GhCommandResult(
                        returncode=124,
                        stdout="",
                        stderr="gh command timed out",
                        timed_out=True,
                    )

                def locked_probe() -> GhAuthProbe:
                    nonlocal probe_calls
                    probe_calls += 1
                    return GhAuthProbe(
                        ok=False,
                        error_kind="keyring_locked",
                        detail="gnome-keyring collection is locked",
                    )

                stats = sync_github_metadata(
                    store,
                    repo,
                    runner=timeout_graphql_runner,
                    auth_probe=locked_probe,
                )

                self.assertEqual(probe_calls, 1)
                self.assertEqual(len(graphql_calls), 1)
                self.assertIn("issues(first:", graphql_calls[0])
                self.assertIn("pageSize=25", graphql_calls[0])
                self.assertNotIn("cursor=", graphql_calls[0])
                self.assertEqual(stats.backfill_incomplete, ["issues"])
                self.assertEqual(len(stats.errors), 1)
                self.assertIn("keyring_locked", stats.errors[0])

                reloaded = store.get_repo_by_id(repo.repo_id)
                assert reloaded is not None
                self.assertEqual(reloaded.backfill("issues").cursor, "C_RESUME")
                self.assertEqual(reloaded.issues_watermark, "2026-10-20T00:00:00Z")
            finally:
                store.close()

    def test_mid_run_timeout_with_unlocked_probe_keeps_page_halving(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PackratStore(Path(tmp) / "repos.sqlite")
            try:
                repo = store.register_repo("octo-org", "widgets")
                recorded_sizes: list[str] = []
                probe_calls = 0

                def unlocked_probe() -> GhAuthProbe:
                    nonlocal probe_calls
                    probe_calls += 1
                    return GhAuthProbe(ok=True)

                stats = sync_github_metadata(
                    store,
                    repo,
                    runner=lambda args, _t: _graphql_timeout_or_small_page(
                        args, recorded_sizes
                    ),
                    auth_probe=unlocked_probe,
                )

                self.assertEqual(probe_calls, 2)
                self.assertEqual(
                    recorded_sizes, ["pageSize=25", "pageSize=12", "pageSize=5"]
                )
                self.assertEqual(stats.issues_upserted, 1)
                self.assertEqual(stats.errors, [])
            finally:
                store.close()


def _rate_header_block(status: str, remaining: int, resource: str = "graphql") -> str:
    """Builds a `gh api -i` header block with `X-RateLimit-*` headers for one response."""
    reset = int(time.time()) + 3600
    return (
        f"HTTP/2.0 {status}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"X-Ratelimit-Limit: 5000\r\n"
        f"X-Ratelimit-Remaining: {remaining}\r\n"
        f"X-Ratelimit-Reset: {reset}\r\n"
        f"X-Ratelimit-Resource: {resource}\r\n"
        f"X-Ratelimit-Used: {5000 - remaining}\r\n\r\n"
    )


class IncludeHeadersRunner:
    """Wraps PagedIssuesRunner so every GraphQL response looks like `gh api -i` output.

    `remaining` lists the `X-RateLimit-Remaining` value for each successive
    GraphQL response; the last value repeats once the list is used up.
    """

    def __init__(self, inner: PagedIssuesRunner, remaining: list[int]) -> None:
        self.inner = inner
        self.remaining = list(remaining)
        self.graphql_args: list[list[str]] = []

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        res = self.inner(args, timeout)
        if "graphql" not in args:
            return res
        self.graphql_args.append(list(args))
        value = self.remaining.pop(0) if len(self.remaining) > 1 else self.remaining[0]
        return GhCommandResult(
            returncode=res.returncode,
            stdout=_rate_header_block("200 OK", value) + res.stdout,
            stderr=res.stderr,
        )


RATE_LIMIT_BODY = json.dumps(
    {
        "message": "API rate limit exceeded for user ID 1.",
        "documentation_url": "https://docs.github.com/rest/overview/rate-limits-for-the-rest-api",
    }
)


class TestRateBudgetIntegration(unittest.TestCase):
    """Tests `gh api -i` header parsing, budget deferral, and rate-limit classification."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PackratStore(Path(self.tmp.name) / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets")
        self.budget = RateBudget(Path(self.tmp.name) / RATE_BUDGET_FILENAME)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _reload(self) -> RepoConfig:
        repo = self.store.get_repo_by_id(self.repo.repo_id)
        assert repo is not None
        return repo

    def test_include_output_records_budget_and_header_401_is_not_auth_error(
        self,
    ) -> None:
        # 4013 contains "401": the header block must never reach failure classification.
        runner = IncludeHeadersRunner(PagedIssuesRunner(THREE_ISSUE_PAGES), [4013])
        stats = sync_github_metadata(
            self.store, self.repo, runner=runner, budget=self.budget
        )

        self.assertEqual(stats.errors, [])
        self.assertEqual(stats.deferred_streams, [])
        self.assertFalse(stats.deferred)
        self.assertEqual(stats.issues_upserted, 6)
        self.assertTrue(all("-i" in a for a in runner.graphql_args))
        snap = self.budget.current("graphql")
        assert snap is not None
        self.assertEqual((snap.remaining, snap.limit), (4013, 5000))

    def test_budget_drop_mid_backfill_defers_and_keeps_cursor(self) -> None:
        # Page 1 leaves 18% of the budget, below the 20% BACKFILL reserve.
        inner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        runner = IncludeHeadersRunner(inner, [900])
        stats = sync_github_metadata(
            self.store, self.repo, runner=runner, budget=self.budget
        )

        self.assertEqual(inner.issue_cursors, [None])
        self.assertEqual(stats.issues_upserted, 2)
        self.assertEqual(stats.errors, [])
        self.assertTrue(stats.deferred)
        self.assertEqual(stats.deferred_streams, ["issues", "prs", "discussions"])
        self.assertIn("issues", stats.backfill_incomplete)
        repo = self._reload()
        self.assertEqual(repo.backfill("issues").cursor, "C1")
        self.assertFalse(repo.backfill("issues").complete)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")
        self.assertIsNone(repo.last_full_sweep_at)

    def test_low_budget_defers_incremental_walk_without_graphql_calls(self) -> None:
        self.store.update_repo_sync_state(
            self.repo.repo_id, issues_watermark="2026-10-20T00:00:00Z"
        )
        self.store.set_backfill_state(
            self.repo.repo_id, "issues", cursor="C1", done_at=None
        )
        self.budget.record(
            _rate_headers(500), worker="t"
        )  # 10%: below the MIRROR reserve

        inner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        runner = IncludeHeadersRunner(inner, [500])
        stats = sync_github_metadata(
            self.store, self._reload(), runner=runner, budget=self.budget
        )

        self.assertEqual(runner.graphql_args, [])
        self.assertEqual(stats.errors, [])
        self.assertEqual(stats.deferred_streams, ["issues", "prs", "discussions"])
        repo = self._reload()
        self.assertEqual(repo.backfill("issues").cursor, "C1")
        self.assertEqual(repo.issues_watermark, "2026-10-20T00:00:00Z")

    def test_rate_limited_graphql_skips_halving_and_defers_remaining_streams(
        self,
    ) -> None:
        calls: list[str] = []

        def runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            cmd = " ".join(args)
            if "/events" in cmd:
                return GhCommandResult(0, 'HTTP/2.0 200 OK\r\nETag: "e1"\r\n\r\n[]', "")
            calls.append(cmd)
            return GhCommandResult(
                returncode=1,
                stdout=_rate_header_block("403 Forbidden", 0) + RATE_LIMIT_BODY,
                stderr="gh: API rate limit exceeded for user ID 1. (HTTP 403)",
            )

        stats = sync_github_metadata(
            self.store, self.repo, runner=runner, budget=self.budget
        )

        self.assertEqual(len(calls), 1)
        self.assertIn("pageSize=25", calls[0])
        self.assertEqual(stats.errors, [])
        self.assertTrue(stats.deferred)
        self.assertEqual(stats.deferred_streams, ["issues", "prs", "discussions"])
        self.assertIsNone(self._reload().last_full_sweep_at)
        self.assertFalse(self.budget.allows("graphql", Priority.VIEWER))

    def test_events_poll_rate_limit_and_low_core_budget_defer(self) -> None:
        def limited_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            return GhCommandResult(
                returncode=1,
                stdout=_rate_header_block("403 Forbidden", 0, "core") + RATE_LIMIT_BODY,
                stderr="gh: API rate limit exceeded for user ID 1. (HTTP 403)",
            )

        stats = sync_github_metadata(
            self.store, self.repo, runner=limited_runner, budget=self.budget
        )
        self.assertEqual(stats.errors, [])
        self.assertEqual(stats.deferred_streams, ["events"])
        self.assertIn("deferred_rate_limit", stats.skipped_reason or "")

        calls: list[Sequence[str]] = []

        def counting_runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            calls.append(args)
            return limited_runner(args, timeout)

        stats2 = sync_github_metadata(
            self.store, self.repo, runner=counting_runner, budget=self.budget
        )
        self.assertEqual(calls, [])
        self.assertEqual(stats2.deferred_streams, ["events"])

    def test_classify_gh_failure_rate_limits_before_auth(self) -> None:
        header_401 = _rate_header_block("502 Bad Gateway", 4013)
        cases = [
            ("gh: API rate limit exceeded (HTTP 403)", "", "rate_limited"),
            (
                "gh: You have exceeded a secondary rate limit (HTTP 403)",
                "",
                "rate_limited",
            ),
            (
                "",
                '{"errors":[{"type":"RATE_LIMITED","message":"limit"}]}',
                "rate_limited",
            ),
            (
                "",
                '{"errors":[{"extensions":{"code":"graphql_rate_limit"}}]}',
                "rate_limited",
            ),
            ("gh: Bad credentials (HTTP 401)", "", "auth_error"),
            ("gh: Bad Gateway (HTTP 502)", "", "http_error"),
        ]
        for stderr, body, expected in cases:
            res = GhCommandResult(returncode=1, stdout=header_401 + body, stderr=stderr)
            self.assertEqual(
                _classify_gh_failure(res, body)[0], expected, stderr or body
            )


def _rate_headers(remaining: int) -> dict[str, str]:
    """Builds a lowercase `x-ratelimit-*` header dict for a graphql snapshot."""
    return {
        "x-ratelimit-resource": "graphql",
        "x-ratelimit-limit": "5000",
        "x-ratelimit-remaining": str(remaining),
        "x-ratelimit-reset": str(int(time.time()) + 3600),
    }


def _pr_node(
    number: int, *, head_owner: str | None, cross_repo: bool | None
) -> dict[str, object]:
    """Builds a minimal open GraphQL pullRequest node with fork fields."""
    node: dict[str, object] = {
        "id": f"PR_{number}",
        "number": number,
        "title": f"PR {number}",
        "state": "OPEN",
        "isDraft": False,
        "updatedAt": f"2026-10-{number:02d}T00:00:00Z",
        "createdAt": f"2026-10-{number:02d}T00:00:00Z",
        "body": "Body",
        "baseRefName": "main",
        "headRefName": f"branch-{number}",
        "headRefOid": f"sha{number}",
        "author": {"login": "alice"},
    }
    if cross_repo is not None:
        node["isCrossRepository"] = cross_repo
    if head_owner is not None:
        node["headRepositoryOwner"] = {"login": head_owner}
    return node


OPEN_PR_PAGES: dict[str | None, tuple[list[dict[str, object]], bool, str | None]] = {
    None: (
        [
            _pr_node(12, head_owner="octo-org", cross_repo=False),
            _pr_node(11, head_owner="forker", cross_repo=True),
        ],
        True,
        "P1",
    ),
    "P1": ([_pr_node(10, head_owner="octo-org", cross_repo=False)], False, "P2"),
}


class OpenPrsRunner:
    """Fake gh runner serving `OPEN_PRS_QUERY` pages; every other stream is empty.

    `/events` answers 304. `mode` selects the open-PR answer: "ok" serves
    `OPEN_PR_PAGES` by cursor, "error" answers HTTP 502, and "rate_limited"
    answers a 403 rate-limit error.
    """

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.open_cursors: list[str | None] = []
        self.stream_calls: list[str] = []

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        cmd = " ".join(args)
        if "/events" in cmd:
            return GhCommandResult(
                0, 'HTTP/2.0 304 Not Modified\r\nETag: "e1"\r\n\r\n', ""
            )
        if "states: OPEN" not in cmd:
            self.stream_calls.append(cmd)
            empty = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
            payload = {
                "data": {
                    "repository": {
                        "issues": empty,
                        "pullRequests": empty,
                        "discussions": empty,
                    }
                }
            }
            return GhCommandResult(0, json.dumps(payload), "")
        cursor = next(
            (a.split("=", 1)[1] for a in args if a.startswith("cursor=")), None
        )
        self.open_cursors.append(cursor)
        if self.mode == "error":
            return GhCommandResult(1, "", "HTTP 502 Bad Gateway")
        if self.mode == "rate_limited":
            return GhCommandResult(
                1,
                _rate_header_block("403 Forbidden", 0) + RATE_LIMIT_BODY,
                "gh: API rate limit exceeded for user ID 1. (HTTP 403)",
            )
        nodes, has_next, end_cursor = OPEN_PR_PAGES[cursor]
        conn = {
            "pageInfo": {"hasNextPage": has_next, "endCursor": end_cursor},
            "nodes": nodes,
        }
        return GhCommandResult(
            0, json.dumps({"data": {"repository": {"pullRequests": conn}}}), ""
        )


class TestOpenPrRefetch(unittest.TestCase):
    """Tests fork-field parsing and the one-time open-PR refetch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PackratStore(Path(self.tmp.name) / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets")
        _mark_backfill_complete(self.store, self.repo.repo_id)
        self.store.update_repo_sync_state(
            self.repo.repo_id,
            events_etag='"e1"',
            last_full_sweep_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
        self.key = refetch_open_prs_key(self.repo.repo_id)
        self.store.set_state(self.key, "1")
        self.budget = RateBudget(Path(self.tmp.name) / RATE_BUDGET_FILENAME)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _reload(self) -> RepoConfig:
        repo = self.store.get_repo_by_id(self.repo.repo_id)
        assert repo is not None
        return repo

    def _fork_fields(self) -> dict[int, tuple[object, object]]:
        rows = self.store.conn.execute(
            "SELECT number, head_repo_owner, is_cross_repository FROM pull_requests"
            " WHERE repo_id = ?",
            (self.repo.repo_id,),
        ).fetchall()
        return {int(r[0]): (r[1], r[2]) for r in rows}

    def test_queries_select_fork_fields(self) -> None:
        for query in (PRS_DELTA_QUERY, OPEN_PRS_QUERY):
            self.assertIn("isCrossRepository", query)
            self.assertIn("headRepositoryOwner { login }", query)
        self.assertIn("pullRequests(first: $pageSize", PRS_DELTA_QUERY)
        self.assertIn(
            "pullRequests(states: OPEN, first: $pageSize, after: $cursor",
            OPEN_PRS_QUERY,
        )

    def test_parse_pr_node_fills_fork_fields(self) -> None:
        same, _ = _parse_pr_node(
            self.repo, _pr_node(1, head_owner="octo-org", cross_repo=False)
        )
        fork, _ = _parse_pr_node(
            self.repo, _pr_node(2, head_owner="forker", cross_repo=True)
        )
        unknown, _ = _parse_pr_node(
            self.repo, _pr_node(3, head_owner=None, cross_repo=None)
        )
        self.assertEqual(
            (same.head_repo_owner, same.is_cross_repository), ("octo-org", False)
        )
        self.assertEqual(
            (fork.head_repo_owner, fork.is_cross_repository), ("forker", True)
        )
        self.assertEqual(
            (unknown.head_repo_owner, unknown.is_cross_repository), (None, None)
        )

    def test_refetch_runs_once_on_304_and_clears_flag(self) -> None:
        runner = OpenPrsRunner()
        stats = sync_github_metadata(self.store, self._reload(), runner=runner)

        self.assertEqual(runner.open_cursors, [None, "P1"])
        self.assertEqual(runner.stream_calls, [])
        self.assertTrue(stats.events_not_modified)
        self.assertEqual(stats.prs_upserted, 3)
        self.assertEqual(stats.errors, [])
        self.assertIsNone(self.store.get_state(self.key))
        self.assertEqual(
            self._fork_fields(),
            {12: ("octo-org", 0), 11: ("forker", 1), 10: ("octo-org", 0)},
        )

        second = OpenPrsRunner()
        stats2 = sync_github_metadata(self.store, self._reload(), runner=second)
        self.assertEqual(second.open_cursors, [])
        self.assertEqual(stats2.prs_upserted, 0)

    def test_deferred_refetch_keeps_flag(self) -> None:
        self.budget.record(
            _rate_headers(500), worker="t"
        )  # 10%: below the MIRROR reserve
        runner = OpenPrsRunner()
        stats = sync_github_metadata(
            self.store, self._reload(), runner=runner, budget=self.budget
        )

        self.assertEqual(runner.open_cursors, [])
        self.assertEqual(stats.errors, [])
        self.assertTrue(stats.deferred)
        self.assertEqual(stats.deferred_streams, ["open_prs"])
        self.assertEqual(self.store.get_state(self.key), "1")

    def test_rate_limited_refetch_defers_every_stream_and_keeps_flag(self) -> None:
        self.store.update_repo_sync_state(
            self.repo.repo_id, last_full_sweep_at="2020-01-01T00:00:00Z"
        )
        runner = OpenPrsRunner("rate_limited")
        stats = sync_github_metadata(
            self.store, self._reload(), runner=runner, budget=self.budget
        )

        self.assertEqual(runner.open_cursors, [None])
        self.assertEqual(runner.stream_calls, [])
        self.assertEqual(stats.errors, [])
        self.assertEqual(
            stats.deferred_streams, ["open_prs", "issues", "prs", "discussions"]
        )
        self.assertEqual(self.store.get_state(self.key), "1")

    def test_failed_refetch_records_error_and_keeps_flag(self) -> None:
        runner = OpenPrsRunner("error")
        stats = sync_github_metadata(self.store, self._reload(), runner=runner)

        self.assertEqual(len(runner.open_cursors), 3)  # page-size halving 25 -> 12 -> 5
        self.assertEqual(stats.deferred_streams, [])
        self.assertEqual(len(stats.errors), 1)
        self.assertIn("github open_prs sync failed", stats.errors[0])
        self.assertEqual(self.store.get_state(self.key), "1")

    def test_public_refetch_returns_count_and_clears_flag(self) -> None:
        runner = OpenPrsRunner()
        count = refetch_open_prs(self.store, self._reload(), runner, budget=self.budget)
        self.assertEqual(count, 3)
        self.assertIsNone(self.store.get_state(self.key))


class RecordingRunner:
    """Wraps a fake runner and records the joined command line of every call."""

    def __init__(self, inner: PagedIssuesRunner) -> None:
        self.inner = inner
        self.commands: list[str] = []

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        self.commands.append(" ".join(args))
        return self.inner(args, timeout)


class TestSyncBackfillOnly(unittest.TestCase):
    """Tests `sync_backfill_only`, the walk run by the service's backfill child."""

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

    def _issues_in_progress(self, *, others_complete: bool) -> None:
        """Leaves the issues backfill at cursor C1 with a watermark already set."""
        if others_complete:
            _mark_backfill_complete(self.store, self.repo.repo_id)
        self.store.update_repo_sync_state(
            self.repo.repo_id, issues_watermark="2026-10-30T00:00:00Z"
        )
        self.store.set_backfill_state(
            self.repo.repo_id, "issues", cursor="C1", done_at=None
        )

    def test_in_progress_backfill_resumes_without_events_or_incremental_pass(
        self,
    ) -> None:
        self._issues_in_progress(others_complete=True)
        inner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        runner = RecordingRunner(inner)
        stats = sync_backfill_only(self.store, self._reload(), runner=runner)

        self.assertEqual(inner.issue_cursors, ["C1", "C2"])
        self.assertFalse(any("/events" in c for c in runner.commands))
        self.assertEqual(stats.issues_upserted, 4)
        self.assertEqual((stats.errors, stats.deferred_streams), ([], []))
        repo = self._reload()
        self.assertTrue(repo.backfill("issues").complete)
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")
        self.assertIsNone(repo.events_etag)

    def test_complete_repo_makes_no_calls(self) -> None:
        _mark_backfill_complete(self.store, self.repo.repo_id)
        runner = RecordingRunner(PagedIssuesRunner(THREE_ISSUE_PAGES))
        stats = sync_backfill_only(self.store, self._reload(), runner=runner)
        self.assertEqual(runner.commands, [])
        self.assertEqual(stats.issues_upserted, 0)

    def test_unstarted_backfill_walks_from_newest_item(self) -> None:
        inner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        runner = RecordingRunner(inner)
        stats = sync_backfill_only(self.store, self.repo, runner=runner)
        self.assertEqual(inner.issue_cursors, [None, "C1", "C2"])
        self.assertFalse(any("/events" in c for c in runner.commands))
        self.assertEqual(stats.issues_upserted, 6)
        repo = self._reload()
        self.assertTrue(all(repo.backfill(s).complete for s in BACKFILL_STREAMS))
        self.assertEqual(repo.issues_watermark, "2026-10-30T00:00:00Z")

    def test_budget_deferral_marks_remaining_streams_deferred(self) -> None:
        self._issues_in_progress(others_complete=False)
        budget = RateBudget(Path(self.tmp.name) / RATE_BUDGET_FILENAME)
        budget.record(_rate_headers(500), worker="t")  # 10%: below the BACKFILL reserve
        inner = PagedIssuesRunner(THREE_ISSUE_PAGES)
        runner = IncludeHeadersRunner(inner, [500])
        stats = sync_backfill_only(
            self.store, self._reload(), runner=runner, budget=budget
        )

        self.assertEqual(runner.graphql_args, [])
        self.assertEqual(stats.errors, [])
        self.assertTrue(stats.deferred)
        self.assertEqual(stats.deferred_streams, ["issues", "prs", "discussions"])
        self.assertEqual(self._reload().backfill("issues").cursor, "C1")


class TestReviewFixes(unittest.TestCase):
    """Regression tests for code-review findings M1, L7, and Q1."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PackratStore(Path(self.tmp.name) / "repos.sqlite")
        repo = self.store.register_repo("octo-org", "widgets")
        self.store.update_repo_sync_state(
            repo.repo_id,
            events_etag='"old"',
            issues_watermark="2026-10-20T00:00:00Z",
            prs_watermark="2026-10-20T00:00:00Z",
            discussions_watermark="2026-10-20T00:00:00Z",
        )
        _mark_backfill_complete(self.store, repo.repo_id)
        self.store.set_state(refetch_open_prs_key(repo.repo_id), None)
        self.repo = self.store.get_repo_by_id(repo.repo_id)
        assert self.repo is not None

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _etag(self) -> str | None:
        repo = self.store.get_repo_by_id(self.repo.repo_id)
        assert repo is not None
        return repo.events_etag

    def test_incomplete_run_keeps_the_previous_events_etag(self) -> None:
        # M1: a failed stream must not let the next poll answer 304.
        runner = PagedIssuesRunner(
            {None: ([30], False, None)}, failing=frozenset({None})
        )
        stats = sync_github_metadata(self.store, self.repo, runner=runner)
        self.assertTrue(stats.errors)
        self.assertEqual(self._etag(), '"old"')

    def test_complete_run_advances_the_events_etag(self) -> None:
        runner = PagedIssuesRunner({None: ([30], False, None)})
        stats = sync_github_metadata(self.store, self.repo, runner=runner)
        self.assertEqual(stats.errors, [])
        self.assertEqual(self._etag(), '"e1"')

    def test_skip_events_poll_walks_without_calling_events(self) -> None:
        # L7: the service already saw the change, so don't poll /events again.
        calls: list[str] = []
        inner = PagedIssuesRunner({None: ([30], False, None)})

        def runner(args: Sequence[str], timeout: int) -> GhCommandResult:
            calls.append(" ".join(args))
            return inner(args, timeout)

        stats = sync_github_metadata(
            self.store, self.repo, runner=runner, skip_events_poll=True
        )
        self.assertFalse(any("/events" in c for c in calls))
        self.assertEqual(stats.issues_upserted, 1)
        self.assertEqual(self._etag(), '"old"')

    def test_split_http_response_uses_the_last_header_block(self) -> None:
        # Q1: interim or redirect header blocks before the final response.
        raw = (
            "HTTP/1.1 100 Continue\r\n\r\n"
            "HTTP/2.0 301 Moved Permanently\r\nLocation: x\r\n\r\n"
            'HTTP/2.0 200 OK\r\nX-Ratelimit-Remaining: 7\r\n\r\n{"ok": true}'
        )
        status, headers, body = _split_http_response(raw)
        self.assertEqual(
            (status, headers["x-ratelimit-remaining"], body), (200, "7", '{"ok": true}')
        )


if __name__ == "__main__":
    unittest.main()
