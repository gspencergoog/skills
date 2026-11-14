#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "tree-sitter>=0.25,<0.26",
#   "tree-sitter-cpp==0.23.4",
#   "tree-sitter-kotlin==1.1.0",
#   "tree-sitter-typescript==0.23.2",
#   "tree-sitter-javascript==0.25.0",
# ]
# ///
"""Packrat tree-sitter NDJSON symbol parser worker for C++, Kotlin, TypeScript, and JavaScript."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ProcessPoolExecutor
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Callable
import warnings

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from common import (  # noqa: E402
    PARSER_IDS,
    PARSER_NAME,
    PROTOCOL_VERSION,
    count_syntax_errors,
    finalize_symbols,
)
from cpp import extract_cpp_symbols  # noqa: E402
from javascript import extract_javascript_symbols  # noqa: E402
from kotlin import extract_kotlin_symbols  # noqa: E402
from typescript import extract_typescript_symbols  # noqa: E402

import tree_sitter  # noqa: E402
import tree_sitter_cpp  # noqa: E402
import tree_sitter_javascript  # noqa: E402
import tree_sitter_kotlin  # noqa: E402
import tree_sitter_typescript  # noqa: E402

DEFAULT_DEADLINE_MS = 5000
_READ_CHUNK_BYTES = 256

_WORKER_PARSERS: dict[str, tree_sitter.Parser] = {}

_EXT_TO_GRAMMAR: dict[str, tuple[str, Callable[[], object], Callable[[Any, bytes], list[dict[str, Any]]]]] = {
    ".c": ("cpp", tree_sitter_cpp.language, extract_cpp_symbols),
    ".cc": ("cpp", tree_sitter_cpp.language, extract_cpp_symbols),
    ".cpp": ("cpp", tree_sitter_cpp.language, extract_cpp_symbols),
    ".h": ("cpp", tree_sitter_cpp.language, extract_cpp_symbols),
    ".hpp": ("cpp", tree_sitter_cpp.language, extract_cpp_symbols),
    ".kt": ("kotlin", tree_sitter_kotlin.language, extract_kotlin_symbols),
    ".ts": ("typescript", tree_sitter_typescript.language_typescript, extract_typescript_symbols),
    ".tsx": ("tsx", tree_sitter_typescript.language_tsx, extract_typescript_symbols),
    ".js": ("javascript", tree_sitter_javascript.language, extract_javascript_symbols),
    ".jsx": ("javascript", tree_sitter_javascript.language, extract_javascript_symbols),
    ".mjs": ("javascript", tree_sitter_javascript.language, extract_javascript_symbols),
}


def _library_banner_string() -> str:
    """Formats the `tree-sitter` version banner field."""
    try:
        ver = importlib.metadata.version("tree-sitter")
    except importlib.metadata.PackageNotFoundError:
        ver = "0.25"
    return f"tree-sitter {ver}"


def _get_parser(grammar_key: str, lang_factory: Callable[[], object]) -> tree_sitter.Parser:
    """Returns a cached `tree_sitter.Parser` for `grammar_key` in the current process."""
    cached = _WORKER_PARSERS.get(grammar_key)
    if cached is None:
        cached = tree_sitter.Parser(tree_sitter.Language(lang_factory()))
        _WORKER_PARSERS[grammar_key] = cached
    return cached


def _parse_bytes_with_deadline(
    parser: tree_sitter.Parser, data: bytes, deadline_ms: int
) -> tree_sitter.Tree | None:
    """Parses `data` and returns `None` when `deadline_ms` expires.

    Note: In `py-tree-sitter` 0.25.0-0.25.2, passing `progress_callback` with a
    bytestring emits a `UserWarning` and ignores the callback, while passing
    `progress_callback` with a `read_callback` segfaults inside `_binding.so` on
    Python 3.13 when C invokes the callback. Checking `time.monotonic()` inside
    the chunked `read_callback` (backed up by `timeout_micros` while present on
    0.25.x) enforces per-file deadlines without segfaulting.
    """
    deadline = time.monotonic() + (max(1, int(deadline_ms)) / 1000.0)
    timed_out = False
    mv = memoryview(data)
    total = len(data)

    def read_chunk(offset: int, _point: tree_sitter.Point) -> bytes:
        nonlocal timed_out
        if time.monotonic() >= deadline:
            timed_out = True
            return b""
        if offset >= total:
            return b""
        return bytes(mv[offset : offset + _READ_CHUNK_BYTES])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        has_tm = hasattr(parser, "timeout_micros")
        if has_tm:
            parser.timeout_micros = max(1, int(deadline_ms) * 1000)
        try:
            tree = parser.parse(read_chunk)
        except ValueError:
            timed_out = True
            tree = None
        finally:
            parser.reset()
            if has_tm:
                parser.timeout_micros = 0

    if timed_out or tree is None or time.monotonic() >= deadline:
        return None
    return tree


def parse_file_request(
    req_id: Any, path: str, content: str, deadline_ms: int = DEFAULT_DEADLINE_MS
) -> dict[str, Any]:
    """Parses one source file and returns a wire-ready NDJSON response payload."""
    ext = Path(path).suffix.lower()
    entry = _EXT_TO_GRAMMAR.get(ext)
    if entry is None:
        return {"id": req_id, "error": f"unsupported-extension:{ext}"}
    grammar_key, lang_factory, extractor = entry
    data = content.encode("utf-8", errors="replace")
    parser = _get_parser(grammar_key, lang_factory)
    tree = _parse_bytes_with_deadline(parser, data, deadline_ms)
    if tree is None:
        return {"id": req_id, "error": "timeout"}
    raw_symbols = extractor(tree.root_node, data)
    syntax_errors = count_syntax_errors(tree.root_node)
    kept_symbols, dropped = finalize_symbols(content, data, raw_symbols)
    return {
        "id": req_id,
        "symbols": kept_symbols,
        "syntax_error_count": syntax_errors,
        "raw_symbol_count": len(raw_symbols),
        "dropped_symbol_count": dropped,
    }


def _decode_request_line(raw_line: bytes) -> tuple[Any, str, str, int] | dict[str, Any] | None:
    """Decodes one NDJSON request line or returns an immediate error payload."""
    stripped = raw_line.strip()
    if not stripped:
        return None
    req = json.loads(stripped.decode("utf-8"))
    if not isinstance(req, dict):
        raise ValueError("NDJSON request must be a JSON object")
    req_id = req.get("id")
    path = req.get("path")
    content = req.get("content")
    raw_deadline = req.get("deadline_ms", DEFAULT_DEADLINE_MS)
    if not isinstance(path, str) or not isinstance(content, str):
        return {"id": req_id, "error": "invalid-request"}
    deadline_ms = (
        int(raw_deadline)
        if isinstance(raw_deadline, int) and not isinstance(raw_deadline, bool) and raw_deadline > 0
        else DEFAULT_DEADLINE_MS
    )
    return req_id, path, content, deadline_ms


def _start_stdout_writer(
    out_queue: queue.Queue[dict[str, Any] | None], sem: threading.BoundedSemaphore
) -> threading.Thread:
    """Starts a background thread that serializes and flushes responses to `stdout`."""

    def _pump() -> None:
        while True:
            item = out_queue.get()
            if item is None:
                return
            try:
                sys.stdout.write(json.dumps(item, ensure_ascii=False) + "\n")
                sys.stdout.flush()
            finally:
                sem.release()

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    return thread


def _make_done_callback(
    req_id: Any, out_queue: queue.Queue[dict[str, Any] | None]
) -> Callable[[Future[dict[str, Any]]], None]:
    def _on_done(fut: Future[dict[str, Any]]) -> None:
        try:
            out_queue.put(fut.result())
        except BaseException as exc:
            out_queue.put({"id": req_id, "error": f"worker-exception:{type(exc).__name__}"})

    return _on_done


def run_worker() -> int:
    """Runs the long-lived NDJSON worker loop until `stdin` closes."""
    banner = {
        "ready": True,
        "parser": PARSER_NAME,
        "protocol": PROTOCOL_VERSION,
        "parser_ids": dict(sorted(PARSER_IDS.items())),
        "library": _library_banner_string(),
    }
    sys.stdout.write(json.dumps(banner) + "\n")
    sys.stdout.flush()

    max_workers = max(1, min(4, os.cpu_count() or 1))
    sem = threading.BoundedSemaphore(max_workers * 2)
    out_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
    writer_thread = _start_stdout_writer(out_queue, sem)

    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        while True:
            raw_line = sys.stdin.buffer.readline()
            if not raw_line:
                break
            decoded = _decode_request_line(raw_line)
            if decoded is None:
                continue
            sem.acquire()
            if isinstance(decoded, dict):
                out_queue.put(decoded)
                continue
            req_id, path, content, deadline_ms = decoded
            fut = pool.submit(parse_file_request, req_id, path, content, deadline_ms)
            fut.add_done_callback(_make_done_callback(req_id, out_queue))

    out_queue.put(None)
    writer_thread.join()
    return 0


def _collect_selftest_fixtures(fixtures_dir: Path) -> list[Path]:
    """Collects fixture source files under `fixtures_dir` in deterministic order."""
    if not fixtures_dir.is_dir():
        return []
    files: list[Path] = []
    for root, _, filenames in os.walk(fixtures_dir):
        root_path = Path(root)
        for fname in sorted(filenames):
            p = root_path / fname
            if p.suffix.lower() in _EXT_TO_GRAMMAR and p.is_file():
                files.append(p)
    return sorted(files)


def _check_selftest_fixture(fixture: Path, fixtures_dir: Path) -> dict[str, Any]:
    """Runs one fixture during `--selftest`, testing `deep_nesting.*` at 50 ms."""
    rel = fixture.relative_to(fixtures_dir).as_posix()
    text = fixture.read_text(encoding="utf-8")
    is_deep = fixture.parent.name == "conformance" and fixture.stem == "deep_nesting"
    is_syn = fixture.parent.name == "conformance" and fixture.stem == "syntax_error"
    deadline = 50 if is_deep else DEFAULT_DEADLINE_MS
    resp = parse_file_request(1, rel, text, deadline_ms=deadline)
    if is_deep:
        ok = resp.get("error") == "timeout"
    elif is_syn:
        ok = "error" not in resp and int(resp.get("syntax_error_count", 0)) >= 1
    else:
        ok = (
            "error" not in resp
            and int(resp.get("syntax_error_count", 0)) == 0
            and len(resp.get("symbols", ())) > 0
        )
    return {
        "fixture": rel,
        "ok": ok,
        "timed_out": resp.get("error") == "timeout",
        "symbol_count": len(resp.get("symbols", ())),
        "syntax_error_count": int(resp.get("syntax_error_count", 0)),
    }


def run_selftest() -> int:
    """Parses all files under `parsers/treesitter/fixtures/` and prints a JSON summary."""
    fixtures_dir = _THIS_DIR / "fixtures"
    fixtures = _collect_selftest_fixtures(fixtures_dir)
    results = [_check_selftest_fixture(f, fixtures_dir) for f in fixtures]
    ok = bool(results) and all(r["ok"] for r in results)
    summary = {
        "ok": ok,
        "parser": PARSER_NAME,
        "library": _library_banner_string(),
        "fixtures_checked": len(results),
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    """CLI entry point supporting `--worker` and `--selftest`."""
    ap = argparse.ArgumentParser(description="Packrat tree-sitter symbol parser.")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--worker", action="store_true", help="Run NDJSON worker loop.")
    group.add_argument("--selftest", action="store_true", help="Parse bundled fixtures.")
    args = ap.parse_args(argv)
    if args.worker:
        return run_worker()
    return run_selftest()


if __name__ == "__main__":
    raise SystemExit(main())
