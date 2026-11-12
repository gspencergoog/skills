"""Selective per-repository merge of one packrat database into another (`packrat import`).

The engine attaches a source `repos.sqlite` read-only and copies each selected
`mirror` repository into the destination inside its own `BEGIN IMMEDIATE`
transaction. It keeps every destination row that the source does not own:
other repositories, `repos.local_path`, leases, viewer sets, notifications, and
the rest of `PERSONAL_TABLES`.

Transaction rules inside `_copy_repo_tx`: only `conn.execute` and the pure
`_in_tx` store helpers run between `BEGIN IMMEDIATE` and `COMMIT`. Nothing in
that window may call a `with self.conn:` store method or `executescript()`,
because either one commits the open transaction early and skips the pre-commit
foreign-key and FTS5 checks.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

try:
    from .db_migrations import (
        IMPORT_ROW_FIXUPS,
        LATEST_SCHEMA_VERSION,
        MIN_IMPORT_SCHEMA_VERSION,
        SchemaTooNewError,
        read_schema_version,
    )
    from .db_schema import (
        ALL_BACKFILL_COLUMNS,
        GIT_SOURCE_KINDS,
        REPO_GIT_TABLES,
        REPO_GITHUB_TABLES,
        REPO_STATE_KEY_PATTERNS,
    )
    from .models import (
        BACKFILL_STREAMS,
        EMBEDDING_DIM,
        GIT_BACKFILL_COLUMNS,
        REPO_MODE_ITEMS,
        REPO_MODE_MIRROR,
        FreshnessKey,
        ImportDisposition,
        ImportRepoOutcome,
        ImportSummary,
        RepoConfig,
        iso_at,
        normalize_utc_iso,
    )
    from .progress import ProgressReporter, SqlitePhaseProgress, format_bytes
except ImportError:
    from db_migrations import (  # type: ignore[no-redef]
        IMPORT_ROW_FIXUPS,
        LATEST_SCHEMA_VERSION,
        MIN_IMPORT_SCHEMA_VERSION,
        SchemaTooNewError,
        read_schema_version,
    )
    from db_schema import (  # type: ignore[no-redef]
        ALL_BACKFILL_COLUMNS,
        GIT_SOURCE_KINDS,
        REPO_GIT_TABLES,
        REPO_GITHUB_TABLES,
        REPO_STATE_KEY_PATTERNS,
    )
    from models import (  # type: ignore[no-redef]
        BACKFILL_STREAMS,
        EMBEDDING_DIM,
        GIT_BACKFILL_COLUMNS,
        REPO_MODE_ITEMS,
        REPO_MODE_MIRROR,
        FreshnessKey,
        ImportDisposition,
        ImportRepoOutcome,
        ImportSummary,
        RepoConfig,
        iso_at,
        normalize_utc_iso,
    )
    from progress import (  # type: ignore[no-redef]
        ProgressReporter,
        SqlitePhaseProgress,
        format_bytes,
    )

if TYPE_CHECKING:
    from db_store import PackratStore

__all__ = [
    "ImportAbortedError",
    "ImportIntegrityError",
    "import_database",
    "is_sqlite_path_alias",
    "mirror_freshness_key",
    "preview_import_database",
    "validate_import_preflight",
]

SRC_SCHEMA = "src_db"
"""Schema name the source database is attached under."""

IMPORT_SEC_PER_GIB = 37.5
"""Measured per-repository copy rate (indexes, FTS5, and pre-commit checks included)."""

LONG_COPY_WARN_SEC = 150.0
"""Estimated single-repository copy time above which a reader-visible lock warning is printed."""

MIN_SELECTED_SHARE = 0.05
"""Floor on the share of the source file attributed to the selected repositories."""

_SQLITE_SIDECARS = ("-wal", "-shm", "-journal")
_PREFIX_SEPARATORS = ("#", "@", ":")
_CHANGE_FIELDS: tuple[str, ...] = ("pr", "checks", "reviews", "threads")
_REPO_UPDATE_EXCLUDED = frozenset(
    {"repo_id", "owner", "name", "full_name", "local_path", "mode", "created_at"}
)
_REPO_INSERT_EXCLUDED = frozenset({"repo_id", "local_path", "mode"})
_GIT_REPO_COLUMNS = frozenset({"last_indexed_sha", *GIT_BACKFILL_COLUMNS})
_RESETTABLE_SYNC_COLUMNS = frozenset(
    {
        *ALL_BACKFILL_COLUMNS,
        *(f"{stream}_watermark" for stream in BACKFILL_STREAMS),
        "last_full_sweep_at",
        "last_indexed_sha",
        "last_events_check_at",
        "events_etag",
        "events_last_modified",
    }
)
"""`repos` sync-state columns reset to NULL on replacement when the source schema lacks them."""


class ImportIntegrityError(sqlite3.DatabaseError):
    """Raised when a pre-commit foreign-key or FTS5 check fails inside an import transaction."""


class ImportAbortedError(RuntimeError):
    """Raised when a batch stops early; `summary` lists committed and remaining outcomes.

    Always raised `from` the original exception so callers can map the cause
    (`sqlite3.OperationalError` lock timeouts, `KeyboardInterrupt`, and so on)
    to the right exit code.
    """

    def __init__(self, message: str, summary: ImportSummary) -> None:
        super().__init__(message)
        self.summary = summary


@dataclass(frozen=True)
class _SourceRepo:
    """One selected source repository plus its alias names and chunk count."""

    row: dict[str, Any]
    aliases: tuple[str, ...]
    chunk_count: int

    @property
    def repo_id(self) -> int:
        return int(self.row["repo_id"])

    @property
    def full_name(self) -> str:
        return str(self.row["full_name"])

    @property
    def match_names(self) -> tuple[str, ...]:
        """Names that identify this repository in the destination (slug first)."""
        return (self.full_name, *self.aliases)


@dataclass(frozen=True)
class _Preflight:
    """Validated inputs shared by previews and live imports."""

    dest_path: Path
    source_path: Path
    source_schema_version: int
    selected: tuple[_SourceRepo, ...]
    local_path: str | None
    source_bytes: int
    total_chunks: int


@dataclass
class _ImportContext:
    """Mutable state for one live import run."""

    store: PackratStore
    conn: sqlite3.Connection
    pre: _Preflight
    allow_older: bool
    now: float | None
    tracker: SqlitePhaseProgress
    reporter: ProgressReporter | None
    outcomes: list[ImportRepoOutcome] = field(default_factory=list)


# ----- path and connection helpers ------------------------------------------------


def is_sqlite_path_alias(target_db_path: Path | str, candidate_path: Path | str) -> bool:
    """Returns whether `candidate_path` names `target_db_path` or one of its sidecars."""
    target = str(target_db_path)
    if target == ":memory:":
        return False
    target_resolved = Path(target).expanduser().resolve()
    forbidden = (
        target_resolved,
        *(Path(f"{target_resolved}{suffix}") for suffix in _SQLITE_SIDECARS),
    )
    raw = Path(candidate_path).expanduser()
    if raw.resolve() in forbidden:
        return True
    if not raw.exists():
        return False
    return any(p.exists() and os.path.samefile(raw, p) for p in forbidden)


def _ro_uri(path: Path, *, immutable: bool = False) -> str:
    """Builds a read-only `file:` URI for `path`."""
    uri = f"file:{quote(str(path))}?mode=ro"
    return f"{uri}&immutable=1" if immutable else uri


def _open_ro_sqlite(path: Path) -> sqlite3.Connection:
    """Opens `path` read-only, falling back to `immutable=1` only when no WAL sidecar exists."""
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=5.0)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        if conn is not None:
            conn.close()
        if Path(f"{path}-wal").exists():
            raise
        conn = sqlite3.connect(_ro_uri(path, immutable=True), uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _resolve_source_path(dest_path: Path, source_path: Path | str) -> Path:
    """Validates that `source_path` is a regular SQLite file distinct from the destination."""
    raw = str(source_path).strip()
    if not raw or raw == ":memory:" or raw.startswith("file:"):
        raise ValueError("Import source must be a regular filesystem path.")
    candidate = Path(raw).expanduser()
    if not candidate.is_file():
        raise ValueError(f"Import source '{candidate}' is not an existing file.")
    if is_sqlite_path_alias(dest_path, candidate):
        raise ValueError("Import source cannot be the destination database itself.")
    return candidate.resolve()


def _existing_dir(path: Path) -> Path:
    """Returns the nearest existing ancestor directory of `path` (for disk-space checks)."""
    current = path.expanduser().resolve().parent
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


# ----- freshness ---------------------------------------------------------------------


def _field(repo: Mapping[str, Any] | sqlite3.Row | RepoConfig, name: str) -> Any:
    """Reads `name` from a dict, `sqlite3.Row`, or `RepoConfig`, returning None when absent."""
    if isinstance(repo, sqlite3.Row):
        return repo[name] if name in repo.keys() else None
    if isinstance(repo, Mapping):
        return repo.get(name)
    return getattr(repo, name, None)


def mirror_freshness_key(repo: Mapping[str, Any] | sqlite3.Row | RepoConfig) -> FreshnessKey:
    """Computes the `FreshnessKey` that decides whether a source snapshot replaces a mirror."""
    done = sum(
        1 for stream in BACKFILL_STREAMS if _field(repo, f"{stream}_backfill_done_at")
    )
    watermarks = [
        normalize_utc_iso(_field(repo, f"{stream}_watermark"))
        for stream in BACKFILL_STREAMS
    ]
    newest = max((w for w in watermarks if w), default="")
    if not newest:
        newest = normalize_utc_iso(_field(repo, "last_full_sweep_at"))
    return FreshnessKey(
        done_backfills=done,
        sync_watermark=newest,
        has_git_index=1 if _field(repo, "last_indexed_sha") else 0,
    )


def _evaluate(
    src: _SourceRepo,
    dest_row: sqlite3.Row | None,
    allow_older: bool,
) -> tuple[ImportDisposition, FreshnessKey | None, bool]:
    """Returns `(disposition, dest_freshness, preserve_local_git)` for one repository."""
    src_has_git = bool(src.row.get("last_indexed_sha"))
    if dest_row is None:
        return "added", None, False
    dest_has_git = bool(dest_row["last_indexed_sha"])
    preserve = not src_has_git and dest_has_git and not allow_older
    dest_key = mirror_freshness_key(dest_row)
    if str(dest_row["mode"] or REPO_MODE_MIRROR) == REPO_MODE_ITEMS:
        return "promoted", dest_key, preserve
    if allow_older or mirror_freshness_key(src.row) > dest_key:
        return "replaced", dest_key, preserve
    return "skipped_older", dest_key, False


# ----- source and destination lookups ------------------------------------------------


def _load_source_repos(src_conn: sqlite3.Connection, schema: str = "main") -> list[_SourceRepo]:
    """Loads every `mirror` repository of the source with its aliases and chunk count."""
    aliases: dict[int, list[str]] = {}
    for row in src_conn.execute(
        f"SELECT alias_full_name, repo_id FROM {schema}.repo_aliases"
    ):
        aliases.setdefault(int(row[1]), []).append(str(row[0]))
    counts = {
        int(r[0]): int(r[1])
        for r in src_conn.execute(
            f"SELECT repo_id, COUNT(*) FROM {schema}.chunks GROUP BY repo_id"
        )
    }
    repos: list[_SourceRepo] = []
    for row in src_conn.execute(
        f"SELECT * FROM {schema}.repos WHERE mode = ? ORDER BY repo_id",
        (REPO_MODE_MIRROR,),
    ):
        data = {k: row[k] for k in row.keys()}
        rid = int(data["repo_id"])
        repos.append(_SourceRepo(data, tuple(aliases.get(rid, ())), counts.get(rid, 0)))
    return repos


def _source_mode_lookup(src_conn: sqlite3.Connection, schema: str) -> dict[str, str]:
    """Maps lowercase slugs (and aliases) of every source repository to its mode."""
    modes: dict[str, str] = {}
    for row in src_conn.execute(f"SELECT full_name, mode FROM {schema}.repos"):
        modes[str(row[0]).lower()] = str(row[1] or REPO_MODE_MIRROR)
    for row in src_conn.execute(
        f"SELECT a.alias_full_name, r.mode FROM {schema}.repo_aliases a "
        f"JOIN {schema}.repos r ON r.repo_id = a.repo_id"
    ):
        modes.setdefault(str(row[0]).lower(), str(row[1] or REPO_MODE_MIRROR))
    return modes


def _resolve_token(
    token: str, mirrors: Sequence[_SourceRepo], modes: Mapping[str, str]
) -> _SourceRepo:
    """Resolves one `--repo` token against source `mirror` repositories.

    Raises:
        ValueError: If the token is unknown, names an `items` repository, or is
            ambiguous as a short name.
    """
    needle = token.strip().lower()
    for repo in mirrors:
        if any(name.lower() == needle for name in repo.match_names):
            return repo
    if modes.get(needle) == REPO_MODE_ITEMS:
        raise ValueError(
            f"Source repository '{token}' is in items mode and has no mirror data to import."
        )
    short = [r for r in mirrors if str(r.row["name"]).lower() == needle]
    if len(short) == 1:
        return short[0]
    if len(short) > 1:
        names = ", ".join(r.full_name for r in short)
        raise ValueError(
            f"Repository name '{token}' is ambiguous in the source ({names}); pass <owner>/<name>."
        )
    raise ValueError(f"Repository '{token}' is not a mirror repository in the source database.")


def _select_source_repos(
    src_conn: sqlite3.Connection,
    repo_tokens: Sequence[str] | None,
    schema: str = "main",
) -> list[_SourceRepo]:
    """Returns the source `mirror` repositories selected by `repo_tokens` (all when None)."""
    mirrors = _load_source_repos(src_conn, schema)
    tokens = [t.strip() for t in (repo_tokens or ()) if t and t.strip()]
    if not tokens:
        if not mirrors:
            raise ValueError("Source database has no mirror repositories to import.")
        return mirrors
    modes = _source_mode_lookup(src_conn, schema)
    selected: dict[int, _SourceRepo] = {}
    for token in tokens:
        repo = _resolve_token(token, mirrors, modes)
        selected.setdefault(repo.repo_id, repo)
    return list(selected.values())


def _find_dest_repo_row(conn: sqlite3.Connection, full_name: str) -> sqlite3.Row | None:
    """Returns the destination `repos` row for `full_name` (alias first, then slug), if any."""
    row = conn.execute(
        """
        SELECT r.* FROM main.repo_aliases a
        JOIN main.repos r ON r.repo_id = a.repo_id
        WHERE lower(a.alias_full_name) = lower(?)
        """,
        (full_name,),
    ).fetchone()
    if row is not None:
        return row
    return conn.execute(
        "SELECT * FROM main.repos WHERE lower(full_name) = lower(?)", (full_name,)
    ).fetchone()


def _match_dest_repo(conn: sqlite3.Connection, src: _SourceRepo) -> sqlite3.Row | None:
    """Finds the one destination repository that `src` (slug or any alias) maps to.

    Raises:
        ValueError: If the names map to two different destination repositories.
    """
    matches: dict[int, sqlite3.Row] = {}
    for name in src.match_names:
        row = _find_dest_repo_row(conn, name)
        if row is not None:
            matches.setdefault(int(row["repo_id"]), row)
    if len(matches) > 1:
        slugs = ", ".join(str(r["full_name"]) for r in matches.values())
        raise ValueError(
            f"Source repository '{src.full_name}' matches several destination repositories "
            f"({slugs}); run 'packrat repo remove' or 'packrat repo rename' first."
        )
    return next(iter(matches.values()), None)


# ----- preflight -----------------------------------------------------------------------


def _check_source_schema(src_conn: sqlite3.Connection, schema: str = "main") -> int:
    """Returns the source schema version, rejecting versions outside the supported range."""
    version = read_schema_version(src_conn) if schema == "main" else _attached_schema_version(src_conn, schema)
    if version > LATEST_SCHEMA_VERSION:
        raise SchemaTooNewError(
            f"Import source schema version {version} is newer than this packrat supports "
            f"({LATEST_SCHEMA_VERSION}); update packrat (git pull) first."
        )
    if version < MIN_IMPORT_SCHEMA_VERSION:
        raise ValueError(
            f"Import source schema version {version} is older than the minimum supported "
            f"({MIN_IMPORT_SCHEMA_VERSION}); open it with a newer packrat and re-export it."
        )
    return version


def _attached_schema_version(conn: sqlite3.Connection, schema: str) -> int:
    """Reads `schema_version` from an attached database (0 when the table is missing)."""
    exists = conn.execute(
        f"SELECT 1 FROM {schema}.sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if exists is None:
        return 0
    row = conn.execute(f"SELECT MAX(version) FROM {schema}.schema_version").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _check_local_path(local_path: str | None, selected: Sequence[_SourceRepo]) -> str | None:
    """Validates `--path` cardinality and that it holds a git checkout; returns the resolved path."""
    if local_path is None:
        return None
    if len(selected) != 1:
        raise ValueError(
            "--path applies to a single repository; pass one --repo <owner>/<name> with it."
        )
    resolved = Path(local_path).expanduser().resolve()
    if not (resolved / ".git").exists():
        raise ValueError(f"Local checkout '{resolved}' has no .git directory.")
    return str(resolved)


def _check_dest_matches(dest_path: Path, selected: Sequence[_SourceRepo]) -> None:
    """Rejects split alias matches against an existing destination before any lock is taken."""
    if not dest_path.exists():
        return
    conn = _open_ro_sqlite(dest_path)
    try:
        for src in selected:
            _match_dest_repo(conn, src)
    finally:
        conn.close()


def _source_size(source_path: Path) -> int:
    """Returns the source file size including any WAL sidecar."""
    total = source_path.stat().st_size
    wal = Path(f"{source_path}-wal")
    if wal.exists():
        total += wal.stat().st_size
    return total


def selected_bytes_estimate(source_bytes: int, selected_chunks: int, total_chunks: int) -> int:
    """Estimates the source bytes attributable to the selected repositories."""
    share = max(selected_chunks / max(total_chunks, 1), MIN_SELECTED_SHARE)
    return int(source_bytes * min(share, 1.0))


def _check_free_disk(dest_path: Path, pre_bytes: int) -> None:
    """Requires `2x` the selected source bytes free next to the destination."""
    required = 2 * pre_bytes
    free = shutil.disk_usage(_existing_dir(dest_path)).free
    if free < required:
        raise ValueError(
            f"Not enough free disk space near '{dest_path}': need {format_bytes(required)} "
            f"(2x the selected source data), have {format_bytes(free)}."
        )


def _run_preflight(
    dest_path: Path | str,
    source_path: Path | str,
    *,
    repo_tokens: Sequence[str] | None,
    local_path: str | None,
    check_disk: bool,
) -> _Preflight:
    """Validates every input without taking `sync.lock` or writing to the destination."""
    dest = Path(dest_path).expanduser()
    source = _resolve_source_path(dest, source_path)
    src_conn = _open_ro_sqlite(source)
    try:
        version = _check_source_schema(src_conn)
        selected = _select_source_repos(src_conn, repo_tokens)
        total_chunks = int(src_conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])
    finally:
        src_conn.close()
    resolved_local = _check_local_path(local_path, selected)
    _check_dest_matches(dest, selected)
    source_bytes = _source_size(source)
    if check_disk:
        selected_chunks = sum(r.chunk_count for r in selected)
        _check_free_disk(dest, selected_bytes_estimate(source_bytes, selected_chunks, total_chunks))
    return _Preflight(
        dest_path=dest,
        source_path=source,
        source_schema_version=version,
        selected=tuple(selected),
        local_path=resolved_local,
        source_bytes=source_bytes,
        total_chunks=total_chunks,
    )


def validate_import_preflight(
    dest_path: Path | str,
    source_path: Path | str,
    *,
    repo_tokens: Sequence[str] | None = None,
    local_path: str | None = None,
    check_disk: bool = True,
) -> Path:
    """Validates an import before `sync.lock` is taken or the destination is opened for writing.

    Returns:
        The resolved source path.

    Raises:
        ValueError: On a bad source path, unsupported old schema, unknown or
            `items` `--repo` token, invalid `--path`, split destination alias
            match, or (when `check_disk`) insufficient free disk space.
        SchemaTooNewError: When the source schema is newer than this packrat.
    """
    return _run_preflight(
        dest_path,
        source_path,
        repo_tokens=repo_tokens,
        local_path=local_path,
        check_disk=check_disk,
    ).source_path


# ----- preview -------------------------------------------------------------------------


def _summarize(
    pre: _Preflight,
    outcomes: Sequence[ImportRepoOutcome],
    *,
    dest_schema_version: int,
    dry_run: bool,
    started: float,
) -> ImportSummary:
    """Builds an `ImportSummary` by counting dispositions."""
    counts = {d: 0 for d in ("added", "promoted", "replaced", "skipped_older", "failed", "not_attempted")}
    for outcome in outcomes:
        counts[outcome.disposition] += 1
    return ImportSummary(
        source_db_path=str(pre.source_path),
        dest_db_path=str(pre.dest_path.resolve()) if pre.dest_path.exists() else str(pre.dest_path),
        source_schema_version=pre.source_schema_version,
        dest_schema_version=dest_schema_version,
        dry_run=dry_run,
        outcomes=tuple(outcomes),
        added_count=counts["added"],
        promoted_count=counts["promoted"],
        replaced_count=counts["replaced"],
        skipped_older_count=counts["skipped_older"],
        failed_count=counts["failed"],
        not_attempted_count=counts["not_attempted"],
        chunks_imported=sum(o.chunks_imported for o in outcomes),
        embeddings_imported=sum(o.embeddings_imported for o in outcomes),
        duration_sec=round(time.monotonic() - started, 3),
    )


def _preview_counts(
    src_conn: sqlite3.Connection, src: _SourceRepo, preserve_git: bool
) -> tuple[int, int]:
    """Counts the chunks and embeddings a live import of `src` would copy."""
    scope, params = _chunk_scope("c.", preserve_git)
    row = src_conn.execute(
        f"""
        SELECT COUNT(*), COUNT(ce.chunk_id)
        FROM chunks c LEFT JOIN chunk_embeddings ce
          ON ce.chunk_id = c.chunk_id AND ce.dim = {EMBEDDING_DIM}
        WHERE c.repo_id = ?{scope}
        """,
        (src.repo_id, *params),
    ).fetchone()
    return int(row[0]), int(row[1])


def _check_preview_dest(dest_path: Path) -> tuple[sqlite3.Connection | None, int]:
    """Opens an existing destination read-only and checks it is at the current schema."""
    if not dest_path.exists():
        return None, LATEST_SCHEMA_VERSION
    conn = _open_ro_sqlite(dest_path)
    version = read_schema_version(conn)
    if version > LATEST_SCHEMA_VERSION:
        conn.close()
        raise SchemaTooNewError(
            f"Destination schema version {version} is newer than this packrat supports."
        )
    if version < LATEST_SCHEMA_VERSION:
        conn.close()
        raise ValueError(
            f"Destination schema version {version} is behind {LATEST_SCHEMA_VERSION}; "
            "run any packrat command (for example 'packrat status') to migrate it first."
        )
    return conn, version


def preview_import_database(
    dest_path: Path | str,
    source_path: Path | str,
    *,
    repo_tokens: Sequence[str] | None = None,
    local_path: str | None = None,
    allow_older: bool = False,
) -> ImportSummary:
    """Reports what `import_database` would do without locking, migrating, or writing anything.

    A missing destination is treated as empty. An existing one is opened
    read-only and must already be at `LATEST_SCHEMA_VERSION`.
    """
    started = time.monotonic()
    pre = _run_preflight(
        dest_path, source_path, repo_tokens=repo_tokens, local_path=local_path, check_disk=False
    )
    dest_conn, dest_version = _check_preview_dest(pre.dest_path)
    src_conn = _open_ro_sqlite(pre.source_path)
    outcomes: list[ImportRepoOutcome] = []
    try:
        for src in pre.selected:
            dest_row = _match_dest_repo(dest_conn, src) if dest_conn is not None else None
            disposition, dest_key, preserve = _evaluate(src, dest_row, allow_older)
            chunks, embeddings = (
                (0, 0) if disposition == "skipped_older" else _preview_counts(src_conn, src, preserve)
            )
            outcomes.append(
                ImportRepoOutcome(
                    full_name=src.full_name,
                    disposition=disposition,
                    dest_repo_id=int(dest_row["repo_id"]) if dest_row is not None else None,
                    chunks_imported=chunks,
                    embeddings_imported=embeddings,
                    src_freshness=mirror_freshness_key(src.row),
                    dest_freshness=dest_key,
                    preserved_local_git=preserve,
                    bound_local_path=pre.local_path is not None,
                )
            )
    finally:
        src_conn.close()
        if dest_conn is not None:
            dest_conn.close()
    return _summarize(
        pre, outcomes, dest_schema_version=dest_version, dry_run=True, started=started
    )


# ----- live import ----------------------------------------------------------------------


def _attach_source(conn: sqlite3.Connection, source_path: Path) -> None:
    """Attaches `source_path` read-only as `src_db` and verifies SQLite resolved the URI."""
    if conn.in_transaction:
        raise ImportIntegrityError("Cannot attach the import source inside an open transaction.")
    conn.execute(f"ATTACH DATABASE ? AS {SRC_SCHEMA}", (_ro_uri(source_path),))
    attached = {
        str(row[1]): str(row[2] or "") for row in conn.execute("PRAGMA database_list")
    }
    actual = attached.get(SRC_SCHEMA, "")
    if not actual or Path(actual).resolve() != source_path.resolve():
        conn.execute(f"DETACH DATABASE {SRC_SCHEMA}")
        raise ImportIntegrityError(
            "SQLite did not open the import source read-only by URI "
            f"(attached '{actual or '<none>'}'); this build lacks SQLITE_USE_URI."
        )


def _detach_source(conn: sqlite3.Connection) -> None:
    """Detaches `src_db` after rolling back any transaction left open by an error."""
    if conn.in_transaction:
        conn.execute("ROLLBACK")
    if any(str(r[1]) == SRC_SCHEMA for r in conn.execute("PRAGMA database_list")):
        conn.execute(f"DETACH DATABASE {SRC_SCHEMA}")


def _table_columns(conn: sqlite3.Connection, schema: str, table: str) -> list[str]:
    """Returns `table`'s column names in `schema` (empty when the table does not exist)."""
    return [str(r[1]) for r in conn.execute(f"PRAGMA {schema}.table_info({table})")]


