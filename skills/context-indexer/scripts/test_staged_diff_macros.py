#!/usr/bin/env python3
"""Unit tests for agents/skills/context-indexer/scripts/staged_diff_macros.sql."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

SCRIPTS_DIR = Path(__file__).resolve().parent
STAGED_MACROS_SQL = SCRIPTS_DIR / "staged_diff_macros.sql"
RAW_MACROS_SQL = SCRIPTS_DIR.parent.parent / "duckdb" / "scripts" / "diff_macros.sql"
SPLIT_DIFF_PY = SCRIPTS_DIR / "stage_diffs.py"


def _find_duckdb() -> str | None:
    """Finds the duckdb CLI executable on PATH or in ~/.local/bin."""
    found = shutil.which("duckdb")
    if found:
        return found
    candidate = Path.home() / ".local/bin/duckdb"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


def _run_git(repo_dir: Path, *args: str) -> str:
    """Runs a git command in repo_dir and returns stdout."""
    res = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout


DUCKDB_BIN = _find_duckdb()


@unittest.skipUnless(DUCKDB_BIN, "duckdb CLI binary not found")
class TestStagedDiffMacros(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir)

    def _query(self, sql: str) -> list[dict[str, Any]]:
        """Executes a SQL query with staged_diff_macros.sql initialized."""
        res = subprocess.run(
            [
                str(DUCKDB_BIN),
                "-batch",
                "-dark-mode",
                "-json",
                "-init",
                str(STAGED_MACROS_SQL),
                "-c",
                sql,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = res.stdout.strip()
        return json.loads(out) if out else []

    def test_raw_and_staged_diff_parity(self) -> None:
        diff_content = (
            "diff --git a/src/service.py b/src/service.py\n"
            "index 1111111..2222222 100644\n"
            "--- a/src/service.py\n"
            "+++ b/src/service.py\n"
            "@@ -10,3 +10,4 @@ def process():\n"
            " keep_before\n"
            "-old_call()\n"
            "+new_call()\n"
            "+extra_call()\n"
            " keep_after\n"
            "diff --git a/tests/test_service.py b/tests/test_service.py\n"
            "--- a/tests/test_service.py\n"
            "+++ b/tests/test_service.py\n"
            "@@ -1 +1,2 @@\n"
            " assert True\n"
            "+new_call()\n"
        )
        raw_diff_path = Path(self.test_dir) / "sample.diff"
        raw_diff_path.write_text(diff_content, encoding="utf-8")

        stage_dir = Path(self.test_dir) / "staged"
        subprocess.run(
            [
                sys.executable,
                str(SPLIT_DIFF_PY),
                "--grouped",
                "--output-dir",
                str(stage_dir),
            ],
            input=diff_content,
            text=True,
            check=True,
            capture_output=True,
        )

        raw_res = subprocess.run(
            [
                str(DUCKDB_BIN),
                "-batch",
                "-dark-mode",
                "-json",
                "-init",
                str(RAW_MACROS_SQL),
                "-c",
                (
                    f"SELECT file_path, hunk_id, func_context, op, old_line, new_line, content "
                    f"FROM read_diff_lines('{raw_diff_path}');"
                ),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        raw_lines = json.loads(raw_res.stdout.strip())
        staged_lines = self._query(
            f"SELECT file_path, hunk_id, func_context, op, old_line, new_line, content "
            f"FROM staged_diff_lines('{stage_dir}');"
        )
        self.assertEqual(raw_lines, staged_lines)

    def test_entity_churn_signature_drift_and_untested_functions(self) -> None:
        repo_dir = Path(self.test_dir) / "repo"
        (repo_dir / "src").mkdir(parents=True)
        (repo_dir / "tests").mkdir(parents=True)
        _run_git(repo_dir, "init")
        _run_git(repo_dir, "config", "user.email", "test@example.com")
        _run_git(repo_dir, "config", "user.name", "Test")

        prod_file = repo_dir / "src" / "worker.py"
        test_file = repo_dir / "tests" / "test_worker.py"

        prod_file.write_text(
            "class Worker:\n"
            "    def tested_method(self, count: int) -> bool:\n"
            "        return count > 0\n\n"
            "    def untested_method(self, label: str) -> str:\n"
            "        return label.strip()\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "def test_worker():\n    assert True\n",
            encoding="utf-8",
        )
        _run_git(repo_dir, "add", ".")
        _run_git(repo_dir, "commit", "-m", "initial")

        prod_file.write_text(
            "class Worker:\n"
            "    def tested_method(self, count: int, strict: bool = False) -> bool:\n"
            "        x = count + 1\n"
            "        return x > 0\n\n"
            "    def untested_method(self, label: str) -> str:\n"
            "        cleaned = label.strip()\n"
            "        return cleaned.upper()\n",
            encoding="utf-8",
        )
        test_file.write_text(
            "def test_worker():\n    assert tested_method(1, strict=True)\n",
            encoding="utf-8",
        )

        diff_text = _run_git(repo_dir, "diff", "HEAD")
        stage_dir = Path(self.test_dir) / "staged_entities"
        subprocess.run(
            [
                sys.executable,
                str(SPLIT_DIFF_PY),
                "--grouped",
                "--with-entities",
                "--repo-dir",
                str(repo_dir),
                "--base-ref",
                "HEAD",
                "--output-dir",
                str(stage_dir),
            ],
            input=diff_text,
            text=True,
            check=True,
            capture_output=True,
        )

        churn = self._query(f"SELECT * FROM diff_entity_churn('{stage_dir}');")
        churn_by_name = {r["name"]: r for r in churn}
        self.assertIn("tested_method", churn_by_name)
        self.assertIn("untested_method", churn_by_name)
        self.assertNotIn("Worker", churn_by_name)
        self.assertEqual(churn_by_name["tested_method"]["lines_added"], 3)
        self.assertEqual(churn_by_name["untested_method"]["lines_added"], 2)

        drift = self._query(f"SELECT * FROM diff_signature_drift('{stage_dir}');")
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["name"], "tested_method")
        self.assertIn("strict: bool=False", drift[0]["new_signature"])

        untested = self._query(f"SELECT * FROM diff_untested_functions('{stage_dir}');")
        self.assertEqual([r["name"] for r in untested], ["untested_method"])

    def test_empty_entities_jsonl_returns_zero_rows_cleanly(self) -> None:
        diff_content = (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1 +1 @@\n"
            "-x = 1\n"
            "+x = 2\n"
        )
        stage_dir = Path(self.test_dir) / "staged_no_entities"
        subprocess.run(
            [
                sys.executable,
                str(SPLIT_DIFF_PY),
                "--grouped",
                "--output-dir",
                str(stage_dir),
            ],
            input=diff_content,
            text=True,
            check=True,
            capture_output=True,
        )
        self.assertEqual(
            self._query(f"SELECT * FROM staged_diff_entities('{stage_dir}');"), []
        )
        self.assertEqual(
            self._query(f"SELECT * FROM diff_entity_churn('{stage_dir}');"), []
        )
        self.assertEqual(
            self._query(f"SELECT * FROM diff_signature_drift('{stage_dir}');"), []
        )
        self.assertEqual(
            self._query(f"SELECT * FROM diff_untested_functions('{stage_dir}');"), []
        )


if __name__ == "__main__":
    unittest.main()
