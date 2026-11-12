#!/usr/bin/env python3
"""CLI entrypoint for packrat: multi-repository SQLite WAL + DuckDB + vector mirror."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
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
    from models import ExportSummary, RepoConfig, SyncStats
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
    "PackratStore": ("db_store", "PackratStore"),
    "ensure_onnx_model": ("embedder", "ensure_onnx_model"),
    "get_default_embedder": ("embedder", "get_default_embedder"),
    "get_default_embedder_model_id": ("embedder", "get_default_embedder_model_id"),
    "sync_repo_embeddings": ("embedder", "sync_repo_embeddings"),
    "run_gh_command": ("gh_transport", "run_gh_command"),
    "sync_git_codebase": ("git_indexer", "sync_git_codebase"),
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
    "service_status": ("serve_cli", "service_status"),
    "AutoSyncRunRecord": ("sync_runtime", "AutoSyncRunRecord"),
    "LockMode": ("sync_runtime", "LockMode"),
    "SyncLockInfo": ("sync_runtime", "SyncLockInfo"),
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
    "  packrat repo add <owner>/<name> --path /path/to/local/checkout\n"
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
                    f"run 'packrat repo add {repo.full_name}' to mirror it."
                )
        return selected
    return [r for r in selected if r.is_mirror]


def _merge_sync_stats(
    repo_name: str,
    git_stats: SyncStats | None,
    gh_stats: SyncStats | None,
    embedded_count: int,
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
    combined.embeddings_updated = embedded_count
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


def _print_status_text(payload: dict[str, Any], auto_sync: AutoSyncStatus) -> None:
    """Prints human-readable `packrat status` output to stdout."""
    print(f"Database: {payload['db_path']}")
    print(f"DuckDB init.sql: {payload['init_sql_path']}")
    print(f"Embedder: {payload['active_embedder']}")
    print(_format_auto_sync_mode(auto_sync))
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
            "rate_budget": _rate_budget_payload(_rate_budget_for(store.db_path)),
            "service": _lazy("service_status")(store),
            "repos": [],
        }
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
    items never fetched before (`TimelineMode.BACKFILL`). Otherwise the full
    delta sync runs, then every stale timeline (`TimelineMode.ALL`) and the
    cross-reference queue. The timeline pass is skipped with `--no-timeline`
    and after a GitHub sync that stopped for a locked keyring or a rate limit.
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
    timeline = _lazy("sync_repo_timelines")(
        store,
        store.get_repo(repo.full_name) or repo,
        mode=t_mode.BACKFILL if backfill_only else t_mode.ALL,
        runner=_lazy("run_gh_command"),
        budget=budget,
        max_batches=SYNC_TIMELINE_MAX_BATCHES,
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
) -> dict[str, Any]:
    """Executes git, GitHub, and embedding sync for a single repository.

    Args:
        budget: Shared GitHub rate budget passed to `sync_github_metadata`.
    """
    active_reporter = reporter if reporter is not None else _lazy("NULL_REPORTER")
    started = time.monotonic()
    git_stats: SyncStats | None = None
    gh_stats: SyncStats | None = None

    if not args.github_only:
        git_stats = _lazy("sync_git_codebase")(
            store,
            repo,
            force=args.force,
            fetch=args.fetch,
            max_commits=args.max_commits,
            reporter=active_reporter,
        )
    if not args.git_only:
        if gh_probe is not None and not gh_probe.ok:
            gh_stats = _skipped_gh_stats_from_probe(repo.full_name, gh_probe)
        else:
            fresh_repo = store.get_repo(repo.full_name) or repo
            gh_stats = _run_github_sync(
                store, fresh_repo, args, active_reporter, budget
            )
    embedded_count = _lazy("sync_repo_embeddings")(
        store,
        repo.repo_id,
        embedder=embedder,
        allow_download=allow_download,
        reporter=active_reporter,
        repo_label=repo.full_name,
    )
    elapsed = max(0.0, time.monotonic() - started)
    return _merge_sync_stats(
        repo.full_name, git_stats, gh_stats, embedded_count, duration_sec=elapsed
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
        if not repo.local_path:
            if not git_only:
                continue
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
        results: list[dict[str, Any]] = []
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
            )
            results.append(res)
            gh_probe = _propagate_keyring_lock(gh_probe, res)
            if not args.json:
                _print_repo_sync_summary(res, reporter)
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
        sys.stderr.write(
            f"Error: Repository '{args.full_name}' is not registered in packrat.\n"
        )
        return 2
    print(f"Removed {args.full_name} and all of its synced data.")
    return 0


def cmd_repo(args: argparse.Namespace) -> int:
    """Handles `packrat repo {list,add,set-path,rename,remove}`."""
    mutating = args.repo_action in ("add", "set-path", "rename", "remove")
    store = _open_store(args.db, refresh_init_sql=mutating)
    try:
        if args.repo_action in ("add", "set-path"):
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
            print(
                f"{r.full_name}\tbranch={r.default_branch}\tpath={r.local_path or '-'}{mode_note}"
            )
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
    print("\nRecipient install commands:")
    print("  packrat auto-sync off")
    print(
        "  rm -f ~/.local/share/packrat/repos.sqlite-wal "
        "~/.local/share/packrat/repos.sqlite-shm"
    )
    print(
        f"  cp {shlex.quote(summary.dest_db_path)} ~/.local/share/packrat/repos.sqlite"
    )
    print(f"  packrat repo set-path {example_repo} /path/to/local/checkout")
    print("  packrat auto-sync on")


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
    p_sync.add_argument(
        "--fetch", action="store_true", help="Run git fetch on canonical remote first."
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
    p_repo_add.add_argument("--path", help="Local git checkout path.")
    p_repo_add.add_argument(
        "--branch", default="main", help="Canonical default branch name."
    )
    p_repo_add.set_defaults(func=cmd_repo)

    p_repo_set = repo_sub.add_parser(
        "set-path", help="Configure the local git worktree path for a repository."
    )
    p_repo_set.add_argument("full_name", help="Repository in <owner>/<name> format.")
    p_repo_set.add_argument("path", help="Local git checkout or worktree path.")
    p_repo_set.add_argument(
        "--branch", default="main", help="Canonical default branch name."
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
    except (_lazy("ExportIntegrityError"), sqlite3.DatabaseError, OSError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("Error: interrupted.\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
