"""Budgeted GitHub API transport over `gh api -i`.

`GhTransport` is the single path packrat workers use to talk to GitHub. Every
call checks the shared `RateBudget` before running `gh`, records the
`X-RateLimit-*` headers of the response into the budget, and writes one
`api_calls` audit row through an `ApiCallStore`.

Rate-limit errors are detected from stderr and the response body only, never
from the header block: with `-i`, a header such as `x-ratelimit-remaining: 4013`
contains `401`, and every header name contains `ratelimit`.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from rate_budget import Priority, RateBudget
from sync_runtime import run_bounded_subprocess

DEFAULT_TIMEOUT_SEC = 30
"""Default per-call `gh` timeout in seconds."""

_INCLUDE_FLAGS = ("-i", "--include")
_NOT_MODIFIED = 304
_RATE_LIMIT_COST_RE = re.compile(r'"rateLimit"\s*:\s*\{[^{}]*?"cost"\s*:\s*(\d+)')

_RATE_LIMIT_SUBSTRINGS: tuple[str, ...] = (
    "rate_limit",
    "api rate limit",
    "secondary rate limit",
)

GhErrorKind = Literal[
    "rate_limited",
    "deferred_rate_limit",
    "auth_failed",
    "keyring_locked",
    "command_error",
]


@dataclass(frozen=True)
class GhCommandResult:
    """Result of executing a gh CLI subprocess."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    error_kind: GhErrorKind | None = None


GhRunner = Callable[[Sequence[str], int], GhCommandResult]


def run_gh_command(args: Sequence[str], timeout: int = 20) -> GhCommandResult:
    """Executes gh with non-interactive environment variables and a strict timeout."""
    env = {
        **os.environ,
        "GH_PROMPT_DISABLED": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PAGER": "cat",
    }
    res = run_bounded_subprocess(["gh", *args], env=env, timeout=timeout)
    if res.timed_out:
        return GhCommandResult(
            returncode=res.returncode,
            stdout=res.stdout,
            stderr="gh command timed out (possible locked keyring or network stall)",
            timed_out=True,
        )
    return GhCommandResult(
        returncode=res.returncode,
        stdout=res.stdout,
        stderr=res.stderr,
        timed_out=False,
    )


def is_rate_limited(text: str) -> bool:
    """Checks whether diagnostics report a primary or secondary GitHub rate limit.

    Matches `RATE_LIMITED`, `graphql_rate_limit`, `API rate limit`, and
    `secondary rate limit`. Callers must pass stderr and the response body
    only, never the header block, whose `x-ratelimit-*` names would match.
    """
    lowered = text.lower()
    return any(token in lowered for token in _RATE_LIMIT_SUBSTRINGS)


_is_rate_limited = is_rate_limited


def split_http_response(raw: str) -> tuple[int, dict[str, str], str]:
    """Splits `gh api -i` output into status code, lowercase header dict, and body.

    When the output holds several header blocks (an interim `100 Continue` or a
    redirect printed before the final response), the last block wins, so the
    status, rate-limit headers, and body all describe the final response.
    """
    normalized = raw.replace("\r\n", "\n")
    parts = normalized.split("\n\n", 1)
    while len(parts) > 1 and parts[1].startswith("HTTP/"):
        parts = parts[1].split("\n\n", 1)
    header_block = parts[0]
    body = parts[1] if len(parts) > 1 else ""
    lines = [ln for ln in header_block.splitlines() if ln.strip()]
    if not lines:
        return 0, {}, body

    status_code = 0
    first_tokens = lines[0].split()
    if len(first_tokens) >= 2 and first_tokens[1].isdigit():
        status_code = int(first_tokens[1])

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status_code, headers, body


_split_http_response = split_http_response


@dataclass(frozen=True)
class GhResponse:
    """HTTP response from a REST call made through `gh api -i`.

    Attributes:
        status: HTTP status code.
        headers: Response headers with lowercase names.
        body: Response body; empty for 304 Not Modified.
    """

    status: int
    headers: dict[str, str]
    body: str


class BudgetDeferred(Exception):
    """Raised when a request must wait for more rate budget.

    Attributes:
        resource: Rate-limit resource the request would spend, such as "graphql".
        priority: Priority of the deferred request.
        detail: Human-readable reason.
    """

    def __init__(self, resource: str, priority: Priority, detail: str = "") -> None:
        """Initializes the exception.

        Args:
            resource: Rate-limit resource the request would spend.
            priority: Priority of the deferred request.
            detail: Human-readable reason; a default is built when empty.
        """
        self.resource = resource
        self.priority = priority
        self.detail = detail or _deferral_detail(resource, priority)
        super().__init__(self.detail)


