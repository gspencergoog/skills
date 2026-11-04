"""Unit tests for a2ui_board.py."""

from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import a2ui_board
from a2ui_board import (
    CALL_ID_PREFIX,
    STACK_BOARD_COMPONENTS,
    board_data_model,
    board_dsl,
    default_store_dir,
    item_for,
    lifecycle_step,
    sync_pane_store,
    write_board_file,
)
from models import Event
from testing import make_check, make_snapshot, make_thread


class TestA2uiBoard(unittest.TestCase):
    def test_lifecycle_steps(self) -> None:
        self.assertEqual(lifecycle_step(make_snapshot(draft=True)), 0)
        self.assertEqual(lifecycle_step(make_snapshot(state="CLOSED")), 0)
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    checks=[make_check(status="IN_PROGRESS", conclusion=None)],
                    rollup_state="PENDING",
                )
            ),
            1,
        )
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    review_decision="REVIEW_REQUIRED",
                    checks=[make_check(status="COMPLETED", conclusion="SUCCESS")],
                )
            ),
            2,
        )
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    review_decision="APPROVED",
                    checks=[make_check(status="IN_PROGRESS", conclusion=None)],
                    rollup_state="PENDING",
                )
            ),
            2,
        )
        self.assertEqual(
            lifecycle_step(
                make_snapshot(
                    review_decision="APPROVED",
                    checks=[make_check(status="COMPLETED", conclusion="SUCCESS")],
                )
            ),
            3,
        )
        self.assertEqual(lifecycle_step(make_snapshot(state="MERGED")), 4)

    def test_badges_and_watch_only(self) -> None:
        conflict_snap = make_snapshot(
            mergeable="CONFLICTING",
            review_decision="CHANGES_REQUESTED",
            checks=[make_check(status="COMPLETED", conclusion="FAILURE")],
        )
        item = item_for(conflict_snap)
        self.assertEqual((item["state"], item["tone"]), ("Conflicts", "danger"))
        self.assertEqual((item["ci"], item["ciTone"]), ("CI failed", "danger"))
        self.assertEqual((item["review"], item["reviewTone"]), ("Changes requested", "danger"))
        self.assertEqual(item["detail"], "- CI failed (test-suite)")

        running_snap = make_snapshot(
            mine=False,
            checks=[make_check(status="IN_PROGRESS", conclusion=None)],
            threads=[make_thread()],
        )
        watch_item = item_for(running_snap, eta_s=180)
        self.assertEqual((watch_item["state"], watch_item["tone"]), ("Watching", "neutral"))
        self.assertEqual((watch_item["ci"], watch_item["ciTone"]), ("CI running, ETA 3m", "active"))
        self.assertEqual(
            (watch_item["review"], watch_item["reviewTone"]),
            ("1 unresolved", "warning"),
        )
        self.assertEqual(watch_item["detail"], "- Watch only")

        # Superseded failed check run that leaves rollup_state="FAILURE" while all
        # latest deduplicated checks succeeded still renders "CI passed".
        rerun_passed = make_snapshot(
            checks=[make_check(status="COMPLETED", conclusion="SUCCESS")],
            rollup_state="FAILURE",
        )
        rerun_item = item_for(rerun_passed)
        self.assertEqual((rerun_item["ci"], rerun_item["ciTone"]), ("CI passed", "success"))

    def test_board_dsl_escaping_and_stack_wave(self) -> None:
        parent = make_snapshot(number=3338, key="acme/widget#3338", head_ref="feat/base")
        child = make_snapshot(
            number=3339,
            key="acme/widget#3339",
            title='fix: handle "quotes", \\slashes\\, and [brackets]',
            base_ref="feat/base",
            stack_parent="acme/widget#3338",
        )
        ev = Event(
            pr=child.key,
            kind="ci_check",
            from_value="IN_PROGRESS",
            to_value="SUCCESS",
            actionable=False,
            suggested_action=None,
            summary="all required CI checks passed",
        )
        dsl = board_dsl(
            {parent.key: parent, child.key: child},
            [ev],
            eta={child.key: 45},
            observed_at="2026-10-27T21:15:00Z",
        )
        parsed: dict[str, object] = {}
        for line in dsl.splitlines():
            key, val = line.split(" = ", 1)
            parsed[key] = json.loads(val)
        self.assertEqual(parsed["$/title"], "### PR Monitor")
        self.assertEqual(parsed["$/status"], "Watching · 21:15 UTC")
        self.assertEqual(parsed["$/statusTone"], "active")
        self.assertEqual(parsed["$/statusPulse"], True)
        self.assertEqual(
            parsed["$/subtitle"],
            "2 PRs in `acme/widget` (`#3338` -> `#3339`) · 1 PR updated",
        )
        items = parsed["$/items"]
        assert isinstance(items, list)
        self.assertEqual(items[0]["wave"], "1")
        self.assertEqual(items[1]["wave"], "2")
        self.assertEqual(items[1]["base"], "PR 3338")
        self.assertEqual(items[1]["title"], 'fix: handle "quotes", \\slashes\\, and [brackets]')
        self.assertEqual(items[1]["detail"], "- all required CI checks passed")

    def test_write_board_file_and_sync_pane_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            board_path = Path(tmp) / "board.a2ui"
            store_dir = Path(tmp) / "plugin_data"
            snap1 = make_snapshot(number=3339, key="acme/widget#3339")
            dm1 = board_data_model({snap1.key: snap1}, [], observed_at="2026-10-27T21:15:00Z")
            dsl1 = board_dsl({snap1.key: snap1}, [], observed_at="2026-10-27T21:15:00Z")

            written = write_board_file(board_path, dsl1)
            self.assertEqual(written, board_path)
            self.assertIn('$/title = "### PR Monitor"', board_path.read_text(encoding="utf-8"))
            self.assertEqual(dm1["subtitle"], "Watching `acme/widget#3339`")
            self.assertEqual(dm1["status"], "Watching · 21:15 UTC")

            # The board is stored as conversations/<convId>/monitor.jsonl.
            status = sync_pane_store(
                "conv-123", dm1, store_dir=store_dir, observed_at="2026-10-27T21:15:00Z"
            )
            self.assertEqual(status, "written")
            line = _surface_lines(store_dir, "conv-123", "monitor")
            self.assertEqual(len(line), 1)
            self.assertEqual(line[0]["seq"], 1)
            self.assertEqual(line[0]["callId"], "call_monitor_1")
            self.assertEqual(line[0]["message"]["version"], "1.0")
            self.assertEqual(line[0]["message"]["createSurface"]["dataModel"], dm1)

            # Invalid conversation_id is reported as unavailable.
            self.assertEqual(sync_pane_store("../bad", dm1, store_dir=store_dir), "unavailable")
            self.assertEqual(
                sync_pane_store("conv-123", dm1, store_dir=store_dir, surface_id="_seq"),
                "unavailable",
            )

    def test_multiple_events_and_schema_upgrade(self) -> None:
        snap = make_snapshot(
            number=3679,
            key="acme/widget#3679",
            checks=[
                make_check(name="build", status="COMPLETED", conclusion="FAILURE"),
                make_check(name="lint", status="COMPLETED", conclusion="FAILURE"),
            ],
        )
        ev1 = Event(
            pr=snap.key,
            kind="ci_check",
            from_value=None,
            to_value="FAILURE",
            actionable=True,
            suggested_action="diagnose_ci",
            summary="CI failed (build)",
        )
        ev2 = Event(
            pr=snap.key,
            kind="ci_check",
            from_value=None,
            to_value="FAILURE",
            actionable=True,
            suggested_action="diagnose_ci",
            summary="CI failed (lint)",
        )
        dm = board_data_model({snap.key: snap}, [ev1, ev2], observed_at="2026-10-27T21:15:00Z")
        self.assertEqual(dm["items"][0]["detail"], "- CI failed (build)\n- CI failed (lint)")


