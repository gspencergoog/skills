"""Kotlin (`.kt`) symbol visitor for packrat's tree-sitter worker."""

from __future__ import annotations

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

_KT_VISIBILITIES = frozenset({"public", "private", "protected", "internal"})
_BODY_TYPES = frozenset({"class_body", "enum_class_body", "function_body", "block"})
_RECEIVER_TYPES = frozenset(
    {"user_type", "nullable_type", "parenthesized_type", "function_type", "dynamic_type"}
)


def _kt_visibility(node: Any, data: bytes) -> str:
    """Returns Kotlin declaration visibility (defaults to `public` when unannotated)."""
    for child in node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.type == "visibility_modifier":
                    val = node_text(data, mod).strip()
                    if val in _KT_VISIBILITIES:
                        return val
    return "public"


def _has_class_modifier(node: Any, data: bytes, target: str) -> bool:
    """Returns whether `node` has `target` (e.g. `enum`) in its `modifiers` child."""
    for child in node.children:
        if child.type == "modifiers":
            for mod in child.children:
                if mod.type == "class_modifier" and node_text(data, mod).strip() == target:
                    return True
    return False


def _kt_class_kind(node: Any, data: bytes) -> str:
    """Classifies `class_declaration` as `interface`, `enum`, or `class`."""
    if any(not c.is_named and c.type == "interface" for c in node.children):
        return "interface"
    if _has_class_modifier(node, data, "enum") or any(
        c.type == "enum_class_body" for c in node.children
    ):
        return "enum"
    return "class"


