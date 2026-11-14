"""Shared LF line indexing, docstring extraction, signature formatting, and caps."""

from __future__ import annotations

from bisect import bisect_right
from typing import Any

PARSER_NAME = "treesitter"
PARSER_ID = "treesitter@1"
PROTOCOL_VERSION = 1

MAX_SIGNATURE_CHARS = 500
MAX_DOCSTRING_CHARS = 4000
GENERATED_LINE_CHARS = 4000
GENERATED_BYTES_PER_LINE = 500
GENERATED_SYMBOL_CAP = 200
NORMAL_SYMBOL_CAP = 1000

PARSER_IDS: dict[str, str] = {
    ".c": PARSER_ID,
    ".cc": PARSER_ID,
    ".cpp": PARSER_ID,
    ".h": PARSER_ID,
    ".hpp": PARSER_ID,
    ".js": PARSER_ID,
    ".jsx": PARSER_ID,
    ".kt": PARSER_ID,
    ".mjs": PARSER_ID,
    ".ts": PARSER_ID,
    ".tsx": PARSER_ID,
}

COMMENT_NODE_TYPES: frozenset[str] = frozenset(
    {"comment", "block_comment", "line_comment", "multiline_comment"}
)
_TRAILING_WS_BYTES = b" \t\r\n"


class LineIndex:
    """Maps UTF-8 byte offsets to 1-based line numbers using LF (`\\n`) boundaries only."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self._lf_starts = [idx + 1 for idx, b in enumerate(data) if b == 10]

    def line_at(self, byte_offset: int) -> int:
        """Returns the 1-based LF line number containing `byte_offset`."""
        clamped = max(0, min(len(self.data), byte_offset))
        return bisect_right(self._lf_starts, clamped) + 1

    def span_lines(self, start_byte: int, end_byte: int) -> tuple[int, int]:
        """Returns `(line_start, line_end)` 1-based inclusive, trimming trailing whitespace."""
        l_start = self.line_at(start_byte)
        end = min(len(self.data), max(start_byte, end_byte))
        while end > start_byte + 1 and self.data[end - 1 : end] in _TRAILING_WS_BYTES:
            end -= 1
        last_byte = max(start_byte, end - 1)
        return l_start, max(l_start, self.line_at(last_byte))


def split_lf_lines(text: str) -> list[str]:
    """Splits `text` on LF (`\\n`) or CRLF (`\\r\\n`) only, ignoring form-feeds."""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def is_generated_source(text: str, data: bytes) -> bool:
    """Returns whether `text` exceeds the generated/minified line or density thresholds."""
    if not data:
        return False
    lines = split_lf_lines(text)
    if any(len(line) > GENERATED_LINE_CHARS for line in lines):
        return True
    line_count = len(lines)
    if text.endswith("\n") and line_count > 1:
        line_count -= 1
    return len(data) > max(1, line_count) * GENERATED_BYTES_PER_LINE


def finalize_symbols(
    text: str, data: bytes, symbols: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Applies the generated-file top-level filter and 200/1000 symbol caps in line order."""
    generated = is_generated_source(text, data)
    cap = GENERATED_SYMBOL_CAP if generated else NORMAL_SYMBOL_CAP
    kept: list[dict[str, Any]] = []
    dropped = 0
    for sym in symbols:
        if generated and sym["parent"] != "":
            dropped += 1
            continue
        kept.append(sym)
    kept.sort(
        key=lambda s: (
            s["line_start"],
            s["line_end"],
            s["qualified_name"],
            s["kind"],
        )
    )
    if len(kept) > cap:
        dropped += len(kept) - cap
        kept = kept[:cap]
    return kept, dropped


def collapse_ws(text: str) -> str:
    """Collapses runs of whitespace into single spaces."""
    return " ".join(text.split())


def node_text(data: bytes, node: Any) -> str:
    """Decodes the UTF-8 slice covered by `node`."""
    if node is None:
        return ""
    return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def slice_signature(
    data: bytes, start_byte: int, end_byte: int, fallback: str
) -> str:
    """Formats a single-line declaration header from `[start_byte, end_byte)`."""
    bounded_end = min(max(start_byte, end_byte), start_byte + 4096)
    raw = data[start_byte:bounded_end].decode("utf-8", errors="replace")
    cleaned = collapse_ws(raw).rstrip()
    while cleaned.endswith((";", "{")):
        cleaned = cleaned[:-1].rstrip()
    return (cleaned or fallback)[:MAX_SIGNATURE_CHARS]


def _clean_block_doc_line(line: str) -> str:
    stripped = line.strip()
    if stripped.startswith("*"):
        stripped = stripped[1:]
        if stripped.startswith(" "):
            stripped = stripped[1:]
    return stripped.rstrip()