def _shared_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Returns the columns `table` has in both `main` and `src_db`, in `main` order."""
    src_cols = set(_table_columns(conn, SRC_SCHEMA, table))
    return [c for c in _table_columns(conn, "main", table) if c in src_cols]


def _chunk_scope(alias: str, preserve_git: bool) -> tuple[str, tuple[str, ...]]:
    """Returns an `AND <alias>source_kind NOT IN (...)` clause when local git chunks are kept."""
    if not preserve_git:
        return "", ()
    marks = ", ".join("?" for _ in GIT_SOURCE_KINDS)
    return f" AND {alias}source_kind NOT IN ({marks})", tuple(GIT_SOURCE_KINDS)


def _upsert_repo_row(
    conn: sqlite3.Connection,
    src: _SourceRepo,
    dest_row: sqlite3.Row | None,
    *,
    local_path: str | None,
    preserve_git: bool,
    now_iso: str,
) -> int:
    """Inserts or updates the destination `repos` row for `src`; returns the destination id."""
    main_cols = _table_columns(conn, "main", "repos")
    src_cols = set(_table_columns(conn, SRC_SCHEMA, "repos"))
    cols = [c for c in main_cols if c in src_cols]
    if dest_row is None:
        keep = [c for c in cols if c not in _REPO_INSERT_EXCLUDED]
        cur = conn.execute(
            f"INSERT INTO main.repos ({', '.join(keep)}, local_path, mode) "
            f"SELECT {', '.join(keep)}, ?, ? FROM {SRC_SCHEMA}.repos WHERE repo_id = ?",
            (local_path, REPO_MODE_MIRROR, src.repo_id),
        )
        return int(cur.lastrowid or 0)
    dest_id = int(dest_row["repo_id"])
    excluded = _REPO_UPDATE_EXCLUDED | (_GIT_REPO_COLUMNS if preserve_git else frozenset())
    keep = [c for c in cols if c not in excluded]
    # Sync-state columns the older source lacks must not keep stale destination values.
    reset = [
        c for c in main_cols
        if c in _RESETTABLE_SYNC_COLUMNS and c not in src_cols and c not in excluded
    ]
    reset_sql = "".join(f", {c} = NULL" for c in reset)
    conn.execute(
        f"UPDATE main.repos SET ({', '.join(keep)}) = "
        f"(SELECT {', '.join(keep)} FROM {SRC_SCHEMA}.repos WHERE repo_id = ?), "
        f"mode = ?, local_path = COALESCE(?, local_path), updated_at = ?{reset_sql} "
        "WHERE repo_id = ?",
        (src.repo_id, REPO_MODE_MIRROR, local_path, now_iso, dest_id),
    )
    if str(dest_row["full_name"]) != src.full_name:
        conn.execute(
            "UPDATE main.repos SET events_etag = NULL, events_last_modified = NULL WHERE repo_id = ?",
            (dest_id,),
        )
    return dest_id


def _upsert_aliases(
    conn: sqlite3.Connection, src: _SourceRepo, dest_id: int, dest_full_name: str, now_iso: str
) -> None:
    """Copies source aliases (and the source slug on renames) without shadowing other repos."""
    conn.execute(
        f"""
        INSERT INTO main.repo_aliases (alias_full_name, repo_id, created_at)
        SELECT a.alias_full_name, ?, a.created_at FROM {SRC_SCHEMA}.repo_aliases a
        WHERE a.repo_id = ?
          AND lower(a.alias_full_name) <> lower(?)
          AND NOT EXISTS (
            SELECT 1 FROM main.repos r WHERE lower(r.full_name) = lower(a.alias_full_name)
          )
        ON CONFLICT(alias_full_name) DO NOTHING
        """,
        (dest_id, src.repo_id, dest_full_name),
    )
    if src.full_name.lower() != dest_full_name.lower():
        conn.execute(
            """
            INSERT INTO main.repo_aliases (alias_full_name, repo_id, created_at)
            SELECT ?, ?, ? WHERE NOT EXISTS (
              SELECT 1 FROM main.repos r WHERE lower(r.full_name) = lower(?)
            )
            ON CONFLICT(alias_full_name) DO NOTHING
            """,
            (src.full_name, dest_id, now_iso, src.full_name),
        )


def _delete_repo_rows(conn: sqlite3.Connection, dest_id: int, preserve_git: bool) -> None:
    """Deletes the destination repository's replaceable rows, FTS entries first."""
    scope, params = _chunk_scope("", preserve_git)
    conn.execute(
        "DELETE FROM main.chunks_fts WHERE rowid IN "
        f"(SELECT rowid FROM main.chunks WHERE repo_id = ?{scope})",
        (dest_id, *params),
    )
    conn.execute(f"DELETE FROM main.chunks WHERE repo_id = ?{scope}", (dest_id, *params))
    tables = REPO_GITHUB_TABLES if preserve_git else (*REPO_GITHUB_TABLES, *REPO_GIT_TABLES)
    for table in tables:
        conn.execute(f"DELETE FROM main.{table} WHERE repo_id = ?", (dest_id,))


