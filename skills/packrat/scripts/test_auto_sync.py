"""Unit tests for idempotent auto-sync timer management and unit hardening."""

from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from auto_sync import (
    LAUNCHD_LABEL,
    SERVICE_NAME,
    TIMER_NAME,
    CmdResult,
    _build_portable_path,
    _parse_launchd_metadata,
    _parse_systemd_metadata,
    disable_auto_sync,
    enable_auto_sync,
    get_auto_sync_status,
    render_launchd_plist,
    render_service_unit,
    render_timer_unit,
)
from sync_runtime import AutoSyncRunRecord, write_auto_sync_run_state


class _FakeSystemctl:
    """Stateful fake runner for `systemctl --user` unit tests."""

    def __init__(self, *, failed: bool = False, fail_rc: int = 0) -> None:
        self.enabled = False
        self.active = False
        self.failed = failed
        self.fail_rc = fail_rc
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> CmdResult:
        argv = tuple(args)
        self.calls.append(argv)
        if argv == ("daemon-reload",):
            return CmdResult(0, "", "")
        if argv == ("enable", "--now", TIMER_NAME):
            self.enabled = True
            self.active = True
            return CmdResult(0, "", "")
        if argv == ("disable", "--now", TIMER_NAME):
            self.enabled = False
            self.active = False
            return CmdResult(0, "", "")
        if argv == ("restart", TIMER_NAME):
            self.active = True
            return CmdResult(0, "", "")
        if argv == ("reset-failed", SERVICE_NAME):
            self.failed = False
            return CmdResult(0, "", "")
        return self._query(argv)

    def _query(self, argv: tuple[str, ...]) -> CmdResult:
        if argv == ("is-enabled", TIMER_NAME):
            if self.enabled:
                return CmdResult(0, "enabled", "")
            return CmdResult(1, "disabled", "")
        if argv == ("is-active", TIMER_NAME):
            if self.active:
                return CmdResult(0, "active", "")
            return CmdResult(3, "inactive", "")
        if argv == ("is-failed", SERVICE_NAME):
            if self.failed:
                return CmdResult(self.fail_rc, "failed", "")
            return CmdResult(1, "inactive", "")
        return CmdResult(1, "", f"unexpected: {argv}")


class _FakeLaunchctl:
    """Stateful fake runner for `launchctl` unit tests."""

    def __init__(self) -> None:
        self.loaded = False
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> CmdResult:
        argv = tuple(args)
        self.calls.append(argv)
        if argv[:2] == ("load", "-w"):
            self.loaded = True
            return CmdResult(0, "", "")
        if argv[:2] == ("unload", "-w"):
            self.loaded = False
            return CmdResult(0, "", "")
        if argv == ("list", LAUNCHD_LABEL):
            return CmdResult(0, "loaded", "") if self.loaded else CmdResult(113, "", "")
        return CmdResult(1, "", f"unexpected: {argv}")


