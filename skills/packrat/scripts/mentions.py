"""Detects issue and pull request mentions in free text for timeline queueing.

A mention of another item in a body, comment, review, or commit message makes
GitHub add a cross-reference event to that item's timeline. Packrat can't see
that event until it refetches the target's timeline, so sync code collects the
mentioned items here and queues them with `PackratStore.enqueue_timeline_dirty`.

Three forms are recognized:

- Bare `#N` (the same rule `github_sync.extract_issue_links` uses), resolved
  against the repository the text came from.
- Qualified `owner/name#N`.
- URLs `https://github.com/owner/name/issues/N` and `.../pull/N`. Suffixes such
  as `/files`, `/commits`, or `#issuecomment-123` are ignored.

Qualified forms resolve through a caller-supplied function (usually a cached
`PackratStore.get_repo`, which follows `repo_aliases`). Only repositories in
mirror mode produce mentions, because `items`-mode repositories don't track
arbitrary item numbers.

Matching is purely textual: mentions inside Markdown code spans or fenced
blocks still count. Reproducing GitHub's own cross-reference rules is out of
scope, and an extra queued refresh costs at most one small timeline query.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from models import RepoConfig

RepoResolver = Callable[[str], RepoConfig | None]
"""Looks up a registered repository by `owner/name`, or returns None."""

_BARE_MENTION_RE = re.compile(r"(?:^|[\s(])#(\d+)\b")
_QUALIFIED_MENTION_RE = re.compile(
    r"(?:^|[\s(])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)#(\d+)\b"
)
_URL_MENTION_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)"
    r"/(?:issues|pull)/(\d+)\b",
    re.IGNORECASE,
)


def _qualified_refs(text: str) -> list[tuple[str, int]]:
    """Returns `(owner/name, number)` pairs from qualified and URL mentions."""
    refs: list[tuple[str, int]] = []
    for pattern in (_QUALIFIED_MENTION_RE, _URL_MENTION_RE):
        for match in pattern.finditer(text):
            owner, name, number = match.groups()
            refs.append((f"{owner}/{name}", int(number)))
    return refs


def extract_item_mentions(
    text: str, source_repo: RepoConfig, resolve_repo: RepoResolver
) -> set[tuple[int, int]]:
    """Finds the mirrored issues and pull requests that `text` mentions.

    Args:
        text: Free text such as a body, comment, or commit message.
        source_repo: Repository the text belongs to; bare `#N` mentions
            resolve here.
        resolve_repo: Resolves `owner/name` to a registered repository,
            following aliases, or returns None for unregistered names.

    Returns:
        Deduplicated `(repo_id, item_number)` pairs in mirror-mode
        repositories. The mentioning item itself is not removed; pass it as
        `source=` to `PackratStore.enqueue_timeline_dirty` instead.
    """
    if not text:
        return set()
    found: set[tuple[int, int]] = set()
    if source_repo.is_mirror:
        found.update(
            (source_repo.repo_id, int(m.group(1)))
            for m in _BARE_MENTION_RE.finditer(text)
        )
    for full_name, number in _qualified_refs(text):
        repo = resolve_repo(full_name)
        if repo is not None and repo.is_mirror:
            found.add((repo.repo_id, number))
    return found


def caching_resolver(lookup: RepoResolver) -> RepoResolver:
    """Wraps `lookup` with a case-insensitive per-call-site cache.

    Args:
        lookup: Uncached resolver, typically `PackratStore.get_repo`.

    Returns:
        A resolver that calls `lookup` at most once per distinct name.
    """
    cache: dict[str, RepoConfig | None] = {}

    def resolve(full_name: str) -> RepoConfig | None:
        key = full_name.lower()
        if key not in cache:
            cache[key] = lookup(full_name)
        return cache[key]

    return resolve
