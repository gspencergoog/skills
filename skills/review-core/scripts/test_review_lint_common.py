#!/usr/bin/env python3
"""Unit tests for review_lint_common.py."""

from __future__ import annotations

import difflib
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_SCRIPTS_DIR = Path(__file__).resolve().parent
_WRITE_PROSE_SCRIPTS = _SCRIPTS_DIR.parents[1] / "write-prose" / "scripts"
_CONTEXT_INDEXER_SCRIPTS = _SCRIPTS_DIR.parents[1] / "context-indexer" / "scripts"
for _p in map(str, (_SCRIPTS_DIR, _WRITE_PROSE_SCRIPTS, _CONTEXT_INDEXER_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import review_lint_common as rlc
import stage_diffs
from analyze_prose import analyze_prose

CODE_PROFILE = rlc.ReportProfile(
    name="code",
    sections=(),
    findings_section="Review Comments (Ordered by Severity)",
    questions_section="Questions",
    required_fields=("File", "Line", "Severity", "Evidence", "Body", "Why"),
    optional_fields=("Category", "Focus", "Suggestion", "Details", "Repro test"),
    validators={"Focus": re.compile(r"^`[a-z0-9][a-z0-9-]*(?::[a-z0-9-]+)?`$")},
)

DESIGN_PROFILE = rlc.ReportProfile(
    name="design",
    sections=(
        "Summary",
        "Principle Scorecard",
        "View & Boundary Assessment",
        "Findings (Ordered by Severity)",
        "Open Design Questions",
        "Checked and Found Clean",
        "Recommended Next Steps",
    ),
    findings_section="Findings (Ordered by Severity)",
    questions_section="Open Design Questions",
    required_fields=(
        "Target",
        "Door",
        "Severity",
        "Principle",
        "Evidence",
        "Body",
        "Why",
        "Suggestion",
    ),
    optional_fields=("Focus", "Details"),
    validators={
        "Door": re.compile(r"^`?(?:one-way|two-way)`?$", re.IGNORECASE),
        "Principle": re.compile(
            r"^`?(?:problem-scope|module-depth|boundaries-state|tradeoffs-simplicity|"
            r"resilience-ops|evolution-fitness|ergonomics-coherence)`?$"
        ),
    },
)

CODE_FIXTURE = """\
## Summary
Checked two files.
Verdict: Minor Revisions Needed

## Review Comments (Ordered by Severity)

### H1. Missing null guard on lookup
- **File**: `pkg/store.py`
- **Line**: `42`
- **Severity**: `high`
- **Evidence**: `Read` — `pkg/store.py:42` dereferences `row[0]`
- **Body**: `get_item` indexes the query tuple without checking for `None`.
- **Why**: Missing keys raise `TypeError` instead of returning `None`.
- **Suggestion**:
```python
return row[0] if row is not None else None
```

## Questions

### Q1. Can callers pass an empty tenant id?
- **File**: `pkg/store.py`
- **Line**: `18`
- **Evidence**: `Speculation` — Checked local callers only.
- **Why**: Empty tenant ids share cache keys (`high` if reachable).
- **To settle**: Inspect `routes.py` input validation.

## Checked and Found Clean
- Cache eviction race: `Read` — `pkg/store.py:12` holds `self._lock`.
"""

DESIGN_FIXTURE = """\
## Summary
Reviewed the store spec.
Verdict: Ready

## Principle Scorecard
| Principle | Score | Assessment |
| :--- | :---: | :--- |

## View & Boundary Assessment
- Module view is acyclic.

## Findings (Ordered by Severity)

### M1. Shared cache key format
- **Target**: `pkg/keys.py:10`
- **Door**: `two-way`
- **Severity**: `medium`
- **Principle**: `module-depth`
- **Evidence**: `Read`: `pkg/keys.py:10` formats keys directly
- **Body**: Two modules format cache keys by hand.
- **Why**: Format drift causes cache misses.
- **Suggestion**: Move formatting into `format_key(tenant)`.

## Open Design Questions
None.

## Checked and Found Clean
- Import cycles: `Executed` — `prepare_design_context.py` found 0 cycles.

## Recommended Next Steps
1. Extract `format_key`.
"""


class ReviewLintCommonTests(unittest.TestCase):
    """Covers parsing, sequence, evidence, verdict, and prose boundary checks."""

    def test_strip_ticks_and_extract_tier(self) -> None:
        self.assertEqual(rlc.strip_ticks(" `high` "), "high")
        self.assertEqual(rlc.extract_tier("`Read` — foo.py:1"), "Read")
        self.assertEqual(rlc.extract_tier("Executed: pytest passed"), "Executed")
        self.assertEqual(rlc.extract_tier("Guesswork: none"), "")
        self.assertEqual(rlc.extract_evidence_detail("`Read`: line 10"), "line 10")

    def test_code_and_design_fixtures_pass(self) -> None:
        for text, profile in (
            (CODE_FIXTURE, CODE_PROFILE),
            (DESIGN_FIXTURE, DESIGN_PROFILE),
        ):
            sections = rlc.parse_sections(text)
            findings = rlc.parse_items(
                sections[profile.findings_section], ("C", "H", "M", "L")
            )
            questions = rlc.parse_items(sections[profile.questions_section], ("Q",))
            clean = [
                (ln, s[2:].strip())
                for ln, s in sections["Checked and Found Clean"]
                if s.strip().startswith("- ")
            ]
            report = rlc.ParsedReport(
                sections=sections,
                findings=findings,
                questions=questions,
                clean_lines=clean,
            )

            self.assertEqual(rlc.check_sections(report, profile), [])
            self.assertEqual(rlc.check_id_sequence(findings), [])
            self.assertEqual(rlc.check_id_sequence(questions, expected_prefix="Q"), [])
            self.assertEqual(rlc.check_severity_matches_id(findings), [])
            self.assertEqual(
                rlc.check_evidence_caps(findings, is_findings=True, profile=profile), []
            )
            self.assertEqual(rlc.check_required_fields(findings, profile), [])
            self.assertEqual(rlc.check_questions(questions), [])
            self.assertEqual(rlc.check_clean_lines(clean), [])
            self.assertEqual(rlc.check_single_fence_per_field(findings), [])
            self.assertEqual(
                rlc.check_verdict([s for _, s in sections["Summary"]], findings), []
            )
            self.assertEqual(rlc.check_prose(findings, profile, analyze_prose), [])

    def test_verdict_strings_and_contradiction(self) -> None:
        high = [rlc.Item("H1", "Blocker", {"Severity": "high"}, 10)]
        for verdict in rlc.VALID_VERDICTS:
            res = rlc.check_verdict(
                [f"Verdict: {verdict}"], [] if verdict == "Ready" else high
            )
            self.assertEqual(res, [])
        bad = rlc.check_verdict(["Verdict: Ready"], high)
        self.assertEqual([v.rule for v in bad], ["verdict-contradicts-findings"])
        missing = rlc.check_verdict(["No verdict here."], [])
        self.assertEqual([v.rule for v in missing], ["missing-verdict"])

    def test_recalled_and_speculation_caps(self) -> None:
        items = [
            rlc.Item(
                "H1", "One", {"Severity": "high", "Evidence": "`Speculation`: guess"}, 5
            ),
            rlc.Item(
                "M1", "Two", {"Severity": "medium", "Evidence": "`Recalled`: old"}, 12
            ),
        ]
        rules = {
            v.rule
            for v in rlc.check_evidence_caps(
                items, is_findings=True, require_detail=True
            )
        }
        self.assertEqual(rules, {"severity-cap", "evidence-recalled"})

    def test_prose_120_words_6_sentences_boundary(self) -> None:
        sent = (
            "The cache returns stale rows after the second retry, and the eviction "
            "timer never resets on new writes from the worker."
        )  # 21 words
        five_sents = " ".join([sent] * 5)  # 105 words, 5 sentences -> passes 120/6
        six_sents = " ".join([sent] * 6)  # 126 words, 6 sentences -> fails 120 words
        item_ok = rlc.Item("M1", "Ok", {"Body": five_sents, "Why": "Breaks."}, 10)
        item_long = rlc.Item("M2", "Long", {"Body": six_sents, "Why": "Breaks."}, 20)
        self.assertEqual(rlc.check_prose([item_ok], CODE_PROFILE, analyze_prose), [])
        hits = rlc.check_prose([item_long], CODE_PROFILE, analyze_prose)
        self.assertIn("comment-prose-length", {v.rule for v in hits})

    def test_violation_aliases(self) -> None:
        v = rlc.Violation("some-rule", "Bad", line=14, fix_hint="Do X")
        self.assertEqual(v.lineno, 14)
        self.assertEqual(v.fix, "Do X")


def _numbered(prefix: str, count: int) -> list[str]:
    return [f"{prefix} line {number}\n" for number in range(1, count + 1)]


def _file_diff(path: str, old: str | None, new: str | None, context: int = 3) -> str:
    """Returns a git-style diff of one file, where None means the file is absent."""
    header = [f"diff --git a/{path} b/{path}\n"]
    if old is None:
        header.append("new file mode 100644\n")
    if new is None:
        header.append("deleted file mode 100644\n")
    body = difflib.unified_diff(
        (old or "").splitlines(keepends=True),
        (new or "").splitlines(keepends=True),
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
        n=context,
    )
    return "".join([*header, *body])


def _line_of(text: str, needle: str) -> int:
    """Returns the 1-based number of the first line of `text` that holds `needle`."""
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    raise AssertionError(f"{needle!r} is not in the fixture")


def _item(
    item_id: str = "H1",
    file: str | None = "pkg/store.py",
    line: str | int | None = 1,
    evidence: str = "`Read`: the row is read without a guard",
    lineno: int = 10,
    **fields: str,
) -> rlc.Item:
    data: dict[str, str] = {"Evidence": evidence}
    if file is not None:
        data["File"] = f"`{file}`"
    if line is not None:
        data["Line"] = f"`{line}`"
    data.update(fields)
    return rlc.Item(item_id, "Finding", data, lineno)


_MIXED = _numbered("mixed", 8)
LINE_CHANGES: dict[str, tuple[str | None, str | None]] = {
    # Lines 6 and 7 deleted from the middle.
    "pkg/mid.py": (
        "".join(_numbered("mid", 12)),
        "".join(_numbered("mid", 5) + _numbered("mid", 12)[7:]),
    ),
    # Lines 1 and 2 deleted from the top.
    "pkg/top.py": ("".join(_numbered("top", 6)), "".join(_numbered("top", 6)[2:])),
    # Lines 5 and 6 deleted from the end.
    "pkg/eof.py": ("".join(_numbered("eof", 6)), "".join(_numbered("eof", 4))),
    # Line 2 replaced, line 5 deleted, and a line added after old line 7.
    "pkg/mixed.py": (
        "".join(_MIXED),
        "".join(
            [_MIXED[0], "MIXED LINE 2\n", *_MIXED[2:4], *_MIXED[5:7], "mixed extra\n"]
            + _MIXED[7:]
        ),
    ),
    "pkg/gone.py": ("gone line 1\ngone line 2\n", None),
    "pkg/empty_gone.py": ("", None),
    "pkg/emptied.py": ("emptied line 1\nemptied line 2\n", ""),
    "pkg/new.py": (None, "new line 1\n"),
}

# File diffs that carry no hunks, so stage_diffs writes no line records for them.
HUNKLESS_DIFF = (
    "diff --git a/pkg/old_name.py b/pkg/renamed.py\n"
    "similarity index 100%\n"
    "rename from pkg/old_name.py\n"
    "rename to pkg/renamed.py\n"
    "diff --git a/tools/run.sh b/tools/run.sh\n"
    "old mode 100644\n"
    "new mode 100755\n"
    "diff --git a/pkg/new_empty.py b/pkg/new_empty.py\n"
    "new file mode 100644\n"
    "index 0000000..e69de29\n"
    "diff --git a/assets/logo.png b/assets/logo.png\n"
    "deleted file mode 100644\n"
    "index 6772730..0000000\n"
    "Binary files a/assets/logo.png and /dev/null differ\n"
)

STORE_OLD = '''\
"""Tenant store backed by SQLite."""

import sqlite3

QUERY = "SELECT value FROM items WHERE tenant = ? AND key = ?"


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.cache = {}

    def get_item(self, tenant, key):
        row = self.db.execute(QUERY, (tenant, key)).fetchone()
        if row is None:
            return None
        return row[0]

    def close(self):
        self.db.close()
'''

STORE_NEW = '''\
"""Tenant store backed by SQLite."""

import sqlite3

QUERY = "SELECT value FROM items WHERE tenant = ? AND key = ?"


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.cache = {}

    def get_item(self, tenant, key):
        cached = self.cache.get((tenant, key))
        if cached is not None:
            return cached
        row = self.db.execute(QUERY, (tenant, key)).fetchone()
        return row[0]

    def close(self):
        self.db.close()
'''

ROUTES = '''\
"""HTTP routes for the tenant store."""

from pkg.store import Store


def lookup(store: Store, request):
    tenant = request.headers.get("X-Tenant", "")
    return store.get_item(tenant, request.args['key'])
'''

LEGACY = "def legacy_lookup(db, key):\n    return db[key]  # no tenant scoping\n"

STORE_CHANGES: dict[str, tuple[str | None, str | None]] = {
    "pkg/store.py": (STORE_OLD, STORE_NEW),
    "pkg/legacy.py": (LEGACY, None),
}

LONG_TABLE = "".join(f"entry number {n} of the long table\n" for n in range(1, 61))

GUIDE = """\
# Release guide

6. **Always End Entries with a Link to the PR**:
   - Conclude each entry with a clickable link to its associated pull request:
     `[#<pr-number>](https://example.invalid/pull/<pr-number>)`
7. Run the [`validate-patterns`](../tools/validate.md) skill's `validate` command
   after raising the **minor** version.
"""

GRAPH = """\
/**
 * Walks the graph depth-first from the root, raising an error on a self-reference,
 * a back edge, or a traversal deeper than the global limit.
 */
/// Returns the reachable set, which the orphan check
/// reuses after the walk.
export function walk(root) {}
"""

HOOKS = """\
# A minimal environment keeps the developer's git config, and any
# GIT_DIR exported by a calling hook, out of the test repository.
ENV = {}
LIMIT = RETRIES - 1
"""

ROW_LINE = _line_of(STORE_NEW, "row = self.db.execute")
CACHED_LINE = _line_of(STORE_NEW, "cached = self.cache.get")
ROW_QUOTE = '"row = self.db.execute(QUERY, (tenant, key)).fetchone()"'

PLAYBOOK = """\
# Deletion and Consolidation

Theme id: `deletion-and-consolidation`. Lane for merges: A.

## Cues (for triage)

1. A numbered cue is not a failure mode.

## Failure modes

1. A deleted symbol is still referenced.
   Look at: every deleted name. Settles it: `rg` the name; `Read`.
2. Deleted tests covered a scenario that nothing else covers.
   Look at: each deleted assertion. Settles it: `rg` the suite; `Read`.
3. Consolidation drops a branch that one caller needs.

```text
9. A numbered line inside a code fence is not a failure mode.
```

## Hypothesis templates

7. A numbered template is not a failure mode.
"""


class StagedDiffCase(unittest.TestCase):
    """Builds a workspace and staged diff directories in a temporary directory."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()

    def write(self, rel: str, text: str) -> Path:
        path = self.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def stage(self, diff_text: str, name: str = "staged") -> Path:
        """Stages `diff_text` with the real stage_diffs.py and returns the directory."""
        diff_dir = self.root / name
        stage_diffs.split_diff_grouped(diff_text, str(diff_dir))
        return diff_dir

    def stage_changes(
        self,
        changes: dict[str, tuple[str | None, str | None]],
        context: int = 3,
        extra_diff: str = "",
    ) -> rlc.StagedDiffIndex:
        """Writes the new side of `changes` to the workspace and indexes their diff."""
        parts = []
        for rel, (old, new) in changes.items():
            parts.append(_file_diff(rel, old, new, context))
            if new is not None:
                self.write(rel, new)
        diff_dir = self.stage("".join(parts) + extra_diff, f"staged_u{context}")
        return rlc.load_staged_diff_index(diff_dir)

    def check(
        self,
        item: rlc.Item,
        index: rlc.StagedDiffIndex | None = None,
        workspace: bool = True,
    ) -> list[rlc.Violation]:
        return rlc.check_grounding(
            [item],
            workspace=self.workspace if workspace else None,
            diff_index=index,
        )

    def rules(
        self, item: rlc.Item, index: rlc.StagedDiffIndex | None = None
    ) -> list[str]:
        return [violation.rule for violation in self.check(item, index)]


class LoadStagedDiffIndexTests(StagedDiffCase):
    """Covers the line tables built from real stage_diffs.py output."""

    def test_three_context_lines_and_none_give_the_same_index(self) -> None:
        with_context = self.stage_changes(LINE_CHANGES, 3, HUNKLESS_DIFF)
        without_context = self.stage_changes(LINE_CHANGES, 0, HUNKLESS_DIFF)
        self.assertEqual(with_context, without_context)

    def test_mid_file_deletion_records_the_lines_on_both_sides(self) -> None:
        for context in (3, 0):
            with self.subTest(context=context):
                index = self.stage_changes(LINE_CHANGES, context)
                self.assertEqual(
                    index.deletion_seam_lines_by_file["pkg/mid.py"], {5, 6}
                )
                self.assertEqual(index.deleted_old_lines_by_file["pkg/mid.py"], {6, 7})
                self.assertEqual(
                    index.deleted_line_texts_by_file["pkg/mid.py"],
                    ("mid line 6", "mid line 7"),
                )
                self.assertEqual(index.added_lines_by_file["pkg/mid.py"], frozenset())

    def test_mixed_hunk_records_added_lines_and_every_deletion_seam(self) -> None:
        for context in (3, 0):
            with self.subTest(context=context):
                index = self.stage_changes(LINE_CHANGES, context)
                self.assertEqual(index.added_lines_by_file["pkg/mixed.py"], {2, 7})
                self.assertEqual(
                    index.deletion_seam_lines_by_file["pkg/mixed.py"], {1, 2, 4, 5}
                )
                self.assertEqual(
                    index.deleted_old_lines_by_file["pkg/mixed.py"], {2, 5}
                )
                self.assertEqual(
                    index.deleted_line_texts_by_file["pkg/mixed.py"],
                    ("mixed line 2", "mixed line 5"),
                )

    def test_top_of_file_deletion_without_context_clamps_the_seam_to_line_one(
        self,
    ) -> None:
        index = self.stage_changes(LINE_CHANGES, 0)
        hunks = [
            json.loads(line)
            for line in (self.root / "staged_u0" / "diff_hunks.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        (top_hunk,) = [hunk for hunk in hunks if hunk["file_path"] == "pkg/top.py"]
        self.assertEqual((top_hunk["new_start"], top_hunk["new_count"]), (0, 0))
        self.assertEqual(index.deletion_seam_lines_by_file["pkg/top.py"], {1})
        self.assertEqual(index.deleted_old_lines_by_file["pkg/top.py"], {1, 2})

    def test_end_of_file_deletion_keeps_old_line_numbers_past_the_new_end(self) -> None:
        for context in (3, 0):
            with self.subTest(context=context):
                index = self.stage_changes(LINE_CHANGES, context)
                self.assertEqual(index.deleted_old_lines_by_file["pkg/eof.py"], {5, 6})
                self.assertEqual(
                    index.deletion_seam_lines_by_file["pkg/eof.py"], {4, 5}
                )

    def test_deleted_files_come_from_hunks_and_from_chunk_headers(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3, HUNKLESS_DIFF)
        self.assertEqual(
            index.deleted_files, {"pkg/gone.py", "pkg/empty_gone.py", "assets/logo.png"}
        )
        self.assertEqual(index.deleted_old_lines_by_file["pkg/gone.py"], {1, 2})
        self.assertEqual(
            index.deleted_line_texts_by_file["pkg/gone.py"],
            ("gone line 1", "gone line 2"),
        )
        # An empty or binary deleted file has no hunk, so only the chunk header
        # listed in manifest.json shows the deletion.
        for hunkless in ("pkg/empty_gone.py", "assets/logo.png"):
            self.assertNotIn(hunkless, index.deleted_old_lines_by_file)
        for deleted in index.deleted_files:
            self.assertNotIn(deleted, index.added_lines_by_file)
            self.assertNotIn(deleted, index.deletion_seam_lines_by_file)

    def test_file_emptied_but_kept_is_not_a_deleted_file(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 0)
        self.assertNotIn("pkg/emptied.py", index.deleted_files)
        self.assertEqual(index.added_lines_by_file["pkg/emptied.py"], frozenset())
        self.assertEqual(index.deletion_seam_lines_by_file["pkg/emptied.py"], {1})
        self.assertEqual(index.deleted_old_lines_by_file["pkg/emptied.py"], {1, 2})

    def test_files_changed_without_hunks_have_empty_added_sets(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3, HUNKLESS_DIFF)
        for hunkless in ("pkg/renamed.py", "tools/run.sh", "pkg/new_empty.py"):
            self.assertEqual(index.added_lines_by_file[hunkless], frozenset())
            self.assertNotIn(hunkless, index.deleted_files)
        self.assertEqual(index.added_lines_by_file["pkg/new.py"], {1})
        self.assertNotIn("pkg/old_name.py", index.added_lines_by_file)

    def test_deleted_file_names_with_spaces_or_quoted_bytes_are_decoded(self) -> None:
        diff = (
            _file_diff("docs/release notes.md", "", None)
            + _file_diff("docs/a b/notes.md", "", None)
            + 'diff --git "a/docs/caf\\303\\251 \\"menu\\".md" "b/docs/caf\\303\\251 \\"menu\\".md"\n'
            + "deleted file mode 100644\n"
            + "index e69de29..0000000\n"
            + "diff --git a/docs/old plan.md b/docs/new plan.md\n"
            + "similarity index 100%\n"
            + "rename from docs/old plan.md\n"
            + "rename to docs/new plan.md\n"
        )
        index = rlc.load_staged_diff_index(self.stage(diff))
        self.assertLessEqual(
            {"docs/release notes.md", "docs/a b/notes.md", 'docs/caf\u00e9 "menu".md'},
            index.deleted_files,
        )
        self.assertIn("docs/new plan.md", index.added_lines_by_file)
        self.assertNotIn("docs/new plan.md", index.deleted_files)

    def test_hunk_body_line_that_looks_like_a_header_is_not_a_deletion(self) -> None:
        # The diff shows these lines as `--- a/fake` and `+++ /dev/null`.
        old = "keep this line\n-- a/fake\n"
        new = "keep this line\n++ /dev/null\n"
        index = rlc.load_staged_diff_index(
            self.stage(_file_diff("notes/patch.txt", old, new))
        )
        self.assertEqual(index.deleted_files, frozenset())
        self.assertIn("notes/patch.txt", index.added_lines_by_file)

    def test_header_lookalike_in_a_continued_hunk_is_not_a_deletion(self) -> None:
        added = [f"added line {number}\n" for number in range(1, 61)]
        added[39] = "++ /dev/null\n"
        diff = _file_diff(
            "notes/long.txt", "kept line\n", "kept line\n" + "".join(added)
        )
        diff_dir = self.root / "split"
        # A low line limit makes stage_diffs.py continue the hunk in later
        # chunks, where body lines follow the file header with no `@@` line.
        stage_diffs.split_diff_grouped(diff, str(diff_dir), max_lines=20)
        chunks = [
            path.read_text(encoding="utf-8")
            for path in sorted(diff_dir.glob("diff_chunk_*.diff"))
        ]
        continued = [chunk for chunk in chunks if "@@" not in chunk]
        self.assertTrue(any("+++ /dev/null" in chunk for chunk in continued))
        index = rlc.load_staged_diff_index(diff_dir)
        self.assertEqual(index.deleted_files, frozenset())
        self.assertIn("notes/long.txt", index.added_lines_by_file)

    def test_plain_unified_diff_without_git_headers_marks_deletions(self) -> None:
        diff = (
            "--- a/pkg/plain_gone.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-plain line 1\n"
            "-plain line 2\n"
            "--- a/pkg/plain_kept.py\n"
            "+++ b/pkg/plain_kept.py\n"
            "@@ -1,1 +1,2 @@\n"
            " plain kept 1\n"
            "+plain kept 2\n"
        )
        index = rlc.load_staged_diff_index(self.stage(diff))
        self.assertEqual(index.deleted_files, {"pkg/plain_gone.py"})
        self.assertEqual(index.added_lines_by_file, {"pkg/plain_kept.py": {2}})

    def test_line_records_alone_still_yield_seams_and_deleted_files(self) -> None:
        self.stage_changes(LINE_CHANGES, 3)
        staged = self.root / "staged_u3"
        full = rlc.load_staged_diff_index(staged)
        (staged / "manifest.json").unlink()
        without_manifest = rlc.load_staged_diff_index(staged)
        # Without chunk headers a file is called deleted when its hunks leave
        # no new-side lines, which also covers a file that was only emptied.
        self.assertEqual(
            without_manifest.deleted_files, {"pkg/gone.py", "pkg/emptied.py"}
        )
        self.assertEqual(
            without_manifest.deletion_seam_lines_by_file["pkg/mid.py"], {5, 6}
        )
        (staged / "diff_hunks.jsonl").unlink()
        lines_only = rlc.load_staged_diff_index(staged)
        self.assertEqual(lines_only.deleted_files, frozenset())
        self.assertEqual(lines_only.deletion_seam_lines_by_file["pkg/mid.py"], {5, 6})
        self.assertEqual(lines_only.deletion_seam_lines_by_file["pkg/top.py"], {1})
        self.assertEqual(lines_only.added_lines_by_file["pkg/mixed.py"], {2, 7})
        self.assertEqual(
            lines_only.deleted_old_lines_by_file, full.deleted_old_lines_by_file
        )

    def test_manifest_without_its_chunk_files_still_lists_changed_files(self) -> None:
        self.stage_changes(LINE_CHANGES, 3, HUNKLESS_DIFF)
        staged = self.root / "staged_u3"
        for chunk in staged.glob("diff_chunk_*.diff"):
            chunk.unlink()
        index = rlc.load_staged_diff_index(staged)
        self.assertEqual(index.added_lines_by_file["pkg/renamed.py"], frozenset())
        self.assertIn("pkg/gone.py", index.deleted_files)
        # Without its header, an empty deleted file looks like any changed file.
        self.assertNotIn("pkg/empty_gone.py", index.deleted_files)
        self.assertIn("pkg/empty_gone.py", index.added_lines_by_file)

    def test_header_without_a_and_b_prefixes_names_no_file(self) -> None:
        diff = (
            "diff --git pkg/bare.py pkg/bare.py\n"
            "deleted file mode 100644\n"
            "index e69de29..0000000\n"
        )
        index = rlc.load_staged_diff_index(self.stage(diff))
        self.assertEqual(index.deleted_files, frozenset())
        self.assertNotIn("pkg/bare.py", index.added_lines_by_file)

    def test_records_without_the_expected_fields_are_skipped(self) -> None:
        staged = self.root / "handmade"
        staged.mkdir()
        line_records = [
            [1, 2, 3],
            {"op": "+", "new_line": 4},
            {"file_path": "a.py", "hunk_id": 1, "op": "+", "new_line": 4},
            {"file_path": "a.py", "hunk_id": 1, "op": "-", "content": "gone"},
            {"file_path": "a.py", "hunk_id": 1, "op": " "},
        ]
        (staged / "diff_lines.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in line_records),
            encoding="utf-8",
        )
        (staged / "diff_hunks.jsonl").write_text(
            'null\n{"hunk_id": 1}\n', encoding="utf-8"
        )
        index = rlc.load_staged_diff_index(staged)
        self.assertEqual(index.added_lines_by_file, {"a.py": {4}})
        self.assertEqual(index.deletion_seam_lines_by_file, {"a.py": {4, 5}})
        self.assertEqual(index.deleted_line_texts_by_file, {"a.py": ("gone",)})
        self.assertEqual(index.deleted_old_lines_by_file, {})
        self.assertEqual(index.deleted_files, frozenset())

    def test_empty_diff_gives_an_empty_index(self) -> None:
        index = rlc.load_staged_diff_index(self.stage(""))
        self.assertEqual(
            index,
            rlc.StagedDiffIndex(
                added_lines_by_file={},
                deletion_seam_lines_by_file={},
                deleted_old_lines_by_file={},
                deleted_line_texts_by_file={},
                deleted_files=frozenset(),
            ),
        )

    def test_directory_that_is_not_a_staged_diff_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            rlc.load_staged_diff_index(self.root / "absent")
        with self.assertRaises(FileNotFoundError):
            rlc.load_staged_diff_index(self.workspace)

    def test_malformed_json_names_the_file_and_line(self) -> None:
        staged = self.stage(_file_diff("pkg/new.py", None, "new line 1\n"))
        (staged / "manifest.json").write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"manifest\.json: not a JSON document"):
            rlc.load_staged_diff_index(staged)
        (staged / "manifest.json").unlink()
        with (staged / "diff_lines.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("\n{broken\n")
        with self.assertRaisesRegex(
            ValueError, r"diff_lines\.jsonl:3: not a JSON record"
        ):
            rlc.load_staged_diff_index(staged)

    def test_string_path_is_accepted(self) -> None:
        staged = self.stage(_file_diff("pkg/new.py", None, "new line 1\n"))
        index = rlc.load_staged_diff_index(str(staged))
        self.assertEqual(index.added_lines_by_file, {"pkg/new.py": {1}})


class GroundingAnchorTests(StagedDiffCase):
    """Covers the File and Line anchor rules of check_grounding."""

    def test_anchors_on_changed_lines_pass_with_and_without_context(self) -> None:
        anchors = [
            ("pkg/mixed.py", 2),  # added line
            ("pkg/mixed.py", "6-8"),  # range that overlaps an added line
            ("pkg/mid.py", 5),  # line before a mid-file deletion
            ("pkg/mid.py", 6),  # line after a mid-file deletion
            ("pkg/mid.py", 7),  # old number of a deleted line
            ("pkg/top.py", 1),  # line after a top-of-file deletion
            ("pkg/eof.py", 4),  # last line before an end-of-file deletion
            ("pkg/eof.py", 6),  # old number of a deleted line past the new end
            ("pkg/emptied.py", 1),  # file that survives with no lines
            ("pkg/gone.py", 2),  # deleted file
            ("pkg/gone.py", 40),  # any line of a deleted file
            ("pkg/empty_gone.py", 1),  # deleted file that was empty
            ("assets/logo.png", 1),  # deleted binary file
            ("pkg/renamed.py", 3),  # pure rename, no hunks
            ("pkg/new_empty.py", 1),  # new file with no lines
            ("pkg/new.py", 1),  # new file
        ]
        for context in (3, 0):
            index = self.stage_changes(LINE_CHANGES, context, HUNKLESS_DIFF)
            self.write("pkg/renamed.py", "".join(_numbered("renamed", 5)))
            self.write("pkg/new_empty.py", "")
            for file, line in anchors:
                with self.subTest(context=context, file=file, line=line):
                    self.assertEqual(self.check(_item(file=file, line=line), index), [])

    def test_off_hunk_line_names_the_nearest_changed_lines(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        (violation,) = self.check(_item(file="pkg/mid.py", line=9, lineno=42), index)
        self.assertEqual(violation.rule, "anchor-line-unchanged")
        self.assertEqual(violation.line, 42)
        self.assertEqual(
            violation.message,
            "'H1' Line 9 of `pkg/mid.py` is not a line the diff changed.",
        )
        self.assertEqual(
            violation.fix_hint,
            rlc.GROUNDING_FIX_HINTS["anchor-line-unchanged"]
            + " Nearest changed lines in `pkg/mid.py`: 5-6.",
        )
        (mixed,) = self.check(_item(file="pkg/mixed.py", line=8), index)
        self.assertTrue(
            mixed.fix_hint.endswith("Nearest changed lines in `pkg/mixed.py`: 4-5, 7.")
        )

    def test_context_line_inside_a_hunk_is_still_unchanged(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        self.assertEqual(
            self.rules(_item(file="pkg/mid.py", line=3), index),
            ["anchor-line-unchanged"],
        )

    def test_file_outside_the_diff_fails_the_changed_line_rule(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        self.write("pkg/routes.py", ROUTES)
        (violation,) = self.check(_item(file="pkg/routes.py", line=7), index)
        self.assertEqual(violation.rule, "anchor-line-unchanged")
        self.assertEqual(
            violation.message,
            "'H1' is anchored to `pkg/routes.py`, which the diff does not change.",
        )
        self.assertTrue(
            violation.fix_hint.startswith(
                rlc.GROUNDING_FIX_HINTS["anchor-line-unchanged"]
            )
        )

    def test_missing_file_is_reported_once(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        (violation,) = self.check(_item(file="pkg/nope.py", line=3), index)
        self.assertEqual(violation.rule, "anchor-file-missing")
        self.assertEqual(
            violation.message,
            "'H1' File `pkg/nope.py` is not a file in the reviewed checkout and is "
            "not deleted by the diff.",
        )
        self.assertEqual(
            violation.fix_hint, rlc.GROUNDING_FIX_HINTS["anchor-file-missing"]
        )
        # The old name of a renamed file and a directory are not files either.
        index = self.stage_changes(LINE_CHANGES, 3, HUNKLESS_DIFF)
        for not_a_file in ("pkg/old_name.py", "pkg"):
            self.assertEqual(
                self.rules(_item(file=not_a_file, line=1), index),
                ["anchor-file-missing"],
            )

    def test_paths_that_leave_the_workspace_are_never_read(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("outside the workspace\n", encoding="utf-8")
        for escape in ("../secret.txt", str(secret), "pkg/../../secret.txt"):
            with self.subTest(path=escape):
                self.assertEqual(
                    self.rules(_item(file=escape, line=1)), ["anchor-file-missing"]
                )
        link = self.workspace / "link.txt"
        try:
            link.symlink_to(secret)
        except OSError:
            self.skipTest("symbolic links are not available")
        self.assertEqual(
            self.rules(_item(file="link.txt", line=1)), ["anchor-file-missing"]
        )

    def test_absolute_path_inside_the_workspace_is_accepted(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        inside = os.path.join(self.workspace, "pkg", "mixed.py")
        self.assertEqual(self.check(_item(file=inside, line=2), index), [])
        self.assertEqual(self.check(_item(file="./pkg/mixed.py", line=2), index), [])

    def test_file_field_as_a_markdown_link_names_its_target(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        self.write("docs/release notes.md", "notes line\n")
        (self.root / "secret.txt").write_text("outside\n", encoding="utf-8")
        ws = self.workspace.as_posix()
        accepted = [
            # A file address with a base name as its label.
            f"[mixed.py](file://{ws}/pkg/mixed.py#L2)",
            f"[`mixed.py`](file://{ws}/pkg/mixed.py)",
            # A path relative to the workspace.
            "[pkg/mixed.py](pkg/mixed.py#L2)",
            # A web address, so the label names the file.
            "[pkg/mixed.py](https://example.invalid/blob/main/pkg/mixed.py#L2)",
            "[pkg/mixed.py](//example.invalid/blob/main/pkg/mixed.py)",
            # A code span that comes before a link.
            "`pkg/mixed.py` (see [the guide](https://example.invalid/guide))",
            # The first of two links.
            f"[mixed.py](file://{ws}/pkg/mixed.py) (and [nope.py](file://{ws}/nope.py))",
        ]
        for raw in accepted:
            with self.subTest(file=raw):
                item = _item(file=None, line=2, File=raw)
                self.assertEqual(self.check(item, index), [])
        encoded = f"[notes](file://{ws}/docs/release%20notes.md)"
        self.assertEqual(self.check(_item(file=None, line=1, File=encoded)), [])
        rejected = [
            # A web address with a label that is only a base name.
            "[mixed.py](https://example.invalid/blob/main/pkg/mixed.py)",
            # A file outside the workspace.
            f"[secret.txt](file://{self.root.as_posix()}/secret.txt)",
            # An address that cannot be parsed.
            "[mixed.py](file://[bad)",
        ]
        for raw in rejected:
            with self.subTest(file=raw):
                item = _item(file=None, line=1, File=raw)
                self.assertEqual(self.rules(item, index), ["anchor-file-missing"])

    def test_workspace_behind_a_symbolic_link_matches_absolute_paths(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        link = self.root / "ws_link"
        try:
            link.symlink_to(self.workspace, target_is_directory=True)
        except OSError:
            self.skipTest("symbolic links are not available")
        through_link = _item(file=str(link / "pkg" / "mixed.py"), line=2)
        direct = _item(file=str(self.workspace / "pkg" / "mixed.py"), line=2)
        cases = ((self.workspace, through_link), (link, direct), (link, through_link))
        for workspace, item in cases:
            with self.subTest(workspace=workspace.name, file=item.fields["File"]):
                found = rlc.check_grounding(
                    [item], workspace=workspace, diff_index=index
                )
                self.assertEqual(found, [])

    def test_line_past_the_end_is_out_of_bounds_only(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        (violation,) = self.check(_item(file="pkg/eof.py", line=40), index)
        self.assertEqual(violation.rule, "anchor-line-out-of-bounds")
        self.assertEqual(
            violation.message,
            "'H1' Line 40 is outside `pkg/eof.py`, which has lines 1-4.",
        )
        self.assertEqual(
            violation.fix_hint, rlc.GROUNDING_FIX_HINTS["anchor-line-out-of-bounds"]
        )
        self.assertEqual(
            self.rules(_item(file="pkg/eof.py", line=0), index),
            ["anchor-line-out-of-bounds"],
        )
        self.assertEqual(
            self.rules(_item(file="pkg/mixed.py", line="7-20"), index),
            ["anchor-line-out-of-bounds"],
        )

    def test_lines_are_counted_by_newlines_only(self) -> None:
        self.write("pkg/tail.py", "first line\nsecond line")
        self.write("pkg/paged.py", "page one\x0cpage two\nlast line\n")
        for file in ("pkg/tail.py", "pkg/paged.py"):
            with self.subTest(file=file):
                self.assertEqual(self.check(_item(file=file, line=2)), [])
                self.assertEqual(
                    self.rules(_item(file=file, line=3)), ["anchor-line-out-of-bounds"]
                )

    def test_unreadable_file_is_reported_as_missing(self) -> None:
        locked = self.write("pkg/locked.py", "locked line\n")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o644)
        if os.access(locked, os.R_OK):
            self.skipTest("file permissions are not enforced for this user")
        self.assertEqual(
            self.rules(_item(file="pkg/locked.py", line=1)), ["anchor-file-missing"]
        )

    def test_each_rule_needs_its_own_input(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        off_hunk = _item(file="pkg/mid.py", line=9)
        past_end = _item(file="pkg/eof.py", line=40)
        missing = _item(file="pkg/nope.py", line=1)
        self.assertEqual(rlc.check_grounding([off_hunk, past_end, missing]), [])
        workspace_only = rlc.check_grounding(
            [off_hunk, past_end, missing], workspace=self.workspace
        )
        self.assertEqual(
            [v.rule for v in workspace_only],
            ["anchor-line-out-of-bounds", "anchor-file-missing"],
        )
        diff_only = rlc.check_grounding([off_hunk, past_end, missing], diff_index=index)
        self.assertEqual([v.rule for v in diff_only], ["anchor-line-unchanged"] * 3)

    def test_items_without_a_file_or_line_are_not_anchored(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        self.assertEqual(self.check(_item(file=None, line=9), index), [])
        self.assertEqual(self.check(_item(file="pkg/mid.py", line=None), index), [])
        self.assertEqual(
            self.rules(_item(file="pkg/nope.py", line=None), index),
            ["anchor-file-missing"],
        )

    def test_file_field_markup_and_line_formats_are_tolerated(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        for line in ("L5", "5-6", "5\u20136", "5..6", "lines 5 to 6", "6-5"):
            with self.subTest(line=line):
                self.assertEqual(
                    self.check(_item(file="pkg/mid.py", line=line), index), []
                )
        marked_up = rlc.Item(
            "Q1", "Question", {"File": "`pkg/mid.py` (changed)", "Line": "`5`"}, 3
        )
        self.assertEqual(self.check(marked_up, index), [])

    def test_questions_and_findings_are_checked_in_order(self) -> None:
        index = self.stage_changes(LINE_CHANGES, 3)
        items = [
            _item("C1", "pkg/mid.py", 9, lineno=5),
            _item("M1", "pkg/mid.py", 5, lineno=15),
            _item("Q1", "pkg/nope.py", 1, lineno=25),
        ]
        violations = rlc.check_grounding(
            items, workspace=self.workspace, diff_index=index
        )
        self.assertEqual(
            [(v.line, v.rule) for v in violations],
            [(5, "anchor-line-unchanged"), (25, "anchor-file-missing")],
        )
        self.assertIn("'C1'", violations[0].message)
        self.assertIn("'Q1'", violations[1].message)


class GroundingQuoteTests(StagedDiffCase):
    """Covers the evidence-quote-mismatch rule of check_grounding."""

    def setUp(self) -> None:
        super().setUp()
        self.index = self.stage_changes(STORE_CHANGES, 3)
        self.write("pkg/routes.py", ROUTES)

    def quote_rules(self, evidence: str, **anchor: str | int | None) -> list[str]:
        anchor.setdefault("line", ROW_LINE)
        return self.rules(_item(evidence=evidence, **anchor), self.index)

    def test_quote_next_to_its_leading_citation_passes(self) -> None:
        self.assertEqual(
            self.quote_rules(
                f"`Read`: `pkg/store.py:{ROW_LINE}` {ROW_QUOTE} has no guard"
            ),
            [],
        )

    def test_drifted_quote_is_reported_with_its_citation(self) -> None:
        evidence = (
            f"`Read`: `pkg/store.py:{ROW_LINE}` "
            '"row = self.db.execute(QUERY, tenant).fetchone()"'
        )
        (violation,) = self.check(_item(evidence=evidence, line=ROW_LINE), self.index)
        self.assertEqual(violation.rule, "evidence-quote-mismatch")
        self.assertEqual(
            violation.message,
            "'H1' Evidence quotes \"row = self.db.execute(QUERY, tenant).fetchone()\", "
            f"but that text is not within 15 lines of `pkg/store.py:{ROW_LINE}`.",
        )
        self.assertEqual(
            violation.fix_hint, rlc.GROUNDING_FIX_HINTS["evidence-quote-mismatch"]
        )

    def test_trailing_citation_binds_a_quote_from_another_file(self) -> None:
        evidence = (
            "`Read`: \"return store.get_item(tenant, request.args['key'])\" "
            "(`pkg/routes.py:8`) reaches the unguarded read"
        )
        self.assertEqual(self.quote_rules(evidence), [])
        # The same quote without its citation is checked against File:Line only.
        uncited = "`Read`: \"return store.get_item(tenant, request.args['key'])\""
        self.assertEqual(self.quote_rules(uncited), ["evidence-quote-mismatch"])

    def test_two_sided_quotes_bind_to_their_own_files(self) -> None:
        evidence = (
            '`Read`: "cached = self.cache.get((tenant, key))" '
            f"(`pkg/store.py:{CACHED_LINE}`) vs "
            "\"return store.get_item(tenant, request.args['key'])\" (`pkg/routes.py:8`)"
        )
        self.assertEqual(self.quote_rules(evidence, line=CACHED_LINE), [])
        leading = (
            f'`Read`: `pkg/store.py:{CACHED_LINE}` "cached = self.cache.get((tenant, key))" '
            "whereas `pkg/routes.py:8` "
            "\"return store.get_item(tenant, request.args['key'])\""
        )
        self.assertEqual(self.quote_rules(leading, line=CACHED_LINE), [])
        one_wrong = evidence.replace("request.args['key']", "request.json['key']")
        (violation,) = self.check(
            _item(evidence=one_wrong, line=CACHED_LINE), self.index
        )
        self.assertIn("request.json['key']", violation.message)
        self.assertIn("`pkg/routes.py:8`", violation.message)

    def test_quote_of_deleted_lines_matches_the_diff(self) -> None:
        evidence = '`Read`: `pkg/store.py:15` removes "if row is None: return None"'
        self.assertEqual(self.quote_rules(evidence), [])
        without_diff = self.check(_item(evidence=evidence, line=ROW_LINE), None)
        self.assertEqual([v.rule for v in without_diff], ["evidence-quote-mismatch"])

    def test_quote_from_a_deleted_file_matches_the_diff(self) -> None:
        evidence = '`Read`: `pkg/legacy.py:2` "return db[key] # no tenant scoping"'
        self.assertEqual(self.quote_rules(evidence, file="pkg/legacy.py", line=2), [])
        cross_file = (
            f"`Read`: `pkg/store.py:{ROW_LINE}` {ROW_QUOTE} replaces "
            '"def legacy_lookup(db, key):" (`pkg/legacy.py:1`)'
        )
        self.assertEqual(self.quote_rules(cross_file), [])

    def test_ellipses_split_a_quote_into_separately_checked_parts(self) -> None:
        for mark in ("...", "\u2026", "[...]", " [\u2026] ", "....."):
            with self.subTest(mark=mark):
                evidence = (
                    f"`Read`: `pkg/store.py:{CACHED_LINE}` "
                    f'"cached = self.cache.get((tenant, key)){mark}return cached"'
                )
                self.assertEqual(self.quote_rules(evidence), [])
        fabricated_part = (
            f"`Read`: `pkg/store.py:{CACHED_LINE}` "
            '"cached = self.cache.get((tenant, key)) ... return cached or default"'
        )
        (violation,) = self.check(
            _item(evidence=fabricated_part, line=ROW_LINE), self.index
        )
        self.assertIn('"return cached or default"', violation.message)

    def test_whitespace_escapes_and_backticks_do_not_break_a_match(self) -> None:
        accepted = [
            # Two source lines joined on one line.
            '"if cached is not None: return cached"',
            # Line break written as an escape.
            '"if cached is not None:\\n            return cached"',
            # Inner quotation marks escaped.
            '"tenant = request.headers.get(\\"X-Tenant\\", \\"\\")" (`pkg/routes.py:7`)',
            # Code formatting added inside the quote.
            '"`cached` = `self.cache.get((tenant, key))`"',
            # Curly quotation marks.
            "\u201cif cached is not None: return cached\u201d",
            # A quotation that fills a code span.
            '`"if cached is not None: return cached"`',
            # A trimmed quote that ends with the reviewer's bracket and period.
            '"cached = self.cache.get((tenant, key)."',
        ]
        for snippet in accepted:
            with self.subTest(snippet=snippet):
                evidence = f"`Read`: `pkg/store.py:{CACHED_LINE}` {snippet}"
                self.assertEqual(self.quote_rules(evidence), [])

    def test_markdown_markup_and_comment_margins_do_not_break_a_match(self) -> None:
        self.write("docs/guide.md", GUIDE)
        self.write("pkg/graph.ts", GRAPH)
        self.write("pkg/hooks.py", HOOKS)
        item_with_sub_item = (
            "Always End Entries with a Link to the PR: Conclude each entry with "
            "a clickable link to its associated pull request: "
            "`[#<pr-number>](https://example.invalid/pull/<pr-number>)`"
        )
        accepted = [
            # Emphasis markers around and inside the quoted words.
            ("docs/guide.md", 3, "Always End Entries with a Link to the PR"),
            ("docs/guide.md", 7, "after raising the minor version"),
            # A list item joined to its sub-item, with a link kept as written.
            ("docs/guide.md", 3, item_with_sub_item),
            # A link quoted as its label, and the same line quoted as written.
            ("docs/guide.md", 6, "Run the validate-patterns skill's validate command"),
            (
                "docs/guide.md",
                6,
                "7. Run the [`validate-patterns`](../tools/validate.md)",
            ),
            # A block comment and runs of line comments quoted as flowing text.
            ("pkg/graph.ts", 2, "on a self-reference, a back edge, or a traversal"),
            ("pkg/graph.ts", 5, "which the orphan check reuses after the walk."),
            (
                "pkg/hooks.py",
                1,
                "git config, and any GIT_DIR exported by a calling hook",
            ),
        ]
        for file, line, quote in accepted:
            with self.subTest(quote=quote):
                evidence = f'`Read`: `{file}:{line}` "{quote}"'
                item = _item(file=file, line=line, evidence=evidence)
                self.assertEqual(self.rules(item), [])
        rejected = [
            ("docs/guide.md", 3, "Always End Entries with a Link to the Issue"),
            ("docs/guide.md", 3, "always end entries with a link to the PR"),
            ("pkg/graph.ts", 2, "a back edge, a self-reference, or a traversal"),
            # A dash inside a line is not a list marker.
            ("pkg/hooks.py", 4, "LIMIT = RETRIES 1"),
            # Only the label `a` would be left to compare, and "raising" holds "a is".
            ("docs/guide.md", 6, "[`a`](https://example.invalid/not-in-the-file) is"),
        ]
        for file, line, quote in rejected:
            with self.subTest(quote=quote):
                evidence = f'`Read`: `{file}:{line}` "{quote}"'
                item = _item(file=file, line=line, evidence=evidence)
                self.assertEqual(self.rules(item), ["evidence-quote-mismatch"])

    def test_wording_changes_inside_a_quote_are_reported(self) -> None:
        rejected = [
            '"if cached is None: return cached"',
            '"If Cached Is Not None: Return Cached"',
            '`"if cached is not None: return row"`',
            "\u201cif cached is not none: return cached\u201d",
        ]
        for snippet in rejected:
            with self.subTest(snippet=snippet):
                evidence = f"`Read`: `pkg/store.py:{CACHED_LINE}` {snippet}"
                self.assertEqual(
                    self.quote_rules(evidence), ["evidence-quote-mismatch"]
                )

    def test_names_values_and_short_snippets_are_not_quotations(self) -> None:
        ignored = [
            '"X-Tenant-Header-Name"',  # one token
            '`"SELECT_VALUE_FROM_ITEMS"`',  # one token in a code span
            '"no guard"',  # shorter than 12 characters
            "`cached = self.cache.fetch(tenant)`",  # code span without quotation marks
            "'cached = self.cache.fetch(tenant)'",  # single quotation marks
            'a 15" display and a 13" one sit side by side',  # inch marks
        ]
        for snippet in ignored:
            with self.subTest(snippet=snippet):
                evidence = f"`Read`: `pkg/store.py:{CACHED_LINE}` {snippet}"
                self.assertEqual(self.quote_rules(evidence), [])

    def test_only_read_evidence_is_checked(self) -> None:
        fabricated = '"this text is in no file at all"'
        self.assertEqual(
            self.quote_rules(f"`Read`: `pkg/store.py:{ROW_LINE}` {fabricated}"),
            ["evidence-quote-mismatch"],
        )
        for tier in ("Executed", "Fetched", "Deduction", "Speculation"):
            for label in (f"`{tier}`", tier):
                with self.subTest(label=label):
                    self.assertEqual(
                        self.quote_rules(f"{label}: prints {fabricated}"), []
                    )
        mixed = (
            f"`Read`: `pkg/store.py:{ROW_LINE}` {ROW_QUOTE}. "
            f"`Executed`: `pytest -k store` prints {fabricated}"
        )
        self.assertEqual(self.quote_rules(mixed), [])

    def test_quote_is_not_checked_when_a_cited_file_cannot_be_found(self) -> None:
        fabricated = '"this text is in no file at all"'
        for citation in (
            "`vendor/lib.py:12`",
            "`lib.py:12`",
            "spec:64",
            "`../secret.txt:1`",
        ):
            with self.subTest(citation=citation):
                self.assertEqual(
                    self.quote_rules(f"`Read`: {citation} {fabricated}"), []
                )
        # A path named without a line does not hide the quote.
        self.assertEqual(
            self.quote_rules(f"`Read`: unlike `vendor/lib.py`, {fabricated}"),
            ["evidence-quote-mismatch"],
        )

    def test_quote_outside_the_workspace_cannot_be_probed(self) -> None:
        (self.root / "secret.txt").write_text(
            "the secret token is hunter2\n", encoding="utf-8"
        )
        for guess in ('"the secret token is hunter2"', '"the secret token is wrong"'):
            with self.subTest(guess=guess):
                self.assertEqual(
                    self.quote_rules(f"`Read`: `../secret.txt:1` {guess}"), []
                )

    def test_base_name_citations_resolve_to_the_anchor_or_one_diff_file(self) -> None:
        self.assertEqual(
            self.quote_rules(f"`Read`: `store.py:{ROW_LINE}` {ROW_QUOTE}"), []
        )
        from_legacy = '`Read`: `legacy.py:1` "def legacy_lookup(db, key):"'
        self.assertEqual(self.quote_rules(from_legacy), [])
        drifted = '`Read`: `legacy.py:1` "def legacy_lookup(db, tenant, key):"'
        self.assertEqual(self.quote_rules(drifted), ["evidence-quote-mismatch"])
        # This file is not in the diff, so only the File field resolves its
        # base name. An invented quote shows that the citation was resolved.
        self.write("pkg/long.py", LONG_TABLE)
        for quote, expected in (
            ('"entry number 5 of the long table"', []),
            ('"an invented entry of the long table"', ["evidence-quote-mismatch"]),
        ):
            evidence = f"`Read`: `long.py:5` {quote}"
            item = _item(file="pkg/long.py", line=5, evidence=evidence)
            self.assertEqual(self.rules(item), expected)

    def test_ambiguous_base_name_resolves_only_through_the_anchor(self) -> None:
        index = self.stage_changes(
            {
                "pkg/a/legacy.py": (
                    "def legacy_a(db, key):\n    return db[key]\n",
                    None,
                ),
                "pkg/b/legacy.py": (
                    "def legacy_b(db, key):\n    return db.get(key)\n",
                    None,
                ),
            },
            0,
        )
        real = '`Read`: `legacy.py:1` "def legacy_a(db, key):"'
        invented = '`Read`: `legacy.py:1` "an invented line of legacy code"'

        def rules_from(file: str, evidence: str) -> list[str]:
            return self.rules(_item(file=file, line=1, evidence=evidence), index)

        self.assertEqual(rules_from("pkg/a/legacy.py", real), [])
        self.assertEqual(
            rules_from("pkg/a/legacy.py", invented), ["evidence-quote-mismatch"]
        )
        # Seen from another file, `legacy.py` could be either deleted file, so
        # the citation is unknown and the quote is not checked.
        self.assertNotIn(
            "evidence-quote-mismatch", rules_from("pkg/store.py", invented)
        )

    def test_citations_resolve_relative_to_directories_around_the_anchor(self) -> None:
        self.write("pkg/long.py", LONG_TABLE)
        self.write("pkg/deep/nested/leaf.py", "leaf line one\nleaf line two\n")
        self.write("pkg/deep/helpers.py", "def helper_for_the_leaf(): ...\n")
        found = []
        drifted = ["evidence-quote-mismatch"]
        leaf = "pkg/deep/nested/leaf.py"
        cases = [
            # A sibling of the anchor file, cited by base name.
            ("pkg/long.py", "routes.py:6", "def lookup(store: Store, request):", found),
            (
                "pkg/long.py",
                "routes.py:6",
                "def lookup(store, request, tenant):",
                drifted,
            ),
            # A file one and two directories above the anchor file.
            (leaf, "helpers.py:1", "def helper_for_the_leaf(): ...", found),
            (leaf, "deep/helpers.py:1", "def helper_for_the_leaf(): ...", found),
            (leaf, "helpers.py:1", "def helper_for_the_root(): ...", drifted),
        ]
        for file, citation, quote, expected in cases:
            with self.subTest(file=file, citation=citation, quote=quote):
                evidence = f'`Read`: `{citation}` "{quote}"'
                item = _item(file=file, line=1, evidence=evidence)
                self.assertEqual(self.rules(item), expected)

    def test_linked_citations_name_the_file_and_its_lines(self) -> None:
        self.write(
            "other/deep/handlers.py",
            "def first_handler(event): ...\ndef second_handler(event): ...\n",
        )
        target = f"file://{self.workspace.as_posix()}/other/deep/handlers.py"
        real = '"def second_handler(event): ..."'
        invented = '"def third_handler(event): ..."'
        found = [
            f"[handlers.py]({target}#L2) defines {real}",
            f"[handlers.py:2]({target}) defines {real}",
            f"[`handlers.py:1-2`]({target}) defines {real}",
            f"{real} ([handlers.py]({target}#L1-L2))",
        ]
        for citation in found:
            with self.subTest(citation=citation):
                self.assertEqual(self.quote_rules(f"`Read`: {citation}"), [])
        for label in ("handlers.py", "handlers.py:2"):
            with self.subTest(label=label):
                suffix = "" if ":" in label else "#L2"
                evidence = f"`Read`: [{label}]({target}{suffix}) defines {invented}"
                (violation,) = self.check(
                    _item(evidence=evidence, line=ROW_LINE), self.index
                )
                self.assertEqual(violation.rule, "evidence-quote-mismatch")
                self.assertIn("`other/deep/handlers.py:2`", violation.message)
        # A fragment that is not a line reference gives the link no lines, so
        # the quotation is looked for near File:Line only.
        heading = f"`Read`: [handlers.py]({target}#section-2) defines {real}"
        self.assertEqual(self.quote_rules(heading), ["evidence-quote-mismatch"])

    def test_quote_from_a_linked_file_outside_the_workspace_is_not_checked(
        self,
    ) -> None:
        (self.root / "notes.txt").write_text("words outside\n", encoding="utf-8")
        outside = f"file://{self.root.as_posix()}/notes.txt"
        for guess in ('"words outside the workspace"', '"words that are not there"'):
            with self.subTest(guess=guess):
                evidence = f"`Read`: [notes.txt]({outside}#L1) says {guess}"
                self.assertEqual(self.quote_rules(evidence), [])

    def test_link_to_a_web_address_is_cited_by_its_label(self) -> None:
        web = "https://example.invalid/blob/main/pkg/store.py"
        cited = f"`Read`: [pkg/store.py:{ROW_LINE}]({web}#L{ROW_LINE})"
        self.assertEqual(self.quote_rules(f"{cited} {ROW_QUOTE}"), [])
        self.assertEqual(
            self.quote_rules(f'{cited} "row = self.db.fetch(QUERY, tenant)"'),
            ["evidence-quote-mismatch"],
        )

    def test_bare_line_references_point_into_the_cited_files(self) -> None:
        self.write("pkg/long.py", LONG_TABLE)
        quote = '"entry number 50 of the long table"'

        def rules_for(evidence: str) -> list[str]:
            return self.rules(_item(file="pkg/long.py", line=5, evidence=evidence))

        references = (
            "line 50",
            "line `50`",
            "lines 48-52",
            "L50",
            "`:50`",
            "lines 3, 9 and 50",
        )
        for reference in references:
            with self.subTest(reference=reference):
                evidence = f"`Read`: `pkg/long.py:5` sets the header, and {reference} says {quote}"
                self.assertEqual(rules_for(evidence), [])
        self.assertEqual(rules_for(f"`Read`: line 50 says {quote}"), [])
        self.assertEqual(rules_for(f"`Read`: {quote}"), ["evidence-quote-mismatch"])

    def test_quote_must_be_within_fifteen_lines_of_a_citation(self) -> None:
        self.write("pkg/long.py", LONG_TABLE)

        def check_line(number: int) -> list[rlc.Violation]:
            evidence = (
                f'`Read`: `pkg/long.py:20` "entry number {number} of the long table"'
            )
            return self.check(_item(file="pkg/long.py", line=20, evidence=evidence))

        for near in (5, 20, 35):
            self.assertEqual(check_line(near), [], near)
        for far, shown in ((4, 4), (36, 36)):
            (violation,) = check_line(far)
            self.assertEqual(violation.rule, "evidence-quote-mismatch")
            self.assertEqual(
                violation.fix_hint,
                rlc.GROUNDING_FIX_HINTS["evidence-quote-mismatch"]
                + f" The text appears at `pkg/long.py:{shown}`.",
            )
        ranged = '`Read`: `pkg/long.py:20-30` "entry number 45 of the long table"'
        self.assertEqual(
            self.check(_item(file="pkg/long.py", line=20, evidence=ranged)), []
        )

    def test_file_named_without_a_line_is_not_read(self) -> None:
        self.write("pkg/archive.bin", "not worth reading\n")
        evidence = (
            f"`Read`: `pkg/store.py:{ROW_LINE}` {ROW_QUOTE}, unlike `pkg/archive.bin`"
        )
        original = Path.read_text
        with mock.patch.object(
            Path, "read_text", autospec=True, side_effect=original
        ) as read_text:
            self.assertEqual(self.quote_rules(evidence), [])
        read = {call.args[0].name for call in read_text.call_args_list}
        self.assertEqual(read, {"store.py"})

    def test_quotes_need_a_workspace(self) -> None:
        evidence = f'`Read`: `pkg/store.py:{ROW_LINE}` "this text is in no file"'
        fabricated = _item(line=ROW_LINE, evidence=evidence)
        self.assertEqual(rlc.check_grounding([fabricated], diff_index=self.index), [])
        self.assertEqual(
            self.rules(fabricated, self.index), ["evidence-quote-mismatch"]
        )

    def test_each_mismatched_quote_is_reported(self) -> None:
        evidence = (
            f'`Read`: `pkg/store.py:{ROW_LINE}` "first invented quote here" and '
            f'{ROW_QUOTE} and "second invented quote here"'
        )
        violations = self.check(_item(evidence=evidence, line=ROW_LINE), self.index)
        self.assertEqual([v.rule for v in violations], ["evidence-quote-mismatch"] * 2)
        self.assertIn("first invented quote here", violations[0].message)
        self.assertIn("second invented quote here", violations[1].message)

    def test_long_quote_is_shortened_in_the_message(self) -> None:
        long_quote = "an invented sentence that goes on " * 4
        evidence = f'`Read`: `pkg/store.py:{ROW_LINE}` "{long_quote.strip()}"'
        (violation,) = self.check(_item(evidence=evidence, line=ROW_LINE), self.index)
        self.assertIn(
            '"an invented sentence that goes on an invented sentence th..."',
            violation.message,
        )

    def test_missing_anchor_file_does_not_hide_cited_quotes(self) -> None:
        evidence = f'`Read`: `pkg/store.py:{ROW_LINE}` "this text is in no file at all"'
        rules = self.quote_rules(evidence, file="pkg/nope.py", line=1)
        self.assertEqual(rules, ["anchor-file-missing", "evidence-quote-mismatch"])


class WorkerPlaybookFeedbackTests(unittest.TestCase):
    """Covers Playbook Feedback handling for drafts and final reports."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.playbook = Path(tmp.name) / "deletion-and-consolidation.md"
        self.playbook.write_text(PLAYBOOK, encoding="utf-8")

    def rules(self, lines: list[tuple[int, str]] | None, **kwargs) -> list[str]:
        return [v.rule for v in rlc.check_worker_playbook_feedback(lines, **kwargs)]

    def test_final_report_must_not_carry_the_section(self) -> None:
        self.assertEqual(rlc.check_worker_playbook_feedback(None), [])
        for lines in ([], [(31, "None.")], [(31, "Failure modes checked: 1, 2")]):
            (violation,) = rlc.check_worker_playbook_feedback(lines, section_line=30)
            self.assertEqual(violation.rule, "playbook-feedback-leaked")
            self.assertEqual(violation.line, 30)
            self.assertEqual(violation.fix_hint, "")
            self.assertEqual(
                violation.message,
                "Playbook Feedback section must be stripped before writing the final report.",
            )

    def test_general_worker_may_omit_the_section(self) -> None:
        self.assertEqual(self.rules(None, worker_mode=True), [])
        self.assertEqual(self.rules([], worker_mode=True), [])
        self.assertEqual(self.rules([(31, ""), (32, "   ")], worker_mode=True), [])

    def test_focus_worker_with_a_catalog_playbook_must_report(self) -> None:
        for lines in (None, [], [(31, "  ")]):
            (violation,) = rlc.check_worker_playbook_feedback(
                lines, worker_mode=True, playbook_path=self.playbook, section_line=30
            )
            self.assertEqual(violation.rule, "playbook-feedback-missing")
            self.assertEqual(violation.line, 30)
            self.assertEqual(
                violation.fix_hint, rlc.GROUNDING_FIX_HINTS["playbook-feedback-missing"]
            )

    def test_catalog_modes_are_validated_against_the_playbook(self) -> None:
        valid = [(31, ""), (32, "Failure modes checked: 1, 2, 3"), (33, "- None.")]
        self.assertEqual(
            self.rules(valid, worker_mode=True, playbook_path=self.playbook), []
        )
        unknown = [(32, "Failure modes checked: 1, 4, 7, 9")]
        (violation,) = rlc.check_worker_playbook_feedback(
            unknown, worker_mode=True, playbook_path=self.playbook, section_line=30
        )
        self.assertEqual(violation.rule, "playbook-modes-invalid")
        self.assertEqual(violation.line, 32)
        self.assertEqual(
            violation.message,
            "'Failure modes checked:' lists 4, 7, 9, which "
            "`deletion-and-consolidation.md` does not define (it defines 1-3).",
        )
        self.assertEqual(
            violation.fix_hint, rlc.GROUNDING_FIX_HINTS["playbook-modes-invalid"]
        )

    def test_inline_ad_hoc_playbook_accepts_any_positive_numbers(self) -> None:
        # An `adhoc:<slug>` theme has no catalog file, so no playbook path is passed.
        for line in (
            "Failure modes checked: 1, 2",
            "Failure modes checked: 7",
            "Failure modes checked: 1-3 and 5.",
            "failure modes checked: 2 (stale reads), 4 (missing TTL)",
            "- **Failure modes checked**: 1, 2",
            "`Failure modes checked: 1, 2`",
        ):
            with self.subTest(line=line):
                self.assertEqual(self.rules([(40, line)], worker_mode=True), [])

    def test_modes_line_without_positive_numbers_is_invalid(self) -> None:
        for line in (
            "Failure modes checked: none",
            "Failure modes checked:",
            "Failure modes checked: 0, 1",
            "Failure modes checked: all of them (see 1, 2)",
        ):
            with self.subTest(line=line):
                (violation,) = rlc.check_worker_playbook_feedback(
                    [(40, line)], worker_mode=True
                )
                self.assertEqual(violation.rule, "playbook-modes-invalid")
                self.assertEqual(violation.line, 40)

    def test_section_that_does_not_open_with_the_modes_line_is_missing_it(self) -> None:
        for lines in (
            [(40, "None.")],
            [(40, "- The playbook lacks a mode for renamed exports.")],
            [
                (40, "Checked failure modes 1 and 2."),
                (41, "Failure modes checked: 1, 2"),
            ],
        ):
            (violation,) = rlc.check_worker_playbook_feedback(
                lines, worker_mode=True, playbook_path=self.playbook, section_line=39
            )
            self.assertEqual(violation.rule, "playbook-feedback-missing")
            self.assertEqual(violation.line, 40)
            self.assertEqual(
                violation.message,
                "Playbook Feedback does not open with 'Failure modes checked: <numbers>'.",
            )

    def test_ranges_expand_to_each_number(self) -> None:
        in_range = [(40, "Failure modes checked: 1-3")]
        self.assertEqual(
            self.rules(in_range, worker_mode=True, playbook_path=self.playbook), []
        )
        past_range = [(40, "Failure modes checked: 2-5")]
        (violation,) = rlc.check_worker_playbook_feedback(
            past_range, worker_mode=True, playbook_path=self.playbook
        )
        self.assertIn("lists 4, 5, which", violation.message)
        # A reversed or very long range counts as its two ends.
        reversed_range = [(40, "Failure modes checked: 3-1")]
        self.assertEqual(
            self.rules(reversed_range, worker_mode=True, playbook_path=self.playbook),
            [],
        )
        long_range = [(40, "Failure modes checked: 1-500")]
        (violation,) = rlc.check_worker_playbook_feedback(
            long_range, worker_mode=True, playbook_path=self.playbook
        )
        self.assertIn("lists 500, which", violation.message)

    def test_playbook_without_a_failure_modes_heading_uses_every_numbered_item(
        self,
    ) -> None:
        self.playbook.write_text(
            "#### Ad hoc playbook\n\n1. First mode.\n2. Second mode.\n",
            encoding="utf-8",
        )
        ok = [(40, "Failure modes checked: 1, 2")]
        self.assertEqual(
            self.rules(ok, worker_mode=True, playbook_path=self.playbook), []
        )
        bad = [(40, "Failure modes checked: 3")]
        self.assertEqual(
            self.rules(bad, worker_mode=True, playbook_path=self.playbook),
            ["playbook-modes-invalid"],
        )

    def test_playbook_without_numbered_modes_accepts_positive_numbers(self) -> None:
        self.playbook.write_text(
            "# Notes\n\nNo numbered modes here.\n", encoding="utf-8"
        )
        lines = [(40, "Failure modes checked: 1, 2")]
        self.assertEqual(
            self.rules(lines, worker_mode=True, playbook_path=self.playbook), []
        )

    def test_unreadable_playbook_raises(self) -> None:
        lines = [(40, "Failure modes checked: 1")]
        with self.assertRaises(OSError):
            rlc.check_worker_playbook_feedback(
                lines,
                worker_mode=True,
                playbook_path=self.playbook.with_name("absent.md"),
            )


def _finding(
    item_id: str,
    file: str,
    line: str | int,
    body: str,
    title: str = "",
    why: str = "",
    lineno: int = 1,
) -> rlc.Item:
    fields = {"File": f"`{file}`", "Line": f"`{line}`", "Body": body, "Why": why}
    return rlc.Item(item_id, title, fields, lineno)


def _shape(clusters: list[dict]) -> list[list[tuple[str, str]]]:
    """Reduces clusters to their `(reviewer, id)` members for comparison."""
    return [
        [(member["reviewer"], member["id"]) for member in cluster["members"]]
        for cluster in clusters
    ]


class ClusterFindingsTests(unittest.TestCase):
    """Covers same-file grouping of findings from several reviewer reports."""

    def test_similar_findings_on_nearby_lines_of_one_file_form_a_cluster(self) -> None:
        first = _finding(
            "H1",
            "pkg/store.py",
            17,
            "`get_item` indexes the query row without checking for `None`.",
            title="Missing null guard on lookup",
            why="Missing keys raise `TypeError`.",
            lineno=12,
        )
        second = _finding(
            "M1",
            "./pkg/store.py",
            "15-18",
            "The query row is indexed without a `None` check, so a missing key raises `TypeError`.",
            title="get_item crashes on a missing key",
            lineno=30,
        )
        other = _finding(
            "L1", "pkg/store.py", 3, "Import order differs from the style guide."
        )
        clusters = rlc.cluster_findings(
            [("rev-a", [first, other]), ("rev-b", [second])]
        )
        self.assertEqual(
            clusters,
            [
                {
                    "cluster_id": 1,
                    "file": "pkg/store.py",
                    "line_start": 15,
                    "line_end": 18,
                    "size": 2,
                    "reviewers": ["rev-a", "rev-b"],
                    "members": [
                        {
                            "reviewer": "rev-a",
                            "id": "H1",
                            "title": "Missing null guard on lookup",
                            "severity": "high",
                            "line_start": 17,
                            "line_end": 17,
                            "report_line": 12,
                        },
                        {
                            "reviewer": "rev-b",
                            "id": "M1",
                            "title": "get_item crashes on a missing key",
                            "severity": "medium",
                            "line_start": 15,
                            "line_end": 18,
                            "report_line": 30,
                        },
                    ],
                },
                {
                    "cluster_id": 2,
                    "file": "pkg/store.py",
                    "line_start": 3,
                    "line_end": 3,
                    "size": 1,
                    "reviewers": ["rev-a"],
                    "members": [
                        {
                            "reviewer": "rev-a",
                            "id": "L1",
                            "title": "",
                            "severity": "low",
                            "line_start": 3,
                            "line_end": 3,
                            "report_line": 1,
                        }
                    ],
                },
            ],
        )
        self.assertEqual(json.loads(json.dumps(clusters)), clusters)

    def test_identical_wording_in_different_files_stays_separate(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        clusters = rlc.cluster_findings(
            [
                ("rev-a", [_finding("H1", "a.py", 10, body)]),
                ("rev-b", [_finding("H1", "b.py", 10, body)]),
            ]
        )
        self.assertEqual(_shape(clusters), [[("rev-a", "H1")], [("rev-b", "H1")]])
        self.assertEqual([cluster["file"] for cluster in clusters], ["a.py", "b.py"])

    def test_line_window_is_inclusive(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."

        def clusters_at(gap: int, **kwargs) -> list[dict]:
            return rlc.cluster_findings(
                [
                    ("rev-a", [_finding("H1", "a.py", "10-12", body)]),
                    ("rev-b", [_finding("H1", "a.py", 12 + gap, body)]),
                ],
                **kwargs,
            )

        self.assertEqual(len(clusters_at(15)), 1)
        self.assertEqual(len(clusters_at(16)), 2)
        self.assertEqual(len(clusters_at(16, line_window=16)), 1)
        self.assertEqual(len(clusters_at(0)), 1)
        # A range that starts before the other finding is as close as its end.
        before = rlc.cluster_findings(
            [
                ("rev-a", [_finding("H1", "a.py", "40-60", body)]),
                ("rev-b", [_finding("H1", "a.py", 25, body)]),
            ]
        )
        self.assertEqual(len(before), 1)

    def test_wording_threshold_is_inclusive(self) -> None:
        def clusters_for(other_body: str, **kwargs) -> list[dict]:
            return rlc.cluster_findings(
                [
                    (
                        "rev-a",
                        [_finding("H1", "a.py", 10, "alpha bravo charlie delta echo")],
                    ),
                    ("rev-b", [_finding("H1", "a.py", 10, other_body)]),
                ],
                **kwargs,
            )

        # 2 shared tokens of 8 distinct tokens is exactly 0.25.
        self.assertEqual(len(clusters_for("alpha bravo foxtrot golf hotel")), 1)
        # 2 shared tokens of 9 distinct tokens is below 0.25.
        self.assertEqual(len(clusters_for("alpha bravo foxtrot golf hotel india")), 2)
        self.assertEqual(
            len(clusters_for("alpha bravo foxtrot golf hotel india", min_jaccard=0.2)),
            1,
        )

    def test_common_words_and_short_tokens_do_not_count_as_shared_wording(self) -> None:
        # The two bodies share "this", "should", "not", "be", "the", and "to".
        clusters = rlc.cluster_findings(
            [
                (
                    "rev-a",
                    [
                        _finding(
                            "H1",
                            "a.py",
                            10,
                            "This buffer should not be the cache to use.",
                        )
                    ],
                ),
                (
                    "rev-b",
                    [
                        _finding(
                            "H1",
                            "a.py",
                            10,
                            "This socket should not be the pipe to open.",
                        )
                    ],
                ),
            ]
        )
        self.assertEqual(len(clusters), 2)
        only_common = rlc.cluster_findings(
            [
                (
                    "rev-a",
                    [
                        _finding(
                            "H1", "a.py", 10, "This is the it that should not be so."
                        )
                    ],
                ),
                (
                    "rev-b",
                    [
                        _finding(
                            "H1", "a.py", 10, "This is the it that should not be so."
                        )
                    ],
                ),
            ]
        )
        self.assertEqual(len(only_common), 2)

    def test_findings_from_one_report_are_never_linked_directly(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        clusters = rlc.cluster_findings(
            [
                (
                    "rev-a",
                    [
                        _finding("H1", "a.py", 10, body),
                        _finding("H2", "a.py", 11, body),
                    ],
                )
            ]
        )
        self.assertEqual(_shape(clusters), [[("rev-a", "H1")], [("rev-a", "H2")]])

    def test_links_chain_across_reports(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        clusters = rlc.cluster_findings(
            [
                ("rev-a", [_finding("M1", "a.py", 10, body)]),
                (
                    "rev-b",
                    [
                        _finding("M1", "b.py", 10, body),
                        _finding("M2", "a.py", 22, body),
                    ],
                ),
                ("rev-c", [_finding("M1", "a.py", 34, body)]),
            ]
        )
        self.assertEqual(
            _shape(clusters),
            [
                [("rev-a", "M1"), ("rev-b", "M2"), ("rev-c", "M1")],
                [("rev-b", "M1")],
            ],
        )
        self.assertEqual([cluster["cluster_id"] for cluster in clusters], [1, 2])
        self.assertEqual((clusters[0]["line_start"], clusters[0]["line_end"]), (10, 34))
        self.assertEqual(clusters[0]["reviewers"], ["rev-a", "rev-b", "rev-c"])

    def test_finding_that_matches_two_separate_findings_joins_them(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        clusters = rlc.cluster_findings(
            [
                ("rev-a", [_finding("M1", "a.py", 10, body)]),
                ("rev-b", [_finding("M1", "a.py", 40, body)]),
                ("rev-c", [_finding("M1", "a.py", 25, body)]),
            ]
        )
        self.assertEqual(
            _shape(clusters), [[("rev-a", "M1"), ("rev-b", "M1"), ("rev-c", "M1")]]
        )

    def test_absolute_and_relative_forms_of_one_file_match(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        relative = _finding("M1", "pkg/store.py", 17, body)
        linked = rlc.Item(
            "M1",
            "",
            {
                "File": "[store.py](file:///tmp/pr-review-b/pkg/store.py#L17)",
                "Line": "`17`",
                "Body": body,
            },
            1,
        )
        for first, second in ((relative, linked), (linked, relative)):
            (cluster,) = rlc.cluster_findings([("rev-a", [first]), ("rev-b", [second])])
            self.assertEqual(cluster["size"], 2)
            self.assertEqual(cluster["file"], "pkg/store.py")
        # Another directory, and two relative paths that differ, stay apart.
        other_dir = _finding("M1", "lib/store.py", 17, body)
        base_name = _finding("M2", "store.py", 17, body)
        apart = ((linked, other_dir), (relative, base_name), (base_name, relative))
        for first, second in apart:
            clusters = rlc.cluster_findings([("rev-a", [first]), ("rev-b", [second])])
            self.assertEqual([cluster["size"] for cluster in clusters], [1, 1])
        (alone,) = rlc.cluster_findings([("rev-a", [linked])])
        self.assertEqual(alone["file"], "/tmp/pr-review-b/pkg/store.py")

    def test_findings_without_a_location_stay_alone(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        no_file = rlc.Item("H1", "", {"Body": body}, 4)
        no_line = rlc.Item("H1", "", {"File": "`a.py`", "Body": body}, 4)
        line_only = rlc.Item("H1", "", {"Line": "`4`", "Body": body}, 4)
        items = [no_file, no_line, line_only]
        clusters = rlc.cluster_findings([("rev-a", items), ("rev-b", items)])
        self.assertEqual([cluster["size"] for cluster in clusters], [1] * 6)
        self.assertEqual(
            [(c["file"], c["line_start"], c["line_end"]) for c in clusters],
            [("", None, None), ("a.py", None, None), ("", 4, 4)] * 2,
        )

    def test_severity_comes_from_the_field_or_the_id(self) -> None:
        body = "The retry loop never resets its backoff counter after a success."
        stated = rlc.Item(
            "M1", "", {"File": "`a.py`", "Line": "`1`", "Severity": "`critical`"}, 1
        )
        question = SimpleNamespace(
            id="Q1",
            title="Is the backoff reset?",
            line=7,
            fields={"File": "`a.py`", "Line": "`9`", "Why": body},
        )
        clusters = rlc.cluster_findings([("rev-a", [stated, question])])
        self.assertEqual(
            [cluster["members"][0]["severity"] for cluster in clusters],
            ["critical", "question"],
        )
        self.assertEqual(clusters[1]["members"][0]["report_line"], 7)

    def test_no_reports_give_no_clusters(self) -> None:
        self.assertEqual(rlc.cluster_findings([]), [])
        self.assertEqual(rlc.cluster_findings([("rev-a", [])]), [])


class GroundingFixHintTests(unittest.TestCase):
    """Covers the exported fix hints for the grounding and worker rules."""

    def test_hints_cover_every_new_rule(self) -> None:
        self.assertEqual(
            set(rlc.GROUNDING_FIX_HINTS),
            {
                "anchor-file-missing",
                "anchor-line-out-of-bounds",
                "anchor-line-unchanged",
                "evidence-quote-mismatch",
                "playbook-feedback-missing",
                "playbook-modes-invalid",
            },
        )
        for rule, hint in rlc.GROUNDING_FIX_HINTS.items():
            self.assertTrue(hint.endswith("."), rule)
            self.assertNotIn("\n", hint)


if __name__ == "__main__":
    unittest.main()
