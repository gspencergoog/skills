"""GitHub CLI wrapper, GraphQL batch query builder, and rate-limit backoff."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from models import parse_pr_ref, pr_key

BATCH_SIZE = 10
MIN_POLL_BACKOFF_S = 30
MAX_POLL_BACKOFF_S = 600
_STATUS_LINE_RE = re.compile(r"^HTTP/[\d.]+\s+(\d{3})")

PR_FIELDS_FRAGMENT = """
fragment PrFields on PullRequest {
  id
  number
  title
  url
  repository {
    nameWithOwner
  }
  state
  isDraft
  merged
  mergedAt
  closedAt
  updatedAt
  author {
    login
    __typename
  }
  headRefOid
  headRefName
  baseRefName
  mergeable
  mergeStateStatus
  reviewDecision
  autoMergeRequest {
    enabledAt
  }
  reviewRequests(first: 20) {
    totalCount
    nodes {
      requestedReviewer {
        ... on User {
          login
        }
        ... on Team {
          slug
        }
      }
    }
  }
  assignees(first: 10) {
    nodes {
      login
    }
  }
  latestOpinionatedReviews(first: 30) {
    nodes {
      state
      author {
        login
      }
      submittedAt
    }
  }
  reviews(last: 20) {
    nodes {
      state
      submittedAt
      commit {
        oid
      }
      author {
        login
        __typename
      }
    }
  }
  reviewThreads(first: 100) {
    totalCount
    nodes {
      id
      isResolved
      comments(last: 1) {
        nodes {
          author {
            login
            __typename
          }
          createdAt
        }
      }
    }
  }
}
""".strip()


class GhError(Exception):
    """Raised when a ``gh`` command fails or returns invalid GraphQL data."""

    def __init__(
        self,
        message: str,
        code: int = 1,
        stderr: str = "",
        status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stderr = stderr
        self.status = status
        self.headers = headers or {}


class GhRunner(Protocol):
    """Callable interface for executing ``gh`` CLI commands."""

    def __call__(
        self, args: list[str], *, input_text: str | None = None
    ) -> tuple[int, str, str]: ...


class SubprocessGh:
    """Runs the real ``gh`` binary via ``subprocess.run`` with a timeout."""

    def __init__(self, timeout_s: int = 60) -> None:
        self.timeout_s = timeout_s

    def __call__(self, args: list[str], *, input_text: str | None = None) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["gh", *args],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GhError(
                f"gh command timed out after {self.timeout_s}s: gh {' '.join(args[:3])}",
                code=124,
                stderr=str(exc),
            ) from exc
        except FileNotFoundError as exc:
            raise GhError("gh CLI not found on PATH", code=127, stderr=str(exc)) from exc
        return proc.returncode, proc.stdout, proc.stderr


def _alias_block(index: int, owner: str, name: str, number: int) -> str:
    owner_lit = json.dumps(owner)
    name_lit = json.dumps(name)
    return f"""
  pr{index}: repository(owner: {owner_lit}, name: {name_lit}) {{
    pullRequest(number: {number}) {{
      ...PrFields
      commits(last: 1) {{
        nodes {{
          commit {{
            oid
            committedDate
            committer {{
              user {{
                login
              }}
            }}
            statusCheckRollup {{
              state
              contexts(first: 100) {{
                totalCount
                nodes {{
                  __typename
                  ... on CheckRun {{
                    name
                    status
                    conclusion
                    detailsUrl
                    startedAt
                    completedAt
                    isRequired(pullRequestNumber: {number})
                    checkSuite {{
                      workflowRun {{
                        databaseId
                      }}
                    }}
                  }}
                  ... on StatusContext {{
                    context
                    state
                    targetUrl
                    createdAt
                    isRequired(pullRequestNumber: {number})
                  }}
                }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}""".rstrip()


def build_query(keys: list[str]) -> str:
    """Builds a single GraphQL document fetching ``viewer``, ``rateLimit``, and aliased PRs."""
    if not keys:
        raise ValueError("build_query requires at least one PR key")
    aliases: list[str] = []
    for idx, key in enumerate(keys):
        repo, number = parse_pr_ref(key)
        owner, name = repo.split("/", 1)
        aliases.append(_alias_block(idx, owner, name, number))
    joined = "\n".join(aliases)
    return (
        "query {\n  viewer {\n    login\n  }\n"
        "  rateLimit {\n    cost\n    remaining\n    resetAt\n  }\n"
        f"{joined}\n}}\n\n{PR_FIELDS_FRAGMENT}\n"
    )


@dataclass(frozen=True)
class RateLimit:
    """Rate-limit state parsed from ``X-RateLimit-*`` headers and the ``rateLimit`` node.

    Every field is ``None`` when the response did not carry it.
    """

    resource: str | None = None
    limit: int | None = None
    remaining: int | None = None
    used: int | None = None
    reset_at: int | None = None
    """Epoch seconds from ``X-RateLimit-Reset``."""
    cost: int | None = None
    """Point cost of the query from the GraphQL ``rateLimit { cost }`` node."""


@dataclass(frozen=True)
class GraphqlResult:
    """Parsed ``gh api -i graphql`` response: the ``data`` object plus rate-limit details."""

    data: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    """Response headers with lowercased names."""
    rate_limit: RateLimit = field(default_factory=RateLimit)


def split_http_response(text: str) -> tuple[int | None, dict[str, str], str]:
    """Splits ``gh api -i`` output into ``(status, lowercase_headers, body)``.

    Output without a leading ``HTTP/`` status line is treated as a bare body, so the
    status is ``None`` and the headers are empty.
    """
    normalized = text.replace("\r\n", "\n")
    match = _STATUS_LINE_RE.match(normalized)
    if not match:
        return None, {}, text
    split_idx = normalized.find("\n\n")
    head = normalized if split_idx == -1 else normalized[:split_idx]
    body = "" if split_idx == -1 else normalized[split_idx + 2 :]
    headers: dict[str, str] = {}
    for line in head.split("\n")[1:]:
        name, sep, value = line.partition(":")
        if sep and name.strip():
            headers[name.strip().lower()] = value.strip()
    return int(match.group(1)), headers, body


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def parse_rate_limit(headers: dict[str, str], data: dict[str, Any] | None = None) -> RateLimit:
    """Builds a ``RateLimit`` from lowercased headers and an optional GraphQL ``data`` object."""
    node = (data or {}).get("rateLimit")
    cost = _int_or_none(node.get("cost")) if isinstance(node, dict) else None
    remaining = _int_or_none(headers.get("x-ratelimit-remaining"))
    if remaining is None and isinstance(node, dict):
        remaining = _int_or_none(node.get("remaining"))
    return RateLimit(
        resource=headers.get("x-ratelimit-resource"),
        limit=_int_or_none(headers.get("x-ratelimit-limit")),
        remaining=remaining,
        used=_int_or_none(headers.get("x-ratelimit-used")),
        reset_at=_int_or_none(headers.get("x-ratelimit-reset")),
        cost=cost,
    )


def _log_cost(rate: RateLimit, log: Callable[[str], Any]) -> None:
    if rate.cost is None:
        return
    log(
        f"monitor: graphql cost={rate.cost} remaining={rate.remaining} "
        f"limit={rate.limit} reset_at={rate.reset_at}\n"
    )


def _graphql_error_status(payload: dict[str, Any], status: int | None) -> int | None:
    """Returns the HTTP status to report, mapping ``RATE_LIMITED`` errors to 403."""
    if status not in (None, 200):
        return status
    errors = payload.get("errors") or []
    if any(isinstance(err, dict) and err.get("type") == "RATE_LIMITED" for err in errors):
        return 403
    return status


def _failed_call_status(body: str, status: int | None) -> int | None:
    """Returns the status for a non-zero ``gh`` exit, reading GraphQL ``errors`` in the body.

    ``gh api graphql`` exits 1 when an HTTP 200 body carries ``errors``, so a
    ``RATE_LIMITED`` error only shows up in the body.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return status
    return _graphql_error_status(payload, status) if isinstance(payload, dict) else status


def _decode_graphql_body(
    body: str, status: int | None, headers: dict[str, str], stderr: str
) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GhError(
            f"Invalid JSON from gh api graphql: {exc}",
            code=1,
            stderr=stderr,
            status=status,
            headers=headers,
        ) from exc
    if not isinstance(payload, dict):
        raise GhError("GraphQL response is not a JSON object", code=1, stderr=stderr)
    if payload.get("errors"):
        messages = "; ".join(
            str(err.get("message") or err) if isinstance(err, dict) else str(err)
            for err in payload["errors"]
        )
        raise GhError(
            f"GraphQL error: {messages}",
            code=1,
            stderr=stderr,
            status=_graphql_error_status(payload, status),
            headers=headers,
        )
    return payload


def graphql(
    runner: GhRunner,
    query: str,
    variables: dict[str, Any] | None = None,
    log: Callable[[str], Any] | None = None,
) -> GraphqlResult:
    """Executes a GraphQL query via ``gh api -i graphql`` and returns ``data`` with rate limits.

    The ``-i`` header block is split from the body before decoding. Failures raise
    ``GhError`` carrying the HTTP status and headers, so ``RateLimitBackoff`` can honor
    ``X-RateLimit-Reset``. When the query selects ``rateLimit { cost }``, the cost is
    written to ``log`` (default: ``stderr``) once per call.
    """
    args = ["api", "-i", "graphql", "-f", f"query={query}"]
    for key, val in (variables or {}).items():
        args.extend(["-F", f"{key}={val}"])
    code, stdout, stderr = runner(args)
    status, headers, body = split_http_response(stdout)
    if code != 0:
        raise GhError(
            f"gh api graphql failed (exit {code}): {stderr.strip() or body.strip()}",
            code=code,
            stderr=stderr,
            status=_failed_call_status(body, status),
            headers=headers,
        )
    payload = _decode_graphql_body(body, status, headers, stderr)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GhError("GraphQL response missing 'data' object", code=1, stderr=stderr)
    rate = parse_rate_limit(headers, data)
    _log_cost(rate, log or sys.stderr.write)
    return GraphqlResult(data=data, headers=headers, rate_limit=rate)


def fetch_prs(runner: GhRunner, keys: list[str]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Fetches PR GraphQL nodes in batches of 10, returning ``(viewer, raw_by_key)``."""
    if not keys:
        return "", {}
    viewer = ""
    results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(keys), BATCH_SIZE):
        chunk = keys[start : start + BATCH_SIZE]
        data = graphql(runner, build_query(chunk)).data
        viewer_node = data.get("viewer") or {}
        if isinstance(viewer_node, dict) and viewer_node.get("login"):
            viewer = str(viewer_node["login"])
        for idx, key in enumerate(chunk):
            repo_node = data.get(f"pr{idx}")
            pr_node = repo_node.get("pullRequest") if isinstance(repo_node, dict) else None
            if not isinstance(pr_node, dict):
                raise GhError(f"Pull request not found: {key}", code=1)
            results[key] = pr_node
    return viewer, results


REVIEW_SEARCH_QUERY = """
query($reqQuery: String!, $revQuery: String!) {
  viewer {
    login
  }
  rateLimit {
    cost
    remaining
    resetAt
  }
  reviewRequested: search(type: ISSUE, query: $reqQuery, first: 60) {
    nodes {
      ... on PullRequest {
        number
        repository {
          nameWithOwner
        }
      }
    }
  }
  reviewed: search(type: ISSUE, query: $revQuery, first: 60) {
    nodes {
      ... on PullRequest {
        number
        repository {
          nameWithOwner
        }
      }
    }
  }
}
""".strip()


def _search_node_key(node: Any) -> str | None:
    if not isinstance(node, dict):
        return None
    number = node.get("number")
    repo_node = node.get("repository") if isinstance(node.get("repository"), dict) else {}
    repo_name = str(repo_node.get("nameWithOwner") or "").strip()
    if isinstance(number, int) and not isinstance(number, bool) and repo_name:
        return pr_key(repo_name, number)
    return None


def _collect_alias_keys(data: dict[str, Any], alias: str) -> list[str]:
    conn = data.get(alias)
    nodes = (conn.get("nodes") or []) if isinstance(conn, dict) else []
    out: list[str] = []
    for node in nodes:
        key = _search_node_key(node)
        if key:
            out.append(key)
    return out


def search_review_keys(
    runner: GhRunner,
    repo: str | None = None,
    *,
    backoff: RateLimitBackoff | None = None,
) -> tuple[str, list[str]]:
    """Queries GitHub search for open review-requested and reviewed PR keys."""
    cleaned_repo = (repo or "").strip()
    repo_suffix = f" repo:{cleaned_repo}" if cleaned_repo else ""
    variables = {
        "reqQuery": f"is:pr is:open review-requested:@me archived:false{repo_suffix}",
        "revQuery": f"is:pr is:open reviewed-by:@me -author:@me archived:false{repo_suffix}",
    }
    data = graphql(runner, REVIEW_SEARCH_QUERY, variables=variables).data
    if backoff is not None:
        backoff.reset()
    viewer_node = data.get("viewer") if isinstance(data.get("viewer"), dict) else {}
    viewer = str(viewer_node.get("login") or "").strip()
    seen: set[str] = set()
    keys: list[str] = []
    for key in [
        *_collect_alias_keys(data, "reviewRequested"),
        *_collect_alias_keys(data, "reviewed"),
    ]:
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return viewer, keys


class RateLimitBackoff:
    """Computes poll delays honoring ``Retry-After``, ``X-RateLimit-Reset``, and exponential backoff."""

    def __init__(
        self,
        min_delay_s: int = MIN_POLL_BACKOFF_S,
        max_delay_s: int = MAX_POLL_BACKOFF_S,
    ) -> None:
        self.min_delay_s = min_delay_s
        self.max_delay_s = max_delay_s
        self._failures = 0

    def reset(self) -> None:
        self._failures = 0

    @property
    def failures(self) -> int:
        return self._failures

    def next_delay(
        self,
        status: int | None = None,
        headers: dict[str, str] | None = None,
        now_epoch: float | None = None,
    ) -> int:
        """Returns the next backoff delay in seconds (never below ``min_delay_s``)."""
        self._failures += 1
        hdrs = {k.lower(): v for k, v in (headers or {}).items()}
        retry_after = hdrs.get("retry-after")
        if retry_after and retry_after.isdigit():
            return max(self.min_delay_s, min(self.max_delay_s, int(retry_after)))
        reset_at = hdrs.get("x-ratelimit-reset")
        remaining = hdrs.get("x-ratelimit-remaining")
        if (
            status in (403, 429)
            and reset_at
            and reset_at.isdigit()
            and now_epoch is not None
            and remaining in (None, "0")
        ):
            wait = int(reset_at) - int(now_epoch) + 2
            return max(self.min_delay_s, min(self.max_delay_s, wait))
        exp = self.min_delay_s * (2 ** (self._failures - 1))
        return max(self.min_delay_s, min(self.max_delay_s, exp))
