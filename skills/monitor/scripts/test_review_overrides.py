"""Unit tests for review_overrides.py."""

from __future__ import annotations

import io
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from models import TriageVerdict
from review_overrides import (
    OverridesWatcher,
    ReviewOverrides,
    default_overrides_path,
    load_overrides,
    mute_pr,
    normalize_override_key,
    record_triage_entry,
    save_overrides,
    unmute_pr,
)
from testing import make_snapshot


class TestReviewOverrides(unittest.TestCase):
    def test_default_overrides_path_honors_xdg_config_home(self) -> None:
        custom = default_overrides_path({"XDG_CONFIG_HOME": "/tmp/custom-xdg"})
        self.assertEqual(custom, Path("/tmp/custom-xdg/monitor/review_overrides.json"))

        fallback = default_overrides_path({})
        self.assertEqual(fallback, Path.home() / ".config" / "monitor" / "review_overrides.json")

    def test_normalize_override_key_lowercases_repo(self) -> None:
        self.assertEqual(normalize_override_key("Acme/Widget#42"), "acme/widget#42")
        self.assertEqual(
            normalize_override_key("https://github.com/Acme/Widget/pull/42"),
            "acme/widget#42",
        )

    def test_atomic_round_trip_permissions_and_corrupt_json_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "review_overrides.json"
            self.assertEqual(load_overrides(path), ReviewOverrides())

            overrides = ReviewOverrides()
            snap = make_snapshot(
                repo="Acme/Widget",
                number=99,
                head_sha="abc1234",
                updated_at="2026-11-09T10:00:00Z",
                requested_reviewers=["Bob"],
            )
            mute_pr(
                overrides,
                "Acme/Widget#99",
                snap,
                "bob",
                now_iso="2026-11-09T12:00:00Z",
                reason="not pursuing",
            )
            record_triage_entry(
                overrides,
                "Acme/Widget#99",
                watermark="IC_9:2026-11-09T11:00:00Z",
                verdict=TriageVerdict.LIKELY_SHELVED,
                summary="Agreed in comments to shelve.",
                evaluated_at="2026-11-09T12:00:05Z",
            )
            save_overrides(path, overrides)

            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600)

            reloaded = load_overrides(path)
            self.assertEqual(reloaded, overrides)
            mute = reloaded.get_mute("ACME/WIDGET#99")
            assert mute is not None
            self.assertTrue(mute.was_requested)
            self.assertEqual(mute.head_sha, "abc1234")
            triage = reloaded.get_triage("acme/widget#99")
            assert triage is not None
            self.assertEqual(triage.verdict, TriageVerdict.LIKELY_SHELVED)

            # Unmuting clears mute and records active pin timestamp.
            unmute_pr(reloaded, "Acme/Widget#99", now_iso="2026-11-09T13:00:00Z")
            self.assertIsNone(reloaded.get_mute("acme/widget#99"))
            self.assertEqual(reloaded.get_pin("Acme/Widget#99"), "2026-11-09T13:00:00Z")

            # Corrupt JSON logs once to stderr and returns empty overrides.
            path.write_text("{not-json", encoding="utf-8")
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                fallback = load_overrides(path)
            self.assertEqual(fallback, ReviewOverrides())
            self.assertIn("ignoring unreadable review overrides", stderr_buf.getvalue())

    def test_overrides_watcher_reloads_only_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_overrides.json"
            watcher = OverridesWatcher(path)
            self.assertFalse(watcher.changed())
            self.assertEqual(watcher.current().mutes, {})

            overrides = ReviewOverrides()
            mute_pr(overrides, "acme/widget#1", None, "bob", now_iso="2026-11-09T12:00:00Z")
            time.sleep(0.002)
            save_overrides(path, overrides)

            self.assertTrue(watcher.changed())
            loaded = watcher.current()
            self.assertIn("acme/widget#1", loaded.mutes)
            self.assertFalse(watcher.changed())


if __name__ == "__main__":
    unittest.main()
