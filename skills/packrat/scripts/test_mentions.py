"""Unit tests for packrat issue and pull request mention detection."""

from __future__ import annotations

import unittest

from mentions import caching_resolver, extract_item_mentions
from models import REPO_MODE_ITEMS, RepoConfig


def _repo(repo_id: int, full_name: str, mode: str = "mirror") -> RepoConfig:
    owner, name = full_name.split("/", 1)
    return RepoConfig(
        repo_id=repo_id, owner=owner, name=name, full_name=full_name, mode=mode
    )


WIDGETS = _repo(1, "octo-org/widgets")
GADGETS = _repo(2, "octo-org/gadgets")
SPROCKETS = _repo(3, "octo-org/sprockets", REPO_MODE_ITEMS)


class FakeResolver:
    """Case-insensitive resolver with one alias, recording every lookup."""

    def __init__(self) -> None:
        self.by_name = {
            r.full_name.lower(): r for r in (WIDGETS, GADGETS, SPROCKETS)
        }
        self.by_name["octo-org/old-gadgets"] = GADGETS
        self.calls: list[str] = []

    def __call__(self, full_name: str) -> RepoConfig | None:
        self.calls.append(full_name)
        return self.by_name.get(full_name.lower())


class TestExtractItemMentions(unittest.TestCase):
    """Tests bare, qualified, and URL mention forms and their resolution rules."""

    def setUp(self) -> None:
        self.resolve = FakeResolver()

    def _mentions(self, text: str, source: RepoConfig = WIDGETS) -> set[tuple[int, int]]:
        return extract_item_mentions(text, source, self.resolve)

    def test_bare_numbers_resolve_to_the_source_repo(self) -> None:
        self.assertEqual(self._mentions("Fixes #7 and (#8)."), {(1, 7), (1, 8)})

    def test_bare_number_needs_a_leading_boundary(self) -> None:
        self.assertEqual(self._mentions("abc#7 and x/#8"), set())

    def test_qualified_mention_resolves_through_the_resolver(self) -> None:
        self.assertEqual(self._mentions("See octo-org/gadgets#3"), {(2, 3)})

    def test_qualified_mention_is_case_insensitive(self) -> None:
        self.assertEqual(self._mentions("See Octo-Org/Gadgets#3"), {(2, 3)})

    def test_url_forms_ignore_suffixes(self) -> None:
        text = (
            "https://github.com/octo-org/gadgets/issues/4\n"
            "https://github.com/octo-org/gadgets/pull/5/files\n"
            "https://github.com/octo-org/widgets/pull/6/commits\n"
            "https://github.com/octo-org/gadgets/issues/9#issuecomment-123"
        )
        self.assertEqual(self._mentions(text), {(2, 4), (2, 5), (1, 6), (2, 9)})

    def test_url_does_not_also_yield_a_bare_mention(self) -> None:
        text = "https://github.com/octo-org/gadgets/issues/9#issuecomment-123"
        self.assertEqual(self._mentions(text), {(2, 9)})

    def test_alias_resolves_to_the_current_repo(self) -> None:
        self.assertEqual(self._mentions("octo-org/old-gadgets#11"), {(2, 11)})

    def test_unregistered_repo_is_ignored(self) -> None:
        self.assertEqual(
            self._mentions("other-org/thing#2 https://github.com/other-org/thing/pull/3"),
            set(),
        )

    def test_items_mode_repo_is_ignored(self) -> None:
        self.assertEqual(self._mentions("octo-org/sprockets#2"), set())

    def test_bare_mentions_in_items_mode_source_are_ignored(self) -> None:
        self.assertEqual(
            self._mentions("#2 and octo-org/widgets#4", source=SPROCKETS), {(1, 4)}
        )

    def test_code_spans_still_match(self) -> None:
        # Documented behavior: matching is textual, not Markdown-aware.
        self.assertEqual(
            self._mentions("Run `x #7` then\n```\n octo-org/gadgets#3\n```"),
            {(1, 7), (2, 3)},
        )

    def test_duplicates_are_collapsed(self) -> None:
        text = "#7 #7 octo-org/widgets#7 https://github.com/octo-org/widgets/issues/7"
        self.assertEqual(self._mentions(text), {(1, 7)})

    def test_source_item_is_not_removed(self) -> None:
        # The caller drops the self-reference via enqueue_timeline_dirty(source=).
        self.assertEqual(self._mentions("this is #5"), {(1, 5)})

    def test_empty_text(self) -> None:
        self.assertEqual(self._mentions(""), set())


class TestCachingResolver(unittest.TestCase):
    """Tests that the resolver cache looks up each name once."""

    def test_looks_up_each_name_once_case_insensitively(self) -> None:
        inner = FakeResolver()
        resolve = caching_resolver(inner)
        self.assertIs(resolve("octo-org/gadgets"), GADGETS)
        self.assertIs(resolve("Octo-Org/Gadgets"), GADGETS)
        self.assertIsNone(resolve("other-org/thing"))
        self.assertIsNone(resolve("other-org/thing"))
        self.assertEqual(inner.calls, ["octo-org/gadgets", "other-org/thing"])


if __name__ == "__main__":
    unittest.main()
