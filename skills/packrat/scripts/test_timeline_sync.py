"""Tests for the timeline stream (timeline_sync.py)."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import time
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from db_store import PackratStore
from gh_transport import GhCommandResult
from models import (
    IssueRecord,
    PrRecord,
    TimelineMode,
    TimelineTarget,
    iso_at,
)
from progress import NullReporter
from rate_budget import Priority, RateBudget
from timeline_sync import (
    ISSUE_TIMELINE_FRAGMENT,
    ISSUE_TIMELINE_TYPES,
    MAX_EXTRA_PAGES,
    PR_TIMELINE_FRAGMENT,
    PR_TIMELINE_TYPES,
    XREF_TYPES,
    build_timeline_page_query,
    build_timeline_query,
    parse_timeline_items,
    sync_repo_timelines,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
REPO = "octo-org/widgets"
PR_NODE = "PR_kwDOAwidgets7"
ISSUE_NODE = "I_kwDOAwidgets3"
PAGE1_CURSOR = "Y3Vyc29yOnBhZ2Ux"


def _fixture(name: str) -> dict[str, Any]:
    """Loads a JSON fixture from fixtures/."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _type_name(enum_value: str) -> str:
    """Maps a timeline item enum such as READY_FOR_REVIEW_EVENT to ReadyForReviewEvent."""
    return "".join(part.capitalize() for part in enum_value.split("_"))


def _nodes(alias: str, fixture: str = "timeline_batch.json") -> list[dict[str, Any]]:
    """Returns the timeline nodes of one fixture alias."""
    return _fixture(fixture)["data"][alias]["timelineItems"]["nodes"]


def _include_output(body: dict[str, Any], remaining: int) -> str:
    """Renders a `gh api -i` response with graphql `X-RateLimit-*` headers."""
    reset = int(time.time()) + 3600
    return (
        "HTTP/2.0 200 OK\r\n"
        "X-RateLimit-Resource: graphql\r\n"
        "X-RateLimit-Limit: 5000\r\n"
        f"X-RateLimit-Remaining: {remaining}\r\n"
        f"X-RateLimit-Reset: {reset}\r\n"
        "\r\n" + json.dumps(body)
    )


PR_TARGET = TimelineTarget(1, "pr", 7, PR_NODE, None)
ISSUE_TARGET = TimelineTarget(1, "issue", 3, ISSUE_NODE, None)


class FakeGh:
    """Answers timeline queries from fixtures, matching aliases by node id.

    Unknown node ids answer with a null node and a GraphQL error of type
    `error_types.get(node_id, "NOT_FOUND")`. Follow-up page queries (those
    with `after:`) answer from timeline_page.json.
    """

    def __init__(self) -> None:
        batch = _fixture("timeline_batch.json")["data"]
        self.cost = int(batch["rateLimit"]["cost"])
        self.nodes = {
            n["id"]: n for n in batch.values() if isinstance(n, dict) and "id" in n
        }
        self.page = _fixture("timeline_page.json")["data"]["t0"]
        self.queries: list[str] = []
        self.remaining = 4500
        self.error_types: dict[str, str] = {}
        self.fail_batches: GhCommandResult | None = None
        self.fail_pages: GhCommandResult | None = None
        self.endless_pages = False

    @property
    def batch_queries(self) -> list[str]:
        return [q for q in self.queries if "after:" not in q]

    @property
    def page_queries(self) -> list[str]:
        return [q for q in self.queries if "after:" in q]

    def __call__(self, args: Sequence[str], timeout: int) -> GhCommandResult:
        del timeout
        assert list(args[:3]) == ["api", "graphql", "-i"], args
        query = next(a for a in args if a.startswith("query="))[len("query=") :]
        self.queries.append(query)
        is_page = "after:" in query
        failure = self.fail_pages if is_page else self.fail_batches
        if failure is not None:
            return failure
        body = self._page(query) if is_page else self._batch(query)
        return GhCommandResult(0, _include_output(body, self.remaining), "")

    def _aliases(self, query: str) -> list[tuple[str, str]]:
        return re.findall(r'(t\d+): node\(id: "([^"]+)"\)', query)

    def _batch(self, query: str) -> dict[str, Any]:
        data: dict[str, Any] = {"rateLimit": {"cost": self.cost}}
        errors = []
        for alias, node_id in self._aliases(query):
            node = self.nodes.get(node_id)
            data[alias] = copy.deepcopy(node)
            if node is None:
                kind = self.error_types.get(node_id, "NOT_FOUND")
                errors.append(
                    {"type": kind, "path": [alias], "message": f"no node {node_id}"}
                )
        return {"data": data, "errors": errors} if errors else {"data": data}

    def _page(self, query: str) -> dict[str, Any]:
        node = copy.deepcopy(self.page)
        if self.endless_pages:
            node["timelineItems"]["pageInfo"] = {
                "hasNextPage": True,
                "endCursor": "more",
            }
        return {"data": {"rateLimit": {"cost": 1}, "t0": node}}


