"""Unit tests for lint_design_review.py."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import lint_design_review as ldr

VALID_REPORT_WITH_NA = """# Design Review: Store v2

## Summary
Reviewed the [store_v2.md](file:///tmp/store_v2.md) specification against repository contracts.
Citation audit: 2 checked, 0 dropped.
Overall Score: 49/60
Verdict: Revisions Recommended

## Principle Scorecard
| Principle | Score | Assessment |
| :--- | :---: | :--- |
| Problem Isolation (`problem-scope`) | N/A | Audited an existing code module with no standalone problem doc. |
| Module Depth (`module-depth`) | 8/10 | StoreWriter hides batching behind two clean methods. |
| Boundaries & State (`boundaries-state`) | 7/10 | Acyclic imports, though cache invalidation crosses two modules. |
| Tradeoffs & Simplicity (`tradeoffs-simplicity`) | 9/10 | Clear WAL choice over dual-write complexity. |
| Resilience & Ops (`resilience-ops`) | 6/10 | Missing retry bound when SQLite returns SQLITE_BUSY. |
| Evolution & Fitness (`evolution-fitness`) | 9/10 | Schema version header isolates future format changes. |
| Ergonomics & Coherence (`ergonomics-coherence`) | 10/10 | Diagrams and type signatures match on disk. |

## View & Boundary Assessment
- Module view matches `pkg/store` imports with zero cycles.
- One-Way Doors: SQLite `orders_v2` schema header.

## Findings (Ordered by Severity)

### H1. Unbounded retry loop on SQLITE_BUSY stalls writer callers
- **Target**: `pkg/store/writer.py:44`
- **Door**: `two-way`
- **Severity**: `high`
- **Principle**: `resilience-ops`
- **Focus**: `distributed-state-and-consistency`
- **Evidence**: `Read`: `"while True: conn.execute(sql)" (pkg/store/writer.py:44)`
- **Body**: `StoreWriter.flush` retries `SQLITE_BUSY` forever with no timeout or backoff.
- **Why**: A wedged reader lock blocks every caller thread indefinitely in production.
- **Suggestion**: Bound retries with `busy_timeout` and raise `StoreBusyError` after 3 attempts:
```python
conn.execute("PRAGMA busy_timeout = 2000")
```
- **Details**: Callers in `sync_worker.py:88` already catch `StoreBusyError` and requeue work cleanly.

### M1. Cache key layout is duplicated across reader and writer
- **Target**: `pkg/store/reader.py:19`
- **Door**: `two-way`
- **Severity**: `medium`
- **Principle**: `module-depth`
- **Evidence**: `Read`: `"key = f'{tenant}:{epoch}'" (pkg/store/reader.py:19)` and `pkg/store/writer.py:31`
- **Body**: Both modules format tenant cache keys by hand with string interpolation.
- **Why**: Changing the key delimiter in one file silently causes 100% cache misses.
- **Suggestion**: Move key formatting into a single `format_store_key(tenant, epoch)` helper in `pkg/store/keys.py`.

## Open Design Questions

### Q1. Can two CLI processes open the same WAL file over NFS?
- **Target**: `pkg/store/writer.py:12`
- **Evidence**: `Speculation`: Checked local callers; deployment mount type is not documented in repo configs.
- **Why**: SQLite shared-memory WAL locks corrupt databases on network filesystems (`critical` if true).
- **To settle**: Check deployment volume mounts or enforce local-disk path validation at startup.

## Checked and Found Clean
- Import cycle between reader and writer: `Executed`: `prepare_design_context.py` reported 0 Python import cycles.
- Settled WAL format choice (D1): `Read`: `pkg/store/schema.sql:4` matches single-host constraints.

## Recommended Next Steps
1. Bound `SQLITE_BUSY` retries in `writer.py` before rollout.
2. Extract `format_store_key` into `keys.py`.
"""


class LintDesignReviewTests(unittest.TestCase):
    """Tests structural validation, severity caps, prose gates, and genui JSON export."""

    def test_valid_report_passes_and_exports_numeric_only_scorecard(self) -> None:
        """Validates a full report with N/A principle and checks genui JSON contract."""
        parsed = ldr.parse_design_review(VALID_REPORT_WITH_NA)
        violations = ldr.check_design_review(parsed)
        self.assertEqual(violations, [])

        with tempfile.TemporaryDirectory() as tmp:
            out_json = Path(tmp) / "design_review_results.json"
            payload = ldr.export_design_review_json(parsed, out_json)
            self.assertTrue(out_json.is_file())
            self.assertEqual(payload["summary"]["verdict"], "Revisions Recommended")
            self.assertEqual(
                payload["summary"]["citation_audit"], {"checked": 2, "dropped": 0}
            )
            self.assertNotIn("file://", payload["summary"]["overview"])
            # F6: N/A rows excluded from scorecard.rows[], numeric scores only!
            self.assertEqual(payload["scorecard"]["na_principles"], ["problem-scope"])
            self.assertEqual(payload["scorecard"]["overall"], {"score": 49, "max": 60})
            self.assertEqual(len(payload["scorecard"]["rows"]), 6)
            for row in payload["scorecard"]["rows"]:
                self.assertIsInstance(row["score"], int)
            self.assertEqual(
                payload["findings"][0]["triage_item"]["bucket"], ["Revise design"]
            )
            self.assertEqual(len(payload["questions"]), 1)

    def test_catches_severity_cap_recalled_and_scorecard_math(self) -> None:
        """Flags Speculation on high severity, Recalled tier, and mismatched Overall Score."""
        bad = (
            VALID_REPORT_WITH_NA.replace(
                "Overall Score: 49/60",
                "Overall Score: 55/70",
            )
            .replace(
                '- **Evidence**: `Read`: `"while True: conn.execute(sql)" (pkg/store/writer.py:44)`',
                "- **Evidence**: `Speculation`: Probably loops forever.",
            )
            .replace(
                "- **Evidence**: `Read`: `\"key = f'{tenant}:{epoch}'\" (pkg/store/reader.py:19)` and `pkg/store/writer.py:31`",
                "- **Evidence**: `Recalled`: I remember this pattern.",
            )
        )
        violations = ldr.check_design_review(ldr.parse_design_review(bad))
        rules = {v.rule for v in violations}
        self.assertIn("scorecard-math-mismatch", rules)
        self.assertIn("severity-cap", rules)
        self.assertIn("recalled-forbidden", rules)

    def test_catches_prose_ai_isms_em_dashes_and_multi_fence(self) -> None:
        """Flags banned AI words, em-dashes, and multiple Suggestion code fences."""
        bad_prose = VALID_REPORT_WITH_NA.replace(
            "- **Body**: `StoreWriter.flush` retries `SQLITE_BUSY` forever with no timeout or backoff.",
            "- **Body**: This robust module seamlessly leverages retries — causing stalls.",
        ).replace(
            'conn.execute("PRAGMA busy_timeout = 2000")\n```',
            'conn.execute("PRAGMA busy_timeout = 2000")\n```\nAnd:\n```python\npass\n```',
        )
        violations = ldr.check_design_review(ldr.parse_design_review(bad_prose))
        rules = {v.rule for v in violations}
        self.assertIn("comment-ai-ism", rules)
        self.assertIn("suggestion-single-fence", rules)

    def test_catches_section_order_playbook_leak_and_bad_ids(self) -> None:
        """Flags leaked Playbook Feedback, contradictory verdict, duplicate row, and bad IDs."""
        bad_struct = (
            VALID_REPORT_WITH_NA.replace(
                "Verdict: Revisions Recommended", "Verdict: Ready for /plan"
            )
            .replace(
                "| Ergonomics & Coherence (`ergonomics-coherence`) | 10/10 | Diagrams and type signatures match on disk. |",
                "| Ergonomics & Coherence (`ergonomics-coherence`) | 10/10 | Diagrams and type signatures match on disk. |\n"
                "| Ergonomics & Coherence (`ergonomics-coherence`) | 10/10 | Duplicate row. |",
            )
            .replace("### H1.", "### H2.")
            .replace(
                "- Import cycle between reader and writer: `Executed`: `prepare_design_context.py` reported 0 Python import cycles.",
                "- Looks clean with no tier.",
            )
            + "\n## Playbook Feedback\nFailure modes checked: 1, 2\n"
        )
        violations = ldr.check_design_review(ldr.parse_design_review(bad_struct))
        rules = {v.rule for v in violations}
        self.assertIn("playbook-feedback-leaked", rules)
        self.assertIn("verdict-contradicts-findings", rules)
        self.assertIn("scorecard-duplicate-principle", rules)
        self.assertIn("finding-id-sequence", rules)
        self.assertIn("clean-format", rules)

    def test_cli_main_exit_codes(self) -> None:
        """Verifies CLI exit codes 0 (valid), 1 (violations), and 2 (missing file/args)."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ok_md = root / "ok.md"
            ok_md.write_text(VALID_REPORT_WITH_NA, encoding="utf-8")
            out_json = root / "out.json"
            self.assertEqual(
                ldr.main([str(ok_md), "--export-json", str(out_json), "--post-design"]),
                0,
            )
            self.assertTrue(out_json.is_file())

            bad_md = root / "bad.md"
            bad_md.write_text("# Broken\n\n## Summary\nNo verdict.\n", encoding="utf-8")
            self.assertEqual(ldr.main([str(bad_md)]), 1)
            self.assertEqual(ldr.main([str(root / "missing.md")]), 2)
            self.assertEqual(ldr.main([]), 2)


if __name__ == "__main__":
    unittest.main()
