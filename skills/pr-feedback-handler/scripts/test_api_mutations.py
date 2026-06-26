import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import importlib
import runpy

scripts_dir = os.path.dirname(os.path.abspath(__file__))

import update_thread

class TestApiMutations(unittest.TestCase):
    def setUp(self):
        importlib.reload(update_thread)

    @patch('update_thread.run_cmd')
    def test_reply_to_thread_success(self, mock_run):
        mock_run.return_value = '{"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "comment_123", "body": "test reply"}}}}'

        res = update_thread.reply_to_thread("thread_1", "test reply")
        self.assertEqual(res["id"], "comment_123")
        self.assertEqual(res["body"], "test reply")

    @patch('update_thread.run_cmd')
    def test_reply_to_thread_error(self, mock_run):
        mock_run.return_value = '{"errors": [{"message": "GraphQL Error"}]}'
        with self.assertRaises(Exception) as ctx:
            update_thread.reply_to_thread("thread_1", "test reply")
        self.assertIn("GraphQL errors", str(ctx.exception))

    @patch('update_thread.run_cmd')
    def test_resolve_thread_success(self, mock_run):
        mock_run.return_value = '{"data": {"resolveReviewThread": {"thread": {"id": "thread_1", "isResolved": true}}}}'

        res = update_thread.resolve_thread("thread_1")
        self.assertEqual(res["id"], "thread_1")
        self.assertTrue(res["isResolved"])

    @patch('update_thread.run_cmd')
    def test_resolve_thread_error(self, mock_run):
        mock_run.return_value = '{"errors": [{"message": "GraphQL Error"}]}'
        with self.assertRaises(Exception) as ctx:
            update_thread.resolve_thread("thread_1")
        self.assertIn("GraphQL errors", str(ctx.exception))

    # --- CLI main() Tests ---

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    @patch('sys.argv', ['update_thread.py', 'thread_123', '--reply', 'hello reply'])
    def test_main_reply_only(self, mock_resolve, mock_reply):
        update_thread.main()
        mock_reply.assert_called_once_with('thread_123', 'hello reply')
        mock_resolve.assert_not_called()

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    @patch('sys.argv', ['update_thread.py', 'thread_123', '--resolve'])
    def test_main_resolve_only(self, mock_resolve, mock_reply):
        update_thread.main()
        mock_reply.assert_not_called()
        mock_resolve.assert_called_once_with('thread_123')

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    @patch('sys.argv', ['update_thread.py', 'thread_123', '--reply', 'hello reply', '--resolve'])
    def test_main_both(self, mock_resolve, mock_reply):
        update_thread.main()
        mock_reply.assert_called_once_with('thread_123', 'hello reply')
        mock_resolve.assert_called_once_with('thread_123')

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    @patch('sys.argv', ['update_thread.py', 'thread_123'])
    @patch('sys.exit')
    def test_main_neither_fails(self, mock_exit, mock_resolve, mock_reply):
        update_thread.main()
        mock_reply.assert_not_called()
        mock_resolve.assert_not_called()
        mock_exit.assert_called_once_with(1)

    @patch('update_thread.reply_to_thread')
    @patch('sys.argv', ['update_thread.py', 'thread_123', '--reply', 'hello reply'])
    @patch('sys.exit')
    def test_main_failure(self, mock_exit, mock_reply):
        mock_reply.side_effect = Exception("error")
        update_thread.main()
        mock_exit.assert_called_once_with(1)

    # --- runpy direct execution tests ---

    @patch('subprocess.run')
    @patch('sys.argv', ['update_thread.py', 'thread_123', '--reply', 'hello reply', '--resolve'])
    @patch('sys.exit')
    def test_run_as_main(self, mock_exit, mock_run):
        mock_res1 = MagicMock()
        mock_res1.stdout = '{"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "comment_123", "body": "test reply"}}}}'
        mock_res1.returncode = 0
        
        mock_res2 = MagicMock()
        mock_res2.stdout = '{"data": {"resolveReviewThread": {"thread": {"id": "thread_1", "isResolved": true}}}}'
        mock_res2.returncode = 0
        
        mock_run.side_effect = [mock_res1, mock_res2]

        runpy.run_path(os.path.join(scripts_dir, "update_thread.py"), run_name="__main__")

if __name__ == '__main__':
    unittest.main()
