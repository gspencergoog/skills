#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "tree-sitter>=0.23,<0.25",
#   "tree-sitter-cpp>=0.23,<0.24",
# ]
# ///
"""
Unit and Integration tests for C/C++ Cognitive Complexity Analyzer.
Verifies the 15 SonarSource compliance benchmarks, C++17/20 features,
CLI formatters, and self-complexity bounds.
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from cognitive_complexity import (  # noqa: E402
    CppComplexityAnalyzer,
    analyze_paths,
    format_summary_report,
    format_table_report,
    format_text_report,
    main,
)


class TestCppCognitiveComplexity(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = CppComplexityAnalyzer(threshold=15)

    def _get_complexity(self, code: str, func_name: str) -> int:
        file_comp = self.analyzer.analyze_source(code, file_path="test.cc")
        for f in file_comp.functions:
            if f.name == func_name:
                return f.complexity
        raise AssertionError(
            f"Function {func_name} not found in {[f.name for f in file_comp.functions]}"
        )

    # TC01: Flat linear code -> 0
    def test_tc01_linear_code(self) -> None:
        code = """
int linear_function(int a, int b) {
    int x = a + b;
    int y = x * 2;
    return y;
}
"""
        self.assertEqual(self._get_complexity(code, "linear_function"), 0)

    # TC02: Single if statement -> 1
    def test_tc02_single_if(self) -> None:
        code = """
int single_if(int x) {
    if (x > 0) {
        return x;
    }
    return -x;
}
"""
        self.assertEqual(self._get_complexity(code, "single_if"), 1)

    # TC03: Nested if inside for loop -> 3 (Loop +1, If +2)
    def test_tc03_nested_if_in_loop(self) -> None:
        code = """
#include <vector>
int nested_if_loop(const std::vector<int>& items) {
    int total = 0;
    for (int x : items) {
        if (x > 0) {
            total += x;
        }
    }
    return total;
}
"""
        self.assertEqual(self._get_complexity(code, "nested_if_loop"), 3)

    # TC04: Switch statement -> 1
    def test_tc04_switch_statement(self) -> None:
        code = """
int process_command(int cmd) {
    switch (cmd) {
        case 1:
            return 1;
        case 2:
            return 0;
        default:
            return -1;
    }
}
"""
        self.assertEqual(self._get_complexity(code, "process_command"), 1)

    # TC05: Boolean chain a && b && c -> 2 (if +1, bool +1)
    def test_tc05_boolean_chain_same_op(self) -> None:
        code = """
bool bool_chain(bool a, bool b, bool c) {
    if (a && b && c) {
        return true;
    }
    return false;
}
"""
        self.assertEqual(self._get_complexity(code, "bool_chain"), 2)

    # TC06: Boolean switch (a && b) || c -> 3 (if +1, && +1, || +1)
    def test_tc06_boolean_switch(self) -> None:
        code = """
bool bool_switch(bool a, bool b, bool c) {
    if ((a && b) || c) {
        return true;
    }
    return false;
}
"""
        self.assertEqual(self._get_complexity(code, "bool_switch"), 3)

    # TC07: else if chain with bare else -> 4 (if +1, else if +1, else if +1, else +1)
    def test_tc07_else_if_chain(self) -> None:
        code = """
const char* else_if_chain(int x) {
    if (x == 1) {
        return "one";
    } else if (x == 2) {
        return "two";
    } else if (x == 3) {
        return "three";
    } else {
        return "other";
    }
}
"""
        self.assertEqual(self._get_complexity(code, "else_if_chain"), 4)

    # TC08: 3-level nested loop -> 1 + 2 + 3 = 6
    def test_tc08_triple_nested_loop(self) -> None:
        code = """
void triple_loop(int n) {
    for (int i = 0; i < n; ++i) {
        for (int j = 0; j < n; ++j) {
            for (int k = 0; k < n; ++k) {
                int v = i + j + k;
            }
        }
    }
}
"""
        self.assertEqual(self._get_complexity(code, "triple_loop"), 6)

    # TC09: Direct recursion -> +1
    def test_tc09_recursion(self) -> None:
        code = """
int factorial(int n) {
    if (n <= 1) {
        return 1;
    }
    return n * factorial(n - 1);
}
"""
        self.assertEqual(self._get_complexity(code, "factorial"), 2)

    # TC10: try/catch block -> +1 for catch
    def test_tc10_try_catch(self) -> None:
        code = """
int safe_divide(int a, int b) {
    try {
        return a / b;
    } catch (...) {
        return 0;
    }
}
"""
        self.assertEqual(self._get_complexity(code, "safe_divide"), 1)

    # TC11: Nested lambda with branch -> nesting increment for inner control flow
    def test_tc11_nested_lambda_with_branch(self) -> None:
        code = """
int outer_with_lambda(int y) {
    auto f = [](int x) {
        if (x > 0) {
            return x * 2;
        }
        return 0;
    };
    return f(y);
}
"""
        self.assertEqual(self._get_complexity(code, "outer_with_lambda"), 2)

    # TC12: Ternary operator nested in loop -> 3 (loop +1, ternary +2)
    def test_tc12_ternary_nested(self) -> None:
        code = """
void ternary_nested(int n) {
    for (int i = 0; i < n; ++i) {
        int val = (i > 0) ? 1 : -1;
    }
}
"""
        self.assertEqual(self._get_complexity(code, "ternary_nested"), 3)

    # TC13: Guard clause early returns -> 2
    def test_tc13_guard_clause(self) -> None:
        code = """
