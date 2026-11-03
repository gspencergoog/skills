# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the read-only packrat SQLite signal loader."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from packrat_signals import load_packrat_signals
from reviewer_models import parse_iso_timestamp_or_zero

PACKRAT_TEST_SCHEMA = """
CREATE TABLE repos (
  repo_id INTEGER PRIMARY KEY AUTOINCREMENT,
  owner TEXT NOT NULL,
  name TEXT NOT NULL,
  full_name TEXT NOT NULL UNIQUE,
  local_path TEXT,
  default_branch TEXT NOT NULL DEFAULT 'main',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  prs_backfill_cursor TEXT,
  prs_backfill_done_at TEXT
);

CREATE TABLE pull_requests (
  repo_id INTEGER NOT NULL,
  number INTEGER NOT NULL,
  state TEXT NOT NULL,
  author TEXT,
  title TEXT NOT NULL,
  base_ref TEXT,
  head_ref TEXT,
  head_sha TEXT,
  merge_commit_sha TEXT,
  changed_files INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (repo_id, number)
);

CREATE TABLE pr_files (
  repo_id INTEGER NOT NULL,
  pr_number INTEGER NOT NULL,
  file_path TEXT NOT NULL,
  change_type TEXT NOT NULL DEFAULT 'MODIFIED',
  PRIMARY KEY (repo_id, pr_number, file_path)
);

CREATE TABLE reviews (
  repo_id INTEGER NOT NULL,
  review_id TEXT NOT NULL,
  pr_number INTEGER NOT NULL,
  author TEXT,
  state TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  submitted_at TEXT,
  PRIMARY KEY (repo_id, review_id)
);

CREATE TABLE review_threads (
  thread_id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL,
  pr_number INTEGER NOT NULL,
  file_path TEXT,
  reviewer TEXT,
  created_at TEXT,
  updated_at TEXT,
  body TEXT NOT NULL DEFAULT '',
  comments_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE commits (
  repo_id INTEGER NOT NULL,
  sha TEXT NOT NULL,
  author_name TEXT,
  author_email TEXT,
  committed_at TEXT NOT NULL,
  title TEXT NOT NULL,
  pr_number INTEGER,
  PRIMARY KEY (repo_id, sha)
);
"""


def _init_packrat_db(
    db_path: Path, *, backfill_done_at: str | None = "2026-04-01T00:00:00Z"
) -> int:
    """Creates a minimal packrat SQLite database and inserts ``octo-org/widgets``."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(PACKRAT_TEST_SCHEMA)
        cur = conn.execute(
            """
            INSERT INTO repos (
              owner, name, full_name, local_path, default_branch,
              created_at, updated_at, prs_backfill_done_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "octo-org",
                "widgets",
                "octo-org/widgets",
                "/workspace/widgets",
                "main",
                "2026-01-01T00:00:00Z",
                "2026-04-01T00:00:00Z",
                backfill_done_at,
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 1)
    finally:
        conn.close()


