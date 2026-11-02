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


if __name__ == "__main__":
    unittest.main()
