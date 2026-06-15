import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import importlib
import runpy

script_dir = os.path.dirname(os.path.abspath(__file__))
scripts_dir = os.path.abspath(os.path.join(script_dir, "../scripts"))

if scripts_dir not in sys.path:
    sys.path.append(scripts_dir)

import reply_thread
import resolve_thread

class TestApiMutations(unittest.TestCase):
    def setUp(self):
        importlib.reload(reply_thread)
        importlib.reload(resolve_thread)

    @patch('reply_thread.run_cmd')
    def test_reply_to_thread_success(self, mock_run):
        mock_run.return_value = '{"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "comment_123", "body": "test reply"}}}}'
        
        res = reply_thread.reply_to_thread("thread_1", "test reply")
        self.assertEqual(res["id"], "comment_123")
        self.assertEqual(res["body"], "test reply")

    @patch('reply_thread.run_cmd')
    def test_reply_to_thread_error(self, mock_run):
        mock_run.return_value = '{"errors": [{"message": "GraphQL Error"}]}'
        with self.assertRaises(Exception) as ctx:
            reply_thread.reply_to_thread("thread_1", "test reply")
        self.assertIn("GraphQL errors", str(ctx.exception))

    @patch('resolve_thread.run_cmd')
    def test_resolve_thread_success(self, mock_run):
        mock_run.return_value = '{"data": {"resolveReviewThread": {"thread": {"id": "thread_1", "isResolved": true}}}}'
        
        res = resolve_thread.resolve_thread("thread_1")
        self.assertEqual(res["id"], "thread_1")
        self.assertTrue(res["isResolved"])

    @patch('resolve_thread.run_cmd')
    def test_resolve_thread_error(self, mock_run):
        mock_run.return_value = '{"errors": [{"message": "GraphQL Error"}]}'
        with self.assertRaises(Exception) as ctx:
            resolve_thread.resolve_thread("thread_1")
        self.assertIn("GraphQL errors", str(ctx.exception))

    # --- CLI main() Tests ---

    @patch('reply_thread.reply_to_thread')
    @patch('sys.argv', ['reply_thread.py', 'thread_123', 'hello reply'])
    def test_reply_main_success(self, mock_reply):
        reply_thread.main()
        mock_reply.assert_called_once_with('thread_123', 'hello reply')

    @patch('reply_thread.reply_to_thread')
    @patch('sys.argv', ['reply_thread.py', 'thread_123', 'hello reply'])
    @patch('sys.exit')
    def test_reply_main_failure(self, mock_exit, mock_reply):
        mock_reply.side_effect = Exception("error")
        reply_thread.main()
        mock_exit.assert_called_once_with(1)

    @patch('resolve_thread.resolve_thread')
    @patch('sys.argv', ['resolve_thread.py', 'thread_123'])
    def test_resolve_main_success(self, mock_resolve):
        resolve_thread.main()
        mock_resolve.assert_called_once_with('thread_123')

    @patch('resolve_thread.resolve_thread')
    @patch('sys.argv', ['resolve_thread.py', 'thread_123'])
    @patch('sys.exit')
    def test_resolve_main_failure(self, mock_exit, mock_resolve):
        mock_resolve.side_effect = Exception("error")
        resolve_thread.main()
        mock_exit.assert_called_once_with(1)

    # --- runpy direct execution tests ---

    @patch('subprocess.run')
    @patch('sys.argv', ['reply_thread.py', 'thread_123', 'hello reply'])
    @patch('sys.exit')
    def test_run_as_main_reply(self, mock_exit, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = '{"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "comment_123", "body": "test reply"}}}}'
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        runpy.run_path("/Users/gspencer/code/cheats/agents/skills/pr-feedback-handler/scripts/reply_thread.py", run_name="__main__")

    @patch('subprocess.run')
    @patch('sys.argv', ['resolve_thread.py', 'thread_123'])
    @patch('sys.exit')
    def test_run_as_main_resolve(self, mock_exit, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = '{"data": {"resolveReviewThread": {"thread": {"id": "thread_1", "isResolved": true}}}}'
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        runpy.run_path("/Users/gspencer/code/cheats/agents/skills/pr-feedback-handler/scripts/resolve_thread.py", run_name="__main__")

if __name__ == '__main__':
    unittest.main()
