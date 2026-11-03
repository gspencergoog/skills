# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Recency-decayed reviewer scoring and Markdown/JSON report formatters.

Aggregates historical git commits, git blame line ownership, formal pull request
reviews, and inline review thread discussions into unified candidate profiles
through ``IdentityResolver``, applies per-file and per-directory caps with a
180-day half-life decay, and formats recommendation reports.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from reviewer_models import (
    SECONDS_PER_DAY,
    BlameLineTouch,
    GitCommitTouch,
    IdentityResolver,
    PackratLoadResult,
    PackratReviewEvent,
    PackratThreadEvent,
    RankedReviewer,
    compute_recency_weight,
    is_multi_token_name,
    normalize_scope_path,
)

DIRECT_FILE_WEIGHT: float = 10.0
DIRECT_COMMIT_WEIGHT: float = 2.0
DIRECT_COMMIT_CAP: float = 30.0
BLAME_LINE_WEIGHT: float = 0.5
BLAME_FILE_CAP: float = 12.0
DIRECT_APPROVAL_WEIGHT: float = 4.0
DIRECT_COMMENT_WEIGHT: float = 2.5
DIRECT_REVIEW_CAP: float = 24.0
DIRECT_THREAD_WEIGHT: float = 3.0
DIRECT_THREAD_CAP: float = 18.0

DIRECTORY_MATCH_WEIGHT: float = 2.0
DIRECTORY_COMMIT_WEIGHT: float = 0.4
DIRECTORY_COMMIT_CAP: float = 8.0
DIRECTORY_APPROVAL_WEIGHT: float = 1.2
DIRECTORY_COMMENT_WEIGHT: float = 0.75
DIRECTORY_REVIEW_CAP: float = 10.0
DIRECTORY_THREAD_WEIGHT: float = 1.0
DIRECTORY_THREAD_CAP: float = 8.0

MEGA_PR_FILE_THRESHOLD: int = 30
MEGA_PR_FOCUS_FACTOR: float = 0.35

RECENCY_BONUS_MAX: float = 10.0
RECENCY_BONUS_WINDOW_DAYS: float = 30.0

MAX_PRIMARY_REVIEWERS: int = 4
MAX_PRIMARY_OVERFLOW: int = 4
MAX_SECONDARY_REVIEWERS: int = 6


@dataclass
class _FileAccumulator:
    """Uncapped per-file signal totals for a single reviewer candidate."""

    commit_score: float = 0.0
    blame_score: float = 0.0
    review_score: float = 0.0
    thread_score: float = 0.0
    commits: int = 0
    blame_lines: int = 0
    reviews: int = 0
    threads: int = 0
    timestamps: list[int] = field(default_factory=list)

    def merge_from(self, other: _FileAccumulator) -> None:
        """Adds uncapped totals and timestamps from ``other``."""
        self.commit_score += other.commit_score
        self.blame_score += other.blame_score
        self.review_score += other.review_score
        self.thread_score += other.thread_score
        self.commits += other.commits
        self.blame_lines += other.blame_lines
        self.reviews += other.reviews
        self.threads += other.threads
        self.timestamps.extend(other.timestamps)

    def capped_score(self) -> float:
        """Returns the sum of capped per-signal scores for this file."""
        return (
            min(DIRECT_COMMIT_CAP, self.commit_score)
            + min(BLAME_FILE_CAP, self.blame_score)
            + min(DIRECT_REVIEW_CAP, self.review_score)
            + min(DIRECT_THREAD_CAP, self.thread_score)
        )


