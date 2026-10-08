#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "tree-sitter>=0.23,<0.25",
#   "tree-sitter-cpp>=0.23,<0.24",
# ]
# ///
"""
Cognitive Complexity Analyzer for C and C++.
Calculates Cognitive Complexity according to the SonarSource specification
using tree-sitter-cpp (with optional clang-tidy delegation).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

CPP_EXTENSIONS: Tuple[str, ...] = (
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".c++",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inc",
)

LOOP_REASONS: Dict[str, str] = {
    "for_statement": "for loop",
    "for_range_loop": "range-based for loop",
    "while_statement": "while loop",
    "do_statement": "do-while loop",
}

LOGICAL_OP_CANONICAL: Dict[str, str] = {
    "&&": "&&",
    "and": "&&",
    "||": "||",
    "or": "||",
}

DECLARATOR_WRAPPERS: Set[str] = {
    "pointer_declarator",
    "reference_declarator",
    "parenthesized_declarator",
    "attributed_declarator",
    "array_declarator",
}

LEAF_NAME_NODES: Set[str] = {
    "identifier",
    "field_identifier",
    "destructor_name",
    "operator_name",
    "type_identifier",
}


def _find_uv_binary() -> Optional[str]:
    local_uv = Path.home() / ".local" / "bin" / "uv"
    if local_uv.is_file() and os.access(local_uv, os.X_OK):
        return str(local_uv)
    return shutil.which("uv")


def _ensure_tree_sitter() -> Tuple[Any, Any]:
    try:
        import tree_sitter
        import tree_sitter_cpp

        return tree_sitter, tree_sitter_cpp
    except ModuleNotFoundError:
        uv_bin = _find_uv_binary()
        if not uv_bin:
            raise
        out = subprocess.check_output(
            [
                uv_bin,
                "run",
                "--python",
                sys.executable,
                "--with",
                "tree-sitter>=0.23,<0.25",
                "--with",
                "tree-sitter-cpp>=0.23,<0.24",
                "python3",
                "-c",
                "import json, sys; print(json.dumps(sys.path))",
            ],
            text=True,
        )
        for entry in json.loads(out):
            if entry and entry not in sys.path:
                sys.path.append(entry)
        import tree_sitter
        import tree_sitter_cpp

        return tree_sitter, tree_sitter_cpp


_CPP_PARSER: Any = None


def _get_cpp_parser() -> Any:
    global _CPP_PARSER
    if _CPP_PARSER is None:
        ts_mod, ts_cpp_mod = _ensure_tree_sitter()
        language = ts_mod.Language(ts_cpp_mod.language())
        _CPP_PARSER = ts_mod.Parser(language)
    return _CPP_PARSER


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


def _node_text(node: Any) -> str:
    return node.text.decode("utf-8", errors="replace").strip()


def _split_top_level_scope(qualified: str) -> Tuple[Optional[str], str]:
    depth = 0
    last_sep = -1
    i = 0
    while i < len(qualified) - 1:
        ch = qualified[i]
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif ch == ":" and qualified[i + 1] == ":" and depth == 0:
            last_sep = i
            i += 1
        i += 1
    if last_sep == -1:
        return None, qualified.strip()
    return qualified[:last_sep].strip(), qualified[last_sep + 2 :].strip()


def _find_function_declarator(node: Any) -> Optional[Any]:
    curr = node
    while curr is not None:
        if curr.type == "function_declarator":
            return curr
        if curr.type in DECLARATOR_WRAPPERS:
            curr = curr.child_by_field_name("declarator") or (
                curr.named_children[0] if curr.named_children else None
            )
            continue
        for child in curr.named_children:
            if child.type in ("function_declarator", *DECLARATOR_WRAPPERS):
                return _find_function_declarator(child)
        break
    return None


def _extract_declarator_name(decl_node: Any) -> Tuple[Optional[str], str]:
    inner = decl_node.child_by_field_name("declarator")
    if inner is None:
        return None, "<anonymous>"
    if inner.type in LEAF_NAME_NODES:
        return None, _node_text(inner)
    if inner.type == "template_function":
        name_node = inner.child_by_field_name("name")
        return None, _node_text(name_node) if name_node else _node_text(inner)
    if inner.type == "qualified_identifier":
        return _split_top_level_scope(_node_text(inner))
    return _split_top_level_scope(_node_text(inner))


def _unwrap_parens(node: Any) -> Any:
    curr = node
    while curr is not None and curr.type == "parenthesized_expression":
        curr = curr.named_children[0] if curr.named_children else None
    return curr


def _extract_binary_op_token(node: Any) -> Optional[str]:
    op_node = node.child_by_field_name("operator")
    if op_node is not None:
        return _node_text(op_node)
    for child in node.children:
        if not child.is_named:
            txt = _node_text(child)
            if txt in LOGICAL_OP_CANONICAL:
                return txt
    return None


def _extract_callee_name(call_node: Any) -> Optional[str]:
    fn_node = call_node.child_by_field_name("function")
    if fn_node is None:
        return None
    if fn_node.type in ("identifier", "field_identifier"):
        return _node_text(fn_node)
    if fn_node.type == "template_function":
        name_node = fn_node.child_by_field_name("name")
        return _node_text(name_node) if name_node else None
    if fn_node.type == "qualified_identifier":
        _, leaf = _split_top_level_scope(_node_text(fn_node))
        return leaf.split("<", 1)[0].strip()
    return None


class CppFunctionComplexityVisitor:
    """Calculates the Cognitive Complexity of a single C/C++ function or method."""

    def __init__(self, function_name: str, threshold: int = 15) -> None:
        self.function_name = function_name.split("<", 1)[0].strip()
        self.threshold = threshold
        self.complexity = 0
        self.breakdown: List[ComplexityIncrement] = []
        self._handlers: Dict[str, Callable[[Any, int], None]] = {
            "if_statement": self._handle_if,
            "conditional_expression": self._handle_ternary,
            "switch_statement": self._handle_switch,
            "for_statement": self._handle_loop,
            "for_range_loop": self._handle_loop,
            "while_statement": self._handle_loop,
            "do_statement": self._handle_loop,
            "catch_clause": self._handle_catch,
            "goto_statement": self._handle_goto,
            "binary_expression": self._handle_binary,
            "lambda_expression": self._handle_lambda,
            "function_definition": self._handle_nested_function,
            "call_expression": self._handle_call,
        }

    def add_increment(
        self,
        node: Any,
        node_type: str,
        base_increment: int,
        nesting: int,
        nesting_penalty: bool,
        reason: str,
    ) -> None:
        penalty = nesting if nesting_penalty else 0
        total_inc = base_increment + penalty
        self.complexity += total_inc
        line = node.start_point[0] + 1
        col = node.start_point[1] + 1
        suffix = f" + nesting {penalty}" if penalty > 0 else ""
        detail = f"{reason} (+{base_increment}{suffix} = +{total_inc})"
        self.breakdown.append(
            ComplexityIncrement(
                line=line,
                column=col,
                node_type=node_type,
                increment=total_inc,
                nesting=nesting,
                reason=detail,
            )
        )

    def visit(self, node: Optional[Any], nesting: int = 0) -> None:
        if node is None:
            return
        handler = self._handlers.get(node.type)
        if handler is not None:
            handler(node, nesting)
            return
        self._visit_children(node, nesting)

    def _visit_children(self, node: Any, nesting: int) -> None:
        for child in node.named_children:
            self.visit(child, nesting)

    def _handle_if(self, node: Any, nesting: int) -> None:
        is_else_if = node.parent is not None and node.parent.type == "else_clause"
        is_constexpr = any(c.type == "constexpr" for c in node.children)

        if is_else_if:
            label = "else if constexpr branch" if is_constexpr else "else if branch"
            self.add_increment(node, "else_if", 1, nesting, False, label)
        else:
            label = "if constexpr statement" if is_constexpr else "if statement"
            self.add_increment(node, "if", 1, nesting, True, label)

        self.visit(node.child_by_field_name("condition"), nesting)
        self.visit(node.child_by_field_name("consequence"), nesting + 1)

        alt = node.child_by_field_name("alternative")
        if alt is not None:
            self._handle_else_clause(alt, nesting)

    def _handle_else_clause(self, alt_node: Any, nesting: int) -> None:
        nested_if = next(
            (c for c in alt_node.named_children if c.type == "if_statement"), None
        )
        if nested_if is not None:
            self.visit(nested_if, nesting)
            return
        self.add_increment(alt_node, "else", 1, nesting, False, "else branch")
        for child in alt_node.named_children:
            self.visit(child, nesting + 1)

    def _handle_ternary(self, node: Any, nesting: int) -> None:
        self.add_increment(
            node, "ternary", 1, nesting, True, "ternary conditional expression"
        )
        self.visit(node.child_by_field_name("condition"), nesting)
        self.visit(node.child_by_field_name("consequence"), nesting + 1)
        self.visit(node.child_by_field_name("alternative"), nesting + 1)

    def _handle_switch(self, node: Any, nesting: int) -> None:
        self.add_increment(node, "switch", 1, nesting, True, "switch statement")
        self.visit(node.child_by_field_name("condition"), nesting)
        self.visit(node.child_by_field_name("body"), nesting + 1)

    def _handle_loop(self, node: Any, nesting: int) -> None:
        reason = LOOP_REASONS.get(node.type, "loop")
        self.add_increment(node, "loop", 1, nesting, True, reason)
        body = node.child_by_field_name("body")
        for child in node.named_children:
            child_nesting = (nesting + 1) if child == body else nesting
            self.visit(child, child_nesting)

    def _handle_catch(self, node: Any, nesting: int) -> None:
        self.add_increment(node, "catch", 1, nesting, True, "catch block")
        self.visit(node.child_by_field_name("body"), nesting + 1)

    def _handle_goto(self, node: Any, nesting: int) -> None:
        self.add_increment(node, "goto", 1, nesting, False, "goto statement")

    def _handle_binary(self, node: Any, nesting: int) -> None:
        self._process_bool_ops(node, parent_canonical_op=None, nesting=nesting)

    def _process_bool_ops(
        self, node: Optional[Any], parent_canonical_op: Optional[str], nesting: int
    ) -> None:
        unwrapped = _unwrap_parens(node)
        if unwrapped is None:
            return
        if unwrapped.type != "binary_expression":
            self.visit(unwrapped, nesting)
            return

        op_tok = _extract_binary_op_token(unwrapped)
        canonical = LOGICAL_OP_CANONICAL.get(op_tok or "")
        if canonical is None:
            self.visit(unwrapped.child_by_field_name("left"), nesting)
            self.visit(unwrapped.child_by_field_name("right"), nesting)
            return

        if parent_canonical_op is None:
            self.add_increment(
                unwrapped,
                "bool_op_sequence",
                1,
                nesting,
                False,
                f"boolean operator sequence ({op_tok})",
            )
        elif parent_canonical_op != canonical:
            self.add_increment(
                unwrapped,
                "bool_op_switch",
                1,
                nesting,
                False,
                f"boolean operator switch to ({op_tok})",
            )

        self._process_bool_ops(
            unwrapped.child_by_field_name("left"), canonical, nesting
        )
        self._process_bool_ops(
            unwrapped.child_by_field_name("right"), canonical, nesting
        )

    def _handle_lambda(self, node: Any, nesting: int) -> None:
        self.visit(node.child_by_field_name("body"), nesting + 1)

    def _handle_nested_function(self, node: Any, nesting: int) -> None:
        self.visit(node.child_by_field_name("body"), nesting + 1)

    def _handle_call(self, node: Any, nesting: int) -> None:
        callee = _extract_callee_name(node)
        if callee and callee == self.function_name:
            self.add_increment(
                node, "recursion", 1, nesting, False, "direct recursion call"
            )
        self._visit_children(node, nesting)


def _find_compile_commands(file_path: Path) -> Optional[Path]:
    curr = file_path.resolve().parent
    for directory in (curr, *curr.parents):
        candidate = directory / "compile_commands.json"
        if candidate.is_file():
            return directory
    return None


def _try_clang_tidy_analysis(
    file_path: str, threshold: int
) -> Optional[FileComplexity]:
    if file_path == "<stdin>" or not shutil.which("clang-tidy"):
        return None
    p = Path(file_path)
    if not p.is_file():
        return None
    build_dir = _find_compile_commands(p)
    if build_dir is None:
        return None

    cfg = json.dumps(
        {
            "Checks": "-*,readability-function-cognitive-complexity",
            "CheckOptions": [
                {
                    "key": "readability-function-cognitive-complexity.Threshold",
                    "value": "0",
                },
                {
                    "key": "readability-function-cognitive-complexity.DescribeBasicIncrements",
                    "value": "true",
                },
            ],
        }
    )
    res = subprocess.run(
        ["clang-tidy", f"-p={build_dir}", f"--config={cfg}", str(p)],
        text=True,
        capture_output=True,
    )
    if "clang-diagnostic-error" in res.stdout or "error:" in res.stdout:
        return None
    return _parse_clang_tidy_output(res.stdout, file_path, threshold)


def _parse_clang_tidy_output(
    output: str, file_path: str, threshold: int
) -> Optional[FileComplexity]:
    fn_re = re.compile(
        r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): warning: function '(?P<name>[^']+)' has cognitive complexity of (?P<score>\d+)"
    )
    note_re = re.compile(
        r"^(?P<file>.+?):(?P<line>\d+):(?P<col>\d+): note: \+(?P<inc>\d+), (?P<reason>[^\[]+)"
    )
    functions: List[FunctionComplexity] = []
    current_fn: Optional[FunctionComplexity] = None

    for raw_line in output.splitlines():
        m_fn = fn_re.match(raw_line)
        if m_fn:
            line_no = int(m_fn.group("line"))
            score = int(m_fn.group("score"))
            current_fn = FunctionComplexity(
                name=m_fn.group("name"),
                class_name=None,
                line_number=line_no,
                end_line_number=line_no,
                complexity=score,
                exceeds_threshold=score > threshold,
            )
            functions.append(current_fn)
            continue
        m_note = note_re.match(raw_line)
        if m_note and current_fn is not None:
            inc_val = int(m_note.group("inc"))
            current_fn.breakdown.append(
                ComplexityIncrement(
                    line=int(m_note.group("line")),
                    column=int(m_note.group("col")),
                    node_type="clang_tidy",
                    increment=inc_val,
                    nesting=0,
                    reason=m_note.group("reason").strip(),
                )
            )

    if not functions:
        return None
    total = sum(f.complexity for f in functions)
    avg = total / len(functions)
    highest = max(f.complexity for f in functions)
    return FileComplexity(
        path=file_path,
        total_complexity=total,
        average_complexity=avg,
        highest_complexity=highest,
        functions=functions,
    )


class CppComplexityAnalyzer:
    """Parses C/C++ source code and extracts cognitive complexity for all functions/methods."""

    def __init__(self, threshold: int = 15, use_clang_tidy: bool = False) -> None:
        self.threshold = threshold
        self.use_clang_tidy = use_clang_tidy

    def analyze_source(
        self, source_code: str, file_path: str = "<stdin>"
    ) -> FileComplexity:
        if self.use_clang_tidy:
            tidy_result = _try_clang_tidy_analysis(file_path, self.threshold)
            if tidy_result is not None:
                return tidy_result

        parser = _get_cpp_parser()
        tree = parser.parse(source_code.encode("utf-8"))
        functions: List[FunctionComplexity] = []
        self._collect_functions(tree.root_node, scope_prefix=None, functions=functions)

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
        node: Any,
        scope_prefix: Optional[str],
        functions: List[FunctionComplexity],
    ) -> None:
        for child in node.named_children:
            if child.type in ("class_specifier", "struct_specifier", "union_specifier"):
                next_scope = self._extend_scope(scope_prefix, child)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect_functions(body, next_scope, functions)
            elif child.type == "namespace_definition":
                next_scope = self._extend_scope(scope_prefix, child)
                body = child.child_by_field_name("body")
                if body is not None:
                    self._collect_functions(body, next_scope, functions)
            elif child.type == "function_definition":
                fn_comp = self._analyze_function_node(child, scope_prefix)
                if fn_comp is not None:
                    functions.append(fn_comp)
            elif child.type in (
                "template_declaration",
                "linkage_specification",
                "declaration_list",
            ):
                self._collect_functions(child, scope_prefix, functions)

    def _extend_scope(
        self, current_scope: Optional[str], container_node: Any
    ) -> Optional[str]:
        name_node = container_node.child_by_field_name("name")
        if name_node is None:
            return current_scope
        name_text = _node_text(name_node)
        if not name_text:
            return current_scope
        return f"{current_scope}::{name_text}" if current_scope else name_text

    def _analyze_function_node(
        self, node: Any, scope_prefix: Optional[str]
    ) -> Optional[FunctionComplexity]:
        body = node.child_by_field_name("body")
        if body is None:
            return None
        func_decl = _find_function_declarator(node)
        if func_decl is None:
            return None

        qual_scope, func_name = _extract_declarator_name(func_decl)
        class_name = self._combine_scopes(scope_prefix, qual_scope)

        visitor = CppFunctionComplexityVisitor(
            function_name=func_name, threshold=self.threshold
        )
        visitor.visit(body, nesting=0)

        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        return FunctionComplexity(
            name=func_name,
            class_name=class_name,
            line_number=start_line,
            end_line_number=end_line,
            complexity=visitor.complexity,
            exceeds_threshold=visitor.complexity > self.threshold,
            breakdown=visitor.breakdown,
        )

    def _combine_scopes(
        self, outer_scope: Optional[str], decl_scope: Optional[str]
    ) -> Optional[str]:
        if outer_scope and decl_scope:
            return f"{outer_scope}::{decl_scope}"
        return decl_scope or outer_scope


def _format_function_entry(
    func: FunctionComplexity, threshold: int, verbose: bool
) -> List[str]:
    qualified_name = f"{func.class_name + '::' if func.class_name else ''}{func.name}"
    status = f"[EXCEEDS THRESHOLD {threshold}]" if func.exceeds_threshold else "[PASS]"
    lines = [
        f"  {qualified_name} (lines {func.line_number}-{func.end_line_number}) -> Complexity: {func.complexity} {status}"
    ]
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
            lines.extend(
                _format_function_entry(func, report.summary["threshold"], verbose)
            )

    s = report.summary
    lines.extend(
        [
            "\n" + "-" * 60,
            "Summary:",
            f"  Files analyzed:                {s['total_files']}",
            f"  Total functions:               {s['total_functions']}",
            f"  Total complexity:              {s['total_complexity']}",
            f"  Average complexity:            {s['average_complexity']:.2f}",
            f"  Highest complexity:            {s['highest_complexity']}",
            f"  Functions exceeding threshold: {s['functions_exceeding_threshold']} (threshold: {s['threshold']})",
            "=" * 60,
        ]
    )
    return "\n".join(lines)


def format_table_report(report: ComplexityReport) -> str:
    lines = [
        f"{'Function':<35} {'File':<25} {'Lines':<12} {'Complexity':<12} {'Status'}",
        "-" * 88,
    ]
    for file_comp in report.files:
        for func in file_comp.functions:
            q_name = f"{func.class_name + '::' if func.class_name else ''}{func.name}"
            q_display = (q_name[:30] + "...") if len(q_name) > 33 else q_name
            file_display = Path(file_comp.path).name
            f_display = (
                (file_display[:20] + "...") if len(file_display) > 23 else file_display
            )
            status = "WARN" if func.exceeds_threshold else "OK"
            lines.append(
                f"{q_display:<35} {f_display:<25} {f'{func.line_number}-{func.end_line_number}':<12} {func.complexity:<12} {status}"
            )
    return "\n".join(lines)


def format_summary_report(report: ComplexityReport) -> str:
    s = report.summary
    return (
        f"Files: {s['total_files']}, Functions: {s['total_functions']}, "
        f"Total Complexity: {s['total_complexity']}, Avg: {s['average_complexity']:.2f}, "
        f"Over Threshold: {s['functions_exceeding_threshold']}"
    )


def _sort_file_functions(files: List[FileComplexity], sort_key: str) -> None:
    for file_comp in files:
        if sort_key == "complexity":
            file_comp.functions.sort(key=lambda f: f.complexity, reverse=True)
        elif sort_key == "name":
            file_comp.functions.sort(key=lambda f: f.name)
        elif sort_key == "line":
            file_comp.functions.sort(key=lambda f: f.line_number)


def _is_matching_cpp_file(
    candidate: Path, exclude_patterns: Optional[List[str]]
) -> bool:
    if not candidate.is_file() or candidate.suffix.lower() not in CPP_EXTENSIONS:
        return False
    if not exclude_patterns:
        return True
    return not any(candidate.match(pat) for pat in exclude_patterns)


def _collect_cpp_files(p: Path, exclude_patterns: Optional[List[str]]) -> List[Path]:
    if p.is_file():
        return [p]
    if not p.is_dir():
        return []
    return [
        c for c in sorted(p.rglob("*")) if _is_matching_cpp_file(c, exclude_patterns)
    ]


def _analyze_single_path(
    path_str: str,
    analyzer: CppComplexityAnalyzer,
    exclude_patterns: Optional[List[str]],
    seen_paths: Set[str],
    file_results: List[FileComplexity],
) -> None:
    if path_str == "-" or not path_str:
        if "<stdin>" not in seen_paths:
            file_results.append(
                analyzer.analyze_source(sys.stdin.read(), file_path="<stdin>")
            )
            seen_paths.add("<stdin>")
        return

    p = Path(path_str)
    if not p.exists():
        print(f"Error: Path does not exist: {path_str}", file=sys.stderr)
        return

    for cpp_file in _collect_cpp_files(p, exclude_patterns):
        _analyze_cpp_file(cpp_file, analyzer, seen_paths, file_results)


def _analyze_cpp_file(
    cpp_file: Path,
    analyzer: CppComplexityAnalyzer,
    seen_paths: Set[str],
    file_results: List[FileComplexity],
) -> None:
    resolved_key = str(cpp_file.resolve())
    if resolved_key in seen_paths:
        return
    seen_paths.add(resolved_key)
    try:
        content = cpp_file.read_text(encoding="utf-8", errors="replace")
        file_results.append(analyzer.analyze_source(content, file_path=str(cpp_file)))
    except Exception as e:
        print(f"Error reading {cpp_file}: {e}", file=sys.stderr)


def analyze_paths(
    paths: Sequence[str],
    threshold: int = 15,
    sort_key: str = "complexity",
    exclude_patterns: Optional[List[str]] = None,
    use_clang_tidy: bool = False,
) -> ComplexityReport:
    analyzer = CppComplexityAnalyzer(threshold=threshold, use_clang_tidy=use_clang_tidy)
    file_results: List[FileComplexity] = []
    seen_paths: Set[str] = set()
    target_paths = list(paths) if paths else ["-"]

    for path_str in target_paths:
        _analyze_single_path(
            path_str, analyzer, exclude_patterns, seen_paths, file_results
        )

    _sort_file_functions(file_results, sort_key)

    total_files = len(file_results)
    total_funcs = sum(len(f.functions) for f in file_results)
    total_complexity = sum(f.total_complexity for f in file_results)
    avg_complexity = (total_complexity / total_funcs) if total_funcs else 0.0
    highest_complexity = max((f.highest_complexity for f in file_results), default=0)
    exceeding_count = sum(
        1 for f in file_results for func in f.functions if func.exceeds_threshold
    )

    summary = {
        "total_files": total_files,
        "total_functions": total_funcs,
        "total_complexity": total_complexity,
        "average_complexity": round(avg_complexity, 2),
        "highest_complexity": highest_complexity,
        "functions_exceeding_threshold": exceeding_count,
        "threshold": threshold,
    }

    return ComplexityReport(
        version="1.0.0", language="cpp", files=file_results, summary=summary
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Calculate Cognitive Complexity for C/C++ code according to SonarSource standard."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["-"],
        help="Paths to analyze (files or directories, default: stdin).",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=["text", "json", "table", "summary"],
        default="text",
        help="Output format.",
    )
    parser.add_argument(
        "-t",
        "--threshold",
        type=int,
        default=15,
        help="Threshold for flagging complexity.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed breakdown."
    )
    parser.add_argument(
        "-s",
        "--sort",
        choices=["complexity", "name", "line", "file"],
        default="complexity",
        help="Sort criteria.",
    )
    parser.add_argument(
        "-e", "--exclude", action="append", help="Glob patterns to exclude."
    )
    parser.add_argument(
        "--clang-tidy",
        action="store_true",
        help="Delegate to clang-tidy when compile_commands.json is available.",
    )
    parser.add_argument("-V", "--version", action="version", version="%(prog)s 1.0.0")

    args = parser.parse_args()

    try:
        report = analyze_paths(
            paths=args.paths,
            threshold=args.threshold,
            sort_key=args.sort,
            exclude_patterns=args.exclude,
            use_clang_tidy=args.clang_tidy,
        )
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
