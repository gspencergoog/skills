"""Python stdlib `ast` symbol parser for packrat."""

from __future__ import annotations

import ast
import io
import tokenize
import warnings
from typing import Any

from symbol_parsers import ParsedFile, ParsedSymbol, split_lines

PYTHON_PARSER_ID = "py-ast@1"
_ENUM_BASE_NAMES = frozenset({"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"})


def _python_visibility(name: str) -> str:
    """Returns conventional Python visibility: private for single/mangled `_name`, public otherwise."""
    if name.startswith("__") and name.endswith("__"):
        return "public"
    if name.startswith("_"):
        return "private"
    return "public"


def _base_expr_name(expr: ast.expr) -> str:
    """Extracts the trailing identifier from a base class AST expression."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return ""


def _is_enum_class(node: ast.ClassDef) -> bool:
    """True if `node` inherits from a standard Enum class."""
    return any(_base_expr_name(base) in _ENUM_BASE_NAMES for base in node.bases)


def _node_line_start(node: ast.AST) -> int:
    """Returns 1-based start line including leading decorators when present."""
    decorators = getattr(node, "decorator_list", ()) or ()
    lines = [getattr(node, "lineno", 1)]
    for dec in decorators:
        lines.append(getattr(dec, "lineno", lines[0]))
    return max(1, min(lines))


def _node_line_end(node: ast.AST, line_start: int) -> int:
    """Returns 1-based inclusive end line clamped to >= `line_start`."""
    end_line = getattr(node, "end_lineno", None) or getattr(node, "lineno", line_start)
    return max(line_start, int(end_line))


def _collapse_ws(text: str) -> str:
    """Collapses runs of whitespace into single spaces."""
    return " ".join(text.split())


def _header_signature(lines: list[str], node: ast.AST, fallback: str) -> str:
    """Extracts a single-line declaration header ending before its body opener `:`."""
    start_idx = max(0, int(getattr(node, "lineno", 1)) - 1)
    max_end = int(getattr(node, "end_lineno", start_idx + 1) or (start_idx + 1))
    snippet = "\n".join(lines[start_idx:max_end])
    depth = 0
    end_pos: tuple[int, int] | None = None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(snippet).readline):
            if tok.type == tokenize.OP:
                if tok.string in "([{":
                    depth += 1
                elif tok.string in ")]}":
                    depth = max(0, depth - 1)
                elif tok.string == ":" and depth == 0:
                    end_pos = tok.start
                    break
    except tokenize.TokenError:
        end_pos = None
    if end_pos is None:
        return _collapse_ws(lines[start_idx]) if start_idx < len(lines) else fallback
    rel_line, rel_col = end_pos
    parts = snippet.split("\n")[:rel_line]
    parts[rel_line - 1] = parts[rel_line - 1][:rel_col]
    return _collapse_ws(" ".join(parts)) or fallback


def _assignment_signature(lines: list[str], node: ast.AST, name: str) -> str:
    """Builds a single-line collapsed signature for a TypeAlias, AnnAssign, or Assign."""
    l_start = max(1, int(getattr(node, "lineno", 1)))
    l_end = max(l_start, int(getattr(node, "end_lineno", l_start) or l_start))
    joined = _collapse_ws(" ".join(lines[l_start - 1 : min(l_end, l_start + 3)]))
    return joined or name


def _func_kind(node: ast.FunctionDef | ast.AsyncFunctionDef, *, in_class: bool) -> str:
    """Classifies a Python function or method (including `@property` and setter decorators)."""
    if not in_class:
        return "function"
    for dec in node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "property":
            return "getter"
        if isinstance(dec, ast.Attribute) and dec.attr == "setter":
            return "setter"
    return "method"


def _is_typealias_annotation(node: ast.AnnAssign) -> bool:
    """True when `AnnAssign` uses `: TypeAlias = ...` or `: typing.TypeAlias = ...`."""
    ann = node.annotation
    if isinstance(ann, ast.Name):
        return ann.id == "TypeAlias"
    return isinstance(ann, ast.Attribute) and ann.attr == "TypeAlias"


def _simple_assign_targets(node: ast.AST) -> list[str]:
    """Extracts identifier names from `Assign`, `AnnAssign`, or `TypeAlias` statements."""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.Assign):
        names: list[str] = []
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.append(target.id)
        return names
    type_alias_cls = getattr(ast, "TypeAlias", None)
    if type_alias_cls is not None and isinstance(node, type_alias_cls):
        return [node.name.id]
    return []


def _symbol_from_assign(
    lines: list[str],
    node: ast.AST,
    name: str,
    *,
    parent: str,
    kind: str,
) -> ParsedSymbol:
    """Builds a `ParsedSymbol` for a module variable, class field, enum value, or typedef."""
    l_start = _node_line_start(node)
    l_end = _node_line_end(node, l_start)
    qname = f"{parent}.{name}" if parent else name
    return ParsedSymbol(
        kind=kind,
        name=name,
        qualified_name=qname,
        parent=parent,
        signature=_assignment_signature(lines, node, name)[:500],
        docstring="",
        line_start=l_start,
        line_end=l_end,
        visibility=_python_visibility(name),
    )


def _symbol_from_def(
    lines: list[str],
    node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    parent: str,
    kind: str,
) -> ParsedSymbol:
    """Builds a `ParsedSymbol` for a class, enum, function, method, getter, or setter."""
    l_start = _node_line_start(node)
    l_end = _node_line_end(node, l_start)
    qname = f"{parent}.{node.name}" if parent else node.name
    doc = (ast.get_docstring(node, clean=True) or "").strip()
    return ParsedSymbol(
        kind=kind,
        name=node.name,
        qualified_name=qname,
        parent=parent,
        signature=_header_signature(lines, node, node.name)[:500],
        docstring=doc[:4000],
        line_start=l_start,
        line_end=l_end,
        visibility=_python_visibility(node.name),
    )


def _assign_kind(node: ast.AST, name: str, *, in_class: bool, in_enum: bool) -> str:
    """Classifies a Python TypeAlias, AnnAssign, or Assign target."""
    if in_enum and not (name.startswith("__") and name.endswith("__")):
        return "enum_value"
    type_alias_cls = getattr(ast, "TypeAlias", None)
    if type_alias_cls is not None and isinstance(node, type_alias_cls):
        return "typedef"
    if isinstance(node, ast.AnnAssign) and _is_typealias_annotation(node):
        return "typedef"
    return "field" if in_class else "variable"


def _append_assign_symbols(
    lines: list[str],
    node: ast.AST,
    *,
    parent: str,
    in_class: bool,
    in_enum: bool,
    out: list[ParsedSymbol],
) -> None:
    """Appends variable, field, enum value, or typedef symbols from an assignment node."""
    for name in _simple_assign_targets(node):
        kind = _assign_kind(node, name, in_class=in_class, in_enum=in_enum)
        out.append(_symbol_from_assign(lines, node, name, parent=parent, kind=kind))


def _collect_class_members(
    lines: list[str], cls_node: ast.ClassDef, cls_qname: str, out: list[ParsedSymbol]
) -> None:
    """Collects direct methods, getters/setters, fields/enum values, and nested classes."""
    is_enum = _is_enum_class(cls_node)
    for child in cls_node.body:
        if isinstance(child, ast.ClassDef):
            nested_kind = "enum" if _is_enum_class(child) else "class"
            sym = _symbol_from_def(lines, child, parent=cls_qname, kind=nested_kind)
            out.append(sym)
            _collect_class_members(lines, child, sym.qualified_name, out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = _func_kind(child, in_class=True)
            out.append(_symbol_from_def(lines, child, parent=cls_qname, kind=kind))
        else:
            _append_assign_symbols(
                lines, child, parent=cls_qname, in_class=True, in_enum=is_enum, out=out
            )


def _collect_module_symbols(lines: list[str], tree: ast.Module) -> list[ParsedSymbol]:
    """Collects top-level declarations and type members from `tree`, omitting nested defs."""
    out: list[ParsedSymbol] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            kind = "enum" if _is_enum_class(node) else "class"
            sym = _symbol_from_def(lines, node, parent="", kind=kind)
            out.append(sym)
            _collect_class_members(lines, node, sym.qualified_name, out)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(_symbol_from_def(lines, node, parent="", kind="function"))
        else:
            _append_assign_symbols(
                lines, node, parent="", in_class=False, in_enum=False, out=out
            )
    return out


def parse_python(path: str, text: str) -> ParsedFile:
    """Parses Python source using `ast.parse`, reporting syntax errors cleanly."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            tree = ast.parse(text, filename=path)
        except SyntaxError:
            return ParsedFile(
                parser_id=PYTHON_PARSER_ID,
                symbols=(),
                syntax_error_count=1,
                dropped_symbol_count=0,
            )
    lines = split_lines(text)
    symbols = tuple(_collect_module_symbols(lines, tree))
    return ParsedFile(
        parser_id=PYTHON_PARSER_ID,
        symbols=symbols,
        syntax_error_count=0,
        dropped_symbol_count=0,
    )