def _copy_table(
    conn: sqlite3.Connection,
    table: str,
    src_id: int,
    dest_id: int,
    *,
    extra_where: str = "",
    extra_params: Sequence[Any] = (),
    verb: str = "INSERT OR REPLACE",
) -> int:
    """Copies one `repo_id`-keyed table from `src_db` with column intersection; returns rows."""
    cols = _shared_columns(conn, table)
    if not cols:
        return 0
    exprs = ", ".join("?" if c == "repo_id" else c for c in cols)
    params: list[Any] = [dest_id] if "repo_id" in cols else []
    cur = conn.execute(
        f"{verb} INTO main.{table} ({', '.join(cols)}) SELECT {exprs} "
        f"FROM {SRC_SCHEMA}.{table} WHERE repo_id = ?{extra_where}",
        (*params, src_id, *extra_params),
    )
    return int(cur.rowcount or 0)


def _copy_repo_tables(
    conn: sqlite3.Connection, src_id: int, dest_id: int, preserve_git: bool
) -> tuple[int, int]:
    """Copies GitHub (and optionally git) tables, chunks, and embeddings; returns copied counts."""
    tables = REPO_GITHUB_TABLES if preserve_git else (*REPO_GITHUB_TABLES, *REPO_GIT_TABLES)
    for table in tables:
        _copy_table(conn, table, src_id, dest_id)
    scope, params = _chunk_scope("", preserve_git)
    chunks = _copy_table(
        conn, "chunks", src_id, dest_id, extra_where=scope, extra_params=params, verb="INSERT"
    )
    cols = _shared_columns(conn, "chunk_embeddings")
    join_scope, join_params = _chunk_scope("c.", preserve_git)
    cur = conn.execute(
        f"INSERT OR REPLACE INTO main.chunk_embeddings ({', '.join(cols)}) "
        f"SELECT {', '.join('ce.' + c for c in cols)} FROM {SRC_SCHEMA}.chunk_embeddings ce "
        f"JOIN {SRC_SCHEMA}.chunks c ON c.chunk_id = ce.chunk_id "
        f"WHERE c.repo_id = ? AND ce.dim = ?{join_scope}",
        (src_id, EMBEDDING_DIM, *join_params),
    )
    return chunks, int(cur.rowcount or 0)


