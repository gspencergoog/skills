"""Session-scoped multi-language symbol parser orchestrator and NDJSON worker driver."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from blob_parsers import extract_content_entities
from models import CODE_EXTENSIONS, MARKDOWN_EXTENSIONS, SYMBOL_KINDS
from parsers.stamps import Stamp, binary_name, host_platform, read_stamp
from progress import NULL_REPORTER, ProgressReporter

PROTOCOL_VERSION = 1
REGEX_PARSER_ID = "regex@1"
MARKDOWN_PARSER_ID = "md-sections@1"

MAX_SIGNATURE_CHARS = 500
MAX_DOCSTRING_CHARS = 4000
GENERATED_LINE_CHARS = 4000
GENERATED_BYTES_PER_LINE = 500
GENERATED_SYMBOL_CAP = 200
NORMAL_SYMBOL_CAP = 1000
MAX_NDJSON_LINE_BYTES = 32 * 1024 * 1024
BANNER_TIMEOUT_SEC = 10.0
DRAIN_TIMEOUT_SEC = 5.0
TERM_GRACE_SEC = 2.0
MAX_WORKER_RSS_BYTES = 2 * 1024 * 1024 * 1024
_STDERR_RING_BYTES = 4096

_VALID_KINDS = frozenset(SYMBOL_KINDS)
_VALID_VISIBILITIES = frozenset(
    {"", "public", "private", "protected", "internal", "package"}
)

ROUTING: dict[str, str] = {
    ".dart": "dart-analyzer",
    ".swift": "swift-syntax",
    ".py": "py-ast",
    ".ts": "treesitter",
    ".tsx": "treesitter",
    ".js": "treesitter",
    ".jsx": "treesitter",
    ".mjs": "treesitter",
    ".kt": "treesitter",
    ".c": "treesitter",
    ".cc": "treesitter",
    ".cpp": "treesitter",
    ".h": "treesitter",
    ".hpp": "treesitter",
    ".go": "regex",
    ".rs": "regex",
    ".java": "regex",
    ".md": "md-sections",
    ".mdx": "md-sections",
}
"""Extension-to-parser routing table covering `CODE_EXTENSIONS` and `MARKDOWN_EXTENSIONS`."""


@dataclass(frozen=True)
class ParsedSymbol:
    """Normalized symbol declaration emitted by a parser or fallback scanner."""

    kind: str
    name: str
    qualified_name: str
    parent: str
    signature: str
    docstring: str
    line_start: int
    line_end: int
    visibility: str


@dataclass(frozen=True)
class ParsedFile:
    """Per-file output from a parser session including counts of tolerated errors/drops."""

    parser_id: str
    symbols: tuple[ParsedSymbol, ...]
    syntax_error_count: int = 0
    dropped_symbol_count: int = 0


@dataclass(frozen=True)
class DiscoveredParser:
    """Discovered parser runtime metadata resolved at session initialization."""

    parser: str
    parser_dir: Path
    stamp: Stamp
    command: tuple[str, ...] | None


def split_lines(text: str) -> list[str]:
    """Splits `text` on LF (`\\n`) or CRLF (`\\r\\n`) only."""
    return [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]


def _is_strict_int(val: Any) -> bool:
    """Returns True when `val` is an int and not a bool."""
    return isinstance(val, int) and not isinstance(val, bool)


def _decode_line_bounds(raw: Mapping[str, Any]) -> tuple[int, int] | None:
    """Validates `line_start >= 1` and `line_end >= line_start` as strict ints."""
    l_start = raw.get("line_start")
    l_end = raw.get("line_end")
    if not _is_strict_int(l_start) or not _is_strict_int(l_end):
        return None
    if l_start < 1 or l_end < l_start:
        return None
    return int(l_start), int(l_end)


def decode_symbol(raw: Mapping[str, Any]) -> ParsedSymbol | None:
    """Validates, normalizes, and caps one raw symbol dict per the wire rule table."""
    if not isinstance(raw, Mapping):
        return None
    kind = raw.get("kind")
    raw_qname = raw.get("qualified_name")
    bounds = _decode_line_bounds(raw)
    if (
        not isinstance(kind, str)
        or kind not in _VALID_KINDS
        or not isinstance(raw_qname, str)
        or not raw_qname.strip()
        or bounds is None
    ):
        return None
    qname = raw_qname.strip()
    raw_name = raw.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else qname.rsplit(".", 1)[-1]
    raw_parent = raw.get("parent")
    parent = raw_parent.strip() if isinstance(raw_parent, str) else ""
    raw_sig = raw.get("signature")
    collapsed_sig = " ".join(str(raw_sig).split()) if raw_sig is not None else ""
    signature = (collapsed_sig or name)[:MAX_SIGNATURE_CHARS]
    raw_doc = raw.get("docstring")
    docstring = raw_doc.strip()[:MAX_DOCSTRING_CHARS] if isinstance(raw_doc, str) else ""
    raw_vis = raw.get("visibility")
    vis = raw_vis.strip() if isinstance(raw_vis, str) else ""
    return ParsedSymbol(
        kind=kind,
        name=name,
        qualified_name=qname,
        parent=parent,
        signature=signature,
        docstring=docstring,
        line_start=bounds[0],
        line_end=bounds[1],
        visibility=vis if vis in _VALID_VISIBILITIES else "",
    )


def _is_generated_file(text: str, lines: Sequence[str]) -> bool:
    """True if any line > 4,000 chars or average line length > 500 bytes."""
    if not text:
        return False
    if any(len(line) > GENERATED_LINE_CHARS for line in lines):
        return True
    line_count = len(lines)
    if text.endswith("\n") and line_count > 1:
        line_count -= 1
    line_count = max(1, line_count)
    return len(text.encode("utf-8", errors="replace")) > line_count * GENERATED_BYTES_PER_LINE


def _finalize_symbols(
    text: str,
    symbols: Sequence[ParsedSymbol],
    *,
    pre_dropped: int = 0,
) -> tuple[tuple[ParsedSymbol, ...], int]:
    """Applies generated-file filtering and per-file symbol caps in line order."""
    lines = split_lines(text)
    generated = _is_generated_file(text, lines)
    cap = GENERATED_SYMBOL_CAP if generated else NORMAL_SYMBOL_CAP
    kept: list[ParsedSymbol] = []
    dropped = pre_dropped
    for sym in symbols:
        if generated and sym.parent != "":
            dropped += 1
            continue
        kept.append(sym)
    kept.sort(key=lambda s: (s.line_start, s.line_end, s.qualified_name, s.kind))
    if len(kept) > cap:
        dropped += len(kept) - cap
        kept = kept[:cap]
    return tuple(kept), dropped


def _regex_fallback(
    path: str, text: str, *, syntax_error_count: int = 0
) -> ParsedFile:
    """Parses `path` with the regex scanner and normalizes output through `decode_symbol`."""
    entities = extract_content_entities(path, text)
    raw_syms: list[ParsedSymbol] = []
    dropped = 0
    for ent in entities:
        name = str(ent.get("name") or ent.get("entity_name") or "").strip()
        sym = decode_symbol(
            {
                "kind": ent.get("kind") or ent.get("entity_kind") or "function",
                "name": name,
                "qualified_name": name,
                "parent": "",
                "signature": ent.get("signature") or name,
                "docstring": "",
                "line_start": int(ent.get("line_start") or ent.get("start_line") or 0),
                "line_end": int(ent.get("line_end") or ent.get("end_line") or 0),
                "visibility": "",
            }
        )
        if sym is None:
            dropped += 1
        else:
            raw_syms.append(sym)
    final_syms, total_dropped = _finalize_symbols(text, raw_syms, pre_dropped=dropped)
    return ParsedFile(
        parser_id=REGEX_PARSER_ID,
        symbols=final_syms,
        syntax_error_count=syntax_error_count,
        dropped_symbol_count=total_dropped,
    )


def _find_uv_executable() -> str:
    """Locates `uv` on PATH or `~/.local/bin/uv`."""
    found = shutil.which("uv")
    if found:
        return found
    local_uv = Path.home() / ".local" / "bin" / "uv"
    if local_uv.is_file():
        return str(local_uv)
    return "uv"


def _resolve_worker_command(
    parser_name: str, parser_dir: Path, host: str
) -> tuple[str, ...] | None:
    """Resolves the worker argv for compiled or script parsers without cold builds."""
    if parser_name == "py-ast":
        return None
    if parser_name == "treesitter":
        script = parser_dir / "symbol_parser.py"
        if not script.is_file():
            return None
        return (
            _find_uv_executable(),
            "run",
            "--offline",
            "--python",
            sys.executable,
            "--script",
            str(script),
            "--worker",
        )
    os_name = host.split("-", 1)[0]
    short = "dart" if parser_name == "dart-analyzer" else "swift"
    fallback_rel = (
        parser_dir / "build" / "packrat-dart"
        if parser_name == "dart-analyzer"
        else parser_dir / ".build" / "release" / "PackratSwift"
    )
    candidates = (
        parser_dir / binary_name(parser_name, host),
        parser_dir / f"packrat-{short}-{os_name}",
        fallback_rel,
    )
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return (str(cand), "--worker")
    return None


def _discover_parsers(scripts_dir: Path, host: str) -> dict[str, DiscoveredParser]:
    """Discovers available parser runtimes from `scripts_dir/parsers/*/stamp.json`."""
    parsers_dir = Path(scripts_dir) / "parsers"
    if not parsers_dir.is_dir():
        return {}
    discovered: dict[str, DiscoveredParser] = {}
    for sub in sorted(parsers_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith((".", "__")):
            continue
        stamp = read_stamp(sub)
        if stamp is None:
            continue
        if stamp.parser == "py-ast":
            if (sub / "symbol_parser.py").is_file():
                discovered[stamp.parser] = DiscoveredParser(
                    parser=stamp.parser, parser_dir=sub, stamp=stamp, command=None
                )
            continue
        cmd = _resolve_worker_command(stamp.parser, sub, host)
        if cmd is not None:
            discovered[stamp.parser] = DiscoveredParser(
                parser=stamp.parser, parser_dir=sub, stamp=stamp, command=cmd
            )
    return discovered


class _WorkerCrashedError(RuntimeError):
    """Raised when an NDJSON worker crashes, times out on a batch, or violates framing."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


