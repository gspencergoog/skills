"""Fitness tests that keep the schema reference and reader contract in step with the DDL."""

from __future__ import annotations

import re
import shutil
import tempfile
import unittest
from pathlib import Path

from db_migrations import (
    IMPORT_ROW_FIXUPS,
    LATEST_SCHEMA_VERSION,
    MIN_IMPORT_SCHEMA_VERSION,
)
from db_schema import PERSONAL_TABLES, REPO_GIT_TABLES, REPO_GITHUB_TABLES
from db_store import PackratStore

SKILL_DIR = Path(__file__).resolve().parent.parent
SCHEMA_DOC = SKILL_DIR / "references" / "schema_and_recipes.md"
CONTRACT_DOC = SKILL_DIR / "references" / "reader_contract.md"

TABLE_ROW_RE = re.compile(r"^\| `([a-z_]+)` \| (.*?) \| (.*) \|$")
IDENT_RE = re.compile(r"`([a-z][a-z0-9_]*)`")

INTERNAL_TABLE_PREFIXES = ("chunks_fts_", "sqlite_")
"""Tables the docs don't list: FTS5 shadow tables and SQLite internals."""

VALUE_TOKENS = frozenset(
    {
        # Enumerated values and prose that happen to be backticked lowercase words.
        "mirror",
        "items",
        "issue",
        "pr",
        "closes",
        "references",
        "closed_by",
        "xref",
        "mention",
        "commit",
        "graphql",
        "core",
        "authored",
        "review_requested",
        "reviewed",
        "issues",
        "notifications_last_modified",
        "porter unicode61",
        "v_viewer_items",
        "v_watches",
    }
)
"""Backticked lowercase tokens in the column cells that are values, not column names."""


def _table_rows(text: str) -> dict[str, tuple[str, str]]:
    """Returns `{table: (primary_key_cell, columns_cell)}` for every schema table row."""
    rows: dict[str, tuple[str, str]] = {}
    in_schema_table = False
    for line in text.splitlines():
        if line.startswith("| Table | Primary Key |"):
            in_schema_table = True
            continue
        if not line.startswith("|"):
            in_schema_table = False
            continue
        m = TABLE_ROW_RE.match(line)
        if in_schema_table and m:
            rows[m.group(1)] = (m.group(2), m.group(3))
    return rows


def _documented_columns(pk_cell: str, cols_cell: str) -> set[str]:
    """Extracts the column identifiers documented in a table row."""
    idents = set(IDENT_RE.findall(cols_cell))
    tuple_pk = re.match(r"^`\(([a-z0-9_, ]+)\)`", pk_cell)
    if tuple_pk:
        idents |= {c.strip() for c in tuple_pk.group(1).split(",")}
    else:
        idents |= set(IDENT_RE.findall(pk_cell))
    return {i for i in idents if i not in VALUE_TOKENS}


class TestSchemaDocs(unittest.TestCase):
    """Compares the Markdown schema inventory against a freshly created database."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="packrat-schema-docs-"))
        cls.store = PackratStore(cls.tmp / "repos.sqlite")
        conn = cls.store.conn
        cls.tables: dict[str, set[str]] = {}
        cls.views: dict[str, set[str]] = {}
        for name, kind in conn.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
        ):
            if name.startswith(INTERNAL_TABLE_PREFIXES):
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({name})")}
            (cls.tables if kind == "table" else cls.views)[name] = cols
        cls.schema_text = SCHEMA_DOC.read_text(encoding="utf-8")
        cls.contract_text = CONTRACT_DOC.read_text(encoding="utf-8")
        cls.doc_rows = _table_rows(cls.schema_text)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_table_is_documented_and_every_documented_table_exists(self) -> None:
        self.assertEqual(sorted(self.doc_rows), sorted(self.tables))

    def test_documented_columns_exist(self) -> None:
        for table, (pk, cols) in self.doc_rows.items():
            documented = _documented_columns(pk, cols)
            with self.subTest(table=table):
                self.assertEqual(
                    sorted(documented - self.tables[table]),
                    [],
                    f"documented columns missing from {table}",
                )

    def test_actual_columns_are_documented(self) -> None:
        for table, actual in self.tables.items():
            pk, cols = self.doc_rows[table]
            documented = _documented_columns(pk, cols)
            with self.subTest(table=table):
                self.assertEqual(
                    sorted(actual - documented), [], f"undocumented columns in {table}"
                )

    def test_reader_views_listed_in_schema_doc(self) -> None:
        listed = set(re.findall(r"`(v_[a-z_]+)`", self.schema_text))
        self.assertEqual(sorted(set(self.views) - listed), [])

    def test_reader_contract_documents_every_sqlite_view(self) -> None:
        headed = set(
            re.findall(r"^### `(v_[a-z_]+)`", self.contract_text, re.MULTILINE)
        )
        mentioned = set(re.findall(r"`(v_[a-z_]+)`", self.contract_text))
        self.assertEqual(sorted(set(self.views) - mentioned), [])
        for view in ("v_pr_live", "v_pr_checks", "v_service_health", "v_viewer_items"):
            self.assertIn(view, headed)

    def test_service_health_columns_documented_in_contract(self) -> None:
        section = self.contract_text.split("### `v_service_health`", 1)[1]
        section = section.split("\n## ", 1)[0]
        for col in self.views["v_service_health"]:
            with self.subTest(column=col):
                self.assertIn(f"`{col}`", section)

    def test_partitioning_claim_is_accurate(self) -> None:
        for doc in (
            self.schema_text,
            self.contract_text,
            (SKILL_DIR / "SKILL.md").read_text(),
        ):
            self.assertNotIn("Every table is partitioned by", doc)
            self.assertNotIn("All tables are partitioned by", doc)
        self.assertNotIn("has no leases view", self.schema_text)

    def test_every_repo_id_table_has_exactly_one_portability_class(self) -> None:
        """Checks that every `repo_id` table belongs to exactly one portability class."""
        special = frozenset({"repos", "chunks", "repo_aliases"})
        classes = (
            frozenset(REPO_GITHUB_TABLES),
            frozenset(REPO_GIT_TABLES),
            frozenset(PERSONAL_TABLES),
            special,
        )
        self.assertEqual(len(REPO_GITHUB_TABLES), len(classes[0]))
        self.assertEqual(len(REPO_GIT_TABLES), len(classes[1]))
        self.assertEqual(len(PERSONAL_TABLES), len(classes[2]))
        self.assertEqual(sum(len(c) for c in classes), len(frozenset().union(*classes)))
        for table in (*REPO_GITHUB_TABLES, *REPO_GIT_TABLES, *special):
            self.assertIn("repo_id", self.tables.get(table, set()), table)
        for table in PERSONAL_TABLES:
            self.assertIn(table, self.tables)
        conn = self.store.conn
        all_tables = [
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        for table in all_tables:
            cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            if "repo_id" in cols:
                matches = [cls_set for cls_set in classes if table in cls_set]
                self.assertEqual(
                    len(matches),
                    1,
                    f"{table} matched {len(matches)} portability classes",
                )

    def test_every_post_minimum_migration_declares_an_import_fixup(self) -> None:
        """Checks that every migration past `MIN_IMPORT_SCHEMA_VERSION` declares a fixup."""
        expected = set(range(MIN_IMPORT_SCHEMA_VERSION + 1, LATEST_SCHEMA_VERSION + 1))
        self.assertEqual(set(IMPORT_ROW_FIXUPS), expected)


if __name__ == "__main__":
    unittest.main()