class RecordingReporter(NullReporter):
    """Collects warnings."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warn(self, repo: str, message: str) -> None:
        self.warnings.append(f"{repo}: {message}")


class TestQueryBuilders(unittest.TestCase):
    """Tests the batched and follow-up GraphQL documents."""

    def test_batch_aliases_kinds_and_since(self) -> None:
        since = "2027-01-01T00:00:00Z"
        query = build_timeline_query(
            [TimelineTarget(1, "pr", 7, PR_NODE, since), ISSUE_TARGET], xref_only=False
        )
        self.assertIn(f't0: node(id: "{PR_NODE}")', query)
        self.assertIn(f't1: node(id: "{ISSUE_NODE}")', query)
        self.assertIn("rateLimit { cost remaining resetAt }", query)
        pr_block, issue_block = query.split("t1: node", 1)
        self.assertIn("... on PullRequest", pr_block)
        self.assertIn(f'since: "{since}"', pr_block)
        self.assertIn("...PrTimelineItem", pr_block)
        self.assertIn("... on Issue {", issue_block.split("fragment")[0])
        self.assertNotIn("since:", issue_block)
        self.assertIn("first: 100", pr_block)
        self.assertIn(f"itemTypes: [{', '.join(PR_TIMELINE_TYPES)}]", pr_block)
        self.assertIn(f"itemTypes: [{', '.join(ISSUE_TIMELINE_TYPES)}]", issue_block)
        self.assertIn("fragment PrTimelineItem on PullRequestTimelineItems", query)
        self.assertIn("fragment IssueTimelineItem on IssueTimelineItems", query)

    def test_unused_fragments_are_left_out(self) -> None:
        # GraphQL rejects documents that define a fragment nobody spreads.
        query = build_timeline_query([PR_TARGET], xref_only=False)
        self.assertNotIn("IssueTimelineItem", query)
        query = build_timeline_query([ISSUE_TARGET], xref_only=False)
        self.assertNotIn("PrTimelineItem", query)
        self.assertIn("fragment Actor on Actor", query)
        self.assertIn("fragment LinkRef on ReferencedSubject", query)

    def test_xref_only_uses_xref_types(self) -> None:
        query = build_timeline_query([PR_TARGET, ISSUE_TARGET], xref_only=True)
        self.assertEqual(query.count(f"itemTypes: [{', '.join(XREF_TYPES)}]"), 2)
        self.assertNotIn("LABELED_EVENT", query.split("fragment")[0])

    def test_empty_and_unknown_kind_raise(self) -> None:
        with self.assertRaises(ValueError):
            build_timeline_query([], xref_only=False)
        with self.assertRaises(ValueError):
            build_timeline_query(
                [TimelineTarget(1, "discussion", 1, "D_1", None)], xref_only=False
            )

    def test_page_query_has_cursor_and_single_alias(self) -> None:
        query = build_timeline_page_query(PR_TARGET, "abc", xref_only=True)
        self.assertIn(f't0: node(id: "{PR_NODE}")', query)
        self.assertIn('after: "abc"', query)
        self.assertNotIn("t1:", query)
        self.assertIn(f"itemTypes: [{', '.join(XREF_TYPES)}]", query)

    def test_every_requested_type_has_an_inline_fragment(self) -> None:
        for enum_value in PR_TIMELINE_TYPES:
            self.assertIn(f"... on {_type_name(enum_value)} {{", PR_TIMELINE_FRAGMENT)
        for enum_value in ISSUE_TIMELINE_TYPES:
            self.assertIn(
                f"... on {_type_name(enum_value)} {{", ISSUE_TIMELINE_FRAGMENT
            )


class TestParser(unittest.TestCase):
    """Tests per-family mapping of timeline nodes to records."""

    def setUp(self) -> None:
        pr_nodes = _nodes("t0") + _nodes("t0", "timeline_page.json")
        self.pr = {
            e.event_id: e
            for e in parse_timeline_items(PR_TARGET, pr_nodes, repo_full_name=REPO)
        }
        issue_nodes = _nodes("t1")
        self.issue = {
            e.event_id: e
            for e in parse_timeline_items(
                ISSUE_TARGET, issue_nodes, repo_full_name=REPO
            )
        }

    def _subject(self, events: dict[str, Any], event_id: str) -> tuple[Any, Any]:
        e = events[event_id]
        return e.subject, e.subject_type

    def test_fixtures_cover_every_type_and_all_nodes_parse(self) -> None:
        pr_types = {e.event_type for e in self.pr.values()}
        issue_types = {e.event_type for e in self.issue.values()}
        self.assertEqual(pr_types, {_type_name(t) for t in PR_TIMELINE_TYPES})
        self.assertEqual(issue_types, {_type_name(t) for t in ISSUE_TIMELINE_TYPES})
        self.assertEqual(
            len(self.pr), len(_nodes("t0")) + len(_nodes("t0", "timeline_page.json"))
        )
        self.assertEqual(len(self.issue), len(_nodes("t1")))
        sample = self.issue["LE_is1"]
        self.assertEqual(
            (sample.repo_id, sample.item_kind, sample.item_number), (1, "issue", 3)
        )

    def test_reviews(self) -> None:
        bot = self.pr["PRR_bot1"]
        self.assertEqual((bot.actor, bot.actor_type), ("review-helper", "Bot"))
        self.assertEqual(bot.created_at, "2027-01-10T09:01:30Z")
        self.assertEqual((bot.subject, bot.subject_type), ("COMMENTED", None))
        self.assertEqual(json.loads(bot.payload_json)["state"], "COMMENTED")
        pending = self.pr["PRR_pending"]
        self.assertEqual(
            (pending.subject, pending.created_at), ("PENDING", "2027-01-10T10:12:00Z")
        )

    def test_review_requests_and_dismissal(self) -> None:
        self.assertEqual(self._subject(self.pr, "RRE_user"), ("bob-reviewer", "User"))
        self.assertEqual(
            self._subject(self.pr, "RRE_team"), ("octo-org/core-team", "Team")
        )
        self.assertEqual(
            self._subject(self.pr, "RRRE_team"), ("octo-org/core-team", "Team")
        )
        self.assertEqual(self.pr["RRE_user"].actor, "alice-dev")
        dismissed = self.pr["RDE_1"]
        self.assertEqual(
            (dismissed.subject, dismissed.subject_type), ("bob-reviewer", "User")
        )
        self.assertEqual(
            json.loads(dismissed.payload_json),
            {"dismissalMessage": "Addressed in a later push"},
        )

    def test_commit_events(self) -> None:
        self.assertEqual(self._subject(self.pr, "HRFPE_1"), ("2" * 40, "Commit"))
        self.assertEqual(
            json.loads(self.pr["HRFPE_1"].payload_json)["beforeOid"], "1" * 40
        )
        merged = self.pr["MGE_1"]
        self.assertEqual(
            (merged.actor, merged.subject, merged.subject_type),
            ("carol-lead", "4" * 40, "Commit"),
        )
        self.assertEqual(self._subject(self.pr, "REFE_pr1"), ("3" * 40, "Commit"))
        self.assertEqual(self._subject(self.issue, "CLE_is2"), ("5" * 40, "Commit"))

    def test_linked_items(self) -> None:
        self.assertEqual(
            self._subject(self.pr, "CE_pr1"), ("octo-org/widgets#3", "Issue")
        )
        self.assertEqual(
            self._subject(self.pr, "DE_pr1"), ("octo-org/widgets#4", "Issue")
        )
        self.assertEqual(
            self._subject(self.pr, "CRE_pr1"), ("octo-org/gadgets#11", "Issue")
        )
        self.assertEqual(
            json.loads(self.pr["CRE_pr1"].payload_json),
            {"isCrossRepository": True, "willCloseTarget": False},
        )
        self.assertEqual(
            self._subject(self.issue, "CLE_is1"), ("octo-org/widgets#7", "PullRequest")
        )
        self.assertEqual(
            json.loads(self.issue["CLE_is1"].payload_json), {"stateReason": "COMPLETED"}
        )
        self.assertEqual(self._subject(self.pr, "CLE_pr1"), (None, None))
        self.assertEqual(
            self._subject(self.issue, "MADE_is1"), ("octo-org/widgets#2", "Issue")
        )
        self.assertEqual(
            json.loads(self.issue["MADE_is1"].payload_json),
            {"duplicate": "octo-org/widgets#3"},
        )

    def test_refs_labels_assignees_milestones(self) -> None:
        self.assertEqual(self._subject(self.pr, "BRCE_1"), ("main", "Ref"))
        self.assertEqual(
            json.loads(self.pr["ABCFE_1"].payload_json)["oldBase"], "feature-b"
        )
        self.assertEqual(self._subject(self.pr, "HRDE_1"), ("alice/sorting", "Ref"))
        self.assertEqual(self._subject(self.pr, "LE_pr1"), ("needs review", "Label"))
        self.assertEqual(self.pr["LE_pr1"].actor_type, "Bot")
        self.assertEqual(self._subject(self.pr, "UAE_pr1"), ("triage-bot", "Bot"))
        self.assertEqual(
            self._subject(self.issue, "UAE_is1"), ("old-account", "Mannequin")
        )
        self.assertEqual(self._subject(self.issue, "DME_is1"), ("v2.0", "Milestone"))
        self.assertEqual(
            self._subject(self.issue, "TE_is1"), ("octo-org/gadgets", "Repository")
        )
        self.assertEqual(
            json.loads(self.pr["RFMQE_1"].payload_json), {"reason": "CI_FAILED"}
        )

    def test_untrusted_title_text_stays_in_payload(self) -> None:
        renamed = self.issue["RTE_is1"]
        self.assertEqual((renamed.subject, renamed.subject_type), (None, None))
        self.assertEqual(
            json.loads(renamed.payload_json)["currentTitle"], "Widget sort is broken"
        )
        self.assertEqual(self.pr["RFRE_1"].payload_json, "{}")

    def test_unknown_type_keeps_raw_node_and_bad_nodes_are_skipped(self) -> None:
        unknown = {
            "__typename": "PinnedEvent",
            "id": "PE_1",
            "createdAt": "2027-01-01T00:00:00Z",
            "actor": None,
        }
        nodes: list[Any] = [
            unknown,
            None,
            {"__typename": "LabeledEvent", "createdAt": "2027-01-01T00:00:00Z"},
        ]
        events = parse_timeline_items(ISSUE_TARGET, nodes, repo_full_name=REPO)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].subject, events[0].actor), (None, None))
        self.assertEqual(json.loads(events[0].payload_json), unknown)


class TestSyncRepoTimelines(unittest.TestCase):
    """Tests the runner against a fake `gh` and a temporary store."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = PackratStore(Path(self.tmp.name) / "repos.sqlite")
        self.repo = self.store.register_repo("octo-org", "widgets")
        self.rid = self.repo.repo_id
        self.budget = RateBudget(Path(self.tmp.name) / "rate_budget.json")
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    self.rid,
                    7,
                    "Add widget sorting",
                    "MERGED",
                    "2027-01-10T10:32:00Z",
                    node_id=PR_NODE,
                )
            ]
        )
        self.store.upsert_issues(
            [
                IssueRecord(
                    self.rid,
                    3,
                    "Widget sort is broken",
                    "CLOSED",
                    "2027-01-10T12:00:00Z",
                    node_id=ISSUE_NODE,
                )
            ]
        )
        self.gh = FakeGh()
        self.reporter = RecordingReporter()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _sync(self, mode: TimelineMode = TimelineMode.ALL, **kwargs: Any) -> Any:
        options: dict[str, Any] = {
            "runner": self.gh,
            "budget": self.budget,
            "max_batches": 10,
            "drain_xrefs": False,
            "reporter": self.reporter,
        }
        options.update(kwargs)
        return sync_repo_timelines(self.store, self.repo, mode=mode, **options)

    def _synced(self, table: str, number: int) -> tuple[str | None, str | None]:
        row = self.store.conn.execute(
            f"SELECT timeline_synced_at, xref_synced_at FROM {table} WHERE repo_id = ? AND number = ?",
            (self.rid, number),
        ).fetchone()
        return row[0], row[1]

    def _set_synced(
        self, table: str, number: int, synced: str | None, xref: str | None = None
    ) -> None:
        with self.store.conn:
            self.store.conn.execute(
                f"UPDATE {table} SET timeline_synced_at = ?, xref_synced_at = ? WHERE repo_id = ? AND number = ?",
                (synced, xref, self.rid, number),
            )

    def _event_count(self) -> int:
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM timeline_events"
        ).fetchone()[0]

    def _links(self) -> set[tuple[int, int, str]]:
        rows = self.store.conn.execute(
            "SELECT issue_number, pr_number, link_type FROM issue_pr_links WHERE repo_id = ?",
            (self.rid,),
        ).fetchall()
        return {(r[0], r[1], r[2]) for r in rows}

    def test_backfill_pages_writes_events_links_and_marks_synced(self) -> None:
        before = iso_at()
        stats = self._sync(TimelineMode.BACKFILL)

        expected = (
            len(_nodes("t0"))
            + len(_nodes("t1"))
            + len(_nodes("t0", "timeline_page.json"))
        )
        self.assertEqual(stats.errors, [])
        self.assertEqual((stats.items_synced, stats.events_written), (2, expected))
        self.assertEqual(self._event_count(), expected)
        self.assertEqual(stats.graphql_cost, self.gh.cost + 1)
        self.assertEqual(stats.pending_backfill, 0)
        self.assertEqual(len(self.gh.batch_queries), 1)
        self.assertEqual(len(self.gh.page_queries), 1)
        self.assertIn(f'after: "{PAGE1_CURSOR}"', self.gh.page_queries[0])
        for table, number in (("pull_requests", 7), ("issues", 3)):
            synced, xref = self._synced(table, number)
            self.assertIsNotNone(synced)
            self.assertEqual(synced, xref)
            self.assertGreaterEqual(synced, before)
        self.assertEqual(
            self._links(),
            {
                (3, 7, "closed_by"),
                (3, 7, "xref"),
                (3, 8, "xref"),
                (3, 9, "xref"),
                (4, 7, "xref"),
            },
        )
        snap = self.budget.current("graphql")
        assert snap is not None
        self.assertEqual(snap.remaining, 4500)

    def test_incremental_and_backfill_select_different_items(self) -> None:
        self._set_synced("pull_requests", 7, "2027-01-10T10:00:00Z")
        stats = self._sync(TimelineMode.INCREMENTAL)
        self.assertEqual(stats.items_synced, 1)
        self.assertEqual(len(self.gh.batch_queries), 1)
        query = self.gh.batch_queries[0]
        self.assertIn(PR_NODE, query)
        self.assertNotIn(ISSUE_NODE, query)
        self.assertIn('since: "2027-01-10T09:55:00Z"', query)
        self.assertEqual(self._synced("issues", 3), (None, None))
        self.assertEqual(stats.pending_backfill, 1)

        self.gh.queries.clear()
        stats = self._sync(TimelineMode.BACKFILL)
        self.assertEqual(stats.items_synced, 1)
        self.assertIn(ISSUE_NODE, self.gh.batch_queries[0])
        self.assertNotIn(PR_NODE, self.gh.batch_queries[0])
        self.assertNotIn("since:", self.gh.batch_queries[0])

    def test_failed_follow_up_page_leaves_item_unsynced(self) -> None:
        self.gh.fail_pages = GhCommandResult(1, "", "HTTP 502: Bad Gateway")
        stats = self._sync(TimelineMode.BACKFILL)
        # Candidate order puts issue 3 (updated later) first; it has a single page.
        self.assertIsNotNone(self._synced("issues", 3)[0])
        self.assertEqual(self._synced("pull_requests", 7), (None, None))
        self.assertEqual(stats.items_synced, 1)
        self.assertTrue(any("http_error" in e for e in stats.errors), stats.errors)
        self.assertFalse(stats.deferred)
        # The PR's first page was still written; upserts make the refetch idempotent.
        self.assertEqual(self._event_count(), len(_nodes("t0")) + len(_nodes("t1")))

    def test_page_cap_marks_synced_and_reports_item(self) -> None:
        self.gh.endless_pages = True
        stats = self._sync(TimelineMode.BACKFILL)
        self.assertEqual(len(self.gh.page_queries), MAX_EXTRA_PAGES)
        self.assertEqual(stats.capped_items, ["octo-org/widgets#7"])
        self.assertIsNotNone(self._synced("pull_requests", 7)[0])
        self.assertEqual(len(self.reporter.warnings), 1)
        self.assertIn("octo-org/widgets#7", self.reporter.warnings[0])

    def test_budget_denial_defers_before_any_call(self) -> None:
        self.budget.record(
            {
                "x-ratelimit-resource": "graphql",
                "x-ratelimit-limit": "5000",
                "x-ratelimit-remaining": "750",
                "x-ratelimit-reset": str(int(time.time()) + 3600),
            }
        )
        stats = self._sync(TimelineMode.BACKFILL)
        self.assertTrue(stats.deferred)
        self.assertEqual(self.gh.queries, [])
        self.assertEqual(stats.items_synced, 0)
        # 15% remaining: BACKFILL (the default for BACKFILL mode) is denied, VIEWER is not.
        stats = self._sync(TimelineMode.BACKFILL, priority=Priority.VIEWER)
        self.assertFalse(stats.deferred)
        self.assertEqual(stats.items_synced, 2)

    def test_rate_limit_response_stops_run(self) -> None:
        self.gh.fail_batches = GhCommandResult(
            1, "", "gh: API rate limit exceeded for user"
        )
        stats = self._sync(TimelineMode.ALL, drain_xrefs=True)
        self.assertTrue(stats.deferred)
        self.assertEqual(len(self.gh.queries), 1)
        self.assertTrue(
            stats.errors and "rate_limited" in stats.errors[0], stats.errors
        )
        self.assertEqual(self._synced("pull_requests", 7), (None, None))

    def test_null_nodes_marked_synced_unless_error_is_retryable(self) -> None:
        self.store.upsert_issues(
            [
                IssueRecord(
                    self.rid,
                    5,
                    "Deleted",
                    "OPEN",
                    "2027-01-11T00:00:00Z",
                    node_id="I_gone",
                ),
                IssueRecord(
                    self.rid,
                    6,
                    "Flaky",
                    "OPEN",
                    "2027-01-11T00:00:00Z",
                    node_id="I_flaky",
                ),
            ]
        )
        self.gh.error_types["I_flaky"] = "INTERNAL"
        stats = self._sync(TimelineMode.BACKFILL)
        self.assertIsNotNone(self._synced("issues", 5)[0])
        self.assertEqual(self._synced("issues", 6), (None, None))
        self.assertEqual(stats.items_synced, 3)
        self.assertTrue(
            any("octo-org/widgets#6" in e for e in stats.errors), stats.errors
        )
        self.assertEqual(stats.pending_backfill, 1)

    def test_xref_queue_drain_sets_only_xref_synced_at(self) -> None:
        # Both items are fully synced and newer than their updated_at, so only the
        # queued mention makes issue 3 a target.
        now = time.time()
        synced = iso_at(now - 3600)
        with self.store.conn:
            for table in ("pull_requests", "issues"):
                self.store.conn.execute(
                    f"UPDATE {table} SET updated_at = ?", (iso_at(now - 7200),)
                )
        self._set_synced("pull_requests", 7, synced, synced)
        self._set_synced("issues", 3, synced, synced)
        self.store.enqueue_timeline_dirty(
            [(self.rid, 3)], reason="mention", now=now - 60
        )

        stats = self._sync(TimelineMode.INCREMENTAL, drain_xrefs=True)

        self.assertEqual((stats.items_synced, stats.xref_items_synced), (0, 1))
        self.assertEqual(len(self.gh.queries), 1)
        query = self.gh.queries[0]
        self.assertIn(ISSUE_NODE, query)
        self.assertIn(f"itemTypes: [{', '.join(XREF_TYPES)}]", query)
        self.assertIn(f'since: "{iso_at(now - 3600 - 300)}"', query)
        timeline_at, xref_at = self._synced("issues", 3)
        self.assertEqual(timeline_at, synced)
        self.assertGreater(xref_at, synced)
        remaining = self.store.conn.execute(
            "SELECT COUNT(*) FROM timeline_dirty"
        ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_updated_at_ahead_of_local_clock_is_not_refetched(self) -> None:
        # A GitHub updatedAt ahead of the local clock: synced_at takes the later value.
        future = "2099-01-01T00:00:00Z"
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    self.rid,
                    7,
                    "Add widget sorting",
                    "OPEN",
                    future,
                    node_id=PR_NODE,
                )
            ]
        )
        self._set_synced("pull_requests", 7, "2027-01-10T10:00:00Z")
        stats = self._sync(TimelineMode.INCREMENTAL, max_batches=5)
        self.assertEqual(len(self.gh.batch_queries), 1)
        self.assertEqual(stats.items_synced, 1)
        self.assertEqual(self._synced("pull_requests", 7), (future, future))

        self.gh.queries.clear()
        stats = self._sync(TimelineMode.INCREMENTAL, max_batches=5)
        self.assertEqual(self.gh.queries, [])
        self.assertEqual(stats.items_synced, 0)

    def test_item_requeued_during_run_is_fetched_once_per_pass(self) -> None:
        # A row queued after the run start survives mark_timeline_synced, so
        # take_timeline_dirty keeps returning it; the pass must not loop on it.
        now = time.time()
        synced = iso_at(now - 3600)
        with self.store.conn:
            for table in ("pull_requests", "issues"):
                self.store.conn.execute(
                    f"UPDATE {table} SET updated_at = ?", (iso_at(now - 7200),)
                )
        self._set_synced("pull_requests", 7, synced, synced)
        self._set_synced("issues", 3, synced, synced)
        self.store.enqueue_timeline_dirty(
            [(self.rid, 3)], reason="mention", now=now + 600
        )
        stats = self._sync(TimelineMode.INCREMENTAL, drain_xrefs=True, max_batches=5)
        self.assertEqual(len(self.gh.batch_queries), 1)
        self.assertEqual(stats.xref_items_synced, 1)
        remaining = self.store.conn.execute(
            "SELECT COUNT(*) FROM timeline_dirty"
        ).fetchone()[0]
        self.assertEqual(remaining, 1)

    def test_max_batches_bounds_the_pass(self) -> None:
        self.store.upsert_issues(
            [
                IssueRecord(
                    self.rid,
                    n,
                    f"Issue {n}",
                    "OPEN",
                    "2027-01-11T00:00:00Z",
                    node_id=f"I_extra{n}",
                )
                for n in range(100, 125)
            ]
        )
        stats = self._sync(TimelineMode.BACKFILL, max_batches=1)
        self.assertEqual(len(self.gh.batch_queries), 1)
        self.assertEqual(len(re.findall(r"t\d+: node", self.gh.batch_queries[0])), 20)
        self.assertEqual(stats.pending_backfill, 27 - 20)

    def test_three_batches_of_twenty_items_batch_persistence_and_bound_candidate_limit(
        self,
    ) -> None:
        template_node = copy.deepcopy(self.gh.nodes[ISSUE_NODE])
        extra_issues: list[IssueRecord] = []
        for n in range(100, 158):
            node_id = f"I_batch_{n}"
            node_copy = copy.deepcopy(template_node)
            node_copy["id"] = node_id
            for idx, ev in enumerate(node_copy["timelineItems"]["nodes"]):
                if isinstance(ev, dict) and "id" in ev:
                    ev["id"] = f"{ev['id']}_{n}_{idx}"
            self.gh.nodes[node_id] = node_copy
            extra_issues.append(
                IssueRecord(
                    self.rid,
                    n,
                    f"Issue {n}",
                    "OPEN",
                    "2027-01-11T00:00:00Z",
                    node_id=node_id,
                )
            )
        # Keep PR 7 single-page so all 60 items (2 existing + 58 extra) run without follow-up pages.
        self.gh.nodes[PR_NODE]["timelineItems"]["pageInfo"]["hasNextPage"] = False
        self.store.upsert_issues(extra_issues)

        upsert_calls = 0
        mark_batch_calls = 0
        candidate_limits: list[int] = []
        orig_upsert = self.store.upsert_timeline_events
        orig_mark_many = self.store.mark_timelines_synced
        orig_candidates = self.store.timeline_candidates

        def counting_upsert(*args: Any, **kwargs: Any) -> int:
            nonlocal upsert_calls
            upsert_calls += 1
            return orig_upsert(*args, **kwargs)

        def counting_mark_many(*args: Any, **kwargs: Any) -> None:
            nonlocal mark_batch_calls
            mark_batch_calls += 1
            orig_mark_many(*args, **kwargs)

        def recording_candidates(
            repo_id: int, mode: TimelineMode, limit: int
        ) -> list[TimelineTarget]:
            candidate_limits.append(limit)
            return orig_candidates(repo_id, mode, limit)

        self.store.upsert_timeline_events = counting_upsert  # type: ignore[method-assign]
        self.store.mark_timelines_synced = counting_mark_many  # type: ignore[method-assign]
        self.store.timeline_candidates = recording_candidates  # type: ignore[method-assign]

        stats = self._sync(TimelineMode.BACKFILL, max_batches=3)
        self.assertEqual(stats.items_synced, 60)
        self.assertEqual(len(self.gh.batch_queries), 3)
        self.assertEqual(upsert_calls, 3)
        self.assertEqual(mark_batch_calls, 3)
        self.assertEqual(candidate_limits, [20, 20, 20])


if __name__ == "__main__":
    unittest.main()