@dataclass
class _DirAccumulator:
    """Uncapped per-directory signal totals for a single reviewer candidate."""

    commit_score: float = 0.0
    review_score: float = 0.0
    thread_score: float = 0.0
    commits: int = 0
    reviews: int = 0
    threads: int = 0
    timestamps: list[int] = field(default_factory=list)

    def merge_from(self, other: _DirAccumulator) -> None:
        """Adds uncapped totals and timestamps from ``other``."""
        self.commit_score += other.commit_score
        self.review_score += other.review_score
        self.thread_score += other.thread_score
        self.commits += other.commits
        self.reviews += other.reviews
        self.threads += other.threads
        self.timestamps.extend(other.timestamps)

    def capped_score(self) -> float:
        """Returns the sum of capped per-signal scores for this directory."""
        return (
            min(DIRECTORY_COMMIT_CAP, self.commit_score)
            + min(DIRECTORY_REVIEW_CAP, self.review_score)
            + min(DIRECTORY_THREAD_CAP, self.thread_score)
        )


@dataclass
class _CandidateBucket:
    """Aggregates direct and directory touches for one canonical identity."""

    canonical_key: str
    is_github_login: bool
    emails: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    direct_files: dict[str, _FileAccumulator] = field(default_factory=dict)
    directories: dict[str, _DirAccumulator] = field(default_factory=dict)

    def record_contact(self, email: str, name: str) -> None:
        """Records non-empty git author email and display name strings."""
        clean_email = email.strip()
        clean_name = name.strip()
        if clean_email:
            self.emails.add(clean_email)
        if clean_name:
            self.names.add(clean_name)

    def merge_from(self, other: _CandidateBucket) -> None:
        """Merges all contact metadata and file/directory touches from ``other``."""
        self.emails.update(other.emails)
        self.names.update(other.names)
        for file_path, acc in other.direct_files.items():
            self.direct_files.setdefault(file_path, _FileAccumulator()).merge_from(acc)
        for dir_path, acc in other.directories.items():
            self.directories.setdefault(dir_path, _DirAccumulator()).merge_from(acc)


def _parse_changed_scopes(changed_files: Sequence[str]) -> tuple[set[str], set[str]]:
    """Normalizes changed file paths and extracts their immediate parent directories."""
    normalized_files = {
        normalize_scope_path(path, False) for path in changed_files if path and path.strip()
    }
    parent_dirs = {os.path.dirname(path) for path in normalized_files if os.path.dirname(path)}
    return normalized_files, parent_dirs


def _is_in_scope(
    scope_path: str,
    is_directory: bool,
    changed_file_set: set[str],
    valid_parent_dirs: set[str],
) -> bool:
    """Returns whether an event scope matches a changed file or its parent directory."""
    return scope_path in valid_parent_dirs if is_directory else scope_path in changed_file_set


def _get_or_create_bucket(
    buckets: dict[str, _CandidateBucket],
    canonical_key: str,
    is_github_login: bool,
) -> _CandidateBucket:
    """Fetches or initializes the accumulator bucket for ``canonical_key``."""
    existing = buckets.get(canonical_key)
    if existing is not None:
        if is_github_login and not existing.is_github_login:
            existing.is_github_login = True
        return existing
    created = _CandidateBucket(canonical_key=canonical_key, is_github_login=is_github_login)
    buckets[canonical_key] = created
    return created


def _review_focus_factor(pr_changed_files: int) -> float:
    """Returns the mega-PR focus multiplier for a historical pull request."""
    return 1.0 if pr_changed_files <= MEGA_PR_FILE_THRESHOLD else MEGA_PR_FOCUS_FACTOR


def _review_base_weight(state: str, is_directory: bool) -> float | None:
    """Maps a pull request review state to its base point weight, or None to skip."""
    normalized = state.strip().upper()
    if normalized in ("APPROVED", "CHANGES_REQUESTED"):
        return DIRECTORY_APPROVAL_WEIGHT if is_directory else DIRECT_APPROVAL_WEIGHT
    if normalized == "COMMENTED":
        return DIRECTORY_COMMENT_WEIGHT if is_directory else DIRECT_COMMENT_WEIGHT
    return None


