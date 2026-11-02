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
                    ["--db", db_path, "repo", "add", "dart-lang/sdk", "--path", "/tmp/sdk", "--branch", "main"]
                )
            self.assertEqual(rc, 0)
            self.assertIn("dart-lang/sdk", buf.getvalue())
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(packrat.main(["--db", db_path, "repo", "add", "octo-org/widgets"]), 0)

            # 2b. malformed names are rejected.
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(packrat.main(["--db", db_path, "repo", "add", "widgets"]), 2)
                self.assertEqual(packrat.main(["--db", db_path, "repo", "add", "a/b/c"]), 2)

            # 3. status --repo octo-org/widgets --json
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(["--db", db_path, "status", "--repo", "octo-org/widgets", "--json"])
            self.assertEqual(rc, 0)
            status_data = json.loads(buf.getvalue())
            self.assertEqual(len(status_data["repos"]), 1)
            self.assertEqual(status_data["repos"][0]["full_name"], "octo-org/widgets")

            # 4. search --json (empty DB returns [])
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(["--db", db_path, "search", "surface controller", "--json"])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf.getvalue()), [])

            # 5. sql --format json
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = packrat.main(
                    ["--db", db_path, "sql", "SELECT full_name FROM kb.repos ORDER BY repo_id", "--format", "json"]
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
                rc_set = packrat.main(["--db", db_path, "repo", "set-path", "octo-org/widgets", tmp])
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
                rc_ren = packrat.main(["--db", db_path, "repo", "rename", "octo-org/widgets", "new-org/widgets"])
            self.assertEqual(rc_ren, 0)
            self.assertIn("Renamed octo-org/widgets to new-org/widgets", out_ren.getvalue())
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(
                    packrat.main(["--db", db_path, "repo", "rename", "new-org/widgets", "dart-lang/sdk"]), 2
                )
            with mock.patch("sys.stdout", io.StringIO()):
                self.assertEqual(packrat.main(["--db", db_path, "repo", "remove", "dart-lang/sdk"]), 0)
            with mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(packrat.main(["--db", db_path, "repo", "remove", "dart-lang/sdk"]), 2)
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                packrat.main(["--db", db_path, "repo", "list", "--json"])
            self.assertEqual([r["full_name"] for r in json.loads(buf.getvalue())], ["new-org/widgets"])

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


if __name__ == "__main__":
    unittest.main()