class RateLimited(BudgetDeferred):
    """Raised when GitHub answered a request with a rate-limit error."""


class GhApiError(Exception):
    """Raised when a `gh api` call fails for a reason other than rate limits.

    Attributes:
        status: HTTP status code, or 0 when no response was received.
        detail: First diagnostic line from gh or the response.
        rate_limited: Whether the failure was due to a GitHub rate limit.
    """

    def __init__(self, status: int, detail: str, *, rate_limited: bool = False) -> None:
        """Initializes the exception.

        Args:
            status: HTTP status code, or 0 when unknown.
            detail: Diagnostic message.
            rate_limited: Whether GitHub reported a rate limit.
        """
        self.status = status
        self.detail = detail
        self.rate_limited = rate_limited
        super().__init__(f"HTTP {status}: {detail}")


class ApiCallStore(Protocol):
    """Sink for the `api_calls` audit log, implemented by `PackratStore`."""

    def record_api_call(
        self,
        *,
        worker: str,
        resource: str,
        cost: int | None,
        remaining: int | None,
        reset_at: int | None,
        status: int,
    ) -> None:
        """Records one GitHub API call."""
        ...


@dataclass(frozen=True)
class _ParsedOutput:
    """`gh api -i` output split into status, headers, and body."""

    status: int
    headers: dict[str, str]
    body: str


def _deferral_detail(resource: str, priority: Priority) -> str:
    """Builds the standard deferral reason for `resource` at `priority`."""
    return f"{resource} budget below {priority.name} threshold"


def _parse_output(res: GhCommandResult) -> _ParsedOutput:
    """Splits `gh api -i` stdout, detecting 304 responses gh reports as errors.

    Output that does not start with an HTTP status line is returned as the
    body with status 0 and no headers.
    """
    raw = res.stdout.lstrip()
    if raw.startswith("HTTP/"):
        status, headers, body = _split_http_response(raw)
    else:
        status, headers, body = 0, {}, res.stdout
    if status == 0 and "304 Not Modified" in f"{res.stdout}\n{res.stderr}":
        status = _NOT_MODIFIED
    return _ParsedOutput(status=status, headers=headers, body=body)


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    """Returns header `name` as an int, or None when missing or malformed."""
    try:
        return int(headers[name])
    except (KeyError, ValueError):
        return None