def _ingest_commit_touches(
    buckets: dict[str, _CandidateBucket],
    commit_touches: Sequence[GitCommitTouch],
    changed_file_set: set[str],
    valid_parent_dirs: set[str],
    resolver: IdentityResolver,
    now_ts: int,
    gh_lookup: Callable[[str], str | None] | None,
) -> None:
    """Resolves and accumulates direct and parent-directory git commit touches."""
    for touch in commit_touches:
        if not _is_in_scope(
            touch.scope_path, touch.is_directory, changed_file_set, valid_parent_dirs
        ):
            continue
        resolved = resolver.resolve_git_identity(
            touch.sha, touch.email, touch.name, gh_lookup=gh_lookup
        )
        if resolved is None:
            continue
        canonical_key, is_gh = resolved
        bucket = _get_or_create_bucket(buckets, canonical_key, is_gh)
        bucket.record_contact(touch.email, touch.name)
        decay = compute_recency_weight(touch.timestamp, now_ts)
        if touch.is_directory:
            dir_acc = bucket.directories.setdefault(touch.scope_path, _DirAccumulator())
            dir_acc.commit_score += DIRECTORY_COMMIT_WEIGHT * decay
            dir_acc.commits += 1
            dir_acc.timestamps.append(touch.timestamp)
        else:
            file_acc = bucket.direct_files.setdefault(touch.scope_path, _FileAccumulator())
            file_acc.commit_score += DIRECT_COMMIT_WEIGHT * decay
            file_acc.commits += 1
            file_acc.timestamps.append(touch.timestamp)


def _ingest_blame_touches(
    buckets: dict[str, _CandidateBucket],
    blame_touches: Sequence[BlameLineTouch],
    changed_file_set: set[str],
    resolver: IdentityResolver,
    now_ts: int,
    gh_lookup: Callable[[str], str | None] | None,
) -> None:
    """Resolves and accumulates git blame line ownership on changed files."""
    for touch in blame_touches:
        if touch.line_count <= 0 or touch.file_path not in changed_file_set:
            continue
        resolved = resolver.resolve_git_identity(
            touch.sha, touch.email, touch.name, gh_lookup=gh_lookup
        )
        if resolved is None:
            continue
        canonical_key, is_gh = resolved
        bucket = _get_or_create_bucket(buckets, canonical_key, is_gh)
        bucket.record_contact(touch.email, touch.name)
        decay = compute_recency_weight(touch.timestamp, now_ts)
        file_acc = bucket.direct_files.setdefault(touch.file_path, _FileAccumulator())
        file_acc.blame_score += BLAME_LINE_WEIGHT * decay * touch.line_count
        file_acc.blame_lines += touch.line_count
        file_acc.timestamps.append(touch.timestamp)


def _ingest_review_events(
    buckets: dict[str, _CandidateBucket],
    review_events: Sequence[PackratReviewEvent],
    changed_file_set: set[str],
    valid_parent_dirs: set[str],
    resolver: IdentityResolver,
    now_ts: int,
) -> None:
    """Resolves and accumulates historical pull request review events."""
    for event in review_events:
        if not _is_in_scope(
            event.scope_path, event.is_directory, changed_file_set, valid_parent_dirs
        ):
            continue
        base_weight = _review_base_weight(event.state, event.is_directory)
        if base_weight is None:
            continue
        canonical_key = resolver.resolve_github_login(event.reviewer_login)
        if canonical_key is None:
            continue
        bucket = _get_or_create_bucket(buckets, canonical_key, True)
        points = (
            base_weight
            * compute_recency_weight(event.timestamp, now_ts)
            * _review_focus_factor(event.pr_changed_files)
        )
        if event.is_directory:
            dir_acc = bucket.directories.setdefault(event.scope_path, _DirAccumulator())
            dir_acc.review_score += points
            dir_acc.reviews += 1
            dir_acc.timestamps.append(event.timestamp)
        else:
            file_acc = bucket.direct_files.setdefault(event.scope_path, _FileAccumulator())
            file_acc.review_score += points
            file_acc.reviews += 1
            file_acc.timestamps.append(event.timestamp)


