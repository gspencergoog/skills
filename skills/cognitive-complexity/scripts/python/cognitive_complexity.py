#!/usr/bin/env python3
"""
Cognitive Complexity Analyzer for Python.
Calculates Cognitive Complexity according to the SonarSource specification.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


@dataclass
class ComplexityIncrement:
    line: int
    column: int
    node_type: str
    increment: int
    nesting: int
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "line": self.line,
            "column": self.column,
            "type": self.node_type,
            "increment": self.increment,
            "nesting": self.nesting,
            "reason": self.reason,
        }


@dataclass
class FunctionComplexity:
    name: str
    class_name: Optional[str]
    line_number: int
    end_line_number: int
    complexity: int
    exceeds_threshold: bool
    breakdown: List[ComplexityIncrement] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "class_name": self.class_name,
            "line_number": self.line_number,
            "end_line_number": self.end_line_number,
            "complexity": self.complexity,
            "exceeds_threshold": self.exceeds_threshold,
            "breakdown": [b.to_dict() for b in self.breakdown],
        }


@dataclass
class FileComplexity:
    path: str
    total_complexity: int
    average_complexity: float
    highest_complexity: int
    functions: List[FunctionComplexity] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "total_complexity": self.total_complexity,
            "average_complexity": round(self.average_complexity, 2),
            "highest_complexity": self.highest_complexity,
            "functions": [f.to_dict() for f in self.functions],
        }


@dataclass
class ComplexityReport:
    version: str
    language: str
    files: List[FileComplexity]
    summary: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "language": self.language,
            "summary": self.summary,
            "files": [f.to_dict() for f in self.files],
        }


class FunctionComplexityVisitor(ast.NodeVisitor):
    """
    Calculates the Cognitive Complexity of a single Python function or method.
    """

    def __init__(self, function_name: str, threshold: int = 15) -> None:
        self.function_name = function_name
        self.threshold = threshold
        self.complexity = 0
        self.breakdown: List[ComplexityIncrement] = []
        self._current_nesting = 0
        self._in_elif = False

    def _add_increment(
        self,
        node: ast.AST,
        node_type: str,
        base_increment: int,
        nesting_penalty: bool,
        reason: str,
    ) -> None:
        penalty = self._current_nesting if nesting_penalty else 0
        total_increment = base_increment + penalty
        self.complexity += total_increment
        line = getattr(node, "lineno", 0)
        col = getattr(node, "col_offset", 0)
        detail = f"{reason} (+{base_increment}{f' + nesting {penalty}' if penalty > 0 else ''} = +{total_increment})"
        self.breakdown.append(
            ComplexityIncrement(
                line=line,
                column=col,
                node_type=node_type,
                increment=total_increment,
                nesting=self._current_nesting,
                reason=detail,
            )
        )

    def visit_If(self, node: ast.If) -> None:
        is_elif = self._in_elif
        self._in_elif = False

        if is_elif:
            self._add_increment(
                node, "elif", base_increment=1, nesting_penalty=False, reason="elif branch"
            )
        else:
            self._add_increment(
                node, "if", base_increment=1, nesting_penalty=True, reason="if statement"
            )

        self.visit(node.test)

        self._current_nesting += 1
        for item in node.body:
            self.visit(item)
        self._current_nesting -= 1

        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            self._in_elif = True
            self.visit(node.orelse[0])
        else:
            for item in node.orelse:
                self.visit(item)

    def visit_IfExp(self, node: ast.IfExp) -> None:
        self._add_increment(
            node, "ternary", base_increment=1, nesting_penalty=True, reason="ternary expression"
        )
        self.visit(node.test)
        self._current_nesting += 1
        self.visit(node.body)
        self.visit(node.orelse)
        self._current_nesting -= 1

    def visit_For(self, node: ast.For) -> None:
        self._handle_loop(node, "for loop")

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._handle_loop(node, "async for loop")

    def visit_While(self, node: ast.While) -> None:
        self._handle_loop(node, "while loop")

    def _handle_loop(self, node: ast.AST, loop_name: str) -> None:
        self._add_increment(
            node, "loop", base_increment=1, nesting_penalty=True, reason=loop_name
        )
        if hasattr(node, "iter"):
            self.visit(node.iter)
        if hasattr(node, "test"):
            self.visit(node.test)

        self._current_nesting += 1
        body = getattr(node, "body", [])
        for item in body:
            self.visit(item)
        self._current_nesting -= 1

        orelse = getattr(node, "orelse", [])
        for item in orelse:
            self.visit(item)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self._add_increment(
            node, "except", base_increment=1, nesting_penalty=True, reason="except handler"
        )
        if node.type:
            self.visit(node.type)
        self._current_nesting += 1
        for item in node.body:
            self.visit(item)
        self._current_nesting -= 1

    def visit_Match(self, node: Any) -> None:
        self._add_increment(
            node, "match", base_increment=1, nesting_penalty=True, reason="match statement"
        )
        self.visit(node.subject)
        self._current_nesting += 1
        for case in node.cases:
            if case.guard:
                self._add_increment(
                    case, "case_guard", base_increment=1, nesting_penalty=False, reason="case guard"
                )
                self.visit(case.guard)
            for item in case.body:
                self.visit(item)
        self._current_nesting -= 1

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        self._process_bool_op(node, parent_op_type=None)

    def _process_bool_op(self, node: ast.AST, parent_op_type: Optional[type]) -> None:
        if isinstance(node, ast.BoolOp):
            current_op_type = type(node.op)
            op_name = "and" if isinstance(node.op, ast.And) else "or"
            if parent_op_type is None:
                self._add_increment(
                    node,
                    "bool_op_sequence",
                    base_increment=1,
                    nesting_penalty=False,
                    reason=f"boolean operator sequence ({op_name})",
                )
            elif parent_op_type != current_op_type:
                self._add_increment(
                    node,
                    "bool_op_switch",
                    base_increment=1,
                    nesting_penalty=False,
                    reason=f"boolean operator switch to ({op_name})",
                )

            for value in node.values:
                self._process_bool_op(value, current_op_type)
        else:
            self.visit(node)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._handle_comprehension(node, "list comprehension")

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._handle_comprehension(node, "set comprehension")

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._handle_comprehension(node, "dict comprehension")

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._handle_comprehension(node, "generator expression")

    def _handle_comprehension(self, node: ast.AST, name: str) -> None:
        generators = getattr(node, "generators", [])
        for gen in generators:
            self._add_increment(
                gen, "comprehension_for", base_increment=1, nesting_penalty=True, reason=f"{name} for loop"
            )
            for if_expr in gen.ifs:
                self._add_increment(
                    if_expr, "comprehension_if", base_increment=1, nesting_penalty=False, reason=f"{name} if filter"
                )
                self.visit(if_expr)
        if hasattr(node, "elt"):
            self.visit(node.elt)
        if hasattr(node, "key"):
            self.visit(node.key)
        if hasattr(node, "value"):
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._current_nesting += 1
        for item in node.body:
            self.visit(item)
        self._current_nesting -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._current_nesting += 1
        for item in node.body:
            self.visit(item)
        self._current_nesting -= 1

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._current_nesting += 1
        self.visit(node.body)
        self._current_nesting -= 1

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == self.function_name:
            self._add_increment(
                node, "recursion", base_increment=1, nesting_penalty=False, reason="direct recursion call"
            )
        self.generic_visit(node)


class PythonComplexityAnalyzer:
    """
    Parses Python source code and extracts cognitive complexity for all functions/methods.
    """

    def __init__(self, threshold: int = 15) -> None:
        self.threshold = threshold

    def analyze_source(self, source_code: str, file_path: str = "<stdin>") -> FileComplexity:
        try:
            tree = ast.parse(source_code, filename=file_path)
        except SyntaxError as e:
            raise ValueError(f"Syntax error in {file_path}:{e.lineno}:{e.offset}: {e.msg}") from e

        functions: List[FunctionComplexity] = []
        self._collect_functions(tree, class_name=None, functions=functions)

        total_complexity = sum(f.complexity for f in functions)
        avg_complexity = (total_complexity / len(functions)) if functions else 0.0
        highest_complexity = max((f.complexity for f in functions), default=0)

        return FileComplexity(
            path=file_path,
            total_complexity=total_complexity,
            average_complexity=avg_complexity,
            highest_complexity=highest_complexity,
            functions=functions,
        )

    def _collect_functions(
        self,
        node: ast.AST,
        class_name: Optional[str],
        functions: List[FunctionComplexity],
    ) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                c_name = f"{class_name + '.' if class_name else ''}{child.name}"
                self._collect_functions(child, class_name=c_name, functions=functions)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(self._analyze_function(child, class_name=class_name))
                for inner in child.body:
                    if isinstance(inner, ast.ClassDef):
                        c_name = f"{class_name + '.' if class_name else ''}{child.name}.{inner.name}"
                        self._collect_functions(inner, class_name=c_name, functions=functions)

    def _analyze_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef, class_name: Optional[str]
    ) -> FunctionComplexity:
        visitor = FunctionComplexityVisitor(function_name=node.name, threshold=self.threshold)
        for stmt in node.body:
            visitor.visit(stmt)

        end_lineno = getattr(node, "end_lineno", node.lineno)
        return FunctionComplexity(
            name=node.name,
            class_name=class_name,
            line_number=node.lineno,
            end_line_number=end_lineno,
            complexity=visitor.complexity,
            exceeds_threshold=visitor.complexity > self.threshold,
            breakdown=visitor.breakdown,
        )


def _format_function_entry(func: FunctionComplexity, threshold: int, verbose: bool) -> List[str]:
    qualified_name = f"{func.class_name + '.' if func.class_name else ''}{func.name}"
    status = f"[EXCEEDS THRESHOLD {threshold}]" if func.exceeds_threshold else "[PASS]"
    lines = [f"  {qualified_name} (lines {func.line_number}-{func.end_line_number}) -> Complexity: {func.complexity} {status}"]
    if verbose and func.breakdown:
        for b in func.breakdown:
            lines.append(f"    Line {b.line:4d}: {b.reason}")
    return lines


def format_text_report(report: ComplexityReport, verbose: bool = False) -> str:
    lines = [f"Cognitive Complexity Report (Language: {report.language})", "=" * 60]
    for file_comp in report.files:
        lines.append(f"\nFile: {file_comp.path}")
        if not file_comp.functions:
            lines.append("  (No functions or methods found)")
            continue
        for func in file_comp.functions:
            lines.extend(_format_function_entry(func, report.summary["threshold"], verbose))

    s = report.summary
    lines.extend([
        "\n" + "-" * 60,
        "Summary:",
        f"  Files analyzed:                {s['total_files']}",
        f"  Total functions:               {s['total_functions']}",
        f"  Total complexity:              {s['total_complexity']}",
        f"  Average complexity:            {s['average_complexity']:.2f}",
        f"  Highest complexity:            {s['highest_complexity']}",
        f"  Functions exceeding threshold: {s['functions_exceeding_threshold']} (threshold: {s['threshold']})",
        "=" * 60,
    ])
    return "\n".join(lines)


def format_table_report(report: ComplexityReport) -> str:
    lines = [f"{'Function':<35} {'File':<25} {'Lines':<12} {'Complexity':<12} {'Status'}", "-" * 88]
    for file_comp in report.files:
        for func in file_comp.functions:
            q_name = f"{func.class_name + '.' if func.class_name else ''}{func.name}"
            q_display = (q_name[:30] + "...") if len(q_name) > 33 else q_name
            file_display = Path(file_comp.path).name
            f_display = (file_display[:20] + "...") if len(file_display) > 23 else file_display
            status = "WARN" if func.exceeds_threshold else "OK"
            lines.append(f"{q_display:<35} {f_display:<25} {f'{func.line_number}-{func.end_line_number}':<12} {func.complexity:<12} {status}")
    return "\n".join(lines)


def format_summary_report(report: ComplexityReport) -> str:
    s = report.summary
    return f"Files: {s['total_files']}, Functions: {s['total_functions']}, Total Complexity: {s['total_complexity']}, Avg: {s['average_complexity']:.2f}, Over Threshold: {s['functions_exceeding_threshold']}"


def _sort_file_functions(files: List[FileComplexity], sort_key: str) -> None:
    for file_comp in files:
        if sort_key == "complexity":
            file_comp.functions.sort(key=lambda f: f.complexity, reverse=True)
        elif sort_key == "name":
            file_comp.functions.sort(key=lambda f: f.name)
        elif sort_key == "line":
            file_comp.functions.sort(key=lambda f: f.line_number)


def _collect_python_files(p: Path, exclude_patterns: Optional[List[str]]) -> List[Path]:
    if p.is_file():
        return [p]
    if p.is_dir():
        return [
            f for f in p.rglob("*.py")
            if not (exclude_patterns and any(f.match(pat) for pat in exclude_patterns))
        ]
    return []


def analyze_paths(
    paths: Sequence[str],
    threshold: int = 15,
    sort_key: str = "complexity",
    exclude_patterns: Optional[List[str]] = None,
) -> ComplexityReport:
    analyzer = PythonComplexityAnalyzer(threshold=threshold)
    file_results: List[FileComplexity] = []

    for path_str in paths:
        if path_str == "-" or not path_str:
            file_results.append(analyzer.analyze_source(sys.stdin.read(), file_path="<stdin>"))
            continue

        p = Path(path_str)
        if not p.exists():
            print(f"Error: Path does not exist: {path_str}", file=sys.stderr)
            continue

        for py_file in _collect_python_files(p, exclude_patterns):
            try:
                content = py_file.read_text(encoding="utf-8")
                file_results.append(analyzer.analyze_source(content, file_path=str(py_file)))
            except Exception as e:
                print(f"Error reading {py_file}: {e}", file=sys.stderr)

    _sort_file_functions(file_results, sort_key)

    total_files = len(file_results)
    total_funcs = sum(len(f.functions) for f in file_results)
    total_complexity = sum(f.total_complexity for f in file_results)
    avg_complexity = (total_complexity / total_funcs) if total_funcs else 0.0
    highest_complexity = max((f.highest_complexity for f in file_results), default=0)
    exceeding_count = sum(1 for f in file_results for func in f.functions if func.exceeds_threshold)

    summary = {
        "total_files": total_files,
        "total_functions": total_funcs,
        "total_complexity": total_complexity,
        "average_complexity": round(avg_complexity, 2),
        "highest_complexity": highest_complexity,
        "functions_exceeding_threshold": exceeding_count,
        "threshold": threshold,
    }

    return ComplexityReport(version="1.0.0", language="python", files=file_results, summary=summary)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate Cognitive Complexity for Python code according to SonarSource standard."
    )
    parser.add_argument("path", nargs="?", default="-", help="Path to analyze (default: stdin).")
    parser.add_argument("-f", "--format", choices=["text", "json", "table", "summary"], default="text", help="Output format.")
    parser.add_argument("-t", "--threshold", type=int, default=15, help="Threshold for flagging complexity.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed breakdown.")
    parser.add_argument("-s", "--sort", choices=["complexity", "name", "line", "file"], default="complexity", help="Sort criteria.")
    parser.add_argument("-e", "--exclude", action="append", help="Glob patterns to exclude.")
    parser.add_argument("-V", "--version", action="version", version="%(prog)s 1.0.0")

    args = parser.parse_args()

    try:
        report = analyze_paths(paths=[args.path], threshold=args.threshold, sort_key=args.sort, exclude_patterns=args.exclude)
    except Exception as e:
        print(f"Error during analysis: {e}", file=sys.stderr)
        return 2

    formatters = {
        "json": lambda r: json.dumps(r.to_dict(), indent=2),
        "table": format_table_report,
        "summary": format_summary_report,
        "text": lambda r: format_text_report(r, verbose=args.verbose),
    }
    print(formatters.get(args.format, formatters["text"])(report))

    return 1 if report.summary["functions_exceeding_threshold"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