class _WorkerUnavailableError(RuntimeError):
    """Raised when a worker fails startup banner / stamp verification."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


def _read_proc_rss_bytes(pid: int) -> int | None:
    """Reads resident set size in bytes on Linux `/proc/<pid>/statm`; None if unsupported."""
    statm = Path(f"/proc/{pid}/statm")
    try:
        parts = statm.read_text(encoding="ascii").split()
        if len(parts) < 2:
            return None
        return int(parts[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError):
        return None


class _NdjsonWorker:
    """Manages one long-lived NDJSON child process with bounded stderr and line framing."""

    def __init__(
        self,
        spec: DiscoveredParser,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        assert spec.command is not None
        self.spec = spec
        self._stderr_buf: deque[bytes] = deque()
        self._stderr_len = 0
        self._stdout_queue: queue.Queue[bytes | BaseException | None] = queue.Queue()
        self._proc = self._spawn_process(spec.command, spawn)
        self._stdout_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._validate_banner()

    @staticmethod
    def _spawn_process(
        cmd: tuple[str, ...],
        spawn: Callable[..., subprocess.Popen[bytes]] | None,
    ) -> subprocess.Popen[bytes]:
        factory = spawn if spawn is not None else subprocess.Popen
        try:
            return factory(
                list(cmd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise _WorkerUnavailableError("exec-error", str(exc)) from exc

    def _pump_stdout(self) -> None:
        stream = self._proc.stdout
        if stream is None:
            self._stdout_queue.put(None)
            return
        try:
            while True:
                raw = stream.readline(MAX_NDJSON_LINE_BYTES + 1)
                if not raw:
                    self._stdout_queue.put(None)
                    return
                if len(raw) > MAX_NDJSON_LINE_BYTES:
                    self._stdout_queue.put(
                        _WorkerCrashedError("line-too-large", "stdout line exceeds 32 MiB")
                    )
                    return
                self._stdout_queue.put(raw)
        except OSError as exc:
            self._stdout_queue.put(exc)

    def _pump_stderr(self) -> None:
        stream = self._proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    return
                self._stderr_buf.append(chunk)
                self._stderr_len += len(chunk)
                while self._stderr_len > _STDERR_RING_BYTES and self._stderr_buf:
                    dropped = self._stderr_buf.popleft()
                    self._stderr_len -= len(dropped)
        except OSError:
            return

    def stderr_tail(self) -> str:
        """Returns up to the last 4 KiB of decoded worker stderr."""
        return b"".join(self._stderr_buf).decode("utf-8", errors="replace").strip()

    def _read_json_line(
        self, timeout_sec: float, *, on_timeout_reason: str
    ) -> dict[str, Any]:
        try:
            item = self._stdout_queue.get(timeout=max(0.01, timeout_sec))
        except queue.Empty as exc:
            raise _WorkerCrashedError(
                on_timeout_reason, f"timed out after {timeout_sec:.1f}s"
            ) from exc
        if item is None:
            raise _WorkerCrashedError("worker-exit", "unexpected EOF on stdout")
        if isinstance(item, _WorkerCrashedError):
            raise item
        if isinstance(item, BaseException):
            raise _WorkerCrashedError("io-error", str(item)) from item
        try:
            decoded = json.loads(item.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _WorkerCrashedError("malformed-json", str(exc)) from exc
        if not isinstance(decoded, dict):
            raise _WorkerCrashedError("malformed-json", "NDJSON line is not an object")
        return decoded

    def _validate_banner(self) -> None:
        try:
            banner = self._read_json_line(
                BANNER_TIMEOUT_SEC, on_timeout_reason="spawn-timeout"
            )
        except _WorkerCrashedError as exc:
            self.kill()
            reason = "spawn-timeout" if exc.reason == "spawn-timeout" else "bad-banner"
            raise _WorkerUnavailableError(reason, f"{exc}; stderr={self.stderr_tail()}") from exc
        if banner.get("ready") is not True:
            self.kill()
            raise _WorkerUnavailableError("bad-banner", "banner missing ready=true")
        proto = banner.get("protocol")
        if not _is_strict_int(proto) or proto != PROTOCOL_VERSION:
            self.kill()
            raise _WorkerUnavailableError(
                "protocol-skew", f"expected protocol {PROTOCOL_VERSION}, got {proto!r}"
            )
        if banner.get("parser_ids") != dict(self.spec.stamp.parser_ids):
            self.kill()
            raise _WorkerUnavailableError(
                "stamp-mismatch",
                f"banner parser_ids {banner.get('parser_ids')!r} != {self.spec.stamp.parser_ids!r}",
            )

    def _write_batch(self, payloads: Sequence[bytes]) -> list[BaseException]:
        errors: list[BaseException] = []
        stream = self._proc.stdin
        if stream is None:
            return [OSError("worker stdin closed")]

        def _writer() -> None:
            try:
                for line in payloads:
                    stream.write(line)
                stream.flush()
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=_writer, daemon=True)
        thread.start()
        thread.join(timeout=max(5.0, 0.2 * len(payloads)))
        if thread.is_alive():
            errors.append(_WorkerCrashedError("write-timeout", "stdin write stalled"))
        return errors

    def request_batch(
        self, requests: Sequence[tuple[int, str, str]], deadline_ms: int
    ) -> tuple[dict[int, dict[str, Any]], _WorkerCrashedError | None]:
        """Sends `requests` and returns `(responses_by_id, crash_error_or_None)`."""
        payloads = [
            (
                json.dumps(
                    {
                        "id": req_id,
                        "path": path,
                        "content": text,
                        "deadline_ms": deadline_ms,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
            for req_id, path, text in requests
        ]
        pending = {req_id for req_id, _, _ in requests}
        responses: dict[int, dict[str, Any]] = {}
        write_errors = self._write_batch(payloads)
        if write_errors:
            err = write_errors[0]
            crash = (
                err
                if isinstance(err, _WorkerCrashedError)
                else _WorkerCrashedError("write-error", str(err))
            )
            return responses, crash
        batch_deadline = time.monotonic() + max(30.0, 0.2 * len(requests))
        while pending:
            remaining = batch_deadline - time.monotonic()
            if remaining <= 0:
                return responses, _WorkerCrashedError("batch-timeout", "batch deadline exceeded")
            try:
                msg = self._read_json_line(remaining, on_timeout_reason="batch-timeout")
            except _WorkerCrashedError as exc:
                return responses, exc
            msg_id = msg.get("id")
            if _is_strict_int(msg_id) and msg_id in pending:
                pending.remove(msg_id)
                responses[msg_id] = msg
        rss = _read_proc_rss_bytes(self._proc.pid)
        if rss is not None and rss > MAX_WORKER_RSS_BYTES:
            return responses, _WorkerCrashedError(
                "rss-limit", f"worker RSS {rss} exceeded 2 GiB cap"
            )
        return responses, None

    @staticmethod
    def _close_stream(stream: Any) -> None:
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def _escalate_wait(self) -> None:
        steps: tuple[tuple[Callable[[], Any], float], ...] = (
            (lambda: None, DRAIN_TIMEOUT_SEC),
            (self._proc.terminate, TERM_GRACE_SEC),
            (self._proc.kill, TERM_GRACE_SEC),
        )
        for action, limit in steps:
            if self._proc.poll() is not None:
                return
            try:
                action()
                self._proc.wait(timeout=limit)
                return
            except (subprocess.TimeoutExpired, OSError):
                continue

    def kill(self) -> None:
        """Force-terminates the child process immediately and closes pipes."""
        try:
            if self._proc.poll() is None:
                self._proc.kill()
                self._proc.wait(timeout=TERM_GRACE_SEC)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            self._close_stream(stream)

    def close(self) -> None:
        """Closes stdin to drain gracefully for 5 s, then SIGTERM, then SIGKILL."""
        self._close_stream(self._proc.stdin)
        self._escalate_wait()
        for thread in (self._stdout_thread, self._stderr_thread):
            thread.join(timeout=0.5)
        for stream in (self._proc.stdout, self._proc.stderr):
            self._close_stream(stream)


class SymbolParsers(AbstractContextManager["SymbolParsers"]):
    """Session-scoped dispatcher for in-process Python AST and NDJSON worker parsers."""

    def __init__(
        self,
        *,
        scripts_dir: Path | None = None,
        overrides: Mapping[str, str] | None = None,
        reporter: ProgressReporter = NULL_REPORTER,
        max_crashes_per_parser: int = 3,
        deadline_ms: int = 5000,
        spawn: Callable[..., subprocess.Popen[bytes]] | None = None,
    ) -> None:
        self._scripts_dir = (
            Path(scripts_dir) if scripts_dir is not None else Path(__file__).resolve().parent
        )
        self._overrides = dict(overrides or {})
        self._reporter = reporter
        self._max_crashes = max(0, int(max_crashes_per_parser))
        self._deadline_ms = max(1, int(deadline_ms))
        self.spawn_factory = spawn
        self._host = host_platform()
        self._discovered = _discover_parsers(self._scripts_dir, self._host)
        self._workers: dict[str, _NdjsonWorker] = {}
        self._crash_counts: dict[str, int] = {}
        self._unavailable: dict[str, str] = {}
        self._next_id = 1
        self._fingerprint = self._compute_fingerprint()

    def _is_enabled_discovered(self, parser_name: str) -> bool:
        return (
            parser_name in self._discovered
            and self._overrides.get(parser_name) != "disabled"
        )

    def _compute_fingerprint(self) -> str:
        parts: list[str] = []
        for ext, parser_name in sorted(ROUTING.items()):
            if not self._is_enabled_discovered(parser_name):
                continue
            pid = self._discovered[parser_name].stamp.parser_ids.get(ext)
            if pid:
                parts.append(f"{ext}={pid}")
        return ";".join(parts)

    def fingerprint(self) -> str:
        """Returns stable parser availability fingerprint from stamps minus overrides."""
        return self._fingerprint

    def unavailable(self) -> Mapping[str, str]:
        """Returns `{parser: reason}` for parsers in `fingerprint()` that became unavailable."""
        return dict(self._unavailable)

    def is_parser_active(self, parser_name: str) -> bool:
        """True if `parser_name` was discovered, not disabled, and has not become unavailable."""
        return (
            self._is_enabled_discovered(parser_name)
            and parser_name not in self._unavailable
        )

    def parser_id_for(self, path: str) -> str:
        """Returns the expected `parser_id` for `path` given discovery, overrides, and session state."""
        ext = Path(path).suffix.lower()
        parser_name = ROUTING.get(ext, "regex")
        if parser_name == "md-sections":
            return MARKDOWN_PARSER_ID
        if parser_name == "regex" or not self.is_parser_active(parser_name):
            return REGEX_PARSER_ID
        return self._discovered[parser_name].stamp.parser_ids.get(ext, REGEX_PARSER_ID)

    def _parse_python_file(self, path: str, text: str) -> ParsedFile:
        from parsers.python.symbol_parser import parse_python

        raw = parse_python(path, text)
        if raw.syntax_error_count > 0:
            return _regex_fallback(path, text, syntax_error_count=raw.syntax_error_count)
        syms, dropped = _finalize_symbols(text, raw.symbols, pre_dropped=raw.dropped_symbol_count)
        return ParsedFile(
            parser_id=raw.parser_id,
            symbols=syms,
            syntax_error_count=0,
            dropped_symbol_count=dropped,
        )

    def _decode_worker_response(
        self, path: str, text: str, expected_id: str, msg: Mapping[str, Any]
    ) -> ParsedFile:
        if "error" in msg:
            return _regex_fallback(path, text)
        raw_syms = msg.get("symbols")
        if not isinstance(raw_syms, list):
            return _regex_fallback(path, text)
        decoded: list[ParsedSymbol] = []
        dropped = 0
        for item in raw_syms:
            sym = decode_symbol(item)
            if sym is None:
                dropped += 1
            else:
                decoded.append(sym)
        raw_err = msg.get("syntax_error_count", 0)
        syntax_errors = int(raw_err) if _is_strict_int(raw_err) and raw_err >= 0 else 0
        final_syms, total_dropped = _finalize_symbols(text, decoded, pre_dropped=dropped)
        return ParsedFile(
            parser_id=expected_id,
            symbols=final_syms,
            syntax_error_count=syntax_errors,
            dropped_symbol_count=total_dropped,
        )

    def _ensure_worker(self, parser_name: str) -> _NdjsonWorker | None:
        if parser_name in self._unavailable:
            return None
        worker = self._workers.get(parser_name)
        if worker is not None:
            return worker
        spec = self._discovered.get(parser_name)
        if spec is None or spec.command is None:
            return None
        try:
            worker = _NdjsonWorker(spec, spawn=self.spawn_factory)
        except _WorkerUnavailableError as exc:
            self._unavailable[parser_name] = exc.reason
            self._reporter.warn(parser_name, f"parser unavailable ({exc.reason}): {exc}")
            return None
        self._workers[parser_name] = worker
        return worker

    def _record_crash(
        self, parser_name: str, worker: _NdjsonWorker, crash: _WorkerCrashedError
    ) -> bool:
        """Kills crashed worker, increments counter, returns True if bisect should continue."""
        tail = worker.stderr_tail()
        worker.kill()
        self._workers.pop(parser_name, None)
        count = self._crash_counts.get(parser_name, 0) + 1
        self._crash_counts[parser_name] = count
        self._reporter.warn(
            parser_name,
            f"worker crashed #{count} ({crash.reason}): {crash}"
            + (f" | stderr={tail}" if tail else ""),
        )
        if count > self._max_crashes:
            self._unavailable[parser_name] = crash.reason
            return False
        return True

    @staticmethod
    def _fallback_items(
        items: Sequence[tuple[str, str]], out: dict[str, ParsedFile]
    ) -> None:
        for path, text in items:
            out[path] = _regex_fallback(path, text)

    def _collect_worker_responses(
        self,
        parser_name: str,
        worker: _NdjsonWorker,
        items: Sequence[tuple[str, str]],
        out: dict[str, ParsedFile],
    ) -> tuple[list[tuple[str, str]], _WorkerCrashedError | None]:
        reqs = []
        for path, text in items:
            req_id = self._next_id
            self._next_id += 1
            reqs.append((req_id, path, text))
        responses, crash = worker.request_batch(reqs, self._deadline_ms)
        stamp_ids = self._discovered[parser_name].stamp.parser_ids
        remaining: list[tuple[str, str]] = []
        for req_id, path, text in reqs:
            msg = responses.get(req_id)
            if msg is None:
                remaining.append((path, text))
            else:
                expected_id = stamp_ids.get(Path(path).suffix.lower(), REGEX_PARSER_ID)
                out[path] = self._decode_worker_response(path, text, expected_id, msg)
        return remaining, crash

    def _run_worker_slice(
        self,
        parser_name: str,
        items: list[tuple[str, str]],
        out: dict[str, ParsedFile],
    ) -> None:
        if not items:
            return
        worker = None if parser_name in self._unavailable else self._ensure_worker(parser_name)
        if worker is None:
            self._fallback_items(items, out)
            return
        remaining, crash = self._collect_worker_responses(parser_name, worker, items, out)
        if crash is None:
            return
        can_retry = self._record_crash(parser_name, worker, crash)
        if not remaining:
            return
        if not can_retry or len(remaining) == 1:
            self._fallback_items(remaining, out)
            return
        mid = len(remaining) // 2
        self._run_worker_slice(parser_name, remaining[:mid], out)
        self._run_worker_slice(parser_name, remaining[mid:], out)

    def parse_batch(self, files: Mapping[str, str]) -> dict[str, ParsedFile]:
        """Parses `{path: text}` in a batch and returns a `ParsedFile` for every input path."""
        results: dict[str, ParsedFile] = {}
        grouped: dict[str, list[tuple[str, str]]] = {}
        for path, text in files.items():
            ext = Path(path).suffix.lower()
            parser_name = ROUTING.get(ext, "regex")
            if parser_name == "regex" or not self.is_parser_active(parser_name):
                results[path] = _regex_fallback(path, text)
            elif parser_name == "py-ast":
                results[path] = self._parse_python_file(path, text)
            else:
                grouped.setdefault(parser_name, []).append((path, text))
        for parser_name, items in grouped.items():
            self._run_worker_slice(parser_name, items, results)
        return results

    def close(self) -> None:
        """Shuts down every spawned worker cleanly (`Draining` -> `SIGTERM` -> `SIGKILL`)."""
        workers = list(self._workers.values())
        self._workers.clear()
        for worker in workers:
            worker.close()

    def __exit__(self, *_exc: object) -> None:
        self.close()
