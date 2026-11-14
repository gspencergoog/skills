#!/usr/bin/env python3
"""CLI entrypoint for packrat: multi-repository SQLite WAL + DuckDB + vector mirror."""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import os
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

if TYPE_CHECKING:
    from auto_sync import AutoSyncStatus
    from db_store import PackratStore
    from github_sync import GhAuthProbe
    from models import (
        ExportSummary,
        FreshnessKey,
        ImportRepoOutcome,
        ImportSummary,
        RepoConfig,
        SyncStats,
    )
    from progress import ProgressReporter
    from rate_budget import RateBudget, RateSnapshot
    from sync_runtime import AutoSyncRunRecord, LockMode, SyncLockInfo

_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    "AutoSyncStatus": ("auto_sync", "AutoSyncStatus"),
    "disable_auto_sync": ("auto_sync", "disable_auto_sync"),
    "enable_auto_sync": ("auto_sync", "enable_auto_sync"),
    "get_auto_sync_status": ("auto_sync", "get_auto_sync_status"),
    "SchemaTooNewError": ("db_migrations", "SchemaTooNewError"),
    "DEFAULT_DB_PATH": ("db_store", "DEFAULT_DB_PATH"),
    "ExportIntegrityError": ("db_store", "ExportIntegrityError"),
    "ImportAbortedError": ("db_store", "ImportAbortedError"),
    "ImportIntegrityError": ("db_store", "ImportIntegrityError"),
    "PackratStore": ("db_store", "PackratStore"),
    "preview_import_database": ("db_store", "preview_import_database"),
    "validate_import_preflight": ("db_store", "validate_import_preflight"),
    "ensure_onnx_model": ("embedder", "ensure_onnx_model"),
    "get_default_embedder": ("embedder", "get_default_embedder"),
    "get_default_embedder_model_id": ("embedder", "get_default_embedder_model_id"),
    "sync_repo_embeddings": ("embedder", "sync_repo_embeddings"),
    "run_gh_command": ("gh_transport", "run_gh_command"),
    "sync_git_codebase": ("git_indexer", "sync_git_codebase"),
    "check_remote": ("managed_git", "check_remote"),
    "ensure_git": ("managed_git", "ensure_git"),
    "fetch_git": ("managed_git", "fetch_git"),
    "git_status": ("managed_git", "git_status"),
    "move_git": ("managed_git", "move_git"),
    "prune_orphans": ("managed_git", "prune_orphans"),
    "remove_git": ("managed_git", "remove_git"),
    "run_gc": ("managed_git", "run_gc"),
    "validate_url": ("managed_git", "validate_url"),
    "DEFAULT_BACKFILL_MAX_PAGES": ("github_sync", "DEFAULT_BACKFILL_MAX_PAGES"),
    "GhAuthProbe": ("github_sync", "GhAuthProbe"),
    "probe_gh_auth": ("github_sync", "probe_gh_auth"),
    "sync_backfill_only": ("github_sync", "sync_backfill_only"),
    "sync_github_metadata": ("github_sync", "sync_github_metadata"),
    "BACKFILL_STREAMS": ("models", "BACKFILL_STREAMS"),
    "EXIT_LOCKED_RETRY": ("models", "EXIT_LOCKED_RETRY"),
    "RepoConfig": ("models", "RepoConfig"),
    "SearchFilters": ("models", "SearchFilters"),
    "SyncStats": ("models", "SyncStats"),
    "TimelineMode": ("models", "TimelineMode"),
    "UNTRUSTED_PROSE_COLUMNS": ("models", "UNTRUSTED_PROSE_COLUMNS"),
    "utc_now_iso": ("models", "utc_now_iso"),
    "wrap_untrusted_text": ("models", "wrap_untrusted_text"),
    "NULL_REPORTER": ("progress", "NULL_REPORTER"),
    "ProgressReporter": ("progress", "ProgressReporter"),
    "StderrProgressReporter": ("progress", "StderrProgressReporter"),
    "format_duration": ("progress", "format_duration"),
    "RATE_BUDGET_FILENAME": ("rate_budget", "RATE_BUDGET_FILENAME"),
    "RateBudget": ("rate_budget", "RateBudget"),
    "RateSnapshot": ("rate_budget", "RateSnapshot"),
    "hybrid_rrf_search": ("search_engine", "hybrid_rrf_search"),
    "add_live_parsers": ("serve_cli", "add_live_parsers"),
    "cmd_serve": ("serve_cli", "cmd_serve"),
    "fold_timeline_stats": ("serve_cli", "fold_timeline_stats"),
    "format_service_status": ("serve_cli", "format_service_status"),
    "github_sync_stopped": ("serve_cli", "github_sync_stopped"),
    "read_git_poll": ("serve_cli", "read_git_poll"),
    "service_status": ("serve_cli", "service_status"),
    "write_git_poll": ("serve_cli", "write_git_poll"),
    "AutoSyncRunRecord": ("sync_runtime", "AutoSyncRunRecord"),
    "LockMode": ("sync_runtime", "LockMode"),
    "SyncLockInfo": ("sync_runtime", "SyncLockInfo"),
    "SymbolParsers": ("symbol_parsers", "SymbolParsers"),
    "add_parsers_parser": ("parsers_cli", "add_parsers_parser"),
    "collect_parsers_status": ("parsers_cli", "collect_parsers_status"),
    "format_parsers_status_line": ("parsers_cli", "format_parsers_status_line"),
    "acquire_sync_lock": ("sync_runtime", "acquire_sync_lock"),
    "write_auto_sync_run_state": ("sync_runtime", "write_auto_sync_run_state"),
    "sync_repo_timelines": ("timeline_sync", "sync_repo_timelines"),
}


def __getattr__(name: str) -> Any:
    """Lazily loads command-specific symbols on first attribute access (PEP 562)."""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    mod_name, attr_name = target
    mod = __import__(mod_name, fromlist=[attr_name])
    val = getattr(mod, attr_name)
    globals()[name] = val
    return val


def _lazy(name: str) -> Any:
    """Resolves a symbol via module globals (honoring mock.patch) or PEP 562 __getattr__."""
    if name in globals():
        return globals()[name]
    return __getattr__(name)


INIT_SQL_FILENAME = "init.sql"

SYNC_TIMELINE_MAX_BATCHES = 500
"""Timeline batches per pass in `packrat sync` (20 items each, not counting follow-up pages)."""

BACKFILL_INCOMPLETE_TIMELINE_MAX_BATCHES = 25
"""Timeline batch cap under `--backfill-only` while primary stream backfills are still incomplete."""

_DEFAULT_BACKFILL_MAX_PAGES = 250

_EMPTY_TABLE_COUNTS: dict[str, int] = {
    "issues": 0,
    "pull_requests": 0,
    "review_threads": 0,
    "comments": 0,
    "discussions": 0,
    "commits": 0,
    "code_symbols": 0,
    "doc_sections": 0,
    "chunks": 0,
    "timeline_events": 0,
    "timeline_dirty": 0,
    "chunk_embeddings": 0,
}


def _resolve_db_path(db_path_str: str | None) -> Path:
    """Resolves the target SQLite database path from CLI args or default."""
    return (
        Path(db_path_str).expanduser()
        if db_path_str
        else Path(_lazy("DEFAULT_DB_PATH"))
    )


def _open_store(
    db_path_str: str | None, *, refresh_init_sql: bool = False
) -> PackratStore:
    """Opens PackratStore and optionally refreshes init.sql."""
    db_path = _resolve_db_path(db_path_str)
    store = _lazy("PackratStore")(db_path)
    if refresh_init_sql:
        store.write_init_sql()
    return store


NO_REPOS_MESSAGE = (
    "Error: No repositories are registered in packrat.\n"
    "Register one with:\n"
    "  packrat repo add <owner>/<name>\n"
    "Or without a local clone:\n"
    "  packrat repo add <owner>/<name> --github-only\n"
)


def _parse_repo_tokens(repo_arg: str | Sequence[str] | None) -> list[str]:
    """Splits a string or sequence of `--repo` arguments into non-empty tokens."""
    if not repo_arg:
        return []
    items = [repo_arg] if isinstance(repo_arg, str) else list(repo_arg)
    return [
        part.strip() for raw in items for part in str(raw).split(",") if part.strip()
    ]


def _resolve_repo_token(
    store: PackratStore,
    all_repos: Sequence[RepoConfig],
    token: str,
) -> list[RepoConfig]:
    """Resolves one `<owner>/<name>`, alias, or short `<name>` token to registered repos.

    Looks up `store.get_repo(token)` (`full_name` and `repo_aliases`) first. If
    not found, falls back to case-insensitive short `name` matching across
    `all_repos`, preferring `mirror` repos when at least one `mirror` repo
    matches so an auto-registered `items` repo with the same short name never
    creates false ambiguity.

    Raises:
        ValueError: If `token` is unregistered or matches multiple candidate
            repositories by short name.
    """
    cleaned = token.strip()
    exact = store.get_repo(cleaned)
    if exact is not None:
        return [exact]
    needle = cleaned.lower()
    matched = [r for r in all_repos if r.name.lower() == needle]
    if not matched:
        raise ValueError(f"Repository '{cleaned}' is not registered in packrat.")
    mirrors = [r for r in matched if r.is_mirror]
    candidates = mirrors or matched
    if len(candidates) > 1:
        names = ", ".join(r.full_name for r in candidates)
        raise ValueError(
            f"Repository name '{cleaned}' is ambiguous ({names}); pass <owner>/<name>."
        )
    return candidates


def _select_repos(
    store: PackratStore, repo_arg: str | Sequence[str] | None
) -> list[RepoConfig]:
    """Resolves the target repository list from an optional `--repo` filter."""
    all_repos = store.list_repos()
    tokens = _parse_repo_tokens(repo_arg)
    if not tokens:
        return all_repos
    selected: list[RepoConfig] = []
    seen_ids: set[int] = set()
    for token in tokens:
        for repo in _resolve_repo_token(store, all_repos, token):
            if repo.repo_id not in seen_ids:
                seen_ids.add(repo.repo_id)
                selected.append(repo)
    return selected


def _select_mirror_repos(
    store: PackratStore, repo_arg: str | Sequence[str] | None
) -> list[RepoConfig]:
    """Resolves the `mirror` repositories that `packrat sync` or `packrat export` should process.

    `items` repositories hold only watched or notified items; `packrat serve`
    refreshes those, so sync and export skip them unless explicitly named.

    Raises:
        ValueError: If `repo_arg` is unregistered or names any `items` repository.
    """
    tokens = _parse_repo_tokens(repo_arg)
    selected = _select_repos(store, tokens or None)
    if tokens:
        for repo in selected:
            if not repo.is_mirror:
                raise ValueError(
                    f"{repo.full_name} is in items mode; "
                    f"run 'packrat repo add {repo.full_name} [--github-only]' to mirror it."
                )
        return selected
    return [r for r in selected if r.is_mirror]


def _merge_sync_stats(
    repo_name: str,
    git_stats: SyncStats | None,
    gh_stats: SyncStats | None,
    embedding: Any,
    duration_sec: float = 0.0,
) -> dict[str, Any]:
    """Combines git, GitHub, and embedding sync metrics into a single dictionary."""
    combined = _lazy("SyncStats")(
        repo_full_name=repo_name, duration_sec=round(duration_sec, 2)
    )
    if git_stats is not None:
        combined.indexed_ref = git_stats.indexed_ref
        combined.indexed_sha = git_stats.indexed_sha
        combined.commits_upserted = git_stats.commits_upserted
        combined.symbols_upserted = git_stats.symbols_upserted
        combined.doc_sections_upserted = git_stats.doc_sections_upserted
        combined.chunks_upserted += git_stats.chunks_upserted
        combined.syntax_error_count = getattr(git_stats, "syntax_error_count", 0)
        combined.dropped_symbol_count = getattr(git_stats, "dropped_symbol_count", 0)
        combined.reparse_capped = bool(getattr(git_stats, "reparse_capped", False))
        combined.warnings.extend(git_stats.warnings)
        combined.errors.extend(git_stats.errors)
        if git_stats.skipped_reason:
            combined.skipped_reason = git_stats.skipped_reason
    if gh_stats is not None:
        combined.events_not_modified = gh_stats.events_not_modified
        combined.issues_upserted = gh_stats.issues_upserted
        combined.prs_upserted = gh_stats.prs_upserted
        combined.discussions_upserted = gh_stats.discussions_upserted
        combined.chunks_upserted += gh_stats.chunks_upserted
        combined.backfill_incomplete = list(gh_stats.backfill_incomplete)
        combined.deferred = gh_stats.deferred
        combined.deferred_streams = list(gh_stats.deferred_streams)
        combined.timeline = gh_stats.timeline
        combined.warnings.extend(gh_stats.warnings)
        combined.errors.extend(gh_stats.errors)
        if gh_stats.skipped_reason:
            combined.skipped_reason = gh_stats.skipped_reason
    if hasattr(embedding, "embedded_count"):
        combined.embeddings_updated = int(embedding.embedded_count)
        combined.embed_capped = bool(getattr(embedding, "capped", False))
    else:
        combined.embeddings_updated = int(embedding)
    return asdict(combined)


