"""Unit tests for the packrat CLI dispatcher (status, repo, search, sql)."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
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
                    packrat.main(["--db", db_path, "repo", "add", "octo-org/widgets"]), 0
                )

            # 2b. malformed names are rejected.
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(packrat.main(["--db", db_path, "repo", "add", "widgets"]), 2)
                self.assertEqual(packrat.main(["--db", db_path, "repo", "add", "a/b/c"]), 2)

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
                rc = packrat.main(["--db", db_path, "search", "surface controller", "--json"])
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
                mock.patch.dict("os.environ", {"DUCKDB_BIN": "/nonexistent/duckdb_binary"}),
                mock.patch("sys.stderr", err_buf),
            ):
                rc_err = packrat.main(["--db", db_path, "sql", "SELECT 1"])
            self.assertEqual(rc_err, 2)
            self.assertIn("Error executing duckdb", err_buf.getvalue())

            # 7. sync fails with exit code 2 and clear message when local_path is not configured
            err_sync = io.StringIO()
            with mock.patch("sys.stderr", err_sync):
                rc_sync = packrat.main(["--db", db_path, "sync", "--repo", "octo-org/widgets"])
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
                rc_inst = packrat.main(["--db", db_path, "install-cli", "--bin-dir", str(bin_dir)])
            self.assertEqual(rc_inst, 0)
            self.assertTrue((bin_dir / "packrat").is_symlink())

            # 9. repo rename and repo remove
            out_ren = io.StringIO()
            with mock.patch("sys.stdout", out_ren):
                rc_ren = packrat.main(
                    ["--db", db_path, "repo", "rename", "octo-org/widgets", "new-org/widgets"]
                )
            self.assertEqual(rc_ren, 0)
            self.assertIn("Renamed octo-org/widgets to new-org/widgets", out_ren.getvalue())
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    packrat.main(
                        ["--db", db_path, "repo", "rename", "new-org/widgets", "dart-lang/sdk"]
                    ),
                    2,
                )
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "remove", "dart-lang/sdk"]), 0
                )
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "remove", "dart-lang/sdk"]), 2
                )
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                packrat.main(["--db", db_path, "repo", "list", "--json"])
            self.assertEqual(
                [r["full_name"] for r in json.loads(buf.getvalue())], ["new-org/widgets"]
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
                store.set_backfill_state(repo.repo_id, "prs", cursor="CUR_9", done_at=None)
            finally:
                store.close()

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(packrat.main(["--db", db_path, "status"]), 0)
            text = buf.getvalue()
            self.assertIn("Warning: prs backfill incomplete; resumes on next packrat sync", text)
            self.assertIn("Warning: discussions backfill incomplete; history not fetched yet", text)
            self.assertNotIn("Warning: issues", text)

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                self.assertEqual(packrat.main(["--db", db_path, "status", "--json"]), 0)
            item = json.loads(buf.getvalue())["repos"][0]
            self.assertEqual(
                item["backfill"]["issues"],
                {"complete": True, "done_at": "2026-11-01T00:00:00Z", "resumable": False},
            )
            self.assertEqual(
                item["backfill"]["prs"], {"complete": False, "done_at": None, "resumable": True}
            )
            self.assertEqual(len(item["warnings"]), 2)

            store = packrat.PackratStore(Path(db_path))
            try:
                for stream in ("prs", "discussions"):
                    store.set_backfill_state(
                        repo.repo_id, stream, cursor=None, done_at="2026-11-02T00:00:00Z"
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

            def fake_gh_sync(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                full_name = getattr(repo, "full_name", "")
                reporter = kwargs.get("reporter")
                stats = SyncStats(repo_full_name=full_name)
                if full_name == "octo-org/widgets":
                    stats.issues_upserted = 3
                    err_msg = (
                        "github prs sync failed after 0 page(s) (http_error: HTTP 502 Bad Gateway)"
                    )
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
                rc = packrat.main(["--db", db_path, "sync", "--github-only", "--progress", "off"])

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
            CmdResult,
            disable_auto_sync,
            enable_auto_sync,
            get_auto_sync_status,
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            systemd_dir = tmp_path / "systemd_user"
            enabled_flag = False

            def fake_systemctl(args: list[str]) -> CmdResult:
                nonlocal enabled_flag
                cmd_key = " ".join(args[:2])
                if cmd_key == "enable --now":
                    enabled_flag = True
                    return CmdResult(0, "", "")
                if cmd_key == "disable --now":
                    enabled_flag = False
                    return CmdResult(0, "", "")
                if args == ["daemon-reload"]:
                    return CmdResult(0, "", "")
                state_map = {
                    ("is-enabled", True): CmdResult(0, "enabled", ""),
                    ("is-enabled", False): CmdResult(1, "disabled", ""),
                    ("is-active", True): CmdResult(0, "active", ""),
                    ("is-active", False): CmdResult(3, "inactive", ""),
                }
                return state_map.get((args[0], enabled_flag), CmdResult(1, "", "unexpected"))

            st_initial = get_auto_sync_status(
                systemd_dir=systemd_dir, backend="systemd-user", runner=fake_systemctl
            )
            self.assertFalse(st_initial.enabled)
            self.assertEqual(st_initial.detail, "not installed")

            st_on = enable_auto_sync(
                db_path=db_path,
                interval_min=15,
                fetch=True,
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
                rc = packrat.main(["--db", str(db_path), "auto-sync", "status", "--json"])
            self.assertEqual(rc, 0)
            parsed = json.loads(buf.getvalue())
            self.assertTrue(parsed["enabled"])
            self.assertEqual(parsed["interval_min"], 15)

    def test_auto_sync_launchd_macos_backend(self) -> None:
        from auto_sync import CmdResult, disable_auto_sync, enable_auto_sync

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            launchd_dir = tmp_path / "launch_agents"
            loaded = False

            def fake_launchctl(args: list[str]) -> CmdResult:
                nonlocal loaded
                cmd_key = " ".join(args[:2])
                if cmd_key == "load -w":
                    loaded = True
                    return CmdResult(0, "", "")
                if cmd_key == "unload -w":
                    loaded = False
                    return CmdResult(0, "", "")
                if args[0] == "list":
                    return CmdResult(0 if loaded else 113, "loaded" if loaded else "", "")
                return CmdResult(1, "", "unexpected")

            st_on = enable_auto_sync(
                db_path=db_path,
                interval_min=20,
                fetch=True,
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
                    rc2 = packrat.main(["--db", str(db_path), "auto-sync", "on"])
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
                    rc_fail = packrat.main(["--db", str(db_path), "sync", "--lock-mode", "fail"])
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

            def fake_git(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
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
                rc_auto = packrat.main(["--db", str(db_path), "sync", "--auto", "--quiet"])
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

            with mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                rc_empty = packrat.main(["--db", str(db_path), "sync", "--auto", "--json"])
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

            def fake_git(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                name = getattr(repo, "full_name", "")
                local_path = getattr(repo, "local_path", None)
                if not local_path:
                    return SyncStats(repo_full_name=name, skipped_reason="no_local_path")
                return SyncStats(repo_full_name=name, commits_upserted=1)

            def fake_gh(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
                name = getattr(repo, "full_name", "")
                return SyncStats(repo_full_name=name, prs_upserted=3)

            with (
                mock.patch("packrat.probe_gh_auth", return_value=packrat.GhAuthProbe(ok=True)),
                mock.patch("packrat.sync_git_codebase", side_effect=fake_git),
                mock.patch("packrat.sync_github_metadata", side_effect=fake_gh),
                mock.patch("packrat.sync_repo_embeddings", return_value=1),
                mock.patch("sys.stdout", io.StringIO()),
                mock.patch("sys.stderr", io.StringIO()),
            ):
                rc_mixed = packrat.main(["--db", str(db_path), "sync", "--auto", "--quiet"])
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

            def fake_git(store_obj: object, repo: object, **kwargs: object) -> SyncStats:
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
                mock.patch("packrat.probe_gh_auth", return_value=packrat.GhAuthProbe(ok=True)),
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


if __name__ == "__main__":
    unittest.main()
