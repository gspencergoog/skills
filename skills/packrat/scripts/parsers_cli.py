"""CLI handlers for `packrat parsers {status,enable,disable,build,test}`."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from db_store import DEFAULT_DB_PATH, PackratStore
from parsers.conformance import run_conformance
from parsers.stamps import Stamp, host_platform, read_stamp, source_sha256
from symbol_parsers import ROUTING, SymbolParsers

SCRIPT_DIR = Path(__file__).resolve().parent
PARSERS_DIR = SCRIPT_DIR / "parsers"
_BUILD_UNAVAILABLE_MSG = (
    "packrat parsers build/test is not available yet (parser_build module missing)."
)


def _load_parser_build() -> Any | None:
    """Lazily imports `parser_build` (Track 2D), returning None when absent."""
    try:
        return importlib.import_module("parser_build")
    except ModuleNotFoundError:
        return None


def _known_parsers(parsers_dir: Path = PARSERS_DIR) -> dict[str, tuple[Path, Stamp]]:
    """Returns `{parser_name: (dir, stamp)}` for every stamped parser directory."""
    found: dict[str, tuple[Path, Stamp]] = {}
    if not parsers_dir.is_dir():
        return found
    for sub in sorted(parsers_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith((".", "__")):
            continue
        stamp = read_stamp(sub)
        if stamp is not None:
            found[stamp.parser] = (sub, stamp)
    return found


def _host_state_for(
    parser_name: str, session: SymbolParsers, override: str
) -> str:
    """Computes display host availability for one parser row."""
    if override == "disabled":
        return "disabled"
    return "present" if session.is_parser_active(parser_name) else "missing-binary"


def _platform_stamps_for(
    parser_name: str, pdir: Path, stamp: Stamp, build_rows: Mapping[str, Any] | None
) -> dict[str, str]:
    """Computes per-platform `current`/`stale` states for `stamp`."""
    if build_rows is not None and parser_name in build_rows:
        return dict(build_rows[parser_name])
    cur_sha = source_sha256(pdir)
    return {
        plat: ("current" if pstamp.source_sha256 == cur_sha else "stale")
        for plat, pstamp in sorted(stamp.platforms.items())
    }


def collect_parsers_status(
    store: PackratStore, *, scripts_dir: Path = SCRIPT_DIR
) -> dict[str, Any]:
    """Collects per-parser availability, platform stamp staleness, overrides, and failures."""
    parsers_dir = Path(scripts_dir) / "parsers"
    overrides = store.get_parser_overrides()
    last_unavail = store.get_parser_unavailable_reasons()
    build_mod = _load_parser_build()
    build_rows = build_mod.status_rows(parsers_dir) if build_mod is not None else None
    host = host_platform()
    with SymbolParsers(scripts_dir=scripts_dir, overrides=overrides) as session:
        fingerprint = session.fingerprint()
        rows: list[dict[str, Any]] = []
        for parser_name, (pdir, stamp) in sorted(_known_parsers(parsers_dir).items()):
            override = overrides.get(parser_name, "enabled")
            rows.append(
                {
                    "parser": parser_name,
                    "host_state": _host_state_for(parser_name, session, override),
                    "override": override,
                    "parser_ids": dict(stamp.parser_ids),
                    "platforms": _platform_stamps_for(parser_name, pdir, stamp, build_rows),
                    "last_unavailable": last_unavail.get(parser_name, ""),
                }
            )
    return {"host_platform": host, "fingerprint": fingerprint, "parsers": rows}


def format_parsers_status_line(summary: dict[str, Any]) -> str:
    """Formats the one-line `Parsers:` summary printed by `packrat status`."""
    parts = [f"{r['parser']}={r['host_state']}" for r in summary.get("parsers", [])]
    return "Parsers: " + (", ".join(parts) if parts else "none")


def _cmd_status(args: argparse.Namespace, db_path: Path) -> int:
    store = PackratStore(db_path)
    try:
        payload = collect_parsers_status(store)
    finally:
        store.close()
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Host: {payload['host_platform']}")
    print(f"Fingerprint: {payload['fingerprint'] or '<none>'}")
    for row in payload["parsers"]:
        plats = ", ".join(f"{k}={v}" for k, v in row["platforms"].items()) or "none"
        tail = (
            f" last_unavailable={row['last_unavailable']}"
            if row["last_unavailable"]
            else ""
        )
        print(
            f"  {row['parser']}: host={row['host_state']} override={row['override']} "
            f"stamps=[{plats}]{tail}"
        )
    return 0


def _validate_parser_name(name: str) -> tuple[Path, Stamp]:
    known = _known_parsers()
    if name not in known:
        names = ", ".join(sorted(known)) or "<none>"
        raise ValueError(f"Unknown parser '{name}'; known parsers: {names}")
    return known[name]


def _cmd_enable(args: argparse.Namespace, db_path: Path) -> int:
    _validate_parser_name(args.parser)
    store = PackratStore(db_path)
    try:
        store.set_parser_override(args.parser, None)
        store.set_parser_unavailable_reason(args.parser, None)
    finally:
        store.close()
    payload = {"parser": args.parser, "override": "enabled"}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Enabled parser '{args.parser}'.")
    return 0


def _cmd_disable(args: argparse.Namespace, db_path: Path) -> int:
    _, stamp = _validate_parser_name(args.parser)
    store = PackratStore(db_path)
    reset_count = 0
    try:
        store.set_parser_override(args.parser, "disabled")
        if args.reindex:
            ext_pids = sorted(set(stamp.parser_ids.values()))
            exts = sorted(ext for ext, p in ROUTING.items() if p == args.parser)
            reset_count = store.downgrade_parser_index(ext_pids, exts)
    finally:
        store.close()
    payload = {
        "parser": args.parser,
        "override": "disabled",
        "reindexed_files": reset_count,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        msg = f"Disabled parser '{args.parser}'."
        if args.reindex:
            msg += f" Cleared {reset_count} indexed file state row(s) for regex@1 re-index."
        print(msg)
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    build_mod = _load_parser_build()
    if build_mod is None:
        sys.stderr.write(f"Error: {_BUILD_UNAVAILABLE_MSG}\n")
        return 2
    report = build_mod.build(
        args.target or [], all_platforms=bool(args.all_platforms)
    )
    if args.json:
        print(json.dumps(report.to_dict() if hasattr(report, "to_dict") else report.__dict__, indent=2))
    else:
        for entry in getattr(report, "built", ()):
            print(f"Built {entry}")
    return 0 if getattr(report, "ok", True) else 1


def _cmd_test(args: argparse.Namespace, db_path: Path) -> int:
    build_mod = _load_parser_build()
    if build_mod is not None:
        return int(
            build_mod.run_tests(
                conformance=bool(args.conformance), json_output=bool(args.json)
            )
        )
    if not args.conformance:
        sys.stderr.write(f"Error: {_BUILD_UNAVAILABLE_MSG}\n")
        return 2
    overrides = {}
    if db_path.exists():
        store = PackratStore(db_path)
        try:
            overrides = store.get_parser_overrides()
        finally:
            store.close()
    with SymbolParsers(scripts_dir=SCRIPT_DIR, overrides=overrides) as session:
        report = run_conformance(session, PARSERS_DIR)
    payload = {
        "ok": report.ok,
        "outcomes": [
            {
                "parser": o.parser,
                "status": o.status,
                "detail": o.detail,
                "fixtures_checked": o.fixtures_checked,
            }
            for o in report.outcomes
        ],
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for o in report.outcomes:
            print(f"{o.parser}: {o.status} ({o.detail})")
    return 0 if report.ok else 1


def add_parsers_parser(sub: argparse._SubParsersAction[Any]) -> None:
    """Registers `packrat parsers {status,enable,disable,build,test}`."""
    p_parsers = sub.add_parser(
        "parsers", help="Inspect, toggle, build, and test language symbol parsers."
    )
    p_sub = p_parsers.add_subparsers(dest="parsers_action", required=True)

    p_st = p_sub.add_parser("status", help="Show parser availability and stamp status.")
    p_st.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_st.set_defaults(func=cmd_parsers)

    p_en = p_sub.add_parser("enable", help="Re-enable a previously disabled parser.")
    p_en.add_argument("parser", help="Parser name (e.g. py-ast, treesitter, dart-analyzer, swift-syntax).")
    p_en.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_en.set_defaults(func=cmd_parsers)

    p_dis = p_sub.add_parser("disable", help="Disable a parser so its extensions use regex@1.")
    p_dis.add_argument("parser", help="Parser name to disable.")
    p_dis.add_argument(
        "--reindex",
        action="store_true",
        help="Clear indexed_files parser state so matching files downgrade to regex@1 on next sync.",
    )
    p_dis.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_dis.set_defaults(func=cmd_parsers)

    p_bld = p_sub.add_parser("build", help="Build native parser binaries and update stamps.")
    p_bld.add_argument(
        "--target",
        action="append",
        help="Target platform (linux-x86_64 or darwin-arm64; repeatable).",
    )
    p_bld.add_argument(
        "--all-platforms",
        action="store_true",
        help="Build both linux-x86_64 and darwin-arm64 binaries and stamps.",
    )
    p_bld.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_bld.set_defaults(func=cmd_parsers)

    p_tst = p_sub.add_parser("test", help="Run parser unit and conformance tests.")
    p_tst.add_argument(
        "--conformance",
        action="store_true",
        help="Run NDJSON conformance checks across active parser workers.",
    )
    p_tst.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_tst.set_defaults(func=cmd_parsers)


def cmd_parsers(args: argparse.Namespace) -> int:
    """Dispatches `packrat parsers` subcommands."""
    db_path = Path(args.db).expanduser() if getattr(args, "db", None) else DEFAULT_DB_PATH
    action = args.parsers_action
    if action == "status":
        return _cmd_status(args, db_path)
    if action == "enable":
        return _cmd_enable(args, db_path)
    if action == "disable":
        return _cmd_disable(args, db_path)
    if action == "build":
        return _cmd_build(args)
    if action == "test":
        return _cmd_test(args, db_path)
    raise ValueError(f"Unknown parsers action: {action}")
