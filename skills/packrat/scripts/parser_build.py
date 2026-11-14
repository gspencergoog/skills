"""Build tooling, per-platform stamp management, staleness checks, and test runner for packrat parsers."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from db_store import DEFAULT_DB_PATH, PackratStore
from models import iso_at
from parsers.conformance import ConformanceReport, run_conformance
from parsers.stamps import (
    PROTOCOL_VERSION,
    PlatformStamp,
    Stamp,
    binary_name,
    host_platform,
    read_stamp,
    source_sha256,
    write_stamp,
)
from symbol_parsers import ROUTING, SymbolParsers

SCRIPT_DIR = Path(__file__).resolve().parent
PARSERS_DIR = SCRIPT_DIR / "parsers"

VALID_PLATFORMS: tuple[str, ...] = ("linux-x86_64", "darwin-arm64")
"""Supported compilation target platforms for native packrat symbol parsers."""

SWIFT_LINUX_MUSL_SDK = "x86_64-swift-linux-musl"
"""Static Linux SDK identifier required to cross-compile Swift on macOS."""

_TREESITTER_PARSER_IDS: dict[str, str] = {
    ext: "treesitter@1" for ext, parser in sorted(ROUTING.items()) if parser == "treesitter"
}

_DEFAULT_STAMP_META: dict[str, tuple[str, dict[str, str]]] = {
    "python": ("py-ast", {".py": "py-ast@1"}),
    "treesitter": ("treesitter", _TREESITTER_PARSER_IDS),
    "dart": ("dart-analyzer", {".dart": "dart-analyzer@1"}),
    "swift": ("swift-syntax", {".swift": "swift-syntax@1"}),
}

_COMPILED_PARSERS: frozenset[str] = frozenset({"dart-analyzer", "swift-syntax"})
_COMPILED_DIRS: frozenset[str] = frozenset({"dart", "swift"})


@dataclass(frozen=True)
class StaleEntry:
    """Describes one stale platform stamp or unstamped/missing compiled binary."""

    parser: str
    platform: str
    status: str
    expected_sha256: str
    recorded_sha256: str = ""
    binary_present: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serializes this staleness entry to a JSON-compatible dictionary."""
        return {
            "parser": self.parser,
            "platform": self.platform,
            "status": self.status,
            "expected_sha256": self.expected_sha256,
            "recorded_sha256": self.recorded_sha256,
            "binary_present": self.binary_present,
        }


@dataclass(frozen=True)
class BuildReport:
    """Summary returned by `build()`."""

    ok: bool
    host_platform: str
    targets: tuple[str, ...]
    built: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serializes the build report to a JSON-compatible dictionary."""
        return {
            "ok": self.ok,
            "host_platform": self.host_platform,
            "targets": list(self.targets),
            "built": list(self.built),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
        }


def _is_executable(path: Path) -> bool:
    """Returns True when `path` is an existing executable regular file."""
    return path.is_file() and os.access(path, os.X_OK)


def locate_dart(
    *,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Locates `dart` on `PATH`, `$DART_SDK/bin/dart`, or `$FLUTTER_ROOT/bin/cache/dart-sdk/bin/dart`."""
    found = which("dart")
    if found:
        return found
    environ = os.environ if env is None else env
    candidates: list[Path] = []
    dart_sdk = environ.get("DART_SDK")
    if dart_sdk:
        candidates.append(Path(dart_sdk) / "bin" / "dart")
    flutter_root = environ.get("FLUTTER_ROOT")
    if flutter_root:
        candidates.append(Path(flutter_root) / "bin" / "cache" / "dart-sdk" / "bin" / "dart")
        candidates.append(Path(flutter_root) / "bin" / "dart")
    for cand in candidates:
        if _is_executable(cand):
            return str(cand)
    return None


