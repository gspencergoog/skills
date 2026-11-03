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

"""Unit tests for reviewer_models dataclasses, recency math, and IdentityResolver."""

from __future__ import annotations

import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import MagicMock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from reviewer_models import (
    BlameLineTouch,
    GitCommitTouch,
    IdentityResolver,
    PackratLoadResult,
    PackratReviewEvent,
    PackratThreadEvent,
    RankedReviewer,
    compute_recency_weight,
    is_bot_identity,
    parse_github_noreply_login,
    parse_iso_timestamp,
    parse_iso_timestamp_or_zero,
)


class TestDataclassesAndScopePaths(unittest.TestCase):
    """Tests frozen model contracts and scope_path normalization."""

    def test_file_and_directory_scope_path_contracts(self) -> None:
        direct_touch = GitCommitTouch(
            sha="a1b2c3",
            email="alice@widgets.example",
            name="Alice Dev",
            timestamp=1_770_000_000,
            scope_path="./src/widgets/button.py",
            is_directory=False,
        )
        self.assertEqual(direct_touch.scope_path, "src/widgets/button.py")

        dir_review = PackratReviewEvent(
            pr_number=42,
            reviewer_login="alice-dev",
            state="APPROVED",
            timestamp=1_770_000_000,
            scope_path="src/widgets/*",
            is_directory=True,
        )
        self.assertEqual(dir_review.scope_path, "src/widgets")

        dir_thread = PackratThreadEvent(
            thread_id="PRRT_1",
            pr_number=42,
            reviewer_login="alice-dev",
            timestamp=1_770_000_000,
            scope_path="src/widgets/",
            is_directory=True,
        )
        self.assertEqual(dir_thread.scope_path, "src/widgets")

    def test_invalid_scope_paths_rejected(self) -> None:
        with self.assertRaises(ValueError):
            GitCommitTouch("sha1", "a@widgets.example", "A", 1, "src/widgets/", False)
        with self.assertRaises(ValueError):
            PackratReviewEvent(1, "alice", "APPROVED", 1, "src/widgets/*", False)
        with self.assertRaises(ValueError):
            BlameLineTouch("sha1", "a@widgets.example", "A", 1, "/abs/file.py", 4)
        with self.assertRaises(ValueError):
            PackratThreadEvent("t1", 1, "alice", 1, "/*", True)

    def test_frozen_models_and_defaults(self) -> None:
        result = PackratLoadResult(status="ok", repo_full_name="octo-org/widgets")
        self.assertEqual(result.target_pr_files, ())
        self.assertEqual(result.known_logins, frozenset())
        self.assertFalse(result.prs_backfill_complete)

        ranked = RankedReviewer(
            canonical_key="alice-dev",
            username="alice-dev",
            is_github_login=True,
            display_name="Alice Dev",
            email="alice@widgets.example",
            score=28.5,
            direct_files_count=1,
            direct_commits=2,
            blame_lines=5,
            direct_reviews=1,
            direct_threads=1,
            directory_commits=0,
            directory_reviews=0,
            directory_threads=0,
            files=("src/widgets/button.py",),
        )
        with self.assertRaises(FrozenInstanceError):
            ranked.score = 0.0  # type: ignore[misc]


class TestTimestampsAndRecency(unittest.TestCase):
    """Tests ISO-8601 parsing and 180-day half-life decay."""

    def test_parse_iso_timestamp_variants(self) -> None:
        utc_ts = parse_iso_timestamp("2026-03-15T10:20:30Z")
        offset_ts = parse_iso_timestamp("2026-03-15T06:20:30-04:00")
        frac_ts = parse_iso_timestamp("2026-03-15T10:20:30.987654Z")
        naive_ts = parse_iso_timestamp("2026-03-15T10:20:30")

        self.assertIsNotNone(utc_ts)
        self.assertEqual(utc_ts, offset_ts)
        self.assertEqual(utc_ts, frac_ts)
        self.assertEqual(utc_ts, naive_ts)
        self.assertIsNone(parse_iso_timestamp(None))
        self.assertIsNone(parse_iso_timestamp("   "))
        self.assertIsNone(parse_iso_timestamp("not-a-timestamp"))
        self.assertEqual(parse_iso_timestamp_or_zero(None), 0)
        self.assertEqual(parse_iso_timestamp_or_zero("invalid"), 0)
        self.assertEqual(parse_iso_timestamp_or_zero("2026-03-15T10:20:30Z"), utc_ts)

    def test_compute_recency_weight_half_life_and_missing_fallback(self) -> None:
        now_ts = 1_770_000_000
        self.assertAlmostEqual(compute_recency_weight(now_ts, now_ts), 1.0)
        self.assertAlmostEqual(compute_recency_weight(now_ts + 3600, now_ts), 1.0)
        self.assertAlmostEqual(compute_recency_weight(now_ts - 180 * 86400, now_ts), 0.5)
        self.assertAlmostEqual(compute_recency_weight(now_ts - 360 * 86400, now_ts), 0.25)
        self.assertAlmostEqual(compute_recency_weight(0, now_ts), 0.5)
        self.assertAlmostEqual(compute_recency_weight(-100, now_ts), 0.5)


