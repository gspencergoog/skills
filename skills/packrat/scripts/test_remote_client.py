"""Unit and loopback-HTTP integration tests for `remote_client.py`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import remote_client
from db_store import DEFAULT_DB_PATH, PackratStore
from embedder import DeterministicHashEmbedder, sync_repo_embeddings
from models import (
    ClientConfig,
    CommitRecord,
    HttpHelperError,
    PrRecord,
    TextChunk,
    compute_content_sha256,
)


class _StubServerState:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.search_response: dict[str, Any] = {
            "hits": [],
            "repo_watermarks": {},
            "warnings": [],
        }
        self.sql_responses: dict[str, dict[str, Any]] = {}
        self.default_sql_response: dict[str, Any] = {"rows": [], "columns": []}
        self.export_bytes: bytes = b"SQLite format 3\x00stub-payload"
        self.export_etag: str = '"etag-v1"'
        self.error_routes: dict[str, tuple[int, dict[str, Any]]] = {}


class _StubHttpServer(ThreadingHTTPServer):
    def __init__(
        self, server_address: tuple[str, int], state: _StubServerState
    ) -> None:
        self.state = state
        super().__init__(server_address, _StubHttpHandler)


class _StubHttpHandler(BaseHTTPRequestHandler):
    server: _StubHttpServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_body(self) -> str:
        length = int(self.headers.get("Content-Length") or "0")
        return self.rfile.read(length).decode("utf-8") if length > 0 else ""

    def _send_json(
        self, status: int, payload: Any, extra_headers: dict[str, str] | None = None
    ) -> None:
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, method: str, body: str) -> None:
        self.server.state.requests.append(
            {
                "method": method,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def _handle_export(self, state: _StubServerState) -> None:
        if self.headers.get("If-None-Match") == state.export_etag:
            self.send_response(304)
            self.send_header("ETag", state.export_etag)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-sqlite3")
        self.send_header("ETag", state.export_etag)
        self.send_header("Content-Length", str(len(state.export_bytes)))
        self.end_headers()
        self.wfile.write(state.export_bytes)

    def do_GET(self) -> None:
        state = self.server.state
        self._record("GET", "")
        path_only = self.path.split("?", 1)[0]
        if path_only in state.error_routes:
            st, err = state.error_routes[path_only]
            self._send_json(st, err)
            return
        if path_only == "/api/v1/export":
            self._handle_export(state)
            return
        routes: dict[str, Any] = {
            "/api/v1/health": {"beat_age_s": 5, "owner_id": "local"},
            "/api/v1/repos": {"repos": [{"full_name": "octo/app"}]},
            "/api/v1/changes": {"seq": 12, "truncated": False, "changes": []},
        }
        if path_only in routes:
            self._send_json(200, routes[path_only])
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "missing"}})

    def do_POST(self) -> None:
        state = self.server.state
        body = self._read_body()
        self._record("POST", body)
        path_only = self.path.split("?", 1)[0]
        if path_only in state.error_routes:
            st, err = state.error_routes[path_only]
            self._send_json(st, err)
            return
        parsed = json.loads(body) if body else {}
        handlers: dict[str, tuple[int, Any]] = {
            "/api/v1/auth/link": (
                200,
                {"github_login": parsed.get("github_login", ""), "verified": False},
            ),
            "/api/v1/repos": (201, {"full_name": parsed.get("full_name")}),
            "/api/v1/search": (200, state.search_response),
            "/api/v1/sql": (
                200,
                state.sql_responses.get(
                    str(parsed.get("query") or ""), state.default_sql_response
                ),
            ),
            "/api/v1/watches": (200, {"leased": parsed.get("keys", [])}),
            "/api/v1/refresh": (200, {"queued": parsed.get("keys", [])}),
            "/api/v1/reader/batch": (200, {"health": {"beat_age_s": 3}}),
        }
        if path_only in handlers:
            status, payload = handlers[path_only]
            self._send_json(status, payload)
            return
        self._send_json(404, {"error": {"code": "not_found", "message": "missing"}})

    def do_DELETE(self) -> None:
        self._record("DELETE", "")
        self._send_json(202, {"deleting": self.path})


class _LoopbackServerMixin:
    server: _StubHttpServer
    server_thread: threading.Thread
    state: _StubServerState
    base_url: str

    def start_stub_server(self) -> None:
        self.state = _StubServerState()
        self.server = _StubHttpServer(("127.0.0.1", 0), self.state)
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def stop_stub_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)


class TestHygieneAndImports(unittest.TestCase):
    def test_no_corp_substrings_in_remote_client(self) -> None:
        source = (SCRIPTS_DIR / "remote_client.py").read_text(encoding="utf-8").lower()
        forbidden_parts = (
            ("goo", "gle"),
            ("co", "rp"),
            ("sso_", "client"),
            (".go", "og"),
            ("uber", "proxy"),
            ("co", "rprun"),
        )
        for parts in forbidden_parts:
            self.assertNotIn("".join(parts), source)

    def test_lazy_urllib_request_import(self) -> None:
        code = (
            f"import sys; sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
            "import remote_client; "
            "assert 'urllib.request' not in sys.modules; "
            "assert 'urllib.error' not in sys.modules; "
            "assert 'onnxruntime' not in sys.modules; "
            "print('clean')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=False
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "clean")


class TestLoadClientConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_cfg(self, payload: dict[str, Any]) -> Path:
        cfg_path = self.home / ".config" / "packrat" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(payload), encoding="utf-8")
        return cfg_path

    def test_defaults_when_missing(self) -> None:
        cfg = remote_client.load_client_config(env={"HOME": str(self.home)})
        self.assertEqual(cfg.mode, "local")
        self.assertEqual(cfg.remote_url, "")
        self.assertEqual(cfg.http_cmd, ())
        self.assertFalse(cfg.forward_gh_token)

    def test_remote_url_defaults_omitted_mode_to_auto(self) -> None:
        self._write_cfg(
            {
                "remote_url": "https://service.example.invalid/",
                "http_cmd": ["helper", "--flag"],
                "forward_gh_token": True,
                "github_user": "octocat",
            }
        )
        cfg = remote_client.load_client_config(env={"HOME": str(self.home)})
        self.assertEqual(cfg.mode, "auto")
        self.assertEqual(cfg.remote_url, "https://service.example.invalid")
        self.assertEqual(cfg.http_cmd, ("helper", "--flag"))
        self.assertTrue(cfg.forward_gh_token)
        self.assertEqual(cfg.github_user, "octocat")

    def test_env_and_cli_precedence_and_db_override(self) -> None:
        self._write_cfg({"mode": "local", "remote_url": "https://cfg.invalid"})
        env = {
            "HOME": str(self.home),
            "PACKRAT_MODE": "remote",
            "PACKRAT_URL": "https://env.invalid",
        }
        cfg_env = remote_client.load_client_config(env=env)
        self.assertEqual(cfg_env.mode, "remote")
        self.assertEqual(cfg_env.remote_url, "https://env.invalid")

        # Default --db keeps remote mode from env.
        args_default_db = SimpleNamespace(db=str(DEFAULT_DB_PATH))
        self.assertEqual(
            remote_client.load_client_config(args_default_db, env=env).mode, "remote"
        )

        # Non-default --db forces local unless --remote / --remote-url / --mode remote.
        custom_db = str(Path(self.tmp.name) / "fixture.sqlite")
        self.assertEqual(
            remote_client.load_client_config(
                SimpleNamespace(db=custom_db), env=env
            ).mode,
            "local",
        )
        self.assertEqual(
            remote_client.load_client_config(
                SimpleNamespace(db=custom_db, remote=True), env=env
            ).mode,
            "remote",
        )
        self.assertEqual(
            remote_client.load_client_config(
                SimpleNamespace(db=custom_db, remote_url="https://cli.invalid"), env=env
            ).mode,
            "remote",
        )
        self.assertEqual(
            remote_client.load_client_config(
                SimpleNamespace(db=custom_db, routing_mode="remote"), env=env
            ).mode,
            "remote",
        )

        # --local overrides --mode remote.
        self.assertEqual(
            remote_client.load_client_config(
                SimpleNamespace(routing_mode="remote", local=True), env=env
            ).mode,
            "local",
        )
        with self.assertRaises(ValueError):
            remote_client.load_client_config(
                SimpleNamespace(remote=True, local=True), env=env
            )


class TestHttpCmdTransport(unittest.TestCase, _LoopbackServerMixin):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.start_stub_server()

    def tearDown(self) -> None:
        self.stop_stub_server()
        self.tmp.cleanup()

    def test_http_cmd_stdin_token_privacy_and_output_path(self) -> None:
        capture_file = self.tmp_path / "helper_capture.json"
        out_bin = self.tmp_path / "exported.sqlite"
        helper_py = self.tmp_path / "helper.py"
        helper_py.write_text(
            "import json, pathlib, sys\n"
            "req = json.loads(sys.stdin.read())\n"
            f"pathlib.Path({str(capture_file)!r}).write_text("
            "json.dumps({'argv': sys.argv, 'req': req}), encoding='utf-8')\n"
            "if 'output_path' in req:\n"
            "    p = pathlib.Path(req['output_path'])\n"
            "    p.write_bytes(b'streamed-bytes')\n"
            "    print(json.dumps({'status': 200, 'headers': {'ETag': '\"h1\"'}, 'bytes_written': 14}))\n"
            "else:\n"
            "    print(json.dumps({'status': 200, 'headers': {}, 'body': json.dumps({'ok': True})}))\n",
            encoding="utf-8",
        )
        cfg = ClientConfig(
            mode="remote",
            remote_url="https://remote.example.invalid",
            http_cmd=(sys.executable, str(helper_py)),
            forward_gh_token=True,
            github_user="tester",
        )
        transport = remote_client.HttpCmdTransport(
            cfg, token_provider=lambda: "ghp_super_secret_token_123"
        )
        res = transport.request_json("GET", "/api/v1/health")
        self.assertEqual(res, {"ok": True})

        captured = json.loads(capture_file.read_text(encoding="utf-8"))
        self.assertNotIn("ghp_super_secret_token_123", " ".join(captured["argv"]))
        self.assertEqual(
            captured["req"]["headers"]["Authorization"],
            "Bearer ghp_super_secret_token_123",
        )
        self.assertEqual(captured["req"]["headers"]["X-Packrat-Github-User"], "tester")

        exp = remote_client.remote_export_repo(transport, "octo/app", out_bin)
        self.assertEqual(exp["bytes_written"], 14)
        self.assertEqual(exp["etag"], '"h1"')
        self.assertEqual(out_bin.read_bytes(), b"streamed-bytes")

    def test_token_forwarding_blocked_on_plain_non_loopback_http(self) -> None:
        seen_headers: list[dict[str, str]] = []

        def fake_run(
            argv: list[str], *, input: str, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            req = json.loads(input)
            seen_headers.append(req["headers"])
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"status": 200, "headers": {}, "body": "{}"}),
                stderr="",
            )

        cfg = ClientConfig(
            mode="remote",
            remote_url="http://plain.example.invalid",
            http_cmd=("fake-helper",),
            forward_gh_token=True,
        )
        transport = remote_client.HttpCmdTransport(
            cfg, token_provider=lambda: "secret", subprocess_run=fake_run
        )
        transport.request_json("GET", "/api/v1/health")
        self.assertNotIn("Authorization", seen_headers[0])

    def test_retry_503_database_locked_504_401_and_error_codes(self) -> None:
        sleeps: list[float] = []
        responses = [
            {
                "status": 503,
                "headers": {},
                "body": json.dumps(
                    {
                        "error": {
                            "code": "database_locked",
                            "message": "busy",
                            "retryable": True,
                        }
                    }
                ),
            }
        ] * 4

        def fake_run(
            argv: list[str], *, input: str, **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            payload = responses.pop(0)
            return subprocess.CompletedProcess(
                argv, 0, stdout=json.dumps(payload), stderr=""
            )

        cfg = ClientConfig(
            mode="remote", remote_url="https://x.invalid", http_cmd=("h",)
        )
        transport = remote_client.HttpCmdTransport(
            cfg, sleep_fn=sleeps.append, subprocess_run=fake_run
        )
        with self.assertRaises(HttpHelperError) as cm:
            transport.request_json("GET", "/api/v1/health")
        self.assertEqual(cm.exception.code, "database_locked")
        self.assertEqual(cm.exception.status, 503)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(remote_client.exit_code_for_http_error(cm.exception), 3)
        self.assertEqual(len(sleeps), 3)

        # 504 query_timeout -> exit 1, no retry
        responses[:] = [
            {
                "status": 504,
                "headers": {},
                "body": json.dumps(
                    {"error": {"code": "query_timeout", "message": "slow"}}
                ),
            }
        ]
        with self.assertRaises(HttpHelperError) as cm504:
            transport.request_json(
                "POST", "/api/v1/sql", json_body={"query": "SELECT 1"}
            )
        self.assertEqual(cm504.exception.code, "query_timeout")
        self.assertEqual(remote_client.exit_code_for_http_error(cm504.exception), 1)

        # Helper exit code 4 -> auth_expired
        def auth_fail_run(
            argv: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                argv, 4, stdout="", stderr="session expired"
            )

        auth_transport = remote_client.HttpCmdTransport(
            cfg, subprocess_run=auth_fail_run
        )
        with self.assertRaises(HttpHelperError) as cm401:
            auth_transport.request_json("GET", "/api/v1/health")
        self.assertEqual(cm401.exception.code, "auth_expired")
        self.assertEqual(cm401.exception.status, 401)

        # 403 private_repository & 503 disk_high_water preserved without retry
        for status, code in (
            (403, "private_repository"),
            (409, "delete_pending"),
            (503, "disk_high_water"),
            (409, "identity_conflict"),
            (429, "too_many_waiters"),
        ):
            responses[:] = [
                {
                    "status": status,
                    "headers": {},
                    "body": json.dumps({"error": {"code": code, "message": code}}),
                }
            ]
            with self.assertRaises(HttpHelperError) as err_cm:
                transport.request_json("GET", "/api/v1/repos")
            self.assertEqual(err_cm.exception.code, code)
            self.assertEqual(err_cm.exception.status, status)

    def test_direct_http_wrappers_and_export_streaming(self) -> None:
        cfg = ClientConfig(
            mode="remote",
            remote_url=self.base_url,
            forward_gh_token=True,
            github_user="devuser",
        )
        transport = remote_client.HttpCmdTransport(cfg, token_provider=lambda: "tok-1")
        self.assertEqual(remote_client.remote_health(transport)["beat_age_s"], 5)
        self.assertEqual(
            remote_client.remote_auth_link(transport, "devuser")["github_login"],
            "devuser",
        )
        self.assertEqual(
            remote_client.remote_list_repos(transport, mode="mirror")["repos"],
            [{"full_name": "octo/app"}],
        )
        self.assertEqual(
            remote_client.remote_add_repo(transport, "octo/new", git_source="none")[
                "full_name"
            ],
            "octo/new",
        )
        self.assertIn(
            "deleting", remote_client.remote_remove_repo(transport, "octo/new")
        )
        self.assertEqual(
            remote_client.remote_watches(
                transport,
                "add",
                consumer="monitor",
                consumer_id="c1",
                keys=["octo/app#1"],
            )["leased"],
            ["octo/app#1"],
        )
        self.assertEqual(
            remote_client.remote_refresh(transport, ["octo/app#1"])["queued"],
            ["octo/app#1"],
        )
        self.assertEqual(remote_client.remote_changes(transport, since=1)["seq"], 12)
        self.assertEqual(
            remote_client.remote_reader_batch(transport, {"health": True})["health"][
                "beat_age_s"
            ],
            3,
        )

        dest = self.tmp_path / "mirror.sqlite"
        exp1 = remote_client.remote_export_repo(transport, "octo/app", dest)
        self.assertFalse(exp1["not_modified"])
        self.assertEqual(exp1["etag"], '"etag-v1"')
        self.assertEqual(dest.read_bytes(), self.state.export_bytes)

        exp2 = remote_client.remote_export_repo(
            transport, "octo/app", dest, if_none_match='"etag-v1"'
        )
        self.assertTrue(exp2["not_modified"])
        self.assertEqual(dest.read_bytes(), self.state.export_bytes)


class TestRoutingSearchAndSql(unittest.TestCase, _LoopbackServerMixin):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.start_stub_server()

    def tearDown(self) -> None:
        self.stop_stub_server()
        self.tmp.cleanup()

    def test_empty_home_creates_zero_sqlite_files_across_resolver_and_search(
        self,
    ) -> None:
        empty_home = self.tmp_path / "empty_home"
        empty_home.mkdir()
        absent_db = empty_home / ".local" / "share" / "packrat" / "repos.sqlite"
        cfg = ClientConfig(mode="auto", remote_url=self.base_url)

        resolver = remote_client.SessionTargetResolver(cfg, absent_db)
        self.assertEqual(resolver.resolve(), "remote")
        self.assertTrue(resolver.is_remote)
        self.assertFalse(absent_db.exists())

        self.state.search_response = {
            "hits": [
                {
                    "chunk_id": "octo/app:pr:1",
                    "repo_full_name": "octo/app",
                    "source_kind": "pr",
                    "source_ref": "octo/app#1",
                    "title": "Fix bug",
                    "snippet": "sn",
                    "rrf_score": 0.5,
                    "bm25_rank": 1,
                    "vector_rank": None,
                    "cosine_score": None,
                    "is_untrusted": True,
                    "file_path": None,
                    "item_number": 1,
                }
            ],
            "repo_watermarks": {
                "octo/app": {
                    "sync_watermark": "2026-01-02T00:00:00Z",
                    "last_indexed_sha": "abc",
                    "last_commit_at": "2026-01-02T00:00:00Z",
                }
            },
            "warnings": [],
        }
        res = remote_client.hybrid_search("fix bug", cfg, absent_db, mode="fts")
        self.assertEqual(len(res["hits"]), 1)
        self.assertFalse(absent_db.exists())

    def test_session_target_resolver_pins_single_live_target(self) -> None:
        db_path = self.tmp_path / "local.sqlite"
        store = PackratStore(db_path)
        now = 1_700_000_000.0
        store.write_heartbeat(pid=123, version="1", now=now)
        store.close()

        cfg = ClientConfig(mode="auto", remote_url=self.base_url)
        clock = [now + 10.0]
        resolver = remote_client.SessionTargetResolver(
            cfg, db_path, now_fn=lambda: clock[0]
        )
        self.assertEqual(resolver.resolve(), "local")
        # Even if clock advances past 180s later in session, target remains pinned.
        clock[0] = now + 500.0
        self.assertEqual(resolver.resolve(), "local")

        # Fresh resolver sees stale heartbeat -> pins remote.
        stale_resolver = remote_client.SessionTargetResolver(
            cfg, db_path, now_fn=lambda: now + 500.0
        )
        self.assertEqual(stale_resolver.resolve(), "remote")

    def test_hybrid_search_per_repo_tier_dedup_across_shifted_lines(self) -> None:
        db_path = self.tmp_path / "search.sqlite"
        store = PackratStore(db_path)
        repo = store.register_repo("octo", "app")
        store.update_repo_sync_state(
            repo.repo_id,
            prs_watermark="2026-01-05T00:00:00Z",
            last_indexed_sha="oldsha",
        )
        # Local git commit is older (Jan 1) with symbol at old line 10,
        # while local GitHub PR watermark is newer (Jan 5).
        store.upsert_commits(
            [
                CommitRecord(
                    repo_id=repo.repo_id,
                    sha="oldsha",
                    committed_at="2026-01-01T00:00:00Z",
                    title="old commit",
                )
            ]
        )
        sym_cid = "octo/app:symbol:src/auth.py:verify_session:10"
        pr_cid = "octo/app:pr:77"
        store.upsert_pull_requests(
            [
                PrRecord(
                    repo_id=repo.repo_id,
                    number=77,
                    title="verify_session local PR",
                    state="OPEN",
                    updated_at="2026-01-05T00:00:00Z",
                    body="verify_session details",
                )
            ]
        )
        store.upsert_chunks(
            [
                TextChunk(
                    chunk_id=sym_cid,
                    repo_id=repo.repo_id,
                    source_kind="code_symbol",
                    source_ref="src/auth.py:10",
                    title="verify_session",
                    text="def verify_session(): pass",
                    content_sha256=compute_content_sha256(
                        "verify_session", "def verify_session(): pass"
                    ),
                    is_untrusted=False,
                    file_path="src/auth.py",
                ),
                TextChunk(
                    chunk_id=pr_cid,
                    repo_id=repo.repo_id,
                    source_kind="pr",
                    source_ref="octo/app#77",
                    title="verify_session local PR",
                    text="verify_session details",
                    content_sha256=compute_content_sha256(
                        "verify_session local PR", "verify_session details"
                    ),
                    is_untrusted=True,
                    item_number=77,
                ),
            ]
        )
        embedder = DeterministicHashEmbedder()
        sync_repo_embeddings(store, repo.repo_id, embedder)
        store.close()

        # Remote has newer git commit (Jan 10, shifted line 25) and older PR watermark (Jan 2).
        self.state.search_response = {
            "hits": [
                {
                    "chunk_id": "octo/app:symbol:src/auth.py:verify_session:25",
                    "repo_full_name": "octo/app",
                    "source_kind": "code_symbol",
                    "source_ref": "src/auth.py:25",
                    "title": "verify_session",
                    "snippet": "def verify_session(): pass",
                    "rrf_score": 0.03,
                    "bm25_rank": 1,
                    "vector_rank": 1,
                    "cosine_score": 0.91,
                    "is_untrusted": False,
                    "file_path": "src/auth.py",
                    "item_number": None,
                },
                {
                    "chunk_id": "octo/app:pr:77",
                    "repo_full_name": "octo/app",
                    "source_kind": "pr",
                    "source_ref": "octo/app#77",
                    "title": "verify_session remote stale PR",
                    "snippet": "stale",
                    "rrf_score": 0.99,
                    "bm25_rank": 1,
                    "vector_rank": 1,
                    "cosine_score": 0.99,
                    "is_untrusted": True,
                    "file_path": None,
                    "item_number": 77,
                },
            ],
            "repo_watermarks": {
                "octo/app": {
                    "sync_watermark": "2026-01-02T00:00:00Z",
                    "last_indexed_sha": "newsha",
                    "last_commit_at": "2026-01-10T00:00:00Z",
                }
            },
            "warnings": [],
        }

        cfg = ClientConfig(mode="auto", remote_url=self.base_url)
        env = remote_client.hybrid_search(
            "verify_session", cfg, db_path, mode="fts", embedder=embedder
        )
        cids = [h["chunk_id"] for h in env["hits"]]
        titles = {h["chunk_id"]: h["title"] for h in env["hits"]}
        # Git tier: remote wins -> line 25 kept, line 10 dropped (no duplicate).
        self.assertIn("octo/app:symbol:src/auth.py:verify_session:25", cids)
        self.assertNotIn(sym_cid, cids)
        # GitHub tier: local wins -> local PR kept, stale remote PR dropped.
        self.assertEqual(titles[pr_cid], "verify_session local PR")
        self.assertEqual(len(cids), 2)

        # Remote failure in auto mode falls back cleanly to local with warning.
        self.state.error_routes["/api/v1/search"] = (
            401,
            {"error": {"code": "auth_expired", "message": "expired"}},
        )
        fallback = remote_client.hybrid_search(
            "verify_session", cfg, db_path, mode="fts", embedder=embedder
        )
        self.assertTrue(fallback["warnings"])
        self.assertIn(sym_cid, [h["chunk_id"] for h in fallback["hits"]])

    def test_multi_predicate_route_sql_mode(self) -> None:
        db_path = self.tmp_path / "route.sqlite"
        store = PackratStore(db_path)
        store.register_repo("local", "mirror")
        store.ensure_items_repo("local", "itemsonly")
        store.write_heartbeat(pid=1, version="1")
        store.close()

        cfg = ClientConfig(mode="auto", remote_url=self.base_url)
        self.assertEqual(
            remote_client.route_sql_mode(
                "SELECT * FROM v_pull_requests WHERE repo_full_name = 'local/mirror'",
                cfg,
                db_path,
            ),
            "local",
        )
        self.assertEqual(
            remote_client.route_sql_mode(
                "SELECT * FROM v_pull_requests WHERE item_key = 'local/mirror#12'",
                cfg,
                db_path,
            ),
            "local",
        )
        self.assertEqual(
            remote_client.route_sql_mode(
                "SELECT * FROM v_code_symbols WHERE chunk_id = 'local/mirror:symbol:a.py:1'",
                cfg,
                db_path,
            ),
            "local",
        )
        self.assertEqual(
            remote_client.route_sql_mode(
                "SELECT * FROM v_pull_requests WHERE item_key IN ('local/mirror#1', 'remote/only#2')",
                cfg,
                db_path,
            ),
            "remote",
        )
        self.assertEqual(
            remote_client.route_sql_mode(
                "SELECT * FROM v_pull_requests WHERE repo = 'local/itemsonly'",
                cfg,
                db_path,
            ),
            "remote",
        )
        self.assertEqual(
            remote_client.route_sql_mode("SELECT * FROM v_watches", cfg, db_path),
            "local",
        )
        self.assertEqual(
            remote_client.route_sql_mode(
                "SELECT COUNT(*) FROM v_pull_requests", cfg, db_path
            ),
            "remote",
        )

    def test_with_remote_parsing_and_empty_metadata_error(self) -> None:
        with self.assertRaises(ValueError):
            remote_client.parse_with_remote_specs(["1bad=SELECT 1"])
        with self.assertRaises(ValueError):
            remote_client.parse_with_remote_specs(["a=SELECT 1", "A=SELECT 2"])

        cfg = ClientConfig(mode="remote", remote_url=self.base_url)
        self.state.sql_responses["SELECT 1; SELECT 2"] = {"rows": [], "columns": []}
        with self.assertRaises(ValueError) as cm:
            remote_client.run_sql_with_remote(
                "SELECT * FROM empty_view",
                ["empty_view=SELECT 1; SELECT 2"],
                cfg,
                self.tmp_path / "missing.sqlite",
            )
        self.assertIn("single-statement SELECT", str(cm.exception))

    @unittest.skipUnless(
        Path(remote_client.resolve_duckdb_bin()).is_file() or shutil.which("duckdb"),
        "duckdb CLI not installed",
    )
    def test_with_remote_non_empty_and_zero_row_joins_under_empty_home(self) -> None:
        empty_home = self.tmp_path / "capsule_home"
        empty_home.mkdir()
        absent_db = empty_home / ".local" / "share" / "packrat" / "repos.sqlite"
        cfg = ClientConfig(mode="remote", remote_url=self.base_url)

        sub_non_empty = (
            "SELECT 'octo/app#9' AS item_key, 'Fix bug' AS title, 42 AS score"
        )
        sub_zero_row = "SELECT item_key, number FROM v_pull_requests WHERE FALSE"
        self.state.sql_responses[sub_non_empty] = {
            "rows": [{"item_key": "octo/app#9", "title": "Fix bug", "score": 42}],
            "columns": [
                {"name": "item_key", "type": "VARCHAR"},
                {"name": "title", "type": "VARCHAR"},
                {"name": "score", "type": "BIGINT"},
            ],
        }
        self.state.sql_responses[sub_zero_row] = {
            "rows": [],
            "columns": [
                {"name": "item_key", "type": "VARCHAR"},
                {"name": "number", "type": "BIGINT"},
            ],
        }

        with mock.patch.dict(os.environ, {"HOME": str(empty_home)}):
            res = remote_client.run_sql_with_remote(
                "SELECT r.item_key, r.title, r.score, z.number "
                "FROM remote_hits r "
                "LEFT JOIN remote_empty z ON z.item_key = r.item_key "
                "LEFT JOIN v_pull_requests p ON p.number = z.number",
                [f"remote_hits={sub_non_empty}", f"remote_empty={sub_zero_row}"],
                cfg,
                absent_db,
                raw=False,
                fmt="json",
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(len(res.rows), 1)
            self.assertEqual(res.rows[0]["item_key"], "octo/app#9")
            self.assertIn(
                '<untrusted_repo_content source="octo/app#9">', res.rows[0]["title"]
            )
            self.assertIsNone(res.rows[0]["number"])

            raw_res = remote_client.run_sql_with_remote(
                "SELECT item_key, title FROM remote_hits",
                [f"remote_hits={sub_non_empty}"],
                cfg,
                absent_db,
                raw=True,
                fmt="json",
            )
            self.assertEqual(raw_res.returncode, 0, raw_res.stderr)
            self.assertEqual(raw_res.rows[0]["title"], "Fix bug")

        # Zero local repos.sqlite created anywhere in empty HOME.
        self.assertFalse(absent_db.exists())
        # Verify raw=True was sent for staged remote subqueries.
        sql_reqs = [
            json.loads(r["body"])
            for r in self.state.requests
            if r["path"] == "/api/v1/sql"
        ]
        self.assertTrue(all(req["raw"] is True for req in sql_reqs))


if __name__ == "__main__":
    unittest.main()
