"""Unit tests for normalize.py."""

from __future__ import annotations

import copy
import json
import unittest

from models import ViewerReview
from normalize import normalize_checks, normalize_pr, resolve_stack_parents
from testing import load_fixture


class TestNormalize(unittest.TestCase):
    def test_same_named_checks_keep_the_worst_run_in_any_order(self) -> None:
        def run(conclusion: str | None, status: str = "COMPLETED") -> dict[str, object]:
            return {"__typename": "CheckRun", "name": "build", "status": status,
                    "conclusion": conclusion}  # fmt: skip

        cases = [
            ([run("FAILURE"), run("SUCCESS")], ("COMPLETED", "FAILURE")),
            ([run("SUCCESS"), run("FAILURE")], ("COMPLETED", "FAILURE")),
            ([run(None, "IN_PROGRESS"), run("SUCCESS")], ("IN_PROGRESS", None)),
            ([run("TIMED_OUT"), run(None, "QUEUED")], ("COMPLETED", "TIMED_OUT")),
        ]
        for nodes, expected in cases:
            rollup = {"contexts": {"totalCount": 2, "nodes": nodes}}
            checks = normalize_checks(rollup)
            self.assertEqual([(c.status, c.conclusion) for c in checks], [expected])

    def test_normalize_pr_open_ci_running_fixture(self) -> None:
        payload = json.loads(load_fixture("pr_open_ci_running.json"))
        raw = payload["data"]["pr0"]["pullRequest"]
        snap = normalize_pr(raw, viewer="alice")
        self.assertEqual(snap.key, "acme/widget#3339")
        self.assertTrue(snap.mine)
        self.assertEqual(snap.state, "OPEN")
        self.assertFalse(snap.draft)
        self.assertEqual(snap.last_committer, "alice")
        self.assertEqual(snap.head_committed_date, "2026-10-27T21:00:00Z")
        self.assertEqual(snap.rollup_state, "PENDING")
        self.assertEqual(snap.requested_reviewers, ["bob", "team:core-reviewers"])
        self.assertEqual(snap.assignees, ["alice"])
        self.assertEqual(snap.approvals, ["carol"])
        self.assertEqual(snap.changes_requested, [])
        self.assertEqual(snap.required_summary(), "PENDING")

        by_name = {c.name: c for c in snap.checks}
        self.assertEqual(set(by_name.keys()), {"test-web", "lint-optional", "cla/google"})
        self.assertEqual(by_name["test-web"].run_id, 500)
        self.assertTrue(by_name["test-web"].required)
        self.assertFalse(by_name["lint-optional"].required)
        self.assertEqual(by_name["cla/google"].status, "COMPLETED")
        self.assertEqual(by_name["cla/google"].conclusion, "SUCCESS")
        self.assertIsNone(by_name["cla/google"].run_id)

        t_by_id = {t.id: t for t in snap.threads}
        self.assertTrue(t_by_id["PRRT_bot_1"].last_author_is_bot)
        self.assertFalse(t_by_id["PRRT_human_1"].last_author_is_bot)
        self.assertEqual(snap.truncated, [])

    def test_normalize_stack_and_truncation(self) -> None:
        payload = json.loads(load_fixture("pr_stack.json"))
        raw_parent = payload["data"]["pr0"]["pullRequest"]
        raw_child = payload["data"]["pr1"]["pullRequest"]
        raw_child["reviewThreads"]["totalCount"] = 150

        s_parent = normalize_pr(raw_parent, viewer="bob")
        s_child = normalize_pr(raw_child, viewer="alice")
        self.assertFalse(s_parent.mine)
        self.assertEqual(s_parent.state, "MERGED")
        self.assertIsNone(s_parent.rollup_state)
        self.assertTrue(s_child.auto_merge)
        self.assertEqual(s_child.truncated, ["reviewThreads"])

        snaps = {s_parent.key: s_parent, s_child.key: s_child}
        resolve_stack_parents(snaps)
        self.assertIsNone(s_parent.stack_parent)
        self.assertEqual(s_child.stack_parent, "acme/widget#3338")
        self.assertEqual(s_child.stack_parent_state, "MERGED")

        # Without parent in watched set, stack_parent is cleared.
        resolve_stack_parents({s_child.key: s_child})
        self.assertIsNone(s_child.stack_parent)

    def test_normalize_pr_prefers_canonical_name_with_owner_or_url_over_caller_repo(
        self,
    ) -> None:
        payload = json.loads(load_fixture("pr_open_ci_running.json"))
        raw = payload["data"]["pr0"]["pullRequest"]
        snap_from_url = normalize_pr(raw, viewer="alice", repo="legacy/Widget")
        self.assertEqual(snap_from_url.repo, "acme/widget")
        self.assertEqual(snap_from_url.key, "acme/widget#3339")

        raw_with_repo = {**raw, "repository": {"nameWithOwner": "canonical/widget"}}
        snap_from_repo = normalize_pr(raw_with_repo, viewer="alice", repo="legacy/Widget")
        self.assertEqual(snap_from_repo.repo, "canonical/widget")
        self.assertEqual(snap_from_repo.key, "canonical/widget#3339")

    def test_normalize_pr_extracts_viewer_last_review_and_opinionated_state(self) -> None:
        payload = json.loads(load_fixture("pr_open_ci_running.json"))
        base_raw = payload["data"]["pr0"]["pullRequest"]

        # 1. COMMENTED-only review with commit oid and PENDING ignored
        raw_commented = {
            **base_raw,
            "latestOpinionatedReviews": {"nodes": []},
            "reviews": {
                "nodes": [
                    {
                        "state": "COMMENTED",
                        "submittedAt": "2026-10-27T20:00:00Z",
                        "commit": {"oid": "1111aaaa"},
                        "author": {"login": "other", "__typename": "User"},
                    },
                    {
                        "state": "COMMENTED",
                        "submittedAt": "2026-10-27T20:30:00Z",
                        "commit": {"oid": "2222bbbb"},
                        "author": {"login": "Bob", "__typename": "User"},
                    },
                    {
                        "state": "PENDING",
                        "submittedAt": "2026-10-27T21:00:00Z",
                        "commit": {"oid": "3333cccc"},
                        "author": {"login": "bob", "__typename": "User"},
                    },
                ]
            },
        }
        snap = normalize_pr(raw_commented, viewer="  bob ")
        self.assertEqual(
            snap.viewer_last_review,
            ViewerReview(
                state="COMMENTED",
                submitted_at="2026-10-27T20:30:00Z",
                commit_oid="2222bbbb",
            ),
        )
        self.assertIsNone(snap.viewer_opinionated_state)

        # 2. Post-approval COMMENTED keeps viewer_opinionated_state == "APPROVED"
        raw_post_approval = {
            **base_raw,
            "latestOpinionatedReviews": {
                "nodes": [
                    {
                        "state": "APPROVED",
                        "author": {"login": "BOB"},
                        "submittedAt": "2026-10-27T20:10:00Z",
                    }
                ]
            },
            "reviews": {
                "nodes": [
                    {
                        "state": "APPROVED",
                        "submittedAt": "2026-10-27T20:10:00Z",
                        "commit": {"oid": "aaaa1111"},
                        "author": {"login": "bob", "__typename": "User"},
                    },
                    {
                        "state": "COMMENTED",
                        "submittedAt": "2026-10-27T20:45:00Z",
                        "commit": {"oid": "bbbb2222"},
                        "author": {"login": "bob", "__typename": "User"},
                    },
                ]
            },
        }
        snap_approved = normalize_pr(raw_post_approval, viewer="bob")
        self.assertEqual(snap_approved.viewer_opinionated_state, "APPROVED")
        self.assertEqual(
            snap_approved.viewer_last_review,
            ViewerReview(
                state="COMMENTED",
                submitted_at="2026-10-27T20:45:00Z",
                commit_oid="bbbb2222",
            ),
        )

        # 3. Greatest submittedAt wins, ties broken by later node, and missing commit_oid -> None
        raw_ties = {
            **base_raw,
            "latestOpinionatedReviews": {
                "nodes": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "author": {"login": "bob"},
                        "submittedAt": "2026-10-27T20:50:00Z",
                    }
                ]
            },
            "reviews": {
                "nodes": [
                    {
                        "state": "COMMENTED",
                        "submittedAt": "2026-10-27T20:50:00Z",
                        "commit": {"oid": "first"},
                        "author": {"login": "bob", "__typename": "User"},
                    },
                    {
                        "state": "CHANGES_REQUESTED",
                        "submittedAt": "2026-10-27T20:50:00Z",
                        "commit": None,
                        "author": {"login": "bob", "__typename": "User"},
                    },
                    {
                        "state": "APPROVED",
                        "submittedAt": "2026-10-27T19:00:00Z",
                        "commit": {"oid": "older"},
                        "author": {"login": "bob", "__typename": "User"},
                    },
                ]
            },
        }
        snap_ties = normalize_pr(raw_ties, viewer="bob")
        self.assertEqual(snap_ties.viewer_opinionated_state, "CHANGES_REQUESTED")
        self.assertEqual(
            snap_ties.viewer_last_review,
            ViewerReview(
                state="CHANGES_REQUESTED",
                submitted_at="2026-10-27T20:50:00Z",
                commit_oid=None,
            ),
        )

        # Fallback to latestOpinionatedReviews when 20 thread-reply reviews overflow reviews(last: 20)
        raw_overflow = copy.deepcopy(raw_ties)
        raw_overflow["reviews"] = {
            "nodes": [
                {
                    "author": {"login": "alice"},
                    "state": "COMMENTED",
                    "submittedAt": f"2026-10-27T21:{i:02d}:00Z",
                    "commit": {"oid": "2222222"},
                }
                for i in range(20)
            ]
        }
        snap_overflow = normalize_pr(raw_overflow, viewer="bob")
        self.assertEqual(
            snap_overflow.viewer_last_review,
            ViewerReview(
                state="CHANGES_REQUESTED",
                submitted_at="2026-10-27T20:50:00Z",
                commit_oid=None,
            ),
        )

    def test_normalize_pr_extracts_thread_outdated_and_participants(self) -> None:
        payload = json.loads(load_fixture("pr_open_ci_running.json"))
        raw = copy.deepcopy(payload["data"]["pr0"]["pullRequest"])
        raw["reviewThreads"] = {
            "totalCount": 3,
            "nodes": [
                {
                    "id": "PRRT_long",
                    "isResolved": False,
                    "isOutdated": True,
                    "firstComment": {"nodes": [{"author": {"login": "bob", "__typename": "User"}}]},
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "carol", "__typename": "User"},
                                "createdAt": "2026-10-27T20:10:00Z",
                            },
                            {
                                "author": {"login": "Bob", "__typename": "User"},
                                "createdAt": "2026-10-27T20:20:00Z",
                            },
                            {
                                "author": {"login": "alice", "__typename": "User"},
                                "createdAt": "2026-10-27T20:30:00Z",
                            },
                        ]
                    },
                },
                {
                    "id": "PRRT_packrat_hydrated",
                    "isResolved": True,
                    "isOutdated": False,
                    "participants": ["bob", "alice", "bob", ""],
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "alice", "__typename": "User"},
                                "createdAt": "2026-10-27T20:40:00Z",
                            }
                        ]
                    },
                },
                {
                    "id": "PRRT_packrat_unhydrated",
                    "isResolved": False,
                    "participants": [],
                    "comments": {
                        "nodes": [
                            {
                                "author": {"login": "alice", "__typename": "User"},
                                "createdAt": "2026-10-27T20:50:00Z",
                            }
                        ]
                    },
                },
            ],
        }
        snap = normalize_pr(raw, viewer="bob")
        by_id = {t.id: t for t in snap.threads}

        long_t = by_id["PRRT_long"]
        self.assertTrue(long_t.outdated)
        self.assertEqual(long_t.participants, ("bob", "carol", "alice"))
        self.assertEqual(long_t.last_author, "alice")
        self.assertEqual(long_t.last_comment_at, "2026-10-27T20:30:00Z")

        hydrated_t = by_id["PRRT_packrat_hydrated"]
        self.assertFalse(hydrated_t.outdated)
        self.assertEqual(hydrated_t.participants, ("bob", "alice"))

        unhydrated_t = by_id["PRRT_packrat_unhydrated"]
        self.assertEqual(unhydrated_t.participants, ())

    def test_normalize_pr_extracts_viewer_requested_at_and_comment_watermarks(self) -> None:
        payload = json.loads(load_fixture("pr_open_ci_running.json"))
        base_raw = payload["data"]["pr0"]["pullRequest"]

        # 1. Post-review human comments + timelineItems vs reviewRequests max requestedAt
        raw_reviewed = {
            **copy.deepcopy(base_raw),
            "reviewRequests": {
                "totalCount": 1,
                "nodes": [
                    {
                        "requestedReviewer": {"login": "bob"},
                        "requestedAt": "2026-10-27T20:15:00Z",
                    }
                ],
            },
            "timelineItems": {
                "nodes": [
                    {
                        "createdAt": "2026-10-27T20:10:00Z",
                        "requestedReviewer": {"__typename": "User", "login": "bob"},
                    },
                    {
                        "createdAt": "2026-10-27T20:45:00Z",
                        "requestedReviewer": {"__typename": "User", "login": "BOB"},
                    },
                    {
                        "createdAt": "2026-10-27T21:30:00Z",
                        "requestedReviewer": {"__typename": "User", "login": "carol"},
                    },
                ]
            },
            "reviews": {
                "nodes": [
                    {
                        "state": "CHANGES_REQUESTED",
                        "submittedAt": "2026-10-27T20:30:00Z",
                        "commit": {"oid": "deadbeef"},
                        "author": {"login": "bob", "__typename": "User"},
                    }
                ]
            },
            "comments": {
                "nodes": [
                    {
                        "id": "IC_1",
                        "createdAt": "2026-10-27T20:20:00Z",
                        "author": {"login": "bob", "__typename": "User"},
                    },
                    {
                        "id": "IC_2",
                        "createdAt": "2026-10-27T20:50:00Z",
                        "author": {"login": "alice", "__typename": "User"},
                    },
                    {
                        "id": "IC_3",
                        "createdAt": "2026-10-27T21:10:00Z",
                        "author": {"login": "Bob", "__typename": "User"},
                    },
                    {
                        "id": "IC_bot",
                        "createdAt": "2026-10-27T21:55:00Z",
                        "author": {"login": "ci-helper[bot]", "__typename": "Bot"},
                    },
                ]
            },
        }
        snap = normalize_pr(raw_reviewed, viewer="bob")
        self.assertEqual(snap.viewer_requested_at, "2026-10-27T20:45:00Z")
        self.assertEqual(snap.viewer_last_comment_at, "2026-10-27T21:10:00Z")
        self.assertEqual(snap.author_last_comment_at, "2026-10-27T20:50:00Z")
        self.assertEqual(snap.discussion_watermark, "IC_3:2026-10-27T21:10:00Z")

        # 2. Comments older than last_review (with only bot comments newer) -> watermark None
        raw_no_post_review = {
            **copy.deepcopy(raw_reviewed),
            "comments": {
                "nodes": [
                    {
                        "id": "IC_old",
                        "createdAt": "2026-10-27T20:00:00Z",
                        "author": {"login": "alice", "__typename": "User"},
                    },
                    {
                        "id": "IC_bot",
                        "createdAt": "2026-10-27T21:55:00Z",
                        "author": {"login": "ci-helper", "is_bot": True, "__typename": "User"},
                    },
                ]
            },
        }
        snap_old = normalize_pr(raw_no_post_review, viewer="bob")
        self.assertIsNone(snap_old.viewer_last_comment_at)
        self.assertEqual(snap_old.author_last_comment_at, "2026-10-27T20:00:00Z")
        self.assertIsNone(snap_old.discussion_watermark)

        # 3. Directly requested PR with no formal review yet + viewer comment -> watermark set
        raw_direct_comment = {
            **copy.deepcopy(base_raw),
            "latestOpinionatedReviews": {"nodes": []},
            "reviews": {"nodes": []},
            "comments": {
                "nodes": [
                    {
                        "id": "IC_hold",
                        "createdAt": "2026-10-27T21:12:00Z",
                        "author": {"login": "bob", "__typename": "User"},
                    }
                ]
            },
        }
        snap_direct = normalize_pr(raw_direct_comment, viewer="bob")
        self.assertIsNone(snap_direct.viewer_last_review)
        self.assertEqual(snap_direct.viewer_last_comment_at, "2026-10-27T21:12:00Z")
        self.assertEqual(snap_direct.discussion_watermark, "IC_hold:2026-10-27T21:12:00Z")

        # 4. Author viewing own PR: author_last_comment_at is None and watermark is None
        snap_self = normalize_pr(raw_reviewed, viewer="alice")
        self.assertIsNone(snap_self.viewer_requested_at)
        self.assertEqual(snap_self.viewer_last_comment_at, "2026-10-27T20:50:00Z")
        self.assertIsNone(snap_self.author_last_comment_at)
        self.assertIsNone(snap_self.discussion_watermark)


if __name__ == "__main__":
    unittest.main()
