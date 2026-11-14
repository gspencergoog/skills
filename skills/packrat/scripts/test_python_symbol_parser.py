"""Unit tests for the in-process Python stdlib `ast` symbol parser (`py-ast`)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from parsers.python.symbol_parser import PYTHON_PARSER_ID, parse_python
from parsers.stamps import host_platform, read_stamp, source_sha256

FIXTURES_DIR = Path(__file__).resolve().parent / "parsers" / "python" / "fixtures"


class TestPythonSymbolParser(unittest.TestCase):
    """Verifies `parse_python` against golden declarations, docstrings, and errors."""

    def test_golden_sample_symbols(self) -> None:
        sample = FIXTURES_DIR / "sample_symbols.py"
        text = sample.read_text(encoding="utf-8")
        parsed = parse_python("sample_symbols.py", text)

        self.assertEqual(parsed.parser_id, PYTHON_PARSER_ID)
        self.assertEqual(parsed.syntax_error_count, 0)
        self.assertEqual(parsed.dropped_symbol_count, 0)

        actual = [
            (
                s.kind,
                s.name,
                s.qualified_name,
                s.parent,
                s.visibility,
                s.docstring,
            )
            for s in parsed.symbols
        ]
        self.assertEqual(
            actual,
            [
                ("variable", "DEFAULT_LIMIT", "DEFAULT_LIMIT", "", "public", ""),
                ("variable", "_internal_flag", "_internal_flag", "", "private", ""),
                ("typedef", "OldStyleAlias", "OldStyleAlias", "", "public", ""),
                ("typedef", "ModernAlias", "ModernAlias", "", "public", ""),
                ("enum", "Color", "Color", "", "public", "Supported widget colors."),
                ("enum_value", "RED", "Color.RED", "Color", "public", ""),
                ("enum_value", "GREEN", "Color.GREEN", "Color", "public", ""),
                (
                    "class",
                    "BaseWidget",
                    "BaseWidget",
                    "",
                    "public",
                    "Base UI widget with lifecycle hooks.",
                ),
                ("field", "count", "BaseWidget.count", "BaseWidget", "public", ""),
                (
                    "field",
                    "_secret_tag",
                    "BaseWidget._secret_tag",
                    "BaseWidget",
                    "private",
                    "",
                ),
                (
                    "method",
                    "__init__",
                    "BaseWidget.__init__",
                    "BaseWidget",
                    "public",
                    "Initialize the widget with a label.",
                ),
                (
                    "getter",
                    "label",
                    "BaseWidget.label",
                    "BaseWidget",
                    "public",
                    "Current widget label.",
                ),
                ("setter", "label", "BaseWidget.label", "BaseWidget", "public", ""),
                (
                    "method",
                    "_internal_helper",
                    "BaseWidget._internal_helper",
                    "BaseWidget",
                    "private",
                    "",
                ),
                (
                    "function",
                    "render_all",
                    "render_all",
                    "",
                    "public",
                    "Render every widget and return total count.",
                ),
            ],
        )

        names = {s.name for s in parsed.symbols}
        self.assertNotIn("nested_closure", names)
        self.assertNotIn("local_only", names)

        render_sym = [s for s in parsed.symbols if s.name == "render_all"][0]
        self.assertEqual(
            render_sym.signature,
            "def render_all( items: list[BaseWidget], *, strict: bool = False, ) -> int",
        )
        prop_sym = [s for s in parsed.symbols if s.kind == "getter"][0]
        self.assertEqual(text.splitlines()[prop_sym.line_start - 1].strip(), "@property")

    def test_form_feed_line_numbers_use_lf_only(self) -> None:
        text = "x = 1\f# still line 1\ndef after_ff():\n    return 2\n"
        parsed = parse_python("ff.py", text)
        fn = [s for s in parsed.symbols if s.name == "after_ff"][0]
        self.assertEqual(fn.line_start, 2)
        self.assertEqual(fn.line_end, 3)

    def test_syntax_error_reports_per_file_error(self) -> None:
        parsed = parse_python("broken.py", "def broken(:\n    pass\n")
        self.assertEqual(parsed.syntax_error_count, 1)
        self.assertEqual(parsed.symbols, ())

    def test_python_stamp_matches_sources(self) -> None:
        pdir = Path(__file__).resolve().parent / "parsers" / "python"
        stamp = read_stamp(pdir)
        self.assertIsNotNone(stamp)
        assert stamp is not None
        self.assertEqual(stamp.parser, "py-ast")
        self.assertEqual(stamp.platforms["any"].source_sha256, source_sha256(pdir))
        self.assertTrue(host_platform())


if __name__ == "__main__":
    unittest.main()