def _decode_json_object(body: str) -> dict[str, Any] | None:
    """Decodes `body` as a JSON object, returning None for anything else."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _graphql_cost(payload: dict[str, Any] | None) -> int | None:
    """Returns `data.rateLimit.cost` from a GraphQL payload when present."""
    data = payload.get("data") if payload else None
    rate = data.get("rateLimit") if isinstance(data, dict) else None
    cost = rate.get("cost") if isinstance(rate, dict) else None
    if isinstance(cost, int) and not isinstance(cost, bool):
        return cost
    return None


def _graphql_field_args(variables: Mapping[str, Any]) -> list[str]:
    """Converts GraphQL variables into `gh api` `-f` / `-F` field flags.

    Strings use `-f` (raw). Ints, bools (as `true`/`false`), and None (as
    `null`) use `-F` (typed).

    Raises:
        TypeError: If a value has any other type.
    """
    args: list[str] = []
    for key, value in variables.items():
        if isinstance(value, str):
            args += ["-f", f"{key}={value}"]
        elif isinstance(value, bool):
            args += ["-F", f"{key}={'true' if value else 'false'}"]
        elif isinstance(value, int):
            args += ["-F", f"{key}={value}"]
        elif value is None:
            args += ["-F", f"{key}=null"]
        else:
            msg = (
                f"Unsupported GraphQL variable type for {key!r}: {type(value).__name__}"
            )
            raise TypeError(msg)
    return args


def _first_line(*candidates: str) -> str:
    """Returns the first non-empty line among `candidates`."""
    for text in candidates:
        for line in (text or "").splitlines():
            if line.strip():
                return line.strip()[:500]
    return ""


def _graphql_error_text(payload: dict[str, Any] | None, body: str) -> str:
    """Returns the GraphQL `errors` as text, or the raw body if it is not JSON."""
    if payload is None:
        return body
    errors = payload.get("errors")
    return json.dumps(errors) if errors else ""


def _graphql_failed(res: GhCommandResult, parsed: _ParsedOutput, payload: Any) -> bool:
    """Returns True when a GraphQL call reported any failure signal."""
    if res.returncode != 0 or res.timed_out or parsed.status >= 400:
        return True
    return payload is None or bool(payload.get("errors")) or payload.get("data") is None


def _is_api_args(args: Sequence[str]) -> bool:
    """Returns True for `gh api ...` invocations."""
    return bool(args) and args[0] == "api"


def _runner_resource(args: Sequence[str]) -> str:
    """Infers the rate-limit resource of a `gh api` invocation."""
    positional = [a for a in args[:3] if a not in _INCLUDE_FLAGS]
    return "graphql" if positional[:2] == ["api", "graphql"] else "core"


def _with_include_flag(args: Sequence[str]) -> list[str]:
    """Returns `args` with `-i` inserted after `api` when no include flag is present."""
    out = list(args)
    if not any(a in _INCLUDE_FLAGS for a in out):
        out.insert(1, "-i")
    return out


def _runner_call_cost(resource: str, parsed: _ParsedOutput) -> int | None:
    """Computes the API call cost for an `as_runner` response."""
    if resource == "graphql":
        match = _RATE_LIMIT_COST_RE.search(parsed.body)
        if match is not None:
            return int(match.group(1))
        if "rateLimit" not in parsed.body:
            return None
        return _graphql_cost(_decode_json_object(parsed.body))
    return 0 if parsed.status == _NOT_MODIFIED else 1


def _invoke_runner(
    runner: GhRunner, args: Sequence[str], timeout: int
) -> GhCommandResult:
    """Invokes `runner(args, timeout)`, converting `GhApiError` into `GhCommandResult`."""
    try:
        return runner(args, timeout)
    except GhApiError as exc:
        kind: GhErrorKind = "rate_limited" if exc.rate_limited else "command_error"
        return GhCommandResult(
            returncode=1, stdout="", stderr=exc.detail, error_kind=kind
        )


def _tag_runner_result(res: GhCommandResult, parsed: _ParsedOutput) -> GhCommandResult:
    """Attaches `error_kind='rate_limited'` when a failed runner response hits a rate limit."""
    if (
        res.returncode != 0
        and res.error_kind is None
        and is_rate_limited(f"{res.stderr}\n{parsed.body}")
    ):
        return dataclasses.replace(res, error_kind="rate_limited")
    return res


class GhTransport:
    """Budgeted, audited access to the GitHub API through the `gh` CLI."""

    def __init__(
        self,
        budget: RateBudget,
        store: ApiCallStore,
        runner: GhRunner | None = None,
        *,
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        """Initializes the transport.

        Args:
            budget: Shared rate budget consulted before and updated after calls.
            store: Sink for `api_calls` audit rows.
            runner: Runs `gh` arguments with a timeout; defaults to
                `github_sync.run_gh_command`.
            timeout_sec: Per-call timeout passed to the runner.
        """
        self._budget = budget
        self._store = store
        self._runner: GhRunner = runner or run_gh_command
        self._timeout_sec = timeout_sec

    def _ensure_allowed(self, resource: str, priority: Priority) -> None:
        """Raises BudgetDeferred when the budget denies `resource` at `priority`."""
        if not self._budget.allows(resource, priority):
            raise BudgetDeferred(resource, priority)

    def _record(
        self,
        parsed: _ParsedOutput,
        *,
        worker: str,
        resource: str,
        cost: int | None,
    ) -> None:
        """Records the response headers in the budget and writes one api_calls row."""
        headers = parsed.headers
        self._budget.record(headers, worker)
        self._store.record_api_call(
            worker=worker,
            resource=headers.get("x-ratelimit-resource") or resource,
            cost=cost,
            remaining=_int_header(headers, "x-ratelimit-remaining"),
            reset_at=_int_header(headers, "x-ratelimit-reset"),
            status=parsed.status,
        )

    def graphql(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        worker: str,
        priority: Priority,
    ) -> dict[str, Any]:
        """Runs one GraphQL query.

        Args:
            query: GraphQL document.
            variables: Query variables; str, int, bool, or None values.
            worker: Name of the calling worker, for the audit log.
            priority: Scheduling priority of the request.

        Returns:
            The decoded payload, with `data` and possibly partial `errors`.

        Raises:
            BudgetDeferred: The budget denies the request; gh was not run.
            RateLimited: GitHub answered with a rate-limit error.
            GhApiError: gh failed without usable data, the body was not JSON,
                or `data` was null.
            TypeError: A variable has an unsupported type.
        """
        field_args = _graphql_field_args(variables)
        self._ensure_allowed("graphql", priority)
        args = ["api", "-i", "graphql", *field_args, "-f", f"query={query}"]
        res = self._runner(args, self._timeout_sec)
        parsed = _parse_output(res)
        payload = _decode_json_object(parsed.body)
        self._record(
            parsed, worker=worker, resource="graphql", cost=_graphql_cost(payload)
        )
        if not _graphql_failed(res, parsed, payload):
            return payload  # type: ignore[return-value]
        error_text = _graphql_error_text(payload, parsed.body)
        if _is_rate_limited(f"{res.stderr}\n{error_text}"):
            raise RateLimited("graphql", priority, _first_line(res.stderr, error_text))
        if (
            payload is not None
            and payload.get("data") is not None
            and not res.timed_out
        ):
            return payload
        raise GhApiError(
            parsed.status, _first_line(res.stderr, error_text, "gh api graphql failed")
        )

    def rest_get(
        self,
        path: str,
        *,
        worker: str,
        priority: Priority,
        etag: str | None = None,
        if_modified_since: str | None = None,
        resource: str = "core",
    ) -> GhResponse:
        """Runs one conditional REST GET.

        Args:
            path: API path, such as "repos/o/r/events".
            worker: Name of the calling worker, for the audit log.
            priority: Scheduling priority of the request.
            etag: Value for `If-None-Match`, when known.
            if_modified_since: Value for `If-Modified-Since`, when known.
            resource: Rate-limit resource the request spends.

        Returns:
            The response; status 304 carries an empty body that was not parsed.

        Raises:
            BudgetDeferred: The budget denies the request; gh was not run.
            RateLimited: GitHub answered with a rate-limit error.
            GhApiError: Any other non-2xx response or gh failure.
        """
        self._ensure_allowed(resource, priority)
        args = ["api", "-i", path]
        if etag:
            args += ["-H", f"If-None-Match: {etag}"]
        if if_modified_since:
            args += ["-H", f"If-Modified-Since: {if_modified_since}"]
        res = self._runner(args, self._timeout_sec)
        parsed = _parse_output(res)
        cost = 0 if parsed.status == _NOT_MODIFIED else 1
        self._record(parsed, worker=worker, resource=resource, cost=cost)
        if parsed.status == _NOT_MODIFIED:
            return GhResponse(status=_NOT_MODIFIED, headers=parsed.headers, body="")
        if 200 <= parsed.status < 300 and not res.timed_out:
            return GhResponse(
                status=parsed.status, headers=parsed.headers, body=parsed.body
            )
        if _is_rate_limited(f"{res.stderr}\n{parsed.body}"):
            raise RateLimited(resource, priority, _first_line(res.stderr, parsed.body))
        raise GhApiError(
            parsed.status, _first_line(res.stderr, parsed.body, f"GET {path} failed")
        )

    def as_runner(self, worker: str, priority: Priority) -> GhRunner:
        """Adapts the transport to the `GhRunner` shape used by `github_sync`.

        For `gh api` invocations the adapter checks the budget first. When it
        denies the request, gh is not run and the result carries a
        `packrat: deferred_rate_limit (...)` stderr that `github_sync`
        classifies as rate limited. Otherwise it inserts `-i` when absent,
        runs gh, records the budget and an api_calls row, and returns the raw
        result unchanged. Non-`api` invocations, such as `gh auth token`, are
        passed through without budgeting.

        Args:
            worker: Name of the calling worker, for the audit log.
            priority: Scheduling priority of every request made through it.

        Returns:
            A runner callable `(args, timeout) -> GhCommandResult`.
        """

        def run(
            args: Sequence[str], timeout: int = DEFAULT_TIMEOUT_SEC
        ) -> GhCommandResult:
            if not _is_api_args(args):
                return self._runner(args, timeout)
            resource = _runner_resource(args)
            if not self._budget.allows(resource, priority):
                return GhCommandResult(
                    returncode=1,
                    stdout="",
                    stderr=f"packrat: deferred_rate_limit ({_deferral_detail(resource, priority)})",
                    error_kind="deferred_rate_limit",
                )
            res = _invoke_runner(self._runner, _with_include_flag(args), timeout)
            parsed = _parse_output(res)
            cost = _runner_call_cost(resource, parsed)
            self._record(parsed, worker=worker, resource=resource, cost=cost)
            return _tag_runner_result(res, parsed)

        run._uses_gh_cli = self._runner is run_gh_command
        run.is_budgeted_runner = True
        return run
