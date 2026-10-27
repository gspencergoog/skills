"""Unit tests for normalize.py."""

from __future__ import annotations

import json
import unittest

from normalize import normalize_pr, resolve_stack_parents
from testing import load_fixture


class TestNormalize(unittest.TestCase):
    def test_normalize_pr_open_ci_running_fixture(self) -> None:
        payload = json.loads(load_fixture("pr_open_ci_running.json"))
        raw = payload["data"]["pr0"]["pullRequest"]
        snap = normalize_pr(raw, viewer="alice")
        self.assertEqual(snap.key, "acme/widget#3339")
        self.assertTrue(snap.mine)
        self.assertEqual(snap.state, "OPEN")
        self.assertFalse(snap.draft)
        self.assertEqual(snap.last_committer, "alice")
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


if __name__ == "__main__":
    unittest.main()
