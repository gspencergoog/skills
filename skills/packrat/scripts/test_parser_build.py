"""Unit and fitness tests for `parser_build.py`."""

from __future__ import annotations

import io
import json
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import parser_build
from parsers.stamps import (
    PlatformStamp,
    Stamp,
    binary_name,
    read_stamp,
    source_sha256,
    write_stamp,
)


def _seed_parsers_tree(root: Path) -> Path:
    """Creates a synthetic `scripts/parsers` directory with python, treesitter, dart, and swift."""
    parsers_dir = root / "parsers"
    py_dir = parsers_dir / "python"
    py_dir.mkdir(parents=True)
    (py_dir / "symbol_parser.py").write_text("def parse_python(): pass\n", encoding="utf-8")

    ts_dir = parsers_dir / "treesitter"
    ts_dir.mkdir(parents=True)
    (ts_dir / "symbol_parser.py").write_text("# /// script\n", encoding="utf-8")

    dart_dir = parsers_dir / "dart"
    (dart_dir / "bin").mkdir(parents=True)
    (dart_dir / "pubspec.yaml").write_text("name: packrat_dart\n", encoding="utf-8")
    (dart_dir / "bin" / "packrat_dart.dart").write_text("void main() {}\n", encoding="utf-8")

    swift_dir = parsers_dir / "swift"
    (swift_dir / "Sources" / "PackratSwift").mkdir(parents=True)
    (swift_dir / "Package.swift").write_text("// swift-tools-version: 6.0\n", encoding="utf-8")
    (swift_dir / "Sources" / "PackratSwift" / "main.swift").write_text(
        "print(1)\n", encoding="utf-8"
    )
    return parsers_dir


class _FakeBuildRunner:
    """Mock subprocess runner recording commands and materializing build outputs."""

    def __init__(
        self,
        *,
        swift_version: str = "Swift version 6.0.3 (swift-6.0.3-RELEASE)",
        swift_sdks: str = "x86_64-swift-linux-musl\n",
        fail_prefix: tuple[str, ...] | None = None,
    ) -> None:
        self.swift_version = swift_version
        self.swift_sdks = swift_sdks
        self.fail_prefix = fail_prefix
        self.calls: list[tuple[list[str], str]] = []

    def __call__(
        self, cmd: list[str], *, cwd: str = "", **_kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(part) for part in cmd]
        self.calls.append((argv, cwd))
        if self.fail_prefix is not None and tuple(argv[: len(self.fail_prefix)]) == self.fail_prefix:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="simulated failure")
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, stdout=self.swift_version, stderr="")
        if len(argv) >= 3 and argv[1:3] == ["sdk", "list"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.swift_sdks, stderr="")
        if len(argv) >= 3 and argv[1:3] == ["compile", "exe"]:
            out_idx = argv.index("-o") + 1
            out_path = Path(argv[out_idx])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(b"\x7fELF-dart")
            out_path.chmod(0o644)
        if len(argv) >= 2 and argv[1] == "build":
            swift_dir = Path(cwd)
            if "--swift-sdk" in argv:
                sdk = argv[argv.index("--swift-sdk") + 1]
                bin_path = swift_dir / ".build" / sdk / "release" / "PackratSwift"
            else:
                bin_path = swift_dir / ".build" / "release" / "PackratSwift"
            bin_path.parent.mkdir(parents=True, exist_ok=True)
            bin_path.write_bytes(b"\x7fELF-swift")
            bin_path.chmod(0o644)
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")


