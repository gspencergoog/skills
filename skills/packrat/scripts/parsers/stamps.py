"""Per-parser stamp I/O, host platform detection, and source hashing."""

from __future__ import annotations

import hashlib
import json
import os
import platform
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from models import SCRIPTS_HASH_EXCLUDED_DIRS

PROTOCOL_VERSION = 1
_TEXT_SOURCE_SUFFIXES = (
    ".py",
    ".dart",
    ".swift",
    ".json",
    ".md",
    ".yaml",
    ".lock",
    ".resolved",
)
_BINARY_PREFIXES: dict[str, str] = {
    "dart": "packrat-dart",
    "dart-analyzer": "packrat-dart",
    "packrat-dart": "packrat-dart",
    "swift": "packrat-swift",
    "swift-syntax": "packrat-swift",
    "packrat-swift": "packrat-swift",
}


@dataclass(frozen=True)
class PlatformStamp:
    """Build metadata for one `(parser, platform)` target."""

    source_sha256: str
    built_at: str = ""
    built_on: str = ""


@dataclass(frozen=True)
class Stamp:
    """Serialized stamp metadata in `parsers/<parser>/stamp.json`."""

    parser: str
    protocol: int
    parser_ids: Mapping[str, str]
    platforms: Mapping[str, PlatformStamp]


def host_platform() -> str:
    """Returns canonical `<os>-<arch>` for the host (e.g. `linux-x86_64`, `darwin-arm64`)."""
    sys_name = platform.system().lower()
    raw_arch = platform.machine().lower()
    if raw_arch in ("amd64", "x86_64"):
        arch = "x86_64"
    elif raw_arch in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = raw_arch
    return f"{sys_name}-{arch}"


def binary_name(parser: str, platform_name: str) -> str:
    """Returns the platform binary filename for a compiled parser."""
    prefix = _BINARY_PREFIXES.get(parser, f"packrat-{parser}")
    return f"{prefix}-{platform_name}"


def _collect_source_files(base: Path) -> list[tuple[str, Path]]:
    """Collects non-stamp text source `(rel_path, path)` pairs under `base`."""
    files: list[tuple[str, Path]] = []
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in SCRIPTS_HASH_EXCLUDED_DIRS and not d.startswith(".")
        )
        root_path = Path(root)
        for fname in sorted(filenames):
            if fname == "stamp.json" or not fname.endswith(_TEXT_SOURCE_SUFFIXES):
                continue
            path = root_path / fname
            if path.is_file() and not path.is_symlink():
                files.append((path.relative_to(base).as_posix(), path))
    return sorted(files, key=lambda item: item[0])


def source_sha256(parser_dir: Path) -> str:
    """Hashes all non-stamp text source files under `parser_dir`, skipping build/fixture dirs."""
    base = Path(parser_dir)
    if not base.is_dir():
        return ""
    hasher = hashlib.sha256()
    for rel_path, path in _collect_source_files(base):
        hasher.update(rel_path.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _parse_platforms(raw: Any) -> dict[str, PlatformStamp]:
    """Decodes the `platforms` object inside a `stamp.json` file."""
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, PlatformStamp] = {}
    for key, val in raw.items():
        if not isinstance(key, str) or not isinstance(val, Mapping):
            continue
        sha = val.get("source_sha256")
        if not isinstance(sha, str):
            continue
        result[key] = PlatformStamp(
            source_sha256=sha,
            built_at=str(val.get("built_at") or ""),
            built_on=str(val.get("built_on") or ""),
        )
    return result


def read_stamp(parser_dir: Path) -> Stamp | None:
    """Reads and validates `parser_dir/stamp.json`, returning `None` if absent or malformed."""
    stamp_path = Path(parser_dir) / "stamp.json"
    if not stamp_path.is_file():
        return None
    try:
        data = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    parser = data.get("parser")
    protocol = data.get("protocol")
    raw_ids = data.get("parser_ids")
    if (
        not isinstance(parser, str)
        or not parser
        or type(protocol) is not int
        or not isinstance(raw_ids, Mapping)
    ):
        return None
    parser_ids = {
        str(k): str(v) for k, v in raw_ids.items() if isinstance(k, str) and isinstance(v, str)
    }
    if not parser_ids:
        return None
    return Stamp(
        parser=parser,
        protocol=protocol,
        parser_ids=parser_ids,
        platforms=_parse_platforms(data.get("platforms")),
    )


def write_stamp(parser_dir: Path, stamp: Stamp) -> Path:
    """Writes `parser_dir/stamp.json` deterministically and returns its path."""
    stamp_path = Path(parser_dir) / "stamp.json"
    payload = {
        "parser": stamp.parser,
        "protocol": int(stamp.protocol),
        "parser_ids": {str(k): str(v) for k, v in sorted(stamp.parser_ids.items())},
        "platforms": {
            str(k): {
                "source_sha256": v.source_sha256,
                "built_at": v.built_at,
                "built_on": v.built_on,
            }
            for k, v in sorted(stamp.platforms.items())
        },
    }
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    stamp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return stamp_path