def _ingest_thread_events(
    buckets: dict[str, _CandidateBucket],
    thread_events: Sequence[PackratThreadEvent],
    changed_file_set: set[str],
    valid_parent_dirs: set[str],
    resolver: IdentityResolver,
    now_ts: int,
) -> None:
    """Resolves and accumulates historical inline review thread events."""
    for event in thread_events:
        if not _is_in_scope(
            event.scope_path, event.is_directory, changed_file_set, valid_parent_dirs
        ):
            continue
        canonical_key = resolver.resolve_github_login(event.reviewer_login)
        if canonical_key is None:
            continue
        bucket = _get_or_create_bucket(buckets, canonical_key, True)
        decay = compute_recency_weight(event.timestamp, now_ts)
        if event.is_directory:
            dir_acc = bucket.directories.setdefault(event.scope_path, _DirAccumulator())
            dir_acc.thread_score += DIRECTORY_THREAD_WEIGHT * decay
            dir_acc.threads += 1
            dir_acc.timestamps.append(event.timestamp)
        else:
            file_acc = bucket.direct_files.setdefault(event.scope_path, _FileAccumulator())
            file_acc.thread_score += DIRECT_THREAD_WEIGHT * decay
            file_acc.threads += 1
            file_acc.timestamps.append(event.timestamp)


def _merge_late_linked_git_buckets(
    buckets: dict[str, _CandidateBucket],
    resolver: IdentityResolver,
) -> None:
    """Merges ``git:<email>`` buckets when another commit linked that email to one login."""
    email_to_gh_keys: dict[str, set[str]] = {}
    for key, bucket in buckets.items():
        if not bucket.is_github_login:
            continue
        for email in bucket.emails | resolver.observed_emails(key):
            email_to_gh_keys.setdefault(email.lower(), set()).add(key)

    for key in [k for k, b in buckets.items() if not b.is_github_login and k.startswith("git:")]:
        matched_keys = email_to_gh_keys.get(key[4:].lower(), set())
        if len(matched_keys) == 1:
            target_key = next(iter(matched_keys))
            buckets[target_key].merge_from(buckets.pop(key))


def _compute_recency_bonus(timestamps: Iterable[int], now_ts: int) -> float:
    """Computes ``max(0.0, 10.0 - min_days_ago / 30.0)`` across positive timestamps."""
    if now_ts <= 0:
        return 0.0
    positive_ts = [ts for ts in timestamps if ts > 0]
    if not positive_ts:
        return 0.0
    min_days_ago = max(0.0, (now_ts - max(positive_ts)) / SECONDS_PER_DAY)
    return max(0.0, RECENCY_BONUS_MAX - (min_days_ago / RECENCY_BONUS_WINDOW_DAYS))


def _select_display_name(bucket: _CandidateBucket, resolver: IdentityResolver) -> str:
    """Selects the preferred human display name for ``bucket``."""
    names = bucket.names | resolver.observed_names(bucket.canonical_key)
    if not names:
        return resolver.preferred_display_name(bucket.canonical_key)
    return min(
        names,
        key=lambda item: (not is_multi_token_name(item), -len(item), item),
    )


def _select_email(bucket: _CandidateBucket, resolver: IdentityResolver) -> str:
    """Selects the preferred contact email address for ``bucket``."""
    emails = bucket.emails | resolver.observed_emails(bucket.canonical_key)
    if not emails:
        return resolver.preferred_email(bucket.canonical_key)
    return min(
        emails,
        key=lambda item: (
            "noreply.github.com" in item.lower(),
            item.lower(),
            item != item.lower(),
            item,
        ),
    )


def _active_directories(bucket: _CandidateBucket) -> dict[str, _DirAccumulator]:
    """Returns directory accumulators whose paths are not covered by direct files."""
    covered_dirs = {os.path.dirname(path) for path in bucket.direct_files if os.path.dirname(path)}
    return {
        dir_path: acc
        for dir_path, acc in bucket.directories.items()
        if dir_path not in covered_dirs
    }


