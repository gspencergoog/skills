"""Test helpers for the /monitor skill: FakeGh runner, fixture loader, and snapshot builders."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from models import CheckRun, PrSnapshot, Thread

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

Matcher = Callable[[list[str]], bool] | str


class FakeGh:
    """Scripted ``GhRunner`` double that matches calls against predicates or substrings."""

    def __init__(self, responses: list[tuple[Matcher, int, str, str]] | None = None) -> None:
        self._responses: list[tuple[Matcher, int, str, str | Exception]] = list(responses or [])
        self.calls: list[tuple[list[str], str | None]] = []

    def add(
        self,
        matcher: Matcher,
        code: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._responses.append((matcher, code, stdout, stderr))

    def add_error(self, matcher: Matcher, exc: Exception) -> None:
        self._responses.append((matcher, 1, "", exc))

    def __call__(self, args: list[str], *, input_text: str | None = None) -> tuple[int, str, str]:
        self.calls.append((list(args), input_text))
        joined = " ".join(args)
        for idx, (matcher, code, stdout, stderr_or_exc) in enumerate(self._responses):
            matched = matcher(args) if callable(matcher) else (matcher in joined)
            if matched:
                self._responses.pop(idx)
                if isinstance(stderr_or_exc, Exception):
                    raise stderr_or_exc
                return code, stdout, stderr_or_exc
        raise AssertionError(f"Unexpected gh call: gh {joined}")


def load_fixture(name: str) -> str:
    """Reads a file from ``scripts/fixtures/<name>``."""
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def make_check(
    name: str = "test-suite",
    status: str = "COMPLETED",
    conclusion: str | None = "SUCCESS",
    required: bool = True,
    url: str | None = "https://github.com/acme/widget/actions/runs/100/job/200",
    run_id: int | None = 100,
    started_at: str | None = "2026-10-27T21:00:00Z",
    completed_at: str | None = "2026-10-27T21:02:00Z",
) -> CheckRun:
    """Constructs a ``CheckRun`` with defaults for unit tests."""
    return CheckRun(
        name=name,
        status=status,
        conclusion=conclusion,
        required=required,
        url=url,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
    )


def make_thread(
    thread_id: str = "PRRT_1",
    resolved: bool = False,
    last_author: str = "reviewer1",
    last_author_is_bot: bool = False,
    last_comment_at: str = "2026-10-27T21:05:00Z",
) -> Thread:
    """Constructs a ``Thread`` with defaults for unit tests."""
    return Thread(
        id=thread_id,
        resolved=resolved,
        last_author=last_author,
        last_author_is_bot=last_author_is_bot,
        last_comment_at=last_comment_at,
    )


def make_snapshot(**overrides: Any) -> PrSnapshot:
    """Constructs an open ``PrSnapshot`` with sensible defaults, applying ``overrides``."""
    number = int(overrides.get("number", 3339))
    repo = str(overrides.get("repo", "acme/widget"))
    defaults: dict[str, Any] = {
        "key": f"{repo}#{number}",
        "number": number,
        "repo": repo,
        "title": "feat: add decision planner format",
        "url": f"https://github.com/{repo}/pull/{number}",
        "author": "alice",
        "mine": True,
        "state": "OPEN",
        "draft": False,
        "head_sha": "aaa1111",
        "head_ref": "feat/decision-planner",
        "base_ref": "main",
        "last_committer": "alice",
        "rollup_state": "SUCCESS",
        "mergeable": "MERGEABLE",
        "merge_state": "BLOCKED",
        "review_decision": "REVIEW_REQUIRED",
        "requested_reviewers": ["bob"],
        "assignees": ["alice"],
        "approvals": [],
        "changes_requested": [],
        "checks": [make_check()],
        "threads": [],
        "auto_merge": False,
        "stack_parent": None,
        "stack_parent_state": None,
        "truncated": [],
        "updated_at": "2026-10-27T21:05:00Z",
    }
    defaults.update(overrides)
    return PrSnapshot(**defaults)
