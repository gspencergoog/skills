"""Remote HTTP v1 client, hybrid router, and DuckDB `--with-remote` SQL stager.

Provides configuration loading, an external `http_cmd` JSON transport (with a
lazy `urllib.request` fallback), single-session live-target pinning, parallel
local+remote search deduplication, and multi-predicate SQL routing without
creating local SQLite databases on read-only paths.
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

try:
    from .db_store import DEFAULT_DB_PATH, PackratStore, render_init_sql
    from .models import (
        EXIT_LOCKED_RETRY,
        UNTRUSTED_PROSE_COLUMNS,
        ClientConfig,
        HttpHelperError,
        SearchFilters,
        SearchResponseEnvelope,
        SqlColumnMeta,
        SqlResponseEnvelope,
        normalize_utc_iso,
        wrap_untrusted_text,
    )
except ImportError:
    from db_store import DEFAULT_DB_PATH, PackratStore, render_init_sql  # type: ignore[no-redef]
    from models import (  # type: ignore[no-redef]
        EXIT_LOCKED_RETRY,
        UNTRUSTED_PROSE_COLUMNS,
        ClientConfig,
        HttpHelperError,
        SearchFilters,
        SearchResponseEnvelope,
        SqlColumnMeta,
        SqlResponseEnvelope,
        normalize_utc_iso,
        wrap_untrusted_text,
    )

if TYPE_CHECKING:
    from search_engine import SearchMode

_VALID_CLIENT_MODES: frozenset[str] = frozenset({"local", "remote", "auto"})
_VALID_SQL_FORMATS: frozenset[str] = frozenset({"box", "json", "markdown", "csv"})
_LOCK_RETRY_BACKOFFS: tuple[float, ...] = (0.1, 0.25, 0.5)
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})
_AUTH_EXIT_CODES: frozenset[int] = frozenset({4, 10, 401})
_AUTH_TEXT_MARKERS: tuple[str, ...] = (
    "auth_expired",
    "unauthorized",
    "auth expired",
    "authentication",
    "credential",
    "certificate",
    "token expired",
    "login required",
    "401",
)
_GIT_TIER_KINDS: frozenset[str] = frozenset({"commit", "code_symbol", "doc_section"})
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SLUG_PREFIX_RE = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:[:#%?]|$)")
_SIMPLE_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_SQL_TYPE_RE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_ ]*(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(?:\[\d*\])*$"
)
_FORBIDDEN_TYPE_WORDS: frozenset[str] = frozenset(
    {
        "ATTACH",
        "COPY",
        "CREATE",
        "DELETE",
        "DROP",
        "FROM",
        "INSERT",
        "SELECT",
        "SET",
        "UNION",
        "UPDATE",
        "WHERE",
    }
)
_PERSONAL_VIEW_RE = re.compile(
    r"\b(v_viewer_items|v_watches|v_notifications|v_changes|v_service_health)\b",
    re.IGNORECASE,
)
_COL_EXPR_TPL = (
    r"(?:LOWER\s*\(\s*)?(?:[A-Za-z_][A-Za-z0-9_]*\.)?"
    r"(?:{cols})(?:\s*\))?"
)
_SINGLE_PRED_RE = re.compile(
    _COL_EXPR_TPL.format(
        cols="repo|repo_full_name|full_name|item_key|issue_key|pr_key|chunk_id"
    )
    + r"\s*(?:=|LIKE|IS\s+NOT\s+DISTINCT\s+FROM)\s*"
    + r"(?:LOWER\s*\(\s*)?'([^']*)'(?:\s*\))?",
    re.IGNORECASE,
)
_IN_PRED_RE = re.compile(
    _COL_EXPR_TPL.format(
        cols="repo|repo_full_name|full_name|item_key|issue_key|pr_key|chunk_id"
    )
    + r"\s+IN\s*\(([^)]+)\)",
    re.IGNORECASE,
)
_QUOTED_STR_RE = re.compile(r"'([^']*)'")
_CHUNK_SIZE_BYTES = 65536


@dataclass(frozen=True)
class LocalDuckDbResult:
    """Output of a local DuckDB query execution (with optional `--with-remote` staging)."""

    returncode: int
    stdout: str
    stderr: str
    rows: list[dict[str, Any]]


def _resolve_home_dir(env_map: Mapping[str, str]) -> Path:
    raw_home = str(env_map.get("HOME") or "").strip()
    if raw_home:
        return Path(raw_home).expanduser()
    return Path.home()


def _validate_client_mode(raw: str) -> Literal["local", "remote", "auto"]:
    cleaned = raw.strip().lower()
    if cleaned not in _VALID_CLIENT_MODES:
        raise ValueError(
            f"Invalid routing mode {raw!r}; expected one of {sorted(_VALID_CLIENT_MODES)}."
        )
    if cleaned == "local":
        return "local"
    if cleaned == "remote":
        return "remote"
    return "auto"


def _read_config_dict(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.is_file():
        return {}
    loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"Client config {cfg_path} must be a JSON object.")
    return loaded


def _parse_http_cmd(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        raise TypeError("Client config `http_cmd` must be a list of strings.")
    return tuple(str(part) for part in raw if str(part))


def _is_non_default_db(raw_db: Any, home_dir: Path) -> bool:
    if not raw_db:
        return False
    resolved = Path(str(raw_db)).expanduser().resolve()
    home_default = (
        (home_dir / ".local" / "share" / "packrat" / "repos.sqlite")
        .expanduser()
        .resolve()
    )
    return resolved not in {DEFAULT_DB_PATH.expanduser().resolve(), home_default}


def _resolve_remote_url(
    data: Mapping[str, Any], env_map: Mapping[str, str], args: Any
) -> tuple[str, str]:
    remote_url = str(data.get("remote_url") or "").strip()
    if env_map.get("PACKRAT_URL"):
        remote_url = str(env_map["PACKRAT_URL"]).strip()
    cli_remote_url = (
        str(getattr(args, "remote_url", None) or "").strip() if args is not None else ""
    )
    if cli_remote_url:
        remote_url = cli_remote_url
    return remote_url.rstrip("/"), cli_remote_url


def _cli_mode_override(args: Any) -> tuple[str | None, bool, str]:
    if args is None:
        return None, False, ""
    cli_remote = bool(getattr(args, "remote", False))
    cli_local = bool(getattr(args, "local", False))
    if cli_remote and cli_local:
        raise ValueError("Cannot pass both --remote and --local.")
    routing_mode = str(getattr(args, "routing_mode", None) or "").strip().lower()
    if cli_remote:
        return "remote", True, routing_mode
    if cli_local:
        return "local", False, routing_mode
    return (routing_mode or None), False, routing_mode


def _resolve_effective_mode(
    data: Mapping[str, Any],
    env_map: Mapping[str, str],
    args: Any,
    home_dir: Path,
    remote_url: str,
    cli_remote_url: str,
) -> Literal["local", "remote", "auto"]:
    raw_mode = str(data.get("mode") or "").strip().lower()
    explicit: str | None = raw_mode or None
    env_mode = str(env_map.get("PACKRAT_MODE") or "").strip().lower()
    if env_mode:
        explicit = env_mode

    cli_mode, cli_remote, cli_routing_mode = _cli_mode_override(args)
    if cli_mode:
        explicit = cli_mode

    mode_str = explicit if explicit is not None else ("auto" if remote_url else "local")
    raw_db = getattr(args, "db", None) if args is not None else None
    if (
        _is_non_default_db(raw_db, home_dir)
        and not cli_remote
        and not cli_remote_url
        and cli_routing_mode != "remote"
    ):
        mode_str = "local"
    return _validate_client_mode(mode_str)


def load_client_config(
    args: Any = None,
    *,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ClientConfig:
    """Loads `ClientConfig` from JSON, `PACKRAT_*` env vars, and CLI flags."""
    env_map = os.environ if env is None else env
    home_dir = _resolve_home_dir(env_map)
    cfg_path = (
        Path(config_path).expanduser()
        if config_path is not None
        else home_dir / ".config" / "packrat" / "config.json"
    )
    data = _read_config_dict(cfg_path)
    remote_url, cli_remote_url = _resolve_remote_url(data, env_map, args)
    mode = _resolve_effective_mode(
        data, env_map, args, home_dir, remote_url, cli_remote_url
    )
    return ClientConfig(
        mode=mode,
        remote_url=remote_url,
        http_cmd=_parse_http_cmd(data.get("http_cmd")),
        forward_gh_token=bool(data.get("forward_gh_token", False)),
        github_user=str(data.get("github_user") or "").strip(),
    )


def exit_code_for_http_error(exc: HttpHelperError) -> int:
    """Maps `HttpHelperError` to the CLI exit code (`3` for retryable lock contention)."""
    return EXIT_LOCKED_RETRY if exc.code == "database_locked" else 1


def _is_token_eligible_url(url: str) -> bool:
    import urllib.parse

    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    return scheme == "https" or (scheme == "http" and host in _LOOPBACK_HOSTS)


def _default_gh_token_provider() -> str | None:
    env = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        proc = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def _header_ci(headers: Mapping[str, Any], name: str) -> str | None:
    target = name.lower()
    for key, val in headers.items():
        if str(key).lower() == target:
            return str(val)
    return None


def _is_auth_failure(returncode: int, text: str) -> bool:
    if returncode in _AUTH_EXIT_CODES:
        return True
    low = text.lower()
    return any(marker in low for marker in _AUTH_TEXT_MARKERS)


def _extract_body_error_obj(resp: Mapping[str, Any]) -> Any:
    err_obj = resp.get("error")
    if err_obj is not None:
        return err_obj
    body = resp.get("body")
    if not isinstance(body, str) or not body.lstrip().startswith("{"):
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    if isinstance(parsed, Mapping):
        return parsed.get("error")
    return None


def _extract_error_fields(
    status: int, resp: Mapping[str, Any]
) -> tuple[str, str, bool]:
    err_obj = _extract_body_error_obj(resp)
    if isinstance(err_obj, Mapping):
        return (
            str(err_obj.get("code") or ""),
            str(err_obj.get("message") or ""),
            bool(err_obj.get("retryable", False)),
        )
    if isinstance(err_obj, str) and err_obj.strip():
        return err_obj.strip(), err_obj.strip(), False
    body_text = str(resp.get("body") or "").strip()
    return "", body_text or f"HTTP {status}", False


def _fallback_error_code(status: int) -> str:
    if status == 403:
        return "forbidden"
    return f"http_{status}" if status > 0 else "helper_error"


def _map_http_error(status: int, resp: Mapping[str, Any]) -> HttpHelperError:
    code, message, retryable = _extract_error_fields(status, resp)
    if status == 401 or code in ("unauthorized", "auth_expired"):
        return HttpHelperError(
            "auth_expired",
            message or "Remote authentication expired.",
            status or 401,
            retryable=False,
        )
    if status == 504 or code == "query_timeout":
        return HttpHelperError(
            "query_timeout",
            message or "Remote query timed out.",
            status or 504,
            retryable=False,
        )
    if status == 503 and code == "database_locked":
        return HttpHelperError(
            "database_locked",
            message or "Remote database is locked.",
            503,
            retryable=True,
        )
    resolved_code = code or _fallback_error_code(status)
    return HttpHelperError(
        resolved_code, message or resolved_code, status, retryable=retryable
    )


class HttpCmdTransport:
    """HTTP v1 client transport using `http_cmd` stdin/stdout JSON or lazy `urllib.request`."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        token_provider: Callable[[], str | None] | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        subprocess_run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.config = config
        self._token_provider = token_provider
        self._sleep = sleep_fn
        self._subprocess_run = subprocess_run
        self._token_loaded = False
        self._cached_token: str | None = None

    def _get_token(self) -> str | None:
        if not self._token_loaded:
            provider = (
                self._token_provider
                if self._token_provider is not None
                else _default_gh_token_provider
            )
            self._cached_token = provider()
            self._token_loaded = True
        return self._cached_token

    def _resolve_url(self, path_or_url: str) -> str:
        if path_or_url.startswith(("http://", "https://")):
            return path_or_url
        base = self.config.remote_url.rstrip("/")
        if not base:
            raise HttpHelperError(
                "missing_remote_url",
                "Remote mode requested without a configured remote_url.",
                0,
                retryable=False,
            )
        return f"{base}/{path_or_url.lstrip('/')}"

    def _build_headers(
        self,
        url: str,
        extra: Mapping[str, str] | None,
        *,
        has_json: bool,
    ) -> dict[str, str]:
        headers = {str(k): str(v) for k, v in (extra or {}).items()}
        if has_json and _header_ci(headers, "content-type") is None:
            headers["Content-Type"] = "application/json"
        if (
            self.config.github_user
            and _header_ci(headers, "x-packrat-github-user") is None
        ):
            headers["X-Packrat-Github-User"] = self.config.github_user
        if (
            self.config.forward_gh_token
            and _header_ci(headers, "authorization") is None
            and _is_token_eligible_url(url)
        ):
            token = self._get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _invoke_subprocess(
        self, argv: Sequence[str], *, input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        runner = self._subprocess_run or subprocess.run
        return runner(
            list(argv),
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def _run_http_cmd(
        self, payload: Mapping[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        try:
            proc = self._invoke_subprocess(
                self.config.http_cmd,
                input_text=json.dumps(payload),
                timeout=timeout_s + 5.0,
            )
        except subprocess.TimeoutExpired as exc:
            raise HttpHelperError(
                "helper_timeout",
                f"HTTP helper timed out after {timeout_s + 5.0:.1f}s.",
                504,
                retryable=True,
            ) from exc
        except OSError as exc:
            raise HttpHelperError(
                "helper_exec_error",
                f"Failed to execute http_cmd: {exc}",
                0,
                retryable=False,
            ) from exc

        parsed: dict[str, Any] | None = None
        if proc.stdout.strip():
            try:
                candidate = json.loads(proc.stdout)
                if isinstance(candidate, dict):
                    parsed = candidate
            except ValueError:
                parsed = None
        if parsed is not None and ("status" in parsed or "error" in parsed):
            return parsed

        detail = (proc.stderr or proc.stdout).strip() or (
            f"http_cmd exited with status {proc.returncode}"
        )
        if _is_auth_failure(proc.returncode, detail):
            raise HttpHelperError("auth_expired", detail, 401, retryable=False)
        raise HttpHelperError("helper_failed", detail, 0, retryable=False)

    @staticmethod
    def _stream_to_path(resp: Any, output_path: Path) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.parent / f"{output_path.name}.tmp.{os.getpid()}"
        written = 0
        try:
            with tmp_path.open("wb") as handle:
                while True:
                    chunk = resp.read(_CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
            os.replace(tmp_path, output_path)
            return written
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _run_direct_http(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str | None,
        timeout_s: float,
        output_path: Path | None,
    ) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        data = body.encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, headers=dict(headers), method=method.upper()
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                resp_headers = dict(resp.headers.items())
                if output_path is not None:
                    written = self._stream_to_path(resp, output_path)
                    return {
                        "status": int(resp.status),
                        "headers": resp_headers,
                        "bytes_written": written,
                    }
                text = resp.read().decode("utf-8", errors="replace")
                return {
                    "status": int(resp.status),
                    "headers": resp_headers,
                    "body": text,
                }
        except urllib.error.HTTPError as exc:
            err_headers = dict(exc.headers.items()) if exc.headers else {}
            err_body = exc.read().decode("utf-8", errors="replace")
            return {
                "status": int(exc.code),
                "headers": err_headers,
                "body": err_body,
            }
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError) or "timed out" in str(exc).lower():
                raise HttpHelperError(
                    "query_timeout" if "/sql" in url else "network_timeout",
                    f"Request to {url} timed out: {exc}",
                    504,
                    retryable=True,
                ) from exc
            raise HttpHelperError(
                "network_error",
                f"Request to {url} failed: {exc}",
                0,
                retryable=True,
            ) from exc

    def _send_once(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: str | None,
        timeout_s: float,
        output_path: Path | None,
    ) -> dict[str, Any]:
        if self.config.http_cmd:
            payload: dict[str, Any] = {
                "method": method.upper(),
                "url": url,
                "headers": dict(headers),
                "timeout_s": float(timeout_s),
            }
            if body is not None:
                payload["body"] = body
            if output_path is not None:
                payload["output_path"] = str(output_path)
            return self._run_http_cmd(payload, timeout_s)
        return self._run_direct_http(method, url, headers, body, timeout_s, output_path)

    def request(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        body: str | None = None,
        timeout_s: float = 30.0,
        output_path: Path | None = None,
    ) -> dict[str, Any]:
        """Executes one HTTP request with bounded `503 database_locked` retries."""
        url = self._resolve_url(path_or_url)
        encoded_body = json.dumps(json_body) if json_body is not None else body
        req_headers = self._build_headers(
            url, headers, has_json=(json_body is not None)
        )
        max_attempts = len(_LOCK_RETRY_BACKOFFS) + 1
        for attempt in range(max_attempts):
            resp = self._send_once(
                method, url, req_headers, encoded_body, timeout_s, output_path
            )
            status = int(resp.get("status") or 0)
            if status in (200, 201, 202, 204, 304):
                return resp
            exc = _map_http_error(status, resp)
            if (
                status == 503
                and exc.code == "database_locked"
                and attempt < len(_LOCK_RETRY_BACKOFFS)
            ):
                base_delay = _LOCK_RETRY_BACKOFFS[attempt]
                self._sleep(base_delay * random.uniform(0.8, 1.2))
                continue
            raise exc
        raise HttpHelperError(
            "database_locked", "Remote database is locked.", 503, retryable=True
        )

    def request_json(
        self,
        method: str,
        path_or_url: str,
        *,
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        timeout_s: float = 30.0,
    ) -> Any:
        """Executes a request and decodes its JSON response body."""
        resp = self.request(
            method,
            path_or_url,
            headers=headers,
            json_body=json_body,
            timeout_s=timeout_s,
        )
        raw_body = str(resp.get("body") or "")
        if not raw_body.strip():
            return {}
        try:
            return json.loads(raw_body)
        except ValueError as exc:
            raise HttpHelperError(
                "invalid_json",
                f"Malformed JSON response from {path_or_url}: {exc}",
                int(resp.get("status") or 0),
                retryable=False,
            ) from exc


def _parse_iso_epoch(raw: str | None) -> float | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    candidate = f"{text[:-1]}+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


class SessionTargetResolver:
    """Pins one live target (`local` or `remote`) for the duration of a CLI session."""

    def __init__(
        self,
        config: ClientConfig,
        db_path: Path,
        *,
        max_beat_age_s: float = 180.0,
        now_fn: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.db_path = Path(db_path)
        self.max_beat_age_s = float(max_beat_age_s)
        self._now = now_fn
        self._resolved: Literal["local", "remote"] | None = None

    def _local_service_healthy(self) -> bool:
        if not self.db_path.is_file():
            return False
        store: PackratStore | None = None
        try:
            store = PackratStore(self.db_path, read_only=True)
            hb = store.read_heartbeat()
        except (sqlite3.DatabaseError, OSError):
            return False
        finally:
            if store is not None:
                store.close()
        if hb is None or not hb.beat_at:
            return False
        beat_epoch = _parse_iso_epoch(hb.beat_at)
        if beat_epoch is None:
            return False
        return (self._now() - beat_epoch) <= self.max_beat_age_s

    def resolve(self) -> Literal["local", "remote"]:
        """Resolves and caches the session live target without creating `repos.sqlite`."""
        if self._resolved is not None:
            return self._resolved
        if self.config.mode == "local":
            self._resolved = "local"
            return self._resolved
        if self.config.mode == "remote":
            if not self.config.remote_url:
                raise HttpHelperError(
                    "missing_remote_url",
                    "Remote mode requires remote_url to be configured.",
                    0,
                    retryable=False,
                )
            self._resolved = "remote"
            return self._resolved
        if self._local_service_healthy():
            self._resolved = "local"
        elif self.config.remote_url:
            self._resolved = "remote"
        else:
            self._resolved = "local"
        return self._resolved

    @property
    def target(self) -> Literal["local", "remote"]:
        return self.resolve()

    @property
    def is_remote(self) -> bool:
        return self.resolve() == "remote"


# ----- Typed HTTP v1 API wrappers ---------------------------------------------


def _expect_dict(data: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise HttpHelperError(
            "invalid_response",
            f"Expected JSON object from {endpoint}.",
            0,
            retryable=False,
        )
    return data


def remote_health(transport: HttpCmdTransport) -> dict[str, Any]:
    """Calls `GET /api/v1/health`."""
    return _expect_dict(
        transport.request_json("GET", "/api/v1/health"), "/api/v1/health"
    )


def remote_auth_link(
    transport: HttpCmdTransport, github_login: str | None = None
) -> dict[str, Any]:
    """Calls `POST /api/v1/auth/link`."""
    body = {"github_login": github_login} if github_login else {}
    return _expect_dict(
        transport.request_json("POST", "/api/v1/auth/link", json_body=body),
        "/api/v1/auth/link",
    )


def remote_list_repos(
    transport: HttpCmdTransport, *, mode: str | None = None
) -> dict[str, Any]:
    """Calls `GET /api/v1/repos`."""
    import urllib.parse

    path = "/api/v1/repos"
    if mode:
        path = f"{path}?mode={urllib.parse.quote(mode, safe='')}"
    return _expect_dict(transport.request_json("GET", path), "/api/v1/repos")


def remote_add_repo(
    transport: HttpCmdTransport,
    full_name: str,
    *,
    default_branch: str | None = None,
    git_source: str | None = None,
    git_url: str | None = None,
) -> dict[str, Any]:
    """Calls `POST /api/v1/repos`."""
    payload: dict[str, Any] = {"full_name": full_name}
    if default_branch is not None:
        payload["default_branch"] = default_branch
    if git_source is not None:
        payload["git_source"] = git_source
    if git_url is not None:
        payload["git_url"] = git_url
    return _expect_dict(
        transport.request_json("POST", "/api/v1/repos", json_body=payload),
        "/api/v1/repos",
    )


def remote_remove_repo(transport: HttpCmdTransport, full_name: str) -> dict[str, Any]:
    """Calls `DELETE /api/v1/repos/:owner/:name`."""
    import urllib.parse

    owner, sep, name = full_name.strip().partition("/")
    if not sep or not owner or not name or "/" in name:
        raise ValueError(f"Invalid repository slug {full_name!r}; expected owner/name.")
    path = (
        f"/api/v1/repos/{urllib.parse.quote(owner, safe='')}/"
        f"{urllib.parse.quote(name, safe='')}"
    )
    return _expect_dict(transport.request_json("DELETE", path), path)


def remote_search(
    transport: HttpCmdTransport,
    query: str,
    *,
    repo: str | None = None,
    kinds: Sequence[str] = (),
    path_prefix: str | None = None,
    state: str | None = None,
    mode: str = "hybrid",
    limit: int = 15,
) -> SearchResponseEnvelope:
    """Calls `POST /api/v1/search` and normalizes `SearchResponseEnvelope`."""
    payload: dict[str, Any] = {"query": query, "mode": mode, "limit": int(limit)}
    if repo:
        payload["repo"] = repo
    if kinds:
        payload["kinds"] = [str(k) for k in kinds]
    if path_prefix:
        payload["path_prefix"] = path_prefix
    if state:
        payload["state"] = state
    raw = _expect_dict(
        transport.request_json("POST", "/api/v1/search", json_body=payload),
        "/api/v1/search",
    )
    hits = [dict(h) for h in raw.get("hits", ()) if isinstance(h, Mapping)]
    wms_raw = raw.get("repo_watermarks")
    wms = (
        {str(k).lower(): dict(v) for k, v in wms_raw.items() if isinstance(v, Mapping)}
        if isinstance(wms_raw, Mapping)
        else {}
    )
    warnings = [str(w) for w in raw.get("warnings", ())]
    return {"hits": hits, "repo_watermarks": wms, "warnings": warnings}


def remote_sql(
    transport: HttpCmdTransport,
    query: str,
    *,
    raw: bool = False,
    limit_rows: int | None = None,
    timeout_s: float = 30.0,
) -> SqlResponseEnvelope:
    """Calls `POST /api/v1/sql` and normalizes `SqlResponseEnvelope`."""
    payload: dict[str, Any] = {"query": query, "raw": bool(raw)}
    if limit_rows is not None:
        payload["limit_rows"] = int(limit_rows)
    data = _expect_dict(
        transport.request_json(
            "POST", "/api/v1/sql", json_body=payload, timeout_s=timeout_s
        ),
        "/api/v1/sql",
    )
    rows = [dict(r) for r in data.get("rows", ()) if isinstance(r, Mapping)]
    cols: list[SqlColumnMeta] = []
    for col in data.get("columns", ()):
        if isinstance(col, Mapping) and col.get("name"):
            cols.append(
                {
                    "name": str(col["name"]),
                    "type": str(col.get("type") or "VARCHAR"),
                }
            )
    return {"rows": rows, "columns": cols}


def remote_watches(
    transport: HttpCmdTransport,
    op: Literal["add", "renew", "remove"],
    *,
    consumer: str,
    consumer_id: str,
    keys: Sequence[str] | None = None,
    freshness_s: int | None = None,
    ttl_s: int | None = None,
) -> dict[str, Any]:
    """Calls `POST /api/v1/watches`."""
    payload: dict[str, Any] = {
        "op": op,
        "consumer": consumer,
        "consumer_id": consumer_id,
    }
    if keys is not None:
        payload["keys"] = [str(k) for k in keys]
    if freshness_s is not None:
        payload["freshness_s"] = int(freshness_s)
    if ttl_s is not None:
        payload["ttl_s"] = int(ttl_s)
    return _expect_dict(
        transport.request_json("POST", "/api/v1/watches", json_body=payload),
        "/api/v1/watches",
    )


def remote_refresh(transport: HttpCmdTransport, keys: Sequence[str]) -> dict[str, Any]:
    """Calls `POST /api/v1/refresh`."""
    return _expect_dict(
        transport.request_json(
            "POST", "/api/v1/refresh", json_body={"keys": [str(k) for k in keys]}
        ),
        "/api/v1/refresh",
    )


def remote_changes(
    transport: HttpCmdTransport,
    *,
    since: int = 0,
    wait: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """Calls `GET /api/v1/changes`."""
    path = f"/api/v1/changes?since={int(since)}&wait={int(wait)}&limit={int(limit)}"
    timeout_s = max(30.0, float(wait) + 15.0)
    return _expect_dict(
        transport.request_json("GET", path, timeout_s=timeout_s), "/api/v1/changes"
    )


def remote_reader_batch(
    transport: HttpCmdTransport, ops: Mapping[str, Any]
) -> dict[str, Any]:
    """Calls `POST /api/v1/reader/batch`."""
    return _expect_dict(
        transport.request_json("POST", "/api/v1/reader/batch", json_body=dict(ops)),
        "/api/v1/reader/batch",
    )


def remote_export_repo(
    transport: HttpCmdTransport,
    repo_full_name: str,
    dest_path: Path,
    *,
    if_none_match: str | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Streams `GET /api/v1/export` to `dest_path` using `output_path` and optional `ETag`."""
    import urllib.parse

    dest = Path(dest_path)
    path = f"/api/v1/export?repo={urllib.parse.quote(repo_full_name, safe='')}"
    headers = {"If-None-Match": if_none_match} if if_none_match else None
    resp = transport.request(
        "GET",
        path,
        headers=headers,
        timeout_s=timeout_s,
        output_path=dest,
    )
    status = int(resp.get("status") or 0)
    resp_headers = resp.get("headers")
    etag = (
        _header_ci(resp_headers, "etag") if isinstance(resp_headers, Mapping) else None
    )
    return {
        "status": status,
        "not_modified": status == 304,
        "etag": etag,
        "bytes_written": int(resp.get("bytes_written") or 0),
        "path": str(dest),
    }


# ----- Hybrid Search & Per-(Repo, Tier) Deduplication -------------------------


def _local_search_task(
    db_path: Path,
    query: str,
    filters: SearchFilters,
    mode: SearchMode,
    limit: int,
    embedder: Any | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str | None]]]:
    from search_engine import hybrid_rrf_search, repo_watermarks

    store = PackratStore(db_path, read_only=True)
    try:
        hits = hybrid_rrf_search(
            store,
            query=query,
            filters=filters,
            mode=mode,
            limit=limit,
            embedder=embedder,
            allow_cache_rebuild=False,
        )
        wms = repo_watermarks(store)
        return [asdict(h) for h in hits], wms
    finally:
        store.close()


def _tier_for_kind(source_kind: str) -> str:
    return "git" if source_kind in _GIT_TIER_KINDS else "github"


def _git_freshness_key(wm: Mapping[str, Any] | None) -> tuple[str, int]:
    if not wm:
        return ("", 0)
    commit_at = normalize_utc_iso(wm.get("last_commit_at"))
    has_sha = 1 if wm.get("last_indexed_sha") else 0
    return (commit_at, has_sha)


def _github_freshness_key(wm: Mapping[str, Any] | None) -> str:
    if not wm:
        return ""
    return normalize_utc_iso(wm.get("sync_watermark"))


def _pick_tier_winner(
    repo_key: str,
    tier: str,
    local_wms: Mapping[str, Mapping[str, Any]],
    remote_wms: Mapping[str, Mapping[str, Any]],
) -> Literal["local", "remote"]:
    local_wm = local_wms.get(repo_key)
    remote_wm = remote_wms.get(repo_key)
    if local_wm is not None and remote_wm is None:
        return "local"
    if remote_wm is not None and local_wm is None:
        return "remote"
    if tier == "git":
        return (
            "remote"
            if _git_freshness_key(remote_wm) > _git_freshness_key(local_wm)
            else "local"
        )
    return (
        "remote"
        if _github_freshness_key(remote_wm) > _github_freshness_key(local_wm)
        else "local"
    )


def _merge_search_results(
    local_hits: list[dict[str, Any]],
    local_wms: dict[str, dict[str, str | None]],
    remote_env: SearchResponseEnvelope,
    limit: int,
) -> SearchResponseEnvelope:
    remote_wms = remote_env["repo_watermarks"]
    winners: dict[tuple[str, str], Literal["local", "remote"]] = {}

    def _winner(repo_key: str, tier: str) -> Literal["local", "remote"]:
        pair = (repo_key, tier)
        if pair not in winners:
            winners[pair] = _pick_tier_winner(repo_key, tier, local_wms, remote_wms)
        return winners[pair]

    merged: list[dict[str, Any]] = []
    for side, hits in (("local", local_hits), ("remote", remote_env["hits"])):
        for hit in hits:
            repo_key = str(hit.get("repo_full_name") or "").lower()
            tier = _tier_for_kind(str(hit.get("source_kind") or ""))
            if _winner(repo_key, tier) == side:
                merged.append(hit)

    merged.sort(
        key=lambda h: (
            float(h.get("rrf_score") or 0.0),
            float(h["cosine_score"]) if h.get("cosine_score") is not None else -1.0,
        ),
        reverse=True,
    )
    combined_wms = {**remote_wms, **local_wms}
    return {
        "hits": merged[:limit],
        "repo_watermarks": combined_wms,
        "warnings": list(remote_env["warnings"]),
    }


def hybrid_search(
    query: str,
    config: ClientConfig,
    db_path: Path,
    *,
    filters: SearchFilters | None = None,
    mode: SearchMode = "hybrid",
    limit: int = 15,
    transport: HttpCmdTransport | None = None,
    embedder: Any | None = None,
) -> SearchResponseEnvelope:
    """Runs local and/or remote search and deduplicates per `(repo, tier)` watermark."""
    active_filters = filters or SearchFilters()
    has_local = Path(db_path).is_file()
    if config.mode == "local":
        if not has_local:
            return {"hits": [], "repo_watermarks": {}, "warnings": []}
        hits, wms = _local_search_task(
            Path(db_path), query, active_filters, mode, limit, embedder
        )
        return {"hits": hits, "repo_watermarks": wms, "warnings": []}

    active_transport = transport or HttpCmdTransport(config)
    if config.mode == "remote":
        if not config.remote_url:
            raise HttpHelperError(
                "missing_remote_url",
                "Remote mode requires remote_url to be configured.",
                0,
                retryable=False,
            )
        return remote_search(
            active_transport,
            query,
            repo=active_filters.repo,
            kinds=active_filters.kinds,
            path_prefix=active_filters.path_prefix,
            state=active_filters.state,
            mode=mode,
            limit=limit,
        )

    if not config.remote_url:
        if not has_local:
            return {"hits": [], "repo_watermarks": {}, "warnings": []}
        hits, wms = _local_search_task(
            Path(db_path), query, active_filters, mode, limit, embedder
        )
        return {"hits": hits, "repo_watermarks": wms, "warnings": []}

    if not has_local:
        return remote_search(
            active_transport,
            query,
            repo=active_filters.repo,
            kinds=active_filters.kinds,
            path_prefix=active_filters.path_prefix,
            state=active_filters.state,
            mode=mode,
            limit=limit,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        local_fut = pool.submit(
            _local_search_task,
            Path(db_path),
            query,
            active_filters,
            mode,
            limit,
            embedder,
        )
        remote_fut = pool.submit(
            remote_search,
            active_transport,
            query,
            repo=active_filters.repo,
            kinds=active_filters.kinds,
            path_prefix=active_filters.path_prefix,
            state=active_filters.state,
            mode=mode,
            limit=limit,
        )
        local_hits, local_wms = local_fut.result()
        try:
            remote_env = remote_fut.result()
        except (HttpHelperError, OSError) as exc:
            return {
                "hits": local_hits,
                "repo_watermarks": local_wms,
                "warnings": [
                    f"Remote search unavailable ({exc}); using local results."
                ],
            }
    return _merge_search_results(local_hits, local_wms, remote_env, limit)


# ----- SQL Routing & `--with-remote` Staging ----------------------------------


def _extract_repo_slug_from_literal(literal: str) -> str | None:
    cleaned = literal.strip().rstrip("%_")
    if not cleaned:
        return None
    if _SIMPLE_SLUG_RE.match(cleaned):
        return cleaned
    match = _SLUG_PREFIX_RE.match(cleaned)
    if match:
        return match.group(1)
    return None


def extract_sql_repo_refs(sql: str) -> set[str]:
    """Extracts `<owner>/<name>` repository references from SQL filter predicates."""
    refs: set[str] = set()
    for match in _SINGLE_PRED_RE.finditer(sql):
        slug = _extract_repo_slug_from_literal(match.group(1))
        if slug:
            refs.add(slug.lower())
    for match in _IN_PRED_RE.finditer(sql):
        for lit_match in _QUOTED_STR_RE.finditer(match.group(1)):
            slug = _extract_repo_slug_from_literal(lit_match.group(1))
            if slug:
                refs.add(slug.lower())
    return refs


def references_personal_views(sql: str) -> bool:
    """Returns True when `sql` names one of the per-caller personal views."""
    return bool(_PERSONAL_VIEW_RE.search(sql))


def route_sql_mode(
    sql: str,
    config: ClientConfig,
    db_path: Path,
    *,
    resolver: SessionTargetResolver | None = None,
) -> Literal["local", "remote"]:
    """Chooses `"local"` or `"remote"` for `packrat sql` based on mode and predicates."""
    if config.mode == "local":
        return "local"
    if config.mode == "remote":
        if not config.remote_url:
            raise HttpHelperError(
                "missing_remote_url",
                "Remote mode requires remote_url to be configured.",
                0,
                retryable=False,
            )
        return "remote"
    if not config.remote_url:
        return "local"
    resolved_db = Path(db_path)
    if not resolved_db.is_file():
        return "remote"

    refs = extract_sql_repo_refs(sql)
    if refs:
        store = PackratStore(resolved_db, read_only=True)
        try:
            mirror_names = {
                r.full_name.lower() for r in store.list_repos(mode="mirror")
            }
        finally:
            store.close()
        return "local" if refs.issubset(mirror_names) else "remote"

    if references_personal_views(sql):
        active_resolver = resolver or SessionTargetResolver(config, resolved_db)
        return active_resolver.resolve()

    return "remote"


def parse_with_remote_specs(specs: Sequence[str]) -> list[tuple[str, str]]:
    """Parses and validates repeatable `ALIAS=SQL` `--with-remote` arguments."""
    parsed: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in specs:
        alias_raw, sep, sub_sql_raw = str(raw).partition("=")
        alias = alias_raw.strip()
        sub_sql = sub_sql_raw.strip()
        if not sep or not _ALIAS_RE.match(alias):
            raise ValueError(
                f"Invalid --with-remote spec {raw!r}; expected ALIAS=SQL "
                "with identifier alias ^[A-Za-z_][A-Za-z0-9_]*$."
            )
        low = alias.lower()
        if low in seen:
            raise ValueError(f"Duplicate --with-remote alias {alias!r}.")
        if not sub_sql:
            raise ValueError(f"Empty SQL subquery for --with-remote alias {alias!r}.")
        seen.add(low)
        parsed.append((alias, sub_sql))
    return parsed


def _real_user_home() -> Path | None:
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return None


def _candidate_duckdb_bases(which_bin: str | None) -> list[Path]:
    bases: list[Path] = [Path.home()]
    real_home = _real_user_home()
    if real_home is not None and real_home not in bases:
        bases.append(real_home)
    if which_bin:
        which_parent = Path(which_bin).resolve().parent.parent
        if which_parent not in bases:
            bases.append(which_parent)
    return bases


def resolve_duckdb_bin() -> str:
    """Resolves the DuckDB v2.0 CLI binary before relying on `$HOME`."""
    env_bin = os.environ.get("DUCKDB_BIN", "").strip()
    if env_bin:
        return env_bin

    which_bin = shutil.which("duckdb")
    rel_candidates = (
        Path(".local/share/mise/installs/github-duckdb-duckdb/2/duckdb"),
        Path(".local/share/mise/installs/duckdb/2/duckdb"),
        Path(".local/share/mise/installs/duckdb/latest/duckdb"),
        Path(".local/bin/duckdb"),
    )
    for base in _candidate_duckdb_bases(which_bin):
        for rel in rel_candidates:
            candidate = base / rel
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    homebrew = Path("/opt/homebrew/bin/duckdb")
    if homebrew.is_file() and os.access(homebrew, os.X_OK):
        return str(homebrew)
    return which_bin or str(Path.home() / ".local" / "bin" / "duckdb")


def _validate_duckdb_type(raw_type: str) -> str | None:
    cleaned = raw_type.strip()
    upper = cleaned.upper()
    if upper in ("", "NULL", '"NULL"', "SQLNULL", "ANY"):
        return None
    if not _SAFE_SQL_TYPE_RE.match(cleaned):
        return "VARCHAR"
    words = {tok for tok in re.split(r"[^A-Za-z_]+", upper) if tok}
    if words & _FORBIDDEN_TYPE_WORDS:
        return "VARCHAR"
    return cleaned


def _write_private_file(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)


def _stage_remote_view_sql(alias: str, env: SqlResponseEnvelope, tmp_dir: Path) -> str:
    rows = env.get("rows", [])
    columns = env.get("columns", [])
    if rows:
        json_path = tmp_dir / f"{alias}.json"
        _write_private_file(json_path, json.dumps(rows, ensure_ascii=False))
        escaped_path = str(json_path).replace("'", "''")
        return (
            f'CREATE OR REPLACE TEMP VIEW "{alias}" AS '
            f"SELECT * FROM read_json_auto('{escaped_path}');"
        )
    if columns:
        select_exprs: list[str] = []
        for col in columns:
            col_name = str(col.get("name") or "")
            if not col_name:
                continue
            escaped_col = col_name.replace('"', '""')
            validated_type = _validate_duckdb_type(str(col.get("type") or ""))
            if validated_type is None:
                select_exprs.append(f'NULL AS "{escaped_col}"')
            else:
                select_exprs.append(
                    f'CAST(NULL AS {validated_type}) AS "{escaped_col}"'
                )
        if select_exprs:
            return (
                f'CREATE OR REPLACE TEMP VIEW "{alias}" AS '
                f"SELECT {', '.join(select_exprs)} WHERE FALSE;"
            )
    raise ValueError(
        f"--with-remote subquery for '{alias}' returned 0 rows and no column metadata; "
        "use a single-statement SELECT query."
    )


def _build_setup_sql(
    attach_db: Path, remote_view_clauses: Sequence[str], tmp_dir: Path
) -> str:
    preamble: list[str] = []
    if not Path.home().is_dir():
        escaped_tmp = str(tmp_dir).replace("'", "''")
        preamble.append(f"SET home_directory = '{escaped_tmp}';")
    parts = [*preamble, render_init_sql(attach_db), *remote_view_clauses]
    return "\n".join(parts) + "\n"


def _strip_json_preamble(stdout: str) -> str:
    lines = stdout.splitlines()
    while lines and lines[0].lstrip().startswith("--"):
        lines.pop(0)
    text = "\n".join(lines).strip()
    if not text or text.startswith("["):
        return text
    arr_idx = text.find("\n[")
    if arr_idx != -1:
        return text[arr_idx + 1 :].lstrip()
    return text[text.index("[") :] if "[" in text else text


def _parse_duckdb_json(stdout: str) -> list[dict[str, Any]]:
    text = _strip_json_preamble(stdout)
    if not text:
        return []
    rows: list[dict[str, Any]] = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        parsed, end = decoder.raw_decode(text, idx)
        if isinstance(parsed, list):
            rows = [r for r in parsed if isinstance(r, dict)]
        idx = end
        while idx < len(text) and text[idx].isspace():
            idx += 1
    return rows


def _sql_source_ref(row: Mapping[str, Any], col: str) -> str:
    for key in ("item_key", "source_ref", "full_name"):
        val = row.get(key)
        if isinstance(val, str) and val:
            return val
    return f"packrat:sql:{col}"


def wrap_sql_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Wraps `UNTRUSTED_PROSE_COLUMNS` values in `<untrusted_repo_content>` tags."""
    wrapped: list[dict[str, Any]] = []
    for row in rows:
        out = dict(row)
        for col, val in row.items():
            if isinstance(val, str) and col.lower() in UNTRUSTED_PROSE_COLUMNS:
                out[col] = wrap_untrusted_text(_sql_source_ref(row, col), val)
        wrapped.append(out)
    return wrapped


def _cell_text(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _render_csv(cols: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(cols)
    for row in rows:
        writer.writerow([_cell_text(row.get(c)) for c in cols])
    return buf.getvalue()


def _render_markdown(cols: list[str], cells: list[list[str]]) -> str:
    esc = [[c.replace("|", "\\|") for c in r] for r in cells]
    lines = [
        "| " + " | ".join(cols) + " |",
        "|" + "|".join("---" for _ in cols) + "|",
    ]
    lines.extend("| " + " | ".join(r) + " |" for r in esc)
    return "\n".join(lines) + "\n"


def _render_box(cols: list[str], cells: list[list[str]]) -> str:
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


def render_sql_table(rows: list[dict[str, Any]], fmt: str) -> str:
    """Renders SQL rows as `json`, `csv`, `markdown`, or `box` text."""
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


def _strip_init_banner(stdout: str, fmt: str) -> str:
    if fmt != "json":
        return stdout
    lines = stdout.splitlines(keepends=True)
    while lines and lines[0].lstrip().startswith("--"):
        lines.pop(0)
    return "".join(lines)


def _fetch_staged_envelopes(
    parsed_specs: Sequence[tuple[str, str]],
    config: ClientConfig,
    transport: HttpCmdTransport | None,
) -> list[tuple[str, SqlResponseEnvelope]]:
    if not parsed_specs:
        return []
    if not config.remote_url:
        raise HttpHelperError(
            "missing_remote_url",
            "--with-remote requires remote_url to be configured.",
            0,
            retryable=False,
        )
    active_transport = transport or HttpCmdTransport(config)
    return [
        (alias, remote_sql(active_transport, sub_sql, raw=True))
        for alias, sub_sql in parsed_specs
    ]


def _format_duckdb_output(
    proc: subprocess.CompletedProcess[str], *, raw: bool, fmt: str
) -> LocalDuckDbResult:
    if proc.returncode != 0:
        return LocalDuckDbResult(proc.returncode, proc.stdout, proc.stderr, [])
    if raw:
        rows = _parse_duckdb_json(proc.stdout) if fmt == "json" else []
        return LocalDuckDbResult(
            0, _strip_init_banner(proc.stdout, fmt), proc.stderr, rows
        )
    try:
        rows = _parse_duckdb_json(proc.stdout)
    except json.JSONDecodeError:
        return LocalDuckDbResult(0, proc.stdout, proc.stderr, [])
    wrapped = wrap_sql_rows(rows)
    return LocalDuckDbResult(0, render_sql_table(wrapped, fmt), proc.stderr, wrapped)


def run_sql_with_remote(
    sql: str,
    with_remote_specs: Sequence[str],
    config: ClientConfig,
    db_path: Path,
    *,
    raw: bool = False,
    fmt: str = "json",
    transport: HttpCmdTransport | None = None,
) -> LocalDuckDbResult:
    """Stages remote subqueries as temp DuckDB views and executes `sql` locally."""
    if fmt not in _VALID_SQL_FORMATS:
        raise ValueError(
            f"Unsupported SQL format {fmt!r}; expected one of {sorted(_VALID_SQL_FORMATS)}."
        )
    parsed_specs = parse_with_remote_specs(with_remote_specs)
    duckdb_bin = resolve_duckdb_bin()
    staged_envs = _fetch_staged_envelopes(parsed_specs, config, transport)

    with tempfile.TemporaryDirectory(prefix="packrat_remote_sql_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        os.chmod(tmp_dir, 0o700)

        resolved_db = Path(db_path)
        if resolved_db.is_file():
            attach_db = resolved_db.resolve()
        else:
            attach_db = tmp_dir / "empty_kb.sqlite"
            PackratStore(attach_db).close()

        view_clauses = [
            _stage_remote_view_sql(alias, env, tmp_dir) for alias, env in staged_envs
        ]
        setup_path = tmp_dir / "setup.sql"
        _write_private_file(
            setup_path, _build_setup_sql(attach_db, view_clauses, tmp_dir)
        )

        fmt_flag = f"-{fmt}" if raw else "-json"
        cmd = [
            duckdb_bin,
            "-batch",
            "-dark-mode",
            "-no-agent",
            "-no-stdin",
            fmt_flag,
            ":memory:",
            "-init",
            str(setup_path),
            "-c",
            f"-- user_sql\n{sql}",
        ]
        proc_env = {**os.environ, "DUCKDB_AGENT_MODE": "0"}
        proc_env.pop("AI_AGENT", None)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env=proc_env,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return LocalDuckDbResult(2, "", f"Error executing duckdb: {exc}\n", [])
        return _format_duckdb_output(proc, raw=raw, fmt=fmt)
