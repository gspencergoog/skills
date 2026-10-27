"""GitHub CLI wrapper, GraphQL batch query builder, and rate-limit backoff."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Protocol

from models import parse_pr_ref

BATCH_SIZE = 10
MIN_POLL_BACKOFF_S = 30
MAX_POLL_BACKOFF_S = 600

PR_FIELDS_FRAGMENT = """
fragment PrFields on PullRequest {
  id
  number
  title
  url
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
    """Builds a single GraphQL document fetching ``viewer`` and aliased PRs."""
    if not keys:
        raise ValueError("build_query requires at least one PR key")
    aliases: list[str] = []
    for idx, key in enumerate(keys):
        repo, number = parse_pr_ref(key)
        owner, name = repo.split("/", 1)
        aliases.append(_alias_block(idx, owner, name, number))
    joined = "\n".join(aliases)
    return f"query {{\n  viewer {{\n    login\n  }}\n{joined}\n}}\n\n{PR_FIELDS_FRAGMENT}\n"


def graphql(
    runner: GhRunner, query: str, variables: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Executes a GraphQL query via ``gh api graphql`` and returns ``data``."""
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, val in (variables or {}).items():
        args.extend(["-F", f"{key}={val}"])
    code, stdout, stderr = runner(args)
    if code != 0:
        raise GhError(
            f"gh api graphql failed (exit {code}): {stderr.strip() or stdout.strip()}",
            code=code,
            stderr=stderr,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise GhError(f"Invalid JSON from gh api graphql: {exc}", code=1, stderr=stderr) from exc
    if payload.get("errors"):
        messages = "; ".join(str(err.get("message") or err) for err in payload["errors"])
        raise GhError(f"GraphQL error: {messages}", code=1, stderr=stderr)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise GhError("GraphQL response missing 'data' object", code=1, stderr=stderr)
    return data


def fetch_prs(runner: GhRunner, keys: list[str]) -> tuple[str, dict[str, dict[str, Any]]]:
    """Fetches PR GraphQL nodes in batches of 10, returning ``(viewer, raw_by_key)``."""
    if not keys:
        return "", {}
    viewer = ""
    results: dict[str, dict[str, Any]] = {}
    for start in range(0, len(keys), BATCH_SIZE):
        chunk = keys[start : start + BATCH_SIZE]
        data = graphql(runner, build_query(chunk))
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