def _collect_active_timestamps(
    direct_files: dict[str, _FileAccumulator],
    active_dirs: dict[str, _DirAccumulator],
) -> list[int]:
    """Collects timestamps from all direct file and non-skipped directory touches."""
    timestamps: list[int] = []
    for file_acc in direct_files.values():
        timestamps.extend(file_acc.timestamps)
    for dir_acc in active_dirs.values():
        timestamps.extend(dir_acc.timestamps)
    return timestamps


def _sum_file_counts(
    direct_files: dict[str, _FileAccumulator],
) -> tuple[int, int, int, int]:
    """Sums ``(commits, blame_lines, reviews, threads)`` across direct files."""
    commits = 0
    blame = 0
    reviews = 0
    threads = 0
    for acc in direct_files.values():
        commits += acc.commits
        blame += acc.blame_lines
        reviews += acc.reviews
        threads += acc.threads
    return commits, blame, reviews, threads


def _sum_dir_counts(active_dirs: dict[str, _DirAccumulator]) -> tuple[int, int, int]:
    """Sums ``(commits, reviews, threads)`` across active parent directories."""
    commits = 0
    reviews = 0
    threads = 0
    for acc in active_dirs.values():
        commits += acc.commits
        reviews += acc.reviews
        threads += acc.threads
    return commits, reviews, threads


def _finalize_candidate(
    bucket: _CandidateBucket,
    resolver: IdentityResolver,
    now_ts: int,
) -> RankedReviewer | None:
    """Scores a candidate bucket with per-file/per-directory caps and ``<dir>/*`` skipping."""
    active_dirs = _active_directories(bucket)
    if not bucket.direct_files and not active_dirs:
        return None

    direct_files_count = len(bucket.direct_files)
    direct_points = sum(acc.capped_score() for acc in bucket.direct_files.values())
    dir_points = sum(acc.capped_score() for acc in active_dirs.values())
    active_timestamps = _collect_active_timestamps(bucket.direct_files, active_dirs)

    total_score = (
        DIRECT_FILE_WEIGHT * direct_files_count
        + direct_points
        + DIRECTORY_MATCH_WEIGHT * len(active_dirs)
        + dir_points
        + _compute_recency_bonus(active_timestamps, now_ts)
    )
    direct_commits, blame_lines, direct_reviews, direct_threads = _sum_file_counts(
        bucket.direct_files
    )
    dir_commits, dir_reviews, dir_threads = _sum_dir_counts(active_dirs)
    matched_files = tuple(
        sorted(bucket.direct_files) + sorted(f"{dir_path}/*" for dir_path in active_dirs)
    )
    return RankedReviewer(
        canonical_key=bucket.canonical_key,
        username=bucket.canonical_key if bucket.is_github_login else "",
        is_github_login=bucket.is_github_login,
        display_name=_select_display_name(bucket, resolver),
        email=_select_email(bucket, resolver),
        score=total_score,
        direct_files_count=direct_files_count,
        direct_commits=direct_commits,
        blame_lines=blame_lines,
        direct_reviews=direct_reviews,
        direct_threads=direct_threads,
        directory_commits=dir_commits,
        directory_reviews=dir_reviews,
        directory_threads=dir_threads,
        files=matched_files,
    )


def _candidate_sort_key(candidate: RankedReviewer) -> tuple[float, int, int, str]:
    """Returns the deterministic descending sort tuple for ``candidate``."""
    return (
        -round(candidate.score, 4),
        -(candidate.direct_reviews + candidate.direct_threads),
        -candidate.direct_commits,
        candidate.canonical_key,
    )