def _rewrite_prefixes(
    ctx: _ImportContext, dest_id: int, src_full_name: str, dest_full_name: str
) -> None:
    """Rewrites `<src>#`, `<src>@`, and `<src>:` chunk ids to the destination slug.

    Embeddings go first (as `rename_repo` does) under deferred foreign keys,
    because `chunk_embeddings.chunk_id` has no `ON UPDATE CASCADE`.
    """
    if src_full_name == dest_full_name:
        return
    conn = ctx.conn
    conn.execute("PRAGMA defer_foreign_keys = ON")
    plen = len(src_full_name)
    marks = ", ".join("?" for _ in _PREFIX_SEPARATORS)
    prefix_match = f"lower(substr({{col}}, 1, ?)) = lower(?) AND substr({{col}}, ?, 1) IN ({marks})"
    conn.execute(
        "UPDATE main.chunk_embeddings SET chunk_id = ? || substr(chunk_id, ?) "
        "WHERE chunk_id IN (SELECT chunk_id FROM main.chunks WHERE repo_id = ?) "
        f"AND {prefix_match.format(col='chunk_id')}",
        (dest_full_name, plen + 1, dest_id, plen, src_full_name, plen + 1, *_PREFIX_SEPARATORS),
    )
    for column in ("chunk_id", "source_ref"):
        conn.execute(
            f"UPDATE main.chunks SET {column} = ? || substr({column}, ?) "
            f"WHERE repo_id = ? AND {prefix_match.format(col=column)}",
            (dest_full_name, plen + 1, dest_id, plen, src_full_name, plen + 1, *_PREFIX_SEPARATORS),
        )
    ctx.store._rewrite_timeline_subjects_in_tx(src_full_name, dest_full_name)


