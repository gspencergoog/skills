"""In-process multi-user HTTP v1 server and sandboxed DuckDB endpoint for packrat.

Serves all `/api/v1/*` routes on a `ThreadingHTTPServer` sharing in-memory
`ChangeNotifier` wakeups and `TokenRegistry` transports with `PackratService`.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import os
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

try:
    from .db_store import PackratStore, mirror_freshness_key
    from .embedder import Embedder
    from .gh_transport import (
        BudgetDeferred,
        GhApiError,
        GhRunner,
        GhTransport,
        NullApiCallStore,
        RateLimited,
        make_token_gh_runner,
    )
    from .managed_git import (
        git_status,
        validate_branch,
        validate_slug,
        validate_url,
    )
    from .models import (
        BACKFILL_STREAMS,
        UNTRUSTED_PROSE_COLUMNS,
        PendingDeleteError,
        RepoConfig,
        SearchFilters,
        SearchResponseEnvelope,
        ServerConfig,
        SqlColumnMeta,
        SqlResponseEnvelope,
        parse_item_key,
        wrap_untrusted_text,
    )
    from .personal_store import (
        ChangeNotifier,
        IdentityConflictError,
        PersonalStore,
        TooManyWaitersError,
        normalize_github_login,
    )
    from .rate_budget import (
        RATE_BUDGET_FILENAME,
        InMemoryRateBudget,
        Priority,
        RateBudget,
    )
    from .reader_ops import _backfill_warnings_for_repo, execute_reader_batch
    from .search_engine import (
        VALID_SEARCH_MODES,
        hybrid_rrf_search,
        repo_watermarks,
    )
    from .service import disk_usage_stats
except ImportError:
    from db_store import PackratStore, mirror_freshness_key  # type: ignore[no-redef]
    from embedder import Embedder  # type: ignore[no-redef]
    from gh_transport import (  # type: ignore[no-redef]
        BudgetDeferred,
        GhApiError,
        GhRunner,
        GhTransport,
        NullApiCallStore,
        RateLimited,
        make_token_gh_runner,
    )
    from managed_git import (  # type: ignore[no-redef]
        git_status,
        validate_branch,
        validate_slug,
        validate_url,
    )
    from models import (  # type: ignore[no-redef]
        BACKFILL_STREAMS,
        UNTRUSTED_PROSE_COLUMNS,
        PendingDeleteError,
        RepoConfig,
        SearchFilters,
        SearchResponseEnvelope,
        ServerConfig,
        SqlColumnMeta,
        SqlResponseEnvelope,
        parse_item_key,
        wrap_untrusted_text,
    )
    from personal_store import (  # type: ignore[no-redef]
        ChangeNotifier,
        IdentityConflictError,
        PersonalStore,
        TooManyWaitersError,
        normalize_github_login,
    )
    from rate_budget import (  # type: ignore[no-redef]
        RATE_BUDGET_FILENAME,
        InMemoryRateBudget,
        Priority,
        RateBudget,
    )
    from reader_ops import (  # type: ignore[no-redef]
        _backfill_warnings_for_repo,
        execute_reader_batch,
    )
    from search_engine import (  # type: ignore[no-redef]
        VALID_SEARCH_MODES,
        hybrid_rrf_search,
        repo_watermarks,
    )
    from service import disk_usage_stats  # type: ignore[no-redef]

MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024
MAX_SQL_STDOUT_BYTES = 16 * 1024 * 1024
MAX_SQL_ROWS = 10_000
DEFAULT_SQL_TIMEOUT_S = 20.0
DEFAULT_SQL_CONCURRENCY = 4
DEFAULT_TOKEN_TTL_S = 300.0
DEFAULT_EXPORT_CACHE_TTL_S = 3600.0
MAX_CHANGES_WAIT_S = 60.0
MAX_CHANGES_LIMIT = 500
MAX_REFRESH_KEYS = 50
MAX_SEARCH_LIMIT = 100
_EXPORT_STREAM_CHUNK = 65536
_IDENTITY_TOUCH_INTERVAL_S = 3600.0

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"),
    ("Cache-Control", "no-store"),
    ("X-Packrat-Api-Version", "1"),
)

_SERVER_STARTUP_HOME = Path.home()
_INIT_SQL_TEMPLATE_PATH = Path(__file__).resolve().parent / "init.sql"
_FS_PATH_RE = re.compile(r"(?:/[A-Za-z0-9._-]+){2,}")
_PERSONAL_VIEW_SQL_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+"
    r"(?:v_viewer_items|v_notifications|v_watches|v_changes|v_api_calls|v_service_health)"
    r"\s+AS\b.*?;",
    re.IGNORECASE | re.DOTALL,
)
_ATTACH_KB_RE = re.compile(
    r"ATTACH\s+(?:IF\s+NOT\s+EXISTS\s+)?'[^']*'\s+AS\s+kb\b.*?;",
    re.IGNORECASE | re.DOTALL,
)
_AGENT_ENV_KEYS: tuple[str, ...] = (
    "AI_AGENT",
    "ANTIGRAVITY_AGENT",
    "CLAUDECODE",
    "GEMINI_CLI",
    "CURSOR_AGENT",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)


class _ApiError(Exception):
    """Structured HTTP v1 API exception mapped directly to an error response."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message
        self.retryable = bool(retryable)


ApiError = _ApiError


@dataclass(frozen=True)
class _RequestContext:
    """Per-request authenticated caller context and thread-local stores."""

    owner_id: str
    viewer_login: str
    login_verified: bool
    has_bearer: bool
    read_store: PackratStore
    personal_store: PersonalStore
    now: float