def rank_reviewers(
    changed_files: Sequence[str],
    commit_touches: Sequence[GitCommitTouch],
    blame_touches: Sequence[BlameLineTouch],
    packrat_result: PackratLoadResult,
    resolver: IdentityResolver,
    *,
    now_ts: int,
    gh_lookup: Callable[[str], str | None] | None = None,
) -> tuple[list[RankedReviewer], list[RankedReviewer]]:
    """Ranks Primary and Secondary reviewer candidates across git and packrat signals.

    Args:
        changed_files: Repository-relative file paths modified by the change.
        commit_touches: Direct and parent-directory git commit records.
        blame_touches: Aggregated git blame line ownership records.
        packrat_result: Historical pull request reviews and inline threads from packrat.
        resolver: Identity resolver for merging GitHub logins and git authors.
        now_ts: Reference Unix epoch timestamp in seconds for recency decay.
        gh_lookup: Optional callback mapping a commit SHA to a GitHub login.

    Returns:
        Tuple ``(primary, secondary)`` of ranked ``RankedReviewer`` objects.
    """
    changed_file_set, valid_parent_dirs = _parse_changed_scopes(changed_files)
    if not changed_file_set:
        return [], []

    buckets: dict[str, _CandidateBucket] = {}
    _ingest_commit_touches(
        buckets, commit_touches, changed_file_set, valid_parent_dirs, resolver, now_ts, gh_lookup
    )
    _ingest_blame_touches(buckets, blame_touches, changed_file_set, resolver, now_ts, gh_lookup)
    _ingest_review_events(
        buckets, packrat_result.review_events, changed_file_set, valid_parent_dirs, resolver, now_ts
    )
    _ingest_thread_events(
        buckets, packrat_result.thread_events, changed_file_set, valid_parent_dirs, resolver, now_ts
    )
    _merge_late_linked_git_buckets(buckets, resolver)

    ranked: list[RankedReviewer] = []
    for key, bucket in buckets.items():
        if resolver.is_excluded(key, bucket.emails, bucket.names):
            continue
        candidate = _finalize_candidate(bucket, resolver, now_ts)
        if candidate is not None:
            ranked.append(candidate)

    ranked.sort(key=_candidate_sort_key)
    all_primary = [cand for cand in ranked if cand.direct_files_count > 0]
    dir_only = [cand for cand in ranked if cand.direct_files_count == 0]

    primary = all_primary[:MAX_PRIMARY_REVIEWERS]
    overflow = all_primary[MAX_PRIMARY_REVIEWERS : MAX_PRIMARY_REVIEWERS + MAX_PRIMARY_OVERFLOW]
    secondary = sorted(dir_only + overflow, key=_candidate_sort_key)[:MAX_SECONDARY_REVIEWERS]
    return primary, secondary


def _format_reviewer_cell(candidate: RankedReviewer) -> str:
    """Formats a table cell as ``@login`` for GitHub users or ``Name (git:<email>)``."""
    if candidate.is_github_login:
        if candidate.display_name and candidate.display_name.lower() != candidate.username.lower():
            return f"**@{candidate.username}** ({candidate.display_name})"
        return f"**@{candidate.username}**"
    raw_email = candidate.canonical_key.removeprefix("git:")
    if candidate.display_name and candidate.display_name.lower() != raw_email.lower():
        return f"{candidate.display_name} ({candidate.canonical_key})"
    return candidate.canonical_key


def _format_matched_files_cell(files: Sequence[str], max_items: int = 4) -> str:
    """Formats matched file or ``<dir>/*`` paths for a Markdown table row."""
    shown = ", ".join(f"`{path}`" for path in files[:max_items])
    remaining = len(files) - max_items
    return f"{shown} (+{remaining} more)" if remaining > 0 else shown


def _render_reviewer_table(candidates: Sequence[RankedReviewer]) -> list[str]:
    """Renders a Markdown table of scored reviewer candidates."""
    lines = [
        "| Reviewer | Score | Reviews | Threads | Commits | Blame Lines | Matched Files |",
        "| :--- | ---: | ---: | ---: | ---: | ---: | :--- |",
    ]
    for cand in candidates:
        reviews = cand.direct_reviews + cand.directory_reviews
        threads = cand.direct_threads + cand.directory_threads
        commits = cand.direct_commits + cand.directory_commits
        files_cell = _format_matched_files_cell(cand.files)
        lines.append(
            f"| {_format_reviewer_cell(cand)} | {cand.score:.1f} | {reviews} "
            f"| {threads} | {commits} | {cand.blame_lines} | {files_cell} |"
        )
    return lines


