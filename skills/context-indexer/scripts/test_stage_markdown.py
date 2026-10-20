#!/usr/bin/env python3
"""Unit tests for stage_markdown.py and staged_md_macros.sql."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any

from stage_markdown import main as stage_markdown_main

SCRIPTS_DIR = Path(__file__).resolve().parent
STAGED_MD_MACROS_SQL = SCRIPTS_DIR / "staged_md_macros.sql"


def _find_duckdb() -> str | None:
    """Finds the duckdb CLI executable on PATH or in ~/.local/bin."""
    found = shutil.which("duckdb")
    if found:
        return found
    candidate = Path.home() / ".local/bin/duckdb"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return None


DUCKDB_BIN = _find_duckdb()


@unittest.skipUnless(DUCKDB_BIN, "duckdb CLI binary not found")
class TestStageMarkdownAndMacros(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_dir = Path(tempfile.mkdtemp())
        self.out_dir = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.repo_dir)
        shutil.rmtree(self.out_dir)

    def _query(self, sql: str) -> list[dict[str, Any]]:
        """Executes a SQL query with staged_md_macros.sql loaded."""
        res = subprocess.run(
            [
                str(DUCKDB_BIN),
                "-batch",
                "-dark-mode",
                "-json",
                "-init",
                str(STAGED_MD_MACROS_SQL),
                "-c",
                sql,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        out = res.stdout.strip()
        return json.loads(out) if out else []

    def test_setext_headings_duplicate_slugs_and_blockquote_fences(self) -> None:
        spec_md = (
            "Specification Title\n"
            "===================\n"
            "Intro paragraph.\n"
            "Overview\n"
            "--------\n"
            "> [!NOTE]\n"
            "> ```bash\n"
            "> # comment inside blockquote fence\n"
            "> echo ok\n"
            "> ```\n"
            "### Overview\n"
            "Duplicate heading for slug disambiguation.\n"
        )
        (self.repo_dir / "spec.md").write_text(spec_md, encoding="utf-8")
        rc = stage_markdown_main(
            ["--root", str(self.repo_dir), "--output-dir", str(self.out_dir), "spec.md"]
        )
        self.assertEqual(rc, 0)

        sections = self._query(
            f"SELECT section_id, level, heading, breadcrumb, anchor_slug, "
            f"line_start, line_end, subtree_line_end, own_code_blocks "
            f"FROM staged_md_sections('{self.out_dir}');"
        )
        self.assertEqual(
            sections,
            [
                {
                    "section_id": 1,
                    "level": 1,
                    "heading": "Specification Title",
                    "breadcrumb": "Specification Title",
                    "anchor_slug": "specification-title",
                    "line_start": 1,
                    "line_end": 3,
                    "subtree_line_end": 12,
                    "own_code_blocks": 0,
                },
                {
                    "section_id": 2,
                    "level": 2,
                    "heading": "Overview",
                    "breadcrumb": "Specification Title > Overview",
                    "anchor_slug": "overview",
                    "line_start": 4,
                    "line_end": 10,
                    "subtree_line_end": 12,
                    "own_code_blocks": 1,
                },
                {
                    "section_id": 3,
                    "level": 3,
                    "heading": "Overview",
                    "breadcrumb": "Specification Title > Overview > Overview",
                    "anchor_slug": "overview-1",
                    "line_start": 11,
                    "line_end": 12,
                    "subtree_line_end": 12,
                    "own_code_blocks": 0,
                },
            ],
        )

        blocks = self._query(
            f"SELECT block_id, section_id, lang, code_lines, code "
            f"FROM staged_md_code_blocks('{self.out_dir}');"
        )
        self.assertEqual(
            blocks,
            [
                {
                    "block_id": 1,
                    "section_id": 2,
                    "lang": "bash",
                    "code_lines": 2,
                    "code": "# comment inside blockquote fence\necho ok",
                }
            ],
        )

    def test_tables_frontmatter_and_tags(self) -> None:
        doc_md = (
            "---\n"
            "name: table-spec\n"
            "description: >-\n"
            "  Multi-line folded\n"
            "  frontmatter summary.\n"
            "owners:\n"
            "  - alice\n"
            "  - bob\n"
            "---\n"
            "# Config Matrix\n"
            "Tracked with #direct and #time:2026-10-20.\n"
            "| Flag |Expr | Default |\n"
            "| :--- | :---: | ---: |\n"
            "| `--mode` | `a | b` | `fast` |\n"
            r"| `--pipe` | a \| b | `slow` |"
            "\n"
        )
        (self.repo_dir / "matrix.md").write_text(doc_md, encoding="utf-8")
        stage_markdown_main(
            [
                "--root",
                str(self.repo_dir),
                "--output-dir",
                str(self.out_dir),
                "matrix.md",
            ]
        )

        fm = self._query(
            f"SELECT file_path, has_frontmatter, line_end, "
            f"attributes->>'name' AS name, "
            f"attributes->>'description' AS description, "
            f"tags "
            f"FROM staged_md_frontmatter('{self.out_dir}');"
        )
        self.assertEqual(
            fm,
            [
                {
                    "file_path": "matrix.md",
                    "has_frontmatter": True,
                    "line_end": 9,
                    "name": "table-spec",
                    "description": "Multi-line folded frontmatter summary.",
                    "tags": ["#direct", "#time:2026-10-20"],
                }
            ],
        )

        tables = self._query(
            f"SELECT table_id, row_index, cells->>'Flag' AS flag, "
            f"cells->>'Expr' AS expr, cells->>'Default' AS def_val "
            f"FROM staged_md_tables('{self.out_dir}') "
            f"ORDER BY row_index;"
        )
        self.assertEqual(
            tables,
            [
                {
                    "table_id": 1,
                    "row_index": 1,
                    "flag": "`--mode`",
                    "expr": "`a | b`",
                    "def_val": "`fast`",
                },
                {
                    "table_id": 1,
                    "row_index": 2,
                    "flag": "`--pipe`",
                    "expr": "a | b",
                    "def_val": "`slow`",
                },
            ],
        )

    def test_links_broken_links_and_requirements(self) -> None:
        (self.repo_dir / "other.md").write_text(
            "# Target Doc\n## Valid Section\nContent.\n", encoding="utf-8"
        )
        main_md = (
            "# Normative Spec\n"
            "The client MUST validate tokens before forwarding.\n"
            "The server MUST NOT log raw secrets.\n"
            "```python\n"
            "# MUST inside code block is ignored\n"
            "```\n"
            "Ignore `[fake](nope.md)` in inline code.\n"
            "Valid link to [Other Valid](other.md#valid-section).\n"
            "Broken anchor to [Other Bad Anchor](other.md#nonexistent).\n"
            "Broken file link via [Ref Link][missing-ref].\n"
            "\n"
            "[missing-ref]: missing_file.md\n"
        )
        (self.repo_dir / "main.md").write_text(main_md, encoding="utf-8")
        stage_markdown_main(
            [
                "--root",
                str(self.repo_dir),
                "--output-dir",
                str(self.out_dir),
                "*.md",
            ]
        )

        broken = self._query(
            f"SELECT file_path, label, target_path, anchor, reason "
            f"FROM staged_md_broken_links('{self.out_dir}');"
        )
        self.assertEqual(
            broken,
            [
                {
                    "file_path": "main.md",
                    "label": "Other Bad Anchor",
                    "target_path": "other.md",
                    "anchor": "nonexistent",
                    "reason": "missing_anchor",
                },
                {
                    "file_path": "main.md",
                    "label": "Ref Link",
                    "target_path": "missing_file.md",
                    "anchor": "",
                    "reason": "missing_file",
                },
            ],
        )

        reqs = self._query(
            f"SELECT file_path, section_heading, line_num, keyword, statement "
            f"FROM staged_md_requirements('{self.out_dir}');"
        )
        self.assertEqual(
            reqs,
            [
                {
                    "file_path": "main.md",
                    "section_heading": "Normative Spec",
                    "line_num": 2,
                    "keyword": "MUST",
                    "statement": "The client MUST validate tokens before forwarding.",
                },
                {
                    "file_path": "main.md",
                    "section_heading": "Normative Spec",
                    "line_num": 3,
                    "keyword": "MUST NOT",
                    "statement": "The server MUST NOT log raw secrets.",
                },
            ],
        )

    def test_split_chunks_and_manifest(self) -> None:
        long_md = (
            "# Part One\n" + ("Line a\n" * 10) + "## Part Two\n" + ("Line b\n" * 10)
        )
        (self.repo_dir / "long.md").write_text(long_md, encoding="utf-8")
        stage_markdown_main(
            [
                "--root",
                str(self.repo_dir),
                "--output-dir",
                str(self.out_dir),
                "--split-chunks",
                "--max-lines",
                "12",
                "long.md",
            ]
        )

        manifest_path = self.out_dir / "manifest.json"
        self.assertTrue(manifest_path.is_file())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["chunks"]), 2)
        self.assertEqual(manifest["chunks"][0]["chunk_name"], "md_chunk_01.md")
        self.assertEqual(manifest["chunks"][1]["chunk_name"], "md_chunk_02.md")

        sections = self._query(
            f"SELECT section_id, heading, chunk_name "
            f"FROM staged_md_sections('{self.out_dir}');"
        )
        self.assertEqual(
            sections,
            [
                {
                    "section_id": 1,
                    "heading": "Part One",
                    "chunk_name": "md_chunk_01.md",
                },
                {
                    "section_id": 2,
                    "heading": "Part Two",
                    "chunk_name": "md_chunk_02.md",
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
