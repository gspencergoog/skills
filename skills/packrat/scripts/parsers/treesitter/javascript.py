"""JavaScript (`.js`, `.jsx`, `.mjs`) symbol visitor for packrat's tree-sitter worker."""

from __future__ import annotations

from typing import Any

from typescript import extract_typescript_symbols


def extract_javascript_symbols(root_node: Any, data: bytes) -> list[dict[str, Any]]:
    """Extracts symbols from a parsed JavaScript or JSX syntax tree."""
    return extract_typescript_symbols(root_node, data)
