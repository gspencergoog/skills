#!/usr/bin/env python3
"""
Unit and Integration tests for Python Cognitive Complexity Analyzer.
Verifies the 15 SonarSource compliance benchmarks + edge cases + CLI + formatters.
"""

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Add script directory to sys.path
sys.path.insert(0, str(Path(__file__).parent))

from cognitive_complexity import (
    ComplexityIncrement,
    ComplexityReport,
    FileComplexity,
    FunctionComplexity,
    PythonComplexityAnalyzer,
    analyze_paths,
    format_summary_report,
    format_table_report,
    format_text_report,
    main,
)


class TestPythonCognitiveComplexity(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = PythonComplexityAnalyzer(threshold=15)

    def _get_complexity(self, code: str, func_name: str) -> int:
        file_comp = self.analyzer.analyze_source(code, file_path="test.py")
        for f in file_comp.functions:
            if f.name == func_name:
                return f.complexity
        raise AssertionError(f"Function {func_name} not found in {[f.name for f in file_comp.functions]}")

    # TC01: Flat linear code -> 0
    def test_tc01_linear_code(self) -> None:
        code = """
def linear_function(a, b):
    x = a + b
    y = x * 2
    return y
"""
        self.assertEqual(self._get_complexity(code, "linear_function"), 0)

    # TC02: Single if statement -> 1
    def test_tc02_single_if(self) -> None:
        code = """
def single_if(x):
    if x > 0:
        return x
    return -x
"""
        self.assertEqual(self._get_complexity(code, "single_if"), 1)

    # TC03: Nested if inside for loop -> 3 (Loop +1, If +2)
    def test_tc03_nested_if_in_loop(self) -> None:
        code = """
def nested_if_loop(items):
    total = 0
    for x in items:
        if x > 0:
            total += x
    return total
"""
        self.assertEqual(self._get_complexity(code, "nested_if_loop"), 3)

    # TC04: Switch / Match statement -> 1 + guards
    def test_tc04_pattern_matching(self) -> None:
        code = """
def process_command(cmd):
    match cmd:
        case "start":
            return 1
        case "stop":
            return 0
        case "restart" if cmd.is_ready():
            return 2
        case _:
            return -1
"""
        self.assertEqual(self._get_complexity(code, "process_command"), 2)

    # TC05: Boolean chain a and b and c -> 1
    def test_tc05_boolean_chain_same_op(self) -> None:
        code = """
def bool_chain(a, b, c):
    if a and b and c:
        return True
    return False
"""
        self.assertEqual(self._get_complexity(code, "bool_chain"), 2)

    # TC06: Boolean switch a and b or c -> 2 (if + 1, switch + 1)
    def test_tc06_boolean_switch(self) -> None:
        code = """
def bool_switch(a, b, c):
    if (a and b) or c:
        return True
    return False
"""
        self.assertEqual(self._get_complexity(code, "bool_switch"), 3)

    # TC07: elif chains -> 1 per branch, 0 nesting penalty
    def test_tc07_elif_chain(self) -> None:
        code = """
def elif_chain(x):
    if x == 1:
        return "one"
    elif x == 2:
        return "two"
    elif x == 3:
        return "three"
    else:
        return "other"
"""
        self.assertEqual(self._get_complexity(code, "elif_chain"), 3)

    # TC08: 3-level nested loop -> 1 + 2 + 3 = 6
    def test_tc08_triple_nested_loop(self) -> None:
        code = """
def triple_loop(matrix):
    for row in matrix:
        for col in row:
            for item in col:
                print(item)
"""
        self.assertEqual(self._get_complexity(code, "triple_loop"), 6)

    # TC09: Direct recursion -> +1
    def test_tc09_recursion(self) -> None:
        code = """
def factorial(n):
    if n <= 1:
        return 1
    return n * factorial(n - 1)
"""
        self.assertEqual(self._get_complexity(code, "factorial"), 2)

    # TC10: try/except block -> +1 for except
    def test_tc10_try_except(self) -> None:
        code = """
def safe_divide(a, b):
    try:
        return a / b
    except ZeroDivisionError:
        return 0
"""
        self.assertEqual(self._get_complexity(code, "safe_divide"), 1)

    # TC11: Nested closure / lambda -> nesting increment for inner control flow
    def test_tc11_nested_lambda_with_branch(self) -> None:
        code = """
def outer_with_lambda(items):
    f = lambda x: x * 2 if x > 0 else 0
    return [f(x) for x in items]
"""
        self.assertEqual(self._get_complexity(code, "outer_with_lambda"), 3)

    # TC12: Ternary operator -> 1 + nesting
    def test_tc12_ternary_nested(self) -> None:
        code = """
def ternary_nested(items):
    for x in items:
        val = 1 if x > 0 else -1
"""
        self.assertEqual(self._get_complexity(code, "ternary_nested"), 3)

    # TC13: Guard clause early return
    def test_tc13_guard_clause(self) -> None:
        code = """
def guard_clause(user):
    if user is None:
        return None
    if not user.is_active:
        return None
    return user.name
"""
        self.assertEqual(self._get_complexity(code, "guard_clause"), 2)

    # TC14: List comprehension with if filter
    def test_tc14_comprehension_filter(self) -> None:
        code = """
def filter_evens(nums):
    return [x * 2 for x in nums if x % 2 == 0]
"""
        self.assertEqual(self._get_complexity(code, "filter_evens"), 2)

    # TC15: SonarSource Whitepaper Appendix B Example
    def test_tc15_sonarsource_appendix_b(self) -> None:
        code = """
def get_element(matrix):
    for i in range(len(matrix)):                    # +1 (nesting 0)
        for j in range(len(matrix[i])):             # +2 (nesting 1)
            if matrix[i][j] is not None:            # +3 (nesting 2)
                if matrix[i][j] > 0 and matrix[i][j] < 100:  # +4 (nesting 3) + 1 (bool) = +5
                    return matrix[i][j]
                elif matrix[i][j] == 0:             # +1 (elif base increment, no nesting penalty)
                    continue
    return None
"""
        self.assertEqual(self._get_complexity(code, "get_element"), 12)

    def test_class_methods_and_async(self) -> None:
        code = """
class Calculator:
    def __init__(self):
        self.total = 0

    async def compute(self, stream):
        async for item in stream:
            if item > 0:
                self.total += item

    def complex_comprehensions(self, data):
        s = {x for x in data if x > 0}
        d = {k: v for k, v in data.items() if v}
        g = (x for x in data if x < 0)
        return s, d, g
"""
        file_comp = self.analyzer.analyze_source(code, "calc.py")
        func_names = [f.name for f in file_comp.functions]
        self.assertIn("__init__", func_names)
        self.assertIn("compute", func_names)
        self.assertIn("complex_comprehensions", func_names)

        compute_func = next(f for f in file_comp.functions if f.name == "compute")
        self.assertEqual(compute_func.complexity, 3)

    def test_syntax_error(self) -> None:
        with self.assertRaises(ValueError):
            self.analyzer.analyze_source("def bad_syntax(:", "bad.py")

    def test_nested_functions_and_while_loops(self) -> None:
        code = """
def outer_func(n):
    def inner_helper(x):
        while x > 0:
            x -= 1
        return x
    return inner_helper(n)
"""
        file_comp = self.analyzer.analyze_source(code, "nested.py")
        outer = next(f for f in file_comp.functions if f.name == "outer_func")
        self.assertEqual(outer.complexity, 2)

    def test_formatters(self) -> None:
        file_comp = FileComplexity(
            path="long/path/to/very_long_file_name_that_should_be_truncated.py",
            total_complexity=20,
            average_complexity=10.0,
            highest_complexity=18,
            functions=[
                FunctionComplexity(
                    name="extremely_long_function_name_that_needs_truncation_for_table_view",
                    class_name="VeryLongClassNameForTestingTruncationLogic",
                    line_number=1,
                    end_line_number=50,
                    complexity=18,
                    exceeds_threshold=True,
                    breakdown=[
                        ComplexityIncrement(10, 4, "if", 2, 1, "if statement (+1 + nesting 1 = +2)")
                    ],
                ),
                FunctionComplexity(
                    name="simple",
                    class_name=None,
                    line_number=55,
                    end_line_number=60,
                    complexity=2,
                    exceeds_threshold=False,
                    breakdown=[],
                )
            ]
        )
        empty_file = FileComplexity("empty.py", 0, 0.0, 0, [])

        report = ComplexityReport(
            version="1.0.0",
            language="python",
            summary={
                "total_files": 2,
                "total_functions": 2,
                "total_complexity": 20,
                "average_complexity": 10.0,
                "highest_complexity": 18,
                "functions_exceeding_threshold": 1,
                "threshold": 15,
            },
            files=[file_comp, empty_file],
        )

        text_out = format_text_report(report, verbose=True)
        self.assertIn("extremely_long_function_name", text_out)
        self.assertIn("EXCEEDS THRESHOLD", text_out)
        self.assertIn("(No functions or methods found)", text_out)

        table_out = format_table_report(report)
        self.assertIn("...", table_out)

        summary_out = format_summary_report(report)
        self.assertIn("Total Complexity: 20", summary_out)

    def test_analyze_paths_and_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = Path(tmpdir) / "a.py"
            f2 = Path(tmpdir) / "b.py"
            f1.write_text("def z():\n    pass\n\ndef a():\n    if True:\n        pass\n")
            f2.write_text("def b():\n    for i in range(10):\n        if i:\n            pass\n")

            # Sort by name
            rep_name = analyze_paths([str(f1)], threshold=15, sort_key="name")
            self.assertEqual(rep_name.files[0].functions[0].name, "a")

            # Sort by line
            rep_line = analyze_paths([str(f1)], threshold=15, sort_key="line")
            self.assertEqual(rep_line.files[0].functions[0].line_number, 1)

            # Directory traversal
            rep_dir = analyze_paths([tmpdir], threshold=1, sort_key="complexity")
            self.assertEqual(rep_dir.summary["total_files"], 2)
            self.assertGreater(rep_dir.summary["functions_exceeding_threshold"], 0)

            # Missing path
            rep_missing = analyze_paths([str(Path(tmpdir) / "nonexistent.py")])
            self.assertEqual(rep_missing.summary["total_files"], 0)

    def test_cli_main(self) -> None:
        # Test stdin with json format
        test_code = "def sample():\n    if True:\n        pass\n"
        with patch("sys.stdin", io.StringIO(test_code)), patch("sys.argv", ["prog", "-f", "json", "-"]):
            code = main()
            self.assertEqual(code, 0)

        # Test table format
        with patch("sys.stdin", io.StringIO(test_code)), patch("sys.argv", ["prog", "-f", "table", "-"]):
            code = main()
            self.assertEqual(code, 0)

        # Test summary format
        with patch("sys.stdin", io.StringIO(test_code)), patch("sys.argv", ["prog", "-f", "summary", "-"]):
            code = main()
            self.assertEqual(code, 0)

        # Test threshold violation exit code 1
        high_code = "def high():\n    if True:\n        if True:\n            if True:\n                pass\n"
        with patch("sys.stdin", io.StringIO(high_code)), patch("sys.argv", ["prog", "-t", "2", "-"]):
            code = main()
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
