"""Unit tests for `symbol_parsers.py`, NDJSON worker lifecycle, stamps, and hashing."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import symbol_parsers
from models import (
    CODE_EXTENSIONS,
    MARKDOWN_EXTENSIONS,
    SCRIPTS_HASH_EXCLUDED_DIRS,
    compute_scripts_sha256,
)
from parsers.conformance import run_conformance
from parsers.stamps import (
    PlatformStamp,
    Stamp,
    binary_name,
    host_platform,
    read_stamp,
    source_sha256,
    write_stamp,
)
from symbol_parsers import (
    REGEX_PARSER_ID,
    ROUTING,
    ParsedSymbol,
    SymbolParsers,
    decode_symbol,
    split_lines,
)


def _setup_fake_dart_worker(
    root: Path,
    script_body: str,
    *,
    stamp_ids: dict[str, str] | None = None,
) -> Path:
    """Creates a temp `scripts_dir` containing a fake executable Dart NDJSON worker."""
    scripts_dir = root
    dart_dir = scripts_dir / "parsers" / "dart"
    dart_dir.mkdir(parents=True, exist_ok=True)
    host = host_platform()
    ids = stamp_ids if stamp_ids is not None else {".dart": "dart-analyzer@1"}
    bin_path = dart_dir / binary_name("dart-analyzer", host)
    bin_path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script_body), encoding="utf-8")
    bin_path.chmod(bin_path.stat().st_mode | stat.S_IXUSR)
    write_stamp(
        dart_dir,
        Stamp(
            parser="dart-analyzer",
            protocol=1,
            parser_ids=ids,
            platforms={host: PlatformStamp(source_sha256="abc", built_at="now", built_on=host)},
        ),
    )
    return scripts_dir


class TestSymbolParsersCore(unittest.TestCase):
    """Tests routing, line splitting, `decode_symbol` rules, generated files, and hashing."""

    def test_routing_covers_all_code_and_markdown_extensions(self) -> None:
        self.assertEqual(set(ROUTING), set(CODE_EXTENSIONS) | set(MARKDOWN_EXTENSIONS))

    def test_split_lines_uses_lf_and_crlf_only(self) -> None:
        lines = split_lines("a\fstill_a\nb\r\nc")
        self.assertEqual(lines, ["a\fstill_a", "b", "c"])

    def test_decode_symbol_rule_table_and_caps(self) -> None:
        self.assertIsNone(decode_symbol({"kind": "alien", "qualified_name": "Foo", "line_start": 1, "line_end": 2}))
        self.assertIsNone(decode_symbol({"kind": "class", "qualified_name": "  ", "line_start": 1, "line_end": 2}))
        self.assertIsNone(decode_symbol({"kind": "class", "qualified_name": "Foo", "line_start": 0, "line_end": 2}))
        self.assertIsNone(decode_symbol({"kind": "class", "qualified_name": "Foo", "line_start": 5, "line_end": 3}))
        self.assertIsNone(decode_symbol({"kind": "class", "qualified_name": "Foo", "line_start": True, "line_end": 3}))

        sym = decode_symbol(
            {
                "kind": "method",
                "qualified_name": "Outer.innerMethod",
                "line_start": 10,
                "line_end": 12,
                "extra_unknown_key": [1, 2, 3],
            }
        )
        assert sym is not None
        self.assertEqual(sym.name, "innerMethod")
        self.assertEqual(sym.parent, "")
        self.assertEqual(sym.signature, "innerMethod")
        self.assertEqual(sym.docstring, "")
        self.assertEqual(sym.visibility, "")

        capped = decode_symbol(
            {
                "kind": "function",
                "name": "big",
                "qualified_name": "big",
                "signature": "x   " + ("a" * 600),
                "docstring": "  " + ("d" * 4500) + "  ",
                "line_start": 1,
                "line_end": 5,
                "visibility": "weird",
            }
        )
        assert capped is not None
        self.assertEqual(len(capped.signature), 500)
        self.assertNotIn("   ", capped.signature)
        self.assertEqual(len(capped.docstring), 4000)
        self.assertEqual(capped.visibility, "")

    def test_compute_scripts_sha256_skips_build_and_fixture_dirs_and_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp) / "scripts"
            pdir = scripts / "parsers" / "dart"
            pdir.mkdir(parents=True)
            (scripts / "packrat.py").write_text("print(1)\n", encoding="utf-8")
            (pdir / "symbol_visitor.dart").write_text("class A {}\n", encoding="utf-8")
            write_stamp(
                pdir,
                Stamp(
                    parser="dart-analyzer",
                    protocol=1,
                    parser_ids={".dart": "dart-analyzer@1"},
                    platforms={},
                ),
            )
            base_hash = compute_scripts_sha256(scripts)
            base_parser_hash = source_sha256(pdir)

            for ignored in (".dart_tool", ".build", "build", "__pycache__", "fixtures"):
                self.assertIn(ignored, SCRIPTS_HASH_EXCLUDED_DIRS)
                sub = pdir / ignored
                sub.mkdir(exist_ok=True)
                (sub / "artifact.dart").write_text("class Noise {}\n", encoding="utf-8")
                (sub / "artifact.py").write_text("x = 1\n", encoding="utf-8")
            (pdir / "packrat-dart-linux-x86_64").write_bytes(b"\x7fELF\x00binary")

            self.assertEqual(compute_scripts_sha256(scripts), base_hash)
            self.assertEqual(source_sha256(pdir), base_parser_hash)

            (pdir / "symbol_visitor.dart").write_text("class B {}\n", encoding="utf-8")
            self.assertNotEqual(compute_scripts_sha256(scripts), base_hash)
            self.assertNotEqual(source_sha256(pdir), base_parser_hash)


class TestNdjsonWorkerLifecycle(unittest.TestCase):
    """Tests NDJSON worker spawning, banners, errors, bisection, crashing, and shutdown."""

    def test_out_of_order_ids_per_file_error_deadline_and_generated_guard(self) -> None:
        script = """
        import json, sys
        print(json.dumps({"ready": True, "parser": "dart-analyzer", "protocol": 1, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)
        pending = []
        for line in sys.stdin:
            req = json.loads(line)
            pending.append(req)
            if len(pending) >= 2:
                for r in reversed(pending):
                    if "error" in r["path"]:
                        print(json.dumps({"id": r["id"], "error": "timeout"}), flush=True)
                    elif "generated" in r["path"]:
                        syms = [
                            {"kind": "class", "name": f"C{i}", "qualified_name": f"C{i}", "parent": "", "signature": f"class C{i}", "docstring": "", "line_start": i + 1, "line_end": i + 1, "visibility": "public"}
                            for i in range(250)
                        ] + [
                            {"kind": "method", "name": "m", "qualified_name": "C0.m", "parent": "C0", "signature": "void m()", "docstring": "", "line_start": 1, "line_end": 1, "visibility": "public"}
                        ]
                        print(json.dumps({"id": r["id"], "symbols": syms, "syntax_error_count": 0}), flush=True)
                    else:
                        print(json.dumps({"id": r["id"], "symbols": [{"kind": "class", "name": f"D{r['deadline_ms']}", "qualified_name": f"D{r['deadline_ms']}", "parent": "", "signature": "class D", "docstring": "doc", "line_start": 1, "line_end": 2, "visibility": "public"}], "syntax_error_count": 0}), flush=True)
                pending.clear()
        for r in reversed(pending):
            print(json.dumps({"id": r["id"], "symbols": [{"kind": "class", "name": "Last", "qualified_name": "Last", "parent": "", "signature": "class Last", "docstring": "", "line_start": 1, "line_end": 1, "visibility": "public"}], "syntax_error_count": 0}), flush=True)
        """
        with tempfile.TemporaryDirectory() as tmp:
            scripts = _setup_fake_dart_worker(Path(tmp), script)
            with SymbolParsers(scripts_dir=scripts, overrides={}, deadline_ms=3210) as parsers:
                self.assertIn(".dart=dart-analyzer@1", parsers.fingerprint())
                res = parsers.parse_batch(
                    {
                        "a_ok.dart": "class A {}",
                        "b_error.dart": "class FallbackClass {}\n",
                    }
                )
                self.assertEqual(res["a_ok.dart"].parser_id, "dart-analyzer@1")
                self.assertEqual(res["a_ok.dart"].symbols[0].name, "D3210")
                self.assertEqual(res["b_error.dart"].parser_id, REGEX_PARSER_ID)
                self.assertEqual(res["b_error.dart"].symbols[0].name, "FallbackClass")
                self.assertEqual(dict(parsers.unavailable()), {})

                # Generated file (> 4000 chars line): drops nested & caps at 200.
                gen_res = parsers.parse_batch(
                    {
                        "a_generated.dart": "// " + ("x" * 4100) + "\nclass C0 {}",
                        "b_tail.dart": "class Tail {}",
                    }
                )
                gf = gen_res["a_generated.dart"]
                self.assertEqual(len(gf.symbols), 200)
                self.assertEqual(gf.dropped_symbol_count, 51)
                self.assertTrue(all(s.parent == "" for s in gf.symbols))

    def test_crash_bisection_then_fourth_crash_marks_unavailable(self) -> None:
        script = """
        import json, os, sys
        print(json.dumps({"ready": True, "parser": "dart-analyzer", "protocol": 1, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)
        for line in sys.stdin:
            req = json.loads(line)
            if "poison" in req["path"]:
                sys.stderr.write("fatal: poison file crash\\n")
                sys.stderr.flush()
                os._exit(1)
            print(json.dumps({"id": req["id"], "symbols": [{"kind": "class", "name": "Ok", "qualified_name": "Ok", "parent": "", "signature": "class Ok", "docstring": "", "line_start": 1, "line_end": 1, "visibility": "public"}], "syntax_error_count": 0}), flush=True)
        """
        with tempfile.TemporaryDirectory() as tmp:
            scripts = _setup_fake_dart_worker(Path(tmp), script)
            warns: list[tuple[str, str]] = []

            class SpyRep:
                def start_phase(self, *_a: object, **_k: object) -> None: ...
                def advance(self, *_a: object, **_k: object) -> None: ...
                def finish_phase(self, *_a: object, **_k: object) -> None: ...
                def clear_active_line(self) -> None: ...
                def error(self, *_a: object, **_k: object) -> None: ...
                def warn(self, repo: str, msg: str) -> None:
                    warns.append((repo, msg))

            with SymbolParsers(
                scripts_dir=scripts, overrides={}, reporter=SpyRep(), max_crashes_per_parser=3
            ) as parsers:
                # 4-file batch crashes on poison -> bisects -> only poison falls back to regex.
                batch1 = parsers.parse_batch(
                    {
                        "f1.dart": "class One {}",
                        "f2_poison.dart": "class TwoPoison {}",
                        "f3.dart": "class Three {}",
                        "f4.dart": "class Four {}",
                    }
                )
                self.assertEqual(batch1["f1.dart"].parser_id, "dart-analyzer@1")
                self.assertEqual(batch1["f2_poison.dart"].parser_id, REGEX_PARSER_ID)
                self.assertEqual(batch1["f3.dart"].parser_id, "dart-analyzer@1")
                self.assertEqual(batch1["f4.dart"].parser_id, "dart-analyzer@1")
                self.assertEqual(dict(parsers.unavailable()), {})
                self.assertTrue(any("poison file crash" in w[1] for w in warns))

                # 3rd and 4th crash mark dart-analyzer Unavailable cleanly.
                parsers.parse_batch({"x1_poison.dart": "class X1 {}"})
                self.assertEqual(dict(parsers.unavailable()), {})
                parsers.parse_batch({"x2_poison.dart": "class X2 {}", "after.dart": "class After {}"})
                unavail = parsers.unavailable()
                self.assertIn("dart-analyzer", unavail)
                self.assertEqual(parsers.parser_id_for("another.dart"), REGEX_PARSER_ID)

    def test_banner_failures_protocol_skew_stamp_mismatch_and_32mib_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1. Missing/bad banner
            s1 = _setup_fake_dart_worker(root / "c1", 'print("not-json", flush=True)\n')
            with SymbolParsers(scripts_dir=s1, overrides={}) as p1:
                out1 = p1.parse_batch({"a.dart": "class A {}"})
                self.assertEqual(out1["a.dart"].parser_id, REGEX_PARSER_ID)
                self.assertEqual(p1.unavailable()["dart-analyzer"], "bad-banner")

            # 2. Late banner -> spawn-timeout
            s2 = _setup_fake_dart_worker(
                root / "c2",
                'import time; time.sleep(0.4); print(\'{"ready": true}\', flush=True)\n',
            )
            with (
                mock.patch.object(symbol_parsers, "BANNER_TIMEOUT_SEC", 0.05),
                SymbolParsers(scripts_dir=s2, overrides={}) as p2,
            ):
                p2.parse_batch({"a.dart": "class A {}"})
                self.assertEqual(p2.unavailable()["dart-analyzer"], "spawn-timeout")

            # 3. Protocol skew
            s3 = _setup_fake_dart_worker(
                root / "c3",
                'import json; print(json.dumps({"ready": True, "protocol": 2, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)\n',
            )
            with SymbolParsers(scripts_dir=s3, overrides={}) as p3:
                p3.parse_batch({"a.dart": "class A {}"})
                self.assertEqual(p3.unavailable()["dart-analyzer"], "protocol-skew")

            # 4. Stamp mismatch
            s4 = _setup_fake_dart_worker(
                root / "c4",
                'import json; print(json.dumps({"ready": True, "protocol": 1, "parser_ids": {".dart": "dart-analyzer@9"}}), flush=True)\n',
            )
            with SymbolParsers(scripts_dir=s4, overrides={}) as p4:
                p4.parse_batch({"a.dart": "class A {}"})
                self.assertEqual(p4.unavailable()["dart-analyzer"], "stamp-mismatch")

            # 5. Oversized >32 MiB line
            s5 = _setup_fake_dart_worker(
                root / "c5",
                """
                import json, sys
                print(json.dumps({"ready": True, "protocol": 1, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)
                sys.stdin.readline()
                sys.stdout.write("{" + ("x" * (4096)) + "}\\n")
                sys.stdout.flush()
                """,
            )
            with (
                mock.patch.object(symbol_parsers, "MAX_NDJSON_LINE_BYTES", 512),
                SymbolParsers(scripts_dir=s5, overrides={}, max_crashes_per_parser=0) as p5,
            ):
                p5.parse_batch({"a.dart": "class A {}"})
                self.assertEqual(p5.unavailable()["dart-analyzer"], "line-too-large")

    def test_stderr_flooding_hung_draining_and_discovery_exclusions(self) -> None:
        script = """
        import json, sys, time
        print(json.dumps({"ready": True, "protocol": 1, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)
        for line in sys.stdin:
            req = json.loads(line)
            sys.stderr.write("E" * 16384 + "\\n")
            sys.stderr.flush()
            print(json.dumps({"id": req["id"], "symbols": [], "syntax_error_count": 0}), flush=True)
        # Ignore EOF and hang during Draining so orchestrator escalates to SIGTERM/SIGKILL
        time.sleep(30)
        """
        with tempfile.TemporaryDirectory() as tmp:
            scripts = _setup_fake_dart_worker(Path(tmp), script)
            # Add a swift stamp with NO binary -> must stay out of fingerprint AND unavailable()
            swift_dir = scripts / "parsers" / "swift"
            write_stamp(
                swift_dir,
                Stamp(parser="swift-syntax", protocol=1, parser_ids={".swift": "swift-syntax@1"}, platforms={}),
            )
            with (
                mock.patch.object(symbol_parsers, "DRAIN_TIMEOUT_SEC", 0.05),
                mock.patch.object(symbol_parsers, "TERM_GRACE_SEC", 0.05),
                SymbolParsers(scripts_dir=scripts, overrides={}) as parsers,
            ):
                self.assertIn(".dart=dart-analyzer@1", parsers.fingerprint())
                self.assertNotIn(".swift=", parsers.fingerprint())
                res = parsers.parse_batch(
                    {"ok.dart": "class A {}", "no_bin.swift": "class S {}"}
                )
                self.assertEqual(res["ok.dart"].parser_id, "dart-analyzer@1")
                self.assertEqual(res["no_bin.swift"].parser_id, REGEX_PARSER_ID)
                self.assertEqual(dict(parsers.unavailable()), {})

            # Overrides disable a discovered parser completely
            with SymbolParsers(
                scripts_dir=scripts, overrides={"dart-analyzer": "disabled"}
            ) as disabled:
                self.assertEqual(disabled.fingerprint(), "")
                out = disabled.parse_batch({"a.dart": "class A {}"})
                self.assertEqual(out["a.dart"].parser_id, REGEX_PARSER_ID)
                self.assertEqual(dict(disabled.unavailable()), {})

    def test_conformance_runner_checks_timeout_syntax_and_skips_missing_binary(self) -> None:
        script = """
        import json, sys, time
        print(json.dumps({"ready": True, "parser": "dart-analyzer", "protocol": 1, "parser_ids": {".dart": "dart-analyzer@1"}}), flush=True)
        for line in sys.stdin:
            req = json.loads(line)
            if "deep_nesting" in req["path"] and req["deadline_ms"] <= 50:
                print(json.dumps({"id": req["id"], "error": "timeout"}), flush=True)
            else:
                print(json.dumps({"id": req["id"], "symbols": [], "syntax_error_count": 2}), flush=True)
        """
        with tempfile.TemporaryDirectory() as tmp:
            scripts = _setup_fake_dart_worker(Path(tmp), script)
            dart_conf = scripts / "parsers" / "dart" / "fixtures" / "conformance"
            dart_conf.mkdir(parents=True)
            (dart_conf / "deep_nesting.dart").write_text("void f() {}\n", encoding="utf-8")
            (dart_conf / "syntax_error.dart").write_text("class Broken {\n", encoding="utf-8")

            swift_dir = scripts / "parsers" / "swift"
            swift_conf = swift_dir / "fixtures" / "conformance"
            swift_conf.mkdir(parents=True)
            (swift_conf / "syntax_error.swift").write_text("struct {\n", encoding="utf-8")
            write_stamp(
                swift_dir,
                Stamp(parser="swift-syntax", protocol=1, parser_ids={".swift": "swift-syntax@1"}, platforms={}),
            )

            with SymbolParsers(scripts_dir=scripts, overrides={}) as parsers:
                report = run_conformance(parsers, scripts / "parsers")
            self.assertTrue(report.ok)
            by_name = {o.parser: o for o in report.outcomes}
            self.assertEqual(by_name["dart-analyzer"].status, "passed")
            self.assertEqual(by_name["swift-syntax"].status, "skipped")
            self.assertEqual(by_name["swift-syntax"].detail, "missing-binary")


if __name__ == "__main__":
    unittest.main()