def _format_pr_target_line(
    pr_number: int | None,
    pr_title: str,
    pr_author: str | None,
) -> str | None:
    """Formats the optional pull request target metadata line."""
    clean_title = pr_title.strip()
    clean_author = (pr_author or "").strip().lstrip("@")
    author_suffix = f" (by @{clean_author})" if clean_author else ""
    if pr_number is not None and clean_title:
        return f"**PR Target**: #{pr_number} {clean_title}{author_suffix}"
    if clean_title:
        return f"**PR Target**: {clean_title}{author_suffix}"
    if pr_number is not None:
        return f"**PR Target**: #{pr_number}{author_suffix}"
    return None


def _append_packrat_callouts(lines: list[str], packrat_result: PackratLoadResult) -> None:
    """Appends fallback notes or incomplete backfill warnings as Markdown callouts."""
    for raw_note in (packrat_result.fallback_note, packrat_result.backfill_warning):
        note = (raw_note or "").strip()
        if note:
            lines.append(note if note.startswith(">") else f"> {note}")
            lines.append("")


def _select_suggested_logins(
    primary: Sequence[RankedReviewer],
    secondary: Sequence[RankedReviewer],
) -> list[str]:
    """Selects GitHub-linked logins for ``gh pr edit --add-reviewer``."""
    gh_primary = [cand.username for cand in primary if cand.is_github_login and cand.username]
    gh_secondary = [cand.username for cand in secondary if cand.is_github_login and cand.username]
    selected: list[str] = []
    for login in gh_primary[:2] + gh_secondary[:1]:
        if login not in selected:
            selected.append(login)
    return selected


def _append_suggested_action(
    lines: list[str],
    primary: Sequence[RankedReviewer],
    secondary: Sequence[RankedReviewer],
    *,
    effective_repo: str | None,
    pr_number: int | None,
    gh_available: bool,
) -> None:
    """Appends the ``gh pr edit`` command block when GitHub logins are available."""
    if not gh_available:
        return
    suggested_logins = _select_suggested_logins(primary, secondary)
    if not suggested_logins:
        return
    pr_arg = str(pr_number) if pr_number is not None else "<pr-number>"
    repo_flag = f" -R {effective_repo}" if effective_repo else ""
    joined = ",".join(suggested_logins)
    lines.extend(
        [
            "",
            "### Suggested Action",
            "```bash",
            f"gh pr edit {pr_arg} --add-reviewer {joined}{repo_flag}",
            "```",
        ]
    )


def _append_report_header(
    lines: list[str],
    *,
    effective_repo: str | None,
    pr_number: int | None,
    effective_title: str,
    effective_author: str | None,
) -> None:
    """Appends repository and pull request metadata lines to ``lines``."""
    if effective_repo:
        lines.append(f"**Repository**: `{effective_repo}`")
    target_line = _format_pr_target_line(pr_number, effective_title, effective_author)
    if target_line:
        lines.append(target_line)
    if effective_repo or target_line:
        lines.append("")


def _append_reviewer_section(
    lines: list[str],
    heading: str,
    empty_message: str,
    candidates: Sequence[RankedReviewer],
) -> None:
    """Appends a primary or secondary reviewer section with a table or fallback text."""
    lines.append(heading)
    if candidates:
        lines.extend(_render_reviewer_table(candidates))
    else:
        lines.append(empty_message)


