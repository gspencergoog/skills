"""C and C++ (`.c`, `.cc`, `.cpp`, `.h`, `.hpp`) symbol visitor for packrat."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from common import (
    COMMENT_NODE_TYPES,
    LineIndex,
    collapse_ws,
    extract_docstring,
    join_qualified,
    make_symbol,
    node_text,
    slice_signature,
)

_DECLARATOR_WRAPPERS = frozenset(
    {
        "pointer_declarator",
        "reference_declarator",
        "parenthesized_declarator",
        "attributed_declarator",
        "array_declarator",
        "init_declarator",
    }
)
_LEAF_NAME_TYPES = frozenset(
    {
        "identifier",
        "field_identifier",
        "type_identifier",
        "destructor_name",
        "operator_name",
    }
)
_CLASS_SPEC_TYPES = frozenset({"class_specifier", "struct_specifier", "union_specifier"})
_TEMPLATE_ARGS_RE = re.compile(r"<[^<>]*>")


@dataclass(frozen=True)
class _CppDeclInfo:
    """Unwrapped C/C++ declarator name, qualification, and function flag."""

    leaf_name: str
    qualified_tail: str
    is_function: bool


def _strip_template_args(text: str) -> str:
    """Removes `<...>` template argument clauses before splitting `::`."""
    prev = None
    curr = text
    while prev != curr:
        prev = curr
        curr = _TEMPLATE_ARGS_RE.sub("", curr)
    return curr


def _normalize_cpp_qname(raw: str) -> tuple[str, str]:
    """Maps C++ `::` segments to `.` and returns `(leaf_name, dotted_path)`."""
    cleaned = collapse_ws(raw).strip()
    if "::" not in cleaned:
        return cleaned, cleaned
    prefix, leaf = cleaned.rsplit("::", 1)
    clean_prefix = ".".join(
        seg.strip() for seg in _strip_template_args(prefix).split("::") if seg.strip()
    )
    leaf = leaf.strip()
    if leaf.startswith("operator"):
        leaf = leaf.replace(" ", "")
    elif "<" in leaf:
        leaf = _strip_template_args(leaf).strip()
    dotted = f"{clean_prefix}.{leaf}" if clean_prefix else leaf
    return leaf, dotted


def _is_fn_pointer_inner(inner: Any) -> bool:
    """Detects `(*fp)(args)` so function-pointer variables are not marked as functions."""
    if inner is None or inner.type != "parenthesized_declarator":
        return False
    return any(
        c.type in ("pointer_declarator", "reference_declarator")
        for c in inner.named_children
    )


def _step_declarator_wrapper(curr: Any, is_func: bool) -> tuple[Any | None, bool, bool]:
    """Advances one wrapper layer around a C/C++ declarator."""
    ctype = curr.type
    if ctype == "function_declarator":
        inner = curr.child_by_field_name("declarator")
        return inner, (is_func or not _is_fn_pointer_inner(inner)), True
    if ctype in _DECLARATOR_WRAPPERS:
        nxt = curr.child_by_field_name("declarator")
        if nxt is None:
            nxt = next(iter(curr.named_children), None)
        return nxt, is_func, True
    if ctype == "template_function":
        return curr.child_by_field_name("name"), is_func, True
    return curr, is_func, False


def _leaf_declarator_info(curr: Any, data: bytes, is_func: bool) -> _CppDeclInfo | None:
    """Builds `_CppDeclInfo` from an unwrapped leaf or qualified C/C++ declarator."""
    if curr is None:
        return None
    if curr.type == "qualified_identifier":
        leaf, dotted = _normalize_cpp_qname(node_text(data, curr))
        return _CppDeclInfo(leaf_name=leaf, qualified_tail=dotted, is_function=is_func)
    if curr.type in _LEAF_NAME_TYPES:
        raw = collapse_ws(node_text(data, curr)).strip()
        if curr.type == "operator_name":
            raw = raw.replace(" ", "")
        return _CppDeclInfo(leaf_name=raw, qualified_tail=raw, is_function=is_func)
    return None


def _unwrap_declarator(node: Any, data: bytes) -> _CppDeclInfo | None:
    """Unwraps C/C++ declarator wrappers to extract `(leaf_name, qualified_tail, is_function)`."""
    curr = node
    is_func = False
    while curr is not None:
        curr, is_func, stepped = _step_declarator_wrapper(curr, is_func)
        if not stepped:
            break
    return _leaf_declarator_info(curr, data, is_func)


def _namespace_path(ns_node: Any, data: bytes) -> tuple[str, str]:
    """Returns `(leaf_name, dotted_segments)` for a C++ `namespace_definition`."""
    name_node = ns_node.child_by_field_name("name")
    if name_node is None:
        return "(anonymous)", "(anonymous)"
    raw = node_text(data, name_node).strip()
    parts = [p.strip() for p in raw.split("::") if p.strip()]
    if not parts:
        return "(anonymous)", "(anonymous)"
    return parts[-1], ".".join(parts)


def _cpp_header_end(node: Any) -> int:
    """Returns byte offset before `{` body of a C/C++ compound declaration."""
    body = node.child_by_field_name("body")
    return body.start_byte if body is not None else node.end_byte


def _has_friend_specifier(node: Any, data: bytes) -> bool:
    """Returns whether `node` is a `friend` declaration inside a class."""
    for child in node.children:
        if child.type == "friend_declaration":
            return True
        if not child.is_named and node_text(data, child).strip() == "friend":
            return True
    return False


class _CppVisitor:
    """Walks a `tree-sitter-cpp` syntax tree to collect C and C++ symbols."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.index = LineIndex(data)
        self.symbols: list[dict[str, Any]] = []

    def visit_root(self, root: Any) -> list[dict[str, Any]]:
        self._visit_list(root.children, parent="", in_class=False, class_leaf="", vis="")
        return self.symbols

    def _emit(
        self,
        *,
        kind: str,
        name: str,
        parent: str,
        outer: Any,
        sig_end: int,
        visibility: str,
        qname_override: str = "",
    ) -> str:
        l_start, l_end = self.index.span_lines(outer.start_byte, outer.end_byte)
        final_name = name or f"{kind}@L{l_start}"
        qname = qname_override or join_qualified(parent, final_name)
        doc = extract_docstring(outer, self.index)
        sig = slice_signature(self.data, outer.start_byte, sig_end, final_name)
        self.symbols.append(
            make_symbol(
                kind=kind,
                name=final_name,
                qualified_name=qname,
                parent=parent,
                signature=sig,
                docstring=doc,
                line_start=l_start,
                line_end=l_end,
                visibility=visibility,
            )
        )
        return qname

    def _visit_list(
        self,
        children: list[Any],
        *,
        parent: str,
        in_class: bool,
        class_leaf: str,
        vis: str,
    ) -> None:
        curr_vis = vis
        for child in children:
            if not child.is_named or child.type in COMMENT_NODE_TYPES:
                continue
            if child.type == "access_specifier":
                spec = node_text(self.data, child).strip().rstrip(":")
                if spec in ("public", "protected", "private"):
                    curr_vis = spec
                continue
            self._visit_node(
                child,
                outer=child,
                parent=parent,
                in_class=in_class,
                class_leaf=class_leaf,
                vis=curr_vis,
            )

    def _visit_template(
        self,
        node: Any,
        outer: Any,
        parent: str,
        in_class: bool,
        class_leaf: str,
        vis: str,
    ) -> None:
        for child in node.named_children:
            if child.type != "template_parameter_list" and child.type not in COMMENT_NODE_TYPES:
                self._visit_node(
                    child,
                    outer=outer,
                    parent=parent,
                    in_class=in_class,
                    class_leaf=class_leaf,
                    vis=vis,
                )
                return

    def _visit_linkage(
        self,
        node: Any,
        outer: Any,
        parent: str,
        in_class: bool,
        class_leaf: str,
        vis: str,
    ) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            return
        if body.type == "declaration_list":
            self._visit_list(
                body.children, parent=parent, in_class=in_class, class_leaf=class_leaf, vis=vis
            )
        else:
            self._visit_node(
                body, outer=outer, parent=parent, in_class=in_class, class_leaf=class_leaf, vis=vis
            )

    def _visit_node(
        self,
        node: Any,
        *,
        outer: Any,
        parent: str,
        in_class: bool,
        class_leaf: str,
        vis: str,
    ) -> None:
        ntype = node.type
        if ntype == "friend_declaration" or _has_friend_specifier(node, self.data):
            return
        if ntype == "template_declaration":
            self._visit_template(node, outer, parent, in_class, class_leaf, vis)
            return
        if ntype == "linkage_specification":
            self._visit_linkage(node, outer, parent, in_class, class_leaf, vis)
            return
        if ntype == "namespace_definition":
            self._visit_namespace(node, outer, parent)
            return
        if ntype in _CLASS_SPEC_TYPES:
            self._visit_class_or_struct(node, outer, parent, vis)
            return
        simple = {
            "enum_specifier": self._visit_enum,
            "alias_declaration": self._visit_using_alias,
            "type_definition": self._visit_typedef,
        }.get(ntype)
        if simple is not None:
            simple(node, outer, parent, vis)
        elif ntype == "function_definition":
            self._visit_func_def(node, outer, parent, in_class, class_leaf, vis)
        elif ntype in ("declaration", "field_declaration"):
            self._visit_decl_stmt(node, outer, parent, in_class, class_leaf, vis)

    def _visit_namespace(self, node: Any, outer: Any, parent: str) -> None:
        leaf, dotted = _namespace_path(node, self.data)
        qname = join_qualified(parent, dotted)
        self._emit(
            kind="namespace",
            name=leaf,
            parent=parent,
            outer=outer,
            sig_end=_cpp_header_end(node),
            visibility="",
            qname_override=qname,
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_list(body.children, parent=qname, in_class=False, class_leaf="", vis="")

    def _visit_class_or_struct(self, node: Any, outer: Any, parent: str, vis: str) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            return
        kind = "class" if node.type == "class_specifier" else "struct"
        default_member_vis = "private" if kind == "class" else "public"
        raw_name = node_text(self.data, node.child_by_field_name("name")).strip()
        leaf, dotted = _normalize_cpp_qname(raw_name) if raw_name else ("", "")
        qname_override = join_qualified(parent, dotted) if dotted else ""
        qname = self._emit(
            kind=kind,
            name=leaf,
            parent=parent,
            outer=outer,
            sig_end=body.start_byte,
            visibility=vis,
            qname_override=qname_override,
        )
        effective_leaf = leaf or qname.rsplit(".", 1)[-1]
        self._visit_list(
            body.children,
            parent=qname,
            in_class=True,
            class_leaf=effective_leaf,
            vis=default_member_vis,
        )

    def _emit_enum_values(self, body: Any, enum_qname: str, member_vis: str) -> None:
        for item in body.named_children:
            if item.type != "enumerator":
                continue
            ename = node_text(self.data, item.child_by_field_name("name")).strip()
            if ename:
                self._emit(
                    kind="enum_value",
                    name=ename,
                    parent=enum_qname,
                    outer=item,
                    sig_end=item.end_byte,
                    visibility=member_vis,
                )

    def _visit_enum(self, node: Any, outer: Any, parent: str, vis: str) -> None:
        body = node.child_by_field_name("body")
        if body is None:
            return
        name = node_text(self.data, node.child_by_field_name("name")).strip()
        enum_qname = self._emit(
            kind="enum",
            name=name,
            parent=parent,
            outer=outer,
            sig_end=body.start_byte,
            visibility=vis,
        )
        self._emit_enum_values(body, enum_qname, vis or "public")

    def _visit_using_alias(self, node: Any, outer: Any, parent: str, vis: str) -> None:
        name = node_text(self.data, node.child_by_field_name("name")).strip()
        if name:
            self._emit(
                kind="typedef",
                name=name,
                parent=parent,
                outer=outer,
                sig_end=node.end_byte,
                visibility=vis,
            )

    def _visit_typedef(self, node: Any, outer: Any, parent: str, vis: str) -> None:
        type_spec = node.child_by_field_name("type")
        decls = node.children_by_field_name("declarator")
        first_alias = ""
        for decl in decls:
            info = _unwrap_declarator(decl, self.data)
            if info is None or not info.leaf_name:
                continue
            first_alias = first_alias or info.leaf_name
            self._emit(
                kind="typedef",
                name=info.leaf_name,
                parent=parent,
                outer=outer,
                sig_end=node.end_byte,
                visibility=vis,
            )
        if type_spec is not None and type_spec.child_by_field_name("body") is not None:
            self._visit_typedef_inline_tag(type_spec, first_alias, parent, vis)

    def _visit_typedef_inline_tag(
        self, type_spec: Any, alias_name: str, parent: str, vis: str
    ) -> None:
        tag_name = node_text(self.data, type_spec.child_by_field_name("name")).strip()
        if tag_name:
            self._visit_node(
                type_spec, outer=type_spec, parent=parent, in_class=False, class_leaf="", vis=vis
            )
            return
        body = type_spec.child_by_field_name("body")
        if not alias_name or body is None:
            return
        owner_qname = join_qualified(parent, alias_name)
        if type_spec.type in _CLASS_SPEC_TYPES:
            default_vis = "private" if type_spec.type == "class_specifier" else "public"
            self._visit_list(
                body.children,
                parent=owner_qname,
                in_class=True,
                class_leaf=alias_name,
                vis=default_vis,
            )
        elif type_spec.type == "enum_specifier":
            self._emit_enum_values(body, owner_qname, vis or "public")

    def _classify_declarator(
        self, info: _CppDeclInfo, *, in_class: bool, class_leaf: str
    ) -> str:
        is_qualified = "." in info.qualified_tail
        if info.is_function:
            if in_class and info.leaf_name == class_leaf:
                return "constructor"
            if is_qualified:
                owner_seg = info.qualified_tail.rsplit(".", 2)[-2]
                if info.leaf_name == owner_seg:
                    return "constructor"
                return "method"
            return "method" if in_class else "function"
        return "field" if (in_class or is_qualified) else "variable"

    def _visit_func_def(
        self,
        node: Any,
        outer: Any,
        parent: str,
        in_class: bool,
        class_leaf: str,
        vis: str,
    ) -> None:
        info = _unwrap_declarator(node.child_by_field_name("declarator"), self.data)
        if info is None or not info.leaf_name:
            return
        forced = _CppDeclInfo(
            leaf_name=info.leaf_name, qualified_tail=info.qualified_tail, is_function=True
        )
        kind = self._classify_declarator(forced, in_class=in_class, class_leaf=class_leaf)
        qname = join_qualified(parent, forced.qualified_tail)
        self._emit(
            kind=kind,
            name=forced.leaf_name,
            parent=parent,
            outer=outer,
            sig_end=_cpp_header_end(node),
            visibility=vis,
            qname_override=qname,
        )

    def _visit_decl_stmt(
        self,
        node: Any,
        outer: Any,
        parent: str,
        in_class: bool,
        class_leaf: str,
        vis: str,
    ) -> None:
        type_spec = node.child_by_field_name("type")
        if type_spec is not None and type_spec.type in (*_CLASS_SPEC_TYPES, "enum_specifier"):
            if type_spec.child_by_field_name("body") is not None:
                self._visit_node(
                    type_spec,
                    outer=outer,
                    parent=parent,
                    in_class=in_class,
                    class_leaf=class_leaf,
                    vis=vis,
                )
        for decl in node.children_by_field_name("declarator"):
            info = _unwrap_declarator(decl, self.data)
            if info is None or not info.leaf_name:
                continue
            kind = self._classify_declarator(info, in_class=in_class, class_leaf=class_leaf)
            qname = join_qualified(parent, info.qualified_tail)
            self._emit(
                kind=kind,
                name=info.leaf_name,
                parent=parent,
                outer=outer,
                sig_end=node.end_byte,
                visibility=vis,
                qname_override=qname,
            )


def extract_cpp_symbols(root_node: Any, data: bytes) -> list[dict[str, Any]]:
    """Extracts symbols from a parsed C or C++ syntax tree."""
    return _CppVisitor(data).visit_root(root_node)