def _surface_lines(store_dir: Path, conv_id: str, surface_id: str) -> list[dict[str, Any]]:
    path = store_dir / "conversations" / conv_id / f"{surface_id}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _read_seq(store_dir: Path, conv_id: str) -> int:
    return int((store_dir / "conversations" / conv_id / "_seq").read_text(encoding="utf-8"))


def _board_model(
    items: list[dict[str, Any]], status: str = "Watching · 21:15 UTC"
) -> dict[str, Any]:
    return {"status": status, "items": items, "ackLabel": "Acknowledge"}


def _force_syncs(store_dir: str, count: int) -> None:
    for idx in range(count):
        status = sync_pane_store(
            "conv-1", _board_model([{"key": idx}]), store_dir=store_dir, force=True
        )
        if status != "written":
            raise SystemExit(f"sync {idx} returned {status}")


def _contend_for_lock(conv_dir: str, rounds: int, abandon_every: int) -> None:
    """Takes the lock ``rounds`` times and fails if another holder is inside at once.

    Every ``abandon_every``-th round leaves the lock behind as a crashed holder would (a
    foreign token and an old mtime), so the other processes must steal it.
    """
    path = Path(conv_dir)
    marker = path / "holder"
    for idx in range(rounds):
        with a2ui_board._conversation_lock(path):
            try:
                os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            except FileExistsError:
                raise SystemExit(f"two holders at round {idx}") from None
            time.sleep(0.001)
            marker.unlink()
            if idx % abandon_every == 0:
                lock = path / ".lock"
                lock.write_text("0 2026-10-27T21:00:00Z crashed\n", encoding="utf-8")
                old = time.time() - 60
                os.utime(lock, (old, old))