class AutoSyncTests(unittest.TestCase):
    """Tests for `auto_sync.py` unit rendering, metadata parsing, and idempotency."""

    def test_rendered_units_hardened_and_omit_git_ssh_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "home"
            (fake_home / ".ssh").mkdir(parents=True)
            (fake_home / ".ssh" / "github").write_text("dummy-key\n", encoding="utf-8")
            db_path = Path(tmp) / "data" / "repos.sqlite"

            with mock.patch("auto_sync.Path.home", return_value=fake_home):
                portable_path = _build_portable_path()
                unit_fetch = render_service_unit(db_path, fetch=True)
                unit_nofetch = render_service_unit(db_path, fetch=False)
                plist_fetch = plistlib.loads(
                    render_launchd_plist(db_path, interval_min=15, fetch=True)
                )
                plist_nofetch = plistlib.loads(
                    render_launchd_plist(db_path, interval_min=15, fetch=False)
                )

            expected_mise = str(fake_home / ".local" / "share" / "mise" / "shims")
            self.assertIn(expected_mise, portable_path.split(":"))

            self.assertIn("sync --auto --fetch --json", unit_fetch)
            self.assertIn("sync --auto --json", unit_nofetch)
            self.assertNotIn("--fetch", unit_nofetch)
            self.assertIn("TimeoutStartSec=300", unit_fetch)
            self.assertIn("TimeoutStopSec=10", unit_fetch)
            self.assertIn("KillMode=control-group", unit_fetch)
            self.assertIn(
                'Environment="GH_PROMPT_DISABLED=1" "GIT_TERMINAL_PROMPT=0" '
                '"GCM_INTERACTIVE=never"',
                unit_fetch,
            )
            self.assertNotIn("GIT_SSH_COMMAND", unit_fetch)

            self.assertEqual(
                plist_fetch["ProgramArguments"][-4:],
                ["sync", "--auto", "--fetch", "--json"],
            )
            self.assertEqual(
                plist_nofetch["ProgramArguments"][-3:],
                ["sync", "--auto", "--json"],
            )
            self.assertNotIn("GIT_SSH_COMMAND", plist_fetch["EnvironmentVariables"])
            self.assertIn(expected_mise, plist_fetch["EnvironmentVariables"]["PATH"])

    def test_metadata_parsers_return_none_tuple_when_files_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            svc = tmp_path / SERVICE_NAME
            tmr = tmp_path / TIMER_NAME
            plist = tmp_path / "io.packrat.sync.plist"

            self.assertEqual(_parse_systemd_metadata(svc, tmr), (None, None))
            self.assertEqual(_parse_launchd_metadata(plist), (None, None))

            db_file = tmp_path / "repos.sqlite"
            svc.write_text(render_service_unit(db_file, fetch=False))
            tmr.write_text(render_timer_unit(interval_min=25))
            plist_bytes = render_launchd_plist(db_file, interval_min=30, fetch=False)
            plist.write_bytes(plist_bytes)

            self.assertEqual(_parse_systemd_metadata(svc, tmr), (25, False))
            self.assertEqual(_parse_launchd_metadata(plist), (30, False))

    def test_systemd_idempotent_enable_preserves_and_updates_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            systemd_dir = tmp_path / "systemd"
            ctl = _FakeSystemctl()

            st_first = enable_auto_sync(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_first.action, "enabled")
            self.assertEqual(st_first.interval_min, 10)
            self.assertTrue(st_first.fetch)
            self.assertIn(("daemon-reload",), ctl.calls)
            self.assertIn(("enable", "--now", TIMER_NAME), ctl.calls)
            self.assertNotIn(("restart", TIMER_NAME), ctl.calls)
            self.assertNotIn(("reset-failed", SERVICE_NAME), ctl.calls)

            ctl.calls.clear()
            st_custom = enable_auto_sync(
                db_path=db_path,
                interval_min=15,
                fetch=False,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_custom.action, "updated")
            self.assertEqual(st_custom.interval_min, 15)
            self.assertFalse(st_custom.fetch)
            self.assertIn(("daemon-reload",), ctl.calls)
            self.assertIn(("restart", TIMER_NAME), ctl.calls)

            ctl.calls.clear()
            with mock.patch("auto_sync.os.replace", wraps=os.replace) as mock_replace:
                st_noop = enable_auto_sync(
                    db_path=db_path,
                    systemd_dir=systemd_dir,
                    backend="systemd-user",
                    runner=ctl,
                )
            self.assertEqual(st_noop.action, "already_enabled")
            self.assertEqual(st_noop.interval_min, 15)
            self.assertFalse(st_noop.fetch)
            self.assertEqual(mock_replace.call_count, 0)
            self.assertNotIn(("daemon-reload",), ctl.calls)
            self.assertNotIn(("enable", "--now", TIMER_NAME), ctl.calls)
            self.assertNotIn(("restart", TIMER_NAME), ctl.calls)

            ctl.calls.clear()
            st_fetch_update = enable_auto_sync(
                db_path=db_path,
                fetch=True,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_fetch_update.action, "updated")
            self.assertEqual(st_fetch_update.interval_min, 15)
            self.assertTrue(st_fetch_update.fetch)
            self.assertIn(("restart", TIMER_NAME), ctl.calls)

    def test_systemd_reset_failed_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            systemd_dir = tmp_path / "systemd"
            ctl = _FakeSystemctl()

            enable_auto_sync(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )

            ctl.failed = True
            ctl.fail_rc = 0
            st_failed = get_auto_sync_status(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_failed.service_state, "failed")
            self.assertIn("service=failed", st_failed.detail)

            ctl.calls.clear()
            st_recovered = enable_auto_sync(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_recovered.action, "updated")
            self.assertEqual(st_recovered.service_state, "inactive")
            self.assertIn(("reset-failed", SERVICE_NAME), ctl.calls)
            self.assertIn(("restart", TIMER_NAME), ctl.calls)

            ctl.failed = True
            ctl.fail_rc = 1
            ctl.calls.clear()
            st_recovered_stdout = enable_auto_sync(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_recovered_stdout.action, "updated")
            self.assertIn(("reset-failed", SERVICE_NAME), ctl.calls)

    def test_invalid_interval_raises_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for invalid in (0, 1441):
                with self.assertRaises(ValueError):
                    enable_auto_sync(
                        db_path=tmp_path / "repos.sqlite",
                        interval_min=invalid,
                        systemd_dir=tmp_path / "systemd",
                        backend="systemd-user",
                        runner=_FakeSystemctl(),
                    )

    def test_launchd_idempotent_enable_preserves_and_updates_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "repos.sqlite"
            launchd_dir = tmp_path / "launch_agents"
            plist_str = str(launchd_dir / "io.packrat.sync.plist")
            ctl = _FakeLaunchctl()

            st_first = enable_auto_sync(
                db_path=db_path,
                launchd_dir=launchd_dir,
                backend="launchd",
                runner=ctl,
            )
            self.assertEqual(st_first.action, "enabled")
            self.assertEqual(st_first.interval_min, 10)
            self.assertTrue(st_first.fetch)
            self.assertIn(("load", "-w", plist_str), ctl.calls)
            self.assertNotIn(("unload", "-w", plist_str), ctl.calls)

            ctl.calls.clear()
            st_custom = enable_auto_sync(
                db_path=db_path,
                interval_min=15,
                fetch=False,
                launchd_dir=launchd_dir,
                backend="launchd",
                runner=ctl,
            )
            self.assertEqual(st_custom.action, "updated")
            self.assertEqual(st_custom.interval_min, 15)
            self.assertFalse(st_custom.fetch)
            self.assertIn(("unload", "-w", plist_str), ctl.calls)
            self.assertIn(("load", "-w", plist_str), ctl.calls)

            ctl.calls.clear()
            with mock.patch("auto_sync.os.replace", wraps=os.replace) as mock_replace:
                st_noop = enable_auto_sync(
                    db_path=db_path,
                    launchd_dir=launchd_dir,
                    backend="launchd",
                    runner=ctl,
                )
            self.assertEqual(st_noop.action, "already_enabled")
            self.assertEqual(st_noop.interval_min, 15)
            self.assertFalse(st_noop.fetch)
            self.assertEqual(mock_replace.call_count, 0)
            self.assertEqual(ctl.calls, [("list", LAUNCHD_LABEL)])

    def test_status_and_disable_populate_last_run_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "state" / "repos.sqlite"
            systemd_dir = tmp_path / "systemd"
            ctl = _FakeSystemctl()

            record = AutoSyncRunRecord(
                ran_at="2026-11-03T03:00:00Z",
                duration_sec=1.25,
                outcome="degraded_keyring_locked",
                repos_total=3,
                git_commits_upserted=7,
                github_items_upserted=0,
                detail="gnome-keyring locked",
            )
            write_auto_sync_run_state(db_path, record)

            enable_auto_sync(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            st_status = get_auto_sync_status(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_status.action, "status")
            self.assertEqual(st_status.last_run, record)
            serialized = json.loads(json.dumps(asdict(st_status)))
            outcome = serialized["last_run"]["outcome"]
            self.assertEqual(outcome, "degraded_keyring_locked")

            st_disabled = disable_auto_sync(
                db_path=db_path,
                systemd_dir=systemd_dir,
                backend="systemd-user",
                runner=ctl,
            )
            self.assertEqual(st_disabled.action, "disabled")
            self.assertFalse(st_disabled.enabled)
            self.assertEqual(st_disabled.last_run, record)


if __name__ == "__main__":
    unittest.main()