class TestPackratSignalsFallbackAndSchema(unittest.TestCase):
    """Tests fallback statuses, read-only behavior, and schema resilience."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)

    def test_no_github_repo_returns_without_note(self) -> None:
        missing = self.tmp_dir / "never_created.sqlite"
        res_none = load_packrat_signals(None, ["src/app.py"], db_path=missing)
        res_blank = load_packrat_signals("   ", ["src/app.py"], db_path=missing)

        self.assertEqual(res_none.status, "no_github_repo")
        self.assertIsNone(res_none.fallback_note)
        self.assertEqual(res_blank.status, "no_github_repo")
        self.assertFalse(missing.exists())

    def test_missing_db_does_not_create_file_on_disk(self) -> None:
        missing = self.tmp_dir / "absent" / "repos.sqlite"
        res = load_packrat_signals(
            "octo-org/widgets",
            ["src/button.py"],
            db_path=missing,
            repo_dir="/work/widgets",
        )

        self.assertEqual(res.status, "missing_db")
        self.assertFalse(missing.exists())
        self.assertIsNotNone(res.fallback_note)
        assert res.fallback_note is not None
        self.assertIn(
            "packrat repo add octo-org/widgets --path /work/widgets",
            res.fallback_note,
        )
        self.assertIn("packrat sync --repo octo-org/widgets", res.fallback_note)

    def test_unreadable_db_for_corrupt_bytes_and_empty_schema(self) -> None:
        corrupt = self.tmp_dir / "corrupt.sqlite"
        corrupt.write_bytes(b"not a valid sqlite header")
        res_corrupt = load_packrat_signals("octo-org/widgets", ["src/button.py"], db_path=corrupt)
        self.assertEqual(res_corrupt.status, "unreadable_db")
        self.assertIn("--repo octo-org/widgets", res_corrupt.fallback_note or "")

        empty_db = self.tmp_dir / "empty.sqlite"
        sqlite3.connect(empty_db).close()
        res_empty = load_packrat_signals("octo-org/widgets", ["src/button.py"], db_path=empty_db)
        self.assertEqual(res_empty.status, "unreadable_db")

    def test_untracked_repo_and_incomplete_backfill_warning(self) -> None:
        db_path = self.tmp_dir / "repos.sqlite"
        _init_packrat_db(db_path, backfill_done_at=None)

        untracked = load_packrat_signals(
            "octo-org/unregistered", ["src/button.py"], db_path=db_path
        )
        self.assertEqual(untracked.status, "untracked_repo")
        self.assertIn(
            "packrat sync --repo octo-org/unregistered",
            untracked.fallback_note or "",
        )

        tracked = load_packrat_signals("OCTO-ORG/WIDGETS", ["src/button.py"], db_path=db_path)
        self.assertEqual(tracked.status, "ok")
        self.assertEqual(tracked.repo_full_name, "octo-org/widgets")
        self.assertFalse(tracked.prs_backfill_complete)
        self.assertIn(
            "packrat sync --repo octo-org/widgets",
            tracked.backfill_warning or "",
        )

    def test_legacy_repos_table_without_backfill_columns(self) -> None:
        db_path = self.tmp_dir / "legacy.sqlite"
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE repos (
                  repo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  owner TEXT NOT NULL,
                  name TEXT NOT NULL,
                  full_name TEXT NOT NULL UNIQUE,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                INSERT INTO repos (owner, name, full_name, created_at, updated_at)
                VALUES ('octo-org', 'widgets', 'octo-org/widgets', '2026-01-01T00:00:00Z', '2026-01-02T00:00:00Z');
                """
            )
            conn.commit()
        finally:
            conn.close()

        res = load_packrat_signals("octo-org/widgets", ["src/button.py"], db_path=db_path)
        self.assertEqual(res.status, "ok")
        self.assertFalse(res.prs_backfill_complete)
        self.assertIsNotNone(res.backfill_warning)