def _sanitize_error_text(raw: str, redact_paths: Sequence[str] = ()) -> str:
    """Strips local filesystem paths and `-init` banners from diagnostic strings."""
    cleaned_lines: list[str] = []
    for line in (raw or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("-- Loading resources from"):
            continue
        for secret_path in redact_paths:
            if secret_path:
                stripped = stripped.replace(secret_path, "<db>")
        stripped = _FS_PATH_RE.sub("<path>", stripped)
        cleaned_lines.append(stripped)
    joined = " ".join(cleaned_lines).strip()
    return (
        (joined[:397] + "...") if len(joined) > 400 else (joined or "Request failed.")
    )


def parse_listen_address(spec: tuple[str, int] | str) -> tuple[str, int]:
    """Parses a `HOST:PORT` string or `(host, port)` tuple."""
    if isinstance(spec, tuple):
        return str(spec[0]), int(spec[1])
    raw = str(spec).strip()
    if not raw:
        raise ValueError("HTTP listen address must be non-empty.")
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1 or end + 1 >= len(raw) or raw[end + 1] != ":":
            raise ValueError(f"Invalid IPv6 listen address {raw!r}.")
        return raw[1:end], int(raw[end + 2 :])
    host, sep, port_str = raw.rpartition(":")
    if not sep or not port_str:
        raise ValueError(f"Expected HOST:PORT listen address, got {raw!r}.")
    return (host or "127.0.0.1"), int(port_str)


# ----- 1. Edge Authentication ----------------------------------------------------


def sign_edge_request(
    kid: str,
    secret: str | bytes,
    ts: int,
    method: str,
    raw_path_with_query: str,
    email: str,
) -> str:
    """Builds the `X-Packrat-Edge-Sig` header value for an edge-proxied request."""
    key_bytes = secret if isinstance(secret, bytes) else secret.encode()
    payload = f"{int(ts)}\n{method.upper()}\n{raw_path_with_query}\n{email}".encode()
    digest = hmac.new(key_bytes, payload, hashlib.sha256).hexdigest()
    return f"kid={kid},ts={int(ts)},sig={digest}"


def _parse_edge_sig_header(raw_sig: str) -> tuple[str, int, str]:
    """Extracts `(kid, ts, sig_hex)` from `X-Packrat-Edge-Sig`."""
    fields: dict[str, str] = {}
    for item in raw_sig.split(","):
        part = item.strip()
        if "=" not in part:
            raise _ApiError(401, "unauthorized", "Malformed edge signature header.")
        key, val = part.split("=", 1)
        k_clean = key.strip()
        if k_clean in fields or not val.strip():
            raise _ApiError(401, "unauthorized", "Invalid edge signature parameters.")
        fields[k_clean] = val.strip()
    kid = fields.get("kid", "")
    ts_raw = fields.get("ts", "")
    sig = fields.get("sig", "")
    if not kid or not sig or not ts_raw.isdigit():
        raise _ApiError(401, "unauthorized", "Incomplete edge signature header.")
    return kid, int(ts_raw), sig.lower()


def _decode_hmac_mapping(raw_map: Mapping[str, Any]) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for kid, secret in raw_map.items():
        clean_kid = str(kid).strip()
        if not clean_kid or secret is None:
            continue
        if isinstance(secret, bytes):
            if secret:
                out[clean_kid] = secret
        elif isinstance(secret, str) and secret.strip():
            out[clean_kid] = secret.strip().encode()
    return out


def _load_hmac_keys_from_env(env_name: str) -> dict[str, bytes]:
    """Loads `{kid: key_bytes}` from `os.environ[env_name]` JSON text or file path."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return {}
    if not raw.startswith("{"):
        candidate = Path(raw)
        if candidate.is_file():
            raw = candidate.read_text(encoding="utf-8").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in HMAC key configuration {env_name}.") from exc
    if not isinstance(parsed, Mapping):
        raise TypeError(f"HMAC key configuration {env_name} must be a JSON object.")
    nested = parsed.get("keys")
    source = nested if isinstance(nested, Mapping) else parsed
    return _decode_hmac_mapping(source)


def _extract_edge_user(headers: Mapping[str, str]) -> str:
    for name in (
        "X-Packrat-User",
        "X-Packrat-Verified-User",
        "X-Packrat-User-Email",
    ):
        val = (headers.get(name) or "").strip()
        if val:
            return val
    return ""


class EdgeAuthenticator:
    """Authenticates caller identity on loopback or behind an HMAC-signing edge proxy."""

    def __init__(
        self,
        *,
        trust_edge_proxy: bool = False,
        hmac_keys: Mapping[str, str | bytes] | None = None,
        hmac_secret_env: str = "PACKRAT_EDGE_HMAC_KEYS_JSON",
        max_skew_s: int = 60,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.trust_edge_proxy = bool(trust_edge_proxy)
        self.hmac_secret_env = hmac_secret_env
        self.max_skew_s = int(max_skew_s)
        self._clock = clock
        self._explicit_keys = (
            _decode_hmac_mapping(hmac_keys) if hmac_keys is not None else None
        )

    def _active_keys(self) -> dict[str, bytes]:
        if self._explicit_keys is not None:
            return self._explicit_keys
        try:
            return _load_hmac_keys_from_env(self.hmac_secret_env)
        except (OSError, TypeError, ValueError):
            return {}

    def authenticate(
        self,
        method: str,
        raw_path_with_query: str,
        headers: Mapping[str, str],
    ) -> str:
        """Returns verified `owner_id` or raises `_ApiError(401, 'unauthorized', ...)`."""
        if not self.trust_edge_proxy:
            return "local"
        raw_user = _extract_edge_user(headers)
        raw_sig = (headers.get("X-Packrat-Edge-Sig") or "").strip()
        if not raw_user or not raw_sig:
            raise _ApiError(401, "unauthorized", "Missing edge authentication headers.")
        kid, ts_val, sig_hex = _parse_edge_sig_header(raw_sig)
        now = self._clock()
        if abs(now - ts_val) > self.max_skew_s:
            raise _ApiError(
                401, "unauthorized", "Edge signature timestamp outside skew window."
            )
        key_bytes = self._active_keys().get(kid)
        if key_bytes is None:
            raise _ApiError(401, "unauthorized", "Unknown edge signature key ID.")
        message = (
            f"{ts_val}\n{method.upper()}\n{raw_path_with_query}\n{raw_user}".encode()
        )
        expected = hmac.new(key_bytes, message, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig_hex):
            raise _ApiError(401, "unauthorized", "Invalid edge signature.")
        return raw_user.lower()


# ----- 2. Per-Owner Bearer Token Registry ----------------------------------------


@dataclass
class CachedToken:
    """In-memory sliding-TTL cache entry for a caller's verified GitHub token."""

    token_sha256: str
    github_login: str
    transport: Any
    budget: InMemoryRateBudget
    expires_at: float


def _hash_github_token(token: str) -> str:
    return hashlib.sha256(b"github.com\0" + token.encode()).hexdigest()


def _default_verify_github_user(transport: GhTransport) -> str:
    """Verifies a bearer token via `GET /user` and returns normalized `login`."""
    try:
        resp = transport.rest_get("user", worker="auth", priority=Priority.HOT)
        payload = json.loads(resp.body)
    except (GhApiError, BudgetDeferred, json.JSONDecodeError, OSError) as exc:
        raise _ApiError(401, "unauthorized", "Invalid GitHub bearer token.") from exc
    if not isinstance(payload, Mapping) or not payload.get("login"):
        raise _ApiError(401, "unauthorized", "GitHub /user response missing login.")
    try:
        return normalize_github_login(str(payload["login"]))
    except ValueError as exc:
        raise _ApiError(401, "unauthorized", "Invalid GitHub login.") from exc


class TokenRegistry:
    """Thread-safe 5-minute sliding cache of verified per-owner GitHub transports."""

    def __init__(
        self,
        empty_config_dir: Path,
        *,
        verifier: Callable[[str], str | tuple[str, Any]] | None = None,
        runner_factory: Callable[[str, Path], GhRunner] = make_token_gh_runner,
        ttl_s: float = DEFAULT_TOKEN_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.empty_config_dir = Path(empty_config_dir)
        self.empty_config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.empty_config_dir, 0o700)
        self._verifier = verifier
        self._runner_factory = runner_factory
        self.ttl_s = float(ttl_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, CachedToken] = {}

    def _lookup_valid(
        self, owner_id: str, token_sha: str, moment: float
    ) -> CachedToken | None:
        with self._lock:
            entry = self._cache.get(owner_id)
            if (
                entry is not None
                and entry.token_sha256 == token_sha
                and moment < entry.expires_at
            ):
                entry.expires_at = moment + self.ttl_s
                return entry
            return None

    def _verify_and_build(
        self, clean_token: str, token_sha: str, moment: float
    ) -> CachedToken:
        budget = InMemoryRateBudget(clock=self._clock)
        runner = self._runner_factory(clean_token, self.empty_config_dir)
        transport: Any = GhTransport(budget, NullApiCallStore(), runner=runner)
        if self._verifier is None:
            login = _default_verify_github_user(transport)
        else:
            try:
                outcome = self._verifier(clean_token)
                if isinstance(outcome, tuple):
                    raw_login, transport = outcome
                else:
                    raw_login = outcome
                login = normalize_github_login(str(raw_login))
            except _ApiError:
                raise
            except (
                GhApiError,
                BudgetDeferred,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                raise _ApiError(
                    401, "unauthorized", "Invalid GitHub bearer token."
                ) from exc
        return CachedToken(
            token_sha256=token_sha,
            github_login=login,
            transport=transport,
            budget=budget,
            expires_at=moment + self.ttl_s,
        )

    def authenticate(
        self,
        owner_id: str,
        raw_token: str,
        personal_store: PersonalStore,
        *,
        now: float | None = None,
    ) -> CachedToken:
        """Verifies or refreshes `raw_token` and links `owner_id` with `verified=True`."""
        clean_token = raw_token.strip()
        if not clean_token:
            raise _ApiError(401, "unauthorized", "Empty bearer token.")
        moment = self._clock() if now is None else now
        token_sha = _hash_github_token(clean_token)
        cached = self._lookup_valid(owner_id, token_sha, moment)
        if cached is not None:
            ident = personal_store.get_identity(owner_id)
            if (
                ident is None
                or not ident.login_verified
                or ident.github_login != cached.github_login
            ):
                personal_store.link_identity(
                    owner_id, cached.github_login, verified=True, now=moment
                )
            return cached

        entry = self._verify_and_build(clean_token, token_sha, moment)
        with self._lock:
            self._cache[owner_id] = entry
        personal_store.link_identity(
            owner_id, entry.github_login, verified=True, now=moment
        )
        return entry

    def transport_for_owner(
        self, owner_id: str, *, now: float | None = None
    ) -> Any | None:
        """Returns the unexpired cached `GhTransport` for `owner_id`, or None."""
        moment = self._clock() if now is None else now
        with self._lock:
            entry = self._cache.get(owner_id)
            if entry is None:
                return None
            if moment >= entry.expires_at:
                del self._cache[owner_id]
                return None
            return entry.transport


# ----- 3. Sandboxed Per-Request DuckDB SQL Runner --------------------------------


def _resolve_duckdb_binary(explicit: str | None = None) -> str:
    """Resolves the DuckDB CLI binary, preferring direct binaries over shell shims."""
    if explicit:
        return explicit
    env_bin = os.environ.get("DUCKDB_BIN", "").strip()
    if env_bin:
        return env_bin
    candidates = (
        _SERVER_STARTUP_HOME
        / ".local/share/mise/installs/github-duckdb-duckdb/2/duckdb",
        _SERVER_STARTUP_HOME / ".local/share/mise/installs/duckdb/2/duckdb",
        _SERVER_STARTUP_HOME / "cmd/duckdb",
        _SERVER_STARTUP_HOME / ".local/bin/duckdb",
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
    found = shutil.which("duckdb")
    if found:
        return found
    return str(_SERVER_STARTUP_HOME / ".local/bin/duckdb")


def _strip_init_sql_template(raw_sql: str) -> str:
    """Extracts shared `kb` views and vector macros from `init.sql`."""
    without_ext = re.sub(
        r"^\s*(?:INSTALL|LOAD)\s+sqlite\s*;\s*$",
        "",
        raw_sql,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    without_attach = _ATTACH_KB_RE.sub("", without_ext)
    return _PERSONAL_VIEW_SQL_RE.sub("", without_attach).strip()


def _sql_literal(value: Any) -> str:
    """Formats a Python scalar as a SQL-escaped literal for DuckDB temp tables."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value) if math.isfinite(value) else "NULL"
    cleaned = str(value).replace("\x00", "").replace("'", "''")
    return f"'{cleaned}'"


def _insert_values_sql(table: str, rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return ""
    tuples = [f"({', '.join(_sql_literal(v) for v in row)})" for row in rows]
    return f"INSERT INTO {table} VALUES\n  " + ",\n  ".join(tuples) + ";\n"


def _item_key_is_pending(item_key: Any, pending_repos: set[str]) -> bool:
    if not pending_repos or not isinstance(item_key, str) or "#" not in item_key:
        return False
    return item_key.split("#", 1)[0].lower() in pending_repos


def _build_personal_views_sql(
    temp_rows: Mapping[str, list[tuple[Any, ...]]],
    health: Mapping[str, Any],
    pending_repos: set[str],
) -> str:
    """Builds caller-isolated `_caller_*` temp tables and replacement personal views."""
    viewer_rows = [
        r
        for r in temp_rows.get("viewer_items", [])
        if not _item_key_is_pending(r[1], pending_repos)
    ]
    watch_rows = [
        r
        for r in temp_rows.get("watches", [])
        if not _item_key_is_pending(r[2], pending_repos)
    ]
    notif_rows = temp_rows.get("notifications", [])
    change_rows = temp_rows.get("changes", [])
    synced_map = health.get("viewer_sets_synced_at")
    synced_json = json.dumps(
        synced_map if isinstance(synced_map, Mapping) else {},
        separators=(",", ":"),
        sort_keys=True,
    )
    health_row = [
        (
            health.get("pid"),
            health.get("started_at"),
            health.get("beat_at"),
            health.get("version"),
            health.get("beat_age_s"),
            health.get("graphql_remaining"),
            health.get("graphql_reset_at"),
            health.get("core_remaining"),
            health.get("core_reset_at"),
            health.get("active_leases"),
            health.get("viewer_login"),
            health.get("last_ok_at"),
            health.get("consecutive_failed_ticks", 0),
            synced_json,
        )
    ]
    parts = [
        """
CREATE OR REPLACE VIEW v_api_calls AS
SELECT
  CAST(NULL AS VARCHAR) AS "at",
  CAST(NULL AS VARCHAR) AS worker,
  CAST(NULL AS VARCHAR) AS kind,
  CAST(NULL AS VARCHAR) AS target,
  CAST(NULL AS BIGINT) AS status,
  CAST(NULL AS BIGINT) AS cost,
  CAST(NULL AS BIGINT) AS remaining,
  CAST(NULL AS BIGINT) AS reset_at,
  CAST(NULL AS BIGINT) AS duration_ms,
  CAST(NULL AS VARCHAR) AS resource
WHERE FALSE;

CREATE TEMP TABLE _caller_viewer_items (
  set_name VARCHAR,
  item_key VARCHAR,
  first_seen_at VARCHAR,
  last_seen_at VARCHAR
);
CREATE TEMP TABLE _caller_watches (
  consumer VARCHAR,
  consumer_id VARCHAR,
  item_key VARCHAR,
  freshness_s BIGINT,
  lease_expires_at VARCHAR,
  active BOOLEAN
);
CREATE TEMP TABLE _caller_notifications (
  thread_id VARCHAR,
  reason VARCHAR,
  subject_type VARCHAR,
  item_key VARCHAR,
  updated_at VARCHAR,
  unread BIGINT,
  seen_at VARCHAR
);
CREATE TEMP TABLE _caller_changes (
  seq BIGINT,
  item_key VARCHAR,
  kind VARCHAR,
  fields VARCHAR,
  "at" VARCHAR
);
CREATE TEMP TABLE _caller_health (
  pid BIGINT,
  started_at VARCHAR,
  beat_at VARCHAR,
  version VARCHAR,
  beat_age_s BIGINT,
  graphql_remaining BIGINT,
  graphql_reset_at BIGINT,
  core_remaining BIGINT,
  core_reset_at BIGINT,
  active_leases BIGINT,
  viewer_login VARCHAR,
  last_ok_at VARCHAR,
  consecutive_failed_ticks BIGINT,
  viewer_sets_synced_json VARCHAR
);
""",
        _insert_values_sql("_caller_viewer_items", viewer_rows),
        _insert_values_sql("_caller_watches", watch_rows),
        _insert_values_sql("_caller_notifications", notif_rows),
        _insert_values_sql("_caller_changes", change_rows),
        _insert_values_sql("_caller_health", health_row),
        """
CREATE OR REPLACE VIEW v_viewer_items AS
SELECT set_name, item_key, first_seen_at, last_seen_at FROM _caller_viewer_items;

CREATE OR REPLACE VIEW v_watches AS
SELECT consumer, consumer_id, item_key, freshness_s, lease_expires_at, active
FROM _caller_watches;

CREATE OR REPLACE VIEW v_notifications AS
SELECT thread_id, reason, subject_type, item_key, updated_at, unread, seen_at
FROM _caller_notifications;

CREATE OR REPLACE VIEW v_changes AS
SELECT seq, item_key, kind, fields, "at" FROM _caller_changes;

CREATE OR REPLACE VIEW v_service_health AS
SELECT
  pid, started_at, beat_at, version, beat_age_s,
  graphql_remaining, graphql_reset_at, core_remaining, core_reset_at,
  active_leases, viewer_login, last_ok_at, consecutive_failed_ticks,
  viewer_sets_synced_json
FROM _caller_health;
""",
    ]
    return "".join(parts)


def _parse_duckdb_json(stdout: str) -> list[dict[str, Any]]:
    """Parses `duckdb -json` output into the final statement's row list."""
    rows: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    text = stdout.strip()
    idx = 0
    while idx < len(text):
        parsed, end = decoder.raw_decode(text, idx)
        if isinstance(parsed, list):
            rows = [item for item in parsed if isinstance(item, dict)]
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return rows


def _infer_column_type(rows: Sequence[Mapping[str, Any]], col: str) -> str:
    for row in rows:
        val = row.get(col)
        if val is None:
            continue
        if isinstance(val, bool):
            return "BOOLEAN"
        if isinstance(val, int):
            return "BIGINT"
        if isinstance(val, float):
            return "DOUBLE"
        if isinstance(val, (list, dict)):
            return "JSON"
        return "VARCHAR"
    return "VARCHAR"


def _sql_source_ref(row: Mapping[str, Any], col: str) -> str:
    for key in ("item_key", "source_ref", "repo_full_name", "repo", "full_name"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    return f"packrat:sql:{col}"


def _wrap_untrusted_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wrapped: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        for col, val in row.items():
            if isinstance(val, str) and col.lower() in UNTRUSTED_PROSE_COLUMNS:
                out[col] = wrap_untrusted_text(_sql_source_ref(row, col), val)
        wrapped.append(out)
    return wrapped


def _kill_process_group(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=2.0)


def _read_stream_chunk(
    sel: selectors.BaseSelector,
    fileobj: Any,
    chunks: list[bytes],
    cur_bytes: int,
    cap_bytes: int,
    *,
    enforce_cap: bool,
) -> tuple[int, bool]:
    data = os.read(fileobj.fileno(), 65536)
    if not data:
        sel.unregister(fileobj)
        return cur_bytes, False
    next_total = cur_bytes + len(data)
    if enforce_cap and next_total > cap_bytes:
        return next_total, True
    if cur_bytes < cap_bytes:
        chunks.append(data[: cap_bytes - cur_bytes] if not enforce_cap else data)
    return next_total, False


def _drain_duckdb_process(
    proc: subprocess.Popen[bytes],
    deadline: float,
    max_stdout_bytes: int,
) -> tuple[str, str]:
    """Drains `proc` stdout/stderr with byte-cap and process-group timeout enforcement."""
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_len = 0
    stderr_len = 0
    with selectors.DefaultSelector() as sel:
        assert proc.stdout is not None and proc.stderr is not None
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(proc)
                raise _ApiError(
                    504,
                    "query_timeout",
                    "SQL query exceeded execution timeout.",
                    retryable=False,
                )
            for key, _ in sel.select(timeout=min(0.2, remaining)):
                if key.data == "out":
                    stdout_len, exceeded = _read_stream_chunk(
                        sel,
                        key.fileobj,
                        stdout_chunks,
                        stdout_len,
                        max_stdout_bytes,
                        enforce_cap=True,
                    )
                    if exceeded:
                        _kill_process_group(proc)
                        raise _ApiError(
                            413,
                            "result_too_large",
                            "SQL query output exceeded the 16 MiB limit.",
                            retryable=False,
                        )
                else:
                    stderr_len, _ = _read_stream_chunk(
                        sel,
                        key.fileobj,
                        stderr_chunks,
                        stderr_len,
                        65536,
                        enforce_cap=False,
                    )
    try:
        proc.wait(timeout=max(0.05, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(proc)
        raise _ApiError(
            504,
            "query_timeout",
            "SQL query exceeded execution timeout.",
            retryable=False,
        ) from exc
    return (
        b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )


class DuckDbSqlSandbox:
    """Bounded-concurrency `:memory:` DuckDB runner isolated from `personal.sqlite`."""

    def __init__(
        self,
        db_path: Path,
        *,
        max_concurrent: int = DEFAULT_SQL_CONCURRENCY,
        timeout_s: float = DEFAULT_SQL_TIMEOUT_S,
        max_stdout_bytes: int = MAX_SQL_STDOUT_BYTES,
        max_rows: int = MAX_SQL_ROWS,
        duckdb_bin: str | None = None,
        hmac_secret_env: str = "PACKRAT_EDGE_HMAC_KEYS_JSON",
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.timeout_s = float(timeout_s)
        self.max_stdout_bytes = int(max_stdout_bytes)
        self.max_rows = int(max_rows)
        self.duckdb_bin = _resolve_duckdb_binary(duckdb_bin)
        self._hmac_secret_env = hmac_secret_env
        self._sem = threading.BoundedSemaphore(max(1, int(max_concurrent)))
        self._shared_views_sql = _strip_init_sql_template(
            _INIT_SQL_TEMPLATE_PATH.read_text(encoding="utf-8")
        )

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        for key in (*_AGENT_ENV_KEYS, self._hmac_secret_env):
            env.pop(key, None)
        env["HOME"] = str(_SERVER_STARTUP_HOME)
        env["DUCKDB_AGENT_MODE"] = "0"
        env["MY_IS_AGENT"] = "0"
        return env

    def _pending_delete_full_names(self, read_store: PackratStore) -> set[str]:
        pending_ids = read_store.pending_delete_repo_ids()
        if not pending_ids:
            return set()
        return {
            r.full_name.lower()
            for r in read_store.list_repos(include_pending_delete=True)
            if r.repo_id in pending_ids
        }

    def _write_setup_file(
        self,
        read_store: PackratStore,
        personal_store: PersonalStore,
        owner_id: str,
        viewer_login: str,
        moment: float,
    ) -> Path:
        temp_rows = personal_store.caller_temp_rows(
            owner_id=owner_id, viewer_login=viewer_login, now=moment
        )
        health = execute_reader_batch(
            read_store,
            {"health": True},
            personal_store=personal_store,
            owner_id=owner_id,
            viewer_login=viewer_login,
            now=moment,
        )["health"]
        pending_names = self._pending_delete_full_names(read_store)
        escaped_db = str(self.db_path).replace("'", "''")
        personal_sql = _build_personal_views_sql(temp_rows, health, pending_names)
        script = (
            f"LOAD sqlite;\n"
            f"ATTACH '{escaped_db}' AS kb (TYPE SQLITE, READ_ONLY);\n"
            f"{self._shared_views_sql}\n"
            f"{personal_sql}\n"
            "SET memory_limit = '1GB';\n"
            "SET threads = 2;\n"
            "SET enable_external_access = false;\n"
            "SET lock_configuration = true;\n"
        )
        fd, raw_path = tempfile.mkstemp(prefix="packrat_sql_", suffix=".sql")
        setup_path = Path(raw_path)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(script)
        except BaseException:
            setup_path.unlink(missing_ok=True)
            raise
        return setup_path

    def _spawn_and_collect(
        self, setup_path: Path, sql_text: str, deadline: float
    ) -> tuple[int, str, str]:
        argv = [
            self.duckdb_bin,
            "-batch",
            "-dark-mode",
            "-no-agent",
            "-no-stdin",
            "-json",
            ":memory:",
            "-init",
            str(setup_path),
            "-c",
            "-- user_sql\n" + sql_text,
        ]
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._child_env(),
                start_new_session=True,
            )
        except OSError as exc:
            raise _ApiError(
                503, "duckdb_unavailable", "DuckDB runtime is unavailable."
            ) from exc
        try:
            stdout, stderr = _drain_duckdb_process(
                proc, deadline, self.max_stdout_bytes
            )
        finally:
            if proc.poll() is None:
                _kill_process_group(proc)
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
        return int(proc.returncode or 0), stdout, stderr

    def _describe_empty_query(
        self, setup_path: Path, query: str, deadline: float
    ) -> list[SqlColumnMeta]:
        trimmed = query.strip().rstrip(";").strip()
        if not trimmed or (deadline - time.monotonic()) <= 0.1:
            return []
        try:
            rc, stdout, _ = self._spawn_and_collect(
                setup_path, f"DESCRIBE (\n{trimmed}\n)", deadline
            )
            if rc != 0:
                return []
            desc_rows = _parse_duckdb_json(stdout)
        except (_ApiError, json.JSONDecodeError, ValueError):
            return []
        return [
            SqlColumnMeta(
                name=str(item["column_name"]),
                type=str(item["column_type"]),
            )
            for item in desc_rows
            if "column_name" in item and "column_type" in item
        ]

    def run_query(
        self,
        query: str,
        *,
        read_store: PackratStore,
        personal_store: PersonalStore,
        owner_id: str,
        viewer_login: str,
        raw: bool = False,
        limit_rows: int | None = None,
        now: float | None = None,
    ) -> SqlResponseEnvelope:
        """Runs `query` inside a fresh locked-down `:memory:` DuckDB subprocess."""
        row_cap = (
            min(max(1, int(limit_rows)), self.max_rows)
            if limit_rows is not None
            else self.max_rows
        )
        moment = time.time() if now is None else now
        with self._sem:
            setup_path = self._write_setup_file(
                read_store, personal_store, owner_id, viewer_login, moment
            )
            try:
                deadline = time.monotonic() + self.timeout_s
                rc, stdout, stderr = self._spawn_and_collect(
                    setup_path, query, deadline
                )
                if rc != 0:
                    msg = _sanitize_error_text(
                        stderr, redact_paths=(str(setup_path), str(self.db_path))
                    )
                    raise _ApiError(400, "sql_error", msg, retryable=False)
                try:
                    rows = _parse_duckdb_json(stdout)
                except json.JSONDecodeError as exc:
                    raise _ApiError(
                        500, "sql_parse_error", "Failed to decode DuckDB JSON output."
                    ) from exc
                if len(rows) > row_cap:
                    raise _ApiError(
                        413,
                        "too_many_rows",
                        f"SQL query returned {len(rows)} rows (limit {row_cap}).",
                        retryable=False,
                    )
                if rows:
                    columns = [
                        SqlColumnMeta(name=col, type=_infer_column_type(rows, col))
                        for col in rows[0]
                    ]
                else:
                    columns = self._describe_empty_query(setup_path, query, deadline)
            finally:
                setup_path.unlink(missing_ok=True)
        final_rows = rows if raw else _wrap_untrusted_rows(rows)
        return SqlResponseEnvelope(rows=final_rows, columns=columns)


# ----- 4. Repository Serialization & Helpers -------------------------------------


def _read_git_poll_dict(store: PackratStore, repo_id: int) -> dict[str, Any]:
    raw = store.get_state(f"git_poll:{int(repo_id)}")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _classify_clone_status(
    repo: RepoConfig, disk: Mapping[str, Any], poll: Mapping[str, Any], now: float
) -> str:
    if repo.git_source == "none":
        return "none"
    if repo.git_source == "path":
        return "cloned" if repo.last_indexed_sha else "pending"
    if disk.get("cloning"):
        return "cloning"
    last_err = str(poll.get("last_error") or "")
    backoff = float(poll.get("backoff_until") or 0.0)
    if last_err and (not disk.get("exists") or backoff > now):
        return "error"
    if not disk.get("exists") or not disk.get("local_tip"):
        return "pending"
    return "cloned"


def _serialize_repo_entry(
    store: PackratStore, repo: RepoConfig, now: float
) -> dict[str, Any]:
    """Serializes one `RepoConfig` for `/api/v1/repos` without `git_url` or `local_path`."""
    root = store.git_root
    disk = (
        git_status(root, repo)
        if (repo.git_source == "managed" and root is not None)
        else {"exists": False, "cloning": False, "local_tip": "", "size_bytes": 0}
    )
    poll = (
        _read_git_poll_dict(store, repo.repo_id) if repo.git_source == "managed" else {}
    )
    raw_err = str(poll.get("last_error") or "")
    clean_poll = {
        "checked_at": poll.get("checked_at"),
        "fetched_at": poll.get("fetched_at"),
        "remote_sha": poll.get("remote_sha"),
        "backoff_until": poll.get("backoff_until"),
        "last_error": _sanitize_error_text(raw_err) if raw_err else None,
    }
    backfill = {
        stream: {
            "complete": repo.backfill(stream).complete,
            "done_at": repo.backfill(stream).done_at,
            "cursor": repo.backfill(stream).cursor,
            "resumable": repo.backfill(stream).cursor is not None,
        }
        for stream in BACKFILL_STREAMS
    }
    git_source_out = "none" if repo.git_source == "none" else "managed"
    return {
        "repo_id": repo.repo_id,
        "owner": repo.owner,
        "name": repo.name,
        "full_name": repo.full_name,
        "mode": repo.mode,
        "default_branch": repo.default_branch,
        "git_source": git_source_out,
        "clone_status": _classify_clone_status(repo, disk, poll, now),
        "last_indexed_sha": repo.last_indexed_sha,
        "local_tip": disk.get("local_tip") or None,
        "size_bytes": int(disk.get("size_bytes") or 0),
        "git_poll": clean_poll,
        "issues_watermark": repo.issues_watermark,
        "prs_watermark": repo.prs_watermark,
        "discussions_watermark": repo.discussions_watermark,
        "last_full_sweep_at": repo.last_full_sweep_at,
        "created_at": repo.created_at,
        "updated_at": repo.updated_at,
        "counts": store.table_counts(repo.repo_id),
        "backfill": backfill,
        "warnings": _backfill_warnings_for_repo(repo),
    }


def _parse_bearer_header(auth_header: str | None) -> str | None:
    if auth_header is None or not auth_header.strip():
        return None
    scheme, sep, token = auth_header.strip().partition(" ")
    if not sep or scheme.lower() != "bearer" or not token.strip():
        raise _ApiError(
            401, "unauthorized", "Authorization header must use Bearer scheme."
        )
    return token.strip()


def _parse_iso_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    candidate = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        from datetime import datetime

        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        return None


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    norm_etag = etag.strip().removeprefix("W/").strip()
    for token in if_none_match.split(","):
        cleaned = token.strip()
        if cleaned == "*":
            return True
        if cleaned.removeprefix("W/").strip() == norm_etag:
            return True
    return False


def _search_warnings(
    store: PackratStore, repo_filter: str | None, has_hits: bool
) -> list[str]:
    repos = store.list_repos("mirror")
    if repo_filter:
        repos = [r for r in repos if r.full_name.lower() == repo_filter.lower()]
    if not has_hits and not repos:
        return ["No mirror repositories matched."]
    incomplete = [
        r.full_name
        for r in repos
        if any(not r.backfill(s).complete for s in BACKFILL_STREAMS)
    ]
    if incomplete:
        return [f"Backfill incomplete for: {', '.join(incomplete)}"]
    return []


def _parse_repo_slug_pair(full_name: str) -> tuple[str, str]:
    raw = (full_name or "").strip()
    owner, sep, name = raw.partition("/")
    if not sep or "/" in name:
        raise _ApiError(
            400,
            "invalid_repo",
            f"Invalid repository name {raw!r}; expected owner/name.",
        )
    try:
        return validate_slug(owner, "owner"), validate_slug(name, "name")
    except ValueError as exc:
        raise _ApiError(400, "invalid_repo", str(exc)) from exc


# ----- 5. HTTP Server & Route Dispatch -------------------------------------------


def _resolve_unverified_identity(
    personal_store: PersonalStore,
    owner_id: str,
    headers: Mapping[str, str],
    now: float,
) -> tuple[str, bool]:
    ident = personal_store.get_identity(owner_id)
    raw_gh_user = (headers.get("X-Packrat-Github-User") or "").strip()
    if raw_gh_user and (ident is None or not ident.login_verified):
        with contextlib.suppress(ValueError, IdentityConflictError):
            norm_login = normalize_github_login(raw_gh_user)
            if ident is None or ident.github_login != norm_login:
                ident = personal_store.link_identity(
                    owner_id, norm_login, verified=False, now=now
                )
    if ident is None:
        return "", False
    last_ts = _parse_iso_ts(ident.last_active_at)
    if last_ts is not None and (now - last_ts) >= _IDENTITY_TOUCH_INTERVAL_S:
        personal_store.touch_identity(owner_id, now=now)
    return ident.github_login, ident.login_verified


class _PackratRequestHandler(BaseHTTPRequestHandler):
    """HTTP/1.1 handler delegating `/api/v1/*` routes to `PackratHttpServer`."""

    protocol_version = "HTTP/1.1"
    server: PackratHttpServer

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.server.access_log is not None:
            self.server.access_log(fmt % args)

    def _write_security_headers(self, extra: Mapping[str, str] | None = None) -> None:
        for key, val in SECURITY_HEADERS:
            self.send_header(key, val)
        if extra:
            for key, val in extra.items():
                self.send_header(key, val)

    def _send_json(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._write_security_headers(extra_headers)
        self.end_headers()
        self.wfile.write(body)

    def _send_not_modified(self, etag: str) -> None:
        self.send_response(304)
        self.send_header("ETag", etag)
        self._write_security_headers()
        self.end_headers()

    def _send_api_error(self, err: _ApiError) -> None:
        self._send_json(
            err.status,
            {
                "error": {
                    "code": err.code,
                    "message": err.message,
                    "retryable": err.retryable,
                }
            },
        )

    def _parse_body_length(self, *, allow_empty: bool) -> int:
        if "Transfer-Encoding" in self.headers:
            self.close_connection = True
            raise _ApiError(
                400, "bad_request", "Chunked transfer encoding is not supported."
            )
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            if allow_empty:
                return 0
            self.close_connection = True
            raise _ApiError(400, "bad_request", "Missing Content-Length header.")
        try:
            length = int(raw_len)
        except ValueError as exc:
            self.close_connection = True
            raise _ApiError(
                400, "bad_request", "Invalid Content-Length header."
            ) from exc
        if length < 0:
            self.close_connection = True
            raise _ApiError(400, "bad_request", "Negative Content-Length header.")
        if length > MAX_REQUEST_BODY_BYTES:
            self.close_connection = True
            raise _ApiError(
                413, "payload_too_large", "Request body exceeds 1 MiB limit."
            )
        return length

    def _read_json_body(self, *, allow_empty: bool = False) -> dict[str, Any]:
        length = self._parse_body_length(allow_empty=allow_empty)
        if length == 0:
            return {} if allow_empty else _raise_empty_json()
        raw_bytes = self.rfile.read(length)
        try:
            decoded = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise _ApiError(
                400, "invalid_json", "Malformed JSON request body."
            ) from exc
        if not isinstance(decoded, dict):
            raise _ApiError(400, "invalid_json", "Request body must be a JSON object.")
        return decoded

    def _dispatch(self, method: str) -> None:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            try:
                self.server.dispatch_request(self, method)
            except _ApiError as err:
                self._send_api_error(err)
            except TooManyWaitersError as exc:
                self._send_api_error(
                    _ApiError(429, "too_many_waiters", str(exc), retryable=True)
                )
            except PendingDeleteError as exc:
                self._send_api_error(
                    _ApiError(409, "delete_pending", str(exc), retryable=True)
                )
            except IdentityConflictError as exc:
                self._send_api_error(
                    _ApiError(409, "identity_conflict", str(exc), retryable=False)
                )
            except sqlite3.OperationalError as exc:
                self._send_api_error(_map_sqlite_error(exc))
            except Exception:  # noqa: BLE001
                self._send_api_error(
                    _ApiError(500, "internal_error", "Internal server error.")
                )

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")


def _raise_empty_json() -> dict[str, Any]:
    raise _ApiError(400, "invalid_json", "Expected non-empty JSON request body.")


def _map_sqlite_error(exc: sqlite3.OperationalError) -> _ApiError:
    lowered = str(exc).lower()
    if "locked" in lowered or "busy" in lowered:
        return _ApiError(
            503,
            "database_locked",
            "Database is temporarily busy; retry shortly.",
            retryable=True,
        )
    return _ApiError(500, "database_error", "Database operation failed.")


def _int_param(
    params: Mapping[str, list[str]],
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    vals = params.get(name)
    if not vals or vals[0] == "":
        return default
    try:
        parsed = int(vals[0])
    except ValueError as exc:
        raise _ApiError(
            400, "invalid_request", f"Query parameter {name!r} must be an integer."
        ) from exc
    if parsed < minimum:
        raise _ApiError(
            400, "invalid_request", f"Query parameter {name!r} must be >= {minimum}."
        )
    return min(parsed, maximum) if maximum is not None else parsed


def _float_param(
    params: Mapping[str, list[str]],
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = MAX_CHANGES_WAIT_S,
) -> float:
    vals = params.get(name)
    if not vals or vals[0] == "":
        return default
    try:
        parsed = float(vals[0])
    except ValueError as exc:
        raise _ApiError(
            400, "invalid_request", f"Query parameter {name!r} must be numeric."
        ) from exc
    if not math.isfinite(parsed) or parsed < minimum:
        raise _ApiError(
            400, "invalid_request", f"Query parameter {name!r} must be >= {minimum}."
        )
    return min(parsed, maximum)


class PackratHttpServer(ThreadingHTTPServer):
    """Multi-threaded HTTP v1 server backed by read-only `PackratStore` + `PersonalStore`."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        listen_address: tuple[str, int] | str,
        db_path: Path,
        personal_db_path: Path | None = None,
        *,
        server_config: ServerConfig | None = None,
        trust_edge_proxy: bool = False,
        hmac_keys: Mapping[str, str | bytes] | None = None,
        notifier: ChangeNotifier | None = None,
        token_registry: TokenRegistry | None = None,
        token_verifier: Callable[[str], str | tuple[str, Any]] | None = None,
        public_repo_checker: Callable[[str, str, Any], bool] | None = None,
        embedder: Embedder | None = None,
        export_dir: Path | None = None,
        export_cache_ttl_s: float = DEFAULT_EXPORT_CACHE_TTL_S,
        sql_sandbox: DuckDbSqlSandbox | None = None,
        disk_stats_fn: Callable[[Path], tuple[int, float]] = disk_usage_stats,
        stop_event: threading.Event | None = None,
        access_log: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.personal_db_path = (
            Path(personal_db_path).expanduser().resolve()
            if personal_db_path is not None
            else (self.db_path.parent / "personal.sqlite")
        )
        self.server_config = server_config or ServerConfig()
        self.notifier = notifier if notifier is not None else ChangeNotifier()
        self.stop_event = stop_event if stop_event is not None else threading.Event()
        self.access_log = access_log
        self._clock = clock
        self._disk_stats_fn = disk_stats_fn
        self.embedder = embedder
        self.export_dir = (
            Path(export_dir).expanduser().resolve()
            if export_dir is not None
            else (self.db_path.parent / "exports")
        )
        self.export_cache_ttl_s = float(export_cache_ttl_s)
        self._public_repo_checker = public_repo_checker
        self._ensure_databases_initialized()

        self.edge_auth = EdgeAuthenticator(
            trust_edge_proxy=trust_edge_proxy,
            hmac_keys=hmac_keys,
            hmac_secret_env=self.server_config.hmac_secret_env,
            clock=clock,
        )
        self.token_registry = (
            token_registry
            if token_registry is not None
            else TokenRegistry(
                self.personal_db_path.parent / "gh_empty_config",
                verifier=token_verifier,
                clock=clock,
            )
        )
        self.sql_sandbox = (
            sql_sandbox
            if sql_sandbox is not None
            else DuckDbSqlSandbox(
                self.db_path, hmac_secret_env=self.server_config.hmac_secret_env
            )
        )
        self._tls = threading.local()
        self._stores_lock = threading.Lock()
        self._open_stores: list[PackratStore | PersonalStore] = []
        self._repo_write_lock = threading.Lock()
        self._export_locks_guard = threading.Lock()
        self._export_locks: dict[str, threading.Lock] = {}
        self._shutting_down = False
        super().__init__(parse_listen_address(listen_address), _PackratRequestHandler)

    def _ensure_databases_initialized(self) -> None:
        if not self.db_path.is_file():
            PackratStore(self.db_path).close()
        if not self.personal_db_path.is_file():
            PersonalStore(self.personal_db_path, notifier=self.notifier).close()

    @property
    def base_url(self) -> str:
        """Returns the loopback/bound `http://host:port` base URL."""
        host = str(self.server_address[0])
        port = int(self.server_address[1])
        fmt_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
        return f"http://{fmt_host}:{port}"

    # ----- Thread-local store lifecycle ------------------------------------------

    def _thread_stores(self) -> tuple[PackratStore, PersonalStore]:
        pair = getattr(self._tls, "stores", None)
        if pair is not None:
            return pair
        read_store = PackratStore(self.db_path, read_only=True)
        try:
            personal_store = PersonalStore(
                self.personal_db_path, notifier=self.notifier
            )
        except BaseException:
            read_store.close()
            raise
        with self._stores_lock:
            self._open_stores.extend((read_store, personal_store))
        self._tls.stores = (read_store, personal_store)
        return read_store, personal_store

    def _close_current_thread_stores(self) -> None:
        pair: tuple[PackratStore, PersonalStore] | None = getattr(
            self._tls, "stores", None
        )
        if pair is None:
            return
        self._tls.stores = None
        read_store, personal_store = pair
        with self._stores_lock:
            for item in (read_store, personal_store):
                if item in self._open_stores:
                    self._open_stores.remove(item)
        with contextlib.suppress(sqlite3.Error):
            read_store.close()
        with contextlib.suppress(sqlite3.Error):
            personal_store.close()

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._close_current_thread_stores()

    def server_close(self) -> None:
        self._shutting_down = True
        self.stop_event.set()
        self.notifier.notify_all()
        super().server_close()
        self._close_current_thread_stores()
        with self._stores_lock:
            remaining = list(self._open_stores)
            self._open_stores.clear()
        for store in remaining:
            with contextlib.suppress(sqlite3.ProgrammingError, sqlite3.Error):
                store.close()

    def _with_writable_repo_store(
        self, fn: Callable[[PackratStore], Any], personal_store: PersonalStore
    ) -> Any:
        try:
            with self._repo_write_lock:
                store = PackratStore(self.db_path, personal_store=personal_store)
                try:
                    return fn(store)
                finally:
                    store.close()
        except sqlite3.OperationalError as exc:
            raise _map_sqlite_error(exc) from exc

    # ----- Request authentication & visibility checks ----------------------------

    def _build_context(
        self, method: str, raw_path: str, headers: Mapping[str, str]
    ) -> _RequestContext:
        now = self._clock()
        owner_id = self.edge_auth.authenticate(method, raw_path, headers)
        read_store, personal_store = self._thread_stores()
        bearer = _parse_bearer_header(headers.get("Authorization"))
        if bearer is not None:
            cached = self.token_registry.authenticate(
                owner_id, bearer, personal_store, now=now
            )
            return _RequestContext(
                owner_id=owner_id,
                viewer_login=cached.github_login,
                login_verified=True,
                has_bearer=True,
                read_store=read_store,
                personal_store=personal_store,
                now=now,
            )
        viewer_login, login_verified = _resolve_unverified_identity(
            personal_store, owner_id, headers, now
        )
        return _RequestContext(
            owner_id=owner_id,
            viewer_login=viewer_login,
            login_verified=login_verified,
            has_bearer=False,
            read_store=read_store,
            personal_store=personal_store,
            now=now,
        )

    def _is_admin(self, owner_id: str) -> bool:
        return owner_id.lower() in {u.lower() for u in self.server_config.admin_users}

    def _check_public_repo(self, owner: str, name: str, owner_id: str) -> bool:
        transport = self.token_registry.transport_for_owner(owner_id)
        if self._public_repo_checker is not None:
            return bool(self._public_repo_checker(owner, name, transport))
        active_tx = transport
        if active_tx is None:
            budget = RateBudget(self.db_path.parent / RATE_BUDGET_FILENAME)
            active_tx = GhTransport(budget, NullApiCallStore())
        try:
            resp = active_tx.rest_get(
                f"repos/{owner}/{name}", worker="http_visibility", priority=Priority.HOT
            )
            data = json.loads(resp.body)
        except RateLimited as exc:
            raise _ApiError(
                429,
                "rate_limited",
                "GitHub rate limit reached while verifying repository visibility.",
                retryable=True,
            ) from exc
        except (GhApiError, BudgetDeferred, json.JSONDecodeError, OSError):
            return False
        return isinstance(data, Mapping) and data.get("private") is False

    def _require_public_repo(self, owner: str, name: str, owner_id: str) -> None:
        if not self._check_public_repo(owner, name, owner_id):
            raise _ApiError(
                403,
                "private_repository",
                f"Repository '{owner}/{name}' is private or inaccessible.",
            )

    def _ensure_public_item_repos(
        self, ctx: _RequestContext, raw_keys: Sequence[str]
    ) -> list[str]:
        """Validates `raw_keys` and checks visibility BEFORE registering unknown `items` repos."""
        parsed_keys: list[tuple[str, str, int]] = []
        missing_repos: list[tuple[str, str]] = []
        seen_slugs: set[str] = set()
        pending_ids = ctx.read_store.pending_delete_repo_ids()

        for raw in raw_keys:
            try:
                owner, name, number = parse_item_key(str(raw))
                validate_slug(owner, "owner")
                validate_slug(name, "name")
            except ValueError as exc:
                raise _ApiError(400, "invalid_item_key", str(exc)) from exc
            parsed_keys.append((owner, name, number))
            slug_low = f"{owner}/{name}".lower()
            if slug_low in seen_slugs:
                continue
            seen_slugs.add(slug_low)
            existing = ctx.read_store.get_repo(
                f"{owner}/{name}", include_pending_delete=True
            )
            if existing is not None:
                if existing.repo_id in pending_ids:
                    raise _ApiError(
                        409,
                        "delete_pending",
                        f"Repository '{existing.full_name}' is pending deletion.",
                        retryable=True,
                    )
                continue
            self._require_public_repo(owner, name, ctx.owner_id)
            missing_repos.append((owner, name))

        if missing_repos:
            self._with_writable_repo_store(
                lambda wstore: [
                    wstore.ensure_items_repo(o, n, now=ctx.now)
                    for o, n in missing_repos
                ],
                ctx.personal_store,
            )
        return [
            ctx.read_store.canonical_item_key(f"{o}/{n}#{num}")
            for o, n, num in parsed_keys
        ]

    # ----- Top-level route dispatcher --------------------------------------------

    def dispatch_request(self, handler: _PackratRequestHandler, method: str) -> None:
        split = urllib.parse.urlsplit(handler.path)
        route = split.path.rstrip("/") or "/"
        params = urllib.parse.parse_qs(split.query, keep_blank_values=False)
        ctx = self._build_context(method, handler.path, handler.headers)

        if method == "GET":
            self._dispatch_get(handler, route, params, ctx)
            return
        if method == "POST":
            self._dispatch_post(handler, route, ctx)
            return
        if method == "DELETE" and route.startswith("/api/v1/repos/"):
            rest = route.removeprefix("/api/v1/repos/")
            owner_raw, sep, name_raw = rest.partition("/")
            if not sep or "/" in name_raw:
                raise _ApiError(404, "not_found", f"Unknown route {route!r}.")
            owner, name = _parse_repo_slug_pair(
                f"{urllib.parse.unquote(owner_raw)}/{urllib.parse.unquote(name_raw)}"
            )
            handler._send_json(202, self._handle_delete_repo(ctx, owner, name))
            return
        raise _ApiError(404, "not_found", f"Unknown route {method} {route}.")

    def _dispatch_get(
        self,
        handler: _PackratRequestHandler,
        route: str,
        params: Mapping[str, list[str]],
        ctx: _RequestContext,
    ) -> None:
        if route == "/api/v1/health":
            handler._send_json(200, self._handle_health(ctx))
            return
        if route == "/api/v1/repos":
            handler._send_json(200, self._handle_list_repos(ctx, params))
            return
        if route == "/api/v1/changes":
            handler._send_json(200, self._handle_changes(ctx, params))
            return
        if route == "/api/v1/export":
            self._stream_export(handler, ctx, params)
            return
        raise _ApiError(404, "not_found", f"Unknown route GET {route}.")

    def _dispatch_post(
        self,
        handler: _PackratRequestHandler,
        route: str,
        ctx: _RequestContext,
    ) -> None:
        if route == "/api/v1/auth/link":
            body = handler._read_json_body(allow_empty=True)
            handler._send_json(200, self._handle_auth_link(ctx, body))
            return
        post_handlers: dict[
            str, Callable[[_RequestContext, Mapping[str, Any]], Mapping[str, Any]]
        ] = {
            "/api/v1/repos": self._handle_add_repo,
            "/api/v1/search": self._handle_search,
            "/api/v1/sql": self._handle_sql,
            "/api/v1/watches": self._handle_watches,
            "/api/v1/refresh": self._handle_refresh,
            "/api/v1/reader/batch": self._handle_reader_batch,
        }
        target = post_handlers.get(route)
        if target is None:
            raise _ApiError(404, "not_found", f"Unknown route POST {route}.")
        body = handler._read_json_body(allow_empty=False)
        handler._send_json(200, target(ctx, body))

    # ----- Individual route implementations --------------------------------------

    def _handle_health(self, ctx: _RequestContext) -> dict[str, Any]:
        health = execute_reader_batch(
            ctx.read_store,
            {"health": True},
            personal_store=ctx.personal_store,
            owner_id=ctx.owner_id,
            viewer_login=ctx.viewer_login,
            now=ctx.now,
        )["health"]
        schema_ver, views_ver = ctx.read_store.read_schema_and_views_version()
        free_bytes, used_pct = self._disk_stats_fn(self.db_path)
        budget = RateBudget(self.db_path.parent / RATE_BUDGET_FILENAME)
        rate_payload = {
            name: {**asdict(snap), "expired": snap.expired(ctx.now)}
            for name, snap in sorted(budget.last_snapshots().items())
        }
        return {
            **health,
            "status": "ok",
            "schema_version": schema_ver,
            "views_version": views_ver,
            "free_bytes": free_bytes,
            "used_pct": used_pct,
            "disk": {
                "free_bytes": free_bytes,
                "used_pct": used_pct,
                "high_water_pct": self.server_config.disk_high_water_pct,
            },
            "rate_budget": rate_payload,
            "owner_id": ctx.owner_id,
            "login_verified": ctx.login_verified,
        }

    def _link_verified_bearer_identity(
        self, ctx: _RequestContext, raw_login: str
    ) -> Any:
        if raw_login:
            try:
                requested = normalize_github_login(raw_login)
            except ValueError as exc:
                raise _ApiError(400, "invalid_login", str(exc)) from exc
            if requested != ctx.viewer_login:
                raise _ApiError(
                    400,
                    "login_mismatch",
                    f"Requested login {requested!r} does not match bearer token user {ctx.viewer_login!r}.",
                )
        return ctx.personal_store.link_identity(
            ctx.owner_id, ctx.viewer_login, verified=True, now=ctx.now
        )

    def _link_unverified_identity(self, ctx: _RequestContext, raw_login: str) -> Any:
        if not raw_login:
            raise _ApiError(
                400,
                "invalid_request",
                "Provide github_login or an Authorization: Bearer token.",
            )
        try:
            return ctx.personal_store.link_identity(
                ctx.owner_id, raw_login, verified=False, now=ctx.now
            )
        except IdentityConflictError:
            raise
        except ValueError as exc:
            raise _ApiError(400, "invalid_login", str(exc)) from exc

    def _handle_auth_link(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        raw_login = str(body.get("github_login") or "").strip()
        ident = (
            self._link_verified_bearer_identity(ctx, raw_login)
            if ctx.has_bearer
            else self._link_unverified_identity(ctx, raw_login)
        )
        return {
            "owner_id": ident.owner_id,
            "github_login": ident.github_login,
            "login_verified": ident.login_verified,
            "last_active_at": ident.last_active_at,
            "updated_at": ident.updated_at,
        }

    def _handle_list_repos(
        self, ctx: _RequestContext, params: Mapping[str, list[str]]
    ) -> dict[str, Any]:
        mode_vals = params.get("mode")
        mode = mode_vals[0].strip() if mode_vals else None
        if mode not in (None, "", "mirror", "items"):
            raise _ApiError(
                400, "invalid_request", f"Unsupported repository mode {mode!r}."
            )
        repos = ctx.read_store.list_repos(mode=mode or None)
        return {
            "repos": [
                _serialize_repo_entry(ctx.read_store, repo, ctx.now) for repo in repos
            ]
        }

    def _check_add_repo_acl(
        self, ctx: _RequestContext, owner: str, body: Mapping[str, Any]
    ) -> None:
        is_admin = self._is_admin(ctx.owner_id)
        if body.get("local_path") is not None:
            raise _ApiError(
                403, "forbidden", "local_path is not permitted on server repositories."
            )
        if body.get("git_url") is not None and not is_admin:
            raise _ApiError(
                403, "forbidden", "Only admin users may specify a custom git_url."
            )
        if not is_admin and owner.lower() not in {
            o.lower() for o in self.server_config.allowlisted_orgs
        }:
            raise _ApiError(
                403,
                "forbidden",
                f"Organization '{owner}' is not in allowlisted_orgs.",
            )

    def _validate_add_repo_inputs(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> tuple[str, str, str, str, str | None]:
        owner, name = _parse_repo_slug_pair(str(body.get("full_name") or ""))
        self._check_add_repo_acl(ctx, owner, body)
        raw_git_url = body.get("git_url")
        validated_url: str | None = None
        if raw_git_url is not None and str(raw_git_url).strip():
            try:
                validated_url = validate_url(
                    str(raw_git_url).strip(),
                    allowed_schemes=self.server_config.allowed_git_protocols,
                )
            except ValueError as exc:
                raise _ApiError(400, "invalid_git_url", str(exc)) from exc
        src = str(
            body.get("git_source") or self.server_config.default_git_source
        ).strip()
        if src not in ("managed", "none"):
            raise _ApiError(
                400, "invalid_git_source", f"Unsupported git_source {src!r}."
            )
        raw_branch = str(body.get("default_branch") or body.get("branch") or "").strip()
        try:
            branch = validate_branch(raw_branch) if raw_branch else "main"
        except ValueError as exc:
            raise _ApiError(400, "invalid_branch", str(exc)) from exc
        return owner, name, branch, src, validated_url

    def _handle_add_repo(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        owner, name, branch, git_source, git_url = self._validate_add_repo_inputs(
            ctx, body
        )
        full_name = f"{owner}/{name}"
        existing = ctx.read_store.get_repo(full_name, include_pending_delete=True)
        if (
            existing is not None
            and existing.repo_id in ctx.read_store.pending_delete_repo_ids()
        ):
            raise _ApiError(
                409,
                "delete_pending",
                f"Repository '{existing.full_name}' is pending deletion.",
                retryable=True,
            )
        if existing is None or not existing.is_mirror:
            _, used_pct = self._disk_stats_fn(self.db_path)
            if used_pct >= self.server_config.disk_high_water_pct:
                raise _ApiError(
                    503,
                    "disk_high_water",
                    f"Server disk usage ({used_pct}%) exceeds high-water threshold ({self.server_config.disk_high_water_pct}%).",
                    retryable=False,
                )
        self._require_public_repo(owner, name, ctx.owner_id)
        repo = self._with_writable_repo_store(
            lambda wstore: wstore.register_repo(
                owner,
                name,
                local_path=None,
                default_branch=branch,
                git_source=git_source,  # type: ignore[arg-type]
                git_url=git_url,
                update_binding=True,
                now=ctx.now,
            ),
            ctx.personal_store,
        )
        return {"repo": _serialize_repo_entry(ctx.read_store, repo, ctx.now)}

    def _handle_delete_repo(
        self, ctx: _RequestContext, owner: str, name: str
    ) -> dict[str, Any]:
        if not self._is_admin(ctx.owner_id):
            raise _ApiError(
                403, "forbidden", "Repository deletion requires admin privileges."
            )
        full_name = f"{owner}/{name}"

        def _mark(wstore: PackratStore) -> RepoConfig:
            found = wstore.get_repo(full_name, include_pending_delete=True)
            if found is None:
                raise _ApiError(
                    404, "not_found", f"Repository '{full_name}' is not registered."
                )
            wstore.mark_repo_pending_delete(found.repo_id, now=ctx.now)
            return found

        repo = self._with_writable_repo_store(_mark, ctx.personal_store)
        return {
            "deleted": repo.full_name,
            "repo_id": repo.repo_id,
            "status": "pending_delete",
        }

    def _handle_search(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> SearchResponseEnvelope:
        query, mode, limit, filters = _parse_search_body(body)
        hits = hybrid_rrf_search(
            ctx.read_store,
            query=query,
            filters=filters,
            mode=mode,  # type: ignore[arg-type]
            limit=limit,
            embedder=self.embedder,
            allow_cache_rebuild=False,
        )
        return SearchResponseEnvelope(
            hits=[asdict(h) for h in hits],
            repo_watermarks=repo_watermarks(ctx.read_store),
            warnings=_search_warnings(ctx.read_store, filters.repo, bool(hits)),
        )

    def _handle_sql(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> SqlResponseEnvelope:
        raw_query = body.get("query")
        if not isinstance(raw_query, str) or not raw_query.strip():
            raise _ApiError(
                400, "invalid_request", "SQL query must be a non-empty string."
            )
        limit_rows_raw = body.get("limit_rows")
        limit_rows: int | None = None
        if limit_rows_raw is not None:
            try:
                limit_rows = int(limit_rows_raw)
            except (TypeError, ValueError) as exc:
                raise _ApiError(
                    400, "invalid_request", "limit_rows must be an integer."
                ) from exc
            if limit_rows < 1:
                raise _ApiError(400, "invalid_request", "limit_rows must be >= 1.")
        return self.sql_sandbox.run_query(
            raw_query,
            read_store=ctx.read_store,
            personal_store=ctx.personal_store,
            owner_id=ctx.owner_id,
            viewer_login=ctx.viewer_login,
            raw=bool(body.get("raw", False)),
            limit_rows=limit_rows,
            now=ctx.now,
        )

    def _handle_watches(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        op = str(body.get("op") or body.get("action") or "").strip()
        consumer = str(body.get("consumer") or "").strip()
        consumer_id = str(body.get("consumer_id") or body.get("id") or "").strip()
        if op not in ("add", "renew", "remove") or not consumer or not consumer_id:
            raise _ApiError(
                400,
                "invalid_request",
                "Watch request requires op ('add'|'renew'|'remove'), consumer, and consumer_id.",
            )
        raw_keys = body.get("keys") if "keys" in body else body.get("items")
        if op == "add":
            if not isinstance(raw_keys, list) or not raw_keys:
                raise _ApiError(
                    400, "invalid_request", "watch add requires a non-empty keys list."
                )
            freshness_s = _positive_int_field(
                body, "freshness_s", "freshness", default=120
            )
            ttl_s = _positive_int_field(body, "ttl_s", "ttl", default=900)
            canon_keys = self._ensure_public_item_repos(ctx, [str(k) for k in raw_keys])
            leased = ctx.personal_store.add_watch(
                ctx.owner_id,
                consumer,
                consumer_id,
                canon_keys,
                freshness_s,
                ttl_s,
                now=ctx.now,
            )
            return {
                "leased": leased,
                "expires_at": round(ctx.now + ttl_s, 3),
                "ttl_s": ttl_s,
                "freshness_s": freshness_s,
            }
        opt_keys = _optional_canonical_keys(ctx.read_store, raw_keys)
        if op == "renew":
            ttl_s = _positive_int_field(body, "ttl_s", "ttl", default=900)
            renewed = ctx.personal_store.renew_watch(
                ctx.owner_id,
                consumer,
                consumer_id,
                ttl_s,
                item_keys=opt_keys,
                now=ctx.now,
            )
            return {"renewed": renewed, "ttl_s": ttl_s}
        removed = ctx.personal_store.remove_watch(
            ctx.owner_id, consumer, consumer_id, item_keys=opt_keys
        )
        return {"removed": removed}

    def _handle_refresh(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        raw_keys = body.get("keys") if "keys" in body else body.get("items")
        if not isinstance(raw_keys, list) or not raw_keys:
            raise _ApiError(
                400, "invalid_request", "refresh requires a non-empty keys list."
            )
        if len(raw_keys) > MAX_REFRESH_KEYS:
            raise _ApiError(
                400,
                "too_many_keys",
                f"refresh accepts at most {MAX_REFRESH_KEYS} item keys per request.",
            )
        canon_keys = self._ensure_public_item_repos(ctx, [str(k) for k in raw_keys])
        queued = ctx.personal_store.request_refresh(
            ctx.owner_id, canon_keys, now=ctx.now
        )
        return {"queued": queued, "requested": queued}

    def _handle_changes(
        self, ctx: _RequestContext, params: Mapping[str, list[str]]
    ) -> dict[str, Any]:
        since = _int_param(params, "since", 0, minimum=0)
        limit = _int_param(
            params, "limit", MAX_CHANGES_LIMIT, minimum=1, maximum=MAX_CHANGES_LIMIT
        )
        wait_s = _float_param(
            params, "wait", 0.0, minimum=0.0, maximum=MAX_CHANGES_WAIT_S
        )
        max_seq = ctx.personal_store.max_change_seq()
        min_seq = ctx.personal_store.min_change_seq()
        if since < min_seq or since > max_seq:
            return {"seq": max_seq, "truncated": False, "reset": True, "changes": []}

        cursor = since
        deadline = time.monotonic() + wait_s
        while True:
            next_seq, truncated, rows = ctx.personal_store.changes_since_for_caller(
                cursor,
                viewer_login=ctx.viewer_login,
                owner_id=ctx.owner_id,
                limit=limit,
            )
            cursor = next_seq
            if rows or self._shutting_down or time.monotonic() >= deadline:
                return {
                    "seq": cursor,
                    "truncated": truncated,
                    "changes": [{**asdict(r), "fields": list(r.fields)} for r in rows],
                }
            self.notifier.wait_until(deadline)

    def _handle_reader_batch(
        self, ctx: _RequestContext, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        ops = body["ops"] if isinstance(body.get("ops"), Mapping) else body
        if not isinstance(ops, Mapping):
            raise _ApiError(400, "invalid_request", "ops must be a JSON object.")
        return execute_reader_batch(
            ctx.read_store,
            ops,
            personal_store=ctx.personal_store,
            owner_id=ctx.owner_id,
            viewer_login=ctx.viewer_login,
            now=ctx.now,
        )

    # ----- Per-repo locked snapshot export ---------------------------------------

    def _export_lock_for(self, slug_lower: str) -> threading.Lock:
        with self._export_locks_guard:
            lock = self._export_locks.get(slug_lower)
            if lock is None:
                lock = threading.Lock()
                self._export_locks[slug_lower] = lock
            return lock

    def _open_or_build_export(
        self, ctx: _RequestContext, owner: str, name: str
    ) -> tuple[Any, str, int]:
        full_name = f"{owner}/{name}"
        repo = ctx.read_store.get_repo(full_name, include_pending_delete=True)
        if (
            repo is not None
            and repo.repo_id in ctx.read_store.pending_delete_repo_ids()
        ):
            raise _ApiError(
                409,
                "delete_pending",
                f"Repository '{repo.full_name}' is pending deletion.",
                retryable=True,
            )
        if repo is None:
            raise _ApiError(
                404, "not_found", f"Repository '{full_name}' is not registered."
            )
        if not repo.is_mirror:
            raise _ApiError(
                400,
                "not_mirror_repo",
                f"Repository '{repo.full_name}' is not in mirror mode.",
            )

        with self._export_lock_for(repo.full_name.lower()):
            fresh = ctx.read_store.get_repo(repo.full_name)
            if fresh is None or not fresh.is_mirror:
                raise _ApiError(
                    404,
                    "not_found",
                    f"Mirror repository '{full_name}' is no longer available.",
                )
            self.export_dir.mkdir(parents=True, exist_ok=True)
            snap_path = self.export_dir / f"{fresh.owner}__{fresh.name}.sqlite"
            meta_path = self.export_dir / f"{fresh.owner}__{fresh.name}.meta.json"
            fk = mirror_freshness_key(fresh)
            wm_list = [
                fk.done_backfills,
                fk.sync_watermark,
                fresh.last_indexed_sha or "none",
            ]
            cached_etag = _try_reuse_export_cache(
                snap_path, meta_path, wm_list, ctx.now, self.export_cache_ttl_s
            )
            if cached_etag is None:
                summary, cached_etag = ctx.read_store.export_single_repo_snapshot(
                    fresh.full_name, snap_path, now=ctx.now
                )
                _write_export_meta(
                    meta_path, ctx.now, wm_list, cached_etag, summary.dest_bytes
                )
            handle = snap_path.open("rb")
            size = os.fstat(handle.fileno()).st_size
            return handle, cached_etag, size

    def _stream_export(
        self,
        handler: _PackratRequestHandler,
        ctx: _RequestContext,
        params: Mapping[str, list[str]],
    ) -> None:
        repo_vals = params.get("repo")
        if not repo_vals or not repo_vals[0].strip():
            raise _ApiError(400, "invalid_repo", "Missing required ?repo=owner/name.")
        owner, name = _parse_repo_slug_pair(repo_vals[0])
        handle, etag, size = self._open_or_build_export(ctx, owner, name)
        try:
            if _etag_matches(handler.headers.get("If-None-Match"), etag):
                handler._send_not_modified(etag)
                return
            handler.send_response(200)
            handler.send_header("Content-Type", "application/vnd.sqlite3")
            handler.send_header("Content-Length", str(size))
            handler.send_header("ETag", etag)
            handler._write_security_headers()
            handler.end_headers()
            shutil.copyfileobj(handle, handler.wfile, length=_EXPORT_STREAM_CHUNK)
        finally:
            handle.close()


def _parse_search_kinds(raw_kinds: Any) -> tuple[str, ...]:
    if isinstance(raw_kinds, str):
        return tuple(k.strip() for k in raw_kinds.split(",") if k.strip())
    if isinstance(raw_kinds, Sequence):
        return tuple(str(k).strip() for k in raw_kinds if str(k).strip())
    return ()


def _parse_search_body(
    body: Mapping[str, Any],
) -> tuple[str, str, int, SearchFilters]:
    query = str(body.get("query") or "").strip()
    if not query:
        raise _ApiError(400, "invalid_request", "Search query must be non-empty.")
    mode = str(body.get("mode") or "hybrid").strip()
    if mode not in VALID_SEARCH_MODES:
        raise _ApiError(400, "invalid_request", f"Unsupported search mode {mode!r}.")
    limit = min(
        MAX_SEARCH_LIMIT, _positive_int_field(body, "limit", "limit", default=15)
    )
    repo_filter = str(body.get("repo") or "").strip() or None
    path_prefix = str(body.get("path_prefix") or body.get("path") or "").strip() or None
    state = str(body.get("state") or "").strip() or None
    filters = SearchFilters(
        repo=repo_filter,
        kinds=_parse_search_kinds(body.get("kinds")),
        path_prefix=path_prefix,
        state=state,
    )
    return query, mode, limit, filters


def _positive_int_field(
    body: Mapping[str, Any], primary: str, alt: str, *, default: int
) -> int:
    raw = body.get(primary, body.get(alt, default))
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise _ApiError(
            400, "invalid_request", f"{primary} must be an integer."
        ) from exc
    if val < 1:
        raise _ApiError(400, "invalid_request", f"{primary} must be >= 1.")
    return val


def _optional_canonical_keys(
    read_store: PackratStore, raw_keys: Any
) -> list[str] | None:
    if raw_keys is None:
        return None
    if not isinstance(raw_keys, list):
        raise _ApiError(400, "invalid_request", "keys must be a list of item keys.")
    out: list[str] = []
    for raw in raw_keys:
        try:
            parse_item_key(str(raw))
        except ValueError as exc:
            raise _ApiError(400, "invalid_item_key", str(exc)) from exc
        out.append(read_store.canonical_item_key(str(raw)))
    return out


def _try_reuse_export_cache(
    snap_path: Path,
    meta_path: Path,
    wm_list: list[Any],
    now: float,
    ttl_s: float,
) -> str | None:
    if not snap_path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        etag = str(meta["etag"])
        built_at = float(meta["built_at"])
        size_bytes = int(meta["size_bytes"])
        freshness = meta.get("freshness")
    except (OSError, KeyError, TypeError, ValueError):
        return None
    if snap_path.stat().st_size != size_bytes or not etag:
        return None
    if (now - built_at) < ttl_s or freshness == wm_list:
        return etag
    return None


def _write_export_meta(
    meta_path: Path,
    built_at: float,
    wm_list: list[Any],
    etag: str,
    size_bytes: int,
) -> None:
    tmp = (
        meta_path.parent / f"{meta_path.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    try:
        tmp.write_text(
            json.dumps(
                {
                    "built_at": built_at,
                    "freshness": wm_list,
                    "etag": etag,
                    "size_bytes": size_bytes,
                }
            ),
            encoding="utf-8",
        )
        os.replace(tmp, meta_path)
    except OSError:
        tmp.unlink(missing_ok=True)