int guard_clause(const int* ptr, bool active) {
    if (!ptr) {
        return -1;
    }
    if (!active) {
        return -2;
    }
    return *ptr;
}
"""
        self.assertEqual(self._get_complexity(code, "guard_clause"), 2)

    # TC14: goto statement -> +1 flat increment
    def test_tc14_goto_statement(self) -> None:
        code = """
int cleanup_flow(int err) {
    if (err != 0) {
        goto fail;
    }
    return 0;
fail:
    return -1;
}
"""
        # if (+1) + goto (+1, no nesting penalty) = 2
        self.assertEqual(self._get_complexity(code, "cleanup_flow"), 2)

    # TC15: SonarSource Whitepaper Appendix B Example
    def test_tc15_sonarsource_appendix_b(self) -> None:
        code = """
int get_element(int matrix[4][4], int rows, int cols) {
    for (int i = 0; i < rows; ++i) {                     // +1 (nesting 0)
        for (int j = 0; j < cols; ++j) {                 // +2 (nesting 1)
            if (matrix[i][j] != -1) {                    // +3 (nesting 2)
                if (matrix[i][j] > 0 && matrix[i][j] < 100) { // +4 (nesting 3) + 1 (bool) = +5
                    return matrix[i][j];
                } else if (matrix[i][j] == 0) {          // +1 (else if base)
                    continue;
                }
            }
        }
    }
    return -1;
}
"""
        self.assertEqual(self._get_complexity(code, "get_element"), 12)

    def test_if_constexpr_and_else_if_constexpr(self) -> None:
        code = """
template <typename T>
int dispatch_type(T val) {
    for (int i = 0; i < 2; ++i) {        // +1 (nesting 0)
        if constexpr (sizeof(T) == 8) {  // +2 (1 + nesting 1)
            return 8;
        } else if constexpr (sizeof(T) == 4) { // +1 (else if constexpr, no nesting penalty)
            if (val > 0) {               // +3 (1 + nesting 2)
                return 4;
            }
        } else {                         // +1 (bare else, no nesting penalty)
            if (val < 0) {               // +3 (1 + nesting 2)
                return -1;
            }
        }
    }
    return 0;
}
"""
        # 1 + 2 + 1 + 3 + 1 + 3 = 11
        self.assertEqual(self._get_complexity(code, "dispatch_type"), 11)

    def test_out_of_line_methods_templates_and_operators(self) -> None:
        code = """
namespace core {
template <typename T>
class Box {
public:
    Box() {
        if (true) {}
    }
    ~Box() {
        if (true) {}
    }
    bool operator==(const Box& other) const {
        return true ? true : false;
    }
    bool check(int x);
};

template <typename T>
bool Box<T>::check(int x) {
    if (x > 0 and x < 10 or x == 42) {
        return true;
    }
    return false;
}
}
"""
        file_comp = self.analyzer.analyze_source(code, "box.hpp")
        by_name = {f.name: f for f in file_comp.functions}
        self.assertIn("Box", by_name)
        self.assertIn("~Box", by_name)
        self.assertIn("operator==", by_name)
        self.assertIn("check", by_name)
        self.assertEqual(by_name["check"].class_name, "core::Box<T>")
        # if (+1) + 'and' sequence (+1) + 'or' switch (+1) = 3
        self.assertEqual(by_name["check"].complexity, 3)

    def test_header_with_declarations_only(self) -> None:
        code = """
#pragma once
namespace api {
void initialize(int flags);
int compute_value(const char* input, int len);
class Client {
public:
    Client();
    ~Client();
    bool connect(const char* host);
};
}
"""
        file_comp = self.analyzer.analyze_source(code, "client.h")
        self.assertEqual(len(file_comp.functions), 0)
        self.assertEqual(file_comp.total_complexity, 0)

    def test_cli_and_formatters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f1 = Path(tmpdir) / "sample.cc"
            f1.write_text(
                "int foo(int x) {\n    if (x > 0) return x;\n    return 0;\n}\n"
            )

            rep = analyze_paths([str(f1)], threshold=15, sort_key="name")
            self.assertEqual(rep.summary["total_files"], 1)
            self.assertEqual(rep.summary["total_functions"], 1)

            text_out = format_text_report(rep, verbose=True)
            self.assertIn("Language: cpp", text_out)
            self.assertIn("foo", text_out)

            table_out = format_table_report(rep)
            self.assertIn("foo", table_out)

            summary_out = format_summary_report(rep)
            self.assertIn("Total Complexity: 1", summary_out)

        sample_cpp = "int bar(int x) { if (x > 0) { if (x > 1) { if (x > 2) return 1; } } return 0; }"
        with (
            patch("sys.stdin", io.StringIO(sample_cpp)),
            patch("sys.argv", ["prog", "-f", "json", "-t", "2", "-"]),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            exit_code = main()
            self.assertEqual(exit_code, 1)

    def test_self_cognitive_complexity_under_threshold(self) -> None:
        py_engine_dir = SCRIPT_DIR.parent / "python"
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "py_cc_check", py_engine_dir / "cognitive_complexity.py"
        )
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules["py_cc_check"] = mod
        spec.loader.exec_module(mod)

        py_analyzer = mod.PythonComplexityAnalyzer(threshold=15)
        cpp_engine_src = (SCRIPT_DIR / "cognitive_complexity.py").read_text(
            encoding="utf-8"
        )
        res = py_analyzer.analyze_source(
            cpp_engine_src, file_path="scripts/cpp/cognitive_complexity.py"
        )
        exceeding = [f for f in res.functions if f.exceeds_threshold]
        self.assertEqual(
            exceeding,
            [],
            f"Functions in scripts/cpp/cognitive_complexity.py exceeding threshold 15: "
            f"{[(f.name, f.complexity) for f in exceeding]}",
        )


if __name__ == "__main__":
    unittest.main()
