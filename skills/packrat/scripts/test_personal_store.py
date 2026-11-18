"""Unit tests for Wave 1 multi-user `PersonalStore`, reader ops, and worker hooks."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from db_store import PackratStore
from embedder import DeterministicHashEmbedder
from gh_transport import (
    GhCommandResult,
    GhTransport,
    NullApiCallStore,
    make_token_gh_runner,
)
from git_cmd import build_isolated_git_config_args
from hot_sync import HotRefresher
from managed_git import validate_url
from models import (
    CheckRunRecord,
    CommitFileRecord,
    CommitRecord,
    IssuePrLink,
    IssueRecord,
    LatestReviewRecord,
    NotificationRecord,
    OpinionatedReviewRecord,
    PendingDeleteError,
    PrFileRecord,
    PrLiveRecord,
    PrRecord,
    ReviewRecord,
    ReviewRequestRecord,
    ReviewThreadRecord,
    SearchFilters,
    ServerConfig,
    StoredEmbeddingRecord,
    TextChunk,
    ThreadActivityRecord,
)
from personal_store import (
    ChangeNotifier,
    IdentityConflictError,
    PersonalStore,
    TooManyWaitersError,
    load_server_config,
)
from rate_budget import InMemoryRateBudget, Priority
from reader_ops import execute_reader_batch
from search_engine import (
    hybrid_rrf_search,
    rebuild_vector_mmap_cache,
    repo_watermarks,
)
from service import PackratService, reconcile_server_config
from signals import SignalPoller
from viewer_sync import ViewerSync


class _FakeGraphqlTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def graphql(
        self,
        query: str,
        variables: dict[str, object],
        *,
        worker: str,
        priority: Priority,
    ) -> dict[str, object]:
        del worker, priority
        self.calls.append((query, dict(variables)))
        return self._responses.pop(0) if self._responses else {"data": {}}


class TestPersonalStoreAndPrimitives(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.notifier = ChangeNotifier(max_waiters=2)
        self.personal = PersonalStore(
            self.tmp_path / "personal.sqlite", notifier=self.notifier
        )
        self.store = PackratStore(
            self.tmp_path / "repos.sqlite", personal_store=self.personal
        )

    def tearDown(self) -> None:
        self.store.close()
        self.personal.close()
        self._tmp.cleanup()

    def test_identity_verification_and_notification_isolation(self) -> None:
        t0 = 1_760_000_000.0
        ident_a = self.personal.link_identity(
            "alice@example.com", "alice-gh", verified=True, now=t0
        )
        self.assertTrue(ident_a.login_verified)

        with self.assertRaises(IdentityConflictError):
            self.personal.link_identity(
                "mallory@example.com", "alice-gh", verified=False, now=t0 + 10
            )

        # Unverified user linking an unclaimed login never exposes notifications.
        self.personal.link_identity(
            "bob@example.com", "bob-gh", verified=False, now=t0 + 10
        )
        self.personal.upsert_notifications(
            "alice@example.com",
            [
                NotificationRecord(
                    thread_id="t1",
                    reason="review_requested",
                    subject_type="PullRequest",
                    item_key="octo-org/widgets#1",
                    updated_at="2026-11-14T00:00:00Z",
                    unread=True,
                )
            ],
            now=t0 + 20,
        )
        self.personal.upsert_notifications(
            "bob@example.com",
            [
                NotificationRecord(
                    thread_id="t2",
                    reason="mention",
                    subject_type="PullRequest",
                    item_key="octo-org/widgets#2",
                    updated_at="2026-11-14T00:00:00Z",
                    unread=True,
                )
            ],
            now=t0 + 20,
        )
        self.assertEqual(
            len(self.personal.notifications_for_owner("alice@example.com")), 1
        )
        self.assertEqual(self.personal.notifications_for_owner("bob@example.com"), [])

    def test_changes_cursor_advances_to_max_seq_across_private_rows(self) -> None:
        t0 = 1_760_000_000.0
        self.personal.link_identity("alice@example.com", "alice", verified=True, now=t0)
        self.personal.link_identity("bob@example.com", "bob", verified=True, now=t0)

        self.personal.replace_viewer_set(
            "alice", "authored", ["octo-org/widgets#1"], now=t0
        )
        self.personal.replace_viewer_set(
            "alice", "authored_open", [], now=t0 + 5
        )
        self.assertEqual(
            self.personal.viewer_set_sync_times("alice"),
            {
                "authored": "2025-10-09T08:53:20Z",
                "authored_open": "2025-10-09T08:53:25Z",
            },
        )
        self.assertEqual(self.personal.viewer_set_sync_times("bob"), {})
        self.personal.upsert_notifications(
            "alice@example.com",
            [
                NotificationRecord(
                    thread_id="t1",
                    reason="author",
                    subject_type="PullRequest",
                    item_key="octo-org/widgets#1",
                    updated_at="2026-11-14T00:00:00Z",
                    unread=True,
                )
            ],
            now=t0 + 1,
        )
        max_seq = self.personal.max_change_seq()
        self.assertGreaterEqual(max_seq, 2)

        # Bob sees 0 rows from Alice's private changes, yet Bob's cursor advances to max_seq!
        next_seq, truncated, bob_changes = self.personal.changes_since_for_caller(
            0, viewer_login="bob", owner_id="bob@example.com"
        )
        self.assertEqual(bob_changes, [])
        self.assertFalse(truncated)
        self.assertEqual(next_seq, max_seq)

        # Broadcast PR change appended via PackratStore is visible to both.
        repo = self.store.register_repo(
            "octo-org", "widgets", local_path=None, git_source="none"
        )
        self.store.upsert_pr_live(
            PrLiveRecord(
                repo_id=repo.repo_id,
                number=1,
                title="Fix",
                state="OPEN",
                updated_at="2026-11-14T01:00:00Z",
            ),
            now=t0 + 2,
        )
        next_seq2, _, bob_changes2 = self.personal.changes_since_for_caller(
            next_seq, viewer_login="bob", owner_id="bob@example.com"
        )
        self.assertEqual(len(bob_changes2), 1)
        self.assertEqual(bob_changes2[0].item_key, "octo-org/widgets#1")
        self.assertGreater(next_seq2, next_seq)

    def test_change_notifier_enforces_max_waiters(self) -> None:
        import threading
        import time

        ready = threading.Barrier(3)
        release = threading.Event()

        def _hold() -> None:
            with self.notifier._cond:
                self.notifier._waiters += 1
            ready.wait()
            release.wait(timeout=5)
            with self.notifier._cond:
                self.notifier._waiters -= 1

        threads = [threading.Thread(target=_hold) for _ in range(2)]
        for t in threads:
            t.start()
        ready.wait()
        try:
            with self.assertRaises(TooManyWaitersError):
                self.notifier.wait_until(time.monotonic() + 2.0)
        finally:
            release.set()
            for t in threads:
                t.join()

    def test_in_memory_budget_null_store_and_token_runner(self) -> None:
        budget = InMemoryRateBudget()
        now = 1_760_000_000.0
        self.assertTrue(budget.allows("graphql", Priority.BACKFILL, now=now))
        budget.record(
            {
                "x-ratelimit-resource": "graphql",
                "x-ratelimit-remaining": "100",
                "x-ratelimit-limit": "5000",
                "x-ratelimit-reset": str(int(now + 3600)),
            },
            now=now,
        )
        self.assertFalse(budget.allows("graphql", Priority.BACKFILL, now=now + 1))
        self.assertTrue(budget.allows("graphql", Priority.HOT, now=now + 1))

        empty_cfg = self.tmp_path / "gh_empty"
        runner = make_token_gh_runner("ghs_secret_token", empty_cfg)
        self.assertTrue(empty_cfg.is_dir())
        self.assertEqual(empty_cfg.stat().st_mode & 0o777, 0o700)

        from types import SimpleNamespace

        captured_env: dict[str, str] = {}

        def _fake_bounded(cmd: object, **kwargs: object) -> SimpleNamespace:
            del cmd
            env = kwargs.get("env")
            if isinstance(env, dict):
                captured_env.update(env)
            return SimpleNamespace(
                returncode=0,
                stdout='HTTP/2.0 200 OK\r\n\r\n{"login":"octocat"}',
                stderr="",
                timed_out=False,
            )

        with mock.patch(
            "gh_transport.run_bounded_subprocess", side_effect=_fake_bounded
        ):
            tx = GhTransport(budget, NullApiCallStore(), runner=runner)
            resp = tx.rest_get("user", worker="auth", priority=Priority.HOT)
        self.assertEqual(resp.status, 200)
        self.assertEqual(captured_env.get("GH_TOKEN"), "ghs_secret_token")
        self.assertEqual(captured_env.get("GH_CONFIG_DIR"), str(empty_cfg))
        self.assertNotIn("GH_TOKEN", os.environ)

    def test_allowed_git_protocols_blocks_ssh_in_server_mode(self) -> None:
        with mock.patch.dict(os.environ, {"PACKRAT_GIT_ALLOWED_PROTOCOLS": "https"}):
            self.assertEqual(
                validate_url("https://github.com/octo-org/widgets.git"),
                "https://github.com/octo-org/widgets.git",
            )
            with self.assertRaises(ValueError):
                validate_url("git@github.com:octo-org/widgets.git")
            with self.assertRaises(ValueError):
                validate_url("ssh://git@github.com/octo-org/widgets.git")
            cfg_args = build_isolated_git_config_args()
            self.assertIn("protocol.allow=never", cfg_args)
            self.assertIn("protocol.https.allow=always", cfg_args)
            self.assertIn("protocol.ssh.allow=never", cfg_args)

    def test_pending_delete_hides_repo_across_store_fts_vectors_and_views(self) -> None:
        embedder = DeterministicHashEmbedder()
        r1 = self.store.register_repo(
            "octo-org", "keep-me", local_path=None, git_source="none"
        )
        r2 = self.store.register_repo(
            "octo-org", "drop-me", local_path=None, git_source="none"
        )

        for repo, label in ((r1, "keep-me"), (r2, "drop-me")):
            cid = f"{repo.full_name}#1:body"
            text = f"shared keyword alpha in {label}"
            vec = embedder.embed_texts([text])[0]
            self.store.upsert_pull_requests(
                [
                    PrRecord(
                        repo_id=repo.repo_id,
                        number=1,
                        title=f"Title {label}",
                        state="OPEN",
                        updated_at="2026-11-14T00:00:00Z",
                    )
                ]
            )
            self.store.upsert_chunks(
                [
                    TextChunk(
                        chunk_id=cid,
                        repo_id=repo.repo_id,
                        source_kind="pr",
                        source_ref=f"{repo.full_name}#1",
                        title=f"Title {label}",
                        text=text,
                        content_sha256=f"sha-{label}",
                        updated_at="2026-11-14T00:00:00Z",
                        item_number=1,
                    )
                ]
            )
            self.store.upsert_embeddings(
                [
                    StoredEmbeddingRecord(
                        chunk_id=cid,
                        model_id=embedder.model_id,
                        content_sha256=f"sha-{label}",
                        vector=tuple(vec),
                    )
                ]
            )

        rebuild_vector_mmap_cache(self.store)
        hits_before = hybrid_rrf_search(
            self.store, "shared keyword alpha", SearchFilters(), embedder=embedder
        )
        self.assertEqual(len(hits_before), 2)

        # Mark r2 pending delete while mmap cache still holds both vectors on disk!
        self.store.mark_repo_pending_delete(r2.repo_id)
        self.assertIsNone(self.store.get_repo("octo-org/drop-me"))
        self.assertIsNotNone(
            self.store.get_repo("octo-org/drop-me", include_pending_delete=True)
        )
        self.assertEqual(
            [r.full_name for r in self.store.list_repos()], ["octo-org/keep-me"]
        )
        with self.assertRaises(PendingDeleteError):
            self.store.ensure_items_repo("octo-org", "drop-me")

        # Unfiltered hybrid/vector search with allow_cache_rebuild=False must exclude r2.
        hits_after = hybrid_rrf_search(
            self.store,
            "shared keyword alpha",
            SearchFilters(),
            mode="vector",
            embedder=embedder,
            allow_cache_rebuild=False,
        )
        self.assertEqual([h.repo_full_name for h in hits_after], ["octo-org/keep-me"])
        hits_fts = hybrid_rrf_search(
            self.store,
            "shared keyword alpha",
            SearchFilters(),
            mode="fts",
            embedder=embedder,
        )
        self.assertEqual([h.repo_full_name for h in hits_fts], ["octo-org/keep-me"])
        wms = repo_watermarks(self.store)
        self.assertEqual(list(wms.keys()), ["octo-org/keep-me"])

        # SQLite views also hide r2.
        view_rows = self.store.conn.execute(
            "SELECT repo_full_name FROM v_pr_basic"
        ).fetchall()
        self.assertEqual([r[0] for r in view_rows], ["octo-org/keep-me"])

        # Removing a pending_delete repo succeeds cleanly.
        self.assertTrue(self.store.remove_repo("octo-org/drop-me"))
        self.assertEqual(self.store.pending_delete_repo_ids(), set())

    def test_private_repo_visibility_gates_across_workers(self) -> None:
        t0 = 1_760_000_000.0
        self.personal.link_identity("alice@example.com", "alice", verified=True, now=t0)

        # 1. ViewerSync skips isPrivate == True before ensure_items_repo.
        viewer_tx = _FakeGraphqlTransport(
            [
                {
                    "data": {
                        "viewer": {"login": "alice"},
                        "authored": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {
                                    "id": "PR_priv",
                                    "number": 9,
                                    "state": "OPEN",
                                    "repository": {
                                        "nameWithOwner": "secret-org/private-repo",
                                        "isPrivate": True,
                                    },
                                },
                                {
                                    "id": "PR_pub",
                                    "number": 10,
                                    "state": "OPEN",
                                    "repository": {
                                        "nameWithOwner": "octo-org/public-repo",
                                        "isPrivate": False,
                                    },
                                },
                            ],
                        },
                        "reviewRequested": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        },
                        "reviewed": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        },
                        "issues": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [],
                        },
                    }
                }
            ]
        )
        hot_tx = _FakeGraphqlTransport(
            [
                {
                    "data": {
                        "pr0": {
                            "id": "PR_pub",
                            "number": 10,
                            "title": "Public PR",
                            "state": "OPEN",
                            "updatedAt": "2026-11-14T00:00:00Z",
                            "repository": {
                                "nameWithOwner": "octo-org/public-repo",
                                "isPrivate": False,
                            },
                        }
                    }
                }
            ]
        )
        hot = HotRefresher(hot_tx, self.store, forbid_private=True)
        viewer = ViewerSync(viewer_tx, self.store, hot, forbid_private=True)
        changed = viewer.sync_all_users(self.personal, now=t0)
        self.assertEqual(changed, ["octo-org/public-repo#10"])
        self.assertIsNone(self.store.get_repo("secret-org/private-repo"))
        self.assertIsNotNone(self.store.get_repo("octo-org/public-repo"))

        # 2. SignalPoller skips private notifications before ensure_items_repo.
        notif_body = json.dumps(
            [
                {
                    "id": "n-priv",
                    "reason": "mention",
                    "unread": True,
                    "updated_at": "2026-11-14T01:00:00Z",
                    "subject": {
                        "type": "PullRequest",
                        "url": "https://api.github.com/repos/secret-org/priv2/pulls/3",
                    },
                    "repository": {"full_name": "secret-org/priv2", "private": True},
                },
                {
                    "id": "n-pub",
                    "reason": "review_requested",
                    "unread": True,
                    "updated_at": "2026-11-14T01:00:00Z",
                    "subject": {
                        "type": "PullRequest",
                        "url": "https://api.github.com/repos/octo-org/public-repo/pulls/10",
                    },
                    "repository": {
                        "full_name": "octo-org/public-repo",
                        "private": False,
                    },
                },
            ]
        )
        poller_tx = GhTransport(
            InMemoryRateBudget(),
            NullApiCallStore(),
            runner=lambda _args, timeout=30: GhCommandResult(
                returncode=0,
                stdout=f"HTTP/2.0 200 OK\r\nX-Poll-Interval: 60\r\nLast-Modified: Sat, 14 Nov 2026 01:00:00 GMT\r\n\r\n{notif_body}",
                stderr="",
            ),
        )
        poller = SignalPoller(poller_tx, self.store, forbid_private=True)
        dirty = poller.poll_user_notifications(
            self.personal, lambda _owner: poller_tx, now=t0 + 100
        )
        self.assertTrue(dirty.viewer)
        self.assertIsNone(self.store.get_repo("secret-org/priv2"))
        stored_notifs = self.personal.notifications_for_owner("alice@example.com")
        self.assertEqual([n["thread_id"] for n in stored_notifs], ["n-pub"])

    def test_reconcile_server_config_and_execute_reader_batch_all_ops(self) -> None:
        cfg_path = self.tmp_path / "server.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "allowlisted_orgs": ["octo-org"],
                    "admin_users": ["admin@example.com"],
                    "default_git_source": "managed",
                    "allowed_git_protocols": ["https"],
                    "disk_high_water_pct": 85.0,
                    "repos": [
                        {"full_name": "octo-org/widgets", "default_branch": "main"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        cfg = load_server_config(cfg_path)
        self.assertIsInstance(cfg, ServerConfig)
        seeded = reconcile_server_config(self.store, cfg)
        self.assertEqual(len(seeded), 1)
        repo = seeded[0]
        self.assertEqual(repo.git_source, "managed")

        # Populate data for all 8 reader_ops.
        now = 1_760_000_000.0
        self.store.write_heartbeat(1234, "packrat-serve/2+abcdef123456", now)
        self.personal.link_identity(
            "alice@example.com", "alice", verified=True, now=now
        )
        self.personal.replace_viewer_set(
            "alice", "review_requested", ["octo-org/widgets#42"], now=now
        )
        self.store.upsert_issues(
            [
                IssueRecord(
                    repo_id=repo.repo_id,
                    number=7,
                    title="Bug report",
                    state="OPEN",
                    updated_at="2026-11-13T00:00:00Z",
                )
            ]
        )
        self.store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=repo.repo_id,
                    number=42,
                    title="Add feature",
                    state="OPEN",
                    updated_at="2026-11-14T00:00:00Z",
                    author="bob",
                    body="Closes #7",
                    base_ref="main",
                    head_ref="feat-branch",
                    head_sha="deadbeef1234",
                    files=(
                        PrFileRecord(
                            repo_id=repo.repo_id,
                            pr_number=42,
                            file_path="src/app.py",
                        ),
                    ),
                    reviews=(
                        ReviewRecord(
                            repo_id=repo.repo_id,
                            review_id="rev1",
                            pr_number=42,
                            author="carol",
                            author_type="User",
                            state="APPROVED",
                            submitted_at="2026-11-14T00:05:00Z",
                        ),
                    ),
                    review_threads=(
                        ReviewThreadRecord(
                            thread_id="th1",
                            repo_id=repo.repo_id,
                            pr_number=42,
                            file_path="src/app.py",
                            line=10,
                            is_resolved=False,
                            reviewer="carol",
                            body="Please check edge case",
                            created_at="2026-11-14T00:04:00Z",
                            updated_at="2026-11-14T00:04:00Z",
                        ),
                    ),
                    issue_links=(
                        IssuePrLink(
                            repo_id=repo.repo_id,
                            pr_number=42,
                            issue_number=7,
                            link_type="closes",
                        ),
                    ),
                ),
                PrRecord(
                    repo_id=repo.repo_id,
                    number=43,
                    title="Stacked child",
                    state="OPEN",
                    updated_at="2026-11-14T00:10:00Z",
                    author="bob",
                    base_ref="feat-branch",
                    head_ref="feat-child",
                    head_sha="cafebabe5678",
                ),
            ]
        )
        self.store.upsert_pr_live(
            PrLiveRecord(
                repo_id=repo.repo_id,
                number=42,
                title="Add feature",
                state="OPEN",
                updated_at="2026-11-14T00:00:00Z",
                author="bob",
                base_ref="main",
                head_ref="feat-branch",
                head_sha="deadbeef1234",
            ),
            now=now,
        )
        self.store.replace_check_runs(
            repo.repo_id,
            42,
            "deadbeef1234",
            [
                CheckRunRecord(
                    name="ci", kind="CheckRun", status="COMPLETED", conclusion="SUCCESS"
                )
            ],
            now=now,
        )
        self.store.replace_review_requests(
            repo.repo_id,
            42,
            [ReviewRequestRecord(reviewer="alice", reviewer_kind="User")],
        )
        self.store.replace_latest_opinionated_reviews(
            repo.repo_id,
            42,
            [
                OpinionatedReviewRecord(
                    author="carol", author_type="User", state="APPROVED"
                )
            ],
        )
        self.store.replace_latest_reviews(
            repo.repo_id,
            42,
            [
                LatestReviewRecord(
                    author="carol",
                    author_type="User",
                    state="APPROVED",
                    submitted_at="2026-11-14T00:05:00Z",
                    commit_oid="deadbeef1234",
                )
            ],
            now=now,
        )
        self.store.replace_thread_activity(
            repo.repo_id,
            42,
            [ThreadActivityRecord(thread_id="th1", is_resolved=False)],
        )
        self.store.upsert_commits(
            [
                CommitRecord(
                    repo_id=repo.repo_id,
                    sha="1111222233334444555566667777888899990000",
                    committed_at="2026-11-12T00:00:00Z",
                    title="Initial app",
                    author_name="Carol Dev",
                    author_email="carol@example.com",
                    pr_number=42,
                    files=(
                        CommitFileRecord(
                            repo_id=repo.repo_id,
                            sha="1111222233334444555566667777888899990000",
                            file_path="src/app.py",
                        ),
                    ),
                )
            ]
        )

        self.store.upsert_chunks(
            [
                TextChunk(
                    chunk_id="octo-org/widgets#42:body",
                    repo_id=repo.repo_id,
                    source_kind="pr",
                    source_ref="octo-org/widgets#42",
                    title="Add feature",
                    text="Closes #7",
                    content_sha256="sha-42",
                    updated_at="2026-11-14T00:00:00Z",
                    item_number=42,
                )
            ]
        )

        res = execute_reader_batch(
            self.store,
            {
                "health": True,
                "viewer_items": {"sets": ["review_requested"]},
                "items": {
                    "keys": ["octo-org/widgets#42"],
                },
                "known_keys": {"keys": ["octo-org/widgets#42", "octo-org/widgets#999"]},
                "stack_neighbors": {
                    "keys": ["octo-org/widgets#42"],
                },
                "repo_probe": {"repo": "octo-org/widgets"},
                "context": {
                    "repo": "octo-org/widgets",
                    "files": ["src/app.py"],
                },
                "review_signals": {
                    "repo": "octo-org/widgets",
                    "pr_number": 42,
                },
            },
            personal_store=self.personal,
            owner_id="alice@example.com",
            viewer_login="alice",
            now=now + 5,
        )
        self.assertEqual(res["health"]["viewer_login"], "alice")
        self.assertEqual(
            res["health"]["viewer_sets_synced_at"],
            {"review_requested": "2025-10-09T08:53:20Z"},
        )
        self.assertEqual(len(res["viewer_items"]), 1)
        self.assertEqual(res["viewer_items"][0]["state"], "OPEN")
        self.assertEqual(
            [r["item_key"] for r in res["items"]["pr_live"]],
            ["octo-org/widgets#42"],
        )
        self.assertEqual(
            res["known_keys"], {"octo-org/widgets#42": "octo-org/widgets#42"}
        )
        self.assertEqual(
            res["stack_neighbors"]["octo-org/widgets#42"],
            ["octo-org/widgets#42", "octo-org/widgets#43"],
        )
        self.assertTrue(res["repo_probe"]["found"])
        self.assertEqual(len(res["context"]["prior_review_threads"]), 1)
        self.assertIn(
            "<untrusted_repo_content",
            res["context"]["prior_review_threads"][0]["untrusted_excerpt"],
        )
        sig = res["review_signals"]
        self.assertEqual(sig["status"], "ok")
        self.assertEqual(sig["target_pr_files"], ["src/app.py"])
        self.assertIn("carol", sig["known_logins"])
        self.assertEqual(
            sig["sha_to_login"].get("1111222233334444555566667777888899990000"),
            "bob",
        )

    def test_service_drains_user_refresh_and_pending_deletes(self) -> None:
        now = 1_760_000_000.0
        repo = self.store.register_repo(
            "octo-org", "widgets", local_path=None, git_source="none"
        )
        to_drop = self.store.register_repo(
            "octo-org", "obsolete", local_path=None, git_source="none"
        )
        self.store.mark_repo_pending_delete(to_drop.repo_id)

        self.personal.request_refresh(
            "alice@example.com", ["octo-org/widgets#5"], now=now
        )
        used_transports: list[object] = []
        alice_token_obj = object()

        class _RecordingHot:
            def refresh_hot(
                self, keys: list[str], *, transport: object | None = None
            ) -> list[str]:
                used_transports.append(transport)
                return list(keys)

        class _NoopSignals:
            poll_interval_s = 60

            def poll_notifications(self, _now: float) -> None:
                return None

            def poll_repo_events(self, _now: float) -> None:
                return None

            def poll_check_runs(self, _leases: object, _now: float) -> None:
                return None

        class _NoopViewer:
            def refresh_viewer(self, lookback_days: int = 7) -> list[str]:
                del lookback_days
                return []

        class _NoopMirror:
            def sync_mirror(self, _repo_ids: object) -> list[object]:
                return []

        rebuilt: list[bool] = []
        svc = PackratService(
            self.store,
            _NoopSignals(),  # type: ignore[arg-type]
            _RecordingHot(),
            _NoopViewer(),
            _NoopMirror(),
            child_argv=None,
            clock=lambda: now,
            log=lambda _m: None,
            personal_store=self.personal,
            transport_for_owner=lambda owner: (
                alice_token_obj if owner == "alice@example.com" else None
            ),
            rebuild_mmap_fn=lambda _s: rebuilt.append(True),
        )
        report = svc.run_once(now)
        self.assertEqual(report.requested, ["octo-org/widgets#5"])
        self.assertEqual(used_transports, [alice_token_obj])
        self.assertIsNone(
            self.store.get_repo_by_id(to_drop.repo_id, include_pending_delete=True)
        )
        self.assertIsNotNone(self.store.get_repo_by_id(repo.repo_id))
        self.assertTrue(rebuilt)


if __name__ == "__main__":
    unittest.main()