def _apply_row_fixups(conn: sqlite3.Connection, source_version: int, dest_id: int) -> None:
    """Replays every migration row fixup newer than the source schema on the imported rows."""
    for version in range(source_version + 1, LATEST_SCHEMA_VERSION + 1):
        fixup = IMPORT_ROW_FIXUPS.get(version)
        if fixup is not None:
            fixup(conn, dest_id)


def _rebuild_fts(conn: sqlite3.Connection, dest_id: int, preserve_git: bool) -> None:
    """Indexes the newly copied chunks in the external-content `chunks_fts` table."""
    scope, params = _chunk_scope("", preserve_git)
    conn.execute(
        "INSERT INTO main.chunks_fts (rowid, chunk_id, source_kind, source_ref, title, text) "
        "SELECT rowid, chunk_id, source_kind, source_ref, title, text "
        f"FROM main.chunks WHERE repo_id = ?{scope}",
        (dest_id, *params),
    )


def _sync_state_keys(ctx: _ImportContext, src_id: int, dest_id: int) -> None:
    """Replaces per-repository `service_state` sync cursors and bumps `vector_cache_seq`."""
    conn = ctx.conn
    for prefix in REPO_STATE_KEY_PATTERNS:
        exact = f"{prefix}{dest_id}"
        conn.execute(
            "DELETE FROM main.service_state WHERE key = ? OR key LIKE ?",
            (exact, f"{exact}:%"),
        )
        src_exact = f"{prefix}{src_id}"
        conn.execute(
            "INSERT OR REPLACE INTO main.service_state (key, value, updated_at) "
            "SELECT ? || substr(key, ?), value, updated_at "
            f"FROM {SRC_SCHEMA}.service_state WHERE key = ? OR key LIKE ?",
            (exact, len(src_exact) + 1, src_exact, f"{src_exact}:%"),
        )
    ctx.store._bump_vector_cache_seq_in_tx(ctx.now)


