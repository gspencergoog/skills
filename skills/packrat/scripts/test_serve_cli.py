"""Unit tests for `serve_cli.py` and `DryRunStore`."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from db_store import PackratStore
from serve_cli import DryRunRunner, DryRunStore


class TestDryRunStore(unittest.TestCase):
    """Tests that `DryRunStore` blocks unverified store attributes and drops writes."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="packrat-serve-cli-"))
        self.store = PackratStore(self.tmp / "repos.sqlite")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_blocks_unprefixed_write_methods_and_conn(self) -> None:
        dry = DryRunStore(self.store)
        for blocked in (
            "register_repo",
            "rename_repo",
            "conn",
            "close",
            "unknown_method",
        ):
            with (
                self.subTest(blocked=blocked),
                self.assertRaisesRegex(
                    AttributeError, "DryRunStore blocks unverified store attribute"
                ),
            ):
                getattr(dry, blocked)

    def test_drops_writes_and_allows_verified_reads(self) -> None:
        self.store.register_repo("octo-org", "widgets")
        self.store.request_refresh(["octo-org/widgets#7"], now=100.0)
        dry = DryRunStore(self.store)

        self.assertEqual(dry.take_refresh_requests(), ["octo-org/widgets#7"])
        # Queue remains intact on the underlying store.
        self.assertEqual(dry.take_refresh_requests(), ["octo-org/widgets#7"])
        self.assertFalse(dry.replace_thread_activity(1, 7, []))
        self.assertEqual(len(dry.list_repos()), 1)
        self.assertIsNotNone(dry.get_repo("octo-org/widgets"))
        placeholder = dry.ensure_items_repo("other", "repo")
        self.assertEqual(placeholder.repo_id, 0)
        self.assertIsNone(self.store.get_repo("other/repo"))

    def test_take_refresh_requests_delegates_to_peek_refresh_requests(self) -> None:
        class FakePeekStore:
            def peek_refresh_requests(self) -> list[str]:
                return ["octo-org/widgets#42"]

        dry = DryRunStore(FakePeekStore())  # type: ignore[arg-type]
        self.assertEqual(dry.take_refresh_requests(), ["octo-org/widgets#42"])


class TestDryRunRunner(unittest.TestCase):
    """Tests `DryRunRunner` canned responses and summary formatting."""

    def test_records_rest_and_graphql_calls(self) -> None:
        runner = DryRunRunner()
        rest_res = runner(["api", "-i", "notifications"])
        gql_res = runner(
            ["api", "-i", "graphql", "-f", "query=query { viewer { login } }"]
        )
        self.assertIn("304 Not Modified", rest_res.stdout)
        self.assertIn('"data": {}', gql_res.stdout)
        self.assertEqual(
            runner.summary(),
            ["GET notifications", "graphql query { viewer { login } }"],
        )


if __name__ == "__main__":
    unittest.main()
