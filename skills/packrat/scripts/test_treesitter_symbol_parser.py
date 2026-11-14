"""Unit and conformance tests for the `treesitter` NDJSON symbol parser worker."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

from parsers.conformance import run_conformance
from parsers.stamps import read_stamp, source_sha256
from parsers.treesitter.common import PARSER_ID, PARSER_IDS, PARSER_NAME, PROTOCOL_VERSION
from symbol_parsers import SymbolParsers, _find_uv_executable

SCRIPTS_DIR = Path(__file__).resolve().parent
PARSER_DIR = SCRIPTS_DIR / "parsers" / "treesitter"
FIXTURES_DIR = PARSER_DIR / "fixtures"
WORKER_SCRIPT = PARSER_DIR / "symbol_parser.py"


def _probe_offline_uv_worker() -> tuple[tuple[str, ...] | None, str]:
    """Checks whether `uv run --offline --script` can launch the tree-sitter worker."""
    uv_bin = _find_uv_executable()
    cmd = (
        uv_bin,
        "run",
        "--offline",
        "--python",
        sys.executable,
        "--script",
        str(WORKER_SCRIPT),
        "--worker",
    )
    try:
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        return None, f"uv executable unavailable ({exc})"
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    proc.stdin.close()
    banner_line = proc.stdout.readline()
    stderr_out = proc.stderr.read().decode("utf-8", errors="replace").strip()
    proc.wait(timeout=10)
    proc.stdout.close()
    proc.stderr.close()
    if not banner_line:
        return (
            None,
            "tree-sitter offline wheels missing; warm cache with: "
            f"`uv run --script {WORKER_SCRIPT} --selftest` ({stderr_out})",
        )
    return cmd, ""


class _WorkerHarness:
    """Drives a live `symbol_parser.py --worker` subprocess over NDJSON pipes."""

    def __init__(self, cmd: tuple[str, ...]) -> None:
        self._proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert self._proc.stdout is not None
        raw_banner = self._proc.stdout.readline()
        self.banner: dict[str, Any] = json.loads(raw_banner.decode("utf-8"))
        self._next_id = 1

    def request(self, path: str, content: str, *, deadline_ms: int = 5000) -> dict[str, Any]:
        req_id = self._next_id
        self._next_id += 1
        results = self.request_many([(req_id, path, content, deadline_ms)])
        return results[req_id]

    def request_many(
        self, items: list[tuple[int, str, str, int]]
    ) -> dict[int, dict[str, Any]]:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        for req_id, path, content, deadline_ms in items:
            payload = {
                "id": req_id,
                "path": path,
                "content": content,
                "deadline_ms": deadline_ms,
            }
            self._proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self._proc.stdin.flush()
        out: dict[int, dict[str, Any]] = {}
        while len(out) < len(items):
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError("Unexpected EOF from tree-sitter worker")
            msg = json.loads(line.decode("utf-8"))
            out[int(msg["id"])] = msg
        return out

    def close(self) -> int:
        assert self._proc.stdin is not None and self._proc.stdout is not None and self._proc.stderr is not None
        self._proc.stdin.close()
        rc = self._proc.wait(timeout=10)
        self._proc.stdout.close()
        self._proc.stderr.close()
        return rc


def _tuples(symbols: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str, str]]:
    return [
        (
            s["kind"],
            s["name"],
            s["qualified_name"],
            s["parent"],
            s["visibility"],
            s["docstring"],
        )
        for s in symbols
    ]


class TestTreesitterSymbolParser(unittest.TestCase):
    """Verifies golden extraction, deadlines, syntax recovery, generated caps, and conformance."""

    _worker_cmd: tuple[str, ...] | None = None
    _skip_reason: str = ""
    _worker: _WorkerHarness | None = None

    @classmethod
    def setUpClass(cls) -> None:
        cmd, reason = _probe_offline_uv_worker()
        cls._worker_cmd = cmd
        cls._skip_reason = reason
        if cmd is not None:
            cls._worker = _WorkerHarness(cmd)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._worker is not None:
            cls._worker.close()
            cls._worker = None

    def _harness(self) -> _WorkerHarness:
        if self._worker is None:
            self.skipTest(self._skip_reason)
        assert self._worker is not None
        return self._worker

    def test_stamp_matches_sources_and_routing_extensions(self) -> None:
        stamp = read_stamp(PARSER_DIR)
        self.assertIsNotNone(stamp)
        assert stamp is not None
        self.assertEqual(stamp.parser, PARSER_NAME)
        self.assertEqual(stamp.protocol, PROTOCOL_VERSION)
        self.assertEqual(dict(stamp.parser_ids), PARSER_IDS)
        self.assertEqual(stamp.platforms["any"].source_sha256, source_sha256(PARSER_DIR))

    def test_worker_banner_matches_stamp(self) -> None:
        harness = self._harness()
        self.assertIs(harness.banner.get("ready"), True)
        self.assertEqual(harness.banner.get("parser"), PARSER_NAME)
        self.assertEqual(harness.banner.get("protocol"), PROTOCOL_VERSION)
        self.assertEqual(harness.banner.get("parser_ids"), PARSER_IDS)
        self.assertTrue(str(harness.banner.get("library", "")).startswith("tree-sitter "))

    def test_golden_typescript_overloads_jsdoc_and_same_line_accessors(self) -> None:
        harness = self._harness()
        text = (FIXTURES_DIR / "sample.ts").read_text(encoding="utf-8")
        resp = harness.request("src/sample.ts", text)
        self.assertEqual(resp["syntax_error_count"], 0)
        syms = resp["symbols"]

        expected = [
            ("variable", "DEFAULT_LIMIT", "DEFAULT_LIMIT", "", "public", "Top-level limit configuration."),
            ("variable", "internalCounter", "internalCounter", "", "", ""),
            ("typedef", "WidgetId", "WidgetId", "", "public", "Alias for identifiers."),
            ("enum", "Theme", "Theme", "", "public", "Supported display themes."),
            ("enum_value", "Light", "Theme.Light", "Theme", "public", ""),
            ("enum_value", "Dark", "Theme.Dark", "Theme", "public", ""),
            ("interface", "Renderable", "Renderable", "", "public", "Contract for renderable components."),
            ("field", "id", "Renderable.id", "Renderable", "public", ""),
            ("method", "render", "Renderable.render", "Renderable", "public", ""),
            ("namespace", "Layout", "Layout", "", "public", "Namespace for layout utilities."),
            ("function", "measure", "Layout.measure", "Layout", "public", "Measures available width."),
            ("class", "Widget", "Widget", "", "public", "Base UI widget supporting overloads and private state."),
            ("field", "id", "Widget.id", "Widget", "public", "Public identifier."),
            ("field", "label", "Widget.label", "Widget", "protected", ""),
            ("field", "#secretToken", "Widget.#secretToken", "Widget", "private", ""),
            ("constructor", "constructor", "Widget.constructor", "Widget", "public", "Creates a widget instance."),
            ("getter", "title", "Widget.title", "Widget", "public", ""),
            ("setter", "title", "Widget.title", "Widget", "public", ""),
            ("method", "format", "Widget.format", "Widget", "public", "Formats widget output."),
            ("method", "format", "Widget.format", "Widget", "public", ""),
            ("method", "format", "Widget.format", "Widget", "public", ""),
            ("method", "reset", "Widget.reset", "Widget", "private", ""),
            ("function", "parseWidget", "parseWidget", "", "public", "Parses raw widget input."),
        ]
        self.assertEqual(_tuples(syms), expected)

        # Decorator is included in `line_start`, excluding preceding JSDoc.
        widget_cls = [s for s in syms if s["name"] == "Widget"][0]
        self.assertEqual(text.split("\n")[widget_cls["line_start"] - 1].strip(), "@Sealed()")

        # Same-line getter and setter share `line_start` with distinct kinds/signatures.
        getter = [s for s in syms if s["kind"] == "getter"][0]
        setter = [s for s in syms if s["kind"] == "setter"][0]
        self.assertEqual(getter["line_start"], setter["line_start"])
        self.assertEqual(getter["signature"], "get title(): string")
        self.assertEqual(setter["signature"], "set title(v: string)")

        # Overloads keep identical `qualified_name` with distinct `line_start`.
        overloads = [s for s in syms if s["qualified_name"] == "Widget.format"]
        self.assertEqual(len(overloads), 3)
        self.assertEqual(len({s["line_start"] for s in overloads}), 3)

        # Local functions/closures inside method bodies are omitted.
        all_names = {s["name"] for s in syms}
        self.assertNotIn("localHelper", all_names)
        self.assertNotIn("arrowLocal", all_names)

    def test_golden_tsx_and_javascript_variants(self) -> None:
        harness = self._harness()
        tsx_resp = harness.request(
            "ui/sample.tsx", (FIXTURES_DIR / "sample.tsx").read_text(encoding="utf-8")
        )
        self.assertEqual(tsx_resp["syntax_error_count"], 0)
        self.assertEqual(
            _tuples(tsx_resp["symbols"]),
            [
                ("interface", "BadgeProps", "BadgeProps", "", "public", "Props passed to a badge component."),
                ("field", "text", "BadgeProps.text", "BadgeProps", "public", ""),
                ("function", "Badge", "Badge", "", "public", "Renders an inline status badge."),
                ("class", "Card", "Card", "", "public", "Stateful card component."),
                ("field", "title", "Card.title", "Card", "public", ""),
                ("method", "renderView", "Card.renderView", "Card", "public", ""),
            ],
        )

        js_text = (FIXTURES_DIR / "sample.js").read_text(encoding="utf-8")
        for ext in (".js", ".jsx", ".mjs"):
            js_resp = harness.request(f"lib/sample{ext}", js_text)
            self.assertEqual(js_resp["syntax_error_count"], 0)
            self.assertEqual(
                _tuples(js_resp["symbols"]),
                [
                    ("variable", "MAX_RETRIES", "MAX_RETRIES", "", "public", "Default retry count for requests."),
                    ("variable", "moduleCache", "moduleCache", "", "", ""),
                    ("class", "Counter", "Counter", "", "public", "Counter widget written in plain JavaScript."),
                    ("field", "count", "Counter.count", "Counter", "public", ""),
                    ("field", "#step", "Counter.#step", "Counter", "private", ""),
                    ("constructor", "constructor", "Counter.constructor", "Counter", "public", ""),
                    ("getter", "current", "Counter.current", "Counter", "public", ""),
                    ("setter", "current", "Counter.current", "Counter", "public", ""),
                    ("method", "increment", "Counter.increment", "Counter", "public", "Increments the counter by step."),
                    ("function", "createCounter", "createCounter", "", "public", "Creates a default counter instance."),
                ],
            )

    def test_golden_kotlin_kdoc_extensions_and_accessors(self) -> None:
        harness = self._harness()
        kt_text = (FIXTURES_DIR / "sample.kt").read_text(encoding="utf-8")
        resp = harness.request("src/sample.kt", kt_text)
        self.assertEqual(resp["syntax_error_count"], 0)
        self.assertEqual(
            _tuples(resp["symbols"]),
            [
                ("variable", "DEFAULT_TIMEOUT_MS", "DEFAULT_TIMEOUT_MS", "", "private", "Global timeout in milliseconds."),
                ("typedef", "Headers", "Headers", "", "public", "String map shorthand."),
                ("enum", "SyncMode", "SyncMode", "", "public", "Execution modes for sync tasks."),
                ("enum_value", "FAST", "SyncMode.FAST", "SyncMode", "public", ""),
                ("enum_value", "FULL", "SyncMode.FULL", "SyncMode", "public", ""),
                ("interface", "Repository", "Repository", "", "public", "Storage repository contract."),
                ("method", "count", "Repository.count", "Repository", "public", ""),
                ("object", "Registry", "Registry", "", "public", "Process-wide registry singleton."),
                ("field", "version", "Registry.version", "Registry", "public", ""),
                ("field", "handle", "UserAccount.handle", "UserAccount", "private", ""),
                ("field", "id", "UserAccount.id", "UserAccount", "public", ""),
                ("constructor", "init", "UserAccount.init", "UserAccount", "public", ""),
                ("class", "UserAccount", "UserAccount", "", "public", "Immutable user account model."),
                ("field", "secondaryLabel", "UserAccount.secondaryLabel", "UserAccount", "public", ""),
                ("getter", "secondaryLabel", "UserAccount.secondaryLabel", "UserAccount", "public", ""),
                ("constructor", "init", "UserAccount.init", "UserAccount", "public", "Secondary constructor from id only."),
                ("field", "nickname", "UserAccount.nickname", "UserAccount", "internal", ""),
                ("getter", "nickname", "UserAccount.nickname", "UserAccount", "internal", ""),
                ("setter", "nickname", "UserAccount.nickname", "UserAccount", "internal", ""),
                ("method", "count", "UserAccount.count", "UserAccount", "public", "Returns item count."),
                ("method", "clearHandle", "UserAccount.clearHandle", "UserAccount", "protected", ""),
                ("object", "Factory", "UserAccount.Factory", "UserAccount", "public", ""),
                ("method", "guest", "UserAccount.Factory.guest", "UserAccount.Factory", "public", "Builds a guest account."),
                ("function", "shout", "String.shout", "", "public", "Uppercases a receiver string with exclamation."),
            ],
        )
        names = {s["name"] for s in resp["symbols"]}
        self.assertNotIn("transientToken", names)
        self.assertNotIn("nestedLocal", names)

    def test_golden_cpp_namespaces_doxygen_and_access_specifiers(self) -> None:
        harness = self._harness()
        cpp_text = (FIXTURES_DIR / "sample.cpp").read_text(encoding="utf-8")
        resp = harness.request("include/sample.hpp", cpp_text)
        self.assertEqual(resp["syntax_error_count"], 0)
        self.assertEqual(
            _tuples(resp["symbols"]),
            [
                ("namespace", "core", "engine.core", "", "", ""),
                ("namespace", "(anonymous)", "engine.core.(anonymous)", "engine.core", "", ""),
                ("variable", "hidden_seed", "engine.core.(anonymous).hidden_seed", "engine.core.(anonymous)", "", ""),
                ("struct", "Point", "engine.core.Point", "engine.core", "", "2D integer coordinate."),
                ("field", "x", "engine.core.Point.x", "engine.core.Point", "public", ""),
                ("field", "y", "engine.core.Point.y", "engine.core.Point", "public", ""),
                ("field", "tag_", "engine.core.Point.tag_", "engine.core.Point", "private", ""),
                ("enum", "PixelFormat", "engine.core.PixelFormat", "engine.core", "", "Pixel channel format."),
                ("enum_value", "Rgba8", "engine.core.PixelFormat.Rgba8", "engine.core.PixelFormat", "public", ""),
                ("enum_value", "Float16", "engine.core.PixelFormat.Float16", "engine.core.PixelFormat", "public", ""),
                ("typedef", "ByteBuffer", "engine.core.ByteBuffer", "engine.core", "", ""),
                ("typedef", "HandleId", "engine.core.HandleId", "engine.core", "", ""),
                ("class", "Surface", "engine.core.Surface", "engine.core", "", "Hardware surface wrapper."),
                ("field", "secret_id_", "engine.core.Surface.secret_id_", "engine.core.Surface", "private", ""),
                ("constructor", "Surface", "engine.core.Surface.Surface", "engine.core.Surface", "public", "Constructs a surface with dimensions."),
                ("method", "~Surface", "engine.core.Surface.~Surface", "engine.core.Surface", "public", ""),
                ("method", "width", "engine.core.Surface.width", "engine.core.Surface", "public", "Returns current width."),
                ("method", "resize", "engine.core.Surface.resize", "engine.core.Surface", "public", ""),
                ("field", "width_", "engine.core.Surface.width_", "engine.core.Surface", "protected", ""),
                ("field", "height_", "engine.core.Surface.height_", "engine.core.Surface", "protected", ""),
                ("method", "resize", "engine.core.Surface.resize", "engine.core", "", ""),
                ("function", "compute_area", "engine.core.compute_area", "engine.core", "", "Computes total pixel area."),
            ],
        )
        c_resp = harness.request(
            "src/legacy.c",
            "typedef struct { int fd; } FileHandle;\nint open_fd(void) { return 0; }\n",
        )
        self.assertEqual(
            [s["qualified_name"] for s in c_resp["symbols"]],
            ["FileHandle", "FileHandle.fd", "open_fd"],
        )

    def test_form_feed_line_numbers_use_lf_only(self) -> None:
        harness = self._harness()
        ts_src = "const a = 1;\fconst sameLine = 2;\nexport function lineTwo(): void {}\n"
        resp = harness.request("ff.ts", ts_src)
        by_name = {s["name"]: s for s in resp["symbols"]}
        self.assertEqual(by_name["sameLine"]["line_start"], 1)
        self.assertEqual(by_name["lineTwo"]["line_start"], 2)

    def test_deep_nesting_times_out_at_50ms_and_worker_recovers(self) -> None:
        harness = self._harness()
        conf_dir = FIXTURES_DIR / "conformance"
        for ext in ("ts", "kt", "cpp"):
            src = (conf_dir / f"deep_nesting.{ext}").read_text(encoding="utf-8")
            timed_out = harness.request(f"deep_nesting.{ext}", src, deadline_ms=50)
            self.assertEqual(timed_out, {"id": timed_out["id"], "error": "timeout"})

        recovered = harness.request("ok.ts", "export function alive(): number { return 1; }\n")
        self.assertNotIn("error", recovered)
        self.assertEqual(recovered["symbols"][0]["name"], "alive")

    def test_syntax_error_fixtures_report_error_count_without_crashing(self) -> None:
        harness = self._harness()
        conf_dir = FIXTURES_DIR / "conformance"
        for ext in ("ts", "kt", "cpp"):
            src = (conf_dir / f"syntax_error.{ext}").read_text(encoding="utf-8")
            resp = harness.request(f"syntax_error.{ext}", src)
            self.assertNotIn("error", resp)
            self.assertGreaterEqual(resp["syntax_error_count"], 1)

    def test_generated_one_line_js_bundle_caps_and_drops_nested_members(self) -> None:
        harness = self._harness()
        bundle = (FIXTURES_DIR / "minified_bundle.js").read_text(encoding="utf-8")
        self.assertGreater(len(bundle), 4000)

        # Q1 measurement: `minified_bundle.js` (8,239 bytes, 1 line) yields 270 raw
        # tree-sitter symbols (240 top-level var/function/class declarations + 30
        # nested class methods; 0 local IIFE helper functions). Generated-file rules
        # drop all 30 nested members and cap top-level output at 200 (70 dropped).
        resp = harness.request("dist/minified_bundle.js", bundle)
        self.assertEqual(resp["syntax_error_count"], 0)
        self.assertEqual(resp["raw_symbol_count"], 270)
        self.assertEqual(resp["dropped_symbol_count"], 70)
        self.assertEqual(len(resp["symbols"]), 200)
        self.assertTrue(all(s["parent"] == "" for s in resp["symbols"]))

        with SymbolParsers(scripts_dir=SCRIPTS_DIR, overrides={}) as session:
            parsed = session.parse_batch({"dist/minified_bundle.js": bundle})[
                "dist/minified_bundle.js"
            ]
            self.assertEqual(parsed.parser_id, PARSER_ID)
            self.assertEqual(len(parsed.symbols), 200)
            self.assertTrue(all(s.parent == "" for s in parsed.symbols))

    def test_run_conformance_passes_treesitter(self) -> None:
        if self._worker_cmd is None:
            self.skipTest(self._skip_reason)
        with SymbolParsers(scripts_dir=SCRIPTS_DIR, overrides={}) as session:
            self.assertTrue(session.is_parser_active(PARSER_NAME))
            report = run_conformance(session, SCRIPTS_DIR / "parsers")
        by_parser = {o.parser: o for o in report.outcomes}
        self.assertIn(PARSER_NAME, by_parser)
        self.assertEqual(by_parser[PARSER_NAME].status, "passed", by_parser[PARSER_NAME].detail)
        self.assertEqual(by_parser[PARSER_NAME].fixtures_checked, 6)


if __name__ == "__main__":
    unittest.main()