class TestBotAndNoreplyParsing(unittest.TestCase):
    """Tests bot filtering and GitHub noreply email extraction."""

    def test_is_bot_identity(self) -> None:
        bots = [
            "dependabot[bot]",
            "dependabot",
            "renovate",
            "renovate[bot]",
            "github-actions",
            "github-actions[bot]",
            "github-actions@github.com",
            "copilot-pull-request-reviewer",
            "copilot-pull-request-reviewer[bot]",
            "noreply@github.com",
            "git:noreply@github.com",
            "49699333+dependabot[bot]@users.noreply.github.com",
            "12345+renovate@users.noreply.github.com",
        ]
        for entry in bots:
            self.assertTrue(is_bot_identity(entry), msg=f"Expected bot for {entry!r}")

        humans = [
            "alice-dev",
            "@alice-dev",
            "alice@widgets.example",
            "12345+alice-dev@users.noreply.github.com",
            "dependabot-fan",
            "",
        ]
        for entry in humans:
            self.assertFalse(is_bot_identity(entry), msg=f"Expected human for {entry!r}")

    def test_parse_github_noreply_login(self) -> None:
        self.assertEqual(
            parse_github_noreply_login("123456+alice-dev@users.noreply.github.com"),
            "alice-dev",
        )
        self.assertEqual(
            parse_github_noreply_login("bob-octo@users.noreply.github.com"),
            "bob-octo",
        )
        self.assertIsNone(parse_github_noreply_login("noreply@github.com"))
        self.assertIsNone(parse_github_noreply_login("noreply@users.noreply.github.com"))
        self.assertIsNone(
            parse_github_noreply_login("49699333+dependabot[bot]@users.noreply.github.com")
        )
        self.assertIsNone(parse_github_noreply_login("12345+dependabot@users.noreply.github.com"))
        self.assertIsNone(parse_github_noreply_login("alice@widgets.example"))
        self.assertIsNone(parse_github_noreply_login(""))