def _queue_reader_refresh(
    ctx: _ImportContext, dest_id: int, dest_full_name: str, now_iso: str
) -> None:
    """Queues refreshes and `pr` change rows for leased and viewer-set items of a replaced repo."""
    conn = ctx.conn
    conn.execute(
        """
        INSERT OR IGNORE INTO main.refresh_requests (repo_id, item_number, requested_at)
        SELECT repo_id, item_number, ? FROM main.watches WHERE repo_id = ?
        UNION
        SELECT repo_id, item_number, ? FROM main.viewer_sets
        WHERE repo_id = ? AND set_name <> 'issues'
        """,
        (now_iso, dest_id, now_iso, dest_id),
    )
    numbers = conn.execute(
        """
        SELECT DISTINCT n FROM (
          SELECT item_number AS n FROM main.watches WHERE repo_id = ?
          UNION
          SELECT item_number FROM main.viewer_sets WHERE repo_id = ? AND set_name <> 'issues'
        ) WHERE n IN (SELECT number FROM main.pull_requests WHERE repo_id = ?)
        ORDER BY n
        """,
        (dest_id, dest_id, dest_id),
    ).fetchall()
    for row in numbers:
        ctx.store._append_change_in_tx(
            f"{dest_full_name}#{int(row[0])}", "pr", _CHANGE_FIELDS, ctx.now
        )


