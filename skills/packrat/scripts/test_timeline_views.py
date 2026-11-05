"""Unit tests for the timeline DuckDB views in init.sql.

Builds a temporary SQLite database with PackratStore, renders init.sql, and
queries the views through the `duckdb` CLI. Skipped when `duckdb` is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from db_store import PackratStore
from models import (
    CommentRecord,
    CommitRecord,
    IssueRecord,
    PrRecord,
    ReviewRecord,
    TimelineEventRecord,
)

REPO = "octo-org/widgets"


def _at(hhmm: str) -> str:
    """Returns an ISO timestamp on a fixed day for an `HH:MM` time."""
    return f"2026-03-02T{hhmm}:00Z"


@unittest.skipUnless(shutil.which("duckdb"), "duckdb CLI not installed")
class TimelineViewsTest(unittest.TestCase):
    """Checks the timeline views: reviews, PR stream, lifecycle, request latency, and links."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp_dir = Path(tempfile.mkdtemp(prefix="packrat-timeline-views-"))
        store = PackratStore(cls.tmp_dir / "repos.sqlite")
        try:
            repo_id = store.register_repo("octo-org", "widgets").repo_id
            _seed(store, repo_id)
            cls.init_file = store.write_init_sql(cls.tmp_dir / "init.sql")
        finally:
            store.close()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def _query(self, query: str) -> list[dict[str, Any]]:
        """Executes a DuckDB SQL query with init.sql and returns parsed JSON rows."""
        res = subprocess.run(
            [
                "duckdb",
                "-batch",
                "-no-stdin",
                "-dark-mode",
                "-json",
                "-init",
                str(self.init_file),
                "-c",
                query,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = res.stdout.strip()
        return json.loads(out) if out else []

    def _lifecycle(self) -> dict[int, dict[str, Any]]:
        rows = self._query("SELECT * FROM v_pr_lifecycle ORDER BY pr_number;")
        return {row["pr_number"]: row for row in rows}

    def test_reviews_deduplicated_by_review_id(self) -> None:
        rows = self._query(
            "SELECT review_id, author_type, in_timeline, in_reviews FROM v_pr_reviews_all "
            "WHERE review_id IN ('R_carol', 'R_helper') ORDER BY review_id;"
        )
        self.assertEqual(
            rows,
            [
                {
                    "review_id": "R_carol",
                    "author_type": "User",
                    "in_timeline": True,
                    "in_reviews": True,
                },
                {
                    "review_id": "R_helper",
                    "author_type": "Bot",
                    "in_timeline": True,
                    "in_reviews": True,
                },
            ],
        )

    def test_pending_reviews_excluded(self) -> None:
        rows = self._query(
            "SELECT review_id FROM v_pr_reviews_all WHERE review_id LIKE 'R_pending%';"
        )
        self.assertEqual(rows, [])

    def test_human_flag_ignores_bots_and_pr_author(self) -> None:
        rows = self._query(
            "SELECT review_id, is_bot, is_human FROM v_pr_reviews_all WHERE pr_number = 3 "
            "ORDER BY review_id;"
        )
        flags = {row["review_id"]: (row["is_bot"], row["is_human"]) for row in rows}
        self.assertEqual(
            flags,
            {
                "R_bob3": (False, True),
                "R_ci": (True, False),
                "R_helper": (True, False),
                "R_self": (False, False),
            },
        )

    def test_reviewed_before_request(self) -> None:
        lifecycle = self._lifecycle()
        self.assertTrue(lifecycle[1]["reviewed_before_request"])  # review predates request
        self.assertTrue(lifecycle[2]["reviewed_before_request"])  # no request at all
        self.assertFalse(lifecycle[3]["reviewed_before_request"])  # bot/self reviews ignored
        self.assertFalse(lifecycle[4]["reviewed_before_request"])  # only bot and pending reviews
        self.assertIsNone(lifecycle[4]["first_review_at"])

    def test_lifecycle_pr1_latencies_and_merge(self) -> None:
        pr1 = self._lifecycle()[1]
        self.assertEqual(pr1["first_ready_at"], "2026-03-02 10:00:00")
        self.assertEqual(pr1["first_review_at"], "2026-03-02 10:30:00")
        self.assertEqual(pr1["first_approval_at"], "2026-03-02 12:00:00")
        self.assertEqual(pr1["first_review_requested_at"], "2026-03-02 11:00:00")
        self.assertEqual(pr1["merged_by"], "maintainer")
        self.assertEqual(pr1["force_pushes_after_first_review"], 1)
        self.assertEqual(pr1["draft_cycles"], 0)
        self.assertEqual(pr1["ready_to_first_review_min"], 30)
        self.assertEqual(pr1["request_to_first_review_min"], -30)
        self.assertEqual(pr1["ready_to_merge_min"], 180)

    def test_lifecycle_draft_pr(self) -> None:
        pr2 = self._lifecycle()[2]
        self.assertEqual(pr2["first_ready_at"], "2026-03-02 09:00:00")
        self.assertEqual(pr2["draft_cycles"], 1)
        self.assertEqual(pr2["ready_to_first_review_min"], 45)
        self.assertIsNone(pr2["merged_by"])
        self.assertIsNone(pr2["ready_to_merge_min"])

    def test_lifecycle_counts_are_bigint(self) -> None:
        rows = self._query(
            "SELECT DISTINCT typeof(draft_cycles) AS d, "
            "typeof(force_pushes_after_first_review) AS f, "
            "typeof(ready_to_merge_min) AS m FROM v_pr_lifecycle;"
        )
        self.assertEqual(rows, [{"d": "BIGINT", "f": "BIGINT", "m": "BIGINT"}])

    def test_pr_timeline_stream(self) -> None:
        rows = self._query(
            "SELECT event_type, actor, subject FROM v_pr_timeline WHERE pr_number = 1 "
            "ORDER BY \"at\", event_type, actor;"
        )
        reviews = [r for r in rows if r["event_type"] == "PullRequestReview"]
        self.assertEqual(
            [(r["actor"], r["subject"]) for r in reviews],
            [("bob", "COMMENTED"), ("carol", "APPROVED")],
        )
        types = {r["event_type"] for r in rows}
        self.assertIn("IssueComment", types)
        self.assertIn("MergedEvent", types)
        self.assertIn("HeadRefForcePushedEvent", types)

    def test_review_request_latency(self) -> None:
        rows = self._query(
            "SELECT reviewer, reviewer_type, requested_by, removed_at, first_review_at, "
            "latency_min "
            "FROM v_review_request_latency WHERE pr_number = 1 ORDER BY reviewer;"
        )
        by_reviewer = {row["reviewer"]: row for row in rows}
        self.assertEqual(set(by_reviewer), {"carol", "erin", "octo-org/core"})
        carol = by_reviewer["carol"]
        self.assertEqual(carol["requested_by"], "alice")
        self.assertEqual(carol["first_review_at"], "2026-03-02 12:00:00")
        self.assertEqual(carol["latency_min"], 60)
        self.assertIsNone(carol["removed_at"])
        erin = by_reviewer["erin"]
        self.assertEqual(erin["removed_at"], "2026-03-02 11:10:00")
        self.assertIsNone(erin["first_review_at"])
        team = by_reviewer["octo-org/core"]
        self.assertEqual(team["reviewer_type"], "Team")
        self.assertIsNone(team["first_review_at"])
        self.assertIsNone(team["latency_min"])

    def test_issue_pr_links_precedence_and_filtering(self) -> None:
        rows = self._query(
            "SELECT issue_number, pr_number, link_type FROM v_issue_pr_links "
            "ORDER BY issue_number, pr_number;"
        )
        self.assertEqual(
            [(r["issue_number"], r["pr_number"], r["link_type"]) for r in rows],
            [
                (10, 1, "closed_by"),  # commit closer beats a text reference
                (11, 2, "xref"),
                (12, 2, "closes"),
            ],
        )


def _seed(store: PackratStore, repo_id: int) -> None:
    """Writes the PRs, reviews, timeline events, commits, and links the tests read."""
    store.upsert_pull_requests(_prs(repo_id))
    store.upsert_issues(
        [
            IssueRecord(
                repo_id=repo_id,
                number=n,
                title=f"Issue {n}",
                state="CLOSED",
                updated_at=_at("14:00"),
            )
            for n in (10, 11, 12)
        ]
    )
    store.upsert_commits(
        [
            CommitRecord(
                repo_id=repo_id,
                sha="a" * 40,
                committed_at=_at("13:00"),
                title="Fix widgets (#1)",
                pr_number=1,
            )
        ]
    )
    store.upsert_timeline_events(_events(repo_id), repo_full_name=REPO)
    with store.conn:
        store.conn.executemany(
            "INSERT OR IGNORE INTO issue_pr_links (repo_id, issue_number, pr_number, link_type) "
            "VALUES (?, ?, ?, ?)",
            [
                (repo_id, 10, 1, "references"),
                (repo_id, 11, 2, "references"),
                (repo_id, 11, 2, "xref"),
                (repo_id, 12, 2, "xref"),
                (repo_id, 12, 2, "closes"),
                (repo_id, 3, 1, "references"),  # PR-to-PR pair, dropped by the view
            ],
        )


def _review(
    repo_id: int, pr: int, review_id: str, author: str, state: str, hhmm: str, **kw: Any
) -> ReviewRecord:
    return ReviewRecord(
        repo_id=repo_id,
        review_id=review_id,
        pr_number=pr,
        state=state,
        author=author,
        submitted_at=_at(hhmm),
        **kw,
    )


def _prs(repo_id: int) -> list[PrRecord]:
    def pr(number: int, **kw: Any) -> PrRecord:
        return PrRecord(
            repo_id=repo_id,
            number=number,
            title=f"PR {number}",
            state=kw.pop("state", "OPEN"),
            updated_at=_at("14:00"),
            author="alice",
            **kw,
        )

    return [
        pr(
            1,
            state="MERGED",
            created_at=_at("10:00"),
            merged_at=_at("13:00"),
            reviews=(
                _review(repo_id, 1, "R_bob1", "bob", "COMMENTED", "10:30"),
                _review(repo_id, 1, "R_carol", "carol", "APPROVED", "12:00"),
                _review(repo_id, 1, "R_pending1", "dave", "PENDING", "10:10"),
            ),
            comments=(
                CommentRecord(
                    repo_id=repo_id,
                    comment_id="C_1",
                    parent_kind="pr",
                    parent_number=1,
                    body="Looks close.",
                    author="bob",
                    created_at=_at("10:20"),
                ),
            ),
        ),
        pr(2, created_at=_at("08:00")),
        pr(
            3,
            created_at=_at("09:00"),
            reviews=(
                # author_type is unknown here; the timeline row says Bot.
                _review(repo_id, 3, "R_helper", "helperbot", "APPROVED", "10:00"),
                _review(repo_id, 3, "R_ci", "ci[bot]", "COMMENTED", "10:05"),
                _review(repo_id, 3, "R_self", "Alice", "COMMENTED", "10:10"),
                _review(repo_id, 3, "R_bob3", "bob", "APPROVED", "12:00"),
            ),
        ),
        pr(
            4,
            created_at=_at("09:00"),
            reviews=(
                _review(repo_id, 4, "R_bot4", "lintbot", "COMMENTED", "10:00", author_type="Bot"),
                _review(repo_id, 4, "R_pending4", "bob", "PENDING", "10:30"),
            ),
        ),
    ]


def _event(
    repo_id: int,
    event_id: str,
    number: int,
    event_type: str,
    hhmm: str,
    actor: str | None = None,
    **kw: Any,
) -> TimelineEventRecord:
    return TimelineEventRecord(
        event_id=event_id,
        repo_id=repo_id,
        item_kind=kw.pop("item_kind", "pr"),
        item_number=number,
        event_type=event_type,
        created_at=_at(hhmm),
        actor=actor,
        actor_type="User" if actor else None,
        **kw,
    )


def _review_event(
    repo_id: int,
    review_id: str,
    number: int,
    author: str,
    state: str,
    hhmm: str,
    author_type: str,
) -> TimelineEventRecord:
    payload = json.dumps({"state": state, "submittedAt": _at(hhmm), "createdAt": _at(hhmm)})
    return TimelineEventRecord(
        event_id=review_id,
        repo_id=repo_id,
        item_kind="pr",
        item_number=number,
        event_type="PullRequestReview",
        created_at=_at(hhmm),
        actor=author,
        actor_type=author_type,
        subject=state,
        payload_json=payload,
    )


def _events(repo_id: int) -> list[TimelineEventRecord]:
    e = repo_id
    user = {"subject_type": "User"}
    team = {"subject": "octo-org/core", "subject_type": "Team"}
    merge_commit = {"subject": "b" * 40, "subject_type": "Commit"}
    closing_commit = {"item_kind": "issue", "subject": "a" * 40, "subject_type": "Commit"}
    return [
        # PR 1: reviewed by bob (10:30) before the first request (11:00).
        _event(e, "E1", 1, "HeadRefForcePushedEvent", "10:15", "alice"),
        _event(e, "E2", 1, "ReviewRequestedEvent", "11:00", "alice", subject="carol", **user),
        _event(e, "E3", 1, "ReviewRequestedEvent", "11:00", "alice", **team),
        _event(e, "E4", 1, "ReviewRequestedEvent", "11:05", "alice", subject="erin", **user),
        _event(e, "E5", 1, "ReviewRequestRemovedEvent", "11:10", "alice", subject="erin", **user),
        _event(e, "E6", 1, "HeadRefForcePushedEvent", "11:30", "alice"),
        _event(e, "E7", 1, "MergedEvent", "13:00", "maintainer", **merge_commit),
        _review_event(e, "R_carol", 1, "carol", "APPROVED", "12:00", "User"),
        # PR 2: opened as a draft, readied, converted back, readied again; no request.
        _event(e, "E8", 2, "ReadyForReviewEvent", "09:00", "alice"),
        _event(e, "E9", 2, "ConvertToDraftEvent", "09:30", "alice"),
        _event(e, "E10", 2, "ReadyForReviewEvent", "10:00", "alice"),
        _review_event(e, "R_bob2", 2, "bob", "COMMENTED", "09:45", "User"),
        # PR 3: bot and self reviews come first; the human review follows the request.
        _review_event(e, "R_helper", 3, "helperbot", "APPROVED", "10:00", "Bot"),
        _event(e, "E11", 3, "ReviewRequestedEvent", "11:00", "alice", subject="bob", **user),
        # Issue 10 closed by a commit that belongs to PR 1.
        _event(e, "E12", 10, "ClosedEvent", "13:05", "maintainer", **closing_commit),
    ]


if __name__ == "__main__":
    unittest.main()