class TestPackratReviewsThreadsAndIdentities(unittest.TestCase):
    """Tests review/thread extraction, sibling scoping, offline PRs, and commit maps."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "repos.sqlite"
        self.repo_id = _init_packrat_db(self.db_path)

    def test_reviews_and_threads_direct_and_immediate_siblings_only(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            # PR 10: touches changed file src/widgets/button.py
            conn.execute(
                """
                INSERT INTO pull_requests (
                  repo_id, number, state, author, title, base_ref, changed_files, created_at, updated_at
                ) VALUES (?, 10, 'MERGED', 'pr-author', 'Add button', 'main', 3, '2026-02-01T00:00:00Z', '2026-02-02T00:00:00Z')
                """,
                (self.repo_id,),
            )
            conn.execute(
                "INSERT INTO pr_files (repo_id, pr_number, file_path) VALUES (?, 10, 'src/widgets/button.py')",
                (self.repo_id,),
            )
            # Valid review with NULL submitted_at -> falls back to p.updated_at; self-review & bot & DISMISSED ignored.
            conn.executemany(
                "INSERT INTO reviews (repo_id, review_id, pr_number, author, state, submitted_at) VALUES (?, ?, 10, ?, ?, ?)",
                [
                    (self.repo_id, "R_alice", "alice-dev", "APPROVED", None),
                    (self.repo_id, "R_self", "PR-Author", "COMMENTED", "2026-02-02T01:00:00Z"),
                    (self.repo_id, "R_bot", "dependabot[bot]", "COMMENTED", "2026-02-02T01:00:00Z"),
                    (self.repo_id, "R_dismissed", "carol-dev", "DISMISSED", "2026-02-02T01:00:00Z"),
                ],
            )
            thread_comments = json.dumps(
                [
                    {"author": {"login": "alice-dev"}, "body": "Nit on padding"},
                    {"author": {"login": "pr-author"}, "body": "Done"},
                    {"author": None, "body": "Deleted ghost user"},
                    {"author": {"login": "bob-octo"}, "body": "Agreed"},
                    {"author": {"login": "bob-octo"}, "body": "Second reply same thread"},
                    {"author": {"login": "copilot-pull-request-reviewer"}, "body": "Bot note"},
                ]
            )
            conn.executemany(
                """
                INSERT INTO review_threads (
                  thread_id, repo_id, pr_number, file_path, reviewer, created_at, updated_at, comments_json
                ) VALUES (?, ?, 10, 'src/widgets/button.py', ?, ?, ?, ?)
                """,
                [
                    (
                        "T_1",
                        self.repo_id,
                        "alice-dev",
                        "2026-02-01T12:00:00Z",
                        "2026-02-02T03:00:00Z",
                        thread_comments,
                    ),
                    ("T_bad_json", self.repo_id, "dora-dev", None, None, "{malformed"),
                    ("T_obj_json", self.repo_id, None, None, None, '{"unexpected": true}'),
                ],
            )

            # PR 11: touches two immediate sibling files in src/widgets/ (slider.py, tabs.py)
            conn.execute(
                """
                INSERT INTO pull_requests (
                  repo_id, number, state, author, title, base_ref, changed_files, created_at, updated_at
                ) VALUES (?, 11, 'MERGED', 'pr-author', 'Add siblings', 'main', 42, '2026-03-01T00:00:00Z', '2026-03-02T00:00:00Z')
                """,
                (self.repo_id,),
            )
            conn.executemany(
                "INSERT INTO pr_files (repo_id, pr_number, file_path) VALUES (?, 11, ?)",
                [
                    (self.repo_id, "src/widgets/slider.py"),
                    (self.repo_id, "src/widgets/tabs.py"),
                ],
            )
            conn.execute(
                "INSERT INTO reviews (repo_id, review_id, pr_number, author, state, submitted_at) VALUES (?, 'R_sib', 11, 'erin-dev', 'CHANGES_REQUESTED', '2026-03-02T04:00:00Z')",
                (self.repo_id,),
            )
            conn.execute(
                """
                INSERT INTO review_threads (
                  thread_id, repo_id, pr_number, file_path, reviewer, created_at, updated_at, comments_json
                ) VALUES ('T_sib', ?, 11, 'src/widgets/slider.py', 'erin-dev', '2026-03-02T04:00:00Z', '2026-03-02T05:00:00Z', '[]')
                """,
                (self.repo_id,),
            )

            # PR 12: touches nested subdirectory src/widgets/internal/deep.py (must be ignored!)
            conn.execute(
                """
                INSERT INTO pull_requests (
                  repo_id, number, state, author, title, base_ref, changed_files, created_at, updated_at
                ) VALUES (?, 12, 'MERGED', 'pr-author', 'Deep internal', 'main', 1, '2026-03-05T00:00:00Z', '2026-03-05T01:00:00Z')
                """,
                (self.repo_id,),
            )
            conn.execute(
                "INSERT INTO pr_files (repo_id, pr_number, file_path) VALUES (?, 12, 'src/widgets/internal/deep.py')",
                (self.repo_id,),
            )
            conn.execute(
                "INSERT INTO reviews (repo_id, review_id, pr_number, author, state, submitted_at) VALUES (?, 'R_deep', 12, 'deep-only', 'APPROVED', '2026-03-05T02:00:00Z')",
                (self.repo_id,),
            )
            conn.execute(
                """
                INSERT INTO review_threads (
                  thread_id, repo_id, pr_number, file_path, reviewer, created_at, updated_at, comments_json
                ) VALUES ('T_deep', ?, 12, 'src/widgets/internal/deep.py', 'deep-only', '2026-03-05T02:00:00Z', '2026-03-05T02:00:00Z', '[]')
                """,
                (self.repo_id,),
            )
            conn.commit()
        finally:
            conn.close()

        result = load_packrat_signals(
            "octo-org/widgets",
            ["src/widgets/button.py", "README.md"],
            db_path=self.db_path,
        )

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.prs_backfill_complete)
        self.assertIsNone(result.backfill_warning)

        # Direct review on PR 10 + 1 deduplicated directory review on PR 11 (deep PR 12 excluded).
        self.assertEqual(len(result.review_events), 2)
        direct_rev = next(e for e in result.review_events if not e.is_directory)
        dir_rev = next(e for e in result.review_events if e.is_directory)
        self.assertEqual(direct_rev.reviewer_login, "alice-dev")
        self.assertEqual(direct_rev.scope_path, "src/widgets/button.py")
        self.assertEqual(direct_rev.timestamp, parse_iso_timestamp_or_zero("2026-02-02T00:00:00Z"))
        self.assertEqual(direct_rev.pr_changed_files, 3)

        self.assertEqual(dir_rev.reviewer_login, "erin-dev")
        self.assertEqual(dir_rev.scope_path, "src/widgets")
        self.assertEqual(dir_rev.pr_changed_files, 42)

        # Threads: T_1 -> alice-dev & bob-octo (once); T_bad_json -> dora-dev; T_sib -> erin-dev on "src/widgets".
        thread_tuples = {
            (t.thread_id, t.reviewer_login, t.scope_path, t.is_directory)
            for t in result.thread_events
        }
        self.assertEqual(
            thread_tuples,
            {
                ("T_1", "alice-dev", "src/widgets/button.py", False),
                ("T_1", "bob-octo", "src/widgets/button.py", False),
                ("T_bad_json", "dora-dev", "src/widgets/button.py", False),
                ("T_sib", "erin-dev", "src/widgets", True),
            },
        )

    def test_offline_target_pr_loads_files_and_excludes_self_pr_events(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO pull_requests (
                  repo_id, number, state, author, title, base_ref, changed_files, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'main', 1, '2026-03-10T00:00:00Z', '2026-03-10T02:00:00Z')
                """,
                [
                    (self.repo_id, 50, "MERGED", "past-author", "Earlier PR"),
                    (self.repo_id, 99, "OPEN", "target-owner", "Target offline PR"),
                ],
            )
            conn.executemany(
                "INSERT INTO pr_files (repo_id, pr_number, file_path) VALUES (?, ?, 'src/widgets/dialog.py')",
                [(self.repo_id, 50), (self.repo_id, 99)],
            )
            conn.executemany(
                "INSERT INTO reviews (repo_id, review_id, pr_number, author, state, submitted_at) VALUES (?, ?, ?, ?, 'APPROVED', '2026-03-10T03:00:00Z')",
                [
                    (self.repo_id, "R_50", 50, "hist-reviewer"),
                    (self.repo_id, "R_99", 99, "already-reviewed-target"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = load_packrat_signals(
            "octo-org/widgets",
            [],
            db_path=self.db_path,
            target_pr=99,
        )
        self.assertEqual(result.target_pr_author, "target-owner")
        self.assertEqual(result.target_pr_title, "Target offline PR")
        self.assertEqual(result.target_pr_base_ref, "main")
        self.assertEqual(result.target_pr_files, ("src/widgets/dialog.py",))
        self.assertEqual([e.reviewer_login for e in result.review_events], ["hist-reviewer"])

    def test_commit_identity_linkage_skips_merge_commits_and_ambiguous_keys(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executemany(
                """
                INSERT INTO pull_requests (
                  repo_id, number, state, author, title, head_sha, merge_commit_sha, updated_at
                ) VALUES (?, ?, 'MERGED', ?, ?, ?, ?, '2026-03-20T00:00:00Z')
                """,
                [
                    (self.repo_id, 21, "alice-dev", "Feature A", "sha_head_a", "sha_merge_21"),
                    (self.repo_id, 22, "bob-octo", "Squash B", "sha_head_b", "sha_squash_22"),
                    (self.repo_id, 23, "carol-dev", "Feature C", "sha_head_c", None),
                ],
            )
            conn.executemany(
                """
                INSERT INTO commits (
                  repo_id, sha, author_name, author_email, committed_at, title, pr_number
                ) VALUES (?, ?, ?, ?, '2026-03-20T00:00:00Z', ?, ?)
                """,
                [
                    # Unambiguous author commit via pr_number & head_sha
                    (
                        self.repo_id,
                        "sha_head_a",
                        "Alice Dev",
                        "alice@widgets.example",
                        "feat: add A",
                        21,
                    ),
                    # Maintainer merge commit (has BOTH pr_number=21 and merge_commit_sha!) -> MUST BE SKIPPED!
                    (
                        self.repo_id,
                        "sha_merge_21",
                        "Maintainer Jane",
                        "jane@widgets.example",
                        "Merge pull request #21 from octo-org/a",
                        21,
                    ),
                    # Branch sync merge commit -> MUST BE SKIPPED!
                    (
                        self.repo_id,
                        "sha_sync_21",
                        "Maintainer Jane",
                        "jane@widgets.example",
                        "Merge branch 'main' into feat-a",
                        21,
                    ),
                    # Squash merge commit via merge_commit_sha -> linked to bob-octo; single-word name "Alex" dropped from name_to_login!
                    (
                        self.repo_id,
                        "sha_squash_22",
                        "Alex",
                        "bob@widgets.example",
                        "feat: squash B (#22)",
                        None,
                    ),
                    # Ambiguous shared email across bob-octo (PR 22) and carol-dev (PR 23) -> dropped from email_to_login!
                    (
                        self.repo_id,
                        "sha_shared_b",
                        "Bob Octo",
                        "shared@widgets.example",
                        "chore: pair 1",
                        22,
                    ),
                    (
                        self.repo_id,
                        "sha_head_c",
                        "Carol Dev",
                        "shared@widgets.example",
                        "chore: pair 2",
                        23,
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        result = load_packrat_signals("octo-org/widgets", ["src/app.py"], db_path=self.db_path)
        self.assertEqual(result.sha_to_login.get("sha_head_a"), "alice-dev")
        self.assertEqual(result.sha_to_login.get("sha_squash_22"), "bob-octo")
        self.assertNotIn("sha_merge_21", result.sha_to_login)
        self.assertNotIn("sha_sync_21", result.sha_to_login)

        self.assertEqual(result.email_to_login.get("alice@widgets.example"), "alice-dev")
        self.assertEqual(result.email_to_login.get("bob@widgets.example"), "bob-octo")
        self.assertNotIn("jane@widgets.example", result.email_to_login)
        self.assertNotIn("shared@widgets.example", result.email_to_login)

        self.assertEqual(result.name_to_login.get("alice dev"), "alice-dev")
        self.assertEqual(result.name_to_login.get("bob octo"), "bob-octo")
        self.assertNotIn("maintainer jane", result.name_to_login)
        self.assertNotIn("alex", result.name_to_login)


if __name__ == "__main__":
    unittest.main()