class TestParserBuildTooling(unittest.TestCase):
    """Tests `parser_build` host capability checks, command lines, stamps, and staleness."""

    def test_locate_dart_and_uv_fallback_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sdk_dart = root / "dart-sdk" / "bin" / "dart"
            sdk_dart.parent.mkdir(parents=True)
            sdk_dart.write_text("#!/bin/sh\n", encoding="utf-8")
            sdk_dart.chmod(0o755)

            flutter_dart = root / "flutter" / "bin" / "cache" / "dart-sdk" / "bin" / "dart"
            flutter_dart.parent.mkdir(parents=True)
            flutter_dart.write_text("#!/bin/sh\n", encoding="utf-8")
            flutter_dart.chmod(0o755)

            local_uv = root / "home" / ".local" / "bin" / "uv"
            local_uv.parent.mkdir(parents=True)
            local_uv.write_text("#!/bin/sh\n", encoding="utf-8")
            local_uv.chmod(0o755)

            no_which = lambda _name: None
            self.assertEqual(
                parser_build.locate_dart(which=no_which, env={"DART_SDK": str(root / "dart-sdk")}),
                str(sdk_dart),
            )
            self.assertEqual(
                parser_build.locate_dart(
                    which=no_which, env={"FLUTTER_ROOT": str(root / "flutter")}
                ),
                str(flutter_dart),
            )
            self.assertIsNone(parser_build.locate_dart(which=no_which, env={}))

            self.assertEqual(
                parser_build.locate_uv(which=no_which, env={"HOME": str(root / "home")}),
                str(local_uv),
            )
            self.assertIsNone(
                parser_build.locate_uv(which=no_which, env={"HOME": str(root / "empty")})
            )

    def test_build_all_platforms_on_macos_writes_binaries_chmod_and_stamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsers_dir = _seed_parsers_tree(Path(tmp))
            runner = _FakeBuildRunner()
            which = lambda name: f"/usr/local/bin/{name}"

            report = parser_build.build(
                all_platforms=True,
                parsers_dir=parsers_dir,
                runner=runner,
                host="darwin-arm64",
                which=which,
                env={},
                now=1_800_000_000.0,
            )
            self.assertTrue(report.ok, report.errors)
            self.assertEqual(report.targets, ("linux-x86_64", "darwin-arm64"))
            self.assertEqual(
                report.built,
                (
                    "py-ast:any",
                    "treesitter:any",
                    "dart-analyzer:linux-x86_64",
                    "dart-analyzer:darwin-arm64",
                    "swift-syntax:linux-x86_64",
                    "swift-syntax:darwin-arm64",
                ),
            )

            cmds = [c[0] for c in runner.calls]
            # 1. uv sync --script for treesitter
            self.assertIn(
                ["/usr/local/bin/uv", "sync", "--script", str(parsers_dir / "treesitter" / "symbol_parser.py")],
                cmds,
            )
            # 2. Dart Linux cross-compile and macOS native compile
            dart_linux_out = parsers_dir / "dart" / "packrat-dart-linux-x86_64"
            dart_mac_out = parsers_dir / "dart" / "packrat-dart-darwin-arm64"
            self.assertIn(
                [
                    "/usr/local/bin/dart",
                    "compile",
                    "exe",
                    "bin/packrat_dart.dart",
                    "--target-os=linux",
                    "--target-arch=x64",
                    "-o",
                    str(dart_linux_out),
                ],
                cmds,
            )
            self.assertIn(
                [
                    "/usr/local/bin/dart",
                    "compile",
                    "exe",
                    "bin/packrat_dart.dart",
                    "-o",
                    str(dart_mac_out),
                ],
                cmds,
            )
            # 3. Swift Linux musl cross-compile and macOS native compile
            self.assertIn(
                [
                    "/usr/local/bin/swift",
                    "build",
                    "-c",
                    "release",
                    "--swift-sdk",
                    "x86_64-swift-linux-musl",
                ],
                cmds,
            )
            self.assertIn(["/usr/local/bin/swift", "build", "-c", "release"], cmds)

            # 4. All 4 output binaries exist and have 0o755 permissions
            swift_linux_out = parsers_dir / "swift" / "packrat-swift-linux-x86_64"
            swift_mac_out = parsers_dir / "swift" / "packrat-swift-darwin-arm64"
            for bin_file in (dart_linux_out, dart_mac_out, swift_linux_out, swift_mac_out):
                self.assertTrue(bin_file.is_file(), f"missing {bin_file}")
                mode = stat.S_IMODE(bin_file.stat().st_mode)
                self.assertEqual(mode, 0o755)

            # 5. Stamps have both platforms with built_on="darwin-arm64" and zero staleness
            dart_stamp = read_stamp(parsers_dir / "dart")
            assert dart_stamp is not None
            self.assertEqual(set(dart_stamp.platforms), {"linux-x86_64", "darwin-arm64"})
            self.assertEqual(dart_stamp.platforms["linux-x86_64"].built_on, "darwin-arm64")
            self.assertEqual(parser_build.staleness(parsers_dir), [])
            rows = parser_build.status_rows(parsers_dir)
            self.assertEqual(
                rows["dart-analyzer"],
                {"darwin-arm64": "current", "linux-x86_64": "current"},
            )
            self.assertEqual(
                rows["swift-syntax"],
                {"darwin-arm64": "current", "linux-x86_64": "current"},
            )

    def test_build_native_linux_and_refuses_darwin_on_linux(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsers_dir = _seed_parsers_tree(Path(tmp))
            runner = _FakeBuildRunner()
            which = lambda name: f"/usr/bin/{name}"

            # Native Linux build defaults to host ("linux-x86_64")
            ok_rep = parser_build.build(
                parsers_dir=parsers_dir,
                runner=runner,
                host="linux-x86_64",
                which=which,
                env={},
            )
            self.assertTrue(ok_rep.ok, ok_rep.errors)
            dart_linux_out = parsers_dir / "dart" / "packrat-dart-linux-x86_64"
            self.assertIn(
                ["/usr/bin/dart", "compile", "exe", "bin/packrat_dart.dart", "-o", str(dart_linux_out)],
                [c[0] for c in runner.calls],
            )

            # Building darwin-arm64 on Linux is refused with actionable error
            err_buf = io.StringIO()
            with mock.patch("sys.stderr", err_buf):
                bad_rep = parser_build.build(
                    targets=["darwin-arm64"],
                    parsers_dir=parsers_dir,
                    runner=runner,
                    host="linux-x86_64",
                    which=which,
                    env={},
                )
            self.assertFalse(bad_rep.ok)
            self.assertEqual(len(bad_rep.errors), 2)
            for err in bad_rep.errors:
                self.assertIn("macOS binaries must be built on macOS", err)
            self.assertIn("macOS binaries must be built on macOS", err_buf.getvalue())

    def test_macos_swift_cross_compile_refuses_apple_xcode_and_missing_musl_sdk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsers_dir = _seed_parsers_tree(Path(tmp))
            which = lambda name: f"/usr/local/bin/{name}"

            # 1. Apple Xcode Swift refused
            xcode_runner = _FakeBuildRunner(
                swift_version="Apple Swift version 6.0 (swiftlang-6.0.0.9.10 clang-1600.0.26.2)"
            )
            with mock.patch("sys.stderr", io.StringIO()):
                rep_xcode = parser_build.build(
                    targets=["linux-x86_64"],
                    parsers_dir=parsers_dir,
                    runner=xcode_runner,
                    host="darwin-arm64",
                    which=which,
                    env={},
                )
            self.assertFalse(rep_xcode.ok)
            self.assertTrue(any("swiftly" in e and "Apple Swift version" in e for e in rep_xcode.errors))

            # 2. Open-source Swift without x86_64-swift-linux-musl SDK refused
            no_sdk_runner = _FakeBuildRunner(
                swift_version="Swift version 6.0.3 (swift-6.0.3-RELEASE)",
                swift_sdks="aarch64-swift-linux-musl\n",
            )
            with mock.patch("sys.stderr", io.StringIO()):
                rep_sdk = parser_build.build(
                    targets=["linux-x86_64"],
                    parsers_dir=parsers_dir,
                    runner=no_sdk_runner,
                    host="darwin-arm64",
                    which=which,
                    env={},
                )
            self.assertFalse(rep_sdk.ok)
            self.assertTrue(
                any("swiftly" in e and "x86_64-swift-linux-musl" in e for e in rep_sdk.errors)
            )

    def test_staleness_and_status_rows_detect_stale_stamps_and_unstamped_binaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            parsers_dir = _seed_parsers_tree(Path(tmp))
            dart_dir = parsers_dir / "dart"
            cur_dart_sha = source_sha256(dart_dir)

            # 1. Initially, stamp has no platforms and no binaries -> missing-binary in status_rows, 0 stale
            write_stamp(
                dart_dir,
                Stamp(
                    parser="dart-analyzer",
                    protocol=1,
                    parser_ids={".dart": "dart-analyzer@1"},
                    platforms={},
                ),
            )
            self.assertEqual(
                parser_build.status_rows(parsers_dir)["dart-analyzer"],
                {"darwin-arm64": "missing-binary", "linux-x86_64": "missing-binary"},
            )
            self.assertEqual(parser_build.staleness(parsers_dir), [])

            # 2. Binary committed without a matching platform stamp entry -> stale!
            linux_bin = dart_dir / binary_name("dart-analyzer", "linux-x86_64")
            linux_bin.write_bytes(b"\x7fELF")
            rows_unstamped = parser_build.status_rows(parsers_dir)["dart-analyzer"]
            self.assertEqual(rows_unstamped["linux-x86_64"], "stale")
            self.assertEqual(rows_unstamped["darwin-arm64"], "missing-binary")
            stale_unstamped = parser_build.staleness(parsers_dir)
            self.assertEqual(len(stale_unstamped), 1)
            self.assertEqual(stale_unstamped[0].parser, "dart-analyzer")
            self.assertEqual(stale_unstamped[0].platform, "linux-x86_64")
            self.assertEqual(stale_unstamped[0].status, "stale")
            self.assertTrue(stale_unstamped[0].binary_present)

            # 3. Matching stamp written for linux-x86_64, lagging stamp for darwin-arm64
            mac_bin = dart_dir / binary_name("dart-analyzer", "darwin-arm64")
            mac_bin.write_bytes(b"\xcf\xfa\xed\xfe")
            write_stamp(
                dart_dir,
                Stamp(
                    parser="dart-analyzer",
                    protocol=1,
                    parser_ids={".dart": "dart-analyzer@1"},
                    platforms={
                        "linux-x86_64": PlatformStamp(source_sha256=cur_dart_sha),
                        "darwin-arm64": PlatformStamp(source_sha256="0000deadbeef"),
                    },
                ),
            )
            rows_mixed = parser_build.status_rows(parsers_dir)["dart-analyzer"]
            self.assertEqual(rows_mixed["linux-x86_64"], "current")
            self.assertEqual(rows_mixed["darwin-arm64"], "stale")
            stale_mixed = parser_build.staleness(parsers_dir)
            self.assertEqual(len(stale_mixed), 1)
            self.assertEqual(stale_mixed[0].platform, "darwin-arm64")

    def test_run_tests_conformance_and_unit_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parsers_dir = _seed_parsers_tree(root)
            db_path = root / "repos.sqlite"
            write_stamp(
                parsers_dir / "python",
                Stamp(
                    parser="py-ast",
                    protocol=1,
                    parser_ids={".py": "py-ast@1"},
                    platforms={"any": PlatformStamp(source_sha256=source_sha256(parsers_dir / "python"))},
                ),
            )
            runner = _FakeBuildRunner()
            which = lambda name: f"/usr/bin/{name}"

            out_json = io.StringIO()
            with mock.patch("sys.stdout", out_json):
                rc = parser_build.run_tests(
                    conformance=False,
                    json_output=True,
                    parsers_dir=parsers_dir,
                    db_path=db_path,
                    runner=runner,
                    which=which,
                    env={},
                )
            self.assertEqual(rc, 0)
            payload = json.loads(out_json.getvalue())
            self.assertTrue(payload["ok"])
            unit_by_parser = {u["parser"]: u["status"] for u in payload["unit_tests"]}
            self.assertEqual(
                unit_by_parser,
                {
                    "treesitter": "passed",
                    "dart-analyzer": "passed",
                    "swift-syntax": "passed",
                },
            )

    def test_live_parsers_committed_binaries_and_stamps_are_not_stale(self) -> None:
        """Fitness test: any committed `packrat-*-<platform>` binary must match a current stamp."""
        live_parsers_dir = parser_build.PARSERS_DIR
        for sub in sorted(live_parsers_dir.iterdir()):
            if not sub.is_dir() or sub.name.startswith((".", "__")):
                continue
            stamp = read_stamp(sub)
            cur_sha = source_sha256(sub)
            for bin_file in sorted(sub.glob("packrat-*")):
                if not bin_file.is_file():
                    continue
                self.assertIsNotNone(
                    stamp, f"Committed binary {bin_file.name} in {sub.name} has no stamp.json"
                )
                assert stamp is not None
                prefix = binary_name(stamp.parser, "")
                if not bin_file.name.startswith(prefix):
                    continue
                plat = bin_file.name[len(prefix) :]
                self.assertIn(
                    plat,
                    stamp.platforms,
                    f"Committed binary {bin_file.name} missing '{plat}' in {sub.name}/stamp.json",
                )
                self.assertEqual(
                    stamp.platforms[plat].source_sha256,
                    cur_sha,
                    f"Committed binary {bin_file.name} in {sub.name} has stale source_sha256",
                )
        stale_entries = [e for e in parser_build.staleness(live_parsers_dir) if e.status == "stale"]
        self.assertEqual(stale_entries, [])

    def test_ensure_elf_stack_size_patches_zero_pt_gnu_stack_to_8mib(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            elf_path = Path(tmp) / "fake_musl_binary"
            header = bytearray(120)
            header[:6] = b"\x7fELF\x02\x01"
            struct.pack_into("<Q", header, 32, 64)  # e_phoff = 64
            struct.pack_into("<HH", header, 54, 56, 1)  # e_phentsize = 56, e_phnum = 1
            struct.pack_into("<I", header, 64, 0x6474E551)  # p_type = PT_GNU_STACK
            struct.pack_into("<Q", header, 64 + 40, 0)  # p_memsz = 0 (musl 128 KB default)
            elf_path.write_bytes(header)

            parser_build._ensure_elf_stack_size(elf_path)
            patched = elf_path.read_bytes()
            p_memsz = struct.unpack_from("<Q", patched, 64 + 40)[0]
            self.assertEqual(p_memsz, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
