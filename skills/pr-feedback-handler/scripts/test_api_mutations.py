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

    # --- Bulk Processing Tests ---

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    def test_process_decisions_all_success(self, mock_resolve, mock_reply):
        mock_reply.return_value = {"id": "comment_1"}
        mock_resolve.return_value = {"id": "thread_1", "isResolved": True}
        
        decisions = [
            {"threadId": "thread_1", "reply": "fixed 1", "resolve": True, "approved": True},
            {"thread_id": "thread_2", "reply": "fixed 2", "resolve": False, "approved": True},
            {"threadId": "thread_3", "resolve": True, "approved": True}
        ]
        
        success_count, failures = update_thread.process_decisions(decisions)
        self.assertEqual(success_count, 3)
        self.assertEqual(len(failures), 0)
        
        self.assertEqual(mock_reply.call_count, 2)
        mock_reply.assert_any_call("thread_1", "fixed 1")
        mock_reply.assert_any_call("thread_2", "fixed 2")
        
        self.assertEqual(mock_resolve.call_count, 2)
        mock_resolve.assert_any_call("thread_1")
        mock_resolve.assert_any_call("thread_3")

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    def test_process_decisions_skips_unapproved(self, mock_resolve, mock_reply):
        decisions = [
            {"threadId": "thread_1", "reply": "fixed 1", "resolve": True, "approved": False},
        ]
        success_count, failures = update_thread.process_decisions(decisions)
        self.assertEqual(success_count, 0)
        self.assertEqual(len(failures), 0)
        mock_reply.assert_not_called()
        mock_resolve.assert_not_called()

    @patch('update_thread.reply_to_thread')
    @patch('update_thread.resolve_thread')
    def test_process_decisions_failures(self, mock_resolve, mock_reply):
        mock_reply.side_effect = [
            {"id": "comment_1"},
            Exception("Failed to reply")
        ]
        mock_resolve.side_effect = [
            Exception("Failed to resolve"),
            {"id": "thread_3", "isResolved": True}
        ]
        
        decisions = [
            {"threadId": "thread_1", "reply": "fixed 1", "resolve": True, "approved": True},
            {"threadId": "thread_2", "reply": "fixed 2", "resolve": False, "approved": True},
            {"threadId": "thread_3", "resolve": True, "approved": True}
        ]
        
        success_count, failures = update_thread.process_decisions(decisions)
        self.assertEqual(success_count, 1)
        self.assertEqual(len(failures), 2)
        
        self.assertEqual(failures[0]["thread_id"], "thread_1")
        self.assertEqual(failures[0]["action"], "resolve")
        self.assertEqual(failures[0]["error"], "Failed to resolve")
        
        self.assertEqual(failures[1]["thread_id"], "thread_2")
        self.assertEqual(failures[1]["action"], "reply")
        self.assertEqual(failures[1]["error"], "Failed to reply")

    @patch('update_thread.process_decisions')
    @patch('sys.exit')
    @patch('builtins.open')
    def test_main_file_format_decisions_key(self, mock_file_open, mock_exit, mock_process):
        import io
        mock_file_open.return_value = io.StringIO('{"decisions": [{"threadId": "t1", "reply": "hello"}]}')
        mock_process.return_value = (1, [])
        
        with patch('sys.argv', ['update_thread.py', '--file', 'some_file.json']):
            update_thread.main()
            
        mock_process.assert_called_once_with([{"threadId": "t1", "reply": "hello"}])
        mock_exit.assert_not_called()

    @patch('update_thread.process_decisions')
    @patch('sys.exit')
    @patch('builtins.open')
    def test_main_file_format_list(self, mock_file_open, mock_exit, mock_process):
        import io
        mock_file_open.return_value = io.StringIO('[{"threadId": "t1", "reply": "hello"}]')
        mock_process.return_value = (1, [])
        
        with patch('sys.argv', ['update_thread.py', '--file', 'some_file.json']):
            update_thread.main()
            
        mock_process.assert_called_once_with([{"threadId": "t1", "reply": "hello"}])
        mock_exit.assert_not_called()

    @patch('sys.exit')
    @patch('sys.argv', ['update_thread.py', '--file', 'some_file.json', 'thread_123'])
    def test_main_file_with_positional_fails(self, mock_exit):
        update_thread.main()
        mock_exit.assert_called_once_with(1)

    @patch('update_thread.process_decisions')
    @patch('sys.exit')
    @patch('builtins.open')
    def test_main_file_failure_exit(self, mock_file_open, mock_exit, mock_process):
        import io
        mock_file_open.return_value = io.StringIO('[]')
        mock_process.return_value = (0, [{"thread_id": "t1", "action": "reply", "error": "err"}])
        
        with patch('sys.argv', ['update_thread.py', '--file', 'some_file.json']):
            update_thread.main()
            
        mock_exit.assert_called_once_with(1)

if __name__ == '__main__':
    unittest.main()
