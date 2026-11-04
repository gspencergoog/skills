"""Unit tests for the packrat CLI dispatcher (status, repo, search, sql)."""

from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import packrat


class TestPackratCli(unittest.TestCase):
    """Tests CLI subcommands against an isolated temporary SQLite database."""

    def test_status_repo_add_search_and_sql_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "repos.sqlite")

            # 1. repo list --json starts empty: no repositories are seeded by default.
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(["--db", db_path, "repo", "list", "--json"])
            self.assertEqual(rc, 0)
            repos = json.loads(buf.getvalue())
            self.assertEqual(repos, [])

            # 1b. sync with no registered repositories explains how to add one.
            err_empty = io.StringIO()
            with mock.patch("sys.stderr", err_empty):
                rc = packrat.main(["--db", db_path, "sync"])
            self.assertEqual(rc, 2)
            self.assertIn("packrat repo add <owner>/<name>", err_empty.getvalue())

            # 2. repo add
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(
                    [
                        "--db",
                        db_path,
                        "repo",
                        "add",
                        "dart-lang/sdk",
                        "--path",
                        "/tmp/sdk",
                        "--branch",
                        "main",
                    ]
                )
            self.assertEqual(rc, 0)
            self.assertIn("dart-lang/sdk", buf.getvalue())
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "add", "octo-org/widgets"]),
                    0,
                )

            # 2b. malformed names are rejected.
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "add", "widgets"]), 2
                )
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "add", "a/b/c"]), 2
                )

            # 3. status --repo octo-org/widgets --json
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(
                    ["--db", db_path, "status", "--repo", "octo-org/widgets", "--json"]
                )
            self.assertEqual(rc, 0)
            status_data = json.loads(buf.getvalue())
            self.assertEqual(len(status_data["repos"]), 1)
            self.assertEqual(status_data["repos"][0]["full_name"], "octo-org/widgets")

            # 4. search --json (empty DB returns [] on stdout and emits unsynced hint on stderr)
            buf = io.StringIO()
            err_search = io.StringIO()
            with mock.patch("sys.stdout", buf), mock.patch("sys.stderr", err_search):
                rc = packrat.main(
                    ["--db", db_path, "search", "surface controller", "--json"]
                )
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), [])
            self.assertIn("0 indexed chunks in packrat", err_search.getvalue())

            # 5. sql --format json
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(
                    [
                        "--db",
                        db_path,
                        "sql",
                        "SELECT full_name FROM kb.repos ORDER BY repo_id",
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(rc, 0)
            sql_rows = json.loads(buf.getvalue())
            self.assertEqual(len(sql_rows), 2)

            # 6. sql handles missing DUCKDB_BIN gracefully with exit code 2 (M5)
            err_buf = io.StringIO()
            with (
                mock.patch.dict(
                    "os.environ", {"DUCKDB_BIN": "/nonexistent/duckdb_binary"}
                ),
                mock.patch("sys.stderr", err_buf),
            ):
                rc_err = packrat.main(["--db", db_path, "sql", "SELECT 1"])
            self.assertEqual(rc_err, 2)
            self.assertIn("Error executing duckdb", err_buf.getvalue())

            # 7. sync fails with exit code 2 and clear message when local_path is not configured
            err_sync = io.StringIO()
            with mock.patch("sys.stderr", err_sync):
                rc_sync = packrat.main(
                    ["--db", db_path, "sync", "--repo", "octo-org/widgets"]
                )
            self.assertEqual(rc_sync, 2)
            self.assertIn("packrat repo set-path octo-org/widgets", err_sync.getvalue())

            # 8. repo set-path configures local_path and install-cli creates symlink
            out_set = io.StringIO()
            with mock.patch("sys.stdout", out_set):
                rc_set = packrat.main(
                    ["--db", db_path, "repo", "set-path", "octo-org/widgets", tmp]
                )
            self.assertEqual(rc_set, 0)
            self.assertIn("Configured octo-org/widgets", out_set.getvalue())

            bin_dir = Path(tmp) / "bin"
            out_inst = io.StringIO()
            with mock.patch("sys.stdout", out_inst):
                rc_inst = packrat.main(
                    ["--db", db_path, "install-cli", "--bin-dir", str(bin_dir)]
                )
            self.assertEqual(rc_inst, 0)
            self.assertTrue((bin_dir / "packrat").is_symlink())

            # 9. repo rename and repo remove
            out_ren = io.StringIO()
            with mock.patch("sys.stdout", out_ren):
                rc_ren = packrat.main(
                    [
                        "--db",
                        db_path,
                        "repo",
                        "rename",
                        "octo-org/widgets",
                        "new-org/widgets",
                    ]
                )
            self.assertEqual(rc_ren, 0)
            self.assertIn(
                "Renamed octo-org/widgets to new-org/widgets", out_ren.getvalue()
            )
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    packrat.main(
                        [
                            "--db",
                            db_path,
                            "repo",
                            "rename",
                            "new-org/widgets",
                            "dart-lang/sdk",
                        ]
                    ),
                    2,
                )
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "remove", "dart-lang/sdk"]),
                    0,
                )
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "remove", "dart-lang/sdk"]),
                    2,
                )
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                packrat.main(["--db", db_path, "repo", "list", "--json"])
            self.assertEqual(
                [r["full_name"] for r in json.loads(buf.getvalue())],
                ["new-org/widgets"],
            )

    def test_status_reports_incomplete_backfill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "repos.sqlite")
            store = packrat.PackratStore(Path(db_path))
            try:
                repo = store.register_repo("octo-org", "widgets")
                store.set_backfill_state(
                    repo.repo_id, "issues", cursor=None, done_at="2026-11-01T00:00:00Z"
                )
                store.set_backfill_state(
                    repo.repo_id, "prs", cursor="CUR_9", done_at=None
                )
            finally:
                store.close()

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(packrat.main(["--db", db_path, "status"]), 0)
            text = buf.getvalue()
            self.assertIn(
                "Warning: prs backfill incomplete; resumes on next packrat sync", text
            )
            self.assertIn(
                "Warning: discussions backfill incomplete; history not fetched yet",
                text,
            )
            self.assertNotIn("Warning: issues", text)

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(packrat.main(["--db", db_path, "status", "--json"]), 0)
            item = json.loads(buf.getvalue())["repos"][0]
            self.assertEqual(
                item["backfill"]["issues"],
                {
                    "complete": True,
                    "done_at": "2026-11-01T00:00:00Z",
                    "resumable": False,
                },
            )
            self.assertEqual(
                item["backfill"]["prs"],
                {"complete": False, "done_at": None, "resumable": True},
            )
            self.assertEqual(len(item["warnings"]), 2)

            store = packrat.PackratStore(Path(db_path))
            try:
                for stream in ("prs", "discussions"):
                    store.set_backfill_state(
                        repo.repo_id,
                        stream,
                        cursor=None,
                        done_at="2026-11-02T00:00:00Z",
                    )
            finally:
                store.close()
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                packrat.main(["--db", db_path, "status"])
            self.assertNotIn("Warning:", buf.getvalue())

    def test_sync_help_lists_backfill_cap(self) -> None:
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf), self.assertRaises(SystemExit):
            packrat.main(["sync", "--help"])
        self.assertIn("--backfill-max-pages", buf.getvalue())
        self.assertIn("--progress", buf.getvalue())
        self.assertIn("--quiet", buf.getvalue())

    def test_degraded_multi_repo_sync_streams_output_and_exits_one(self) -> None:
        from models import SyncStats

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "repos.sqlite")
            store = packrat.PackratStore(Path(db_path))
            try:
                store.register_repo("octo-org", "widgets")
                store.register_repo("octo-org", "gadgets")
            finally:
                store.close()

            def fake_gh_sync(
                store_obj: object, repo: object, **kwargs: object
            ) -> SyncStats:
                full_name = getattr(repo, "full_name", "")
                reporter = kwargs.get("reporter")
                stats = SyncStats(repo_full_name=full_name)
                if full_name == "octo-org/widgets":
                    stats.issues_upserted = 3
                    err_msg = "github prs sync failed after 0 page(s) (http_error: HTTP 502 Bad Gateway)"
                    stats.errors.append(err_msg)
                    if reporter is not None:
                        reporter.error(full_name, err_msg)
                else:
                    stats.prs_upserted = 5
                return stats

            out_human = io.StringIO()
            err_human = io.StringIO()
            ok_probe = packrat.GhAuthProbe(ok=True)
            with (
                mock.patch("packrat.probe_gh_auth", return_value=ok_probe),
                mock.patch("packrat.sync_github_metadata", side_effect=fake_gh_sync),
                mock.patch("sys.stdout", out_human),
                mock.patch("sys.stderr", err_human),
            ):
                rc = packrat.main(
                    ["--db", db_path, "sync", "--github-only", "--progress", "off"]
                )

            self.assertEqual(rc, 1)
            human_lines = out_human.getvalue().strip().splitlines()
            self.assertEqual(len(human_lines), 2)
            self.assertIn("[octo-org/widgets]", human_lines[0])
            self.assertIn("issues=+3", human_lines[0])
            self.assertIn("[octo-org/gadgets]", human_lines[1])
            self.assertIn("prs=+5", human_lines[1])
            self.assertIn("HTTP 502 Bad Gateway", err_human.getvalue())

            # Verify --json mode keeps stdout valid JSON while returning exit code 1
            out_json = io.StringIO()
            err_json = io.StringIO()
            with (
                mock.patch("packrat.probe_gh_auth", return_value=ok_probe),
                mock.patch("packrat.sync_github_metadata", side_effect=fake_gh_sync),
                mock.patch("sys.stdout", out_json),
                mock.patch("sys.stderr", err_json),
            ):
                rc_json = packrat.main(
                    ["--db", db_path, "sync", "--github-only", "--quiet", "--json"]
                )

            self.assertEqual(rc_json, 1)
            payload = json.loads(out_json.getvalue())
            self.assertEqual(len(payload["results"]), 2)
            self.assertEqual(len(payload["results"][0]["errors"]), 1)
            self.assertEqual(payload["results"][1]["prs_upserted"], 5)
            self.assertIn("HTTP 502 Bad Gateway", err_json.getvalue())

    def test_auto_sync_enable_status_and_disable(self) -> None:
        from auto_sync import (
            disable_auto_sync,
            enable_auto_sync,
            get_auto_sync_status,
        )
        from test_auto_sync import _FakeSystemctl

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            systemd_dir = tmp_path / "systemd_user"
            fake_systemctl = _FakeSystemctl()

            st_initial = get_auto_sync_status(
                systemd_dir=systemd_dir, backend="systemd-user", runner=fake_systemctl
            )
            self.assertFalse(st_initial.enabled)
            self.assertEqual(st_initial.detail, "not installed")

            st_on = enable_auto_sync(
                db_path=db_path,
                interval_min=15,
                fetch=True,
                mode="timer",
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=fake_systemctl,
            )
            self.assertTrue(st_on.enabled)
            self.assertTrue(st_on.active)
            self.assertEqual(st_on.interval_min, 15)
            self.assertTrue(st_on.fetch)
            self.assertTrue((systemd_dir / "packrat-sync.timer").exists())
            self.assertTrue((systemd_dir / "packrat-sync.service").exists())

            st_off = disable_auto_sync(
                systemd_dir=systemd_dir, backend="systemd-user", runner=fake_systemctl
            )
            self.assertFalse(st_off.enabled)
            self.assertFalse((systemd_dir / "packrat-sync.timer").exists())
            self.assertFalse((systemd_dir / "packrat-sync.service").exists())

            # Verify CLI `auto-sync status --json`
            buf = io.StringIO()
            with (
                mock.patch("packrat.get_auto_sync_status", return_value=st_on),
                mock.patch("sys.stdout", buf),
            ):
                rc = packrat.main(
                    ["--db", str(db_path), "auto-sync", "status", "--json"]
                )
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertTrue(parsed["enabled"])
            self.assertEqual(parsed["interval_min"], 15)

    def test_auto_sync_launchd_macos_backend(self) -> None:
        from auto_sync import disable_auto_sync, enable_auto_sync
        from test_auto_sync import _FakeLaunchctl

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            launchd_dir = tmp_path / "launch_agents"
            fake_launchctl = _FakeLaunchctl()

            st_on = enable_auto_sync(
                db_path=db_path,
                interval_min=20,
                fetch=True,
                mode="timer",
                launchd_dir=launchd_dir,
                backend="launchd",
                runner=fake_launchctl,
            )
            self.assertTrue(st_on.enabled)
            self.assertTrue(st_on.active)
            self.assertEqual(st_on.backend, "launchd")
            self.assertEqual(st_on.interval_min, 20)
            self.assertTrue((launchd_dir / "io.packrat.sync.plist").exists())

            st_off = disable_auto_sync(
                launchd_dir=launchd_dir, backend="launchd", runner=fake_launchctl
            )
            self.assertFalse(st_off.enabled)
            self.assertFalse((launchd_dir / "io.packrat.sync.plist").exists())

    def test_auto_sync_cli_idempotent_on_preserves_settings_and_updates(self) -> None:
        import functools

        from auto_sync import enable_auto_sync
        from test_auto_sync import _FakeSystemctl

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            systemd_dir = tmp_path / "systemd_user"
            bound_enable = functools.partial(
                enable_auto_sync,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=_FakeSystemctl(),
            )

            with mock.patch("packrat.enable_auto_sync", side_effect=bound_enable):
                out1 = io.StringIO()
                with mock.patch("sys.stdout", out1):
                    rc1 = packrat.main(
                        [
                            "--db",
                            str(db_path),
                            "auto-sync",
                            "on",
                            "--timer",
                            "--interval-min",
                            "15",
                            "--no-fetch",
                        ]
                    )
                self.assertEqual(rc1, 0)
                self.assertIn("Auto-sync timer: enabled", out1.getvalue())
                self.assertIn("interval=15m, fetch=False", out1.getvalue())

                out2 = io.StringIO()
                with mock.patch("sys.stdout", out2):
                    rc2 = packrat.main(
                        ["--db", str(db_path), "auto-sync", "on", "--timer"]
                    )
                self.assertEqual(rc2, 0)
                self.assertIn("already enabled (no changes", out2.getvalue())
                self.assertIn("interval=15m, fetch=False", out2.getvalue())

                out3 = io.StringIO()
                with mock.patch("sys.stdout", out3):
                    rc3 = packrat.main(
                        [
                            "--db",
                            str(db_path),
                            "auto-sync",
                            "on",
                            "--timer",
                            "--interval-min",
                            "30",
                            "--fetch",
                        ]
                    )
                self.assertEqual(rc3, 0)
                self.assertIn("Auto-sync timer: updated", out3.getvalue())
                self.assertIn("interval=30m, fetch=True", out3.getvalue())

    def test_sync_auto_skips_when_locked_and_fail_mode_exits_one(self) -> None:
        from sync_runtime import SYNC_LOCK_FILENAME, read_auto_sync_run_state
        from test_sync_runtime import _spawn_lock_holder_process

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "repos.sqlite"
            lock_path = db_path.parent / SYNC_LOCK_FILENAME
            with _spawn_lock_holder_process(db_path) as holder_proc:
                holder_bytes = lock_path.read_bytes()

                err_auto = io.StringIO()
                with mock.patch("sys.stderr", err_auto):
                    rc_auto = packrat.main(["--db", str(db_path), "sync", "--auto"])
                self.assertEqual(rc_auto, 0)
                self.assertIn(f"pid={holder_proc.pid}", err_auto.getvalue())
                self.assertEqual(lock_path.read_bytes(), holder_bytes)
                last_run = read_auto_sync_run_state(db_path)
                assert last_run is not None
                self.assertEqual(last_run.outcome, "skipped_locked")

                err_fail = io.StringIO()
                with mock.patch("sys.stderr", err_fail):
                    rc_fail = packrat.main(
                        ["--db", str(db_path), "sync", "--lock-mode", "fail"]
                    )
                self.assertEqual(rc_fail, 1)
                self.assertIn(f"pid={holder_proc.pid}", err_fail.getvalue())
                self.assertEqual(lock_path.read_bytes(), holder_bytes)

    def test_sync_locked_keyring_auto_vs_interactive_and_status_hint(self) -> None:
        from auto_sync import CmdResult, get_auto_sync_status
        from models import SyncStats
        from sync_runtime import read_auto_sync_run_state

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            git_dir = tmp_path / "checkout"
            (git_dir / ".git").mkdir(parents=True)

            store = packrat.PackratStore(db_path)
            try:
                store.register_repo("octo-org", "widgets", local_path=str(git_dir))
                store.register_repo("octo-org", "gadgets", local_path=str(git_dir))
            finally:
                store.close()

            locked_probe = packrat.GhAuthProbe(
                ok=False,
                error_kind="keyring_locked",
                detail="gnome-keyring collection is locked",
            )
            git_synced: list[str] = []
            embedded_repos: list[int] = []

            def fake_git(
                store_obj: object, repo: object, **kwargs: object
            ) -> SyncStats:
                name = getattr(repo, "full_name", "")
                git_synced.append(name)
                return SyncStats(repo_full_name=name, commits_upserted=2)

            def fake_embed(store_obj: object, repo_id: int, **kwargs: object) -> int:
                embedded_repos.append(repo_id)
                return 3

            with (
                mock.patch("packrat.probe_gh_auth", return_value=locked_probe),
                mock.patch("packrat.sync_git_codebase", side_effect=fake_git),
                mock.patch("packrat.sync_repo_embeddings", side_effect=fake_embed),
                mock.patch("packrat.sync_github_metadata") as gh_mock,
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc_auto = packrat.main(
                    ["--db", str(db_path), "sync", "--auto", "--quiet"]
                )
                self.assertEqual(rc_auto, 0)
                gh_mock.assert_not_called()

            self.assertEqual(git_synced, ["octo-org/widgets", "octo-org/gadgets"])
            self.assertEqual(len(embedded_repos), 2)
            last_run = read_auto_sync_run_state(db_path)
            assert last_run is not None
            self.assertEqual(last_run.outcome, "degraded_keyring_locked")
            self.assertEqual(last_run.repos_total, 2)
            self.assertEqual(last_run.git_commits_upserted, 4)
            self.assertEqual(last_run.github_items_upserted, 0)

            st_snap = get_auto_sync_status(
                db_path=db_path,
                systemd_dir=tmp_path / "no_units",
                backend="systemd-user",
                runner=lambda _a: CmdResult(1, "disabled", ""),
            )
            status_out = io.StringIO()
            with (
                mock.patch("packrat.get_auto_sync_status", return_value=st_snap),
                mock.patch("sys.stdout", status_out),
            ):
                self.assertEqual(packrat.main(["--db", str(db_path), "status"]), 0)
            self.assertIn("Last auto-sync:", status_out.getvalue())
            self.assertIn("(degraded_keyring_locked)", status_out.getvalue())
            self.assertIn("Hint: OS keyring was locked", status_out.getvalue())

            with (
                mock.patch("packrat.probe_gh_auth", return_value=locked_probe),
                mock.patch("packrat.sync_git_codebase", side_effect=fake_git),
                mock.patch("packrat.sync_repo_embeddings", side_effect=fake_embed),
                mock.patch("packrat.sync_github_metadata"),
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc_interactive = packrat.main(["--db", str(db_path), "sync", "--quiet"])
                self.assertEqual(rc_interactive, 1)

    def test_sync_auto_zero_repos_and_mixed_local_paths(self) -> None:
        from models import SyncStats
        from sync_runtime import read_auto_sync_run_state

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"

            with (
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc_empty = packrat.main(
                    ["--db", str(db_path), "sync", "--auto", "--json"]
                )
            self.assertEqual(rc_empty, 0)
            empty_rec = read_auto_sync_run_state(db_path)
            assert empty_rec is not None
            self.assertEqual(empty_rec.outcome, "skipped_no_repos")

            git_dir = tmp_path / "widgets_checkout"
            (git_dir / ".git").mkdir(parents=True)
            store = packrat.PackratStore(db_path)
            try:
                store.register_repo("octo-org", "widgets", local_path=str(git_dir))
                store.register_repo("octo-org", "gadgets", local_path=None)
            finally:
                store.close()

            def fake_git(
                store_obj: object, repo: object, **kwargs: object
            ) -> SyncStats:
                name = getattr(repo, "full_name", "")
                local_path = getattr(repo, "local_path", None)
                if not local_path:
                    return SyncStats(
                        repo_full_name=name, skipped_reason="no_local_path"
                    )
                return SyncStats(repo_full_name=name, commits_upserted=1)

            def fake_gh(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                name = getattr(repo, "full_name", "")
                return SyncStats(repo_full_name=name, prs_upserted=3)

            with (
                mock.patch(
                    "packrat.probe_gh_auth", return_value=packrat.GhAuthProbe(ok=True)
                ),
                mock.patch("packrat.sync_git_codebase", side_effect=fake_git),
                mock.patch("packrat.sync_github_metadata", side_effect=fake_gh),
                mock.patch("packrat.sync_repo_embeddings", return_value=1),
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc_mixed = packrat.main(
                    ["--db", str(db_path), "sync", "--auto", "--quiet"]
                )
            self.assertEqual(rc_mixed, 0)
            mixed_rec = read_auto_sync_run_state(db_path)
            assert mixed_rec is not None
            self.assertEqual(mixed_rec.outcome, "ok")
            self.assertEqual(mixed_rec.repos_total, 2)
            self.assertEqual(mixed_rec.git_commits_upserted, 1)
            self.assertEqual(mixed_rec.github_items_upserted, 6)

    def test_sync_auto_propagates_mid_sync_keyring_lock_across_repos(self) -> None:
        from models import SyncStats
        from sync_runtime import read_auto_sync_run_state

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            git_dir = tmp_path / "checkout"
            (git_dir / ".git").mkdir(parents=True)

            store = packrat.PackratStore(db_path)
            try:
                store.register_repo("octo-org", "widgets", local_path=str(git_dir))
                store.register_repo("octo-org", "gadgets", local_path=str(git_dir))
            finally:
                store.close()

            gh_synced: list[str] = []

            def fake_git(
                store_obj: object, repo: object, **kwargs: object
            ) -> SyncStats:
                name = getattr(repo, "full_name", "")
                return SyncStats(repo_full_name=name, commits_upserted=1)

            def fake_gh(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                name = getattr(repo, "full_name", "")
                gh_synced.append(name)
                err_msg = (
                    "github issues sync failed after 1 page(s) "
                    "(keyring_locked: OS keyring is locked)"
                )
                return SyncStats(
                    repo_full_name=name,
                    errors=[err_msg],
                )

            with (
                mock.patch(
                    "packrat.probe_gh_auth", return_value=packrat.GhAuthProbe(ok=True)
                ),
                mock.patch("packrat.sync_git_codebase", side_effect=fake_git),
                mock.patch("packrat.sync_github_metadata", side_effect=fake_gh),
                mock.patch("packrat.sync_repo_embeddings", return_value=2),
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc = packrat.main(["--db", str(db_path), "sync", "--auto", "--quiet"])

            self.assertEqual(rc, 0)
            self.assertEqual(gh_synced, ["octo-org/widgets"])
            last_run = read_auto_sync_run_state(db_path)
            assert last_run is not None
            self.assertEqual(last_run.outcome, "degraded_keyring_locked")
            self.assertEqual(last_run.repos_total, 2)
            self.assertEqual(last_run.git_commits_upserted, 2)

    def _register_two_repos(self, tmp_path: Path) -> Path:
        """Registers octo-org/widgets and octo-org/gadgets with a fake checkout."""
        db_path = tmp_path / "repos.sqlite"
        git_dir = tmp_path / "checkout"
        (git_dir / ".git").mkdir(parents=True)
        store = packrat.PackratStore(db_path)
        try:
            store.register_repo("octo-org", "widgets", local_path=str(git_dir))
            store.register_repo("octo-org", "gadgets", local_path=str(git_dir))
        finally:
            store.close()
        return db_path

    def _run_auto_sync(
        self, db_path: Path, fake_gh: object
    ) -> tuple[int, dict[str, object]]:
        """Runs `sync --auto --json` with git and embeddings stubbed out."""
        from models import SyncStats

        def fake_git(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
            return SyncStats(repo_full_name=getattr(repo, "full_name", ""))

        out = io.StringIO()
        with (
            mock.patch(
                "packrat.probe_gh_auth", return_value=packrat.GhAuthProbe(ok=True)
            ),
            mock.patch("packrat.sync_git_codebase", side_effect=fake_git),
            mock.patch("packrat.sync_github_metadata", side_effect=fake_gh),
            mock.patch("packrat.sync_repo_embeddings", return_value=0),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", io.StringIO()),
        ):
            rc = packrat.main(
                ["--db", str(db_path), "sync", "--auto", "--json", "--quiet"]
            )
        return rc, json.loads(out.getvalue())

    def test_sync_auto_deferred_rate_limit_exits_zero_and_status_shows_budget(
        self,
    ) -> None:
        from auto_sync import CmdResult, get_auto_sync_status
        from models import SyncStats
        from rate_budget import RATE_BUDGET_FILENAME, RateBudget
        from sync_runtime import read_auto_sync_run_state

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = self._register_two_repos(tmp_path)
            budgets: list[object] = []

            def fake_gh(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                budgets.append(kwargs.get("budget"))
                name = getattr(repo, "full_name", "")
                if name == "octo-org/widgets":
                    return SyncStats(repo_full_name=name, prs_upserted=1)
                return SyncStats(
                    repo_full_name=name,
                    deferred=True,
                    deferred_streams=["issues", "prs"],
                )

            rc, payload = self._run_auto_sync(db_path, fake_gh)

            self.assertEqual(rc, 0)
            self.assertEqual(payload["outcome"], "deferred_rate_limit")
            results = payload["results"]
            assert isinstance(results, list)
            self.assertEqual(results[0]["deferred_streams"], [])
            self.assertEqual(results[1]["deferred_streams"], ["issues", "prs"])
            self.assertEqual(len(budgets), 2)
            self.assertIs(budgets[0], budgets[1])
            assert isinstance(budgets[0], RateBudget)
            self.assertEqual(budgets[0].path, tmp_path / RATE_BUDGET_FILENAME)
            last_run = read_auto_sync_run_state(db_path)
            assert last_run is not None
            self.assertEqual(last_run.outcome, "deferred_rate_limit")
            self.assertIn("octo-org/gadgets: issues, prs", last_run.detail)

            budgets[0].record(
                {
                    "X-RateLimit-Resource": "graphql",
                    "X-RateLimit-Limit": "5000",
                    "X-RateLimit-Remaining": "750",
                    "X-RateLimit-Reset": "4102444800",
                },
                worker="test",
            )
            st_snap = get_auto_sync_status(
                db_path=db_path,
                systemd_dir=tmp_path / "no_units",
                backend="systemd-user",
                runner=lambda _a: CmdResult(1, "disabled", ""),
            )
            text_out = io.StringIO()
            json_out = io.StringIO()
            with mock.patch("packrat.get_auto_sync_status", return_value=st_snap):
                with mock.patch("sys.stdout", text_out):
                    self.assertEqual(packrat.main(["--db", str(db_path), "status"]), 0)
                with mock.patch("sys.stdout", json_out):
                    self.assertEqual(
                        packrat.main(["--db", str(db_path), "status", "--json"]), 0
                    )
            self.assertIn("(deferred_rate_limit)", text_out.getvalue())
            self.assertIn(
                "GitHub budget (graphql): 750/5000 (15%)", text_out.getvalue()
            )
            status = json.loads(json_out.getvalue())
            self.assertEqual(status["rate_budget"]["graphql"]["remaining"], 750)
            self.assertFalse(status["rate_budget"]["graphql"]["expired"])

    def test_sync_auto_error_plus_deferral_reports_degraded_error(self) -> None:
        from models import SyncStats

        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._register_two_repos(Path(tmp))

            def fake_gh(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                name = getattr(repo, "full_name", "")
                if name == "octo-org/widgets":
                    return SyncStats(
                        repo_full_name=name,
                        errors=[
                            "github prs sync failed after 0 page(s) (http_error: HTTP 502)"
                        ],
                    )
                return SyncStats(
                    repo_full_name=name, deferred=True, deferred_streams=["issues"]
                )

            rc, payload = self._run_auto_sync(db_path, fake_gh)

            self.assertEqual(rc, 1)
            self.assertEqual(payload["outcome"], "degraded_error")

    def test_sync_auto_sqlite_write_failure_is_recorded_not_raised(self) -> None:
        import sqlite3

        from models import SyncStats
        from sync_runtime import read_auto_sync_run_state

        with tempfile.TemporaryDirectory() as tmp:
            db_path = self._register_two_repos(Path(tmp))
            synced: list[str] = []

            def fake_gh(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                name = getattr(repo, "full_name", "")
                if name == "octo-org/widgets":
                    raise sqlite3.OperationalError("database or disk is full")
                synced.append(name)
                return SyncStats(repo_full_name=name)

            rc, payload = self._run_auto_sync(db_path, fake_gh)

            self.assertEqual(rc, 1)
            self.assertEqual(synced, ["octo-org/gadgets"])
            self.assertEqual(payload["outcome"], "degraded_error")
            results = payload["results"]
            assert isinstance(results, list)
            self.assertIn("database or disk is full", results[0]["errors"][0])
            last_run = read_auto_sync_run_state(db_path)
            assert last_run is not None
            self.assertEqual(last_run.outcome, "degraded_error")


T0 = 1_800_000_000.0
"""Fixed epoch seconds for change-feed rows in the live CLI tests."""


class TestLiveCli(unittest.TestCase):
    """Tests `serve`, `watch`, `changes`, `refresh`, `sync --backfill-only`, and status additions."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "repos.sqlite"
        self.store = packrat.PackratStore(self.db_path)
        self.repo = self.store.register_repo("Octo-Org", "Widgets")

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = packrat.main(["--db", str(self.db_path), *argv])
        return rc, out.getvalue(), err.getvalue()

    def _run_json(self, *argv: str) -> dict[str, object]:
        rc, out, err = self._run(*argv)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def _watch(self, action: str, *items: str) -> dict[str, object]:
        return self._run_json(
            "watch", action, "--consumer", "monitor", "--id", "c1", "--json", *items
        )

    def _leased_keys(self, consumer_id: str = "c1") -> list[str]:
        return [
            w.item_key
            for w in self.store.active_watches()
            if w.consumer_id == consumer_id
        ]

    def _seed_changes(self, count: int) -> None:
        for i in range(count):
            self.store.append_change("Octo-Org/Widgets#1", "pr", ["title"], now=T0 + i)

    # ----- watch --------------------------------------------------------------------

    def test_watch_add_canonicalizes_and_creates_items_repo(self) -> None:
        result = self._watch("add", "octo-org/widgets#5", "new-org/thing#7")
        self.assertEqual(result["leased"], ["Octo-Org/Widgets#5", "new-org/thing#7"])
        self.assertEqual((result["freshness_s"], result["ttl_s"]), (120, 900))
        new_repo = self.store.get_repo("new-org/thing")
        assert new_repo is not None
        self.assertEqual(new_repo.mode, "items")
        lease = next(
            w for w in self.store.active_watches() if w.item_key == "Octo-Org/Widgets#5"
        )
        self.assertEqual(
            (lease.consumer, lease.consumer_id, lease.freshness_s),
            ("monitor", "c1", 120),
        )

    def test_watch_renew_and_remove_all_or_subset(self) -> None:
        self._watch(
            "add", "Octo-Org/Widgets#1", "Octo-Org/Widgets#2", "Octo-Org/Widgets#3"
        )
        self._run_json(
            "watch",
            "add",
            "--consumer",
            "monitor",
            "--id",
            "c2",
            "--json",
            "Octo-Org/Widgets#1",
        )
        self.assertEqual(self._watch("renew")["renewed"], 3)
        self.assertEqual(self._watch("renew", "octo-org/widgets#2")["renewed"], 1)
        self.assertEqual(self._watch("remove", "OCTO-ORG/widgets#2")["removed"], 1)
        self.assertEqual(
            self._leased_keys(), ["Octo-Org/Widgets#1", "Octo-Org/Widgets#3"]
        )
        self.assertEqual(self._watch("remove")["removed"], 2)
        self.assertEqual(self._leased_keys(), [])
        self.assertEqual(self._leased_keys("c2"), ["Octo-Org/Widgets#1"])

    def test_watch_add_without_items_exits_two(self) -> None:
        rc, _, err = self._run("watch", "add", "--consumer", "monitor", "--id", "c1")
        self.assertEqual(rc, 2)
        self.assertIn("at least one ITEM", err)

    # ----- changes ------------------------------------------------------------------

    def test_changes_returns_rows_and_last_seq(self) -> None:
        self._seed_changes(3)
        payload = self._run_json("changes", "--since", "1", "--json")
        self.assertEqual(payload["seq"], 3)
        changes = payload["changes"]
        assert isinstance(changes, list)
        self.assertEqual([c["seq"] for c in changes], [2, 3])
        self.assertEqual(changes[0]["item_key"], "Octo-Org/Widgets#1")
        self.assertEqual(changes[0]["fields"], ["title"])
        limited = self._run_json("changes", "--since", "0", "--limit", "1", "--json")
        self.assertEqual(limited["seq"], 1)

    def test_changes_wait_returns_early_on_new_row(self) -> None:
        self._seed_changes(2)

        def append_later() -> None:
            time.sleep(0.3)
            writer = packrat.PackratStore(self.db_path)
            try:
                writer.append_change("Octo-Org/Widgets#9", "checks", ["check_runs"])
            finally:
                writer.close()

        thread = threading.Thread(target=append_later)
        start = time.monotonic()
        thread.start()
        payload = self._run_json("changes", "--since", "2", "--wait", "5", "--json")
        elapsed = time.monotonic() - start
        thread.join()
        self.assertLess(elapsed, 3.0)
        self.assertEqual(payload["seq"], 3)
        changes = payload["changes"]
        assert isinstance(changes, list)
        self.assertEqual(
            [(c["item_key"], c["kind"]) for c in changes],
            [("Octo-Org/Widgets#9", "checks")],
        )

    def test_changes_wait_times_out_with_no_rows(self) -> None:
        self._seed_changes(3)
        start = time.monotonic()
        payload = self._run_json("changes", "--since", "3", "--wait", "0.5", "--json")
        self.assertGreaterEqual(time.monotonic() - start, 0.4)
        self.assertEqual(payload, {"seq": 3, "changes": []})

    def test_changes_reset_after_prune_and_beyond_max(self) -> None:
        self._seed_changes(3)
        self.assertEqual(
            self._run_json("changes", "--since", "10", "--json"),
            {"reset": True, "seq": 3},
        )
        # The default 7-day window ends at T0 + 2, so seqs 1 and 2 are pruned.
        self.store.prune(now=T0 + 2 + 7 * 86400)
        self.assertEqual(
            self._run_json("changes", "--since", "0", "--json"),
            {"reset": True, "seq": 3},
        )
        kept = self._run_json("changes", "--since", "2", "--json")
        self.assertEqual(kept["seq"], 3)

    def test_changes_bootstrap_from_documented_large_since(self) -> None:
        # M8: reader_contract.md tells readers to bootstrap with this --since.
        self._seed_changes(3)
        bootstrap = self._run_json("changes", "--since", "9007199254740991", "--json")
        self.assertEqual(bootstrap, {"reset": True, "seq": 3})
        self.store.append_change("Octo-Org/Widgets#2", "pr", ["state"], now=T0 + 10)
        follow = self._run_json("changes", "--since", str(bootstrap["seq"]), "--json")
        self.assertEqual(follow["seq"], 4)

    # ----- refresh ------------------------------------------------------------------

    def test_refresh_queues_canonical_keys(self) -> None:
        payload = self._run_json(
            "refresh", "octo-org/widgets#5", "other/repo#2", "--json"
        )
        self.assertEqual(payload["requested"], ["Octo-Org/Widgets#5", "other/repo#2"])
        self.assertEqual(
            sorted(self.store.take_refresh_requests()),
            ["Octo-Org/Widgets#5", "other/repo#2"],
        )
        rc, out, _ = self._run("refresh", "octo-org/widgets#6")
        self.assertEqual(rc, 0)
        self.assertIn("Octo-Org/Widgets#6", out)

    # ----- serve --------------------------------------------------------------------

    def test_serve_once_dry_run_makes_and_records_no_calls(self) -> None:
        self.store.add_watch("monitor", "c1", ["Octo-Org/Widgets#5"], 120, 900)
        payload = self._run_json("serve", "--once", "--dry-run", "--json")
        self.assertTrue(payload["dry_run"])
        calls = payload["intended_calls"]
        assert isinstance(calls, list)
        self.assertTrue(any(c.startswith("GET notifications") for c in calls), calls)
        self.assertTrue(any(c.startswith("graphql ") for c in calls), calls)
        self.assertIsNone(self.store.read_heartbeat())
        api_rows = self.store.conn.execute("SELECT COUNT(*) FROM api_calls").fetchone()[
            0
        ]
        self.assertEqual(api_rows, 0)

    def _table_counts(self, *tables: str) -> dict[str, int]:
        return {
            t: int(self.store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in tables
        }

    def test_serve_dry_run_writes_nothing_and_reports_no_errors(self) -> None:
        # L10: a dry run must not prune, consume refresh requests, or save state.
        self.store.add_watch("monitor", "c1", ["Octo-Org/Widgets#5"], 120, 900)
        self.store.add_watch("monitor", "old", ["Octo-Org/Widgets#7"], 120, 10, now=T0)
        self.store.request_refresh(["Octo-Org/Widgets#6"])
        self._seed_changes(2)
        tables = (
            "watches",
            "refresh_requests",
            "changes",
            "service_state",
            "viewer_sets",
        )
        before = self._table_counts(*tables)

        payload = self._run_json("serve", "--once", "--dry-run", "--json")
        report = payload["report"]
        assert isinstance(report, dict)
        self.assertEqual(report["errors"], [])
        self.assertIn("Octo-Org/Widgets#6", report["requested"])
        calls = payload["intended_calls"]
        assert isinstance(calls, list)
        self.assertTrue(any("number: 6" in c for c in calls), calls)
        self.assertEqual(self._table_counts(*tables), before)
        self.assertEqual(self.store.take_refresh_requests(), ["Octo-Org/Widgets#6"])

    def test_dry_run_store_drops_writes_and_passes_reads(self) -> None:
        from models import PrLiveRecord
        from serve_cli import DryRunStore

        dry = DryRunStore(self.store)
        record = PrLiveRecord(
            repo_id=self.repo.repo_id, number=1, title="t", state="OPEN", updated_at="u"
        )
        self.assertEqual(dry.upsert_pr_live(record), [])
        self.assertEqual(dry.replace_viewer_set("authored", ["a/b#1"]), ([], []))
        self.assertEqual(dry.add_watch("m", "c", ["a/b#1"], 120, 900), ["a/b#1"])
        self.assertEqual(dry.request_refresh(["a/b#2"]), ["a/b#2"])
        self.assertIsNone(
            dry.update_repo_sync_state(self.repo.repo_id, events_etag="x")
        )
        placeholder = dry.ensure_items_repo("new-org", "thing")
        self.assertEqual((placeholder.repo_id, placeholder.mode), (0, "items"))
        self.assertEqual(
            dry.ensure_items_repo("octo-org", "widgets").repo_id, self.repo.repo_id
        )
        self.assertEqual(dry.get_repo("Octo-Org/Widgets").repo_id, self.repo.repo_id)
        counts = self._table_counts(
            "pull_requests", "viewer_sets", "watches", "refresh_requests"
        )
        self.assertEqual(set(counts.values()), {0})
        self.assertIsNone(self.store.get_repo("new-org/thing"))
        self.assertIsNone(self.store.get_repo_by_id(self.repo.repo_id).events_etag)

    # ----- mirror adapter -----------------------------------------------------------

    def _mirror_adapter(self) -> Any:
        from rate_budget import RATE_BUDGET_FILENAME, RateBudget
        from serve_cli import MirrorAdapter

        transport = mock.Mock()
        budget = RateBudget(Path(self.tmp.name) / RATE_BUDGET_FILENAME)
        return MirrorAdapter(self.store, transport, budget)

    def _fake_mirror_sync(self, fields: dict[str, Any], synced: list[str]) -> Any:
        """Fakes `sync_github_metadata`: the first repo gets `fields`, the rest succeed."""
        from models import SyncStats

        def fake_sync(_store: Any, repo: Any, **kwargs: Any) -> SyncStats:
            self.assertIs(kwargs.get("skip_events_poll"), True)
            synced.append(repo.full_name)
            first = repo.repo_id == self.repo.repo_id
            return SyncStats(repo_full_name=repo.full_name, **(fields if first else {}))

        return fake_sync

    def test_mirror_adapter_raises_on_incomplete_runs(self) -> None:
        # M1: a deferred or failed mirror run must not look like success to the service.
        from gh_transport import BudgetDeferred, GhApiError

        other = self.store.register_repo("octo-org", "gadgets")
        items = self.store.ensure_items_repo("items-org", "thing")
        ids = [self.repo.repo_id, other.repo_id, items.repo_id]
        cases: list[tuple[dict[str, Any], type[Exception] | None]] = [
            ({}, None),
            ({"deferred": True, "deferred_streams": ["prs"]}, BudgetDeferred),
            ({"errors": ["github prs sync failed (HTTP 502)"]}, GhApiError),
        ]
        for fields, expected in cases:
            with self.subTest(fields=fields):
                synced: list[str] = []
                fake_sync = self._fake_mirror_sync(fields, synced)
                with mock.patch(
                    "serve_cli.sync_github_metadata", side_effect=fake_sync
                ):
                    adapter = self._mirror_adapter()
                    if expected is None:
                        self.assertEqual(len(adapter.sync_mirror(ids)), 2)
                    else:
                        with self.assertRaises(expected):
                            adapter.sync_mirror(ids)
                self.assertEqual(synced, ["Octo-Org/Widgets", "octo-org/gadgets"])

    # ----- argument validation and items repos --------------------------------------

    def test_live_commands_reject_out_of_range_numbers(self) -> None:
        # L3: out-of-range values exit 2 instead of being stored or acted on.
        watch = ["watch", "add", "--consumer", "m", "--id", "c", "Octo-Org/Widgets#1"]
        cases = [
            [*watch, "--ttl", "0"],
            [*watch, "--freshness", "-5"],
            [*watch, "--ttl", "soon"],
            ["changes", "--since", "-1"],
            ["changes", "--since", "0", "--limit", "0"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    self._run(*argv)
                self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(self.store.active_watches(), [])

    def test_sync_items_repo_explains_items_mode(self) -> None:
        # L12: naming an items repo must not claim that nothing is registered.
        self.store.ensure_items_repo("items-org", "thing")
        rc, _, err = self._run("sync", "--repo", "items-org/thing", "--github-only")
        self.assertEqual(rc, 2)
        self.assertIn("items-org/thing is in items mode", err)
        self.assertIn("packrat repo add items-org/thing", err)

    def test_second_serve_exits_four_while_lock_held(self) -> None:
        from sync_runtime import acquire_serve_lock

        with acquire_serve_lock(self.db_path) as lock:
            self.assertTrue(lock.acquired)
            rc, _, err = self._run("serve", "--once", "--dry-run")
        self.assertEqual(rc, 4)
        self.assertIn("already running", err)

    # ----- sync --backfill-only and status ------------------------------------------

    def test_sync_backfill_only_skips_incremental_sync(self) -> None:
        from models import SyncStats

        ok_probe = packrat.GhAuthProbe(ok=True)
        stats = SyncStats(repo_full_name="Octo-Org/Widgets")
        with (
            mock.patch("packrat.probe_gh_auth", return_value=ok_probe),
            mock.patch("packrat.sync_backfill_only", return_value=stats) as backfill,
            mock.patch("packrat.sync_github_metadata") as delta,
        ):
            rc, _, err = self._run(
                "sync", "--backfill-only", "--progress", "off", "--json"
            )
        self.assertEqual(rc, 0, err)
        backfill.assert_called_once()
        self.assertEqual(backfill.call_args.args[1].full_name, "Octo-Org/Widgets")
        delta.assert_not_called()

        with self.assertRaises(SystemExit) as ctx:
            self._run("sync", "--github-only", "--backfill-only")
        self.assertEqual(ctx.exception.code, 2)

        with self.assertRaises(SystemExit) as ctx_dl:
            self._run("sync", "--download-model", "--no-download-model")
        self.assertEqual(ctx_dl.exception.code, 2)

    def test_status_reports_service_and_leases(self) -> None:
        rc, out, _ = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("Service: never ran", out)

        self.store.add_watch(
            "monitor", "c1", ["Octo-Org/Widgets#5", "Octo-Org/Widgets#6"], 120, 900
        )
        self.store.add_watch("inflight", "x", ["Octo-Org/Widgets#5"], 120, 900)
        payload = self._run_json("status", "--json")
        service = payload["service"]
        assert isinstance(service, dict)
        self.assertEqual(service["leases_by_consumer"], {"inflight": 1, "monitor": 2})
        self.assertIsNone(service["beat_age_s"])

    # ----- auto-sync service mode ---------------------------------------------------

    def test_auto_sync_on_installs_service_and_status_reports_it(self) -> None:
        import functools

        from auto_sync import (
            SERVE_SERVICE_NAME,
            TIMER_NAME,
            enable_auto_sync,
            get_auto_sync_status,
        )
        from test_auto_sync import _FakeSystemctl

        systemd_dir = Path(self.tmp.name) / "systemd_user"
        fake = _FakeSystemctl()
        backend = {
            "systemd_dir": systemd_dir,
            "backend": "systemd-user",
            "runner": fake,
        }
        bound_enable = functools.partial(enable_auto_sync, **backend)
        bound_status = functools.partial(get_auto_sync_status, **backend)
        with (
            mock.patch("packrat.enable_auto_sync", side_effect=bound_enable),
            mock.patch("packrat.get_auto_sync_status", side_effect=bound_status),
        ):
            rc, out, err = self._run("auto-sync", "on", "--timer")
            self.assertEqual(rc, 0, err)
            self.assertIn(TIMER_NAME, fake.enabled)

            rc, out, err = self._run("auto-sync", "on")
            self.assertEqual(rc, 0, err)
            self.assertTrue(out.startswith("Auto-sync service:"), out)
            self.assertEqual(fake.enabled, {SERVE_SERVICE_NAME})
            self.assertTrue((systemd_dir / SERVE_SERVICE_NAME).exists())

            rc, out, err = self._run("timer", "on")
            self.assertEqual(rc, 0, err)
            self.assertTrue(out.startswith("Auto-sync timer:"), out)
            self.assertIn(TIMER_NAME, fake.enabled)

            rc, out, err = self._run("auto-sync", "on")
            self.assertEqual(rc, 0, err)

            rc, out, _ = self._run("status")
        self.assertEqual(rc, 0)
        self.assertIn("Auto-sync: packrat.service (active=True", out)

    def test_read_only_commands_do_not_rewrite_existing_init_sql(self) -> None:
        init_sql_path = self.db_path.parent / packrat.INIT_SQL_FILENAME
        init_sql_path.write_text("-- existing init.sql\n", encoding="utf-8")
        with mock.patch.object(packrat.PackratStore, "write_init_sql") as write_mock:
            self.assertEqual(self._run("status", "--json")[0], 0)
            self.assertEqual(self._run("search", "widget", "--json")[0], 0)
            self.assertEqual(self._run("repo", "list", "--json")[0], 0)
            write_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
