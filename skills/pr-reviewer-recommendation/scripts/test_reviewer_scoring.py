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

"""Unit tests for reviewer_scoring ranking math, caps, exclusions, and reports."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

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
)
from reviewer_scoring import (
    format_json_report,
    format_markdown_report,
    rank_reviewers,
)

NOW_TS = 1_770_000_000
DAY_SECONDS = 86_400


class TestReviewerScoringRanking(unittest.TestCase):
    """Tests candidate scoring, caps, directory skipping, and partitioning."""

    def test_review_only_expert_ranks_primary_alongside_commit_owner(self) -> None:
        changed = ["src/widgets/button.py"]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            known_logins=frozenset({"reviewer-pro", "commit-owner"}),
            review_events=(
                PackratReviewEvent(
                    pr_number=10,
                    reviewer_login="reviewer-pro",
                    state="APPROVED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                    pr_changed_files=3,
                ),
                PackratReviewEvent(
                    pr_number=11,
                    reviewer_login="reviewer-pro",
                    state="CHANGES_REQUESTED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                    pr_changed_files=4,
                ),
            ),
            thread_events=(
                PackratThreadEvent(
                    thread_id="PRRT_1",
                    pr_number=10,
                    reviewer_login="reviewer-pro",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                ),
            ),
        )
        commits = [
            GitCommitTouch(
                sha="c1",
                email="commit.owner@widgets.example",
                name="Commit Owner",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            )
        ]
        blame = [
            BlameLineTouch(
                sha="c1",
                email="commit.owner@widgets.example",
                name="Commit Owner",
                timestamp=NOW_TS,
                file_path="src/widgets/button.py",
                line_count=6,
            )
        ]
        resolver = IdentityResolver(packrat_result)

        primary, secondary = rank_reviewers(
            changed, commits, blame, packrat_result, resolver, now_ts=NOW_TS
        )

        self.assertEqual(secondary, [])
        self.assertEqual([cand.username for cand in primary], ["reviewer-pro", "commit-owner"])
        expert = primary[0]
        self.assertEqual(expert.direct_files_count, 1)
        self.assertEqual(expert.direct_commits, 0)
        self.assertEqual(expert.direct_reviews, 2)
        self.assertEqual(expert.direct_threads, 1)
        # 10 (file) + 8 (2 reviews * 4) + 3 (thread) + 10 (recency_bonus) = 31.0
        self.assertAlmostEqual(expert.score, 31.0)
        # 10 (file) + 2 (commit) + 3 (6 blame * 0.5) + 10 (recency_bonus) = 25.0
        self.assertAlmostEqual(primary[1].score, 25.0)

    def test_per_file_and_per_directory_caps_enforced(self) -> None:
        changed = ["src/widgets/button.py", "src/layout/grid.py"]
        commits = [
            GitCommitTouch(
                sha=f"fc{idx}",
                email="alice-dev@users.noreply.github.com",
                name="Alice Dev",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            )
            for idx in range(20)
        ] + [
            GitCommitTouch(
                sha=f"dc{idx}",
                email="bob-octo@users.noreply.github.com",
                name="Bob Octo",
                timestamp=NOW_TS,
                scope_path="src/layout",
                is_directory=True,
            )
            for idx in range(30)
        ]
        blame = [
            BlameLineTouch(
                sha="fc0",
                email="alice-dev@users.noreply.github.com",
                name="Alice Dev",
                timestamp=NOW_TS,
                file_path="src/widgets/button.py",
                line_count=50,
            )
        ]
        reviews = tuple(
            PackratReviewEvent(
                pr_number=idx,
                reviewer_login="alice-dev",
                state="APPROVED",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
                is_directory=False,
            )
            for idx in range(10)
        ) + tuple(
            PackratReviewEvent(
                pr_number=100 + idx,
                reviewer_login="bob-octo",
                state="APPROVED",
                timestamp=NOW_TS,
                scope_path="src/layout",
                is_directory=True,
            )
            for idx in range(15)
        )
        threads = tuple(
            PackratThreadEvent(
                thread_id=f"ft{idx}",
                pr_number=idx,
                reviewer_login="alice-dev",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
                is_directory=False,
            )
            for idx in range(10)
        ) + tuple(
            PackratThreadEvent(
                thread_id=f"dt{idx}",
                pr_number=100 + idx,
                reviewer_login="bob-octo",
                timestamp=NOW_TS,
                scope_path="src/layout",
                is_directory=True,
            )
            for idx in range(12)
        )
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            review_events=reviews,
            thread_events=threads,
        )
        resolver = IdentityResolver(packrat_result)

        primary, secondary = rank_reviewers(
            changed, commits, blame, packrat_result, resolver, now_ts=NOW_TS
        )

        # Direct caps: 10 (file) + 30 (commit) + 12 (blame) + 24 (review) + 18 (thread) + 10 (recency) = 104.0
        self.assertEqual(len(primary), 1)
        self.assertAlmostEqual(primary[0].score, 104.0)
        self.assertEqual(primary[0].direct_commits, 20)
        self.assertEqual(primary[0].blame_lines, 50)
        self.assertEqual(primary[0].direct_reviews, 10)
        self.assertEqual(primary[0].direct_threads, 10)

        # Directory caps: 2 (dir) + 8 (commit) + 10 (review) + 8 (thread) + 10 (recency) = 38.0
        self.assertEqual(len(secondary), 1)
        self.assertAlmostEqual(secondary[0].score, 38.0)
        self.assertEqual(secondary[0].files, ("src/layout/*",))

    def test_directory_events_skipped_only_for_covered_candidate_directories(self) -> None:
        changed = ["src/widgets/button.py", "src/themes/dark.py"]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            review_events=(
                PackratReviewEvent(
                    pr_number=1,
                    reviewer_login="alice-dev",
                    state="APPROVED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                ),
                PackratReviewEvent(
                    pr_number=2,
                    reviewer_login="alice-dev",
                    state="APPROVED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets",
                    is_directory=True,
                ),
                PackratReviewEvent(
                    pr_number=3,
                    reviewer_login="alice-dev",
                    state="COMMENTED",
                    timestamp=NOW_TS,
                    scope_path="src/themes",
                    is_directory=True,
                ),
            ),
        )
        commits = [
            GitCommitTouch(
                sha="s1",
                email="bob-octo@users.noreply.github.com",
                name="Bob Octo",
                timestamp=NOW_TS,
                scope_path="src/widgets",
                is_directory=True,
            )
        ]
        resolver = IdentityResolver(packrat_result)

        primary, secondary = rank_reviewers(
            changed, commits, [], packrat_result, resolver, now_ts=NOW_TS
        )

        self.assertEqual(len(primary), 1)
        alice = primary[0]
        # src/widgets/* is skipped for alice because she matched src/widgets/button.py;
        # src/themes/* remains active because she had no direct file touch in src/themes.
        self.assertEqual(alice.files, ("src/widgets/button.py", "src/themes/*"))
        self.assertEqual(alice.directory_reviews, 1)
        # 10 (direct file) + 4.0 (APPROVED) + 2.0 (themes/*) + 0.75 (dir COMMENTED) + 10 (recency) = 26.75
        self.assertAlmostEqual(alice.score, 26.75)

        self.assertEqual(len(secondary), 1)
        self.assertEqual(secondary[0].username, "bob-octo")
        self.assertEqual(secondary[0].files, ("src/widgets/*",))

    def test_unified_identity_merges_git_and_packrat_without_double_counting(self) -> None:
        changed = ["src/widgets/button.py"]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            known_logins=frozenset({"alice-dev"}),
            sha_to_login={"sha_corp": "alice-dev"},
            review_events=(
                PackratReviewEvent(
                    pr_number=7,
                    reviewer_login="alice-dev",
                    state="COMMENTED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                ),
            ),
            thread_events=(
                PackratThreadEvent(
                    thread_id="t7",
                    pr_number=7,
                    reviewer_login="alice-dev",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                ),
            ),
        )
        commits = [
            GitCommitTouch(
                sha="sha_unmapped",
                email="alice.corp@widgets.example",
                name="Alice",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            ),
            GitCommitTouch(
                sha="sha_corp",
                email="alice.corp@widgets.example",
                name="Alice Dev",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            ),
            GitCommitTouch(
                sha="sha_dir",
                email="99+alice-dev@users.noreply.github.com",
                name="Alice Dev",
                timestamp=NOW_TS,
                scope_path="src/widgets",
                is_directory=True,
            ),
        ]
        resolver = IdentityResolver(packrat_result)

        primary, secondary = rank_reviewers(
            changed, commits, [], packrat_result, resolver, now_ts=NOW_TS
        )

        self.assertEqual(secondary, [])
        self.assertEqual(len(primary), 1)
        alice = primary[0]
        self.assertEqual(alice.username, "alice-dev")
        self.assertEqual(alice.direct_files_count, 1)
        self.assertEqual(alice.direct_commits, 2)
        self.assertEqual(alice.direct_reviews, 1)
        self.assertEqual(alice.direct_threads, 1)
        self.assertEqual(alice.directory_commits, 0)
        # 10 (1 file once) + 4.0 (2 commits) + 2.5 (COMMENTED) + 3.0 (thread) + 10 (recency once) = 29.5
        self.assertAlmostEqual(alice.score, 29.5)

    def test_mega_pr_attenuates_reviews_above_30_files_only(self) -> None:
        changed = ["src/widgets/button.py"]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            review_events=(
                PackratReviewEvent(
                    pr_number=20,
                    reviewer_login="focused-reviewer",
                    state="APPROVED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                    pr_changed_files=30,
                ),
                PackratReviewEvent(
                    pr_number=21,
                    reviewer_login="mega-reviewer",
                    state="APPROVED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                    pr_changed_files=31,
                ),
            ),
            thread_events=(
                PackratThreadEvent(
                    thread_id="mt1",
                    pr_number=21,
                    reviewer_login="mega-reviewer",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                ),
            ),
        )
        resolver = IdentityResolver(packrat_result)

        primary, _ = rank_reviewers(changed, [], [], packrat_result, resolver, now_ts=NOW_TS)
        by_login = {cand.username: cand for cand in primary}

        # focused: 10 + 4.0 * 1.0 + 10 = 24.0
        self.assertAlmostEqual(by_login["focused-reviewer"].score, 24.0)
        # mega: 10 + 4.0 * 0.35 (1.4) + 3.0 (thread unattenuated) + 10 = 24.4
        self.assertAlmostEqual(by_login["mega-reviewer"].score, 24.4)

    def test_recency_decay_missing_timestamp_and_overflow_partition(self) -> None:
        changed = ["src/widgets/button.py"]
        half_life_ts = NOW_TS - 180 * DAY_SECONDS
        review_events = [
            PackratReviewEvent(
                pr_number=idx + 1,
                reviewer_login=f"rev-{idx}",
                state="APPROVED",
                timestamp=NOW_TS - idx * DAY_SECONDS,
                scope_path="src/widgets/button.py",
                is_directory=False,
            )
            for idx in range(6)
        ]
        review_events.append(
            PackratReviewEvent(
                pr_number=99,
                reviewer_login="null-ts-reviewer",
                state="APPROVED",
                timestamp=0,
                scope_path="src/widgets/button.py",
                is_directory=False,
            )
        )
        commits = [
            GitCommitTouch(
                sha="hl1",
                email="half-life@users.noreply.github.com",
                name="Half Life",
                timestamp=half_life_ts,
                scope_path="src/widgets/button.py",
            )
        ]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            review_events=tuple(review_events),
        )
        resolver = IdentityResolver(packrat_result)

        primary, secondary = rank_reviewers(
            changed, commits, [], packrat_result, resolver, now_ts=NOW_TS
        )

        # Top 4 in primary; next 4 (positions 4..7) overflow into secondary.
        self.assertEqual(len(primary), 4)
        self.assertEqual(len(secondary), 4)
        by_login = {cand.username: cand for cand in primary + secondary}
        # half-life commit: 10 (file) + 2.0 * 0.5 + (10 - 180/30 = 4.0) = 15.0
        self.assertAlmostEqual(by_login["half-life"].score, 15.0)
        # null timestamp review: 10 (file) + 4.0 * 0.5 + 0.0 (no positive ts) = 12.0
        self.assertAlmostEqual(by_login["null-ts-reviewer"].score, 12.0)

    def test_excluded_authors_and_bots_filtered_across_git_and_packrat(self) -> None:
        changed = ["src/widgets/button.py"]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            target_pr_author="pr-author",
            review_events=(
                PackratReviewEvent(1, "pr-author", "APPROVED", NOW_TS, "src/widgets/button.py"),
                PackratReviewEvent(2, "cli-excluded", "APPROVED", NOW_TS, "src/widgets/button.py"),
                PackratReviewEvent(
                    3, "dependabot[bot]", "APPROVED", NOW_TS, "src/widgets/button.py"
                ),
                PackratReviewEvent(4, "gh-linked", "APPROVED", NOW_TS, "src/widgets/button.py"),
                PackratReviewEvent(5, "valid-peer", "APPROVED", NOW_TS, "src/widgets/button.py"),
            ),
        )
        commits = [
            GitCommitTouch(
                sha="s_linked",
                email="secret.author@widgets.example",
                name="Secret Author",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            ),
            GitCommitTouch(
                sha="s_bot",
                email="noreply@github.com",
                name="GitHub",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            ),
        ]
        resolver = IdentityResolver(
            packrat_result,
            excluded_authors=["cli-excluded", "secret.author@widgets.example"],
        )

        primary, secondary = rank_reviewers(
            changed,
            commits,
            [],
            packrat_result,
            resolver,
            now_ts=NOW_TS,
            gh_lookup=lambda sha: "gh-linked" if sha == "s_linked" else None,
        )

        self.assertEqual([cand.username for cand in primary], ["valid-peer"])
        self.assertEqual(secondary, [])


class TestReviewerScoringReports(unittest.TestCase):
    """Tests Markdown and JSON report rendering, unlinked git display, and callouts."""

    def test_unlinked_git_identity_display_and_omission_from_gh_pr_edit(self) -> None:
        changed = ["src/widgets/button.py", "src/themes/dark.py"]
        commits = [
            GitCommitTouch(
                sha="u1",
                email="jordan@widgets.example",
                name="Jordan Lee",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            ),
            GitCommitTouch(
                sha="g1",
                email="55+alice-dev@users.noreply.github.com",
                name="Alice Dev",
                timestamp=NOW_TS - 10 * DAY_SECONDS,
                scope_path="src/widgets/button.py",
            ),
            GitCommitTouch(
                sha="u2",
                email="sam@widgets.example",
                name="Sam Taylor",
                timestamp=NOW_TS,
                scope_path="src/themes",
                is_directory=True,
            ),
        ]
        packrat_result = PackratLoadResult(
            status="missing_db",
            repo_full_name="octo-org/widgets",
            fallback_note=(
                "packrat database missing; run `packrat repo add octo-org/widgets && "
                "packrat sync --repo octo-org/widgets` to include review signals."
            ),
            backfill_warning="prs backfill incomplete for octo-org/widgets; older reviews omitted.",
        )
        resolver = IdentityResolver(packrat_result)
        primary, secondary = rank_reviewers(
            changed, commits, [], packrat_result, resolver, now_ts=NOW_TS
        )

        markdown = format_markdown_report(
            changed,
            primary,
            secondary,
            packrat_result,
            repo="octo-org/widgets",
            pr_number=42,
            pr_title="Improve button contrast",
            pr_author="pr-creator",
            gh_available=True,
        )

        self.assertIn("## Reviewer Recommendation Report", markdown)
        self.assertIn("`octo-org/widgets`", markdown)
        self.assertIn("#42 Improve button contrast (by @pr-creator)", markdown)
        self.assertIn("packrat sync --repo octo-org/widgets", markdown)
        self.assertIn("prs backfill incomplete for octo-org/widgets", markdown)
        self.assertIn("Jordan Lee (git:jordan@widgets.example)", markdown)
        self.assertIn("Sam Taylor (git:sam@widgets.example)", markdown)
        self.assertNotIn("@jordan", markdown)
        self.assertNotIn("@sam", markdown)
        self.assertIn("**@alice-dev** (Alice Dev)", markdown)
        self.assertIn("gh pr edit 42 --add-reviewer alice-dev -R octo-org/widgets", markdown)

    def test_gh_pr_edit_omitted_when_only_unlinked_git_identities_exist(self) -> None:
        changed = ["src/widgets/button.py"]
        commits = [
            GitCommitTouch(
                sha="u1",
                email="jordan@widgets.example",
                name="Jordan Lee",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            )
        ]
        packrat_result = PackratLoadResult(status="no_github_repo")
        resolver = IdentityResolver(packrat_result)
        primary, secondary = rank_reviewers(
            changed, commits, [], packrat_result, resolver, now_ts=NOW_TS
        )

        markdown = format_markdown_report(
            changed, primary, secondary, packrat_result, gh_available=True
        )
        self.assertIn("Jordan Lee (git:jordan@widgets.example)", markdown)
        self.assertNotIn("gh pr edit", markdown)

    def test_json_report_schema_and_values(self) -> None:
        changed = ["src/widgets/button.py"]
        packrat_result = PackratLoadResult(
            status="ok",
            repo_full_name="octo-org/widgets",
            prs_backfill_complete=True,
            review_events=(
                PackratReviewEvent(
                    pr_number=8,
                    reviewer_login="alice-dev",
                    state="APPROVED",
                    timestamp=NOW_TS,
                    scope_path="src/widgets/button.py",
                    is_directory=False,
                ),
            ),
        )
        commits = [
            GitCommitTouch(
                sha="u1",
                email="jordan@widgets.example",
                name="Jordan Lee",
                timestamp=NOW_TS,
                scope_path="src/widgets/button.py",
            )
        ]
        resolver = IdentityResolver(packrat_result)
        primary, secondary = rank_reviewers(
            changed, commits, [], packrat_result, resolver, now_ts=NOW_TS
        )

        raw_json = format_json_report(
            changed, primary, secondary, packrat_result, repo="octo-org/widgets", pr_number=42
        )
        parsed = json.loads(raw_json)

        self.assertEqual(parsed["repo"], "octo-org/widgets")
        self.assertEqual(parsed["pr_number"], 42)
        self.assertEqual(parsed["changed_files"], ["src/widgets/button.py"])
        self.assertEqual(parsed["packrat"]["status"], "ok")
        self.assertTrue(parsed["packrat"]["prs_backfill_complete"])
        self.assertEqual(parsed["suggested_reviewers"], ["alice-dev"])
        self.assertEqual(len(parsed["primary"]), 2)
        self.assertEqual(parsed["secondary"], [])

        by_key = {entry["canonical_key"]: entry for entry in parsed["primary"]}
        self.assertTrue(by_key["alice-dev"]["is_github_login"])
        self.assertEqual(by_key["alice-dev"]["username"], "alice-dev")
        self.assertEqual(by_key["alice-dev"]["direct_reviews"], 1)
        self.assertFalse(by_key["git:jordan@widgets.example"]["is_github_login"])
        self.assertEqual(by_key["git:jordan@widgets.example"]["username"], "")


if __name__ == "__main__":
    unittest.main()