def _first_identifier(node: Any, data: bytes) -> str:
    """Returns the text of the first `identifier` or `type_identifier` child of `node`."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier"):
            return node_text(data, child).strip()
    return ""


def _kt_header_end(node: Any) -> int:
    """Returns the byte offset before the body of a Kotlin declaration."""
    for child in node.children:
        if child.type in _BODY_TYPES or child.type in ("getter", "setter"):
            return child.start_byte
    return node.end_byte


def _base_type_name(type_node: Any, data: bytes) -> str:
    """Extracts the unqualified receiver type identifier (e.g. `List<T>` -> `List`)."""
    for child in type_node.children:
        if child.type in ("identifier", "type_identifier"):
            return node_text(data, child).strip()
        if child.type in _RECEIVER_TYPES:
            inner = _base_type_name(child, data)
            if inner:
                return inner
    raw = node_text(data, type_node).strip().rstrip("?")
    return raw.split("<", 1)[0].rsplit(".", 1)[-1].strip()


def _split_extension_target(node: Any, data: bytes) -> tuple[str, str]:
    """Returns `(receiver_type, member_name)` for a Kotlin function or property."""
    children = node.children
    for idx, child in enumerate(children):
        if not child.is_named and child.type == "." and idx > 0:
            prev = children[idx - 1]
            if prev.type in _RECEIVER_TYPES:
                receiver = _base_type_name(prev, data)
                tail_name = _member_after_dot(children[idx + 1 :], data)
                if receiver and tail_name:
                    return receiver, tail_name
    return "", _non_extension_name(node, data)


def _member_after_dot(after: list[Any], data: bytes) -> str:
    for item in after:
        if item.type in ("identifier", "type_identifier"):
            return node_text(data, item).strip()
        if item.type == "variable_declaration":
            return _first_identifier(item, data)
    return ""


def _non_extension_name(node: Any, data: bytes) -> str:
    for child in node.children:
        if child.type == "variable_declaration":
            return _first_identifier(child, data)
        if child.type in ("identifier", "type_identifier"):
            return node_text(data, child).strip()
    return ""


class _KotlinVisitor:
    """Walks a `tree-sitter-kotlin` syntax tree to collect symbols."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.index = LineIndex(data)
        self.symbols: list[dict[str, Any]] = []

    def visit_root(self, root: Any) -> list[dict[str, Any]]:
        self._visit_declarations(root.children, parent="")
        return self.symbols

    def _emit(
        self,
        *,
        kind: str,
        name: str,
        parent: str,
        node: Any,
        sig_end: int,
        visibility: str,
        qname_override: str = "",
        doc_override: str | None = None,
    ) -> str:
        l_start, l_end = self.index.span_lines(node.start_byte, node.end_byte)
        final_name = name or f"{kind}@L{l_start}"
        qname = qname_override or join_qualified(parent, final_name)
        doc = doc_override if doc_override is not None else extract_docstring(node, self.index)
        sig = slice_signature(self.data, node.start_byte, sig_end, final_name)
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

    def _visit_declarations(self, children: list[Any], parent: str) -> None:
        for child in children:
            if not child.is_named or child.type in COMMENT_NODE_TYPES:
                continue
            ctype = child.type
            if ctype == "class_declaration":
                self._visit_class_like(child, parent)
            elif ctype == "object_declaration":
                self._visit_object(child, parent, is_companion=False)
            elif ctype == "companion_object":
                self._visit_object(child, parent, is_companion=True)
            elif ctype == "type_alias":
                self._visit_type_alias(child, parent)
            elif ctype == "function_declaration":
                self._visit_function(child, parent)
            elif ctype == "property_declaration":
                self._visit_property(child, parent)
            elif ctype == "secondary_constructor" and parent:
                self._emit(
                    kind="constructor",
                    name="init",
                    parent=parent,
                    node=child,
                    sig_end=_kt_header_end(child),
                    visibility=_kt_visibility(child, self.data),
                )

    def _visit_class_like(self, node: Any, parent: str) -> None:
        kind = _kt_class_kind(node, self.data)
        name = _first_identifier(node, self.data)
        qname = self._emit(
            kind=kind,
            name=name,
            parent=parent,
            node=node,
            sig_end=_kt_header_end(node),
            visibility=_kt_visibility(node, self.data),
        )
        for child in node.children:
            if child.type == "primary_constructor":
                self._visit_primary_ctor(child, qname)
            elif child.type == "class_body":
                self._visit_declarations(child.children, parent=qname)
            elif child.type == "enum_class_body":
                self._visit_enum_body(child, qname)

    def _visit_primary_ctor(self, ctor_node: Any, cls_qname: str) -> None:
        self._emit(
            kind="constructor",
            name="init",
            parent=cls_qname,
            node=ctor_node,
            sig_end=ctor_node.end_byte,
            visibility=_kt_visibility(ctor_node, self.data),
            doc_override="",
        )
        params = next((c for c in ctor_node.children if c.type == "class_parameters"), None)
        if params is None:
            return
        for param in params.children:
            if param.type != "class_parameter":
                continue
            if not any(not c.is_named and c.type in ("val", "var") for c in param.children):
                continue
            pname = _first_identifier(param, self.data)
            if pname:
                self._emit(
                    kind="field",
                    name=pname,
                    parent=cls_qname,
                    node=param,
                    sig_end=param.end_byte,
                    visibility=_kt_visibility(param, self.data),
                )

    def _visit_enum_body(self, body: Any, enum_qname: str) -> None:
        for child in body.children:
            if child.type == "enum_entry":
                ename = _first_identifier(child, self.data)
                if ename:
                    self._emit(
                        kind="enum_value",
                        name=ename,
                        parent=enum_qname,
                        node=child,
                        sig_end=_kt_header_end(child),
                        visibility="public",
                    )
        self._visit_declarations(body.children, parent=enum_qname)

    def _visit_object(self, node: Any, parent: str, *, is_companion: bool) -> None:
        name = _first_identifier(node, self.data)
        if not name and is_companion:
            name = "Companion"
        qname = self._emit(
            kind="object",
            name=name,
            parent=parent,
            node=node,
            sig_end=_kt_header_end(node),
            visibility=_kt_visibility(node, self.data),
        )
        body = next((c for c in node.children if c.type == "class_body"), None)
        if body is not None:
            self._visit_declarations(body.children, parent=qname)

    def _visit_type_alias(self, node: Any, parent: str) -> None:
        name = _first_identifier(node, self.data)
        if name:
            self._emit(
                kind="typedef",
                name=name,
                parent=parent,
                node=node,
                sig_end=node.end_byte,
                visibility=_kt_visibility(node, self.data),
            )

    def _visit_function(self, node: Any, parent: str) -> None:
        receiver, name = _split_extension_target(node, self.data)
        if not name:
            return
        kind = "method" if parent else "function"
        qname = f"{receiver}.{name}" if receiver else join_qualified(parent, name)
        self._emit(
            kind=kind,
            name=name,
            parent=parent,
            node=node,
            sig_end=_kt_header_end(node),
            visibility=_kt_visibility(node, self.data),
            qname_override=qname,
        )

    def _visit_property(self, node: Any, parent: str) -> None:
        receiver, name = _split_extension_target(node, self.data)
        if not name:
            return
        vis = _kt_visibility(node, self.data)
        kind = "field" if parent else "variable"
        qname = f"{receiver}.{name}" if receiver else join_qualified(parent, name)
        self._emit(
            kind=kind,
            name=name,
            parent=parent,
            node=node,
            sig_end=_kt_header_end(node),
            visibility=vis,
            qname_override=qname,
        )
        for child in node.children:
            if child.type in ("getter", "setter"):
                acc_vis = _kt_visibility(child, self.data) if any(
                    c.type == "modifiers" for c in child.children
                ) else vis
                self._emit(
                    kind=child.type,
                    name=name,
                    parent=parent,
                    node=child,
                    sig_end=_kt_header_end(child),
                    visibility=acc_vis,
                    qname_override=qname,
                )


def extract_kotlin_symbols(root_node: Any, data: bytes) -> list[dict[str, Any]]:
    """Extracts symbols from a parsed Kotlin syntax tree."""
    return _KotlinVisitor(data).visit_root(root_node)