def _backfill_summary(repo: RepoConfig) -> dict[str, dict[str, Any]]:
    """Returns per-stream backfill progress for `packrat status --json`."""
    summary: dict[str, dict[str, Any]] = {}
    for stream in _lazy("BACKFILL_STREAMS"):
        state = repo.backfill(stream)
        summary[stream] = {
            "complete": state.complete,
            "done_at": state.done_at,
            "resumable": state.cursor is not None,
        }
    return summary


def _backfill_warnings(repo: RepoConfig) -> list[str]:
    """Returns one warning per stream whose full history has not been fetched yet.

    `items` repositories never backfill (they hold only watched, notified, or
    viewer items), so they get no warnings.
    """
    warnings: list[str] = []
    if not repo.is_mirror:
        return warnings
    for stream in _lazy("BACKFILL_STREAMS"):
        state = repo.backfill(stream)
        if state.complete:
            continue
        if state.cursor is not None:
            warnings.append(
                f"{stream} backfill incomplete; resumes on next packrat sync"
            )
        else:
            warnings.append(
                f"{stream} backfill incomplete; history not fetched yet, run packrat sync"
            )
    return warnings


def _timeline_warnings(store: PackratStore, repo: RepoConfig) -> list[str]:
    """Returns a warning when stored items of a `mirror` repository lack a first timeline fetch.

    `items` repositories get their timelines from the service's `timeline`
    worker and are not reported.
    """
    if not repo.is_mirror:
        return []
    pending = store.count_timeline_pending(repo.repo_id)
    if not pending:
        return []
    return [f"timeline backfill incomplete ({pending} items pending)"]


def _format_last_auto_sync(last_run: AutoSyncRunRecord | None) -> str | None:
    """Formats the persisted background auto-sync run summary for CLI output."""
    if last_run is None:
        return None
    dur = _lazy("format_duration")(last_run.duration_sec)
    counts = (
        f"repos={last_run.repos_total}, "
        f"commits=+{last_run.git_commits_upserted}, "
        f"github=+{last_run.github_items_upserted}"
    )
    line = f"Last auto-sync: {last_run.ran_at} ({last_run.outcome}), {dur}, {counts}"
    if last_run.outcome == "degraded_keyring_locked":
        hint = (
            "  Hint: OS keyring was locked during background sync; unlock your keyring "
            "or run `gh auth status` interactively to resume GitHub metadata sync."
        )
        return f"{line}\n{hint}"
    if last_run.detail and last_run.outcome != "ok":
        return f"{line}; {last_run.detail}"
    return line


def _rate_budget_for(db_path: Path) -> RateBudget:
    """Returns the shared GitHub rate budget stored next to the database."""
    return _lazy("RateBudget")(db_path.parent / _lazy("RATE_BUDGET_FILENAME"))


def _rate_budget_payload(
    budget: RateBudget, now: float | None = None
) -> dict[str, Any]:
    """Returns the last recorded snapshot per resource for `packrat status`.

    Each entry is the stored snapshot plus `expired`, which is True once its
    window has reset and the numbers no longer apply.
    """
    moment = time.time() if now is None else now
    return {
        name: {**asdict(snap), "expired": snap.expired(moment)}
        for name, snap in sorted(budget.last_snapshots().items())
    }


def _format_rate_snapshot(snap: RateSnapshot, now: float) -> str:
    """Formats one budget snapshot as a single human-readable line."""
    reset_iso = time.strftime("%H:%M:%SZ", time.gmtime(snap.reset_at))
    if snap.expired(now):
        return f"GitHub budget ({snap.resource}): unknown (window reset at {reset_iso})"
    pct = round(snap.fraction_remaining() * 100)
    return (
        f"GitHub budget ({snap.resource}): {snap.remaining}/{snap.limit} ({pct}%), "
        f"resets {reset_iso}"
    )


def _format_human_size(num_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    val = float(num_bytes)
    for unit in units[:-1]:
        if val < 1024.0:
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.2f} {unit}"
        val /= 1024.0
    return f"{val:.2f} {units[-1]}"


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _format_managed_git_line(
    *,
    cloning: bool,
    exists: bool,
    local_tip: str,
    remote_tip: str,
    fetched_at: Any,
    last_error: str,
    backoff: float,
    size_bytes: int,
    now: float,
) -> tuple[str, str]:
    if cloning:
        return "cloning", "git=managed (cloning)"
    if last_error and (not exists or backoff > now):
        retry = max(0, int(backoff - now))
        suffix = f", retry in {retry}s" if backoff > now else ""
        return "error", f"git=managed (error: {last_error}{suffix})"
    if not exists or not local_tip:
        return "pending", "git=managed (pending clone)"
    if remote_tip and remote_tip != local_tip:
        return (
            "behind",
            f"git=managed (behind, tip {local_tip[:8]}, remote {remote_tip[:8]})",
        )
    remote_part = f", remote {remote_tip[:8]}" if remote_tip else ""
    age_part = (
        f", fetched {_format_age(max(0.0, now - float(fetched_at)))} ago"
        if fetched_at
        else ""
    )
    return (
        "ready",
        f"git=managed (ready, tip {local_tip[:8]}{remote_part}{age_part}, {_format_human_size(size_bytes)})",
    )


def _managed_git_status_payload(
    store: PackratStore, repo: RepoConfig, now: float
) -> dict[str, Any]:
    root = store.git_root
    disk = (
        _lazy("git_status")(root, repo)
        if root is not None
        else {
            "git_dir": None,
            "exists": False,
            "cloning": False,
            "local_tip": "",
            "size_bytes": 0,
        }
    )
    poll = _lazy("read_git_poll")(store, repo.repo_id)
    local_tip = str(disk.get("local_tip") or "")
    remote_tip = str(poll.get("remote_sha") or "")
    fetched_at = poll.get("fetched_at")
    last_error = str(poll.get("last_error") or "")
    backoff = float(poll.get("backoff_until") or 0.0)
    size_bytes = int(disk.get("size_bytes") or 0)
    state, line = _format_managed_git_line(
        cloning=bool(disk.get("cloning")),
        exists=bool(disk.get("exists")),
        local_tip=local_tip,
        remote_tip=remote_tip,
        fetched_at=fetched_at,
        last_error=last_error,
        backoff=backoff,
        size_bytes=size_bytes,
        now=now,
    )
    return {
        "git_source": "managed",
        "git_url": repo.git_url,
        "git_dir": disk.get("git_dir"),
        "git_state": state,
        "local_tip": local_tip or None,
        "remote_tip": remote_tip or None,
        "fetched_at": fetched_at,
        "last_error": last_error or None,
        "size_bytes": size_bytes,
        "git_line": line,
    }


def _repo_git_status_payload(
    store: PackratStore, repo: RepoConfig, now: float
) -> dict[str, Any]:
    """Computes `git_*` status fields and formatted `git=` line for one repo."""
    if repo.git_source == "managed":
        return _managed_git_status_payload(store, repo, now)
    if repo.git_source == "none":
        return {
            "git_source": "none",
            "git_url": None,
            "git_dir": None,
            "git_state": "none",
            "local_tip": None,
            "remote_tip": None,
            "fetched_at": None,
            "last_error": None,
            "size_bytes": 0,
            "git_line": "git=none",
        }
    p = Path(repo.local_path).expanduser() if repo.local_path else None
    exists = bool(
        p is not None
        and p.exists()
        and (
            (p / ".git").exists()
            or ((p / "HEAD").is_file() and (p / "objects").is_dir())
        )
    )
    state = "ready" if exists else "missing"
    line = (
        f"git=path ({repo.local_path})"
        if exists
        else f"git=path ({repo.local_path}, missing)"
    )
    return {
        "git_source": "path",
        "git_url": None,
        "git_dir": repo.local_path,
        "git_state": state,
        "local_tip": repo.last_indexed_sha,
        "remote_tip": None,
        "fetched_at": None,
        "last_error": None,
        "size_bytes": 0,
        "git_line": line,
    }


def _print_status_text(payload: dict[str, Any], auto_sync: AutoSyncStatus) -> None:
    """Prints human-readable `packrat status` output to stdout."""
    print(f"Database: {payload['db_path']}")
    print(f"DuckDB init.sql: {payload['init_sql_path']}")
    print(f"Embedder: {payload['active_embedder']}")
    print(_format_auto_sync_mode(auto_sync))
    if "parsers" in payload:
        print(_lazy("format_parsers_status_line")(payload["parsers"]))
    last_line = _format_last_auto_sync(auto_sync.last_run)
    if last_line is not None:
        print(last_line)
    if "service" in payload:
        print(_lazy("format_service_status")(payload["service"]))
    now = time.time()
    snap_cls = _lazy("RateSnapshot")
    for raw in payload.get("rate_budget", {}).values():
        fields = {k: v for k, v in raw.items() if k != "expired"}
        print(_format_rate_snapshot(snap_cls(**fields), now))
    if not payload["repos"]:
        print(f"\n{NO_REPOS_MESSAGE.replace('Error: ', '')}", end="")
    for item in payload["repos"]:
        c = item["counts"]
        sha_short = (item["last_indexed_sha"] or "none")[:10]
        print(f"\n[{item['full_name']}] (path={item['local_path']}, sha={sha_short})")
        if "git_line" in item:
            print(f"  {item['git_line']}")
        print(
            f"  GitHub: issues={c['issues']} prs={c['pull_requests']} "
            f"threads={c['review_threads']} comments={c['comments']} "
            f"discussions={c['discussions']} timeline_events={c['timeline_events']}"
        )
        print(
            f"  Code/Vectors: commits={c['commits']} symbols={c['code_symbols']} "
            f"docs={c['doc_sections']} chunks={c['chunks']} "
            f"embeddings={c['chunk_embeddings']}"
        )
        for warning in item["warnings"]:
            print(f"  Warning: {warning}")


def _resolve_active_embedder_id(
    *, allow_download: bool = False, reporter: Any = None
) -> tuple[str, Any | None]:
    """Resolves `(model_id, custom_embedder_or_None)`, honoring patched `get_default_embedder`."""
    patched = globals().get("get_default_embedder")
    if patched is not None and getattr(patched, "__module__", None) != "embedder":
        kw: dict[str, Any] = {}
        if allow_download:
            kw["allow_download"] = allow_download
        if reporter is not None:
            kw["reporter"] = reporter
        try:
            emb = patched(**kw)
        except TypeError:
            emb = patched()
        return str(emb.model_id), emb
    rep = reporter if reporter is not None else _lazy("NULL_REPORTER")
    model_id = str(
        _lazy("get_default_embedder_model_id")(
            allow_download=allow_download, reporter=rep
        )
    )
    return model_id, None


