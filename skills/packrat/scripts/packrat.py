#!/usr/bin/env python3
"""CLI entrypoint for packrat: multi-repository SQLite WAL + DuckDB + vector mirror."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from auto_sync import (
    disable_auto_sync,
    enable_auto_sync,
    get_auto_sync_status,
)
from db_store import DEFAULT_DB_PATH, PackratStore
from embedder import (
    ensure_bge_model,
    get_default_embedder,
    sync_repo_embeddings,
)
from git_indexer import sync_git_codebase
from github_sync import sync_github_metadata
from models import RepoConfig, SearchFilters, SyncStats
from search_engine import hybrid_rrf_search


def _open_store(db_path_str: str | None) -> PackratStore:
    """Opens PackratStore and refreshes init.sql."""
    db_path = Path(db_path_str).expanduser() if db_path_str else DEFAULT_DB_PATH
    store = PackratStore(db_path)
    store.write_init_sql()
    return store


NO_REPOS_MESSAGE = (
    "Error: No repositories are registered in packrat.\n"
    "Register one with:\n"
    "  packrat repo add <owner>/<name> --path /path/to/local/checkout\n"
)


def _select_repos(store: PackratStore, repo_arg: str | None) -> list[RepoConfig]:
    """Resolves the target repository list from an optional --repo filter."""
    all_repos = store.list_repos()
    if not repo_arg:
        return all_repos
    needle = repo_arg.strip().lower()
    matched = [
        r for r in all_repos if r.full_name.lower() == needle or r.name.lower() == needle
    ]
    if not matched:
        raise ValueError(f"Repository '{repo_arg}' is not registered in packrat.")
    return matched


def _merge_sync_stats(
    repo_name: str,
    git_stats: SyncStats | None,
    gh_stats: SyncStats | None,
    embedded_count: int,
) -> dict[str, Any]:
    """Combines git, GitHub, and embedding sync metrics into a single dictionary."""
    combined = SyncStats(repo_full_name=repo_name)
    if git_stats is not None:
        combined.indexed_ref = git_stats.indexed_ref
        combined.indexed_sha = git_stats.indexed_sha
        combined.commits_upserted = git_stats.commits_upserted
        combined.symbols_upserted = git_stats.symbols_upserted
        combined.doc_sections_upserted = git_stats.doc_sections_upserted
        combined.chunks_upserted += git_stats.chunks_upserted
        if git_stats.skipped_reason:
            combined.skipped_reason = git_stats.skipped_reason
    if gh_stats is not None:
        combined.events_not_modified = gh_stats.events_not_modified
        combined.issues_upserted = gh_stats.issues_upserted
        combined.prs_upserted = gh_stats.prs_upserted
        combined.discussions_upserted = gh_stats.discussions_upserted
        combined.chunks_upserted += gh_stats.chunks_upserted
        if gh_stats.skipped_reason:
            combined.skipped_reason = gh_stats.skipped_reason
    combined.embeddings_updated = embedded_count
    return asdict(combined)


def cmd_status(args: argparse.Namespace) -> int:
    """Handles `packrat status`."""
    store = _open_store(args.db)
    try:
        repos = _select_repos(store, args.repo)
        embedder = get_default_embedder()
        init_sql_path = store.write_init_sql()
        auto_sync = get_auto_sync_status()
        payload: dict[str, Any] = {
            "db_path": str(store.db_path),
            "init_sql_path": str(init_sql_path),
            "active_embedder": embedder.model_id,
            "auto_sync": asdict(auto_sync),
            "repos": [],
        }
        for r in repos:
            payload["repos"].append(
                {
                    "repo_id": r.repo_id,
                    "full_name": r.full_name,
                    "local_path": r.local_path,
                    "default_branch": r.default_branch,
                    "last_indexed_sha": r.last_indexed_sha,
                    "events_etag": r.events_etag,
                    "issues_watermark": r.issues_watermark,
                    "prs_watermark": r.prs_watermark,
                    "discussions_watermark": r.discussions_watermark,
                    "last_full_sweep_at": r.last_full_sweep_at,
                    "counts": store.table_counts(r.repo_id),
                }
            )
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0

        print(f"Database: {payload['db_path']}")
        print(f"DuckDB init.sql: {payload['init_sql_path']}")
        print(f"Embedder: {payload['active_embedder']}")
        if auto_sync.enabled:
            print(
                f"Auto-sync timer: enabled (active={auto_sync.active}, "
                f"every={auto_sync.interval_min}m, fetch={auto_sync.fetch})"
            )
        else:
            print(f"Auto-sync timer: disabled ({auto_sync.detail})")
        if not payload["repos"]:
            print(f"\n{NO_REPOS_MESSAGE.replace('Error: ', '')}", end="")
        for item in payload["repos"]:
            c = item["counts"]
            sha_short = (item["last_indexed_sha"] or "none")[:10]
            print(f"\n[{item['full_name']}] (path={item['local_path']}, sha={sha_short})")
            print(
                f"  GitHub: issues={c['issues']} prs={c['pull_requests']} "
                f"threads={c['review_threads']} comments={c['comments']} "
                f"discussions={c['discussions']}"
            )
            print(
                f"  Code/Vectors: commits={c['commits']} symbols={c['code_symbols']} "
                f"docs={c['doc_sections']} chunks={c['chunks']} "
                f"embeddings={c['chunk_embeddings']}"
            )
        return 0
    finally:
        store.close()


def _sync_single_repo(
    store: PackratStore,
    repo: RepoConfig,
    args: argparse.Namespace,
    embedder: Any,
) -> dict[str, Any]:
    """Executes git, GitHub, and embedding sync for a single repository."""
    git_stats: SyncStats | None = None
    gh_stats: SyncStats | None = None

    if not args.github_only:
        git_stats = sync_git_codebase(
            store,
            repo,
            force=args.force,
            fetch=args.fetch,
            max_commits=args.max_commits,
        )
    if not args.git_only:
        fresh_repo = store.get_repo(repo.full_name) or repo
        gh_stats = sync_github_metadata(
            store,
            fresh_repo,
            force=args.force,
            max_pages=args.max_pages,
        )
    embedded_count = sync_repo_embeddings(store, repo.repo_id, embedder=embedder)
    return _merge_sync_stats(repo.full_name, git_stats, gh_stats, embedded_count)


def _validate_git_sync_paths(
    repos: Sequence[RepoConfig],
    args: argparse.Namespace,
) -> str | None:
    """Returns an actionable error message if any target repo lacks a valid local git path."""
    if args.github_only:
        return None
    for repo in repos:
        if not repo.local_path:
            return (
                f"Error: No local git worktree path is configured for '{repo.full_name}'.\n"
                f"Configure it with:\n"
                f"  packrat repo set-path {repo.full_name} /path/to/local/checkout\n"
                f"Or sync GitHub metadata only with:\n"
                f"  packrat sync --repo {repo.full_name} --github-only"
            )
        repo_dir = Path(repo.local_path).expanduser()
        if not repo_dir.exists() or not (repo_dir / ".git").exists():
            return (
                f"Error: Configured git worktree path for '{repo.full_name}' is not a valid git checkout: {repo.local_path}\n"
                f"Update it with:\n"
                f"  packrat repo set-path {repo.full_name} /path/to/local/checkout\n"
                f"Or sync GitHub metadata only with:\n"
                f"  packrat sync --repo {repo.full_name} --github-only"
            )
    return None


def cmd_sync(args: argparse.Namespace) -> int:
    """Handles `packrat sync`."""
    if args.download_model:
        ensure_bge_model(allow_download=True)
    store = _open_store(args.db)
    try:
        repos = _select_repos(store, args.repo)
        if not repos:
            sys.stderr.write(NO_REPOS_MESSAGE)
            return 2
        path_err = _validate_git_sync_paths(repos, args)
        if path_err is not None:
            sys.stderr.write(f"{path_err}\n")
            return 2
        embedder = get_default_embedder()
        results = [_sync_single_repo(store, r, args, embedder) for r in repos]
        if args.json:
            print(json.dumps({"embedder": embedder.model_id, "results": results}, indent=2))
            return 0
        for res in results:
            sha_disp = (res.get("indexed_sha") or "unchanged")[:10]
            ref_disp = res.get("indexed_ref") or "-"
            etag_note = "304-not-modified" if res.get("events_not_modified") else "checked"
            print(
                f"[{res['repo_full_name']}] ref={ref_disp} ({sha_disp}) events={etag_note} "
                f"commits=+{res['commits_upserted']} symbols=+{res['symbols_upserted']} "
                f"docs=+{res['doc_sections_upserted']} issues=+{res['issues_upserted']} "
                f"prs=+{res['prs_upserted']} discussions=+{res['discussions_upserted']} "
                f"embeddings=+{res['embeddings_updated']}"
            )
            if res.get("skipped_reason"):
                print(f"  note: {res['skipped_reason']}")
        return 0
    finally:
        store.close()


def cmd_search(args: argparse.Namespace) -> int:
    """Handles `packrat search`."""
    store = _open_store(args.db)
    try:
        kinds = tuple(k.strip() for k in (args.kind or "").split(",") if k.strip())
        filters = SearchFilters(
            repo=args.repo,
            kinds=kinds,
            path_prefix=args.path,
            state=args.state,
        )
        hits = hybrid_rrf_search(
            store,
            query=args.query,
            filters=filters,
            mode=args.mode,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps([asdict(h) for h in hits], indent=2))
            return 0
        if not hits:
            print("No matching chunks found.")
            return 0
        for idx, hit in enumerate(hits, start=1):
            cos_str = f"{hit.cosine_score:.3f}" if hit.cosine_score is not None else "-"
            print(
                f"{idx}. [{hit.repo_full_name}] ({hit.source_kind}) {hit.title}\n"
                f"   ref={hit.source_ref} rrf={hit.rrf_score:.4f} "
                f"bm25_rank={hit.bm25_rank or '-'} vec_rank={hit.vector_rank or '-'} cos={cos_str}\n"
                f"   {hit.snippet}\n"
            )
        return 0
    finally:
        store.close()


def cmd_sql(args: argparse.Namespace) -> int:
    """Handles `packrat sql` via read-only DuckDB v2.0 with init.sql."""
    store = _open_store(args.db)
    init_sql_path = store.write_init_sql()
    store.close()

    duckdb_bin = os.environ.get("DUCKDB_BIN") or shutil.which("duckdb") or str(Path.home() / ".local" / "bin" / "duckdb")
    fmt_flag = f"-{args.format}"
    try:
        proc = subprocess.run(
            [
                duckdb_bin,
                "-batch",
                "-dark-mode",
                "-no-stdin",
                "-init",
                str(init_sql_path),
                fmt_flag,
                ":memory:",
                args.query,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"Error executing duckdb: {exc}\n")
        return 2
    if proc.stdout:
        sys.stdout.write(proc.stdout)
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    return proc.returncode


def _split_full_name(full_name: str) -> tuple[str, str] | None:
    """Splits `<owner>/<name>` into its parts, or returns None if malformed."""
    owner, sep, name = full_name.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        return None
    return owner, name


def _cmd_repo_register(store: PackratStore, args: argparse.Namespace) -> int:
    """Handles `packrat repo add` and `packrat repo set-path`."""
    parts = _split_full_name(args.full_name)
    if parts is None:
        sys.stderr.write("Expected <owner>/<name> format.\n")
        return 2
    resolved_path = str(Path(args.path).expanduser().resolve()) if args.path else None
    rec = store.register_repo(
        parts[0],
        parts[1],
        local_path=resolved_path,
        default_branch=args.branch,
    )
    print(f"Configured {rec.full_name} (repo_id={rec.repo_id}, path={rec.local_path})")
    return 0


def _cmd_repo_rename(store: PackratStore, args: argparse.Namespace) -> int:
    """Handles `packrat repo rename`."""
    parts = _split_full_name(args.new_full_name)
    if parts is None:
        sys.stderr.write("Expected <owner>/<name> format for the new name.\n")
        return 2
    try:
        rec = store.rename_repo(args.full_name, parts[0], parts[1])
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    print(f"Renamed {args.full_name} to {rec.full_name} (repo_id={rec.repo_id})")
    return 0


def _cmd_repo_remove(store: PackratStore, args: argparse.Namespace) -> int:
    """Handles `packrat repo remove`."""
    if not store.remove_repo(args.full_name):
        sys.stderr.write(f"Error: Repository '{args.full_name}' is not registered in packrat.\n")
        return 2
    print(f"Removed {args.full_name} and all of its synced data.")
    return 0


def cmd_repo(args: argparse.Namespace) -> int:
    """Handles `packrat repo {list,add,set-path,rename,remove}`."""
    store = _open_store(args.db)
    try:
        if args.repo_action in ("add", "set-path"):
            return _cmd_repo_register(store, args)
        if args.repo_action == "rename":
            return _cmd_repo_rename(store, args)
        if args.repo_action == "remove":
            return _cmd_repo_remove(store, args)

        repos = store.list_repos()
        if args.json:
            print(json.dumps([asdict(r) for r in repos], indent=2))
            return 0
        if not repos:
            sys.stderr.write(NO_REPOS_MESSAGE.replace("Error: ", ""))
            return 0
        for r in repos:
            print(f"{r.full_name}\tbranch={r.default_branch}\tpath={r.local_path or '-'}")
        return 0
    finally:
        store.close()


def cmd_install_cli(args: argparse.Namespace) -> int:
    """Handles `packrat install-cli` by symlinking packrat.py into ~/.local/bin/packrat."""
    bin_dir = Path(args.bin_dir).expanduser().resolve()
    bin_dir.mkdir(parents=True, exist_ok=True)
    target_script = Path(__file__).resolve()
    target_script.chmod(target_script.stat().st_mode | 0o111)
    link_path = bin_dir / "packrat"
    if link_path.exists() or link_path.is_symlink():
        link_path.unlink()
    link_path.symlink_to(target_script)
    print(f"Installed symlink: {link_path} -> {target_script}")
    path_entries = [
        str(Path(p).expanduser().resolve())
        for p in os.environ.get("PATH", "").split(":")
        if p
    ]
    if str(bin_dir) not in path_entries:
        print(f"Note: '{bin_dir}' is not on your PATH. Add it in your shell profile.")
    return 0


def cmd_auto_sync(args: argparse.Namespace) -> int:
    """Handles `packrat auto-sync {on,off,status}` (`timer` alias)."""
    db_path = Path(args.db).expanduser() if args.db else DEFAULT_DB_PATH
    action = (args.action or "status").lower()
    try:
        if action in ("on", "enable"):
            state = enable_auto_sync(
                db_path=db_path,
                interval_min=args.interval_min,
                fetch=not args.no_fetch,
            )
        elif action in ("off", "disable"):
            state = disable_auto_sync()
        else:
            state = get_auto_sync_status()
    except (RuntimeError, ValueError, OSError) as exc:
        sys.stderr.write(f"Error managing auto-sync timer: {exc}\n")
        return 2

    if args.json:
        print(json.dumps(asdict(state), indent=2))
        return 0
    if state.enabled:
        print(
            f"Auto-sync timer: enabled (active={state.active}, "
            f"interval={state.interval_min}m, fetch={state.fetch}, unit={state.timer_unit_path})"
        )
    else:
        print(f"Auto-sync timer: disabled ({state.detail})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Constructs the argparse CLI parser for packrat."""
    parser = argparse.ArgumentParser(
        prog="packrat",
        description="Local multi-repo SQLite WAL + DuckDB + offline 384-dim vector mirror.",
    )
    parser.add_argument("--db", help="Override SQLite database path.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="Show repository watermarks and table counts.")
    p_status.add_argument("--repo", help="Filter by repository (<owner>/<name> or just <name>).")
    p_status.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser("sync", help="Run incremental git diff-tree, GitHub, and vector sync.")
    p_sync.add_argument("--repo", help="Sync only a specific repository.")
    p_sync.add_argument("--force", action="store_true", help="Force full re-index and bypass ETag.")
    p_sync.add_argument("--git-only", action="store_true", help="Sync local git commits/code only.")
    p_sync.add_argument("--github-only", action="store_true", help="Sync GitHub metadata only.")
    p_sync.add_argument("--fetch", action="store_true", help="Run git fetch on canonical remote first.")
    p_sync.add_argument("--max-pages", type=int, default=10, help="Max GraphQL pages per stream.")
    p_sync.add_argument("--max-commits", type=int, default=300, help="Max git commits to ingest.")
    p_sync.add_argument("--download-model", action="store_true", help="Download BGE ONNX model if missing.")
    p_sync.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_sync.set_defaults(func=cmd_sync)

    p_search = sub.add_parser("search", help="Run hybrid RRF (FTS5 BM25 + 384-dim cosine) search.")
    p_search.add_argument("query", help="Natural language or code search query.")
    p_search.add_argument("--repo", help="Filter by repository.")
    p_search.add_argument("--kind", help="Comma-separated chunk kinds (issue,pr,review_thread,comment,discussion,commit,code_symbol,doc_section).")
    p_search.add_argument("--path", help="Filter by file_path prefix.")
    p_search.add_argument("--state", help="Filter issues/PRs by state (OPEN, CLOSED, MERGED).")
    p_search.add_argument("--mode", choices=("hybrid", "fts", "vector"), default="hybrid", help="Retrieval mode.")
    p_search.add_argument("--limit", type=int, default=12, help="Max results to return.")
    p_search.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_search.set_defaults(func=cmd_search)

    p_sql = sub.add_parser("sql", help="Run read-only DuckDB v2.0 SQL over kb.* tables and v_* views.")
    p_sql.add_argument("query", help="SQL query string.")
    p_sql.add_argument("--format", choices=("box", "json", "markdown", "csv"), default="box", help="DuckDB output format.")
    p_sql.set_defaults(func=cmd_sql)

    p_repo = sub.add_parser("repo", help="Manage tracked repositories.")
    repo_sub = p_repo.add_subparsers(dest="repo_action", required=True)
    p_repo_list = repo_sub.add_parser("list", help="List tracked repositories.")
    p_repo_list.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_repo_list.set_defaults(func=cmd_repo)

    p_repo_add = repo_sub.add_parser("add", help="Register or update a repository.")
    p_repo_add.add_argument("full_name", help="Repository in <owner>/<name> format.")
    p_repo_add.add_argument("--path", help="Local git checkout path.")
    p_repo_add.add_argument("--branch", default="main", help="Canonical default branch name.")
    p_repo_add.set_defaults(func=cmd_repo)

    p_repo_set = repo_sub.add_parser("set-path", help="Configure the local git worktree path for a repository.")
    p_repo_set.add_argument("full_name", help="Repository in <owner>/<name> format.")
    p_repo_set.add_argument("path", help="Local git checkout or worktree path.")
    p_repo_set.add_argument("--branch", default="main", help="Canonical default branch name.")
    p_repo_set.set_defaults(func=cmd_repo)

    p_repo_rename = repo_sub.add_parser(
        "rename",
        help="Rename a tracked repository in place after a GitHub transfer or rename, keeping synced data.",
    )
    p_repo_rename.add_argument("full_name", help="Current repository name in <owner>/<name> format.")
    p_repo_rename.add_argument("new_full_name", help="New repository name in <owner>/<name> format.")
    p_repo_rename.set_defaults(func=cmd_repo)

    p_repo_remove = repo_sub.add_parser("remove", help="Stop tracking a repository and delete its synced data.")
    p_repo_remove.add_argument("full_name", help="Repository in <owner>/<name> format.")
    p_repo_remove.set_defaults(func=cmd_repo)

    p_install = sub.add_parser("install-cli", help="Symlink packrat into ~/.local/bin (or --bin-dir).")
    p_install.add_argument(
        "--bin-dir",
        default=str(Path.home() / ".local" / "bin"),
        help="Target directory on PATH for the packrat symlink (default: ~/.local/bin).",
    )
    p_install.set_defaults(func=cmd_install_cli)

    p_auto = sub.add_parser(
        "auto-sync",
        aliases=["timer"],
        help="Enable, disable, or inspect the background systemd user sync timer.",
    )
    p_auto.add_argument(
        "action",
        nargs="?",
        choices=("on", "off", "enable", "disable", "status"),
        default="status",
        help="Timer action: on/enable, off/disable, or status (default: status).",
    )
    p_auto.add_argument(
        "--interval-min",
        type=int,
        default=10,
        help="Sync interval in minutes when enabling (default: 10).",
    )
    p_auto.add_argument(
        "--no-fetch",
        action="store_true",
        help="Do not pass --fetch to background sync runs.",
    )
    p_auto.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_auto.set_defaults(func=cmd_auto_sync)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI dispatcher."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
