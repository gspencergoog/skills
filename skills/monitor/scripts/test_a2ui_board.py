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
    BASIC_CATALOG,
    CALL_ID_PREFIX,
    REVIEW_BOARD_COMPONENTS,
    REVIEW_BOARD_STEPS,
    STACK_BOARD_COMPONENTS,
    STATUS_CATALOG,
    board_data_model,
    board_dsl,
    default_store_dir,
    drawer_item_for,
    item_for,
    lifecycle_step,
    review_board_data_model,
    review_board_dsl,
    review_item_for,
    sort_review_snapshots,
    sync_pane_store,
    write_board_file,
)
from models import DrawerReason, Event, ReviewSortTier, ReviewStep, TriageEntry, TriageVerdict
from review_overrides import ReviewOverrides, mute_pr, record_triage_entry, unmute_pr
from testing import make_check, make_snapshot, make_thread, make_viewer_review


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


class TestReviewBoard(unittest.TestCase):
    """Covers REVIEW_BOARD_COMPONENTS, 6-tier stack sort, and reviewer data models."""

    def test_review_board_components_schema_and_one_shot_false(self) -> None:
        self.assertEqual(len(REVIEW_BOARD_STEPS), 5)
        self.assertEqual(
            [s["label"] for s in REVIEW_BOARD_STEPS],
            ["Requested", "Waiting on Author", "Re-Review Needed", "Approved", "Merged"],
        )
        by_id = {comp["id"]: comp for comp in REVIEW_BOARD_COMPONENTS}
        self.assertIn("root", by_id)
        allowed_button_keys = {
            "id",
            "component",
            "catalogId",
            "child",
            "variant",
            "action",
            "checks",
            "weight",
            "accessibility",
        }
        allowed_badge_keys = {
            "id",
            "component",
            "catalogId",
            "text",
            "tone",
            "icon",
            "pulse",
            "weight",
            "accessibility",
        }
        allowed_collapsible_keys = {
            "id",
            "component",
            "catalogId",
            "title",
            "child",
            "summary",
            "badge",
            "open",
            "weight",
            "accessibility",
        }
        for comp in REVIEW_BOARD_COMPONENTS:
            self.assertIn(comp["catalogId"], (BASIC_CATALOG, STATUS_CATALOG))
            if "child" in comp:
                self.assertIn(comp["child"], by_id)
            children = comp.get("children")
            if isinstance(children, list):
                for child_id in children:
                    self.assertIn(child_id, by_id)
            elif isinstance(children, dict):
                self.assertIn(children["componentId"], by_id)
            if comp["component"] == "Button":
                self.assertTrue(set(comp).issubset(allowed_button_keys))
                self.assertNotIn("label", comp)
                self.assertEqual(by_id[comp["child"]]["component"], "Text")
            elif comp["component"] == "Badge":
                self.assertTrue(set(comp).issubset(allowed_badge_keys))
            elif comp["component"] == "Collapsible":
                self.assertTrue(set(comp).issubset(allowed_collapsible_keys))

        self.assertEqual(
            by_id["stack_col"]["children"],
            [
                "stack_header",
                "stack_subtitle",
                "stack_progress",
                "stack_divider",
                "stack_rows",
                "stack_drawer",
                "stack_footer",
            ],
        )

        progress = by_id["stack_progress"]
        self.assertEqual(progress["component"], "ProgressBar")
        self.assertEqual(progress["segments"], {"@path": "/segments"})
        self.assertEqual(progress["summary"], {"@path": "/progressSummary"})
        self.assertNotIn("items", progress)

        row_head = by_id["stack_row_head"]
        self.assertEqual(
            row_head["children"],
            [
                "stack_row_id",
                "stack_row_state",
                "stack_row_ci",
                "stack_row_review",
                "triage_row_badge",
                "review_row_btn",
                "mute_row_btn",
            ],
        )

        triage_badge = by_id["triage_row_badge"]
        self.assertEqual(triage_badge["component"], "Badge")
        self.assertEqual(triage_badge["catalogId"], STATUS_CATALOG)
        self.assertEqual(triage_badge["text"], {"@path": "triageLabel"})
        self.assertEqual(triage_badge["tone"], {"@path": "triageTone"})

        btn = by_id["review_row_btn"]
        self.assertEqual(btn["component"], "Button")
        self.assertEqual(btn["variant"], "borderless")
        self.assertEqual(btn["child"], "review_row_btn_label")
        event = btn["action"]["event"]
        self.assertEqual(event["name"], "trigger_pr_review")
        self.assertIs(event["context"]["_oneShot"], False)
        self.assertEqual(event["context"]["pr"], {"@path": "id"})
        self.assertEqual(event["context"]["key"], {"@path": "key"})
        self.assertEqual(event["context"]["repo"], {"@path": "repo"})
        self.assertEqual(event["context"]["number"], {"@path": "number"})
        self.assertEqual(event["context"]["url"], {"@path": "url"})
        self.assertEqual(event["context"]["headSha"], {"@path": "headSha"})
        expected_can_review_checks = [
            {
                "condition": {
                    "@call": "and",
                    "catalogId": BASIC_CATALOG,
                    "args": {"values": [{"@path": "canReview"}, True]},
                },
                "message": "PR is closed or merged",
            }
        ]
        self.assertEqual(btn["checks"], expected_can_review_checks)

        mute_btn = by_id["mute_row_btn"]
        self.assertEqual(mute_btn["component"], "Button")
        self.assertEqual(mute_btn["variant"], "borderless")
        self.assertEqual(mute_btn["child"], "mute_row_btn_label")
        self.assertEqual(by_id["mute_row_btn_label"]["text"], "Mute")
        self.assertEqual(mute_btn["checks"], expected_can_review_checks)
        mute_event = mute_btn["action"]["event"]
        self.assertEqual(mute_event["name"], "mute_review_pr")
        self.assertEqual(
            mute_event["context"],
            {
                "pr": {"@path": "id"},
                "key": {"@path": "key"},
                "repo": {"@path": "repo"},
                "number": {"@path": "number"},
                "headSha": {"@path": "headSha"},
                "updatedAt": {"@path": "updatedAt"},
                "_oneShot": False,
            },
        )
        self.assertIs(mute_event["context"]["_oneShot"], False)

        drawer = by_id["stack_drawer"]
        self.assertEqual(drawer["component"], "Collapsible")
        self.assertEqual(drawer["catalogId"], STATUS_CATALOG)
        self.assertEqual(drawer["title"], {"@path": "/drawerTitle"})
        self.assertEqual(drawer["summary"], {"@path": "/drawerSummary"})
        self.assertEqual(drawer["child"], "drawer_rows")
        self.assertEqual(
            by_id["drawer_rows"]["children"],
            {"path": "/drawerItems", "componentId": "drawer_row"},
        )
        self.assertEqual(
            by_id["drawer_row"]["children"],
            ["drawer_row_id", "drawer_row_badge", "drawer_row_title", "unhide_row_btn"],
        )
        self.assertEqual(by_id["drawer_row_badge"]["text"], {"@path": "drawerBadge"})
        self.assertEqual(by_id["drawer_row_badge"]["tone"], {"@path": "drawerTone"})

        unhide_btn = by_id["unhide_row_btn"]
        self.assertEqual(unhide_btn["component"], "Button")
        self.assertEqual(unhide_btn["variant"], "borderless")
        self.assertEqual(unhide_btn["child"], "unhide_row_btn_label")
        self.assertEqual(by_id["unhide_row_btn_label"]["text"], "Unhide")
        unhide_event = unhide_btn["action"]["event"]
        self.assertEqual(unhide_event["name"], "unmute_review_pr")
        self.assertEqual(
            unhide_event["context"],
            {
                "pr": {"@path": "pr"},
                "key": {"@path": "key"},
                "repo": {"@path": "repo"},
                "number": {"@path": "number"},
                "_oneShot": False,
            },
        )
        self.assertIs(unhide_event["context"]["_oneShot"], False)
        self.assertIs(by_id["ack_btn"]["action"]["event"]["context"]["_oneShot"], True)

    def test_sort_review_snapshots_tiers_clusters_and_waves(self) -> None:
        # Stack A: #101 (root, wave 0, older own_ts) <- #102 (child, wave 1, newer own_ts)
        # Both in Tier 1 (REVIEW_REQUESTED). Cluster max ts is 2026-11-05T12:00:00Z.
        stack_a_root = make_snapshot(
            number=101,
            key="acme/widget#101",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T10:00:00Z",
        )
        stack_a_child = make_snapshot(
            number=102,
            key="acme/widget#102",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            stack_parent="acme/widget#101",
            updated_at="2026-11-05T12:00:00Z",
        )
        # Standalone PR #103 in Tier 1 with own_ts 11:00:00Z (< Stack A cluster max 12:00:00Z,
        # even though #103's own_ts > #101's own_ts 10:00:00Z).
        standalone = make_snapshot(
            number=103,
            key="acme/widget#103",
            mine=False,
            author="carol",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T11:00:00Z",
        )
        # Re-review needed PR #104 in Tier 0 (RE_REVIEW_NEEDED)
        re_review = make_snapshot(
            number=104,
            key="acme/widget#104",
            mine=False,
            author="dave",
            head_sha="bbb2222",
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-11-05T08:00:00Z",
                commit_oid="aaa1111",
            ),
            updated_at="2026-11-05T09:00:00Z",
        )
        # Draft PR #105 in Tier 2 (DRAFT_REQUESTED)
        draft_pr = make_snapshot(
            number=105,
            key="acme/widget#105",
            mine=False,
            author="eve",
            draft=True,
            requested_reviewers=["alice"],
            updated_at="2026-11-05T13:00:00Z",
        )
        # Waiting on author PR #106 in Tier 3 (WAITING_ON_AUTHOR)
        waiting_pr = make_snapshot(
            number=106,
            key="acme/widget#106",
            mine=False,
            author="frank",
            head_sha="ccc3333",
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-11-05T09:30:00Z",
                commit_oid="ccc3333",
            ),
            updated_at="2026-11-05T09:30:00Z",
        )
        # Approved PR #107 in Tier 4 (APPROVED_OPEN)
        approved_pr = make_snapshot(
            number=107,
            key="acme/widget#107",
            mine=False,
            author="grace",
            approvals=["alice"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(
                state="APPROVED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="ddd4444",
            ),
            updated_at="2026-11-05T14:00:00Z",
        )
        # Terminal Closed PR #108 in Tier 5 (TERMINAL)
        closed_pr = make_snapshot(
            number=108,
            key="acme/widget#108",
            mine=False,
            author="heidi",
            state="CLOSED",
            updated_at="2026-11-05T15:00:00Z",
        )

        rows = sort_review_snapshots(
            [
                closed_pr,
                approved_pr,
                waiting_pr,
                draft_pr,
                standalone,
                stack_a_child,
                stack_a_root,
                re_review,
            ],
            viewer="alice",
        )
        ordered_numbers = [snap.number for snap, _, _, _ in rows]
        self.assertEqual(ordered_numbers, [104, 101, 102, 103, 105, 106, 107, 108])
        self.assertEqual(rows[0][2], ReviewSortTier.RE_REVIEW_NEEDED)
        self.assertEqual(rows[-1][2], ReviewSortTier.TERMINAL)
        waves_by_num = {snap.number: wave for snap, _, _, wave in rows}
        self.assertEqual(waves_by_num[101], 0)
        self.assertEqual(waves_by_num[102], 1)
        self.assertEqual(waves_by_num[103], 0)
        direct_item = review_item_for(stack_a_root, viewer="alice", multi_repo=False)
        self.assertEqual(direct_item["id"], "PR 101")

    def test_sort_review_snapshots_cycle_safe(self) -> None:
        cyc1 = make_snapshot(
            number=201,
            key="acme/widget#201",
            stack_parent="acme/widget#202",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T10:00:00Z",
        )
        cyc2 = make_snapshot(
            number=202,
            key="acme/widget#202",
            stack_parent="acme/widget#201",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T10:00:00Z",
        )
        rows = sort_review_snapshots({cyc1.key: cyc1, cyc2.key: cyc2}, viewer="alice")
        self.assertEqual([s.number for s, _, _, _ in rows], [201, 202])

    def test_closed_row_stays_step_zero_neutral_and_excluded_from_merged_segments(self) -> None:
        merged = make_snapshot(
            number=301,
            key="acme/widget#301",
            mine=False,
            author="bob",
            state="MERGED",
            approvals=["alice"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(state="APPROVED", commit_oid="1111111"),
            updated_at="2026-11-05T12:00:00Z",
        )
        closed_unmerged = make_snapshot(
            number=302,
            key="acme/widget#302",
            mine=False,
            author="carol",
            state="CLOSED",
            viewer_last_review=None,
            updated_at="2026-11-05T11:00:00Z",
        )
        open_req = make_snapshot(
            number=303,
            key="acme/widget#303",
            mine=False,
            author="dave",
            state="OPEN",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T10:00:00Z",
        )

        dm = review_board_data_model(
            [merged, closed_unmerged, open_req],
            viewer="alice",
            observed_at="2026-11-05T12:05:00Z",
        )
        self.assertEqual(dm["title"], "Review Queue (3)")
        self.assertEqual(dm["steps"], REVIEW_BOARD_STEPS)

        by_num = {item["number"]: item for item in dm["items"]}
        closed_item = by_num[302]
        self.assertEqual(closed_item["state"], "Closed")
        self.assertEqual(closed_item["tone"], "neutral")
        self.assertEqual(closed_item["step"], int(ReviewStep.REQUESTED))
        self.assertEqual(closed_item["step"], 0)
        self.assertEqual(closed_item["review"], "Unreviewed")
        self.assertEqual(closed_item["reviewTone"], "neutral")
        self.assertEqual(closed_item["actionLabel"], "Closed")
        self.assertFalse(closed_item["canReview"])

        merged_item = by_num[301]
        self.assertEqual(merged_item["state"], "Merged")
        self.assertEqual(merged_item["tone"], "success")
        self.assertEqual(merged_item["step"], int(ReviewStep.MERGED))
        self.assertEqual(merged_item["review"], "Approved by you")
        self.assertEqual(merged_item["reviewTone"], "success")
        self.assertEqual(merged_item["actionLabel"], "Merged")
        self.assertFalse(merged_item["canReview"])

        open_item = by_num[303]
        self.assertEqual(open_item["actionLabel"], "Review")
        self.assertTrue(open_item["canReview"])

        seg_by_label = {seg["label"]: seg for seg in dm["segments"]}
        self.assertEqual(seg_by_label["Requested"]["count"], 1)
        self.assertEqual(seg_by_label["Requested"]["tone"], "warning")
        self.assertEqual(seg_by_label["Merged"]["count"], 1)
        self.assertEqual(seg_by_label["Merged"]["tone"], "success")
        self.assertEqual(seg_by_label["Closed"]["count"], 1)
        self.assertEqual(seg_by_label["Closed"]["tone"], "neutral")
        self.assertEqual(dm["progressSummary"], "1 requested · 1 merged · 1 closed")

    def test_review_item_for_multi_repo_details_and_dsl(self) -> None:
        re_review_snap = make_snapshot(
            number=410,
            repo="acme/alpha",
            key="acme/alpha#410",
            url="https://github.com/acme/alpha/pull/410",
            mine=False,
            author="bob",
            base_ref="main",
            head_sha="9999999abcdef",
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-11-05T10:00:00Z",
                commit_oid="1111111abcdef",
            ),
            threads=[
                make_thread(
                    thread_id="t1",
                    resolved=False,
                    last_author="bob",
                    last_comment_at="2026-11-05T10:05:00Z",
                )
            ],
            checks=[
                make_check(
                    name="unit",
                    status="IN_PROGRESS",
                    conclusion=None,
                    started_at="2026-11-05T10:08:00Z",
                )
            ],
            rollup_state="PENDING",
        )
        ready_snap = make_snapshot(
            number=411,
            repo="acme/beta",
            key="acme/beta#411",
            url="https://github.com/acme/beta/pull/411",
            mine=False,
            author="carol",
            base_ref="release",
            head_sha="7777777abcdef",
            approvals=["alice"],
            review_decision="APPROVED",
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(
                state="APPROVED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="7777777abcdef",
            ),
            checks=[make_check(name="unit", status="COMPLETED", conclusion="SUCCESS")],
        )
        dm = review_board_data_model(
            [re_review_snap, ready_snap],
            viewer="alice",
            durations={"acme/alpha:unit": [180]},
            now=1793873340.0,
            observed_at="2026-11-05T10:09:00Z",
        )
        items = dm["items"]
        self.assertEqual(items[0]["id"], "alpha#410")
        self.assertEqual(items[0]["meta"], "by @bob · acme/alpha · base main")
        self.assertEqual(items[0]["actionLabel"], "Re-review")
        self.assertTrue(items[0]["canReview"])
        self.assertEqual(items[0]["review"], "Re-review needed")
        self.assertEqual(items[0]["reviewTone"], "warning")
        self.assertIn("New commit `9999999` since your review (`1111111`)", items[0]["detail"])
        self.assertIn("1 unread thread reply", items[0]["detail"])
        self.assertIn("ETA", items[0]["ci"])

        self.assertEqual(items[1]["id"], "beta#411")
        self.assertEqual((items[1]["state"], items[1]["tone"]), ("Ready", "success"))
        self.assertEqual(
            (items[1]["review"], items[1]["reviewTone"]), ("Approved by you", "success")
        )
        self.assertEqual(items[1]["step"], int(ReviewStep.APPROVED))

        dsl = review_board_dsl(dm)
        parsed = {
            line.split(" = ", 1)[0]: json.loads(line.split(" = ", 1)[1])
            for line in dsl.splitlines()
        }
        self.assertEqual(parsed["$/title"], "Review Queue (2)")
        self.assertEqual(parsed["$/segments"], dm["segments"])
        self.assertEqual(parsed["$/progressSummary"], dm["progressSummary"])
        self.assertEqual(parsed["$/items"], dm["items"])

    def test_author_to_reviewer_surface_upgrade_stays_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "plugin_data"
            snap = make_snapshot(
                number=500,
                key="acme/widget#500",
                requested_reviewers=["alice"],
            )
            author_dm = board_data_model({snap.key: snap}, [])
            rev_dm = review_board_data_model({snap.key: snap}, viewer="alice")

            # 1. Write author board (defaults to STACK_BOARD_COMPONENTS).
            self.assertEqual(
                sync_pane_store("conv-up", author_dm, store_dir=store_dir),
                "written",
            )
            lines = _surface_lines(store_dir, "conv-up", "monitor")
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["seq"], 1)
            self.assertEqual(
                lines[0]["message"]["createSurface"]["components"],
                STACK_BOARD_COMPONENTS,
            )

            # 2. Switch to reviewer board (REVIEW_BOARD_COMPONENTS) — rewrites single line, bumps seq.
            self.assertEqual(
                sync_pane_store(
                    "conv-up",
                    rev_dm,
                    store_dir=store_dir,
                    components=REVIEW_BOARD_COMPONENTS,
                ),
                "written",
            )
            lines = _surface_lines(store_dir, "conv-up", "monitor")
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["seq"], 2)
            self.assertEqual(_read_seq(store_dir, "conv-up"), 2)
            self.assertEqual(
                lines[0]["message"]["createSurface"]["components"],
                REVIEW_BOARD_COMPONENTS,
            )

            # 3. Calling again with identical reviewer items and components is unchanged.
            self.assertEqual(
                sync_pane_store(
                    "conv-up",
                    rev_dm,
                    store_dir=store_dir,
                    components=REVIEW_BOARD_COMPONENTS,
                ),
                "unchanged",
            )
            self.assertEqual(_read_seq(store_dir, "conv-up"), 2)

            # 4. Switching back to author board rewrites the single line and bumps seq to 3.
            self.assertEqual(
                sync_pane_store(
                    "conv-up",
                    author_dm,
                    store_dir=store_dir,
                    components=STACK_BOARD_COMPONENTS,
                ),
                "written",
            )
            lines = _surface_lines(store_dir, "conv-up", "monitor")
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["seq"], 3)
            self.assertEqual(
                lines[0]["message"]["createSurface"]["components"],
                STACK_BOARD_COMPONENTS,
            )

    def test_empty_drawer_suppresses_title_and_dsl_emits_drawer_keys(self) -> None:
        snap = make_snapshot(
            number=600,
            key="acme/widget#600",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T10:00:00Z",
        )
        dm = review_board_data_model(
            [snap],
            viewer="alice",
            observed_at="2026-11-05T12:00:00Z",
        )
        self.assertEqual(dm["drawerTitle"], "")
        self.assertEqual(dm["drawerSummary"], "")
        self.assertEqual(dm["drawerItems"], [])
        self.assertNotIn("Muted / Dormant", {seg["label"] for seg in dm["segments"]})

        dsl_from_snaps = review_board_dsl(
            [snap],
            viewer="alice",
            observed_at="2026-11-05T12:00:00Z",
        )
        parsed = {
            line.split(" = ", 1)[0]: json.loads(line.split(" = ", 1)[1])
            for line in dsl_from_snaps.splitlines()
        }
        self.assertEqual(parsed["$/drawerTitle"], "")
        self.assertEqual(parsed["$/drawerSummary"], "")
        self.assertEqual(parsed["$/drawerItems"], [])

    def test_mute_and_dormancy_partitioning_segments_and_unhide_pin(self) -> None:
        now_ts = 1793880000.0  # 2026-11-05T12:00:00Z
        active_req = make_snapshot(
            number=601,
            key="acme/widget#601",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            updated_at="2026-11-05T11:00:00Z",
        )
        muted_snap = make_snapshot(
            number=602,
            key="acme/widget#602",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            head_sha="aaa1111",
            updated_at="2026-11-05T10:00:00Z",
        )
        dormant_waiting = make_snapshot(
            number=603,
            key="acme/widget#603",
            mine=False,
            author="carol",
            requested_reviewers=[],
            head_sha="bbb2222",
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-09-21T12:00:00Z",
                commit_oid="bbb2222",
            ),
            updated_at="2026-09-21T12:00:00Z",  # 45 days before now_ts
        )
        dormant_approved = make_snapshot(
            number=604,
            key="acme/widget#604",
            mine=False,
            author="dave",
            requested_reviewers=[],
            approvals=["alice"],
            viewer_opinionated_state="APPROVED",
            viewer_last_review=make_viewer_review(
                state="APPROVED",
                submitted_at="2026-10-06T12:00:00Z",
                commit_oid="ccc3333",
            ),
            head_sha="ccc3333",
            updated_at="2026-10-06T12:00:00Z",  # 30 days before now_ts
        )

        overrides = ReviewOverrides()
        mute_pr(
            overrides,
            "acme/widget#602",
            muted_snap,
            "alice",
            now_iso="2026-11-05T11:30:00Z",
        )

        dm = review_board_data_model(
            [active_req, muted_snap, dormant_waiting, dormant_approved],
            viewer="alice",
            overrides=overrides,
            stale_days=30,
            now_epoch=now_ts,
            observed_at="2026-11-05T12:00:00Z",
        )
        self.assertEqual([it["number"] for it in dm["items"]], [601])
        self.assertEqual(dm["title"], "Review Queue (1)")
        self.assertEqual(dm["status"], "1 awaiting review · 12:00 UTC")
        self.assertEqual(dm["drawerTitle"], "Muted & Dormant (3)")
        self.assertEqual(dm["drawerSummary"], "1 muted · 2 dormant")
        self.assertEqual(
            [(d["number"], d["drawerReason"], d["drawerBadge"]) for d in dm["drawerItems"]],
            [
                (602, "muted", "Muted"),
                (604, "dormant", "Dormant · 30d"),
                (603, "dormant", "Dormant · 45d"),
            ],
        )
        direct_drawer = drawer_item_for(muted_snap, DrawerReason.MUTED, now_epoch=now_ts)
        self.assertEqual(direct_drawer["id"], "PR 602")
        self.assertEqual(direct_drawer["pr"], "acme/widget#602")
        self.assertEqual(direct_drawer["key"], "acme/widget#602")
        self.assertEqual(direct_drawer["drawerTone"], "neutral")

        seg_by_label = {seg["label"]: seg for seg in dm["segments"]}
        self.assertEqual(seg_by_label["Requested"]["count"], 1)
        self.assertEqual(seg_by_label["Muted / Dormant"]["count"], 3)
        self.assertEqual(seg_by_label["Muted / Dormant"]["tone"], "neutral")
        self.assertEqual(dm["progressSummary"], "1 requested · 3 muted / dormant")

        # Unhide pins a dormant PR active for a fresh stale_days window.
        unmute_pr(overrides, "acme/widget#603", now_iso="2026-11-05T12:00:00Z")
        dm_pinned = review_board_data_model(
            [active_req, muted_snap, dormant_waiting, dormant_approved],
            viewer="alice",
            overrides=overrides,
            stale_days=30,
            now_epoch=now_ts,
            observed_at="2026-11-05T12:00:00Z",
        )
        self.assertEqual([it["number"] for it in dm_pinned["items"]], [601, 603])
        self.assertEqual([d["number"] for d in dm_pinned["drawerItems"]], [602, 604])
        self.assertEqual(dm_pinned["drawerSummary"], "1 muted · 1 dormant")

        # stale_days=0 disables dormancy partitioning while keeping explicit mutes.
        dm_no_stale = review_board_data_model(
            [active_req, muted_snap, dormant_waiting, dormant_approved],
            viewer="alice",
            overrides=overrides,
            stale_days=0,
            now_epoch=now_ts,
        )
        self.assertEqual([d["number"] for d in dm_no_stale["drawerItems"]], [602])

    def test_muted_pr_auto_wakeup_restores_row_to_active_items(self) -> None:
        now_ts = 1793880000.0
        muted_snap = make_snapshot(
            number=610,
            key="acme/widget#610",
            mine=False,
            author="bob",
            requested_reviewers=[],
            head_sha="aaa1111",
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="aaa1111",
            ),
            updated_at="2026-11-05T10:00:00Z",
        )
        overrides = ReviewOverrides()
        mute_pr(
            overrides,
            "acme/widget#610",
            muted_snap,
            "alice",
            now_iso="2026-11-05T11:00:00Z",
        )

        dm_muted = review_board_data_model(
            [muted_snap],
            viewer="alice",
            overrides=overrides,
            now_epoch=now_ts,
            observed_at="2026-11-05T12:00:00Z",
        )
        self.assertEqual(dm_muted["items"], [])
        self.assertEqual([d["number"] for d in dm_muted["drawerItems"]], [610])
        self.assertEqual(dm_muted["status"], "Watching · 12:00 UTC")

        # Post-mute push changes head_sha -> auto-wakes back into /items.
        pushed_snap = make_snapshot(
            number=610,
            key="acme/widget#610",
            mine=False,
            author="bob",
            requested_reviewers=[],
            head_sha="bbb2222",
            head_committed_date="2026-11-05T11:30:00Z",
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="aaa1111",
            ),
            updated_at="2026-11-05T11:30:00Z",
        )
        dm_woke = review_board_data_model(
            [pushed_snap],
            viewer="alice",
            overrides=overrides,
            now_epoch=now_ts,
            observed_at="2026-11-05T12:00:00Z",
        )
        self.assertEqual([it["number"] for it in dm_woke["items"]], [610])
        self.assertEqual(dm_woke["drawerItems"], [])
        self.assertEqual(dm_woke["drawerTitle"], "")

    def test_triage_badges_watermark_invalidation_and_detail_bullets(self) -> None:
        shelved_snap = make_snapshot(
            number=620,
            key="acme/widget#620",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            head_sha="aaa1111",
            head_committed_date="2026-11-05T08:00:00Z",
            viewer_requested_at="2026-11-05T09:00:00Z",
            viewer_last_comment_at="2026-11-05T10:30:00Z",
            discussion_watermark="IC_10:2026-11-05T10:30:00Z",
            updated_at="2026-11-05T10:30:00Z",
        )
        overrides = ReviewOverrides()
        triage_entry = record_triage_entry(
            overrides,
            "acme/widget#620",
            watermark="IC_10:2026-11-05T10:30:00Z",
            verdict=TriageVerdict.LIKELY_SHELVED,
            summary="Author noted feature is on hold",
            evaluated_at="2026-11-05T11:00:00Z",
        )
        item = review_item_for(shelved_snap, "alice", triage=triage_entry)
        self.assertEqual(item["updatedAt"], "2026-11-05T10:30:00Z")
        self.assertEqual(item["triageLabel"], "Likely shelved")
        self.assertEqual(item["triageTone"], "warning")
        self.assertIn("- Replied in PR comments after review request", item["detail"])
        self.assertIn("- Likely shelved: Author noted feature is on hold", item["detail"])

        # Deferred verdict uses neutral tone.
        deferred_entry = TriageEntry(
            watermark="IC_10:2026-11-05T10:30:00Z",
            verdict=TriageVerdict.DEFERRED,
            summary="Deferred until next milestone",
            evaluated_at="2026-11-05T11:00:00Z",
        )
        deferred_item = review_item_for(shelved_snap, "alice", triage=deferred_entry)
        self.assertEqual(deferred_item["triageLabel"], "Deferred")
        self.assertEqual(deferred_item["triageTone"], "neutral")
        self.assertIn("- Deferred: Deferred until next milestone", deferred_item["detail"])

        # New comment watermark invalidates stale TriageEntry badge and bullet.
        newer_watermark_snap = make_snapshot(
            number=620,
            key="acme/widget#620",
            mine=False,
            author="bob",
            requested_reviewers=["alice"],
            discussion_watermark="IC_11:2026-11-05T11:45:00Z",
            updated_at="2026-11-05T11:45:00Z",
        )
        invalidated_item = review_item_for(newer_watermark_snap, "alice", triage=triage_entry)
        self.assertEqual(invalidated_item["triageLabel"], "")
        self.assertEqual(invalidated_item["triageTone"], "neutral")
        self.assertNotIn("Likely shelved", invalidated_item["detail"])

        # Partial viewer-thread progress & bot/non-viewer thread filtering.
        partial_threads_snap = make_snapshot(
            number=621,
            key="acme/widget#621",
            mine=False,
            author="bob",
            requested_reviewers=[],
            head_sha="bbb2222",
            head_committed_date="2026-11-05T10:00:00Z",
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="aaa1111",
            ),
            threads=[
                make_thread(
                    thread_id="t1",
                    resolved=False,
                    last_author="bob",
                    last_comment_at="2026-11-05T09:30:00Z",
                    participants=("alice", "bob"),
                ),
                make_thread(
                    thread_id="t2",
                    resolved=False,
                    last_author="alice",
                    last_comment_at="2026-11-05T08:55:00Z",
                    participants=("alice",),
                ),
                make_thread(
                    thread_id="t3",
                    resolved=False,
                    last_author="carol",
                    last_comment_at="2026-11-05T10:15:00Z",
                    participants=("carol", "bob"),
                ),
                make_thread(
                    thread_id="t4",
                    resolved=False,
                    last_author="ci-bot[bot]",
                    last_author_is_bot=True,
                    last_comment_at="2026-11-05T10:20:00Z",
                    participants=("alice", "ci-bot[bot]"),
                ),
            ],
            updated_at="2026-11-05T10:20:00Z",
        )
        partial_item = review_item_for(partial_threads_snap, "alice")
        self.assertIn("1/3 of your threads updated; 2 awaiting author", partial_item["detail"])
        # Only t1 is unread for alice (t3 is carol's thread, t4 is a bot comment).
        self.assertIn("1 unread thread reply", partial_item["detail"])

        unaddressed_snap = make_snapshot(
            number=622,
            key="acme/widget#622",
            mine=False,
            author="bob",
            requested_reviewers=[],
            head_sha="bbb2222",
            head_committed_date="2026-11-05T10:00:00Z",
            viewer_last_review=make_viewer_review(
                state="CHANGES_REQUESTED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="aaa1111",
            ),
            threads=[
                make_thread(
                    thread_id="t1",
                    resolved=False,
                    last_author="alice",
                    last_comment_at="2026-11-05T08:55:00Z",
                    participants=("alice",),
                ),
                make_thread(
                    thread_id="t2",
                    resolved=False,
                    last_author="alice",
                    last_comment_at="2026-11-05T08:56:00Z",
                    participants=("alice",),
                ),
            ],
            updated_at="2026-11-05T10:00:00Z",
        )
        unaddressed_item = review_item_for(unaddressed_snap, "alice")
        self.assertIn("2 of your threads awaiting author", unaddressed_item["detail"])

        peer_only_threads_snap = make_snapshot(
            number=623,
            key="acme/widget#623",
            mine=False,
            author="bob",
            requested_reviewers=[],
            head_sha="aaa1111",
            viewer_last_review=make_viewer_review(
                state="COMMENTED",
                submitted_at="2026-11-05T09:00:00Z",
                commit_oid="aaa1111",
            ),
            threads=[
                make_thread(
                    thread_id="t_peer_only",
                    resolved=False,
                    last_author="bob",
                    last_comment_at="2026-11-05T09:30:00Z",
                    participants=("carol", "bob"),
                ),
            ],
            updated_at="2026-11-05T09:30:00Z",
        )
        peer_only_item = review_item_for(peer_only_threads_snap, "alice")
        self.assertNotIn("unread thread", peer_only_item["detail"])

    def test_is_unchanged_and_sync_pane_store_detect_drawer_items_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "plugin_data"
            active_snap = make_snapshot(
                number=701,
                key="acme/widget#701",
                mine=False,
                author="bob",
                requested_reviewers=["alice"],
                updated_at="2026-11-05T10:00:00Z",
            )
            dormant_snap = make_snapshot(
                number=702,
                key="acme/widget#702",
                mine=False,
                author="carol",
                requested_reviewers=[],
                head_sha="aaa1111",
                viewer_last_review=make_viewer_review(
                    state="CHANGES_REQUESTED",
                    submitted_at="2026-09-20T10:00:00Z",
                    commit_oid="aaa1111",
                ),
                updated_at="2026-09-20T10:00:00Z",
            )
            now_day1 = 1793880000.0
            dm1 = review_board_data_model(
                [active_snap],
                viewer="alice",
                now_epoch=now_day1,
                observed_at="2026-11-05T12:00:00Z",
            )
            self.assertEqual(
                sync_pane_store(
                    "conv-drawer",
                    dm1,
                    store_dir=store_dir,
                    components=REVIEW_BOARD_COMPONENTS,
                ),
                "written",
            )
            self.assertEqual(_read_seq(store_dir, "conv-drawer"), 1)

            # Timestamp-only update with identical items and drawerItems stays unchanged.
            dm1_later = review_board_data_model(
                [active_snap],
                viewer="alice",
                now_epoch=now_day1 + 60,
                observed_at="2026-11-05T12:01:00Z",
            )
            self.assertEqual(
                sync_pane_store(
                    "conv-drawer",
                    dm1_later,
                    store_dir=store_dir,
                    components=REVIEW_BOARD_COMPONENTS,
                ),
                "unchanged",
            )

            # Adding a dormant row mutates ONLY drawerItems (items stays [701]) -> must write!
            dm2 = review_board_data_model(
                [active_snap, dormant_snap],
                viewer="alice",
                now_epoch=now_day1,
                observed_at="2026-11-05T12:02:00Z",
            )
            self.assertEqual(dm2["items"], dm1["items"])
            self.assertNotEqual(dm2["drawerItems"], dm1["drawerItems"])
            self.assertEqual(
                sync_pane_store(
                    "conv-drawer",
                    dm2,
                    store_dir=store_dir,
                    components=REVIEW_BOARD_COMPONENTS,
                ),
                "written",
            )
            self.assertEqual(_read_seq(store_dir, "conv-drawer"), 2)


if __name__ == "__main__":
    unittest.main()