def strip_block_doc(raw: str) -> str:
    """Strips `/** ... */` or `/*! ... */` delimiters and leading `*` prefixes."""
    body = raw[3:]
    if body.endswith("*/"):
        body = body[:-2]
    # Avoid treating `/**/` or `/***` divider banners with only asterisks as docstrings.
    if not body or all(ch == "*" for ch in body):
        return ""
    if body.startswith("*") and not body.startswith("*\n") and not body.startswith("*\r\n"):
        body = body.lstrip("*")
    lines = [_clean_block_doc_line(ln) for ln in split_lf_lines(body)]
    return "\n".join(lines).strip()[:MAX_DOCSTRING_CHARS]


def strip_slash_doc_lines(raw_lines: list[str]) -> str:
    """Strips `///` markers from contiguous triple-slash doc comment lines."""
    cleaned: list[str] = []
    for raw in raw_lines:
        body = raw.strip()
        if body.startswith("///"):
            body = body[3:]
            if body.startswith(" "):
                body = body[1:]
        cleaned.append(body.rstrip())
    return "\n".join(cleaned).strip()[:MAX_DOCSTRING_CHARS]


def _is_trailing_inline_comment(
    comment_node: Any, anchor_line: int, index: LineIndex
) -> bool:
    """Returns whether `comment_node` sits at the end of an earlier statement's line."""
    c_start, _ = index.span_lines(comment_node.start_byte, comment_node.end_byte)
    if c_start == anchor_line:
        return False
    prior = comment_node.prev_sibling
    if prior is None:
        return False
    _, prior_end = index.span_lines(prior.start_byte, prior.end_byte)
    return prior_end == c_start


def _collect_triple_slash(
    first_comment: Any, anchor_line: int, index: LineIndex
) -> str:
    """Collects contiguous preceding `///` comments immediately above `anchor_line`."""
    collected: list[str] = []
    next_line = anchor_line
    curr = first_comment
    while curr is not None and curr.type in COMMENT_NODE_TYPES:
        raw = node_text(index.data, curr).strip()
        c_start, c_end = index.span_lines(curr.start_byte, curr.end_byte)
        if (
            not raw.startswith("///")
            or raw.startswith("////")
            or next_line - c_end > 1
            or _is_trailing_inline_comment(curr, next_line, index)
        ):
            break
        collected.append(raw)
        next_line = c_start
        curr = curr.prev_sibling
    collected.reverse()
    return strip_slash_doc_lines(collected) if collected else ""


def extract_docstring(anchor_node: Any, index: LineIndex) -> str:
    """Extracts an attached `/**`, `/*!`, or `///` docstring immediately preceding `anchor_node`."""
    prev = anchor_node.prev_sibling
    if prev is None or prev.type not in COMMENT_NODE_TYPES:
        return ""
    anchor_line, _ = index.span_lines(anchor_node.start_byte, anchor_node.end_byte)
    _, c_end = index.span_lines(prev.start_byte, prev.end_byte)
    if anchor_line - c_end > 1 or _is_trailing_inline_comment(prev, anchor_line, index):
        return ""
    raw = node_text(index.data, prev).strip()
    if raw.startswith(("/**", "/*!")) and raw != "/**/":
        return strip_block_doc(raw)
    if raw.startswith("///") and not raw.startswith("////"):
        return _collect_triple_slash(prev, anchor_line, index)
    return ""


def count_syntax_errors(root_node: Any) -> int:
    """Counts `ERROR` and missing syntax nodes in `root_node` iteratively."""
    if not root_node.has_error:
        return 0
    count = 0
    stack = [root_node]
    while stack:
        node = stack.pop()
        if node.is_error or node.is_missing:
            count += 1
            continue
        if not node.has_error:
            continue
        stack.extend(reversed(node.children))
    return max(1, count)


def join_qualified(parent: str, name: str) -> str:
    """Joins `parent` and `name` with `.` when `parent` is non-empty."""
    return f"{parent}.{name}" if parent else name


def make_symbol(
    *,
    kind: str,
    name: str,
    qualified_name: str,
    parent: str,
    signature: str,
    docstring: str,
    line_start: int,
    line_end: int,
    visibility: str,
) -> dict[str, Any]:
    """Builds a wire-protocol symbol dictionary."""
    return {
        "kind": kind,
        "name": name,
        "qualified_name": qualified_name,
        "parent": parent,
        "signature": signature,
        "docstring": docstring,
        "line_start": line_start,
        "line_end": line_end,
        "visibility": visibility,
    }