class TestPaneStore(unittest.TestCase):
    """Covers the per-surface store contract in a2ui-pane docs/store-format.md."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store_dir = Path(self._tmp.name) / "plugin_data"
        self.conv_dir = self.store_dir / "conversations" / "conv-1"

    def _sync(self, data_model: dict[str, Any], **kwargs: Any) -> str:
        return sync_pane_store("conv-1", data_model, store_dir=self.store_dir, **kwargs)

    def _write_foreign(self, surface_id: str, seq: int) -> Path:
        self.conv_dir.mkdir(parents=True, exist_ok=True)
        path = self.conv_dir / f"{surface_id}.jsonl"
        record = {
            "seq": seq,
            "callId": f"call_{seq}",
            "ts": "2026-10-27T21:00:00Z",
            "message": {"version": "1.0", "createSurface": {"surfaceId": surface_id}},
        }
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        (self.conv_dir / "_seq").write_text(f"{seq}\n", encoding="utf-8")
        return path

    def test_file_stays_one_line_and_seq_increases(self) -> None:
        for idx in range(50):
            status = self._sync(_board_model([{"key": f"item-{idx}"}]))
            self.assertEqual(status, "written")
            lines = _surface_lines(self.store_dir, "conv-1", "monitor")
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["seq"], idx + 1)
            self.assertEqual(_read_seq(self.store_dir, "conv-1"), idx + 1)
        self.assertEqual(lines[0]["callId"], f"{CALL_ID_PREFIX}_50")
        create = lines[0]["message"]["createSurface"]
        self.assertEqual(create["surfaceId"], "monitor")
        self.assertTrue(create["sendDataModel"])
        self.assertEqual(create["components"], STACK_BOARD_COMPONENTS)
        self.assertEqual(create["dataModel"]["items"], [{"key": "item-49"}])
        names = sorted(p.name for p in self.conv_dir.iterdir())
        self.assertEqual(names, ["_seq", "monitor.jsonl"])
        self.assertEqual((self.conv_dir / "monitor.jsonl").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.conv_dir.stat().st_mode & 0o777, 0o700)

    def test_concurrent_writers_get_unique_seqs(self) -> None:
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_force_syncs, args=(str(self.store_dir), 25)) for _ in range(2)]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(30)
        self.assertEqual([proc.exitcode for proc in procs], [0, 0])
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 50)
        lines = _surface_lines(self.store_dir, "conv-1", "monitor")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["seq"], 50)

    def test_concurrent_stealers_keep_mutual_exclusion(self) -> None:
        self.conv_dir.mkdir(parents=True)
        ctx = multiprocessing.get_context("fork")
        procs = [
            ctx.Process(target=_contend_for_lock, args=(str(self.conv_dir), 40, 2))
            for _ in range(8)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(60)
        self.assertEqual([proc.exitcode for proc in procs], [0] * len(procs))
        leftovers = {p.name for p in self.conv_dir.iterdir()} - {".lock"}
        self.assertEqual(leftovers, set())

    def test_unchanged_items_skip_the_write(self) -> None:
        items = [{"key": "a", "state": "OPEN"}]
        self.assertEqual(self._sync(_board_model(items)), "written")
        before = (self.conv_dir / "monitor.jsonl").read_bytes()
        later = _board_model(items, status="Watching · 21:16 UTC")
        self.assertEqual(self._sync(later), "unchanged")
        self.assertEqual((self.conv_dir / "monitor.jsonl").read_bytes(), before)
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 1)
        self.assertEqual(self._sync(later, force=True), "written")
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 2)

    def test_outdated_components_are_rewritten(self) -> None:
        self.conv_dir.mkdir(parents=True)
        items = [{"key": "a"}]
        record = {
            "seq": 7,
            "callId": "call_monitor_7",
            "ts": "2026-10-27T21:00:00Z",
            "message": {
                "version": "v1.0",
                "createSurface": {
                    "surfaceId": "monitor",
                    "components": [{"id": "root", "component": "Card"}],
                    "dataModel": _board_model(items),
                },
            },
        }
        (self.conv_dir / "monitor.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        # A damaged _seq never makes the seq go backward.
        (self.conv_dir / "_seq").write_text("garbage", encoding="utf-8")
        self.assertEqual(self._sync(_board_model(items)), "written")
        lines = _surface_lines(self.store_dir, "conv-1", "monitor")
        self.assertEqual(lines[0]["seq"], 8)
        self.assertEqual(lines[0]["message"]["createSurface"]["components"], STACK_BOARD_COMPONENTS)

    def test_dismissed_board_is_not_recreated_without_allow_create(self) -> None:
        self.assertEqual(self._sync(_board_model([]), allow_create=False), "dismissed")
        self.assertFalse((self.conv_dir / "monitor.jsonl").exists())
        self.assertFalse((self.conv_dir / "_seq").exists())

        self.assertEqual(self._sync(_board_model([])), "written")
        # The pane deletes the file on dismiss and bumps _seq.
        (self.conv_dir / "monitor.jsonl").unlink()
        (self.conv_dir / "_seq").write_text("2\n", encoding="utf-8")
        self.assertEqual(self._sync(_board_model([{"key": "b"}]), allow_create=False), "dismissed")
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 2)
        # An existing board is still updated when allow_create is false.
        self.assertEqual(self._sync(_board_model([{"key": "b"}])), "written")
        self.assertEqual(self._sync(_board_model([{"key": "c"}]), allow_create=False), "written")
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 4)

    def test_creation_unlinks_foreign_surfaces_and_consumes_their_seqs(self) -> None:
        foreign = self._write_foreign("git-stack", 4)
        self.assertEqual(self._sync(_board_model([])), "written")
        self.assertFalse(foreign.exists())
        lines = _surface_lines(self.store_dir, "conv-1", "monitor")
        self.assertEqual(lines[0]["seq"], 6)
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 6)

        # Once the board exists, other surfaces are left alone.
        other = self._write_foreign("decision_batch", 6)
        self.assertEqual(self._sync(_board_model([{"key": "x"}])), "written")
        self.assertTrue(other.exists())
        self.assertEqual(_read_seq(self.store_dir, "conv-1"), 7)

    def test_stale_lock_is_recovered(self) -> None:
        self.conv_dir.mkdir(parents=True)
        lock = self.conv_dir / ".lock"
        lock.write_text("999999 2026-10-27T21:00:00Z other-token\n", encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))
        self.assertEqual(self._sync(_board_model([])), "written")
        self.assertFalse(lock.exists())
        leftovers = [p.name for p in self.conv_dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])

    def test_fresh_lock_times_out_as_unavailable_and_is_kept(self) -> None:
        self.conv_dir.mkdir(parents=True)
        lock = self.conv_dir / ".lock"
        lock.write_text("999999 2026-10-27T21:00:00Z other-token\n", encoding="utf-8")
        with mock.patch.object(a2ui_board, "LOCK_TIMEOUT_S", 0.05):
            self.assertEqual(self._sync(_board_model([])), "unavailable")
        self.assertTrue(lock.exists())
        self.assertFalse((self.conv_dir / "monitor.jsonl").exists())

    def test_steal_leaves_a_fresh_lock_alone(self) -> None:
        self.conv_dir.mkdir(parents=True)
        lock = self.conv_dir / ".lock"
        lock.write_text("1 2026-10-27T21:00:00Z fresh\n", encoding="utf-8")
        # A waiter whose staleness check is out of date calls steal on a fresh lock. It must
        # never move or remove `.lock`, even briefly: another writer could take it meanwhile.
        with (
            mock.patch.object(os, "rename", wraps=os.rename) as rename,
            mock.patch.object(os, "unlink", wraps=os.unlink) as unlink,
        ):
            self.assertFalse(a2ui_board._steal_stale_lock(lock))
        touched = [Path(c.args[0]).name for c in rename.call_args_list + unlink.call_args_list]
        self.assertNotIn(".lock", touched)
        self.assertEqual(lock.read_text(encoding="utf-8"), "1 2026-10-27T21:00:00Z fresh\n")
        self.assertEqual([p.name for p in self.conv_dir.iterdir()], [".lock"])

    def test_steal_waits_for_a_live_guard_and_clears_a_dead_one(self) -> None:
        self.conv_dir.mkdir(parents=True)
        lock = self.conv_dir / ".lock"
        guard = self.conv_dir / ".lock.steal"
        lock.write_text("999999 2026-10-27T21:00:00Z other-token\n", encoding="utf-8")
        old = time.time() - 60
        os.utime(lock, (old, old))

        # Another waiter holds the guard: the stale lock stays until it finishes.
        guard.write_text("", encoding="utf-8")
        self.assertFalse(a2ui_board._steal_stale_lock(lock))
        self.assertTrue(lock.exists())
        self.assertTrue(guard.exists())

        # A guard past the stale limit lost its owner and is removed.
        os.utime(guard, (old, old))
        self.assertFalse(a2ui_board._steal_stale_lock(lock))
        self.assertFalse(guard.exists())
        self.assertTrue(lock.exists())

        # The next attempt takes the guard and removes the stale lock.
        self.assertTrue(a2ui_board._steal_stale_lock(lock))
        self.assertEqual(list(self.conv_dir.iterdir()), [])

    def test_lock_create_removes_its_lock_when_the_write_fails(self) -> None:
        self.conv_dir.mkdir(parents=True)
        lock = self.conv_dir / ".lock"
        with (
            mock.patch.object(os, "write", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            a2ui_board._try_create_lock(lock, "token")
        self.assertFalse(lock.exists())

    def test_release_keeps_a_lock_with_another_token(self) -> None:
        self.conv_dir.mkdir(parents=True)
        lock = self.conv_dir / ".lock"
        lock.write_text("1 2026-10-27T21:00:00Z theirs\n", encoding="utf-8")
        a2ui_board._release_lock(lock, "mine")
        self.assertTrue(lock.exists())
        a2ui_board._release_lock(lock, "theirs")
        self.assertFalse(lock.exists())

    def test_legacy_file_without_seq_is_unavailable_and_untouched(self) -> None:
        conv_root = self.store_dir / "conversations"
        conv_root.mkdir(parents=True)
        legacy = conv_root / "conv-1.jsonl"
        legacy.write_text('{"seq":1,"message":{}}\n', encoding="utf-8")
        with mock.patch.object(a2ui_board, "LEGACY_WAIT_S", 0.05):
            self.assertEqual(self._sync(_board_model([])), "unavailable")
        self.assertEqual(legacy.read_text(encoding="utf-8"), '{"seq":1,"message":{}}\n')
        self.assertFalse(self.conv_dir.exists())

        # After the TS migrator writes _seq, the board is written above the legacy seq.
        self.conv_dir.mkdir()
        (self.conv_dir / "_seq").write_text("12\n", encoding="utf-8")
        self.assertEqual(self._sync(_board_model([])), "written")
        self.assertEqual(_surface_lines(self.store_dir, "conv-1", "monitor")[0]["seq"], 13)

    def test_legacy_wait_succeeds_when_the_migrator_finishes(self) -> None:
        conv_root = self.store_dir / "conversations"
        conv_root.mkdir(parents=True)
        (conv_root / "conv-1.jsonl").write_text('{"seq":1,"message":{}}\n', encoding="utf-8")

        def migrate() -> None:
            self.conv_dir.mkdir()
            (self.conv_dir / "_seq").write_text("5\n", encoding="utf-8")

        timer = threading.Timer(0.1, migrate)
        timer.start()
        self.addCleanup(timer.cancel)
        self.assertEqual(self._sync(_board_model([])), "written")
        self.assertEqual(_surface_lines(self.store_dir, "conv-1", "monitor")[0]["seq"], 6)

    def test_dismissed_tick_without_a_conversation_dir_creates_nothing(self) -> None:
        self.assertEqual(self._sync(_board_model([]), allow_create=False), "dismissed")
        self.assertFalse(self.conv_dir.exists())

    def test_missing_store_dir_is_unavailable(self) -> None:
        with mock.patch.object(a2ui_board, "default_store_dir", return_value=None):
            self.assertEqual(sync_pane_store("conv-1", _board_model([])), "unavailable")


class TestDefaultStoreDir(unittest.TestCase):
    """Covers the host-aware lookup order in ``default_store_dir``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        home_patch = mock.patch.object(Path, "home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def _make(self, *parts: str) -> Path:
        path = self.root.joinpath(*parts)
        path.mkdir(parents=True)
        return path

    def test_prefers_explicit_a2ui_pane_data(self) -> None:
        explicit = self._make("explicit")
        app_data = self._make("agy")
        (app_data / "plugin_data" / "a2ui-pane").mkdir(parents=True)
        env = {"A2UI_PANE_DATA": str(explicit), "ANTIGRAVITY_APP_DATA_DIR": str(app_data)}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_store_dir(), explicit)

    def test_uses_antigravity_app_data_dir(self) -> None:
        app_data = self._make("antigravity")
        store = app_data / "plugin_data" / "a2ui-pane"
        store.mkdir(parents=True)
        (self.home / ".gemini" / "jetski" / "plugin_data" / "a2ui-pane").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data)}, clear=True):
            self.assertEqual(default_store_dir(), store)

    def test_skips_missing_candidates_and_falls_back_to_jetski_home(self) -> None:
        fallback = self.home / ".gemini" / "jetski" / "plugin_data" / "a2ui-pane"
        fallback.mkdir(parents=True)
        env = {
            "A2UI_PANE_DATA": str(self.root / "missing"),
            "ANTIGRAVITY_APP_DATA_DIR": str(self.root / "no-plugin-data"),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(default_store_dir(), fallback)

    def test_returns_none_when_nothing_exists(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(default_store_dir())


if __name__ == "__main__":
    unittest.main()
