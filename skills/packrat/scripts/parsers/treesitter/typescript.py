"""TypeScript (`.ts`, `.tsx`) symbol visitor for packrat's tree-sitter worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from common import (
    COMMENT_NODE_TYPES,
    LineIndex,
    extract_docstring,
    join_qualified,
    make_symbol,
    node_text,
    slice_signature,
)

_CLASS_TYPES = frozenset({"class_declaration", "abstract_class_declaration", "class"})
_FUNC_TYPES = frozenset(
    {"function_declaration", "generator_function_declaration", "function_signature", "function_expression"}
)
_METHOD_TYPES = frozenset(
    {"method_definition", "method_signature", "abstract_method_signature"}
)
_FIELD_TYPES = frozenset(
    {"public_field_definition", "field_definition", "property_signature"}
)
_VAR_DECL_TYPES = frozenset({"lexical_declaration", "variable_declaration"})
_NAMESPACE_TYPES = frozenset({"internal_module", "module"})
_ACCESS_MODIFIERS = frozenset({"public", "private", "protected"})


@dataclass(frozen=True)
class _DeclMeta:
    """Outer span, docstring anchor, and export flag for a TS/JS declaration."""

    anchor: Any
    start_byte: int
    end_byte: int
    exported: bool


def _child_modifier_visibility(node: Any, data: bytes) -> str:
    """Extracts an explicit `public`, `private`, or `protected` modifier from `node`."""
    for child in node.children:
        if child.type == "accessibility_modifier":
            mod = node_text(data, child).strip()
            if mod in _ACCESS_MODIFIERS:
                return mod
        elif child.type in _ACCESS_MODIFIERS:
            return child.type
    return ""


def _member_visibility(node: Any, name_node: Any, data: bytes) -> str:
    """Computes TS/JS member visibility (`#name` is `private`, default is `public`)."""
    if name_node is not None and name_node.type == "private_property_identifier":
        return "private"
    explicit = _child_modifier_visibility(node, data)
    return explicit or "public"


def _top_visibility(meta: _DeclMeta, node: Any, data: bytes) -> str:
    """Computes top-level or namespace member visibility."""
    explicit = _child_modifier_visibility(node, data)
    if explicit:
        return explicit
    return "public" if meta.exported else ""


def _header_end(node: Any, body_field: str = "body") -> int:
    """Returns byte offset right before `node`'s body block (or `node.end_byte`)."""
    body = node.child_by_field_name(body_field)
    return body.start_byte if body is not None else node.end_byte


def _member_name_node(member: Any) -> Any:
    """Returns the identifier node for a TS (`name`) or JS (`property`) member."""
    return member.child_by_field_name("name") or member.child_by_field_name("property")


def _method_kind(node: Any, leaf_name: str) -> str:
    """Classifies a TS/JS method, getter, setter, or constructor."""
    for child in node.children:
        if not child.is_named:
            if child.type == "get":
                return "getter"
            if child.type == "set":
                return "setter"
    if leaf_name == "constructor":
        return "constructor"
    return "method"


def _pattern_next_nodes(node: Any) -> list[Any]:
    """Returns child pattern nodes to inspect during destructuring traversal."""
    ntype = node.type
    if ntype == "pair_pattern":
        val = node.child_by_field_name("value")
        return [val] if val is not None else []
    if ntype == "assignment_pattern":
        left = node.child_by_field_name("left")
        return [left] if left is not None else []
    return list(node.named_children)


def _extract_pattern_names(pattern_node: Any, data: bytes) -> list[str]:
    """Extracts bound identifier names from an identifier or destructuring pattern."""
    if pattern_node is None:
        return []
    names: list[str] = []
    stack = [pattern_node]
    while stack:
        curr = stack.pop()
        if curr.type in ("identifier", "shorthand_property_identifier_pattern"):
            name = node_text(data, curr).strip()
            if name:
                names.append(name)
        else:
            stack.extend(reversed(_pattern_next_nodes(curr)))
    return names


def _var_sig_end(declarator: Any, decl_end: int) -> int:
    """Trims arrow/function/object bodies from variable signatures."""
    val = declarator.child_by_field_name("value")
    if val is None:
        return decl_end
    if val.type == "arrow_function":
        body = val.child_by_field_name("body")
        if body is not None:
            end = body.start_byte
            return end
    if val.type in ("function_expression", "generator_function", "object", "class"):
        body = val.child_by_field_name("body")
        return body.start_byte if body is not None else val.start_byte
    return decl_end


