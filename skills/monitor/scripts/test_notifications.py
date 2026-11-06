"""Unit tests for notifications.py."""

from __future__ import annotations

import json
import unittest

from gh_client import GhError
from notifications import (
    NotificationPoller,
    NotificationsUnavailable,
    parse_http_response,
    poll,
    subject_to_pr_key,
)
from testing import FakeGh, load_fixture


class TestNotifications(unittest.TestCase):
    def test_subject_to_pr_key(self) -> None:
        self.assertEqual(
            subject_to_pr_key("https://api.github.com/repos/acme/widget/pulls/3339"),
            "acme/widget#3339",
        )
        self.assertEqual(
            subject_to_pr_key("https://api.github.com/repos/acme/widget/issues/42"),
            "acme/widget#42",
        )
        self.assertIsNone(subject_to_pr_key(None))
        self.assertIsNone(subject_to_pr_key("https://api.github.com/repos/acme/widget/commits/abc"))

    def test_parse_http_response_crlf_and_headers(self) -> None:
        raw = "HTTP/2.0 200 OK\r\nX-Poll-Interval: 90\r\nLast-Modified: Tue, 27 Oct\r\n\r\n[]"
        status, headers, body = parse_http_response(raw)
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-poll-interval"], "90")
        self.assertEqual(headers["last-modified"], "Tue, 27 Oct")
        self.assertEqual(body, "[]")

    def test_poll_200_and_304_fixtures(self) -> None:
        fake = FakeGh(
            [
                ("notifications", 0, load_fixture("notifications_200.txt"), ""),
                ("If-Modified-Since", 1, load_fixture("notifications_304.txt"), "gh: HTTP 304"),
            ]
        )
        watched = {"acme/widget#3339"}
        res1 = poll(fake, since=None, watched=watched)
        self.assertFalse(res1.not_modified)
        self.assertEqual(res1.last_modified, "Tue, 27 Oct 2026 21:07:55 GMT")
        self.assertEqual(res1.poll_interval_s, 60)
        self.assertEqual(res1.changed_keys, {"acme/widget#3339"})

        res2 = poll(fake, since=res1.last_modified, watched=watched)
        self.assertTrue(res2.not_modified)
        self.assertEqual(res2.changed_keys, set())

    def test_poll_403_raises_unavailable_and_500_raises_gh_error(self) -> None:
        resp_403 = 'HTTP/2.0 403 Forbidden\nX-Poll-Interval: 60\n\n{"message": "scope"}'
        fake = FakeGh(
            [
                ("notifications", 1, resp_403, "HTTP 403"),
                ("notifications", 1, "HTTP/2.0 502 Bad Gateway\n\n", "HTTP 502"),
            ]
        )
        with self.assertRaises(NotificationsUnavailable):
            poll(fake, since=None, watched={"acme/widget#3339"})
        with self.assertRaises(GhError):
            poll(fake, since=None, watched={"acme/widget#3339"})

    def test_review_requested_unwatched_tracks_and_resets_per_poll(self) -> None:
        body_unwatched_req = json.dumps(
            [
                {
                    "id": "2001",
                    "reason": "review_requested",
                    "subject": {
                        "url": "https://api.github.com/repos/acme/widget/pulls/4000",
                        "type": "PullRequest",
                    },
                }
            ]
        )
        body_watched_and_other = json.dumps(
            [
                {
                    "id": "2002",
                    "reason": "review_requested",
                    "subject": {
                        "url": "https://api.github.com/repos/acme/widget/pulls/3339",
                        "type": "PullRequest",
                    },
                },
                {
                    "id": "2003",
                    "reason": "ci_activity",
                    "subject": {
                        "url": "https://api.github.com/repos/acme/widget/pulls/5000",
                        "type": "PullRequest",
                    },
                },
                {
                    "id": "2004",
                    "reason": "review_requested",
                    "subject": {
                        "url": "https://api.github.com/repos/acme/widget/commits/deadbeef",
                        "type": "Commit",
                    },
                },
            ]
        )
        fake = FakeGh(
            [
                ("notifications", 0, f"HTTP/2.0 200 OK\n\n{body_unwatched_req}", ""),
                ("notifications", 0, f"HTTP/2.0 200 OK\n\n{body_watched_and_other}", ""),
                ("notifications", 1, load_fixture("notifications_304.txt"), "gh: HTTP 304"),
            ]
        )
        poller = NotificationPoller(fake)
        watched = {"acme/widget#3339"}

        res1 = poller.poll(since=None, watched=watched)
        self.assertEqual(res1.changed_keys, set())
        self.assertTrue(res1.review_requested_unwatched)
        self.assertTrue(poller.review_requested_unwatched)
        self.assertTrue(NotificationPoller.review_requested_unwatched)

        res2 = poller.poll(since=None, watched=watched)
        self.assertEqual(res2.changed_keys, {"acme/widget#3339"})
        self.assertFalse(res2.review_requested_unwatched)
        self.assertFalse(poller.review_requested_unwatched)
        self.assertFalse(NotificationPoller.review_requested_unwatched)

        poller.review_requested_unwatched = True
        res3 = poller.poll(since="Tue, 27 Oct 2026 21:07:55 GMT", watched=watched)
        self.assertTrue(res3.not_modified)
        self.assertFalse(res3.review_requested_unwatched)
        self.assertFalse(poller.review_requested_unwatched)
        self.assertFalse(NotificationPoller.review_requested_unwatched)


if __name__ == "__main__":
    unittest.main()