def _verify_before_commit(conn: sqlite3.Connection, dest_id: int) -> None:
    """Runs foreign-key and FTS5 integrity checks while the transaction can still roll back."""
    tables = (*REPO_GITHUB_TABLES, *REPO_GIT_TABLES, "chunks", "chunk_embeddings", "repo_aliases", "refresh_requests")
    for table in tables:
        bad = conn.execute(f"PRAGMA main.foreign_key_check({table})").fetchone()
        if bad is not None:
            # (table, rowid, parent, fkid); the violation may predate this import.
            raise ImportIntegrityError(
                f"Foreign-key violation in {table} after importing repo_id {dest_id}: "
                f"rowid {bad[1]} references a missing {bad[2]} row (may predate this import)."
            )
    try:
        conn.execute("INSERT INTO main.chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)")
    except sqlite3.DatabaseError as exc:
        raise ImportIntegrityError(
            f"Import of repo_id {dest_id} left chunks_fts out of sync: {exc}"
        ) from exc


def _bind_local_path(
    conn: sqlite3.Connection, dest_row: sqlite3.Row | None, local_path: str | None, now_iso: str
) -> bool:
    """Binds `--path` on a skipped repository in its own short transaction."""
    if dest_row is None or local_path is None:
        return False
    with conn:
        conn.execute(
            "UPDATE main.repos SET local_path = ?, updated_at = ? WHERE repo_id = ?",
            (local_path, now_iso, int(dest_row["repo_id"])),
        )
    return True


def _warn_long_copy(ctx: _ImportContext, src: _SourceRepo) -> None:
    """Warns when one repository's copy will likely hold the write lock past the reader window."""
    if ctx.reporter is None:
        return
    est_bytes = selected_bytes_estimate(ctx.pre.source_bytes, src.chunk_count, ctx.pre.total_chunks)
    est_sec = est_bytes / (1024**3) * IMPORT_SEC_PER_GIB
    if est_sec <= LONG_COPY_WARN_SEC:
        return
    ctx.reporter.warn(
        "import",
        f"{src.full_name} is about {format_bytes(est_bytes)}; copying it holds the write lock "
        f"for roughly {est_sec:.0f}s, during which readers see packrat serve as stale.",
    )