def locate_uv(
    *,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Locates `uv` on `PATH` or `~/.local/bin/uv`."""
    found = which("uv")
    if found:
        return found
    environ = os.environ if env is None else env
    home = Path(environ["HOME"]) if "HOME" in environ else Path.home()
    local_uv = home / ".local" / "bin" / "uv"
    if _is_executable(local_uv):
        return str(local_uv)
    return None


def _resolve_targets(
    targets: Sequence[str] | None,
    *,
    all_platforms: bool,
    host: str,
) -> tuple[tuple[str, ...], list[str]]:
    """Normalizes requested target platforms and reports any invalid platform names."""
    raw_list = list(targets or ())
    if all_platforms:
        ordered = list(VALID_PLATFORMS) + [t for t in raw_list if t not in VALID_PLATFORMS]
    elif raw_list:
        ordered = list(dict.fromkeys(raw_list))
    else:
        ordered = [host]
    valid: list[str] = []
    errors: list[str] = []
    for plat in ordered:
        if plat not in VALID_PLATFORMS:
            errors.append(
                f"Unsupported target platform '{plat}'; valid platforms: {', '.join(VALID_PLATFORMS)}"
            )
        else:
            valid.append(plat)
    return tuple(valid), errors


def _update_stamp_entry(
    parser_dir: Path,
    subdir_name: str,
    platform_key: str,
    *,
    host: str,
    built_at: str,
) -> Stamp:
    """Updates `parser_dir/stamp.json` for `platform_key` with current `source_sha256`."""
    existing = read_stamp(parser_dir)
    default_parser, default_ids = _DEFAULT_STAMP_META[subdir_name]
    parser_name = existing.parser if existing is not None else default_parser
    protocol = existing.protocol if existing is not None else PROTOCOL_VERSION
    parser_ids = dict(existing.parser_ids) if existing is not None else dict(default_ids)
    platforms = dict(existing.platforms) if existing is not None else {}
    platforms[platform_key] = PlatformStamp(
        source_sha256=source_sha256(parser_dir),
        built_at=built_at,
        built_on=host,
    )
    stamp = Stamp(
        parser=parser_name,
        protocol=protocol,
        parser_ids=parser_ids,
        platforms=platforms,
    )
    write_stamp(parser_dir, stamp)
    return stamp


def _refresh_python_stamp(
    parsers_dir: Path,
    *,
    host: str,
    built_at: str,
    built: list[str],
    skipped: list[str],
) -> None:
    """Refreshes the `any` platform stamp for the in-process Python AST parser."""
    py_dir = parsers_dir / "python"
    if not (py_dir / "symbol_parser.py").is_file() and not (py_dir / "stamp.json").is_file():
        skipped.append("py-ast")
        return
    _update_stamp_entry(py_dir, "python", "any", host=host, built_at=built_at)
    built.append("py-ast:any")


def _warm_treesitter(
    parsers_dir: Path,
    *,
    host: str,
    built_at: str,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    env: Mapping[str, str] | None,
    built: list[str],
    skipped: list[str],
    errors: list[str],
) -> None:
    """Warms the tree-sitter PEP 723 script environment via `uv sync --script` and writes its stamp."""
    ts_dir = parsers_dir / "treesitter"
    ts_script = ts_dir / "symbol_parser.py"
    if not ts_script.is_file():
        skipped.append("treesitter")
        return
    uv_bin = locate_uv(which=which, env=env)
    if uv_bin is None:
        errors.append(
            "Cannot warm treesitter parser: uv not found on PATH or ~/.local/bin/uv."
        )
        return
    proc = runner(
        [uv_bin, "sync", "--script", str(ts_script)],
        cwd=str(ts_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "uv sync failed").strip()
        errors.append(f"treesitter uv sync failed: {detail}")
        return
    _update_stamp_entry(ts_dir, "treesitter", "any", host=host, built_at=built_at)
    built.append("treesitter:any")


def _build_dart_target(
    dart_dir: Path,
    target: str,
    *,
    host: str,
    built_at: str,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    env: Mapping[str, str] | None,
) -> str | None:
    """Compiles `packrat-dart-<target>` and updates `stamp.json`, returning an error or None."""
    if target == "darwin-arm64" and not host.startswith("darwin"):
        return (
            "Cannot build packrat-dart-darwin-arm64 on Linux: "
            "macOS binaries must be built on macOS."
        )
    dart_bin = locate_dart(which=which, env=env)
    if dart_bin is None:
        return (
            f"Cannot build packrat-dart-{target}: dart not found on PATH, "
            "$DART_SDK/bin/dart, or $FLUTTER_ROOT/bin/cache/dart-sdk/bin/dart."
        )
    out_bin = dart_dir / binary_name("dart-analyzer", target)
    if target == "linux-x86_64" and host != "linux-x86_64":
        cmd = [
            dart_bin,
            "compile",
            "exe",
            "bin/packrat_dart.dart",
            "--target-os=linux",
            "--target-arch=x64",
            "-o",
            str(out_bin),
        ]
    else:
        cmd = [dart_bin, "compile", "exe", "bin/packrat_dart.dart", "-o", str(out_bin)]
    proc = runner(cmd, cwd=str(dart_dir), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "dart compile exe failed").strip()
        return f"Failed building packrat-dart-{target}: {detail}"
    if out_bin.is_file():
        out_bin.chmod(0o755)
    _update_stamp_entry(dart_dir, "dart", target, host=host, built_at=built_at)
    return None


def _check_macos_swift_cross_toolchain(
    swift_bin: str,
    swift_dir: Path,
    runner: Callable[..., Any],
) -> str | None:
    """Verifies swift.org toolchain and Static Linux SDK (`x86_64-swift-linux-musl`) on macOS."""
    ver_proc = runner(
        [swift_bin, "--version"],
        cwd=str(swift_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    ver_text = f"{ver_proc.stdout or ''}\n{ver_proc.stderr or ''}"
    is_xcode_swift = "swiftlang-" in ver_text or (
        "Apple Swift version" in ver_text and "(swift-" not in ver_text
    )
    if ver_proc.returncode != 0 or is_xcode_swift:
        return (
            "Cannot cross-compile packrat-swift-linux-x86_64 with Apple Xcode Swift "
            "(Apple Swift version); install an open-source swift.org toolchain via "
            f"swiftly and the {SWIFT_LINUX_MUSL_SDK} Static Linux SDK."
        )
    sdk_proc = runner(
        [swift_bin, "sdk", "list"],
        cwd=str(swift_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    sdk_text = f"{sdk_proc.stdout or ''}\n{sdk_proc.stderr or ''}"
    if sdk_proc.returncode != 0 or (
        SWIFT_LINUX_MUSL_SDK not in sdk_text and "static-linux" not in sdk_text
    ):
        return (
            "Cannot cross-compile packrat-swift-linux-x86_64 on macOS: missing "
            f"{SWIFT_LINUX_MUSL_SDK} Static Linux SDK (install with swiftly and "
            "`swift sdk install`)."
        )
    return None


def _locate_swift_release_binary(swift_dir: Path, *, cross_musl: bool) -> Path | None:
    """Finds the compiled `PackratSwift` binary under `swift_dir/.build/`."""
    build_root = swift_dir / ".build"
    candidates: list[Path] = []
    if cross_musl:
        candidates.append(build_root / SWIFT_LINUX_MUSL_SDK / "release" / "PackratSwift")
        candidates.append(
            build_root / "out" / "Products" / "Release-staticlinux-x86_64" / "PackratSwift"
        )
    else:
        candidates.append(build_root / "out" / "Products" / "Release" / "PackratSwift")
    candidates.append(build_root / "release" / "PackratSwift")
    if build_root.is_dir():
        candidates.extend(sorted(build_root.glob("*/release/PackratSwift")))
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


_ELF_PT_GNU_STACK = 0x6474E551
_ELF_MIN_STACK_BYTES = 8 * 1024 * 1024


def _ensure_elf_stack_size(
    binary_path: Path, min_bytes: int = _ELF_MIN_STACK_BYTES
) -> None:
    """Ensures 64-bit little-endian ELF binaries request at least `min_bytes` of stack in `PT_GNU_STACK`."""
    try:
        header = binary_path.read_bytes()[:8192]
    except OSError:
        return
    if len(header) < 64 or header[:6] != b"\x7fELF\x02\x01":
        return
    e_phoff = struct.unpack_from("<Q", header, 32)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", header, 54)
    if e_phentsize < 56 or e_phoff + e_phentsize * e_phnum > len(header):
        return
    for idx in range(e_phnum):
        off = e_phoff + idx * e_phentsize
        p_type = struct.unpack_from("<I", header, off)[0]
        p_memsz = struct.unpack_from("<Q", header, off + 40)[0]
        if p_type == _ELF_PT_GNU_STACK and p_memsz < min_bytes:
            with binary_path.open("r+b") as fh:
                fh.seek(off + 40)
                fh.write(struct.pack("<Q", min_bytes))
            return


def _stage_swift_binary(swift_dir: Path, out_bin: Path, *, cross_musl: bool) -> str | None:
    """Copies the built `PackratSwift` executable to `out_bin` and marks it `0o755`."""
    src_bin = _locate_swift_release_binary(swift_dir, cross_musl=cross_musl)
    if src_bin is not None:
        if src_bin.resolve() != out_bin.resolve():
            shutil.copy2(src_bin, out_bin)
        _ensure_elf_stack_size(out_bin)
        out_bin.chmod(0o755)
        return None
    if out_bin.is_file():
        _ensure_elf_stack_size(out_bin)
        out_bin.chmod(0o755)
        return None
    return f"Swift build succeeded but PackratSwift binary was not found under {swift_dir / '.build'}."


def _build_swift_target(
    swift_dir: Path,
    target: str,
    *,
    host: str,
    built_at: str,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
) -> str | None:
    """Compiles `packrat-swift-<target>` and updates `stamp.json`, returning an error or None."""
    if target == "darwin-arm64" and not host.startswith("darwin"):
        return (
            "Cannot build packrat-swift-darwin-arm64 on Linux: "
            "macOS binaries must be built on macOS."
        )
    swift_bin = which("swift")
    if not swift_bin:
        return f"Cannot build packrat-swift-{target}: swift executable not found on PATH."
    cross_musl = target == "linux-x86_64" and host.startswith("darwin")
    if cross_musl:
        sdk_err = _check_macos_swift_cross_toolchain(swift_bin, swift_dir, runner)
        if sdk_err is not None:
            return sdk_err
        cmd = [swift_bin, "build", "-c", "release", "--swift-sdk", SWIFT_LINUX_MUSL_SDK]
    else:
        cmd = [swift_bin, "build", "-c", "release"]
    proc = runner(cmd, cwd=str(swift_dir), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "swift build failed").strip()
        return f"Failed building packrat-swift-{target}: {detail}"
    out_bin = swift_dir / binary_name("swift-syntax", target)
    stage_err = _stage_swift_binary(swift_dir, out_bin, cross_musl=cross_musl)
    if stage_err is not None:
        return stage_err
    _update_stamp_entry(swift_dir, "swift", target, host=host, built_at=built_at)
    return None


def _build_dart_targets(
    dart_dir: Path,
    valid_targets: Sequence[str],
    *,
    host: str,
    built_at: str,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    env: Mapping[str, str] | None,
    built: list[str],
    skipped: list[str],
    errors: list[str],
) -> None:
    """Builds all requested target platforms for `dart-analyzer`."""
    if not dart_dir.is_dir():
        skipped.append("dart-analyzer")
        return
    for target in valid_targets:
        err = _build_dart_target(
            dart_dir,
            target,
            host=host,
            built_at=built_at,
            runner=runner,
            which=which,
            env=env,
        )
        if err is not None:
            errors.append(err)
        else:
            built.append(f"dart-analyzer:{target}")


def _build_swift_targets(
    swift_dir: Path,
    valid_targets: Sequence[str],
    *,
    host: str,
    built_at: str,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    built: list[str],
    skipped: list[str],
    errors: list[str],
) -> None:
    """Builds all requested target platforms for `swift-syntax`."""
    if not swift_dir.is_dir():
        skipped.append("swift-syntax")
        return
    for target in valid_targets:
        err = _build_swift_target(
            swift_dir,
            target,
            host=host,
            built_at=built_at,
            runner=runner,
            which=which,
        )
        if err is not None:
            errors.append(err)
        else:
            built.append(f"swift-syntax:{target}")


def build(
    targets: Sequence[str] = (),
    *,
    all_platforms: bool = False,
    parsers_dir: Path = PARSERS_DIR,
    runner: Callable[..., Any] = subprocess.run,
    host: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> BuildReport:
    """Builds native symbol parser binaries, warms tree-sitter, and updates per-platform stamps."""
    resolved_host = host or host_platform()
    built_at = iso_at(now)
    root = Path(parsers_dir)
    valid_targets, errors = _resolve_targets(
        targets, all_platforms=all_platforms, host=resolved_host
    )
    built: list[str] = []
    skipped: list[str] = []

    _refresh_python_stamp(
        root, host=resolved_host, built_at=built_at, built=built, skipped=skipped
    )
    _warm_treesitter(
        root,
        host=resolved_host,
        built_at=built_at,
        runner=runner,
        which=which,
        env=env,
        built=built,
        skipped=skipped,
        errors=errors,
    )
    _build_dart_targets(
        root / "dart",
        valid_targets,
        host=resolved_host,
        built_at=built_at,
        runner=runner,
        which=which,
        env=env,
        built=built,
        skipped=skipped,
        errors=errors,
    )
    _build_swift_targets(
        root / "swift",
        valid_targets,
        host=resolved_host,
        built_at=built_at,
        runner=runner,
        which=which,
        built=built,
        skipped=skipped,
        errors=errors,
    )

    for err in errors:
        sys.stderr.write(f"Error: {err}\n")

    return BuildReport(
        ok=not errors,
        host_platform=resolved_host,
        targets=valid_targets,
        built=tuple(built),
        skipped=tuple(skipped),
        errors=tuple(errors),
    )


def _committed_binaries_in_dir(parser_dir: Path, parser_name: str) -> dict[str, Path]:
    """Finds `packrat-<short>-<platform>` files present directly inside `parser_dir`."""
    prefix = binary_name(parser_name, "")
    found: dict[str, Path] = {}
    if not parser_dir.is_dir():
        return found
    for child in sorted(parser_dir.iterdir()):
        if not child.is_file() or not child.name.startswith(prefix):
            continue
        plat = child.name[len(prefix) :]
        if plat:
            found[plat] = child
    return found


def _compiled_platform_state(
    plat: str,
    cur_sha: str,
    stamp: Stamp | None,
    binaries: Mapping[str, Path],
) -> str:
    """Computes `"current"`, `"stale"`, or `"missing-binary"` for one compiled parser platform."""
    pstamp = stamp.platforms.get(plat) if stamp is not None else None
    has_bin = plat in binaries
    if pstamp is not None and pstamp.source_sha256 != cur_sha:
        return "stale"
    if has_bin:
        return "current" if (pstamp is not None and pstamp.source_sha256 == cur_sha) else "stale"
    return "missing-binary"


def _resolve_parser_name(sub_name: str, stamp: Stamp | None) -> str:
    """Resolves the canonical parser name for a subdirectory under `parsers/`."""
    if stamp is not None:
        return stamp.parser
    default_meta = _DEFAULT_STAMP_META.get(sub_name)
    if default_meta is not None:
        return default_meta[0]
    return sub_name


def _compiled_states_for(
    cur_sha: str,
    stamp: Stamp | None,
    binaries: Mapping[str, Path],
) -> dict[str, str]:
    """Computes platform states for a compiled parser directory."""
    stamped_keys = set(stamp.platforms.keys()) if stamp is not None else set()
    plats = sorted(set(VALID_PLATFORMS) | stamped_keys | set(binaries.keys()))
    return {
        plat: _compiled_platform_state(plat, cur_sha, stamp, binaries) for plat in plats
    }


def _script_states_for(cur_sha: str, stamp: Stamp) -> dict[str, str]:
    """Computes platform states for an in-process or script parser directory."""
    plats = sorted(stamp.platforms.keys()) or ["any"]
    states: dict[str, str] = {}
    for plat in plats:
        pstamp = stamp.platforms.get(plat)
        is_current = pstamp is not None and pstamp.source_sha256 == cur_sha
        states[plat] = "current" if is_current else "stale"
    return states


def _inspect_parser_dir(
    sub: Path,
) -> tuple[str, str, Stamp | None, dict[str, Path], dict[str, str]] | None:
    """Returns `(parser_name, cur_sha, stamp, binaries, platform_states)` for `sub`."""
    stamp = read_stamp(sub)
    parser_name = _resolve_parser_name(sub.name, stamp)
    is_compiled = parser_name in _COMPILED_PARSERS or sub.name in _COMPILED_DIRS
    binaries = _committed_binaries_in_dir(sub, parser_name) if is_compiled else {}
    if stamp is None and not binaries:
        return None
    cur_sha = source_sha256(sub)
    if is_compiled:
        states = _compiled_states_for(cur_sha, stamp, binaries)
    else:
        assert stamp is not None
        states = _script_states_for(cur_sha, stamp)
    return parser_name, cur_sha, stamp, binaries, states


def _iter_parser_subdirs(parsers_dir: Path) -> list[Path]:
    """Returns sorted candidate parser subdirectories under `parsers_dir`."""
    root = Path(parsers_dir)
    if not root.is_dir():
        return []
    return [
        sub
        for sub in sorted(root.iterdir())
        if sub.is_dir() and not sub.name.startswith((".", "__"))
    ]


def status_rows(parsers_dir: Path = PARSERS_DIR) -> dict[str, dict[str, str]]:
    """Returns `{parser_name: {platform: status_str}}` for `packrat parsers status`."""
    rows: dict[str, dict[str, str]] = {}
    for sub in _iter_parser_subdirs(parsers_dir):
        inspected = _inspect_parser_dir(sub)
        if inspected is not None:
            parser_name, _, _, _, states = inspected
            rows[parser_name] = states
    return rows


def _is_stale_or_missing_stamped_binary(
    plat: str, state: str, stamped_plats: set[str]
) -> bool:
    """Returns True when `state` is stale or a stamped platform lacks its binary."""
    if state == "stale":
        return True
    return state == "missing-binary" and plat in stamped_plats


def _recorded_sha_for(stamp: Stamp | None, plat: str) -> str:
    """Returns the recorded `source_sha256` for `plat` in `stamp`, or `""`."""
    if stamp is None:
        return ""
    pstamp = stamp.platforms.get(plat)
    return pstamp.source_sha256 if pstamp is not None else ""


def _stale_entries_for_dir(sub: Path) -> list[StaleEntry]:
    """Collects `StaleEntry` items for a single parser subdirectory."""
    inspected = _inspect_parser_dir(sub)
    if inspected is None:
        return []
    parser_name, cur_sha, stamp, binaries, states = inspected
    stamped_plats = set(stamp.platforms.keys()) if stamp is not None else set()
    entries: list[StaleEntry] = []
    for plat, state in sorted(states.items()):
        if not _is_stale_or_missing_stamped_binary(plat, state, stamped_plats):
            continue
        entries.append(
            StaleEntry(
                parser=parser_name,
                platform=plat,
                status=state,
                expected_sha256=cur_sha,
                recorded_sha256=_recorded_sha_for(stamp, plat),
                binary_present=plat in binaries,
            )
        )
    return entries


def staleness(parsers_dir: Path = PARSERS_DIR) -> list[StaleEntry]:
    """Returns stale or broken platform stamp entries across `parsers_dir`."""
    entries: list[StaleEntry] = []
    for sub in _iter_parser_subdirs(parsers_dir):
        entries.extend(_stale_entries_for_dir(sub))
    return entries


def _run_treesitter_unit_test(
    ts_dir: Path,
    *,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Runs `uv run --offline --script parsers/treesitter/symbol_parser.py --selftest` if present."""
    ts_script = ts_dir / "symbol_parser.py"
    if not ts_script.is_file():
        return None
    uv_bin = locate_uv(which=which, env=env)
    if uv_bin is None:
        return {"parser": "treesitter", "status": "skipped", "detail": "uv toolchain not found"}
    proc = runner(
        [uv_bin, "run", "--offline", "--script", str(ts_script), "--selftest"],
        cwd=str(ts_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return {"parser": "treesitter", "status": "passed", "detail": "selftest passed"}
    detail = (proc.stderr or proc.stdout or "selftest failed").strip()
    return {"parser": "treesitter", "status": "failed", "detail": detail}


def _run_dart_unit_test(
    dart_dir: Path,
    *,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    env: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Runs `dart pub get --offline && dart test` inside `parsers/dart` if present."""
    if not (dart_dir / "pubspec.yaml").is_file():
        return None
    dart_bin = locate_dart(which=which, env=env)
    if dart_bin is None:
        return {
            "parser": "dart-analyzer",
            "status": "skipped",
            "detail": "dart toolchain not found",
        }
    pub_proc = runner(
        [dart_bin, "pub", "get", "--offline"],
        cwd=str(dart_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if pub_proc.returncode != 0:
        detail = (pub_proc.stderr or pub_proc.stdout or "dart pub get --offline failed").strip()
        return {"parser": "dart-analyzer", "status": "failed", "detail": detail}
    test_proc = runner(
        [dart_bin, "test"],
        cwd=str(dart_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if test_proc.returncode == 0:
        return {"parser": "dart-analyzer", "status": "passed", "detail": "dart test passed"}
    detail = (test_proc.stderr or test_proc.stdout or "dart test failed").strip()
    return {"parser": "dart-analyzer", "status": "failed", "detail": detail}


def _run_swift_unit_test(
    swift_dir: Path,
    *,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
) -> dict[str, str] | None:
    """Runs `swift test` inside `parsers/swift` if present."""
    if not (swift_dir / "Package.swift").is_file():
        return None
    swift_bin = which("swift")
    if not swift_bin:
        return {
            "parser": "swift-syntax",
            "status": "skipped",
            "detail": "swift toolchain not found",
        }
    proc = runner(
        [swift_bin, "test"],
        cwd=str(swift_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode == 0:
        return {"parser": "swift-syntax", "status": "passed", "detail": "swift test passed"}
    detail = (proc.stderr or proc.stdout or "swift test failed").strip()
    return {"parser": "swift-syntax", "status": "failed", "detail": detail}


def _collect_unit_test_outcomes(
    parsers_dir: Path,
    *,
    runner: Callable[..., Any],
    which: Callable[[str], str | None],
    env: Mapping[str, str] | None,
) -> list[dict[str, str]]:
    """Runs unit test suites for discovered parser directories."""
    outcomes: list[dict[str, str]] = []
    for item in (
        _run_treesitter_unit_test(
            parsers_dir / "treesitter", runner=runner, which=which, env=env
        ),
        _run_dart_unit_test(parsers_dir / "dart", runner=runner, which=which, env=env),
        _run_swift_unit_test(parsers_dir / "swift", runner=runner, which=which),
    ):
        if item is not None:
            outcomes.append(item)
    return outcomes


def _read_overrides_if_present(db_path: Path) -> dict[str, str]:
    """Reads parser overrides from `db_path` when the SQLite file exists."""
    if not db_path.exists():
        return {}
    try:
        store = PackratStore(db_path)
    except Exception:
        return {}
    try:
        return store.get_parser_overrides()
    finally:
        store.close()


def _format_and_print_test_results(
    report: ConformanceReport,
    unit_outcomes: Sequence[dict[str, str]] | None,
    *,
    json_output: bool,
) -> int:
    """Prints unit and conformance test outcomes and returns the CLI exit code."""
    unit_ok = all(item["status"] != "failed" for item in (unit_outcomes or ()))
    all_ok = report.ok and unit_ok
    outcomes_list = [
        {
            "parser": o.parser,
            "status": o.status,
            "detail": o.detail,
            "fixtures_checked": o.fixtures_checked,
        }
        for o in report.outcomes
    ]
    if json_output:
        payload: dict[str, Any] = {"ok": all_ok, "outcomes": outcomes_list}
        if unit_outcomes is not None:
            payload["unit_tests"] = list(unit_outcomes)
        print(json.dumps(payload, indent=2))
    else:
        for item in unit_outcomes or ():
            print(f"unit:{item['parser']}: {item['status']} ({item['detail']})")
        for o in report.outcomes:
            print(f"{o.parser}: {o.status} ({o.detail})")
    return 0 if all_ok else 1


def run_tests(
    conformance: bool = False,
    json_output: bool = False,
    *,
    parsers_dir: Path = PARSERS_DIR,
    db_path: Path = DEFAULT_DB_PATH,
    runner: Callable[..., Any] = subprocess.run,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
) -> int:
    """Runs parser conformance checks and (when `conformance=False`) native parser unit tests."""
    root = Path(parsers_dir)
    scripts_dir = root.parent
    overrides = _read_overrides_if_present(Path(db_path))
    unit_outcomes = (
        None
        if conformance
        else _collect_unit_test_outcomes(root, runner=runner, which=which, env=env)
    )
    with SymbolParsers(scripts_dir=scripts_dir, overrides=overrides) as session:
        report = run_conformance(session, root)
    return _format_and_print_test_results(report, unit_outcomes, json_output=json_output)
