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

"""Shared domain models, recency helpers, and identity resolution for reviewer ranking.

Provides frozen dataclasses shared across the git signal collector, the packrat
SQLite signal loader, the scoring engine, and the CLI entrypoint, along with
timestamp parsing, 180-day half-life recency weighting, bot filtering, and the
five-step identity reconciliation ladder.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

HALF_LIFE_DAYS: float = 180.0
SECONDS_PER_DAY: float = 86400.0
MISSING_TIMESTAMP_WEIGHT: float = 0.5

KNOWN_BOT_LOGINS: frozenset[str] = frozenset(
    {
        "dependabot",
        "dependabot-preview",
        "renovate",
        "renovate-bot",
        "github-actions",
        "copilot-pull-request-reviewer",
        "noreply",
    }
)

KNOWN_BOT_EMAILS: frozenset[str] = frozenset(
    {
        "noreply@github.com",
        "actions@github.com",
        "github-actions@github.com",
    }
)

_NOREPLY_EMAIL_RE = re.compile(
    r"^(?:\d+\+)?([A-Za-z0-9-]+)@users\.noreply\.github\.com$",
    re.IGNORECASE,
)
_GIT_AUTHOR_HEADER_RE = re.compile(r"^(.*?)<([^<>]+)>\s*$")
_HANDLE_SEPARATOR_RE = re.compile(r"[\s._-]+")


def normalize_scope_path(scope_path: str, is_directory: bool) -> str:
    """Validates and normalizes a repository-relative file or directory scope path.

    Args:
        scope_path: Repository-relative file path when ``is_directory`` is False,
            or parent directory path when ``is_directory`` is True.
        is_directory: Whether ``scope_path`` denotes a parent directory rather
            than a single file.

    Returns:
        Normalized repository-relative path without leading ``./`` or trailing
        ``/`` or ``/*`` suffixes.

    Raises:
        ValueError: If ``scope_path`` is empty, starts with ``/``, or ends with
            ``/`` or ``/*`` when ``is_directory`` is False.
    """
    cleaned = scope_path.strip()
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if not cleaned or cleaned.startswith("/"):
        raise ValueError(f"Invalid repo-relative scope_path: {scope_path!r}")
    if is_directory:
        cleaned = cleaned.removesuffix("/*").rstrip("/")
        if not cleaned or cleaned == ".":
            raise ValueError(f"Invalid directory scope_path: {scope_path!r}")
        return cleaned
    if cleaned.endswith(("/", "/*")):
        raise ValueError(f"File scope_path must not end with '/' or '/*': {scope_path!r}")
    return cleaned


@dataclass(frozen=True)
class GitCommitTouch:
    """A historical git commit touching a changed file or sibling directory."""

    sha: str
    email: str
    name: str
    timestamp: int
    scope_path: str
    is_directory: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope_path", normalize_scope_path(self.scope_path, self.is_directory)
        )


@dataclass(frozen=True)
class BlameLineTouch:
    """Aggregated git blame line ownership for a single author on a changed file."""

    sha: str
    email: str
    name: str
    timestamp: int
    file_path: str
    line_count: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_path", normalize_scope_path(self.file_path, False))


@dataclass(frozen=True)
class PackratReviewEvent:
    """A historical pull request review recorded in packrat."""

    pr_number: int
    reviewer_login: str
    state: str
    timestamp: int
    scope_path: str
    is_directory: bool = False
    pr_changed_files: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope_path", normalize_scope_path(self.scope_path, self.is_directory)
        )


@dataclass(frozen=True)
class PackratThreadEvent:
    """A historical inline pull request review thread interaction in packrat."""

    thread_id: str
    pr_number: int
    reviewer_login: str
    timestamp: int
    scope_path: str
    is_directory: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "scope_path", normalize_scope_path(self.scope_path, self.is_directory)
        )


@dataclass(frozen=True)
class PackratLoadResult:
    """Result container returned by the read-only packrat SQLite loader."""

    status: str
    repo_full_name: str | None = None
    prs_backfill_complete: bool = False
    fallback_note: str | None = None
    backfill_warning: str | None = None
    target_pr_author: str | None = None
    target_pr_title: str | None = None
    target_pr_base_ref: str | None = None
    target_pr_files: tuple[str, ...] = ()
    known_logins: frozenset[str] = frozenset()
    sha_to_login: dict[str, str] = field(default_factory=dict)
    email_to_login: dict[str, str] = field(default_factory=dict)
    name_to_login: dict[str, str] = field(default_factory=dict)
    review_events: tuple[PackratReviewEvent, ...] = ()
    thread_events: tuple[PackratThreadEvent, ...] = ()


@dataclass(frozen=True)
class RankedReviewer:
    """Scored and ranked reviewer candidate ready for report rendering."""

    canonical_key: str
    username: str
    is_github_login: bool
    display_name: str
    email: str
    score: float
    direct_files_count: int
    direct_commits: int
    blame_lines: int
    direct_reviews: int
    direct_threads: int
    directory_commits: int
    directory_reviews: int
    directory_threads: int
    files: tuple[str, ...] = ()


def parse_iso_timestamp(value: str | None) -> int | None:
    """Parses an ISO-8601 timestamp string into Unix epoch seconds.

    Accepts ``Z`` UTC designators, explicit numeric UTC offsets, fractional
    seconds, and naive timestamps (interpreted as UTC).

    Args:
        value: ISO-8601 timestamp text, or None.

    Returns:
        Integer Unix epoch timestamp in seconds, or None when ``value`` is
        missing or invalid.
    """
    if not value:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    normalized = f"{stripped[:-1]}+00:00" if stripped.endswith(("Z", "z")) else stripped
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def parse_iso_timestamp_or_zero(value: str | None) -> int:
    """Parses an ISO-8601 timestamp string or returns 0 when missing or invalid.

    Args:
        value: ISO-8601 timestamp text, or None.

    Returns:
        Integer Unix epoch timestamp in seconds, or 0 on missing or invalid text.
    """
    parsed = parse_iso_timestamp(value)
    return parsed if parsed is not None else 0


def compute_recency_weight(event_ts: int, now_ts: int) -> float:
    """Calculates the 180-day half-life recency decay multiplier for an event.

    Events with missing or non-positive timestamps receive a neutral one-half-life
    weight (0.5) so historical reviews with null timestamps still contribute
    without outranking recent activity.

    Args:
        event_ts: Event Unix timestamp in seconds.
        now_ts: Reference current Unix timestamp in seconds.

    Returns:
        Decay weight in ``(0.0, 1.0]``.
    """
    if event_ts <= 0 or now_ts <= 0:
        return MISSING_TIMESTAMP_WEIGHT
    days_ago = max(0.0, (now_ts - event_ts) / SECONDS_PER_DAY)
    return float(0.5 ** (days_ago / HALF_LIFE_DAYS))


def _extract_email_candidate_handle(lowered_email: str) -> str:
    """Extracts the account handle portion of a lowercased email address."""
    local_part, _, domain = lowered_email.partition("@")
    if domain == "users.noreply.github.com" and "+" in local_part:
        _, _, local_part = local_part.partition("+")
    return local_part


def is_bot_identity(login_or_email: str) -> bool:
    """Returns whether a GitHub login, git email, or canonical key belongs to a bot.

    Args:
        login_or_email: Login handle, email address, or ``git:<email>`` key.

    Returns:
        Whether the identity matches a known automation account or ``noreply``
        system address.
    """
    cleaned = (login_or_email or "").strip().lower().lstrip("@")
    if cleaned.startswith("git:"):
        cleaned = cleaned[4:].strip()
    if not cleaned:
        return False
    if "[bot]" in cleaned or cleaned in KNOWN_BOT_EMAILS:
        return True
    candidate = (
        _extract_email_candidate_handle(cleaned) if "@" in cleaned else cleaned.removeprefix("app/")
    )
    return candidate in KNOWN_BOT_LOGINS


def parse_github_noreply_login(email: str) -> str | None:
    """Extracts a human GitHub login from a ``users.noreply.github.com`` address.

    Args:
        email: Commit author email address.

    Returns:
        Extracted ``<login>`` when ``email`` matches
        ``^(?:\\d+\\+)?([A-Za-z0-9-]+)@users\\.noreply\\.github\\.com$`` and is not a
        bot account; otherwise None.
    """
    if not email:
        return None
    cleaned = email.strip()
    if is_bot_identity(cleaned):
        return None
    match = _NOREPLY_EMAIL_RE.match(cleaned)
    if not match:
        return None
    login = match.group(1)
    if is_bot_identity(login):
        return None
    return login


def normalize_handle(value: str) -> str:
    """Normalizes a login, email local-part, or full name for exact comparison."""
    cleaned = value.strip().lstrip("@").lower()
    return _HANDLE_SEPARATOR_RE.sub("", cleaned)


def is_multi_token_name(name: str) -> bool:
    """Returns whether ``name`` contains at least two alphanumeric word tokens."""
    tokens = [tok for tok in name.strip().split() if any(ch.isalnum() for ch in tok)]
    return len(tokens) >= 2


def normalize_multi_token_name(name: str) -> str | None:
    """Normalizes whitespace and case for multi-token names, rejecting single words."""
    if not is_multi_token_name(name):
        return None
    return " ".join(name.strip().lower().split())


def _strip_lower_or_none(value: str) -> str | None:
    """Strips and lowercases ``value``, returning None when blank."""
    cleaned = value.strip().lower()
    return cleaned or None


def _index_unambiguous_norms(logins: Iterable[str]) -> dict[str, str]:
    """Builds a normalized-handle map containing only unambiguous single-login hits."""
    grouped: dict[str, list[str]] = {}
    for login in logins:
        norm = normalize_handle(login)
        if norm:
            grouped.setdefault(norm, []).append(login)
    return {norm: items[0] for norm, items in grouped.items() if len(items) == 1}


class IdentityResolver:
    """Reconciles GitHub logins and git commit identities without false merges.

    Applies the five-step identity merge ladder:
    1. Unambiguous ``sha_to_login``, ``email_to_login``, and multi-token
       ``name_to_login`` mappings loaded from ``packrat``.
    2. GitHub noreply email parsing (``<id>+<login>@users.noreply.github.com``).
    3. Optional ``gh_lookup`` commit API callback with negative email caching
       and a circuit breaker that trips on the first raised exception.
    4. Exact case-insensitive or ``[._-]``-normalized full-handle or multi-token
       full-name matching against ``known_logins`` (never substring or single
       first-name matching).
    5. Unlinked fallback ``("git:<email_lower>", False)`` without ``@``.
    """

    def __init__(
        self,
        packrat_result: PackratLoadResult | None = None,
        known_logins: Iterable[str] = (),
        excluded_authors: Iterable[str] = (),
        *,
        collaborator_logins: Iterable[str] = (),
        sha_to_login: Mapping[str, str] | None = None,
        email_to_login: Mapping[str, str] | None = None,
        name_to_login: Mapping[str, str] | None = None,
    ) -> None:
        """Initializes lookup tables, caches, and author exclusion filters."""
        self._canonical_by_lower: dict[str, str] = {}
        self._known_by_lower: dict[str, str] = {}
        self._sha_to_login: dict[str, str] = {}
        self._email_to_login: dict[str, str] = {}
        self._name_to_login: dict[str, str] = {}
        self._gh_email_cache: dict[str, str | None] = {}
        self._gh_circuit_open: bool = False
        self._observed_emails: dict[str, set[str]] = {}
        self._observed_names: dict[str, set[str]] = {}
        self._excluded_emails: set[str] = set()
        self._excluded_keys: set[str] = set()
        self._excluded_norms: set[str] = set()

        self._load_known_logins(packrat_result, known_logins, collaborator_logins)
        self._load_identity_maps(packrat_result, sha_to_login, email_to_login, name_to_login)
        self._known_by_norm = _index_unambiguous_norms(self._known_by_lower.values())

        if packrat_result and packrat_result.target_pr_author:
            self.add_excluded_author(packrat_result.target_pr_author)
        self.add_excluded_authors(excluded_authors)

    @property
    def gh_circuit_open(self) -> bool:
        """Whether ``gh_lookup`` raised an error and tripped the circuit breaker."""
        return self._gh_circuit_open

    def _canonicalize_login(self, login: str) -> str:
        """Registers and returns the canonical casing for a GitHub login."""
        cleaned = login.strip().lstrip("@")
        return self._canonical_by_lower.setdefault(cleaned.lower(), cleaned)

    def _register_known_login(self, login: str) -> str | None:
        """Adds a non-bot GitHub login to ``known_logins`` and returns its canonical form."""
        cleaned = (login or "").strip().lstrip("@")
        if not cleaned or is_bot_identity(cleaned):
            return None
        canonical = self._canonicalize_login(cleaned)
        self._known_by_lower.setdefault(canonical.lower(), canonical)
        return canonical

    def _load_known_logins(
        self,
        packrat_result: PackratLoadResult | None,
        known_logins: Iterable[str],
        collaborator_logins: Iterable[str],
    ) -> None:
        """Populates known repository logins from packrat and collaborator lists."""
        if packrat_result:
            for login in packrat_result.known_logins:
                self._register_known_login(login)
        for login in known_logins:
            self._register_known_login(login)
        for login in collaborator_logins:
            self._register_known_login(login)

    def _register_map_login(self, login: str) -> str:
        """Registers a non-bot login in known_logins while preserving raw bot handles."""
        cleaned_login = login.strip().lstrip("@")
        return self._register_known_login(cleaned_login) or cleaned_login

    def _load_keyed_login_map(
        self,
        base_map: Mapping[str, str],
        extra_map: Mapping[str, str] | None,
        key_normalizer: Callable[[str], str | None],
    ) -> dict[str, str]:
        """Merges and normalizes a ``key -> login`` lookup table."""
        merged = dict(base_map)
        if extra_map:
            merged.update(extra_map)
        target: dict[str, str] = {}
        for raw_key, login in merged.items():
            norm_key = key_normalizer(raw_key)
            canonical = self._register_map_login(login)
            if norm_key and canonical:
                target[norm_key] = canonical
        return target

    def _load_identity_maps(
        self,
        packrat_result: PackratLoadResult | None,
        sha_to_login: Mapping[str, str] | None,
        email_to_login: Mapping[str, str] | None,
        name_to_login: Mapping[str, str] | None,
    ) -> None:
        """Loads unambiguous SHA, email, and multi-token name maps."""
        self._sha_to_login = self._load_keyed_login_map(
            packrat_result.sha_to_login if packrat_result else {},
            sha_to_login,
            _strip_lower_or_none,
        )
        self._email_to_login = self._load_keyed_login_map(
            packrat_result.email_to_login if packrat_result else {},
            email_to_login,
            _strip_lower_or_none,
        )
        self._name_to_login = self._load_keyed_login_map(
            packrat_result.name_to_login if packrat_result else {},
            name_to_login,
            normalize_multi_token_name,
        )

    def add_excluded_authors(self, values: Iterable[str]) -> None:
        """Registers multiple author handles, emails, or names to exclude."""
        for value in values:
            self.add_excluded_author(value)

    def add_excluded_author(self, value: str) -> None:
        """Registers an author login, email, ``Name <email>``, or full name to exclude."""
        cleaned = (value or "").strip()
        if not cleaned:
            return
        header_match = _GIT_AUTHOR_HEADER_RE.match(cleaned)
        if header_match:
            self.add_excluded_author(header_match.group(1))
            self.add_excluded_author(header_match.group(2))
            return
        if cleaned.lower().startswith("git:"):
            cleaned = cleaned[4:].strip()
        if "@" in cleaned and not cleaned.startswith("@"):
            self._add_excluded_email(cleaned)
            return
        token = cleaned.lstrip("@").strip()
        if not token:
            return
        self._excluded_keys.add(token.lower())
        norm = normalize_handle(token)
        if norm:
            self._excluded_norms.add(norm)
        multi_name = normalize_multi_token_name(token)
        if multi_name and multi_name in self._name_to_login:
            mapped = self._name_to_login[multi_name]
            self._excluded_keys.add(mapped.lower())
            self._excluded_norms.add(normalize_handle(mapped))

    def _add_excluded_email(self, email: str) -> None:
        """Indexes an excluded email address and any linked login or local-part."""
        email_lower = email.strip().lower()
        self._excluded_emails.add(email_lower)
        self._excluded_keys.add(f"git:{email_lower}")
        mapped = self._email_to_login.get(email_lower) or parse_github_noreply_login(email_lower)
        if mapped:
            self._excluded_keys.add(mapped.lower())
            self._excluded_norms.add(normalize_handle(mapped))
        local_part, _, domain = email_lower.partition("@")
        if local_part and domain != "users.noreply.github.com":
            self._excluded_keys.add(local_part)
            norm_local = normalize_handle(local_part)
            if norm_local:
                self._excluded_norms.add(norm_local)

    def resolve_github_login(self, login: str) -> str | None:
        """Validates a GitHub login and returns its canonical key, or None for bots."""
        cleaned = (login or "").strip().lstrip("@")
        if not cleaned or is_bot_identity(cleaned):
            return None
        return self._canonicalize_login(cleaned)

    def _record_metadata(self, canonical_key: str, clean_email: str, clean_name: str) -> None:
        """Tracks observed git emails and display names for ``canonical_key``."""
        if clean_email:
            self._observed_emails.setdefault(canonical_key, set()).add(clean_email)
        if clean_name:
            self._observed_names.setdefault(canonical_key, set()).add(clean_name)

    def _match_packrat_maps(self, sha_lower: str, email_lower: str, clean_name: str) -> str | None:
        """Step 1: Checks unambiguous SHA, email, and multi-token name maps."""
        if sha_lower and sha_lower in self._sha_to_login:
            return self._sha_to_login[sha_lower]
        if email_lower and email_lower in self._email_to_login:
            return self._email_to_login[email_lower]
        norm_name = normalize_multi_token_name(clean_name)
        if norm_name and norm_name in self._name_to_login:
            return self._name_to_login[norm_name]
        return None

    def _cache_gh_result(self, email_lower: str, login: str | None) -> str | None:
        """Stores a ``gh_lookup`` result (including None misses) per email."""
        if email_lower:
            self._gh_email_cache[email_lower] = login
        return login

    def _lookup_gh_login(
        self,
        clean_sha: str,
        email_lower: str,
        gh_lookup: Callable[[str], str | None] | None,
    ) -> tuple[str | None, bool]:
        """Step 3: Queries ``gh_lookup`` with negative email caching and circuit breaking.

        Returns:
            Tuple ``(canonical_login, is_bot)`` where ``is_bot=True`` signals the
            caller to drop the commit immediately.
        """
        if email_lower and email_lower in self._gh_email_cache:
            return self._gh_email_cache[email_lower], False
        if gh_lookup is None or self._gh_circuit_open or not clean_sha:
            return None, False
        try:
            raw_login = gh_lookup(clean_sha)
        except (
            OSError,
            RuntimeError,
            ValueError,
            LookupError,
            subprocess.SubprocessError,
        ):
            self._gh_circuit_open = True
            return None, False
        candidate = (raw_login or "").strip().lstrip("@")
        lowered = candidate.lower()
        if not candidate or lowered.startswith("error") or "not found" in lowered:
            return self._cache_gh_result(email_lower, None), False
        if is_bot_identity(candidate):
            return self._cache_gh_result(email_lower, None), True
        canonical = self._canonicalize_login(candidate)
        return self._cache_gh_result(email_lower, canonical), False

    def _match_known_email_local(self, email_lower: str) -> str | None:
        """Matches an email local-part against ``known_logins`` without substrings."""
        if not email_lower:
            return None
        local_part, _, domain = email_lower.partition("@")
        if not local_part or domain == "users.noreply.github.com":
            return None
        if local_part in self._known_by_lower:
            return self._known_by_lower[local_part]
        norm_local = normalize_handle(local_part)
        return self._known_by_norm.get(norm_local) if norm_local else None

    def _match_known_logins(self, email_lower: str, clean_name: str) -> str | None:
        """Step 4: Matches exact full handles or multi-token names against known logins."""
        matched_email = self._match_known_email_local(email_lower)
        if matched_email is not None:
            return matched_email
        if not is_multi_token_name(clean_name):
            return None
        norm_name = normalize_handle(clean_name)
        return self._known_by_norm.get(norm_name) if norm_name else None

    def _resolve_login_ladder(
        self,
        clean_sha: str,
        clean_email: str,
        email_lower: str,
        clean_name: str,
        gh_lookup: Callable[[str], str | None] | None,
    ) -> tuple[str | None, bool]:
        """Executes steps 1 through 4 of the GitHub login merge ladder."""
        mapped = self._match_packrat_maps(clean_sha.lower(), email_lower, clean_name)
        if mapped is not None:
            return (None, True) if is_bot_identity(mapped) else (mapped, False)
        noreply = parse_github_noreply_login(clean_email)
        if noreply is not None:
            return self._canonicalize_login(noreply), False
        gh_resolved, is_bot = self._lookup_gh_login(clean_sha, email_lower, gh_lookup)
        if is_bot or gh_resolved is not None:
            return gh_resolved, is_bot
        return self._match_known_logins(email_lower, clean_name), False

    def resolve_git_identity(
        self,
        sha: str,
        email: str,
        name: str,
        gh_lookup: Callable[[str], str | None] | None = None,
    ) -> tuple[str, bool] | None:
        """Resolves a git commit identity to ``(canonical_key, is_github_login)``.

        Args:
            sha: Commit object SHA (may be empty when unavailable).
            email: Commit author email address.
            name: Commit author display name.
            gh_lookup: Optional callback mapping a commit SHA to a GitHub login.

        Returns:
            ``(canonical_key, True)`` when linked to a GitHub login,
            ``("git:<email_lower>", False)`` when unlinked, or None when the
            identity belongs to a bot or has neither a login nor an email.
        """
        clean_sha = (sha or "").strip()
        clean_email = (email or "").strip()
        clean_name = (name or "").strip()
        email_lower = clean_email.lower()
        if is_bot_identity(clean_email) or is_bot_identity(clean_name):
            return None

        resolved, is_bot = self._resolve_login_ladder(
            clean_sha, clean_email, email_lower, clean_name, gh_lookup
        )
        if is_bot:
            return None
        if resolved is not None:
            self._record_metadata(resolved, clean_email, clean_name)
            return resolved, True
        if not email_lower:
            return None
        fallback_key = f"git:{email_lower}"
        self._record_metadata(fallback_key, clean_email, clean_name)
        return fallback_key, False

    def _lookup_metadata_key(self, canonical_key: str) -> str:
        """Resolves case-insensitive canonical keys to their stored metadata key."""
        cleaned = (canonical_key or "").strip().lstrip("@")
        return self._canonical_by_lower.get(cleaned.lower(), cleaned)

    def observed_emails(self, canonical_key: str) -> set[str]:
        """Returns a copy of all git emails observed for ``canonical_key``."""
        key = self._lookup_metadata_key(canonical_key)
        return set(self._observed_emails.get(key, ()))

    def observed_names(self, canonical_key: str) -> set[str]:
        """Returns a copy of all git display names observed for ``canonical_key``."""
        key = self._lookup_metadata_key(canonical_key)
        return set(self._observed_names.get(key, ()))

    def preferred_display_name(self, canonical_key: str) -> str:
        """Selects the most descriptive display name recorded for ``canonical_key``."""
        key = self._lookup_metadata_key(canonical_key)
        names = self._observed_names.get(key)
        if not names:
            return key.removeprefix("git:")
        return min(
            names,
            key=lambda item: (not is_multi_token_name(item), -len(item), item),
        )

    def preferred_email(self, canonical_key: str) -> str:
        """Selects the preferred contact email recorded for ``canonical_key``."""
        key = self._lookup_metadata_key(canonical_key)
        emails = self._observed_emails.get(key)
        if not emails:
            return key[4:] if key.startswith("git:") else ""
        return min(
            emails,
            key=lambda item: (
                "noreply.github.com" in item.lower(),
                item.lower(),
                item != item.lower(),
                item,
            ),
        )

    def _matches_excluded_email(self, email: str) -> bool:
        """Checks a single candidate email against excluded emails and handles."""
        lowered = email.strip().lower()
        if not lowered:
            return False
        if is_bot_identity(lowered) or lowered in self._excluded_emails:
            return True
        noreply = parse_github_noreply_login(lowered)
        if noreply and (
            noreply.lower() in self._excluded_keys
            or normalize_handle(noreply) in self._excluded_norms
        ):
            return True
        local_part, _, domain = lowered.partition("@")
        if not local_part or domain == "users.noreply.github.com":
            return False
        return (
            local_part in self._excluded_keys
            or normalize_handle(local_part) in self._excluded_norms
        )

    def _matches_excluded_name(self, name: str) -> bool:
        """Checks a single candidate display name against excluded names and handles."""
        cleaned = name.strip()
        if not cleaned:
            return False
        if is_bot_identity(cleaned) or cleaned.lower() in self._excluded_keys:
            return True
        norm = normalize_handle(cleaned)
        return bool(norm and is_multi_token_name(cleaned) and norm in self._excluded_norms)

    def is_excluded(
        self,
        canonical_key: str,
        emails: Iterable[str] = (),
        names: Iterable[str] = (),
    ) -> bool:
        """Returns whether a candidate should be excluded as an author or bot.

        Checks ``canonical_key`` along with both passed and previously observed
        emails and display names across login, email, email local-part, and
        normalized multi-token full name.

        Args:
            canonical_key: Candidate canonical key (GitHub login or ``git:<email>``).
            emails: Candidate git email addresses.
            names: Candidate display names.

        Returns:
            Whether the candidate matches a bot identity or an excluded author.
        """
        raw_key = (canonical_key or "").strip()
        if not raw_key or is_bot_identity(raw_key):
            return True
        key_lower = raw_key.lower().lstrip("@")
        if key_lower in self._excluded_keys:
            return True
        if key_lower.startswith("git:"):
            if self._matches_excluded_email(key_lower[4:]):
                return True
        elif normalize_handle(key_lower) in self._excluded_norms:
            return True

        lookup_key = self._lookup_metadata_key(raw_key)
        all_emails = set(emails) | self._observed_emails.get(lookup_key, set())
        if any(self._matches_excluded_email(email) for email in all_emails):
            return True

        all_names = set(names) | self._observed_names.get(lookup_key, set())
        return any(self._matches_excluded_name(name) for name in all_names)
