#!/usr/bin/env python3
"""Unit tests for analyze_prose.py."""

import pathlib
import tempfile
import unittest

from analyze_prose import analyze_prose


class AnalyzeProseTests(unittest.TestCase):

    def _run_on_text(self, content: str) -> dict:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", encoding="utf-8", delete=False
        ) as tmp:
            tmp.write(content)
            tmp_path = pathlib.Path(tmp.name)
        try:
            return analyze_prose(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_clean_prose_has_no_violations(self):
        report = self._run_on_text(
            "## Summary\n\n"
            "The client retries failed HTTP 503 requests up to three times.\n\n"
            "- Reuses open sockets across requests.\n"
            "- Cancels requests that take longer than five seconds.\n"
            "- Logs timeout errors with the request ID.\n"
        )
        v = report["violations"]
        self.assertEqual(v["banned_ai_words_found"], [])
        self.assertEqual(v["banned_ai_phrases_found"], [])
        self.assertEqual(v["em_dashes_found"], [])
        self.assertEqual(v["smart_quotes_found"], [])
        self.assertFalse(v["inline_header_list_overuse"]["flagged"])
        self.assertEqual(v["sentences_exceeding_25_words"], [])
        self.assertEqual(v["paragraphs_exceeding_4_sentences"], [])

    def test_detects_em_dashes_outside_code_spans(self):
        report = self._run_on_text(
            "---\n"
            "The cache stores tokens in memory\u2014reducing database load.\n"
            "It also handles retries--without blocking the caller.\n"
            "Spaced stand-in -- is also flagged.\n"
            "Literal code span `\u2014` or `--` and <!-- html comment --> are ignored.\n"
            "```python\n"
            "x = '\u2014 --'\n"
            "```\n"
        )
        em_dashes = report["violations"]["em_dashes_found"]
        self.assertEqual([m["line"] for m in em_dashes], [2, 3, 4])

    def test_detects_inline_header_list_overuse(self):
        report = self._run_on_text(
            "- **Pooling**: Reuses open sockets.\n"
            "- **Retries**: Retries failed HTTP 503 requests.\n"
            "- **Timeouts**: Cancels slow requests.\n"
            "- Plain item without bold header.\n"
        )
        overuse = report["violations"]["inline_header_list_overuse"]
        self.assertTrue(overuse["flagged"])
        self.assertEqual(overuse["inline_header_count"], 3)
        self.assertEqual(overuse["total_bullets"], 4)
        self.assertEqual(overuse["ratio"], 0.75)

    def test_detects_banned_words_phrases_and_smart_quotes(self):
        report = self._run_on_text(
            "This module serves as a robust, seamless hub, highlighting the \u201cnew\u201d design.\n"
        )
        v = report["violations"]
        words = {m["banned_word"] for m in v["banned_ai_words_found"]}
        phrases = {m["banned_phrase"] for m in v["banned_ai_phrases_found"]}
        self.assertIn("robust", words)
        self.assertIn("seamless", words)
        self.assertIn("serves as", phrases)
        self.assertIn("dangling -ing participle commentary", phrases)
        self.assertEqual(len(v["smart_quotes_found"]), 1)


if __name__ == "__main__":
    unittest.main()