def format_markdown_report(
    changed_files: Sequence[str],
    primary: Sequence[RankedReviewer],
    secondary: Sequence[RankedReviewer],
    packrat_result: PackratLoadResult,
    *,
    repo: str | None = None,
    pr_number: int | None = None,
    pr_title: str = "",
    pr_author: str | None = None,
    gh_available: bool = True,
) -> str:
    """Formats the reviewer recommendation report as GitHub-flavored Markdown.

    Args:
        changed_files: Repository-relative changed file paths.
        primary: Ranked primary reviewer candidates.
        secondary: Ranked secondary and overflow reviewer candidates.
        packrat_result: Packrat load status and optional fallback/backfill notes.
        repo: Optional ``owner/repo`` slug override.
        pr_number: Optional target pull request number.
        pr_title: Optional target pull request title.
        pr_author: Optional target pull request author login.
        gh_available: Whether the ``gh`` CLI is installed for suggested actions.

    Returns:
        Rendered Markdown report text.
    """
    effective_repo = repo or packrat_result.repo_full_name
    effective_title = pr_title or packrat_result.target_pr_title or ""
    effective_author = pr_author or packrat_result.target_pr_author

    lines: list[str] = ["## Reviewer Recommendation Report", ""]
    _append_report_header(
        lines,
        effective_repo=effective_repo,
        pr_number=pr_number,
        effective_title=effective_title,
        effective_author=effective_author,
    )
    _append_packrat_callouts(lines, packrat_result)

    lines.append("### Changed Files")
    if changed_files:
        lines.extend(f"- `{path}`" for path in changed_files)
    else:
        lines.append("- _(none)_")
    lines.append("")

    _append_reviewer_section(
        lines, "### Primary Reviewers", "No primary candidates identified.", primary
    )
    lines.append("")
    _append_reviewer_section(
        lines,
        "### Secondary / Context Reviewers",
        "No secondary candidates identified.",
        secondary,
    )

    _append_suggested_action(
        lines,
        primary,
        secondary,
        effective_repo=effective_repo,
        pr_number=pr_number,
        gh_available=gh_available,
    )
    return "\n".join(lines)


def _serialize_reviewer(candidate: RankedReviewer) -> dict[str, object]:
    """Converts a ``RankedReviewer`` dataclass into a JSON-serializable dictionary."""
    return {
        "canonical_key": candidate.canonical_key,
        "username": candidate.username,
        "is_github_login": candidate.is_github_login,
        "display_name": candidate.display_name,
        "email": candidate.email,
        "score": round(candidate.score, 4),
        "direct_files_count": candidate.direct_files_count,
        "direct_commits": candidate.direct_commits,
        "blame_lines": candidate.blame_lines,
        "direct_reviews": candidate.direct_reviews,
        "direct_threads": candidate.direct_threads,
        "directory_commits": candidate.directory_commits,
        "directory_reviews": candidate.directory_reviews,
        "directory_threads": candidate.directory_threads,
        "files": list(candidate.files),
    }


def format_json_report(
    changed_files: Sequence[str],
    primary: Sequence[RankedReviewer],
    secondary: Sequence[RankedReviewer],
    packrat_result: PackratLoadResult,
    *,
    repo: str | None = None,
    pr_number: int | None = None,
) -> str:
    """Formats the reviewer recommendation report as indented JSON.

    Args:
        changed_files: Repository-relative changed file paths.
        primary: Ranked primary reviewer candidates.
        secondary: Ranked secondary and overflow reviewer candidates.
        packrat_result: Packrat load status and optional fallback/backfill notes.
        repo: Optional ``owner/repo`` slug override.
        pr_number: Optional target pull request number.

    Returns:
        Pretty-printed JSON report string.
    """
    effective_repo = repo or packrat_result.repo_full_name
    payload = {
        "repo": effective_repo,
        "pr_number": pr_number,
        "changed_files": list(changed_files),
        "packrat": {
            "status": packrat_result.status,
            "repo_full_name": packrat_result.repo_full_name,
            "prs_backfill_complete": packrat_result.prs_backfill_complete,
            "fallback_note": packrat_result.fallback_note,
            "backfill_warning": packrat_result.backfill_warning,
        },
        "primary": [_serialize_reviewer(cand) for cand in primary],
        "secondary": [_serialize_reviewer(cand) for cand in secondary],
        "suggested_reviewers": _select_suggested_logins(primary, secondary),
    }
    return json.dumps(payload, indent=2)