def cmd_status(args: argparse.Namespace) -> int:
    """Handles `packrat status`."""
    store = _open_store(args.db)
    try:
        repos = _select_repos(store, args.repo)
        embedder_id, _ = _resolve_active_embedder_id()
        init_sql_path = store.db_path.parent / INIT_SQL_FILENAME
        if not init_sql_path.exists():
            init_sql_path = store.write_init_sql()
        auto_sync = _lazy("get_auto_sync_status")(db_path=store.db_path)
        verbose = bool(getattr(args, "verbose", False))
        payload: dict[str, Any] = {
            "db_path": str(store.db_path),
            "init_sql_path": str(init_sql_path),
            "active_embedder": embedder_id,
            "auto_sync": asdict(auto_sync),
            "parsers": _lazy("collect_parsers_status")(store),
            "rate_budget": _rate_budget_payload(_rate_budget_for(store.db_path)),
            "service": _lazy("service_status")(store),
            "repos": [],
        }
        now = time.time()
        for r in repos:
            counts = (
                store.table_counts(r.repo_id)
                if (r.is_mirror or verbose)
                else dict(_EMPTY_TABLE_COUNTS)
            )
            payload["repos"].append(
                {
                    "repo_id": r.repo_id,
                    "full_name": r.full_name,
                    "mode": r.mode,
                    "local_path": r.local_path,
                    "default_branch": r.default_branch,
                    "last_indexed_sha": r.last_indexed_sha,
                    "events_etag": r.events_etag,
                    "issues_watermark": r.issues_watermark,
                    "prs_watermark": r.prs_watermark,
                    "discussions_watermark": r.discussions_watermark,
                    "last_full_sweep_at": r.last_full_sweep_at,
                    **_repo_git_status_payload(store, r, now),
                    "counts": counts,
                    "backfill": _backfill_summary(r),
                    "warnings": _backfill_warnings(r) + _timeline_warnings(store, r),
                }
            )
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        _print_status_text(payload, auto_sync)
        return 0
    finally:
        store.close()


def _skipped_gh_stats_from_probe(repo_name: str, probe: GhAuthProbe) -> SyncStats:
    """Builds a skipped GitHub SyncStats record when preflight auth probing fails."""
    kind = probe.error_kind or "auth_error"
    detail = probe.detail or "gh auth unavailable"
    gh_stats = _lazy("SyncStats")(repo_full_name=repo_name)
    gh_stats.skipped_reason = f"github_{kind}: {detail}"
    gh_stats.errors.append(f"github auth preflight failed ({kind}: {detail})")
    return gh_stats


def _run_github_sync(
    store: PackratStore,
    repo: RepoConfig,
    args: argparse.Namespace,
    reporter: ProgressReporter,
    budget: RateBudget | None,
) -> SyncStats:
    """Runs the GitHub part of a repo sync, then its timeline pass.

    `--backfill-only` runs the backfill walks and then fetches timelines of
    items never fetched before (`TimelineMode.BACKFILL`), capped to
    `BACKFILL_INCOMPLETE_TIMELINE_MAX_BATCHES` while primary streams still have
    pending history so open and recent items stay covered without starving
    stream backfills. Otherwise the full delta sync runs, then every stale
    timeline (`TimelineMode.ALL`) and the cross-reference queue. The timeline
    pass is skipped with `--no-timeline` and after a GitHub sync that stopped
    for a locked keyring or a rate limit.
    """
    backfill_only = bool(getattr(args, "backfill_only", False))
    if backfill_only:
        stats = _lazy("sync_backfill_only")(
            store,
            repo,
            backfill_max_pages=args.backfill_max_pages,
            reporter=reporter,
            budget=budget,
        )
    else:
        stats = _lazy("sync_github_metadata")(
            store,
            repo,
            force=args.force,
            max_pages=args.max_pages,
            backfill_max_pages=args.backfill_max_pages,
            reporter=reporter,
            budget=budget,
        )
    if getattr(args, "no_timeline", False) or _lazy("github_sync_stopped")(stats):
        return stats
    t_mode = _lazy("TimelineMode")
    max_batches = (
        BACKFILL_INCOMPLETE_TIMELINE_MAX_BATCHES
        if backfill_only and stats.backfill_incomplete
        else SYNC_TIMELINE_MAX_BATCHES
    )
    timeline = _lazy("sync_repo_timelines")(
        store,
        store.get_repo(repo.full_name) or repo,
        mode=t_mode.BACKFILL if backfill_only else t_mode.ALL,
        runner=_lazy("run_gh_command"),
        budget=budget,
        max_batches=max_batches,
        drain_xrefs=not backfill_only,
        reporter=reporter,
    )
    _lazy("fold_timeline_stats")(stats, timeline)
    return stats


def _sync_single_repo(
    store: PackratStore,
    repo: RepoConfig,
    args: argparse.Namespace,
    embedder: Any,
    reporter: ProgressReporter | None = None,
    gh_probe: GhAuthProbe | None = None,
    budget: RateBudget | None = None,
    *,
    allow_download: bool = False,
    parsers: Any = None,
    max_reparse_files: int | None = None,
    max_embed_chunks: int | None = None,
) -> dict[str, Any]:
    """Executes git, GitHub, and embedding sync for a single repository.

    Args:
        budget: Shared GitHub rate budget passed to `sync_github_metadata`.
    """
    active_reporter = reporter if reporter is not None else _lazy("NULL_REPORTER")
    started = time.monotonic()
    git_stats: SyncStats | None = None
    gh_stats: SyncStats | None = None
    prior_fp = repo.parser_fingerprint

    if not args.github_only:
        git_stats = _lazy("sync_git_codebase")(
            store,
            repo,
            force=args.force,
            fetch=bool(args.fetch and repo.git_source == "path"),
            max_commits=args.max_commits,
            reporter=active_reporter,
            parsers=parsers,
            max_reparse_files=max_reparse_files,
        )
    if not args.git_only:
        if gh_probe is not None and not gh_probe.ok:
            gh_stats = _skipped_gh_stats_from_probe(repo.full_name, gh_probe)
        else:
            fresh_repo = store.get_repo(repo.full_name) or repo
            gh_stats = _run_github_sync(
                store, fresh_repo, args, active_reporter, budget
            )
    embedding = _lazy("sync_repo_embeddings")(
        store,
        repo.repo_id,
        embedder=embedder,
        max_chunks=max_embed_chunks,
        allow_download=allow_download,
        reporter=active_reporter,
        repo_label=repo.full_name,
    )
    if getattr(embedding, "capped", False) and not args.github_only:
        updated_repo = store.get_repo(repo.full_name)
        if updated_repo is not None and updated_repo.parser_fingerprint != prior_fp:
            store.set_parser_fingerprint(repo.repo_id, prior_fp)
    elapsed = max(0.0, time.monotonic() - started)
    return _merge_sync_stats(
        repo.full_name, git_stats, gh_stats, embedding, duration_sec=elapsed
    )