class _TsVisitor:
    """Walks TypeScript, TSX, or JavaScript syntax trees to collect declarations."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.index = LineIndex(data)
        self.symbols: list[dict[str, Any]] = []

    def visit_root(self, root: Any) -> list[dict[str, Any]]:
        self._visit_statement_list(root.children, parent="")
        return self.symbols

    def _emit(
        self,
        *,
        kind: str,
        name: str,
        parent: str,
        meta: _DeclMeta,
        sig_end: int,
        visibility: str,
        qname_override: str = "",
    ) -> str:
        l_start, l_end = self.index.span_lines(meta.start_byte, meta.end_byte)
        final_name = name or f"{kind}@L{l_start}"
        qname = qname_override or join_qualified(parent, final_name)
        doc = extract_docstring(meta.anchor, self.index)
        sig = slice_signature(self.data, meta.start_byte, sig_end, final_name)
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

    def _unwrap_outer(
        self, node: Any, pending_decorators: list[Any]
    ) -> tuple[Any, _DeclMeta] | None:
        anchor = pending_decorators[0] if pending_decorators else node
        start_byte = anchor.start_byte
        end_byte = node.end_byte
        exported = False
        curr = node
        while curr.type in ("export_statement", "ambient_declaration"):
            if curr.type == "export_statement":
                exported = True
            inner = curr.child_by_field_name("declaration") or curr.child_by_field_name("value")
            if inner is None:
                inner = next(
                    (
                        c
                        for c in curr.named_children
                        if c.type != "decorator" and c.type not in COMMENT_NODE_TYPES
                    ),
                    None,
                )
            if inner is None:
                return None
            curr = inner
        return curr, _DeclMeta(
            anchor=anchor, start_byte=start_byte, end_byte=end_byte, exported=exported
        )

    def _visit_statement_list(self, children: list[Any], parent: str) -> None:
        pending_decs: list[Any] = []
        for child in children:
            if child.type == "decorator":
                pending_decs.append(child)
                continue
            if child.type in COMMENT_NODE_TYPES:
                continue
            unwrapped = self._unwrap_outer(child, pending_decs)
            pending_decs.clear()
            if unwrapped is not None:
                self._dispatch_top_decl(unwrapped[0], unwrapped[1], parent)

    def _dispatch_top_decl(self, node: Any, meta: _DeclMeta, parent: str) -> None:
        ntype = node.type
        if ntype in _CLASS_TYPES:
            self._visit_class(node, meta, parent)
        elif ntype == "interface_declaration":
            self._visit_interface(node, meta, parent)
        elif ntype == "enum_declaration":
            self._visit_enum(node, meta, parent)
        elif ntype in _NAMESPACE_TYPES:
            self._visit_namespace(node, meta, parent)
        elif ntype == "type_alias_declaration":
            name = node_text(self.data, node.child_by_field_name("name")).strip()
            if name:
                self._emit(
                    kind="typedef",
                    name=name,
                    parent=parent,
                    meta=meta,
                    sig_end=node.end_byte,
                    visibility=_top_visibility(meta, node, self.data),
                )
        elif ntype in _FUNC_TYPES:
            name = node_text(self.data, node.child_by_field_name("name")).strip()
            if name or meta.exported:
                self._emit(
                    kind="function",
                    name=name,
                    parent=parent,
                    meta=meta,
                    sig_end=_header_end(node),
                    visibility=_top_visibility(meta, node, self.data),
                )
        elif ntype in _VAR_DECL_TYPES:
            self._visit_variables(node, meta, parent)

    def _visit_variables(self, node: Any, meta: _DeclMeta, parent: str) -> None:
        vis = _top_visibility(meta, node, self.data)
        for decl in node.named_children:
            if decl.type != "variable_declarator":
                continue
            sig_end = _var_sig_end(decl, node.end_byte)
            for name in _extract_pattern_names(decl.child_by_field_name("name"), self.data):
                self._emit(
                    kind="variable",
                    name=name,
                    parent=parent,
                    meta=meta,
                    sig_end=sig_end,
                    visibility=vis,
                )

    def _visit_namespace(self, node: Any, meta: _DeclMeta, parent: str) -> None:
        raw_name = node_text(self.data, node.child_by_field_name("name")).strip().strip("\"'")
        if not raw_name:
            return
        qname = join_qualified(parent, raw_name)
        leaf = raw_name.rsplit(".", 1)[-1]
        self._emit(
            kind="namespace",
            name=leaf,
            parent=parent,
            meta=meta,
            sig_end=_header_end(node),
            visibility=_top_visibility(meta, node, self.data),
            qname_override=qname,
        )
        body = node.child_by_field_name("body")
        if body is None:
            return
        if body.type in _NAMESPACE_TYPES:
            sub_meta = _DeclMeta(
                anchor=body,
                start_byte=body.start_byte,
                end_byte=body.end_byte,
                exported=True,
            )
            self._visit_namespace(body, sub_meta, qname)
        else:
            self._visit_statement_list(body.children, parent=qname)

    def _visit_class(self, node: Any, meta: _DeclMeta, parent: str) -> None:
        name = node_text(self.data, node.child_by_field_name("name")).strip()
        cls_qname = self._emit(
            kind="class",
            name=name,
            parent=parent,
            meta=meta,
            sig_end=_header_end(node),
            visibility=_top_visibility(meta, node, self.data),
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_type_members(body.children, cls_qname)

    def _visit_interface(self, node: Any, meta: _DeclMeta, parent: str) -> None:
        name = node_text(self.data, node.child_by_field_name("name")).strip()
        if not name:
            return
        iface_qname = self._emit(
            kind="interface",
            name=name,
            parent=parent,
            meta=meta,
            sig_end=_header_end(node),
            visibility=_top_visibility(meta, node, self.data),
        )
        body = node.child_by_field_name("body")
        if body is not None:
            self._visit_type_members(body.children, iface_qname)

    def _visit_type_members(self, children: list[Any], owner_qname: str) -> None:
        pending_decs: list[Any] = []
        for child in children:
            if child.type == "decorator":
                pending_decs.append(child)
                continue
            if child.type in COMMENT_NODE_TYPES or not child.is_named:
                continue
            anchor = pending_decs[0] if pending_decs else child
            meta = _DeclMeta(
                anchor=anchor,
                start_byte=anchor.start_byte,
                end_byte=child.end_byte,
                exported=False,
            )
            pending_decs.clear()
            self._visit_single_member(child, meta, owner_qname)

    def _visit_single_member(self, member: Any, meta: _DeclMeta, owner_qname: str) -> None:
        mtype = member.type
        if mtype == "construct_signature":
            self._emit(
                kind="constructor",
                name="new",
                parent=owner_qname,
                meta=meta,
                sig_end=member.end_byte,
                visibility="public",
            )
            return
        if mtype not in _METHOD_TYPES and mtype not in _FIELD_TYPES:
            return
        name_node = _member_name_node(member)
        leaf = node_text(self.data, name_node).strip()
        if not leaf:
            return
        vis = _member_visibility(member, name_node, self.data)
        if mtype in _METHOD_TYPES:
            kind = _method_kind(member, leaf)
            sig_end = _header_end(member)
        else:
            kind = "field"
            sig_end = _var_sig_end(member, member.end_byte)
        self._emit(
            kind=kind,
            name=leaf,
            parent=owner_qname,
            meta=meta,
            sig_end=sig_end,
            visibility=vis,
        )

    def _visit_enum(self, node: Any, meta: _DeclMeta, parent: str) -> None:
        name = node_text(self.data, node.child_by_field_name("name")).strip()
        if not name:
            return
        enum_qname = self._emit(
            kind="enum",
            name=name,
            parent=parent,
            meta=meta,
            sig_end=_header_end(node),
            visibility=_top_visibility(meta, node, self.data),
        )
        body = node.child_by_field_name("body")
        if body is None:
            return
        for item in body.named_children:
            if item.type in COMMENT_NODE_TYPES:
                continue
            target = item.child_by_field_name("name") if item.type == "enum_assignment" else item
            val_name = node_text(self.data, target).strip().strip("\"'")
            if not val_name:
                continue
            item_meta = _DeclMeta(
                anchor=item,
                start_byte=item.start_byte,
                end_byte=item.end_byte,
                exported=False,
            )
            self._emit(
                kind="enum_value",
                name=val_name,
                parent=enum_qname,
                meta=item_meta,
                sig_end=item.end_byte,
                visibility="public",
            )


def extract_typescript_symbols(root_node: Any, data: bytes) -> list[dict[str, Any]]:
    """Extracts symbols from a parsed TypeScript or TSX syntax tree."""
    return _TsVisitor(data).visit_root(root_node)