def _copy_repo_tx(
    ctx: _ImportContext,
    src: _SourceRepo,
    dest_row: sqlite3.Row | None,
    disposition: ImportDisposition,
    preserve_git: bool,
) -> tuple[int, int, int]:
    """Copies one repository inside `BEGIN IMMEDIATE`; returns `(dest_id, chunks, embeddings)`."""
    conn = ctx.conn
    if conn.in_transaction:
        raise ImportIntegrityError("A transaction was already open before BEGIN IMMEDIATE.")
    now_iso = iso_at(ctx.now)
    tracker = ctx.tracker
    tracker.begin("copy", detail=f"{src.full_name}: step 1/6, repository row")
    conn.execute("BEGIN IMMEDIATE")
    with tracker.vm_heartbeat(conn):
        dest_id = _upsert_repo_row(
            conn, src, dest_row, local_path=ctx.pre.local_path, preserve_git=preserve_git, now_iso=now_iso
        )
        dest_full_name = str(dest_row["full_name"]) if dest_row is not None else src.full_name
        _upsert_aliases(conn, src, dest_id, dest_full_name, now_iso)
        if dest_row is not None:
            tracker.step(f"{src.full_name}: step 2/6, clearing replaced rows")
            _delete_repo_rows(conn, dest_id, preserve_git)
        tracker.step(f"{src.full_name}: step 3/6, copying tables")
        chunks, embeddings = _copy_repo_tables(conn, src.repo_id, dest_id, preserve_git)
        tracker.step(f"{src.full_name}: step 4/6, rewriting keys and fixups")
        _rewrite_prefixes(ctx, dest_id, src.full_name, dest_full_name)
        _apply_row_fixups(conn, ctx.pre.source_schema_version, dest_id)
        tracker.step(f"{src.full_name}: step 5/6, indexing FTS and state")
        _rebuild_fts(conn, dest_id, preserve_git)
        _sync_state_keys(ctx, src.repo_id, dest_id)
        if disposition != "added":
            _queue_reader_refresh(ctx, dest_id, dest_full_name, now_iso)
        tracker.step(f"{src.full_name}: step 6/6, checking integrity")
        _verify_before_commit(conn, dest_id)
    conn.execute("COMMIT")
    tracker.end(f"{src.full_name} {disposition}, {chunks:,} chunks")
    conn.execute("PRAGMA main.wal_checkpoint(PASSIVE)")
    return dest_id, chunks, embeddings


def _import_repo(ctx: _ImportContext, src: _SourceRepo) -> ImportRepoOutcome:
    """Evaluates and (unless skipped) copies one source repository."""
    dest_row = _match_dest_repo(ctx.conn, src)
    disposition, dest_key, preserve = _evaluate(src, dest_row, ctx.allow_older)
    src_key = mirror_freshness_key(src.row)
    if disposition == "skipped_older":
        bound = _bind_local_path(ctx.conn, dest_row, ctx.pre.local_path, iso_at(ctx.now))
        return ImportRepoOutcome(
            full_name=src.full_name,
            disposition=disposition,
            dest_repo_id=int(dest_row["repo_id"]) if dest_row is not None else None,
            chunks_imported=0,
            embeddings_imported=0,
            src_freshness=src_key,
            dest_freshness=dest_key,
            bound_local_path=bound,
        )
    _warn_long_copy(ctx, src)
    dest_id, chunks, embeddings = _copy_repo_tx(ctx, src, dest_row, disposition, preserve)
    return ImportRepoOutcome(
        full_name=src.full_name,
        disposition=disposition,
        dest_repo_id=dest_id,
        chunks_imported=chunks,
        embeddings_imported=embeddings,
        src_freshness=src_key,
        dest_freshness=dest_key,
        preserved_local_git=preserve,
        bound_local_path=ctx.pre.local_path is not None,
    )


def _failed_outcomes(
    failed: _SourceRepo, remaining: Sequence[_SourceRepo], error: str
) -> list[ImportRepoOutcome]:
    """Builds `failed` and `not_attempted` outcomes for an aborted batch."""
    rows = [
        ImportRepoOutcome(
            full_name=failed.full_name,
            disposition="failed",
            dest_repo_id=None,
            chunks_imported=0,
            embeddings_imported=0,
            src_freshness=mirror_freshness_key(failed.row),
            error=error,
        )
    ]
    rows.extend(
        ImportRepoOutcome(
            full_name=r.full_name,
            disposition="not_attempted",
            dest_repo_id=None,
            chunks_imported=0,
            embeddings_imported=0,
            src_freshness=mirror_freshness_key(r.row),
        )
        for r in remaining
    )
    return rows


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    """Rolls back an open transaction, ignoring secondary SQLite errors."""
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def import_database(
    store: PackratStore,
    source_path: Path | str,
    *,
    repo_tokens: Sequence[str] | None = None,
    local_path: str | None = None,
    allow_older: bool = False,
    reporter: ProgressReporter | None = None,
    now: float | None = None,
) -> ImportSummary:
    """Merges selected `mirror` repositories from `source_path` into `store`.

    The caller holds `sync.lock`. Each repository commits on its own; a failure
    rolls back only the repository in flight and raises `ImportAbortedError`
    carrying the committed, failed, and not-attempted outcomes.

    Raises:
        ValueError, SchemaTooNewError: From preflight validation (nothing written).
        ImportAbortedError: When a repository transaction fails after preflight.
    """
    started = time.monotonic()
    pre = _run_preflight(
        store.db_path, source_path, repo_tokens=repo_tokens, local_path=local_path, check_disk=True
    )
    conn = store.conn
    ctx = _ImportContext(
        store=store,
        conn=conn,
        pre=pre,
        allow_older=allow_older,
        now=now,
        tracker=SqlitePhaseProgress(reporter, group="import"),
        reporter=reporter,
    )
    _attach_source(conn, pre.source_path)
    try:
        _check_source_schema(conn, SRC_SCHEMA)
        for index, src in enumerate(pre.selected):
            try:
                ctx.outcomes.append(_import_repo(ctx, src))
            except BaseException as exc:
                _rollback_quietly(conn)
                partial = _summarize(
                    pre,
                    [*ctx.outcomes, *_failed_outcomes(src, pre.selected[index + 1 :], str(exc))],
                    dest_schema_version=LATEST_SCHEMA_VERSION,
                    dry_run=False,
                    started=started,
                )
                raise ImportAbortedError(
                    f"import stopped at {src.full_name}: {exc}", partial
                ) from exc
        conn.execute("PRAGMA main.wal_checkpoint(TRUNCATE)")
    finally:
        _detach_source(conn)
    return _summarize(
        pre, ctx.outcomes, dest_schema_version=LATEST_SCHEMA_VERSION, dry_run=False, started=started
    )