def _sync_single_repo_guarded(
    store: PackratStore,
    repo: RepoConfig,
    args: argparse.Namespace,
    embedder: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    """Runs `_sync_single_repo`, turning a SQLite write failure into a recorded error.

    A failed write (for example `database or disk is full`) used to abort the
    whole run with a traceback before the auto-sync outcome was recorded. Now
    the repository reports the error, the run ends as `degraded_error`, and
    the next sync resumes from the saved cursors and flags.
    """
    try:
        return _sync_single_repo(store, repo, args, embedder, **kwargs)
    except sqlite3.OperationalError as exc:
        msg = f"sqlite write failed during sync ({exc})"
        reporter = kwargs.get("reporter") or _lazy("NULL_REPORTER")
        reporter.error(repo.full_name, msg)
        failed = _lazy("SyncStats")(repo_full_name=repo.full_name, errors=[msg])
        return asdict(failed)


def _validate_git_sync_paths(
    repos: Sequence[RepoConfig],
    args: argparse.Namespace,
) -> str | None:
    """Returns an actionable error message if any target repo lacks a valid local git path."""
    if args.github_only or getattr(args, "backfill_only", False):
        return None
    git_only = bool(getattr(args, "git_only", False))
    for repo in repos:
        if repo.git_source == "managed":
            continue
        if repo.git_source == "none" or not repo.local_path:
            if not git_only:
                continue
            return (
                f"Error: No local git worktree path or managed clone is configured for '{repo.full_name}'.\n"
                f"Configure one with:\n"
                f"  packrat repo set-path {repo.full_name} /path/to/local/checkout\n"
                f"Or switch to a managed clone:\n"
                f"  packrat repo set-git {repo.full_name} managed\n"
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


def _timeline_summary(res: dict[str, Any]) -> str:
    """Formats the timeline counts of a sync result for the completion line, or ""."""
    timeline = res.get("timeline")
    if not timeline:
        return ""
    return (
        f" timeline=+{timeline['items_synced']} xrefs=+{timeline['xref_items_synced']} "
        f"events=+{timeline['events_written']} graphql_cost={timeline['graphql_cost']}"
    )


def _format_skipped_sync_note(repo_full_name: str, skipped_reason: str) -> str:
    """Formats a repository sync skipped-reason note for `_print_repo_sync_summary`."""
    if skipped_reason == "no_local_path":
        return (
            f"  note: no_local_path (bind a checkout with: "
            f"packrat repo set-path {repo_full_name} /path/to/checkout)"
        )
    if skipped_reason == "pending_clone":
        return (
            f"  note: pending_clone (run `packrat sync --repo {repo_full_name}` "
            f"or `packrat git fetch --repo {repo_full_name}` to clone)"
        )
    return f"  note: {skipped_reason}"


def _print_repo_sync_summary(res: dict[str, Any], reporter: ProgressReporter) -> None:
    """Prints a single repository's sync summary and notes immediately to stdout."""
    reporter.clear_active_line()
    sha_disp = (res.get("indexed_sha") or "unchanged")[:10]
    ref_disp = res.get("indexed_ref") or "-"
    etag_note = "304-not-modified" if res.get("events_not_modified") else "checked"
    dur_sec = float(res.get("duration_sec") or 0.0)
    dur_suffix = f" ({_lazy('format_duration')(dur_sec)})" if dur_sec >= 2.0 else ""
    print(
        f"[{res['repo_full_name']}] ref={ref_disp} ({sha_disp}) events={etag_note} "
        f"commits=+{res['commits_upserted']} symbols=+{res['symbols_upserted']} "
        f"docs=+{res['doc_sections_upserted']} issues=+{res['issues_upserted']} "
        f"prs=+{res['prs_upserted']} discussions=+{res['discussions_upserted']} "
        f"embeddings=+{res['embeddings_updated']}{_timeline_summary(res)}{dur_suffix}",
        flush=True,
    )
    for key in (res.get("timeline") or {}).get("capped_items") or []:
        print(
            f"  note: timeline of {key} has too many pages; marked synced", flush=True
        )
    skipped_reason = res.get("skipped_reason")
    if skipped_reason:
        print(
            _format_skipped_sync_note(str(res["repo_full_name"]), str(skipped_reason)),
            flush=True,
        )
    for stream in res.get("backfill_incomplete") or []:
        print(
            f"  note: {stream} backfill incomplete; run packrat sync again to continue",
            flush=True,
        )
    deferred = res.get("deferred_streams") or []
    if deferred:
        print(
            f"  note: deferred by GitHub rate budget: {', '.join(deferred)}; "
            "resumes on a later sync",
            flush=True,
        )


def _handle_contended_sync_lock(
    args: argparse.Namespace,
    db_path: Path,
    lock_info: SyncLockInfo,
    lock_mode: LockMode,
    started: float,
) -> int:
    """Reports single-flight lock contention and records skipped_locked under --auto."""
    holder = (
        f"pid={lock_info.holder_pid}"
        if lock_info.holder_pid is not None
        else "unknown pid"
    )
    if lock_info.holder_started_at:
        holder = f"{holder}, started_at={lock_info.holder_started_at}"
    detail = f"sync.lock held ({holder})"

    if getattr(args, "auto", False):
        _lazy("write_auto_sync_run_state")(
            db_path,
            _lazy("AutoSyncRunRecord")(
                ran_at=_lazy("utc_now_iso")(),
                duration_sec=round(max(0.0, time.monotonic() - started), 2),
                outcome="skipped_locked",
                detail=detail,
            ),
        )
    if args.json:
        print(
            json.dumps(
                {
                    "skipped": lock_mode == "skip",
                    "reason": "skipped_locked" if lock_mode == "skip" else "locked",
                    "holder_pid": lock_info.holder_pid,
                    "holder_started_at": lock_info.holder_started_at,
                    "results": [],
                },
                indent=2,
            )
        )
    elif lock_mode == "skip":
        if not getattr(args, "quiet", False):
            sys.stderr.write(f"Skipping sync: {detail}\n")
    else:
        sys.stderr.write(f"Error: Another packrat sync is running ({detail}).\n")
    return 0 if lock_mode == "skip" else 1


def _collect_sync_errors(results: Sequence[dict[str, Any]]) -> list[str]:
    """Flattens all error strings recorded across repository sync results."""
    errors: list[str] = []
    for res in results:
        for err in res.get("errors") or ():
            errors.append(str(err))
    return errors


def _deferred_detail(results: Sequence[dict[str, Any]]) -> str:
    """Summarizes rate-budget deferrals as `owner/name: stream, ...; ...`, or ""."""
    parts = [
        f"{res.get('repo_full_name')}: {', '.join(res['deferred_streams'])}"
        for res in results
        if res.get("deferred_streams")
    ]
    if not parts:
        return ""
    return "deferred by GitHub rate budget (" + "; ".join(parts) + ")"


def _classify_sync_outcome(
    results: Sequence[dict[str, Any]],
    gh_probe: GhAuthProbe | None,
) -> tuple[str, str]:
    """Classifies completed repository sync results into `(outcome, detail)`.

    Errors win over deferrals: a run with any error is degraded even when other
    streams were only deferred. Without errors, any deferred stream yields
    `deferred_rate_limit`.
    """
    all_errors = _collect_sync_errors(results)
    if all_errors:
        if all("keyring_locked" in err for err in all_errors):
            return "degraded_keyring_locked", all_errors[0]
        return "degraded_error", all_errors[0]
    deferred = _deferred_detail(results)
    if deferred:
        return "deferred_rate_limit", deferred
    if gh_probe is None or gh_probe.ok:
        return "ok", ""
    if gh_probe.error_kind == "keyring_locked":
        return "degraded_keyring_locked", gh_probe.detail
    return "degraded_error", gh_probe.detail or "sync error"


def _record_auto_sync_run(
    db_path: Path,
    started: float,
    outcome: str,
    results: Sequence[dict[str, Any]],
    detail: str = "",
) -> None:
    """Persists an AutoSyncRunRecord summarizing a background `--auto` execution."""
    git_commits = sum(int(r.get("commits_upserted") or 0) for r in results)
    gh_items = sum(
        int(r.get("issues_upserted") or 0)
        + int(r.get("prs_upserted") or 0)
        + int(r.get("discussions_upserted") or 0)
        for r in results
    )
    _lazy("write_auto_sync_run_state")(
        db_path,
        _lazy("AutoSyncRunRecord")(
            ran_at=_lazy("utc_now_iso")(),
            duration_sec=round(max(0.0, time.monotonic() - started), 2),
            outcome=outcome,
            repos_total=len(results),
            git_commits_upserted=git_commits,
            github_items_upserted=gh_items,
            detail=detail,
        ),
    )


def _probe_github_if_needed(
    args: argparse.Namespace,
    reporter: ProgressReporter,
) -> GhAuthProbe | None:
    """Runs the preflight GitHub token check unless `--git-only` is active."""
    if args.git_only:
        return None
    probe = _lazy("probe_gh_auth")()
    if not probe.ok:
        kind = probe.error_kind or "auth_error"
        detail = probe.detail or "gh auth unavailable"
        reporter.error("github", f"github sync skipped ({kind}: {detail})")
    return probe


def _handle_empty_sync_repos(
    args: argparse.Namespace,
    db_path: Path,
    started: float,
    *,
    is_auto: bool,
) -> int:
    """Handles `packrat sync` when zero matching repositories are registered."""
    if not is_auto:
        sys.stderr.write(NO_REPOS_MESSAGE)
        return 2
    _record_auto_sync_run(
        db_path, started, "skipped_no_repos", [], "no repositories registered"
    )
    if args.json:
        print(
            json.dumps(
                {"skipped": True, "reason": "skipped_no_repos", "results": []},
                indent=2,
            )
        )
    return 0


def _finalize_sync_run(
    args: argparse.Namespace,
    db_path: Path,
    started: float,
    *,
    is_auto: bool,
    embedder_id: str,
    results: list[dict[str, Any]],
    gh_probe: GhAuthProbe | None,
) -> int:
    """Persists auto-sync state, formats JSON output, and computes exit code."""
    outcome, detail = _classify_sync_outcome(results, gh_probe)
    if is_auto:
        _record_auto_sync_run(db_path, started, outcome, results, detail)
    if args.json:
        print(
            json.dumps(
                {"embedder": embedder_id, "outcome": outcome, "results": results},
                indent=2,
            )
        )
    if is_auto:
        return (
            0
            if outcome in ("ok", "degraded_keyring_locked", "deferred_rate_limit")
            else 1
        )
    return 0 if outcome == "ok" else 1


def _propagate_keyring_lock(
    gh_probe: GhAuthProbe | None,
    res: dict[str, Any],
) -> GhAuthProbe | None:
    """Marks `gh_probe` locked when a repository encounters a mid-sync keyring lock."""
    if (
        gh_probe is not None
        and gh_probe.ok
        and any("keyring_locked" in str(err) for err in (res.get("errors") or ()))
    ):
        return _lazy("GhAuthProbe")(
            ok=False,
            error_kind="keyring_locked",
            detail="OS keyring locked during sync",
        )
    return gh_probe


DEFAULT_AUTO_MAX_REPARSE_FILES = 1500
DEFAULT_AUTO_MAX_EMBED_CHUNKS = 15000


def _resolve_auto_caps(
    args: argparse.Namespace, *, is_auto: bool
) -> tuple[int | None, int | None]:
    """Resolves per-run `(max_reparse_files, max_embed_chunks)` caps."""
    raw_reparse = getattr(args, "max_reparse_files", None)
    raw_embed = getattr(args, "max_embed_chunks", None)
    max_reparse = (
        int(raw_reparse)
        if raw_reparse is not None
        else (DEFAULT_AUTO_MAX_REPARSE_FILES if is_auto else None)
    )
    max_embed = (
        int(raw_embed)
        if raw_embed is not None
        else (DEFAULT_AUTO_MAX_EMBED_CHUNKS if is_auto else None)
    )
    return max_reparse, max_embed


def _install_parser_signal_handlers(parsers: Any) -> dict[int, Any]:
    """Installs main-thread SIGTERM/SIGINT handlers closing `parsers` cleanly."""
    if threading.current_thread() is not threading.main_thread() or parsers is None:
        return {}
    previous: dict[int, Any] = {}

    def _on_signal(signum: int, _frame: object) -> None:
        parsers.close()
        raise KeyboardInterrupt(f"received signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[int(sig)] = signal.getsignal(sig)
        signal.signal(sig, _on_signal)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    """Restores prior signal handlers saved by `_install_parser_signal_handlers`."""
    for sig_val, handler in previous.items():
        signal.signal(signal.Signals(sig_val), handler)


def _sync_repos_under_lock(
    args: argparse.Namespace,
    db_path: Path,
    reporter: ProgressReporter,
    started: float,
    budget: RateBudget | None = None,
) -> int:
    """Executes repository syncs while holding the single-flight lock.

    Args:
        budget: Shared GitHub rate budget used by every repository in this run.
    """
    is_auto = bool(getattr(args, "auto", False))
    store = _open_store(args.db, refresh_init_sql=True)
    try:
        repos = _select_mirror_repos(store, args.repo)
        if not repos:
            return _handle_empty_sync_repos(args, db_path, started, is_auto=is_auto)

        if not is_auto:
            path_err = _validate_git_sync_paths(repos, args)
            if path_err is not None:
                sys.stderr.write(f"{path_err}\n")
                return 2

        gh_probe = _probe_github_if_needed(args, reporter)
        allow_download = not getattr(args, "no_download_model", False) and (
            getattr(args, "download_model", False)
            or db_path == Path(_lazy("DEFAULT_DB_PATH"))
        )
        embedder_id, embedder = _resolve_active_embedder_id(
            allow_download=allow_download, reporter=reporter
        )
        max_reparse, max_embed = _resolve_auto_caps(args, is_auto=is_auto)
        results: list[dict[str, Any]] = []
        run_git = not getattr(args, "github_only", False) and not getattr(
            args, "backfill_only", False
        )
        parsers_cm = (
            _lazy("SymbolParsers")(
                overrides=store.get_parser_overrides(), reporter=reporter
            )
            if run_git
            else contextlib.nullcontext(None)
        )
        with parsers_cm as parsers:
            prev_signals = _install_parser_signal_handlers(parsers)
            try:
                for repo in repos:
                    res = _sync_single_repo_guarded(
                        store,
                        repo,
                        args,
                        embedder,
                        reporter=reporter,
                        gh_probe=gh_probe,
                        budget=budget,
                        allow_download=allow_download,
                        parsers=parsers,
                        max_reparse_files=max_reparse,
                        max_embed_chunks=max_embed,
                    )
                    results.append(res)
                    gh_probe = _propagate_keyring_lock(gh_probe, res)
                    if not args.json:
                        _print_repo_sync_summary(res, reporter)
                if parsers is not None:
                    for parser_name, reason in parsers.unavailable().items():
                        store.set_parser_unavailable_reason(parser_name, reason)
            finally:
                _restore_signal_handlers(prev_signals)
        reporter.clear_active_line()

        return _finalize_sync_run(
            args,
            db_path,
            started,
            is_auto=is_auto,
            embedder_id=embedder_id,
            results=results,
            gh_probe=gh_probe,
        )
    finally:
        store.close()


def _record_pre_sync_git_poll(
    db_path_str: str | None,
    repo: RepoConfig,
    *,
    now: float,
    checked_at: float | None = None,
    fetched_at: float | None = None,
    remote_sha: str | None = None,
    remote_default_branch: str | None = None,
    last_error: str | None = None,
) -> RepoConfig:
    """Persists pre-lock managed git metadata in short transactions outside `sync.lock`."""
    store = _open_store(db_path_str, refresh_init_sql=False)
    try:
        if remote_default_branch and remote_default_branch != repo.default_branch:
            store.set_repo_default_branch(repo.repo_id, remote_default_branch, now=now)
        state = _lazy("read_git_poll")(store, repo.repo_id)
        if checked_at is not None:
            state["checked_at"] = checked_at
        if fetched_at is not None:
            state["fetched_at"] = fetched_at
        if remote_sha is not None:
            state["remote_sha"] = remote_sha
        if remote_default_branch:
            state["remote_default_branch"] = remote_default_branch
        if last_error is not None:
            state["last_error"] = last_error
        _lazy("write_git_poll")(store, repo.repo_id, state, now=now)
        return store.get_repo_by_id(repo.repo_id) or repo
    finally:
        store.close()


def _pre_lock_update_branch(
    args: argparse.Namespace,
    repo: RepoConfig,
    new_branch: str,
    *,
    now: float,
    fetched_at: float | None,
    remote_sha: str | None,
    last_error: str,
) -> RepoConfig:
    if not new_branch or new_branch == repo.default_branch:
        return repo
    return _record_pre_sync_git_poll(
        args.db,
        repo,
        now=now,
        fetched_at=fetched_at,
        remote_sha=remote_sha,
        remote_default_branch=new_branch,
        last_error=last_error,
    )


def _ensure_managed_clone_pre_lock(
    args: argparse.Namespace,
    repo: RepoConfig,
    root: Path,
    reporter: ProgressReporter,
) -> None:
    ens = _lazy("ensure_git")(root, repo, reporter=reporter)
    now_done = time.time()
    _record_pre_sync_git_poll(
        args.db,
        repo,
        now=now_done,
        checked_at=now_done,
        fetched_at=now_done if not ens.error else None,
        remote_sha=ens.new_tip or None,
        remote_default_branch=ens.remote_default_branch or None,
        last_error=ens.error,
    )
    if ens.error:
        sys.stderr.write(
            f"Warning: git clone/converge failed for {repo.full_name}: {ens.error}\n"
        )


def _sync_one_managed_repo_pre_lock(
    args: argparse.Namespace,
    repo: RepoConfig,
    root: Path,
    reporter: ProgressReporter,
) -> None:
    """Runs `ensure_git`, `check_remote`, and `fetch_git` for one repo outside `sync.lock`."""
    disk = _lazy("git_status")(root, repo)
    if not disk.get("exists") or disk.get("cloning") or not disk.get("local_tip"):
        _ensure_managed_clone_pre_lock(args, repo, root, reporter)
        return

    now = time.time()
    chk = _lazy("check_remote")(repo)
    if chk.error or chk.timed_out:
        err = chk.error or "ls-remote timed out"
        _record_pre_sync_git_poll(
            args.db, repo, now=now, checked_at=now, last_error=err
        )
        sys.stderr.write(
            f"Warning: git remote check failed for {repo.full_name}: {err}\n"
        )
        return

    repo = _pre_lock_update_branch(
        args,
        repo,
        chk.remote_default_branch,
        now=now,
        fetched_at=None,
        remote_sha=chk.new_tip or None,
        last_error="",
    )
    up_to_date = (
        chk.new_tip
        and chk.new_tip == str(disk.get("local_tip") or "")
        and (
            not chk.remote_default_branch
            or chk.remote_default_branch == repo.default_branch
        )
    )
    if up_to_date:
        _record_pre_sync_git_poll(
            args.db,
            repo,
            now=now,
            checked_at=now,
            remote_sha=chk.new_tip,
            last_error="",
        )
        return

    fetched = _lazy("fetch_git")(root, repo, maintenance="auto")
    now_done = time.time()
    _record_pre_sync_git_poll(
        args.db,
        repo,
        now=now_done,
        checked_at=now,
        fetched_at=now_done if not fetched.error else None,
        remote_sha=fetched.new_tip or chk.new_tip or None,
        remote_default_branch=fetched.remote_default_branch or None,
        last_error=fetched.error,
    )
    if fetched.error:
        sys.stderr.write(
            f"Warning: git fetch failed for {repo.full_name}: {fetched.error}\n"
        )


def _sync_managed_repos_pre_lock(
    args: argparse.Namespace,
    reporter: ProgressReporter,
) -> None:
    """Clones/fetches due `managed` repositories before `sync.lock` is taken."""
    if (
        getattr(args, "github_only", False)
        or getattr(args, "backfill_only", False)
        or getattr(args, "no_fetch", False)
    ):
        return
    store = _open_store(args.db, refresh_init_sql=False)
    try:
        try:
            repos = _select_mirror_repos(store, args.repo)
        except ValueError:
            return
        managed = [r for r in repos if r.git_source == "managed"]
        root = store.git_root
    finally:
        store.close()
    if root is None or not managed:
        return
    for repo in managed:
        _sync_one_managed_repo_pre_lock(args, repo, root, reporter)


def cmd_sync(args: argparse.Namespace) -> int:
    """Handles `packrat sync`."""
    started = time.monotonic()
    db_path = _resolve_db_path(args.db)
    is_auto = bool(getattr(args, "auto", False))
    raw_mode = getattr(args, "lock_mode", None)
    lock_mode: LockMode = raw_mode or ("skip" if is_auto else "wait")

    reporter = _lazy("StderrProgressReporter")(
        mode=getattr(args, "progress", "auto"),
        quiet=bool(getattr(args, "quiet", False)),
    )
    _sync_managed_repos_pre_lock(args, reporter)
    with _lazy("acquire_sync_lock")(db_path, mode=lock_mode) as lock_info:
        if not lock_info.acquired:
            return _handle_contended_sync_lock(
                args, db_path, lock_info, lock_mode, started
            )
        if getattr(args, "download_model", False) and not getattr(
            args, "no_download_model", False
        ):
            _lazy("ensure_onnx_model")(allow_download=True, reporter=reporter)
        budget = _rate_budget_for(db_path)
        return _sync_repos_under_lock(args, db_path, reporter, started, budget=budget)


def _empty_search_hint(store: PackratStore, repo_arg: str | None) -> str | None:
    """Returns a concise stderr diagnostic hint when a 0-hit search runs against an unsynced or incomplete DB."""
    try:
        repos = _select_repos(store, repo_arg)
    except ValueError:
        return None
    if not repos:
        return "Note: no repositories are registered in packrat; run `packrat repo add <owner>/<name>`."
    if not store.has_any_chunks([r.repo_id for r in repos]):
        return "Note: 0 indexed chunks in packrat; run `packrat sync` to populate the mirror."
    incomplete = [r.full_name for r in repos if _backfill_warnings(r)]
    if incomplete:
        joined = ", ".join(incomplete)
        return f"Note: history backfill is incomplete for {joined}; run `packrat status` or `packrat sync`."
    return None


def cmd_search(args: argparse.Namespace) -> int:
    """Handles `packrat search`."""
    store = _open_store(args.db)
    try:
        kinds = tuple(k.strip() for k in (args.kind or "").split(",") if k.strip())
        filters = _lazy("SearchFilters")(
            repo=args.repo,
            kinds=kinds,
            path_prefix=args.path,
            state=args.state,
        )
        hits = _lazy("hybrid_rrf_search")(
            store,
            query=args.query,
            filters=filters,
            mode=args.mode,
            limit=args.limit,
        )
        if not hits:
            hint = _empty_search_hint(store, args.repo)
            if hint:
                sys.stderr.write(f"{hint}\n")
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


def _resolve_duckdb_bin() -> str:
    """Returns the DuckDB CLI binary (`DUCKDB_BIN`, `PATH`, or `~/.local/bin/duckdb`)."""
    return (
        os.environ.get("DUCKDB_BIN")
        or shutil.which("duckdb")
        or str(Path.home() / ".local" / "bin" / "duckdb")
    )


def _run_duckdb(
    init_sql_path: Path, fmt_flag: str, query: str
) -> subprocess.CompletedProcess[str] | None:
    """Runs one read-only DuckDB query, writing a stderr line and returning None on launch failure."""
    try:
        return subprocess.run(
            [
                _resolve_duckdb_bin(),
                "-batch",
                "-dark-mode",
                "-no-stdin",
                "-init",
                str(init_sql_path),
                fmt_flag,
                ":memory:",
                query,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"Error executing duckdb: {exc}\n")
        return None


def _sql_source_ref(row: dict[str, Any], col: str) -> str:
    """Picks the most specific identifier of `row` to label wrapped prose with."""
    for key in ("item_key", "source_ref", "full_name"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    return f"packrat:sql:{col}"


def _wrap_sql_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wraps string values of untrusted prose columns in `<untrusted_repo_content>`."""
    prose_cols = _lazy("UNTRUSTED_PROSE_COLUMNS")
    wrap = _lazy("wrap_untrusted_text")
    wrapped: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        for col, val in row.items():
            if isinstance(val, str) and col.lower() in prose_cols:
                out[col] = wrap(_sql_source_ref(row, col), val)
        wrapped.append(out)
    return wrapped


def _cell_text(val: Any) -> str:
    """Renders one SQL cell for text table output."""
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _render_csv(cols: list[str], rows: list[dict[str, Any]]) -> str:
    """Renders rows as CSV with a header line."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for row in rows:
        writer.writerow([_cell_text(row.get(c)) for c in cols])
    return buf.getvalue()


def _render_markdown(cols: list[str], cells: list[list[str]]) -> str:
    """Renders single-line cells as a GitHub-flavored Markdown table."""
    esc = [[c.replace("|", "\\|") for c in r] for r in cells]
    lines = [
        "| " + " | ".join(cols) + " |",
        "|" + "|".join("---" for _ in cols) + "|",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in esc)
    return "\n".join(lines) + "\n"


def _render_box(cols: list[str], cells: list[list[str]]) -> str:
    """Renders single-line cells as a fixed-width box table."""
    widths = [max(len(c), *(len(r[i]) for r in cells)) for i, c in enumerate(cols)]
    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    header = "|" + "|".join(f" {c.ljust(w)} " for c, w in zip(cols, widths)) + "|"
    lines = [border, header, border]
    lines.extend(
        "|" + "|".join(f" {v.ljust(w)} " for v, w in zip(r, widths)) + "|"
        for r in cells
    )
    lines.append(border)
    return "\n".join(lines) + "\n"


def _render_sql_table(rows: list[dict[str, Any]], fmt: str) -> str:
    """Renders wrapped rows as `json`, `csv`, `markdown`, or `box` text without truncating cells."""
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2) + "\n"
    if not rows:
        return ""
    cols = list(rows[0].keys())
    if fmt == "csv":
        return _render_csv(cols, rows)
    cells = [
        [_cell_text(row.get(c)).replace("\n", "\\n") for c in cols] for row in rows
    ]
    if fmt == "markdown":
        return _render_markdown(cols, cells)
    return _render_box(cols, cells)


def _parse_duckdb_json(stdout: str) -> list[dict[str, Any]]:
    """Parses `duckdb -json` output (one JSON array per statement) into the last result set."""
    rows: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    text = stdout.strip()
    while idx < len(text):
        parsed, end = decoder.raw_decode(text, idx)
        if isinstance(parsed, list):
            rows = [r for r in parsed if isinstance(r, dict)]
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return rows


def cmd_sql(args: argparse.Namespace) -> int:
    """Handles `packrat sql` via read-only DuckDB v2.0 with init.sql.

    Prose columns (`body`, `title`, `text`, ...) hold untrusted repository
    content and are wrapped in `<untrusted_repo_content>` unless `--raw` is set.
    """
    store = _open_store(args.db, refresh_init_sql=True)
    init_sql_path = store.db_path.parent / INIT_SQL_FILENAME
    store.close()

    raw = bool(getattr(args, "raw", False))
    fmt_flag = f"-{args.format}" if raw else "-json"
    proc = _run_duckdb(init_sql_path, fmt_flag, args.query)
    if proc is None:
        return 2
    if proc.stderr:
        sys.stderr.write(proc.stderr)
    if proc.returncode != 0 or raw:
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        return proc.returncode
    try:
        rows = _parse_duckdb_json(proc.stdout)
    except json.JSONDecodeError:
        sys.stdout.write(proc.stdout)
        return 0
    sys.stdout.write(_render_sql_table(_wrap_sql_rows(rows), args.format))
    return 0


def _split_full_name(full_name: str) -> tuple[str, str] | None:
    """Splits `<owner>/<name>` into its parts, or returns None if malformed."""
    owner, sep, name = full_name.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        return None
    return owner, name


def _resolve_repo_add_args(
    args: argparse.Namespace, existing: RepoConfig | None
) -> tuple[str, str | None, str | None, bool, bool]:
    """Resolves `(git_source, local_path, git_url, update_binding, remove_old_git)` for `repo add`."""
    raw_url = getattr(args, "url", None)
    if getattr(args, "github_only", False):
        if raw_url is not None:
            raise ValueError("--url cannot be combined with --github-only.")
        return "none", None, None, True, not getattr(args, "keep_git", False)
    if getattr(args, "path", None):
        if raw_url is not None:
            raise ValueError("--url cannot be combined with --path.")
        resolved = str(Path(args.path).expanduser().resolve())
        return "path", resolved, None, True, not getattr(args, "keep_git", False)
    if existing is not None and existing.git_source == "path" and raw_url is None:
        return "path", existing.local_path, None, False, False
    return "managed", None, raw_url, True, False


def _resolve_set_git_args(
    args: argparse.Namespace,
) -> tuple[str, str | None, str | None, bool, bool]:
    """Resolves `(git_source, local_path, git_url, remove_old_git, clone_now)` for `set-git`/`set-path`."""
    mode = "path" if args.repo_action == "set-path" else str(args.git_mode)
    raw_path = getattr(args, "path", None)
    raw_url = getattr(args, "url", None)
    clone_now = bool(getattr(args, "clone_now", False))
    keep_git = bool(getattr(args, "keep_git", False))
    if mode == "path":
        if not raw_path:
            raise ValueError("`repo set-git path` requires a local directory argument.")
        if raw_url is not None or clone_now:
            raise ValueError("--url and --clone-now apply only to `managed`.")
        return (
            "path",
            str(Path(raw_path).expanduser().resolve()),
            None,
            not keep_git,
            False,
        )
    if raw_path:
        raise ValueError(f"Unexpected path argument for `repo set-git {mode}`.")
    if mode == "none":
        if raw_url is not None or clone_now:
            raise ValueError("--url and --clone-now apply only to `managed`.")
        return "none", None, None, not keep_git, False
    return "managed", None, raw_url, False, clone_now


def _clone_repo_outside_tx(
    db_path_str: str | None,
    root: Path | None,
    rec: RepoConfig,
    *,
    mode: str = "auto",
    quiet: bool = False,
) -> int:
    if root is None:
        return 0
    reporter = _lazy("StderrProgressReporter")(mode=mode, quiet=quiet)
    try:
        res = _lazy("ensure_git")(root, rec, reporter=reporter)
    finally:
        reporter.finish_phase()
    now = time.time()
    _record_pre_sync_git_poll(
        db_path_str,
        rec,
        now=now,
        fetched_at=now if not res.error else None,
        remote_sha=res.new_tip or None,
        remote_default_branch=res.remote_default_branch or None,
        last_error=res.error,
    )
    if res.error:
        sys.stderr.write(f"Error cloning {rec.full_name}: {res.error}\n")
        return 1
    return 0


def _cmd_repo_register(store: PackratStore, args: argparse.Namespace) -> int:
    """Handles `packrat repo add`, `packrat repo set-git`, and `packrat repo set-path`."""
    parts = _split_full_name(args.full_name)
    if parts is None:
        sys.stderr.write("Expected <owner>/<name> format.\n")
        return 2
    existing = store.get_repo(f"{parts[0]}/{parts[1]}")
    if args.repo_action == "add":
        git_source, resolved_path, raw_url, update_binding, remove_old = (
            _resolve_repo_add_args(args, existing)
        )
        clone_now = False
        if (
            existing is not None
            and existing.git_source == "path"
            and git_source == "path"
            and not update_binding
        ):
            sys.stderr.write(
                f"Note: keeping existing path binding for {existing.full_name} "
                f"({existing.local_path}); use `repo set-git {existing.full_name} managed` to switch.\n"
            )
    else:
        if existing is None and args.repo_action == "set-git":
            sys.stderr.write(
                f"Error: Repository '{args.full_name}' is not registered in packrat.\n"
            )
            return 2
        git_source, resolved_path, raw_url, remove_old, clone_now = (
            _resolve_set_git_args(args)
        )
        update_binding = True

    rec = store.register_repo(
        parts[0],
        parts[1],
        local_path=resolved_path,
        default_branch=args.branch or (existing.default_branch if existing else "main"),
        git_source=git_source,  # type: ignore[arg-type]
        git_url=raw_url,
        update_binding=update_binding,
    )
    root = store.git_root
    if (
        remove_old
        and existing is not None
        and existing.git_source == "managed"
        and root is not None
    ):
        _lazy("remove_git")(root, existing)
    print(
        f"Configured {rec.full_name} (repo_id={rec.repo_id}, "
        f"git_source={rec.git_source}, path={rec.local_path})"
    )
    if clone_now and rec.git_source == "managed":
        db_str = str(store.db_path)
        store.close()
        return _clone_repo_outside_tx(db_str, root, rec)
    return 0


def _cmd_repo_rename(store: PackratStore, args: argparse.Namespace) -> int:
    """Handles `packrat repo rename`."""
    parts = _split_full_name(args.new_full_name)
    if parts is None:
        sys.stderr.write("Expected <owner>/<name> format for the new name.\n")
        return 2
    old_repo = store.get_repo(args.full_name)
    try:
        rec = store.rename_repo(args.full_name, parts[0], parts[1])
    except ValueError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    root = store.git_root
    if old_repo is not None and old_repo.git_source == "managed" and root is not None:
        _lazy("move_git")(root, old_repo, rec.owner, rec.name, clone_url=rec.clone_url)
    print(f"Renamed {args.full_name} to {rec.full_name} (repo_id={rec.repo_id})")
    return 0


def _cmd_repo_remove(store: PackratStore, args: argparse.Namespace) -> int:
    """Handles `packrat repo remove`."""
    existing = store.get_repo(args.full_name)
    if not store.remove_repo(args.full_name):
        sys.stderr.write(
            f"Error: Repository '{args.full_name}' is not registered in packrat.\n"
        )
        return 2
    root = store.git_root
    if existing is not None and existing.git_source == "managed" and root is not None:
        _lazy("remove_git")(root, existing)
    print(f"Removed {args.full_name} and all of its synced data.")
    return 0


def cmd_repo(args: argparse.Namespace) -> int:
    """Handles `packrat repo {list,add,set-git,set-path,rename,remove}`."""
    mutating = args.repo_action in ("add", "set-git", "set-path", "rename", "remove")
    store = _open_store(args.db, refresh_init_sql=mutating)
    try:
        if args.repo_action in ("add", "set-git", "set-path"):
            return _cmd_repo_register(store, args)
        if args.repo_action == "rename":
            return _cmd_repo_rename(store, args)
        if args.repo_action == "remove":
            return _cmd_repo_remove(store, args)

        repos = store.list_repos(mode=getattr(args, "mode", None))
        if args.json:
            print(json.dumps([asdict(r) for r in repos], indent=2))
            return 0
        if not repos:
            sys.stderr.write(NO_REPOS_MESSAGE.replace("Error: ", ""))
            return 0
        for r in repos:
            mode_note = "" if r.is_mirror else f"\tmode={r.mode}"
            loc = r.local_path or (r.git_url or "-")
            print(
                f"{r.full_name}\tbranch={r.default_branch}\tgit={r.git_source}\tpath={loc}{mode_note}"
            )
        return 0
    finally:
        store.close()


def _cmd_git_status(store: PackratStore, args: argparse.Namespace) -> int:
    repos = _select_repos(store, getattr(args, "repo", None))
    now = time.time()
    entries = [
        {"full_name": r.full_name, **_repo_git_status_payload(store, r, now)}
        for r in repos
    ]
    if getattr(args, "json", False):
        print(json.dumps(entries, indent=2))
        return 0
    for item in entries:
        print(f"{item['full_name']}\t{item['git_line']}")
    return 0


def _cmd_git_prune(store: PackratStore, args: argparse.Namespace) -> int:
    root = store.git_root
    dry_run = bool(getattr(args, "dry_run", False))
    paths = (
        _lazy("prune_orphans")(root, store.list_repos(), dry_run=dry_run)
        if root is not None
        else []
    )
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"dry_run": dry_run, "pruned": [str(p) for p in paths]}, indent=2
            )
        )
        return 0
    verb = "Would prune" if dry_run else "Pruned"
    for p in paths:
        print(f"{verb} {p}")
    if not paths:
        print("No orphaned managed git directories found.")
    return 0


def _run_single_git_op(db_str: str, root: Path, repo: RepoConfig, op: str) -> Any:
    if op != "fetch":
        return _lazy("run_gc")(root, repo)
    cloned = (root / repo.owner / f"{repo.name}.git").exists()
    res = (
        _lazy("fetch_git")(root, repo, maintenance="auto")
        if cloned
        else _lazy("ensure_git")(root, repo)
    )
    now = time.time()
    _record_pre_sync_git_poll(
        db_str,
        repo,
        now=now,
        fetched_at=now if not res.error else None,
        remote_sha=res.new_tip or None,
        remote_default_branch=res.remote_default_branch or None,
        last_error=res.error,
    )
    return res


def _cmd_git_op(store: PackratStore, args: argparse.Namespace, op: str) -> int:
    repos = [
        r
        for r in _select_repos(store, getattr(args, "repo", None))
        if r.git_source == "managed"
    ]
    root = store.git_root
    db_str = str(store.db_path)
    store.close()
    if root is None:
        return 0
    results = [
        {"repo": r.full_name, **asdict(_run_single_git_op(db_str, root, r, op))}
        for r in repos
    ]
    failed = any(item["error"] for item in results)
    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
        return 1 if failed else 0
    for item in results:
        status = (
            f"error: {item['error']}"
            if item["error"]
            else f"ok ({item['new_tip'][:8] or '-'})"
        )
        print(f"{item['repo']}\t{status}")
    return 1 if failed else 0


def cmd_git(args: argparse.Namespace) -> int:
    """Handles `packrat git {status,fetch,gc,prune}`."""
    store = _open_store(args.db, refresh_init_sql=False)
    try:
        action = args.git_action
        if action == "status":
            return _cmd_git_status(store, args)
        if action == "prune":
            return _cmd_git_prune(store, args)
        return _cmd_git_op(store, args, action)
    finally:
        with contextlib.suppress(sqlite3.ProgrammingError):
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


def _format_auto_sync_mode(state: AutoSyncStatus) -> str:
    """Formats the background-sync line of `packrat status` for service or timer mode."""
    if state.mode == "service":
        return (
            f"Auto-sync: packrat.service (active={state.serve_active}, {state.detail})"
        )
    if state.enabled:
        return (
            f"Auto-sync timer: enabled (active={state.active}, "
            f"every={state.interval_min}m, fetch={state.fetch})"
        )
    return f"Auto-sync: disabled ({state.detail})"


def _format_auto_sync_header(state: AutoSyncStatus) -> str:
    """Formats the primary status line for `packrat auto-sync`."""
    if state.mode == "service":
        verb = {
            "already_enabled": "already enabled (no changes)",
            "updated": "updated",
        }.get(state.action, "enabled")
        return f"Auto-sync service: {verb} (active={state.serve_active}, unit={state.serve_unit_path})"
    if not state.enabled:
        return f"Auto-sync: disabled ({state.detail})"
    details = (
        f"active={state.active}, interval={state.interval_min}m, "
        f"fetch={state.fetch}, unit={state.timer_unit_path}"
    )
    if state.action == "already_enabled":
        return f"Auto-sync timer: already enabled (no changes; {details})"
    if state.action == "updated":
        return f"Auto-sync timer: updated ({details})"
    return f"Auto-sync timer: enabled ({details})"


def cmd_auto_sync(args: argparse.Namespace) -> int:
    """Handles `packrat auto-sync {on,off,status}` (`timer` alias)."""
    db_path = _resolve_db_path(args.db)
    action = (args.action or "status").lower()
    use_timer = bool(args.timer or getattr(args, "command", "") == "timer")
    try:
        if action in ("on", "enable"):
            state = _lazy("enable_auto_sync")(
                db_path=db_path,
                interval_min=args.interval_min,
                fetch=args.fetch,
                mode="timer" if use_timer else "service",
            )
        elif action in ("off", "disable"):
            state = _lazy("disable_auto_sync")(db_path=db_path)
        else:
            state = _lazy("get_auto_sync_status")(db_path=db_path)
    except (RuntimeError, ValueError, OSError) as exc:
        sys.stderr.write(f"Error managing auto-sync: {exc}\n")
        return 2

    if args.json:
        print(json.dumps(asdict(state), indent=2))
        return 0
    print(_format_auto_sync_header(state))
    last_line = _format_last_auto_sync(state.last_run)
    if last_line is not None:
        print(last_line)
    return 0


_EXPORT_IMMEDIATE_PROGRESS_BYTES = 16 * 1024 * 1024


def _format_byte_size(num_bytes: int) -> str:
    """Formats a byte count as a human-readable KiB, MiB, or GiB string."""
    if num_bytes >= 1024**3:
        return f"{num_bytes / (1024**3):.2f} GiB ({num_bytes:,} bytes)"
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MiB ({num_bytes:,} bytes)"
    if num_bytes >= 1024:
        return f"{num_bytes / 1024:.1f} KiB ({num_bytes:,} bytes)"
    return f"{num_bytes:,} bytes"


def _approx_sqlite_footprint(db_path: Path) -> int:
    """Sums the main SQLite file and any active WAL sidecar byte sizes."""
    if str(db_path) == ":memory:":
        return 0
    total = db_path.stat().st_size if db_path.exists() else 0
    wal = Path(f"{db_path}-wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def _print_export_summary(summary: ExportSummary) -> None:
    """Prints human-readable `packrat export` metrics and recipient install instructions."""
    dur = _lazy("format_duration")(summary.duration_sec)
    repos_str = ", ".join(summary.exported_repos)
    example_repo = (
        summary.exported_repos[0] if summary.exported_repos else "<owner>/<name>"
    )
    print(f"Exported packrat database to {summary.dest_db_path} ({dur})")
    print(f"  Schema version: {summary.schema_version}")
    print(f"  Exported repos ({len(summary.exported_repos)}): {repos_str}")
    print(
        f"  Managed git clones on import: {summary.managed_git_repos} repository(ies) "
        "will clone on the recipient's first sync"
    )
    print(
        f"  Removed repos: items={summary.removed_items_repos}, "
        f"mirror={summary.removed_mirror_repos}"
    )
    print(
        f"  Remaining rows: chunks={summary.chunks_remaining:,}, "
        f"embeddings={summary.embeddings_remaining:,}"
    )
    print(
        f"  Size: {_format_byte_size(summary.source_bytes)} -> "
        f"{_format_byte_size(summary.dest_bytes)}"
    )
    quoted_dest = shlex.quote(summary.dest_db_path)
    print(
        "\nRecipient commands (merge into an existing or new packrat DB, "
        "safe while serve runs):"
    )
    print(f"  packrat import {quoted_dest} --clone-now")
    if len(summary.exported_repos) == 1:
        print(f"  packrat import {quoted_dest} --path /path/to/local/checkout")
    else:
        print(f"  packrat repo set-path {example_repo} /path/to/local/checkout")
    print("\nOr cold-start replace on a machine with no existing packrat DB:")
    print("  packrat auto-sync off")
    print(
        "  rm -f ~/.local/share/packrat/repos.sqlite-wal "
        "~/.local/share/packrat/repos.sqlite-shm"
    )
    print(f"  cp {quoted_dest} ~/.local/share/packrat/repos.sqlite")
    print(f"  packrat repo set-path {example_repo} /path/to/local/checkout")
    print("  packrat auto-sync on")


def _exit_code_for_import_cause(cause: BaseException | None) -> int:
    """Maps the cause of an `ImportAbortedError` to a `packrat` exit code.

    Returns `2` for usage or schema errors, `EXIT_LOCKED_RETRY` (`3`) for a
    locked or busy `sqlite3.OperationalError`, and `1` for anything else
    (other SQLite failures, integrity errors, I/O errors, interrupts).
    """
    if isinstance(cause, (ValueError, _lazy("SchemaTooNewError"))):
        return 2
    if isinstance(cause, sqlite3.OperationalError):
        text = str(cause).lower()
        if "locked" in text or "busy" in text:
            return int(_lazy("EXIT_LOCKED_RETRY"))
    return 1


def _format_freshness(key: FreshnessKey) -> str:
    """Formats a `FreshnessKey` as `<backfills>/<watermark>/<git|nogit>`."""
    git = "git" if key.has_git_index else "nogit"
    return f"{key.done_backfills}/{key.sync_watermark or '-'}/{git}"


def _format_import_outcome(outcome: ImportRepoOutcome) -> str:
    """Formats one `ImportRepoOutcome` as a single indented summary line."""
    parts = [
        f"  {outcome.disposition}",
        outcome.full_name,
        f"chunks={outcome.chunks_imported:,}",
        f"embeddings={outcome.embeddings_imported:,}",
        f"src={_format_freshness(outcome.src_freshness)}",
    ]
    if outcome.dest_freshness is not None:
        parts.append(f"dest={_format_freshness(outcome.dest_freshness)}")
    if outcome.preserved_local_git:
        parts.append("kept local git index")
    if outcome.bound_local_path:
        parts.append("bound --path")
    if outcome.error:
        parts.append(f"error: {outcome.error}")
    return "  ".join(parts)


def _print_import_summary(summary: ImportSummary) -> None:
    """Prints human-readable `packrat import` dispositions and row counts."""
    dur = _lazy("format_duration")(summary.duration_sec)
    verb = "Dry run: would import" if summary.dry_run else "Imported"
    print(f"{verb} {summary.source_db_path} into {summary.dest_db_path} ({dur})")
    print(
        f"  Schema version: source={summary.source_schema_version}, "
        f"dest={summary.dest_schema_version}"
    )
    print(
        f"  Repos: added={summary.added_count}, promoted={summary.promoted_count}, "
        f"replaced={summary.replaced_count}, "
        f"skipped_older={summary.skipped_older_count}, "
        f"failed={summary.failed_count}, "
        f"not_attempted={summary.not_attempted_count}"
    )
    print(
        f"  Rows: chunks={summary.chunks_imported:,}, "
        f"embeddings={summary.embeddings_imported:,}"
    )
    for outcome in summary.outcomes:
        print(_format_import_outcome(outcome))


def _emit_import_summary(summary: ImportSummary, *, as_json: bool) -> None:
    """Writes an `ImportSummary` to stdout as JSON or human-readable text."""
    if as_json:
        print(json.dumps(asdict(summary), indent=2))
    else:
        _print_import_summary(summary)


def _run_locked_import(
    args: argparse.Namespace,
    src_path: Path,
    repo_tokens: list[str] | None,
) -> ImportSummary:
    """Opens the destination store under `sync.lock` and runs `import_database`."""
    progress_mode = getattr(args, "progress", "auto")
    reporter = _lazy("StderrProgressReporter")(
        mode=progress_mode,
        quiet=bool(getattr(args, "quiet", False) or getattr(args, "json", False)),
        grace_sec=0.0 if progress_mode == "plain" else 2.0,
    )
    store = _open_store(args.db, refresh_init_sql=True)
    try:
        return store.import_database(
            src_path,
            repo_tokens=repo_tokens,
            local_path=args.path,
            allow_older=bool(args.allow_older),
            reporter=reporter,
        )
    finally:
        reporter.finish_phase()
        store.close()


def _clone_imported_managed_repos(
    args: argparse.Namespace,
    summary: ImportSummary,
) -> int:
    """Clones imported `managed` repos after `_run_locked_import` released `sync.lock`."""
    names = [
        o.full_name
        for o in summary.outcomes
        if o.disposition in ("added", "promoted", "replaced")
    ]
    if not names:
        return 0
    store = _open_store(args.db, refresh_init_sql=False)
    try:
        root = store.git_root
        targets = [
            repo
            for name in names
            if (repo := store.get_repo(name)) is not None
            and repo.git_source == "managed"
        ]
    finally:
        store.close()
    if root is None or not targets:
        return 0
    mode = getattr(args, "progress", "auto")
    quiet = bool(getattr(args, "quiet", False) or getattr(args, "json", False))
    failed = 0
    for repo in targets:
        rc = _clone_repo_outside_tx(args.db, root, repo, mode=mode, quiet=quiet)
        if rc != 0:
            failed += 1
            sys.stderr.write(
                f"  hint: run `packrat sync --repo {repo.full_name}` later to retry cloning.\n"
            )
    return 1 if failed else 0


def cmd_import(args: argparse.Namespace) -> int:
    """Handles `packrat import`.

    `--dry-run` previews dispositions read-only without taking `sync.lock` or
    touching the destination. Live runs validate the source, `--repo`,
    `--path`, and free disk space before waiting on `sync.lock`, so bad
    arguments exit `2` with no lock wait and no destination writes.
    """
    db_path = _resolve_db_path(args.db)
    src_path = Path(args.src).expanduser()
    repo_tokens = _parse_repo_tokens(args.repo) or None
    as_json = bool(getattr(args, "json", False))
    if args.dry_run:
        summary = _lazy("preview_import_database")(
            db_path,
            src_path,
            repo_tokens=repo_tokens,
            local_path=args.path,
            allow_older=bool(args.allow_older),
        )
        _emit_import_summary(summary, as_json=as_json)
        return 0
    _lazy("validate_import_preflight")(
        db_path,
        src_path,
        repo_tokens=repo_tokens,
        local_path=args.path,
        check_disk=True,
    )
    with _lazy("acquire_sync_lock")(db_path, mode="wait") as lock_info:
        if not lock_info.acquired:
            sys.stderr.write(
                "Error: packrat database is busy "
                f"(sync.lock held by pid {lock_info.holder_pid}); retry.\n"
            )
            return int(_lazy("EXIT_LOCKED_RETRY"))
        try:
            summary = _run_locked_import(args, src_path, repo_tokens)
        except _lazy("ImportAbortedError") as exc:
            _emit_import_summary(exc.summary, as_json=as_json)
            sys.stderr.write(f"Error: {exc}\n")
            return _exit_code_for_import_cause(exc.__cause__)
    _emit_import_summary(summary, as_json=as_json)
    if getattr(args, "clone_now", False):
        return _clone_imported_managed_repos(args, summary)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """Handles `packrat export`."""
    store = _open_store(args.db, refresh_init_sql=False)
    try:
        repos = _select_mirror_repos(store, args.repo)
        if not repos:
            raise ValueError(NO_REPOS_MESSAGE.removeprefix("Error: ").strip())
        progress_mode = getattr(args, "progress", "auto")
        footprint = _approx_sqlite_footprint(store.db_path)
        grace_sec = (
            0.0
            if progress_mode == "plain" or footprint >= _EXPORT_IMMEDIATE_PROGRESS_BYTES
            else 2.0
        )
        reporter = _lazy("StderrProgressReporter")(
            mode=progress_mode,
            quiet=bool(getattr(args, "quiet", False) or getattr(args, "json", False)),
            grace_sec=grace_sec,
        )
        repo_ids = [r.repo_id for r in repos] if _parse_repo_tokens(args.repo) else None
        try:
            summary = store.export_database(
                Path(args.dest).expanduser(),
                repo_ids=repo_ids,
                force=bool(getattr(args, "force", False)),
                reporter=reporter,
            )
        finally:
            reporter.finish_phase()
        if args.json:
            print(json.dumps(asdict(summary), indent=2))
            return 0
        _print_export_summary(summary)
        return 0
    finally:
        store.close()


def cmd_live(args: argparse.Namespace) -> int:
    """Dispatches `serve`, `watch`, `changes`, and `refresh` (see serve_cli.py).

    Store commands open the database without rewriting init.sql, since readers
    call `changes` and `watch renew` often.
    """
    db_path = _resolve_db_path(args.db)
    if args.command_kind == "serve":
        return _lazy("cmd_serve")(args, db_path)
    store = _lazy("PackratStore")(db_path)
    try:
        return int(args.store_func(args, store))
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    """Constructs the argparse CLI parser for packrat."""
    parser = argparse.ArgumentParser(
        prog="packrat",
        description="Local multi-repo SQLite WAL + DuckDB + offline 384-dim vector mirror.",
    )
    parser.add_argument("--db", help="Override SQLite database path.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser(
        "status", help="Show repository watermarks and table counts."
    )
    p_status.add_argument(
        "--repo", help="Filter by repository (<owner>/<name> or just <name>)."
    )
    p_status.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_status.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Also count rows of items-mode repositories (slower on large databases).",
    )
    p_status.set_defaults(func=cmd_status)

    p_sync = sub.add_parser(
        "sync", help="Run incremental git diff-tree, GitHub, and vector sync."
    )
    p_sync.add_argument(
        "--repo",
        action="append",
        help="Sync only a specific repository (repeatable or comma-separated).",
    )
    p_sync.add_argument(
        "--force", action="store_true", help="Force full re-index and bypass ETag."
    )
    sync_scope = p_sync.add_mutually_exclusive_group()
    sync_scope.add_argument(
        "--git-only", action="store_true", help="Sync local git commits/code only."
    )
    sync_scope.add_argument(
        "--github-only", action="store_true", help="Sync GitHub metadata only."
    )
    sync_scope.add_argument(
        "--backfill-only",
        action="store_true",
        help="Run only GitHub history backfill walks (no events poll or incremental delta, "
        "which packrat serve owns), plus the git and embedding steps.",
    )
    fetch_mode = p_sync.add_mutually_exclusive_group()
    fetch_mode.add_argument(
        "--fetch", action="store_true", help="Run git fetch on path checkouts first."
    )
    fetch_mode.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip pre-sync remote checks and fetches for managed repositories.",
    )
    p_sync.add_argument(
        "--auto",
        action="store_true",
        help="Background timer mode: skip cleanly on lock/empty DB and degrade to local git on locked keyring.",
    )
    p_sync.add_argument(
        "--lock-mode",
        choices=("wait", "skip", "fail"),
        default=None,
        help="Single-flight lock contention behavior (default: skip with --auto, wait otherwise).",
    )
    p_sync.add_argument(
        "--max-pages",
        type=int,
        default=10,
        help="Max GraphQL pages per stream for incremental updates.",
    )
    p_sync.add_argument(
        "--backfill-max-pages",
        type=int,
        default=_DEFAULT_BACKFILL_MAX_PAGES,
        help="Safety cap on GraphQL pages per stream while backfilling history; "
        "an unfinished backfill resumes on the next sync.",
    )
    p_sync.add_argument(
        "--max-commits", type=int, default=300, help="Max git commits to ingest."
    )
    p_sync.add_argument(
        "--max-reparse-files",
        type=int,
        default=None,
        help="Max files to re-parse per run (default: 1500 under --auto; unlimited otherwise).",
    )
    p_sync.add_argument(
        "--max-embed-chunks",
        type=int,
        default=None,
        help="Max chunks to embed per run (default: 15000 under --auto; unlimited otherwise).",
    )
    p_sync.add_argument(
        "--no-timeline",
        action="store_true",
        help="Skip the PR and issue timeline pass (for rollback and debugging).",
    )
    model_dl = p_sync.add_mutually_exclusive_group()
    model_dl.add_argument(
        "--download-model",
        action="store_true",
        help="Download Snowflake Arctic XS ONNX model if missing (enabled by default on default DB).",
    )
    model_dl.add_argument(
        "--no-download-model",
        action="store_true",
        help="Skip downloading the ONNX embedding model if missing.",
    )
    p_sync.add_argument(
        "--progress",
        choices=("auto", "plain", "off"),
        default="auto",
        help="Progress display mode on stderr (default: auto).",
    )
    p_sync.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress and warnings on stderr (errors are still printed).",
    )
    p_sync.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_sync.set_defaults(func=cmd_sync)

    p_search = sub.add_parser(
        "search", help="Run hybrid RRF (FTS5 BM25 + 384-dim cosine) search."
    )
    p_search.add_argument("query", help="Natural language or code search query.")
    p_search.add_argument("--repo", help="Filter by repository.")
    p_search.add_argument(
        "--kind",
        help="Comma-separated chunk kinds (issue,pr,review_thread,comment,discussion,commit,code_symbol,doc_section).",
    )
    p_search.add_argument("--path", help="Filter by file_path prefix.")
    p_search.add_argument(
        "--state", help="Filter issues/PRs by state (OPEN, CLOSED, MERGED)."
    )
    p_search.add_argument(
        "--mode",
        choices=("hybrid", "fts", "vector"),
        default="hybrid",
        help="Retrieval mode.",
    )
    p_search.add_argument(
        "--limit", type=int, default=12, help="Max results to return."
    )
    p_search.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_search.set_defaults(func=cmd_search)

    p_sql = sub.add_parser(
        "sql", help="Run read-only DuckDB v2.0 SQL over kb.* tables and v_* views."
    )
    p_sql.add_argument("query", help="SQL query string.")
    p_sql.add_argument(
        "--format",
        choices=("box", "json", "markdown", "csv"),
        default="box",
        help="DuckDB output format.",
    )
    p_sql.add_argument(
        "--raw",
        action="store_true",
        help="Disable <untrusted_repo_content> wrapping of prose columns.",
    )
    p_sql.set_defaults(func=cmd_sql)

    p_repo = sub.add_parser("repo", help="Manage tracked repositories.")
    repo_sub = p_repo.add_subparsers(dest="repo_action", required=True)
    p_repo_list = repo_sub.add_parser("list", help="List tracked repositories.")
    p_repo_list.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_repo_list.add_argument(
        "--mode",
        choices=("mirror", "items"),
        default=None,
        help="Only list repositories in this mode.",
    )
    p_repo_list.set_defaults(func=cmd_repo)

    p_repo_add = repo_sub.add_parser("add", help="Register or update a repository.")
    p_repo_add.add_argument("full_name", help="Repository in <owner>/<name> format.")
    add_src = p_repo_add.add_mutually_exclusive_group()
    add_src.add_argument(
        "--path", help="Local git checkout path (uses git_source=path)."
    )
    add_src.add_argument(
        "--github-only",
        action="store_true",
        help="Register without git clone or indexing (git_source=none).",
    )
    p_repo_add.add_argument(
        "--url", help="Override clone URL for this workstation (managed mode)."
    )
    p_repo_add.add_argument(
        "--keep-git",
        action="store_true",
        help="Keep any existing managed bare clone directory when switching to path or none.",
    )
    p_repo_add.add_argument(
        "--branch", default=None, help="Canonical default branch name (default: main)."
    )
    p_repo_add.set_defaults(func=cmd_repo)

    p_repo_set_git = repo_sub.add_parser(
        "set-git",
        help="Switch a repository between managed, path, and none git sources.",
    )
    p_repo_set_git.add_argument(
        "full_name", help="Repository in <owner>/<name> format."
    )
    p_repo_set_git.add_argument(
        "git_mode", choices=("managed", "path", "none"), help="Target git source mode."
    )
    p_repo_set_git.add_argument(
        "path", nargs="?", help="Local git checkout path (required when mode is path)."
    )
    p_repo_set_git.add_argument(
        "--url", help="Override clone URL for this workstation (managed mode)."
    )
    p_repo_set_git.add_argument(
        "--clone-now",
        action="store_true",
        help="Clone immediately when switching to managed.",
    )
    p_repo_set_git.add_argument(
        "--keep-git",
        action="store_true",
        help="Keep any existing managed bare clone directory when switching to path or none.",
    )
    p_repo_set_git.add_argument(
        "--branch", default=None, help="Canonical default branch name."
    )
    p_repo_set_git.set_defaults(func=cmd_repo)

    p_repo_set = repo_sub.add_parser(
        "set-path", help="Configure the local git worktree path for a repository."
    )
    p_repo_set.add_argument("full_name", help="Repository in <owner>/<name> format.")
    p_repo_set.add_argument("path", help="Local git checkout or worktree path.")
    p_repo_set.add_argument(
        "--keep-git",
        action="store_true",
        help="Keep any existing managed bare clone directory.",
    )
    p_repo_set.add_argument(
        "--branch", default=None, help="Canonical default branch name."
    )
    p_repo_set.set_defaults(func=cmd_repo)

    p_repo_rename = repo_sub.add_parser(
        "rename",
        help="Rename a tracked repository in place after a GitHub transfer or rename, keeping synced data.",
    )
    p_repo_rename.add_argument(
        "full_name", help="Current repository name in <owner>/<name> format."
    )
    p_repo_rename.add_argument(
        "new_full_name", help="New repository name in <owner>/<name> format."
    )
    p_repo_rename.set_defaults(func=cmd_repo)

    p_repo_remove = repo_sub.add_parser(
        "remove", help="Stop tracking a repository and delete its synced data."
    )
    p_repo_remove.add_argument("full_name", help="Repository in <owner>/<name> format.")
    p_repo_remove.set_defaults(func=cmd_repo)

    p_install = sub.add_parser(
        "install-cli", help="Symlink packrat into ~/.local/bin (or --bin-dir)."
    )
    p_install.add_argument(
        "--bin-dir",
        default=str(Path.home() / ".local" / "bin"),
        help="Target directory on PATH for the packrat symlink (default: ~/.local/bin).",
    )
    p_install.set_defaults(func=cmd_install_cli)

    p_auto = sub.add_parser(
        "auto-sync",
        aliases=["timer"],
        help="Enable, disable, or inspect background sync (packrat.service, or the timer).",
    )
    p_auto.add_argument(
        "action",
        nargs="?",
        choices=("on", "off", "enable", "disable", "status"),
        default="status",
        help="Timer action: on/enable, off/disable, or status (default: status).",
    )
    p_auto.add_argument(
        "--timer",
        action="store_true",
        help="With `on`: install the periodic packrat-sync.timer instead of packrat.service "
        "(rollback path); stops the service.",
    )
    p_auto.add_argument(
        "--interval-min",
        type=int,
        default=None,
        help="Timer mode only: sync interval in minutes (preserves existing or defaults to 10).",
    )
    fetch_group = p_auto.add_mutually_exclusive_group()
    fetch_group.add_argument(
        "--fetch",
        dest="fetch",
        action="store_true",
        default=None,
        help="Pass --fetch to background sync runs.",
    )
    fetch_group.add_argument(
        "--no-fetch",
        dest="fetch",
        action="store_false",
        help="Do not pass --fetch to background sync runs.",
    )
    p_auto.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_auto.set_defaults(func=cmd_auto_sync)

    p_export = sub.add_parser(
        "export",
        help="Export a scrubbed, vacuumed SQLite database snapshot for sharing.",
    )
    p_export.add_argument(
        "dest",
        help="Destination filesystem path for the exported SQLite database.",
    )
    p_export.add_argument(
        "--repo",
        action="append",
        help="Export only specific mirror repository (<owner>/<name> or <name>; repeatable or comma-separated).",
    )
    p_export.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Overwrite destination file if it already exists.",
    )
    p_export.add_argument(
        "--progress",
        choices=("auto", "plain", "off"),
        default="auto",
        help="Progress display mode on stderr (default: auto).",
    )
    p_export.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output on stderr.",
    )
    p_export.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_export.set_defaults(func=cmd_export)

    p_import = sub.add_parser(
        "import",
        help="Merge mirror repositories from another packrat SQLite file into this one.",
    )
    p_import.add_argument(
        "src",
        help="Source packrat SQLite file (for example, a `packrat export` snapshot).",
    )
    p_import.add_argument(
        "--repo",
        action="append",
        help="Import only specific source mirror repository (<owner>/<name> or <name>; repeatable or comma-separated).",
    )
    p_import.add_argument(
        "--path",
        help="Bind a local git checkout to the single imported repository.",
    )
    p_import.add_argument(
        "--allow-older",
        action="store_true",
        help="Replace a destination repository even when the source is not newer, including its local git index.",
    )
    p_import.add_argument(
        "--clone-now",
        action="store_true",
        help="Clone imported managed repositories after commit and lock release.",
    )
    p_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Report dispositions without locking or writing the destination.",
    )
    p_import.add_argument(
        "--progress",
        choices=("auto", "plain", "off"),
        default="auto",
        help="Progress display mode on stderr (default: auto).",
    )
    p_import.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output on stderr.",
    )
    p_import.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_import.set_defaults(func=cmd_import)

    p_git = sub.add_parser(
        "git", help="Inspect and maintain packrat-managed bare clones."
    )
    git_sub = p_git.add_subparsers(dest="git_action", required=True)
    p_git_status = git_sub.add_parser("status", help="Show managed clone status.")
    p_git_status.add_argument("--repo", action="append", help="Filter repository.")
    p_git_status.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_git_status.set_defaults(func=cmd_git)

    p_git_fetch = git_sub.add_parser(
        "fetch", help="Clone or fetch managed repositories without indexing."
    )
    p_git_fetch.add_argument("--repo", action="append", help="Filter repository.")
    p_git_fetch.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_git_fetch.set_defaults(func=cmd_git)

    p_git_gc = git_sub.add_parser("gc", help="Run git maintenance on managed clones.")
    p_git_gc.add_argument("--repo", action="append", help="Filter repository.")
    p_git_gc.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_git_gc.set_defaults(func=cmd_git)

    p_git_prune = git_sub.add_parser(
        "prune", help="Remove orphaned clone directories under git/."
    )
    p_git_prune.add_argument(
        "--dry-run", action="store_true", help="List directories without deleting."
    )
    p_git_prune.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_git_prune.set_defaults(func=cmd_git)

    _lazy("add_parsers_parser")(sub)
    _lazy("add_live_parsers")(sub, cmd_live)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Main CLI dispatcher.

    Exit codes: `0` success, `1` command failure, `2` usage or data error,
    `3` (`EXIT_LOCKED_RETRY`) SQLite busy or locked; callers may retry.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ValueError, _lazy("SchemaTooNewError")) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    except sqlite3.OperationalError as exc:
        text = str(exc)
        if "locked" in text.lower() or "busy" in text.lower():
            sys.stderr.write(f"Error: packrat database is busy ({text}); retry.\n")
            return int(_lazy("EXIT_LOCKED_RETRY"))
        sys.stderr.write(f"Error: sqlite failure ({text}).\n")
        return 1
    except (
        _lazy("ExportIntegrityError"),
        _lazy("ImportIntegrityError"),
        sqlite3.DatabaseError,
        OSError,
    ) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Error: interrupted.\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
