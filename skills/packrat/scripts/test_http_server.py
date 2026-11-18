"""Offline unit and integration tests for `http_server.py` (Track 2A)."""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from db_store import PackratStore
from embedder import DeterministicHashEmbedder
from http_server import (
    SECURITY_HEADERS,
    ApiError,
    DuckDbSqlSandbox,
    PackratHttpServer,
    TokenRegistry,
    sign_edge_request,
)
from models import (
    CommitFileRecord,
    CommitRecord,
    IssueRecord,
    NotificationRecord,
    PrLiveRecord,
    PrRecord,
    ReviewThreadRecord,
    ServerConfig,
    StoredEmbeddingRecord,
    TextChunk,
    compute_content_sha256,
)
from personal_store import ChangeNotifier, PersonalStore

REQUIRED_SECURITY_HEADERS: dict[str, str] = dict(SECURITY_HEADERS)


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    body: Mapping[str, Any] | bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        req_headers = dict(headers or {})
        payload: bytes | None = None
        if isinstance(body, bytes):
            payload = body
            req_headers.setdefault("Content-Length", str(len(payload)))
        elif body is not None:
            payload = json.dumps(body).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
            req_headers.setdefault("Content-Length", str(len(payload)))
        elif method == "POST":
            payload = b"{}"
            req_headers.setdefault("Content-Type", "application/json")
            req_headers.setdefault("Content-Length", "2")
        conn.request(method, path, body=payload, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, resp_headers, raw
    finally:
        conn.close()


def _assert_security_headers(tc: unittest.TestCase, headers: Mapping[str, str]) -> None:
    for name, expected in REQUIRED_SECURITY_HEADERS.items():
        tc.assertEqual(headers.get(name.lower()), expected, f"missing/mismatch {name}")


class _HttpServerFixture(unittest.TestCase):
    """Base fixture starting a loopback `127.0.0.1:0` `PackratHttpServer`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "repos.sqlite"
        self.personal_db_path = self.tmp_path / "personal.sqlite"
        self.embedder = DeterministicHashEmbedder()
        self.public_repos: set[str] = {
            "flutter/flutter",
            "dart-lang/sdk",
            "a2ui-project/a2ui",
            "octo-org/public-items",
            "custom-org/admin-mirror",
        }
        self.visibility_calls: list[str] = []
        self.token_calls: list[str] = []
        self.tokens: dict[str, str] = {
            "tok_alice_v1": "alice-gh",
            "tok_alice_v2": "alice-gh",
            "tok_bob": "bob-gh",
        }
        self.disk_used_pct = 42.0
        self.notifier = ChangeNotifier(max_waiters=64)
        self._seed_shared_store()
        self.server = self._make_server()
        self.host, self.port = (
            str(self.server.server_address[0]),
            int(self.server.server_address[1]),
        )
        self._thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self._thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5.0)
        self._tmp.cleanup()

    def _seed_shared_store(self) -> None:
        personal = PersonalStore(self.personal_db_path, notifier=self.notifier)
        store = PackratStore(self.db_path, personal_store=personal)
        try:
            store.write_heartbeat(os.getpid(), "packrat-serve/2+test00000000")
            repo = store.register_repo(
                "flutter", "flutter", local_path=None, git_source="none"
            )
            self.seeded_repo = repo
            store.upsert_pull_requests(
                [
                    PrRecord(
                        repo_id=repo.repo_id,
                        number=101,
                        title="Fix engine frame scheduling",
                        state="OPEN",
                        updated_at="2026-11-14T01:00:00Z",
                        author="alice-gh",
                        body="Detailed PR body prose from contributor",
                        url="https://github.com/flutter/flutter/pull/101",
                        review_threads=(
                            ReviewThreadRecord(
                                thread_id="RT_1",
                                repo_id=repo.repo_id,
                                pr_number=101,
                                file_path="engine/scheduler.cc",
                                line=42,
                                is_resolved=False,
                                reviewer="bob-gh",
                                body="Please guard null frame pointer.",
                            ),
                        ),
                    )
                ]
            )
            store.upsert_pr_live(
                PrLiveRecord(
                    repo_id=repo.repo_id,
                    number=101,
                    title="Fix engine frame scheduling",
                    state="OPEN",
                    updated_at="2026-11-14T01:00:00Z",
                    author="alice-gh",
                    head_sha="abc1234",
                    base_ref="main",
                    head_ref="fix-sched",
                )
            )
            store.upsert_issues(
                [
                    IssueRecord(
                        repo_id=repo.repo_id,
                        number=202,
                        title="Frame jitter on startup",
                        state="OPEN",
                        updated_at="2026-11-14T00:30:00Z",
                        body="Steps to reproduce jitter",
                    )
                ]
            )
            store.upsert_commits(
                [
                    CommitRecord(
                        repo_id=repo.repo_id,
                        sha="deadbeef1234",
                        committed_at="2026-11-14T01:05:00Z",
                        title="Fix scheduler race",
                        author_name="Alice Dev",
                        author_email="alice@example.com",
                        message="Fix scheduler race\n\nFull commit message.",
                        pr_number=101,
                        files=(
                            CommitFileRecord(
                                repo_id=repo.repo_id,
                                sha="deadbeef1234",
                                file_path="engine/scheduler.cc",
                                additions=12,
                                deletions=3,
                            ),
                        ),
                    )
                ]
            )
            text = "scheduler frame race condition in engine"
            vec = self.embedder.embed_texts([text])[0]
            sha = compute_content_sha256("Fix engine frame scheduling", text)
            cid = "flutter/flutter#101:body"
            store.upsert_chunks(
                [
                    TextChunk(
                        chunk_id=cid,
                        repo_id=repo.repo_id,
                        source_kind="pr",
                        source_ref="flutter/flutter#101",
                        title="Fix engine frame scheduling",
                        text=text,
                        content_sha256=sha,
                        updated_at="2026-11-14T01:00:00Z",
                        item_number=101,
                    )
                ]
            )
            store.upsert_embeddings(
                [
                    StoredEmbeddingRecord(
                        chunk_id=cid,
                        model_id=self.embedder.model_id,
                        content_sha256=sha,
                        vector=tuple(vec),
                    )
                ]
            )
            store.update_repo_sync_state(
                repo.repo_id,
                prs_watermark="2026-11-14T01:00:00Z",
                last_indexed_sha="deadbeef1234",
            )
        finally:
            store.close()
            personal.close()

    def _check_public(self, owner: str, name: str, _tx: Any) -> bool:
        slug = f"{owner}/{name}"
        self.visibility_calls.append(slug)
        return slug.lower() in {s.lower() for s in self.public_repos}

    def _verify_token(self, token: str) -> str:
        self.token_calls.append(token)
        if token not in self.tokens:
            raise ValueError("bad token")
        return self.tokens[token]

    def _disk_stats(self, _path: Path) -> tuple[int, float]:
        return 50 * 1024 * 1024 * 1024, self.disk_used_pct

    def _make_server(self, **overrides: Any) -> PackratHttpServer:
        cfg = overrides.pop(
            "server_config",
            ServerConfig(
                allowlisted_orgs=("a2ui-project", "flutter", "dart-lang"),
                admin_users=("admin@example.com",),
                allowed_git_protocols=("https",),
                disk_high_water_pct=85.0,
            ),
        )
        return PackratHttpServer(
            ("127.0.0.1", 0),
            self.db_path,
            self.personal_db_path,
            server_config=cfg,
            notifier=self.notifier,
            token_verifier=self._verify_token,
            public_repo_checker=self._check_public,
            embedder=self.embedder,
            disk_stats_fn=self._disk_stats,
            **overrides,
        )

    def req_json(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        status, resp_headers, raw = _request(
            self.host,
            self.port,
            method,
            path,
            body=body,
            headers=headers,
            timeout=timeout,
        )
        _assert_security_headers(self, resp_headers)
        return status, resp_headers, json.loads(raw.decode("utf-8"))


class TestHygieneAndSecurityHeaders(_HttpServerFixture):
    def test_zero_corp_strings_and_zero_direct_execute_calls(self) -> None:
        scripts_dir = Path(__file__).resolve().parent
        banned = tuple(
            "".join(parts)
            for parts in (
                ("sso", "_client"),
                (".", "goog"),
                ("uber", "proxy"),
                ("corp", "run"),
                ("google", "3"),
            )
        )
        for fname in ("http_server.py", "test_http_server.py"):
            text = (scripts_dir / fname).read_text(encoding="utf-8").lower()
            for token in banned:
                self.assertNotIn(token, text)

        srv_text = (scripts_dir / "http_server.py").read_text(encoding="utf-8")
        for forbidden in (".execute(", ".executemany(", ".executescript("):
            self.assertNotIn(forbidden, srv_text)

    def test_security_headers_body_cap_and_loopback_spoof_stripping(self) -> None:
        self.assertEqual(self.host, "127.0.0.1")
        # Spoofed X-Packrat-* headers are ignored when trust_edge_proxy=False.
        status, _, health = self.req_json(
            "GET",
            "/api/v1/health",
            headers={
                "X-Packrat-User": "admin@example.com",
                "X-Packrat-Edge-Sig": "kid=k1,ts=1,sig=deadbeef",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(health["owner_id"], "local")
        self.assertEqual(health["schema_version"], 15)

        # Oversized body (> 1 MiB) returns 413 with security headers.
        big_body = b"x" * (1024 * 1024 + 16)
        status_big, _, err_big = self.req_json(
            "POST",
            "/api/v1/search",
            body=big_body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status_big, 413)
        self.assertEqual(err_big["error"]["code"], "payload_too_large")

        # 404 route still carries security headers and leaks no paths.
        status_404, _, err_404 = self.req_json("GET", "/api/v1/nonexistent")
        self.assertEqual(status_404, 404)
        self.assertEqual(err_404["error"]["code"], "not_found")
        self.assertNotIn(str(self.tmp_path), json.dumps(err_404))


class TestEdgeAuthAndTokens(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "repos.sqlite"
        self.personal_db_path = self.tmp_path / "personal.sqlite"
        self.now = 1_760_000_000.0
        self.verify_calls: list[str] = []
        self.hmac_env_json = json.dumps(
            {
                "active_kid": "k2",
                "keys": {"k1": "old-hmac-secret", "k2": "new-hmac-secret"},
            }
        )
        self._env_patch = mock.patch.dict(
            os.environ, {"PACKRAT_EDGE_HMAC_KEYS_JSON": self.hmac_env_json}
        )
        self._env_patch.start()

        def _verify(tok: str) -> str:
            self.verify_calls.append(tok)
            mapping = {
                "ghs_alice_1": "alice-gh",
                "ghs_alice_2": "alice-gh",
                "ghs_bob_1": "bob-gh",
            }
            if tok not in mapping:
                raise ValueError("invalid")
            return mapping[tok]

        self.srv = PackratHttpServer(
            "127.0.0.1:0",
            self.db_path,
            self.personal_db_path,
            trust_edge_proxy=True,
            token_verifier=_verify,
            public_repo_checker=lambda _o, _n, _t: True,
            clock=lambda: self.now,
        )
        self.host, self.port = (
            str(self.srv.server_address[0]),
            int(self.srv.server_address[1]),
        )
        self._t = threading.Thread(
            target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._t.start()

    def tearDown(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()
        self._t.join(timeout=5.0)
        self._env_patch.stop()
        self._tmp.cleanup()

    def _edge_headers(
        self,
        user: str,
        method: str,
        path: str,
        *,
        kid: str = "k2",
        secret: str = "new-hmac-secret",
        ts: int | None = None,
        bearer: str | None = None,
    ) -> dict[str, str]:
        stamp = int(self.now) if ts is None else ts
        hdrs = {
            "X-Packrat-User": user,
            "X-Packrat-Edge-Sig": sign_edge_request(
                kid, secret, stamp, method, path, user
            ),
        }
        if bearer is not None:
            hdrs["Authorization"] = f"Bearer {bearer}"
        return hdrs

    def test_hmac_kid_rotation_query_tampering_and_skew(self) -> None:
        # Both active k2 and rotated-out k1 succeed within 60s skew.
        for kid, secret in (("k1", "old-hmac-secret"), ("k2", "new-hmac-secret")):
            st, hdrs, body = _request(
                self.host,
                self.port,
                "GET",
                "/api/v1/changes?since=0&limit=10",
                headers=self._edge_headers(
                    "Alice@Example.com",
                    "GET",
                    "/api/v1/changes?since=0&limit=10",
                    kid=kid,
                    secret=secret,
                ),
            )
            _assert_security_headers(self, hdrs)
            self.assertEqual(st, 200)
            self.assertIn("seq", json.loads(body))

        # Query-string tampering (?since=0 signed, ?since=5 requested) -> 401.
        st_tamper, _, _ = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/changes?since=5&limit=10",
            headers=self._edge_headers(
                "alice@example.com", "GET", "/api/v1/changes?since=0&limit=10"
            ),
        )
        self.assertEqual(st_tamper, 401)

        # Clock skew (+61s and -61s) -> 401; +59s -> 200.
        for bad_ts in (int(self.now) + 61, int(self.now) - 61):
            st_skew, _, _ = _request(
                self.host,
                self.port,
                "GET",
                "/api/v1/health",
                headers=self._edge_headers(
                    "alice@example.com", "GET", "/api/v1/health", ts=bad_ts
                ),
            )
            self.assertEqual(st_skew, 401)

        st_ok, _, _ = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/health",
            headers=self._edge_headers(
                "alice@example.com", "GET", "/api/v1/health", ts=int(self.now) - 59
            ),
        )
        self.assertEqual(st_ok, 200)

        # Unknown kid and missing headers -> 401.
        st_kid, _, _ = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/health",
            headers=self._edge_headers(
                "alice@example.com",
                "GET",
                "/api/v1/health",
                kid="k99",
                secret="new-hmac-secret",
            ),
        )
        self.assertEqual(st_kid, 401)
        st_missing, _, _ = _request(self.host, self.port, "GET", "/api/v1/health")
        self.assertEqual(st_missing, 401)

    def test_token_registry_sliding_cache_and_notification_isolation(self) -> None:
        empty_cfg = self.srv.token_registry.empty_config_dir
        self.assertTrue(empty_cfg.is_dir())
        self.assertEqual(empty_cfg.stat().st_mode & 0o777, 0o700)

        # Invalid bearer token -> 401.
        st_bad, _, _ = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/health",
            headers=self._edge_headers(
                "alice@example.com", "GET", "/api/v1/health", bearer="bad_tok"
            ),
        )
        self.assertEqual(st_bad, 401)
        self.verify_calls.clear()

        # Alice links with verified bearer token; second call slides TTL without re-verifying.
        st1, _, link_body = _request(
            self.host,
            self.port,
            "POST",
            "/api/v1/auth/link",
            body={},
            headers=self._edge_headers(
                "alice@example.com", "POST", "/api/v1/auth/link", bearer="ghs_alice_1"
            ),
        )
        self.assertEqual(st1, 200)
        self.assertTrue(json.loads(link_body)["login_verified"])
        self.assertEqual(self.verify_calls, ["ghs_alice_1"])
        self.assertIsNotNone(
            self.srv.token_registry.transport_for_owner("alice@example.com")
        )

        self.now += 200.0
        st2, _, _ = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/health",
            headers=self._edge_headers(
                "alice@example.com", "GET", "/api/v1/health", bearer="ghs_alice_1"
            ),
        )
        self.assertEqual(st2, 200)
        self.assertEqual(self.verify_calls, ["ghs_alice_1"])

        # Token hash rotation triggers fresh verification; expiry clears transport_for_owner.
        st3, _, _ = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/health",
            headers=self._edge_headers(
                "alice@example.com", "GET", "/api/v1/health", bearer="ghs_alice_2"
            ),
        )
        self.assertEqual(st3, 200)
        self.assertEqual(self.verify_calls, ["ghs_alice_1", "ghs_alice_2"])

        self.now += 301.0
        self.assertIsNone(
            self.srv.token_registry.transport_for_owner("alice@example.com")
        )

        # Mallory cannot claim Alice's active verified handle without a bearer token -> 409.
        st_conflict, _, err_conflict = _request(
            self.host,
            self.port,
            "POST",
            "/api/v1/auth/link",
            body={"github_login": "alice-gh"},
            headers=self._edge_headers(
                "mallory@example.com", "POST", "/api/v1/auth/link"
            ),
        )
        self.assertEqual(st_conflict, 409)
        self.assertEqual(json.loads(err_conflict)["error"]["code"], "identity_conflict")

        # Bob links unverified (`login_verified=0`); notifications stay hidden from Bob!
        st_bob, _, bob_link = _request(
            self.host,
            self.port,
            "POST",
            "/api/v1/auth/link",
            body={"github_login": "bob-unverified"},
            headers=self._edge_headers("bob@example.com", "POST", "/api/v1/auth/link"),
        )
        self.assertEqual(st_bob, 200)
        self.assertFalse(json.loads(bob_link)["login_verified"])

        ps = PersonalStore(self.personal_db_path, notifier=self.srv.notifier)
        store = PackratStore(self.db_path, personal_store=ps)
        try:
            store.ensure_items_repo("octo-org", "widgets")
            ps.upsert_notifications(
                "alice@example.com",
                [
                    NotificationRecord(
                        thread_id="nt_alice",
                        reason="review_requested",
                        subject_type="PullRequest",
                        item_key="octo-org/widgets#1",
                        updated_at="2026-11-14T02:00:00Z",
                        unread=True,
                    )
                ],
                now=self.now,
            )
            ps.upsert_notifications(
                "bob@example.com",
                [
                    NotificationRecord(
                        thread_id="nt_bob",
                        reason="mention",
                        subject_type="PullRequest",
                        item_key="octo-org/widgets#1",
                        updated_at="2026-11-14T02:00:00Z",
                        unread=True,
                    )
                ],
                now=self.now,
            )
        finally:
            store.close()
            ps.close()

        # Alice sees only her notification via /reader/batch and /sql; Bob sees 0!
        _, _, alice_sql = _request(
            self.host,
            self.port,
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT thread_id FROM v_notifications", "raw": True},
            headers=self._edge_headers("alice@example.com", "POST", "/api/v1/sql"),
        )
        self.assertEqual(json.loads(alice_sql)["rows"], [{"thread_id": "nt_alice"}])

        _, _, bob_sql = _request(
            self.host,
            self.port,
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT thread_id FROM v_notifications", "raw": True},
            headers=self._edge_headers("bob@example.com", "POST", "/api/v1/sql"),
        )
        self.assertEqual(json.loads(bob_sql)["rows"], [])


class TestDuckDbSqlSandbox(_HttpServerFixture):
    def test_positive_views_wrapping_and_trailing_semicolon_describe(self) -> None:
        # 1. Positive shared-view query with default untrusted prose wrapping.
        st1, _, pr_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT repo, number, title, body FROM v_pull_requests"},
        )
        self.assertEqual(st1, 200)
        self.assertEqual(len(pr_env["rows"]), 1)
        row = pr_env["rows"][0]
        self.assertEqual(row["repo"], "flutter/flutter")
        self.assertEqual(row["number"], 101)
        self.assertIn("<untrusted_repo_content", row["title"])
        self.assertIn("<untrusted_repo_content", row["body"])
        self.assertEqual(
            [c["name"] for c in pr_env["columns"]], ["repo", "number", "title", "body"]
        )

        # 2. raw=True skips XML wrapping; v_file_hotspots and v_api_calls work.
        st2, _, hot_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={
                "query": "SELECT repo, file_path, commit_count, unresolved_thread_count FROM v_file_hotspots",
                "raw": True,
            },
        )
        self.assertEqual(st2, 200)
        self.assertEqual(
            hot_env["rows"],
            [
                {
                    "repo": "flutter/flutter",
                    "file_path": "engine/scheduler.cc",
                    "commit_count": 1,
                    "unresolved_thread_count": 1,
                }
            ],
        )

        st_api, _, api_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT * FROM v_api_calls;"},
        )
        self.assertEqual(st_api, 200)
        self.assertEqual(api_env["rows"], [])
        col_names = {c["name"] for c in api_env["columns"]}
        self.assertTrue(
            {
                "at",
                "worker",
                "kind",
                "target",
                "status",
                "cost",
                "remaining",
                "reset_at",
                "duration_ms",
            }
            <= col_names
        )

        # 3. Trailing-semicolon 0-row SELECT populates `columns` from DESCRIBE!
        st3, _, empty_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={
                "query": "SELECT repo, number, title FROM v_pull_requests WHERE 1=0;   \n"
            },
        )
        self.assertEqual(st3, 200)
        self.assertEqual(empty_env["rows"], [])
        self.assertEqual(
            [c["name"] for c in empty_env["columns"]], ["repo", "number", "title"]
        )
        self.assertTrue(all(c["type"] for c in empty_env["columns"]))

        # Multi-statement 0-row SQL falls back cleanly to columns: [].
        st4, _, multi_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT 1; SELECT repo FROM v_pull_requests WHERE 1=0;"},
        )
        self.assertEqual(st4, 200)
        self.assertEqual(multi_env, {"rows": [], "columns": []})

    def test_sandbox_escapes_blocked_and_cross_request_isolation(self) -> None:
        marker_file = self.tmp_path / "pwned.txt"
        escape_queries = [
            f".shell touch {marker_file}",
            ".read /etc/hostname",
            f".open {self.personal_db_path}",
            "-help",
            "-init /etc/hostname",
            "SELECT * FROM read_text('/etc/hostname')",
            "SELECT * FROM read_csv('/etc/hosts')",
            f"COPY (SELECT 1) TO '{self.tmp_path / 'out.csv'}'",
            f"ATTACH '{self.personal_db_path}' AS p (TYPE SQLITE)",
            "SET enable_external_access = true",
            "SET lock_configuration = false",
            "INSERT INTO kb.repos (owner, name, full_name, created_at, updated_at) VALUES ('x','y','x/y','a','b')",
        ]
        for bad_sql in escape_queries:
            with self.subTest(sql=bad_sql):
                st, _, err = self.req_json(
                    "POST", "/api/v1/sql", body={"query": bad_sql}
                )
                self.assertEqual(st, 400, f"Expected 400 for {bad_sql!r}, got {err}")
                self.assertNotIn(str(self.tmp_path), json.dumps(err))
        self.assertFalse(marker_file.exists())

        # SET VARIABLE override and raw `kb.notifications` expose zero cross-user rows!
        ps = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            ps.link_identity("victim@example.com", "victim", verified=True)
            ps.replace_viewer_set("victim", "authored", ["flutter/flutter#101"])
            ps.upsert_notifications(
                "victim@example.com",
                [
                    NotificationRecord(
                        thread_id="secret_thread",
                        reason="security_alert",
                        subject_type="PullRequest",
                        item_key="flutter/flutter#101",
                        updated_at="2026-11-14T01:00:00Z",
                        unread=True,
                    )
                ],
            )
        finally:
            ps.close()

        st_var, _, var_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={
                "query": "SET VARIABLE packrat_viewer = 'victim'; SELECT * FROM v_viewer_items;",
                "raw": True,
            },
        )
        if st_var == 200:
            self.assertEqual(var_env["rows"], [])

        st_kb, _, kb_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT * FROM kb.notifications", "raw": True},
        )
        self.assertEqual(st_kb, 200)
        self.assertEqual(kb_env["rows"], [])

        # Cross-request CREATE TABLE isolation on a single-slot semaphore.
        single_slot = DuckDbSqlSandbox(self.db_path, max_concurrent=1)
        read_store = PackratStore(self.db_path, read_only=True)
        personal_store = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            res_create = single_slot.run_query(
                "CREATE TABLE leaked_tbl AS SELECT 999 AS secret; SELECT * FROM leaked_tbl;",
                read_store=read_store,
                personal_store=personal_store,
                owner_id="local",
                viewer_login="",
                raw=True,
            )
            self.assertEqual(res_create["rows"], [{"secret": 999}])
            with self.assertRaises(ApiError):
                single_slot.run_query(
                    "SELECT * FROM leaked_tbl",
                    read_store=read_store,
                    personal_store=personal_store,
                    owner_id="local",
                    viewer_login="",
                    raw=True,
                )
        finally:
            read_store.close()
            personal_store.close()

    def test_sql_row_cap_stdout_cap_and_timeout(self) -> None:
        # limit_rows / too_many_rows -> 413
        st_rows, _, err_rows = self.req_json(
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT * FROM range(25)", "limit_rows": 10},
        )
        self.assertEqual(st_rows, 413)
        self.assertEqual(err_rows["error"]["code"], "too_many_rows")

        # 16 MiB stdout byte cap (tested with 2 KiB cap) -> 413 result_too_large
        small_sandbox = DuckDbSqlSandbox(self.db_path, max_stdout_bytes=2048)
        self.server.sql_sandbox = small_sandbox
        st_bytes, _, err_bytes = self.req_json(
            "POST",
            "/api/v1/sql",
            body={"query": "SELECT repeat('a', 500) AS s FROM range(20)"},
        )
        self.assertEqual(st_bytes, 413)
        self.assertEqual(err_bytes["error"]["code"], "result_too_large")

        # Timeout -> 504 query_timeout
        fast_timeout_sandbox = DuckDbSqlSandbox(self.db_path, timeout_s=0.25)
        self.server.sql_sandbox = fast_timeout_sandbox
        st_to, _, err_to = self.req_json(
            "POST",
            "/api/v1/sql",
            body={
                "query": "SELECT count(*) FROM range(50000000) a CROSS JOIN range(500) b"
            },
        )
        self.assertEqual(st_to, 504)
        self.assertEqual(err_to["error"]["code"], "query_timeout")


class TestRoutesVisibilityAndConcurrency(_HttpServerFixture):
    def test_repos_guards_ssrf_allowlist_disk_and_pending_delete(self) -> None:
        # GET /api/v1/repos excludes git_url and local_path.
        st_list, _, list_body = self.req_json("GET", "/api/v1/repos?mode=mirror")
        self.assertEqual(st_list, 200)
        self.assertEqual(len(list_body["repos"]), 1)
        self.assertNotIn("git_url", list_body["repos"][0])
        self.assertNotIn("local_path", list_body["repos"][0])

        # Non-admin `local"` user rejected on git_url, local_path, and unallowlisted org.
        st_ssrf, _, err_ssrf = self.req_json(
            "POST",
            "/api/v1/repos",
            body={
                "full_name": "flutter/genui",
                "git_url": "https://internal.invalid/repo.git",
            },
        )
        self.assertEqual(st_ssrf, 403)
        self.assertEqual(err_ssrf["error"]["code"], "forbidden")

        st_path, _, _ = self.req_json(
            "POST",
            "/api/v1/repos",
            body={"full_name": "flutter/genui", "local_path": "/etc"},
        )
        self.assertEqual(st_path, 403)

        st_org, _, _ = self.req_json(
            "POST",
            "/api/v1/repos",
            body={"full_name": "unlisted-org/secret"},
        )
        self.assertEqual(st_org, 403)

        # Private repo rejected with 403 private_repository.
        st_priv, _, err_priv = self.req_json(
            "POST",
            "/api/v1/repos",
            body={"full_name": "flutter/private-repo"},
        )
        self.assertEqual(st_priv, 403)
        self.assertEqual(err_priv["error"]["code"], "private_repository")

        # Disk high-water cap -> 503 disk_high_water.
        self.disk_used_pct = 86.5
        st_disk, _, err_disk = self.req_json(
            "POST",
            "/api/v1/repos",
            body={"full_name": "dart-lang/sdk", "git_source": "none"},
        )
        self.assertEqual(st_disk, 503)
        self.assertEqual(err_disk["error"]["code"], "disk_high_water")
        self.disk_used_pct = 42.0

        # Allowlisted public repo succeeds for non-admin without git_url.
        st_add, _, add_body = self.req_json(
            "POST",
            "/api/v1/repos",
            body={"full_name": "dart-lang/sdk", "git_source": "none"},
        )
        self.assertEqual(st_add, 200)
        self.assertEqual(add_body["repo"]["full_name"], "dart-lang/sdk")

        # Non-admin DELETE -> 403; Admin DELETE -> 202 and hides repo everywhere.
        st_del_forbid, _, _ = self.req_json("DELETE", "/api/v1/repos/dart-lang/sdk")
        self.assertEqual(st_del_forbid, 403)

        # Configure local as admin to verify admin git_url SSH block + async DELETE.
        self.server.server_config = ServerConfig(
            allowlisted_orgs=("flutter", "dart-lang"),
            admin_users=("local",),
            allowed_git_protocols=("https",),
        )
        st_ssh, _, _ = self.req_json(
            "POST",
            "/api/v1/repos",
            body={
                "full_name": "custom-org/admin-mirror",
                "git_url": "git@github.com:custom-org/admin-mirror.git",
            },
        )
        self.assertEqual(st_ssh, 400)

        st_https, _, _ = self.req_json(
            "POST",
            "/api/v1/repos",
            body={
                "full_name": "custom-org/admin-mirror",
                "git_url": "https://github.com/custom-org/admin-mirror.git",
            },
        )
        self.assertEqual(st_https, 200)

        st_del_ok, _, del_body = self.req_json("DELETE", "/api/v1/repos/dart-lang/sdk")
        self.assertEqual(st_del_ok, 202)
        self.assertEqual(del_body["status"], "pending_delete")

        # Re-adding or watching or refreshing a pending_delete repo -> 409 delete_pending!
        for path, payload in (
            ("/api/v1/repos", {"full_name": "dart-lang/sdk"}),
            (
                "/api/v1/watches",
                {
                    "op": "add",
                    "consumer": "monitor",
                    "consumer_id": "c1",
                    "keys": ["dart-lang/sdk#1"],
                },
            ),
            ("/api/v1/refresh", {"keys": ["dart-lang/sdk#1"]}),
        ):
            st_409, _, err_409 = self.req_json("POST", path, body=payload)
            self.assertEqual(st_409, 409)
            self.assertEqual(err_409["error"]["code"], "delete_pending")

    def test_watches_and_refresh_block_private_repos_before_ensure_items_repo(
        self,
    ) -> None:
        # Unknown private slug on BOTH /watches and /refresh must return 403 WITHOUT creating a row!
        st_w, _, err_w = self.req_json(
            "POST",
            "/api/v1/watches",
            body={
                "op": "add",
                "consumer": "monitor",
                "consumer_id": "m1",
                "keys": ["secret-corp/private-app#7"],
            },
        )
        self.assertEqual(st_w, 403)
        self.assertEqual(err_w["error"]["code"], "private_repository")

        st_r, _, err_r = self.req_json(
            "POST",
            "/api/v1/refresh",
            body={"keys": ["secret-corp/private-app#7"]},
        )
        self.assertEqual(st_r, 403)
        self.assertEqual(err_r["error"]["code"], "private_repository")

        check_store = PackratStore(self.db_path, read_only=True)
        try:
            self.assertIsNone(
                check_store.get_repo(
                    "secret-corp/private-app", include_pending_delete=True
                )
            )
        finally:
            check_store.close()

        # Public unregistered repo succeeds on watch add, renew, remove, and refresh.
        st_add, _, add_res = self.req_json(
            "POST",
            "/api/v1/watches",
            body={
                "op": "add",
                "consumer": "monitor",
                "consumer_id": "m1",
                "keys": ["octo-org/public-items#12"],
                "freshness_s": 60,
                "ttl_s": 300,
            },
        )
        self.assertEqual(st_add, 200)
        self.assertEqual(add_res["leased"], ["octo-org/public-items#12"])
        self.assertIn("expires_at", add_res)

        st_ren, _, ren_res = self.req_json(
            "POST",
            "/api/v1/watches",
            body={
                "op": "renew",
                "consumer": "monitor",
                "consumer_id": "m1",
                "ttl_s": 600,
            },
        )
        self.assertEqual(st_ren, 200)
        self.assertEqual(ren_res["renewed"], 1)

        st_ref, _, ref_res = self.req_json(
            "POST",
            "/api/v1/refresh",
            body={"keys": ["octo-org/public-items#12"]},
        )
        self.assertEqual(st_ref, 200)
        self.assertEqual(ref_res["queued"], ["octo-org/public-items#12"])

        st_rem, _, rem_res = self.req_json(
            "POST",
            "/api/v1/watches",
            body={"op": "remove", "consumer": "monitor", "consumer_id": "m1"},
        )
        self.assertEqual(st_rem, 200)
        self.assertEqual(rem_res["removed"], 1)

    def test_changes_max_seq_rewait_and_64_waiter_cap(self) -> None:
        ps = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            baseline_seq = ps.max_change_seq()
            ps.append_change(
                "flutter/flutter#101",
                "notification",
                ["reason"],
                owner_id="other-user@example.com",
            )
            expected_max = ps.max_change_seq()
        finally:
            ps.close()

        # 0 visible rows for `local` after baseline_seq, yet cursor advances to max_seq!
        st0, _, chg0 = self.req_json(
            "GET", f"/api/v1/changes?since={baseline_seq}&wait=0"
        )
        self.assertEqual(st0, 200)
        self.assertEqual(chg0["changes"], [])
        self.assertEqual(chg0["seq"], expected_max)

        # Long-poll re-waits when woken by an invisible private change, then wakes on broadcast!
        def _emit_events() -> None:
            time.sleep(0.12)
            p_bg = PersonalStore(self.personal_db_path, notifier=self.notifier)
            try:
                p_bg.append_change(
                    "flutter/flutter#101",
                    "notification",
                    ["reason"],
                    owner_id="someone-else@example.com",
                )
                time.sleep(0.12)
                p_bg.append_change(
                    "flutter/flutter#101",
                    "pr",
                    ["head_sha"],
                    owner_id=None,
                    viewer_login=None,
                )
            finally:
                p_bg.close()

        emitter = threading.Thread(target=_emit_events)
        emitter.start()
        started = time.monotonic()
        st_wait, _, chg_wait = self.req_json(
            "GET", f"/api/v1/changes?since={expected_max}&wait=3"
        )
        elapsed = time.monotonic() - started
        emitter.join()
        self.assertEqual(st_wait, 200)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(chg_wait["changes"]), 1)
        self.assertEqual(chg_wait["changes"][0]["kind"], "pr")
        self.assertGreater(chg_wait["seq"], expected_max + 1)

        # 64 concurrent waiters hold slots; the 65th gets HTTP 429 too_many_waiters!
        latest_seq = chg_wait["seq"]
        entered = threading.Event()
        waiter_results: list[int] = []
        results_lock = threading.Lock()

        def _park_waiter() -> None:
            st_w, _, _ = _request(
                self.host,
                self.port,
                "GET",
                f"/api/v1/changes?since={latest_seq}&wait=5",
                timeout=10.0,
            )
            with results_lock:
                waiter_results.append(st_w)

        threads = [
            threading.Thread(target=_park_waiter, daemon=True) for _ in range(64)
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            if self.notifier.active_waiters >= 64:
                entered.set()
                break
            time.sleep(0.02)
        self.assertTrue(
            entered.is_set(), f"Only {self.notifier.active_waiters} waiters parked"
        )

        st_429, _, err_429 = self.req_json(
            "GET", f"/api/v1/changes?since={latest_seq}&wait=2"
        )
        self.assertEqual(st_429, 429)
        self.assertEqual(err_429["error"]["code"], "too_many_waiters")
        self.assertTrue(err_429["error"]["retryable"])

        # Wake all 64 waiters via a broadcast change so they exit cleanly.
        ps_wake = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            ps_wake.append_change("flutter/flutter#101", "checks", ["status"])
        finally:
            ps_wake.close()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(len(waiter_results), 64)
        self.assertTrue(all(code == 200 for code in waiter_results))

    def test_search_reader_batch_export_coalescing_and_locked_503(self) -> None:
        # POST /api/v1/search
        st_s, _, search_env = self.req_json(
            "POST",
            "/api/v1/search",
            body={"query": "scheduler frame race", "mode": "hybrid", "limit": 5},
        )
        self.assertEqual(st_s, 200)
        self.assertEqual(len(search_env["hits"]), 1)
        self.assertEqual(search_env["hits"][0]["chunk_id"], "flutter/flutter#101:body")
        self.assertIn("flutter/flutter", search_env["repo_watermarks"])

        # POST /api/v1/reader/batch
        ps_sync = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            ps_sync.link_identity("local", "octocat", verified=True)
            ps_sync.replace_viewer_set(
                "octocat", "authored_open", ["flutter/flutter#101"], now=1_760_000_000.0
            )
        finally:
            ps_sync.close()
        st_rb, _, rb_env = self.req_json(
            "POST",
            "/api/v1/reader/batch",
            body={
                "ops": {
                    "health": True,
                    "items": {"keys": ["flutter/flutter#101"]},
                    "repo_probe": {"repo": "flutter/flutter"},
                    "context": {
                        "repo": "flutter/flutter",
                        "files": ["engine/scheduler.cc"],
                    },
                }
            },
        )
        self.assertEqual(st_rb, 200)
        self.assertEqual(
            rb_env["health"]["viewer_sets_synced_at"],
            {"authored_open": "2025-10-09T08:53:20Z"},
        )
        st_sh, _, sh_env = self.req_json(
            "POST",
            "/api/v1/sql",
            body={
                "query": "SELECT viewer_sets_synced_json FROM v_service_health",
                "raw": True,
            },
        )
        self.assertEqual(st_sh, 200)
        self.assertEqual(
            json.loads(sh_env["rows"][0]["viewer_sets_synced_json"]),
            {"authored_open": "2025-10-09T08:53:20Z"},
        )
        self.assertTrue(rb_env["repo_probe"]["found"])
        self.assertEqual(len(rb_env["items"]["pr_live"]), 1)
        self.assertEqual(len(rb_env["context"]["hotspots"]), 1)

        # Concurrent GET /api/v1/export coalesces snapshot build + supports ETag 304.
        orig_export = PackratStore.export_single_repo_snapshot
        export_calls = 0
        count_lock = threading.Lock()

        def _counted_export(
            store_self: PackratStore, full_name: str, dest_path: Path | str, **kw: Any
        ) -> Any:
            nonlocal export_calls
            time.sleep(0.08)
            with count_lock:
                export_calls += 1
            return orig_export(store_self, full_name, dest_path, **kw)

        with (
            mock.patch.object(
                PackratStore, "export_single_repo_snapshot", _counted_export
            ),
            concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool,
        ):
            futs = [
                pool.submit(
                    _request,
                    self.host,
                    self.port,
                    "GET",
                    "/api/v1/export?repo=flutter/flutter",
                )
                for _ in range(4)
            ]
            outcomes = [f.result() for f in futs]

        self.assertEqual(export_calls, 1)
        etag = outcomes[0][1].get("etag", "")
        self.assertTrue(etag.startswith('"'))
        for st_exp, hdrs_exp, data_exp in outcomes:
            self.assertEqual(st_exp, 200)
            _assert_security_headers(self, hdrs_exp)
            self.assertEqual(hdrs_exp.get("etag"), etag)
            self.assertTrue(data_exp.startswith(b"SQLite format 3\x00"))

        # Validate exported SQLite snapshot is schema v15.
        snap_check = self.tmp_path / "downloaded.sqlite"
        snap_check.write_bytes(outcomes[0][2])
        imported_store = PackratStore(snap_check, read_only=True)
        try:
            self.assertEqual(imported_store.read_schema_and_views_version()[0], 15)
            self.assertEqual(
                [r.full_name for r in imported_store.list_repos()], ["flutter/flutter"]
            )
        finally:
            imported_store.close()

        # Conditional GET with If-None-Match -> 304 Not Modified with security headers.
        st_304, hdrs_304, body_304 = _request(
            self.host,
            self.port,
            "GET",
            "/api/v1/export?repo=flutter/flutter",
            headers={"If-None-Match": etag},
        )
        self.assertEqual(st_304, 304)
        _assert_security_headers(self, hdrs_304)
        self.assertEqual(body_304, b"")

        # SQLite lock contention on writable store maps to 503 database_locked (retryable=True).
        with mock.patch.object(
            PackratStore,
            "register_repo",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            st_503, _, err_503 = self.req_json(
                "POST",
                "/api/v1/repos",
                body={"full_name": "a2ui-project/a2ui", "git_source": "none"},
            )
        self.assertEqual(st_503, 503)
        self.assertEqual(err_503["error"]["code"], "database_locked")
        self.assertTrue(err_503["error"]["retryable"])

    def test_ten_client_concurrent_stress_zero_sqlite_busy(self) -> None:
        stop_bg = threading.Event()
        bg_errors: list[str] = []

        def _writer_loop() -> None:
            ps = PersonalStore(self.personal_db_path, notifier=self.notifier)
            wstore = PackratStore(self.db_path, personal_store=ps)
            idx = 0
            try:
                while not stop_bg.is_set():
                    idx += 1
                    wstore.upsert_pr_live(
                        PrLiveRecord(
                            repo_id=self.seeded_repo.repo_id,
                            number=101,
                            title=f"Fix engine frame scheduling #{idx}",
                            state="OPEN",
                            updated_at="2026-11-14T01:00:00Z",
                            head_sha=f"sha{idx:04d}",
                        )
                    )
                    time.sleep(0.02)
            except (
                sqlite3.Error,
                OSError,
                RuntimeError,
                ValueError,
            ) as exc:  # pragma: no cover
                bg_errors.append(str(exc))
            finally:
                wstore.close()
                ps.close()

        writer = threading.Thread(target=_writer_loop, daemon=True)
        writer.start()

        def _client_worker(worker_id: int) -> list[int]:
            codes: list[int] = []
            for step in range(3):
                kind = (worker_id + step) % 4
                if kind == 0:
                    st, _, _ = self.req_json(
                        "POST",
                        "/api/v1/search",
                        body={"query": "scheduler frame", "limit": 5},
                    )
                elif kind == 1:
                    st, _, b_env = self.req_json(
                        "POST",
                        "/api/v1/reader/batch",
                        body={
                            "health": True,
                            "items": {"keys": ["flutter/flutter#101"]},
                        },
                    )
                    self.assertIsInstance(
                        b_env["health"]["viewer_sets_synced_at"], dict
                    )
                elif kind == 2:
                    st, _, _ = self.req_json(
                        "POST",
                        "/api/v1/sql",
                        body={
                            "query": "SELECT number, state FROM v_pull_requests",
                            "raw": True,
                        },
                    )
                else:
                    st, _, _ = self.req_json("GET", "/api/v1/changes?since=0&wait=0.05")
                codes.append(st)
            return codes

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
                batches = list(pool.map(_client_worker, range(10)))
        finally:
            stop_bg.set()
            writer.join(timeout=5.0)

        self.assertEqual(bg_errors, [])
        flat = [code for batch in batches for code in batch]
        self.assertEqual(len(flat), 30)
        self.assertTrue(all(code == 200 for code in flat), f"Non-200 statuses: {flat}")

    def test_default_token_verifier_and_default_public_repo_checker(self) -> None:
        from gh_transport import GhCommandResult

        def _fake_runner_factory(token: str, _cfg_dir: Path) -> Any:
            def _run(args: list[str], _timeout: int = 20) -> GhCommandResult:
                if "user" in args:
                    if token == "good_gh_token":
                        return GhCommandResult(
                            0, 'HTTP/2.0 200 OK\r\n\r\n{"login":"octocat"}', ""
                        )
                    return GhCommandResult(
                        1, 'HTTP/2.0 401 Unauthorized\r\n\r\n{"message":"Bad"}', "401"
                    )
                if "repos/flutter/public-one" in args:
                    return GhCommandResult(
                        0, 'HTTP/2.0 200 OK\r\n\r\n{"private":false}', ""
                    )
                if "repos/flutter/secret-one" in args:
                    return GhCommandResult(
                        0, 'HTTP/2.0 200 OK\r\n\r\n{"private":true}', ""
                    )
                return GhCommandResult(
                    1, 'HTTP/2.0 404 Not Found\r\n\r\n{"message":"Not Found"}', "404"
                )

            return _run

        reg = TokenRegistry(
            self.tmp_path / "default_gh_cfg", runner_factory=_fake_runner_factory
        )
        srv = PackratHttpServer(
            "127.0.0.1:0",
            self.db_path,
            self.personal_db_path,
            token_registry=reg,
            public_repo_checker=None,
        )
        h, p = str(srv.server_address[0]), int(srv.server_address[1])
        t = threading.Thread(
            target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        t.start()
        try:
            st_bad, _, _ = _request(
                h,
                p,
                "GET",
                "/api/v1/health",
                headers={"Authorization": "Bearer wrong"},
            )
            self.assertEqual(st_bad, 401)

            st_ok, _, body_ok = _request(
                h,
                p,
                "POST",
                "/api/v1/watches",
                body={
                    "op": "add",
                    "consumer": "monitor",
                    "consumer_id": "d1",
                    "keys": ["flutter/public-one#5"],
                },
                headers={"Authorization": "Bearer good_gh_token"},
            )
            self.assertEqual(st_ok, 200)
            self.assertEqual(json.loads(body_ok)["leased"], ["flutter/public-one#5"])

            for priv_key in ("flutter/secret-one#5", "flutter/missing-404#5"):
                st_priv, _, _ = _request(
                    h,
                    p,
                    "POST",
                    "/api/v1/watches",
                    body={
                        "op": "add",
                        "consumer": "monitor",
                        "consumer_id": "d1",
                        "keys": [priv_key],
                    },
                    headers={"Authorization": "Bearer good_gh_token"},
                )
                self.assertEqual(st_priv, 403)
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5.0)

    def test_unverified_github_user_header_edge_aliases_and_repo_warnings(
        self,
    ) -> None:
        from http_server import EdgeAuthenticator, sign_edge_request

        ps = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            ps.replace_viewer_set("octocat", "authored", ["flutter/flutter#101"])
            ps.link_identity("claimed@example.com", "claimeduser", verified=True)
        finally:
            ps.close()

        st_batch, _, batch_res = self.req_json(
            "POST",
            "/api/v1/reader/batch",
            body={"viewer_items": {"sets": ["authored"]}},
            headers={"X-Packrat-Github-User": "OctoCat"},
        )
        self.assertEqual(st_batch, 200)
        self.assertEqual(
            batch_res["viewer_items"][0]["item_key"], "flutter/flutter#101"
        )

        # Conflict or invalid unverified header on passive reads falls back cleanly without 400/409.
        ps2 = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            ps2.conn.execute("DELETE FROM user_identities WHERE owner_id = 'local'")
            ps2.conn.commit()
        finally:
            ps2.close()
        for bad_hdr in ("claimeduser", "bad login!"):
            st_h, _, h_res = self.req_json(
                "GET", "/api/v1/health", headers={"X-Packrat-Github-User": bad_hdr}
            )
            self.assertEqual(st_h, 200)
            self.assertFalse(h_res["login_verified"])

        # Verified identity is never downgraded by an unverified X-Packrat-Github-User header.
        ps3 = PersonalStore(self.personal_db_path, notifier=self.notifier)
        try:
            ps3.link_identity("local", "verifiedcat", verified=True)
            ps3.replace_viewer_set("verifiedcat", "authored", ["flutter/flutter#101"])
        finally:
            ps3.close()
        st_v, _, v_res = self.req_json(
            "POST",
            "/api/v1/reader/batch",
            body={"viewer_items": {"sets": ["authored"]}},
            headers={"X-Packrat-Github-User": "impersonator"},
        )
        self.assertEqual(st_v, 200)
        self.assertEqual(len(v_res["viewer_items"]), 1)

        # EdgeAuthenticator accepts X-Packrat-Verified-User and X-Packrat-User-Email aliases.
        edge = EdgeAuthenticator(
            trust_edge_proxy=True, hmac_keys={"k1": "supersecret"}, clock=lambda: 1000.0
        )
        for hdr_name in ("X-Packrat-Verified-User", "X-Packrat-User-Email"):
            sig_hdr = sign_edge_request(
                "k1", "supersecret", 1000, "GET", "/api/v1/health", "Alice@Example.com"
            )
            owner = edge.authenticate(
                "GET",
                "/api/v1/health",
                {hdr_name: "Alice@Example.com", "X-Packrat-Edge-Sig": sig_hdr},
            )
            self.assertEqual(owner, "alice@example.com")

        # GET /api/v1/repos includes backfill warnings for incomplete mirror repositories.
        st_repos, _, repos_res = self.req_json("GET", "/api/v1/repos")
        self.assertEqual(st_repos, 200)
        flutter_entry = next(
            r for r in repos_res["repos"] if r["full_name"] == "flutter/flutter"
        )
        self.assertTrue(flutter_entry["warnings"])


if __name__ == "__main__":
    unittest.main()