class TestIdentityResolver(unittest.TestCase):
    """Tests the 5-step Identity Merge Ladder, caching, and exclusions."""

    def test_packrat_maps_merge_github_and_git_identities(self) -> None:
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            known_logins=frozenset({"alice-dev", "bob-octo"}),
            sha_to_login={"abc111": "alice-dev", "bot999": "dependabot[bot]"},
            email_to_login={"a.corp@widgets.example": "alice-dev"},
            name_to_login={"Bob Octo": "bob-octo", "Alex": "alex"},
        )
        resolver = IdentityResolver(packrat_result)

        self.assertEqual(resolver.resolve_github_login("@alice-dev"), "alice-dev")
        self.assertEqual(
            resolver.resolve_git_identity("abc111", "other@widgets.example", "A"),
            ("alice-dev", True),
        )
        self.assertEqual(
            resolver.resolve_git_identity("def222", "a.corp@widgets.example", "Alice Dev"),
            ("alice-dev", True),
        )
        self.assertEqual(
            resolver.resolve_git_identity("ghi333", "b123@widgets.example", "Bob Octo"),
            ("bob-octo", True),
        )
        # Single-word name in name_to_login ("Alex") must never trigger a merge.
        self.assertEqual(
            resolver.resolve_git_identity("jkl444", "achen@widgets.example", "Alex"),
            ("git:achen@widgets.example", False),
        )
        # Commit linked to a bot SHA is dropped rather than emitted as git:<email>.
        self.assertIsNone(
            resolver.resolve_git_identity("bot999", "ci@widgets.example", "CI Helper")
        )

    def test_noreply_and_exact_known_login_matching_without_false_merges(self) -> None:
        resolver = IdentityResolver(known_logins={"alice-dev", "dan", "jordan-lee", "alex"})

        # Noreply email resolves directly even when absent from known_logins.
        self.assertEqual(
            resolver.resolve_git_identity(
                "sha1", "777+carol-dev@users.noreply.github.com", "Carol"
            ),
            ("carol-dev", True),
        )
        # Exact normalized handle and multi-token full name merge cleanly.
        self.assertEqual(
            resolver.resolve_git_identity("sha2", "alice.dev@widgets.example", "Alice"),
            ("alice-dev", True),
        )
        self.assertEqual(
            resolver.resolve_git_identity("sha3", "a42@widgets.example", "Alice Dev"),
            ("alice-dev", True),
        )
        # Substring collision ("jordan" vs "dan") must NEVER merge into "dan".
        self.assertEqual(
            resolver.resolve_git_identity("sha4", "jordan@widgets.example", "Jordan"),
            ("git:jordan@widgets.example", False),
        )
        # Single first-name ("Alex") must NEVER merge into "@alex".
        self.assertEqual(
            resolver.resolve_git_identity("sha5", "achen@widgets.example", "Alex"),
            ("git:achen@widgets.example", False),
        )

    def test_reverse_substring_collision_dan_not_merged_into_jordan(self) -> None:
        resolver = IdentityResolver(known_logins={"jordan"})
        self.assertEqual(
            resolver.resolve_git_identity("sha1", "dan@widgets.example", "Dan"),
            ("git:dan@widgets.example", False),
        )

    def test_gh_lookup_positive_and_negative_email_caching(self) -> None:
        resolver = IdentityResolver(known_logins=set())
        gh_lookup = MagicMock(side_effect=[None, "erin-gh"])

        first = resolver.resolve_git_identity(
            "sha1", "unlinked@widgets.example", "Unlinked User", gh_lookup=gh_lookup
        )
        second = resolver.resolve_git_identity(
            "sha2", "unlinked@widgets.example", "Unlinked User", gh_lookup=gh_lookup
        )
        third = resolver.resolve_git_identity(
            "sha3", "erin@widgets.example", "Erin Dev", gh_lookup=gh_lookup
        )
        fourth = resolver.resolve_git_identity(
            "sha4", "erin@widgets.example", "Erin Dev", gh_lookup=gh_lookup
        )

        self.assertEqual(first, ("git:unlinked@widgets.example", False))
        self.assertEqual(second, ("git:unlinked@widgets.example", False))
        self.assertEqual(third, ("erin-gh", True))
        self.assertEqual(fourth, ("erin-gh", True))
        self.assertEqual(gh_lookup.call_count, 2)
        self.assertFalse(resolver.gh_circuit_open)

    def test_gh_lookup_circuit_breaker_trips_on_first_failure(self) -> None:
        resolver = IdentityResolver(known_logins={"frank-dev"})
        gh_lookup = MagicMock(side_effect=TimeoutError("gh api timed out"))

        first = resolver.resolve_git_identity(
            "sha1", "f.unknown@widgets.example", "Frank Dev", gh_lookup=gh_lookup
        )
        second = resolver.resolve_git_identity(
            "sha2", "other@widgets.example", "Other Person", gh_lookup=gh_lookup
        )

        self.assertTrue(resolver.gh_circuit_open)
        self.assertEqual(gh_lookup.call_count, 1)
        # Falls back to Step 4 (known_logins multi-token full name) and Step 5.
        self.assertEqual(first, ("frank-dev", True))
        self.assertEqual(second, ("git:other@widgets.example", False))

    def test_git_fallback_and_metadata_preferences(self) -> None:
        resolver = IdentityResolver(known_logins={"grace-hopper"})
        res1 = resolver.resolve_git_identity("sha1", "Grace@Widgets.Example", "Grace")
        res2 = resolver.resolve_git_identity("sha2", "grace@widgets.example", "Grace Hopper")
        res3 = resolver.resolve_git_identity(
            "sha3", "999+grace-hopper@users.noreply.github.com", "Grace"
        )
        self.assertEqual(res1, ("git:grace@widgets.example", False))
        self.assertEqual(res2, ("grace-hopper", True))
        self.assertEqual(res3, ("grace-hopper", True))
        self.assertEqual(resolver.preferred_display_name("grace-hopper"), "Grace Hopper")
        self.assertEqual(
            resolver.preferred_email("grace-hopper"),
            "grace@widgets.example",
        )
        self.assertEqual(
            resolver.preferred_email("git:grace@widgets.example"),
            "Grace@Widgets.Example",
        )

    def test_is_excluded_across_login_email_local_part_name_and_bots(self) -> None:
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            target_pr_author="pr-owner",
            email_to_login={"corp.owner@widgets.example": "mapped-owner"},
        )
        resolver = IdentityResolver(
            packrat_result,
            excluded_authors=[
                "@cli-excluded",
                "corp.owner@widgets.example",
                "Branch Author <branch.author@widgets.example>",
                "dan",
            ],
        )

        # Target PR author excluded across login, email local-part, and full name.
        self.assertTrue(resolver.is_excluded("pr-owner"))
        self.assertTrue(
            resolver.is_excluded(
                "git:pr.owner@widgets.example", {"pr.owner@widgets.example"}, set()
            )
        )
        self.assertTrue(resolver.is_excluded("git:p1@widgets.example", set(), {"PR Owner"}))
        # CLI handle, mapped email -> login, git author header, and bots excluded.
        self.assertTrue(resolver.is_excluded("cli-excluded"))
        self.assertTrue(resolver.is_excluded("mapped-owner"))
        self.assertTrue(resolver.is_excluded("branch-author"))
        self.assertTrue(
            resolver.is_excluded(
                "git:branch.author@widgets.example",
                {"branch.author@widgets.example"},
                {"Branch Author"},
            )
        )
        self.assertTrue(resolver.is_excluded("dependabot[bot]"))

        # Distinct candidates (including substring "jordan" vs "dan") stay included.
        self.assertFalse(resolver.is_excluded("jordan", {"jordan@widgets.example"}, {"Jordan Lee"}))
        self.assertFalse(
            resolver.is_excluded("alice-dev", {"alice@widgets.example"}, {"Alice Dev"})
        )


if __name__ == "__main__":
    unittest.main()
