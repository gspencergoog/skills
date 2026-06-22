import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import importlib
import http.server
import urllib.request
import threading
import json
import time

script_dir = os.path.dirname(os.path.abspath(__file__))
dashboard_dir = os.path.abspath(os.path.join(script_dir, "../scripts"))

if dashboard_dir not in sys.path:
    sys.path.append(dashboard_dir)

import launch_dashboard
from launch_dashboard import check_git_state

class TestLaunchDashboard(unittest.TestCase):
    def setUp(self):
        importlib.reload(launch_dashboard)

    @patch('launch_dashboard.run_git')
    def test_check_git_state_not_git(self, mock_git):
        mock_git.return_value = ""
        res = check_git_state(".", "branch-x", "owner/repo")
        self.assertFalse(res["isGit"])
        self.assertIn("is not inside a git repository", res["error"])

    @patch('launch_dashboard.run_git')
    def test_check_git_state_no_git_dir(self, mock_git):
        def side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "rev-parse --show-toplevel":
                return "/Users/gspencer/code/project"
            elif cmd == "rev-parse --git-dir":
                return ""
            return ""
        mock_git.side_effect = side_effect
        res = check_git_state(".", "branch-x", "owner/repo")
        self.assertTrue(res["isGit"])
        self.assertFalse(res["isWorktree"])

    @patch('launch_dashboard.run_git')
    def test_check_git_state_success(self, mock_git):
        def side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "rev-parse --show-toplevel":
                return "/Users/gspencer/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-x"
            elif cmd == "status --porcelain -uno":
                return ""
            elif cmd == "cherry -v":
                return ""
            elif cmd == "remote get-url upstream":
                return "git@github.com:owner/repo.git"
            elif cmd == "rev-parse --git-dir":
                return ".git"
            elif cmd == "rev-parse --git-common-dir":
                return ".git"
            return ""
        
        mock_git.side_effect = side_effect
        
        res = check_git_state(".", "branch-x", "owner/repo")
        self.assertTrue(res["isGit"])
        self.assertTrue(res["isCorrectBranch"])
        self.assertTrue(res["isCorrectRepo"])
        self.assertFalse(res["isDirty"])
        self.assertFalse(res["hasUnpushed"])
        self.assertFalse(res["isWorktree"])

    @patch('launch_dashboard.run_git')
    def test_check_git_state_dirty_mismatched(self, mock_git):
        def side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "rev-parse --show-toplevel":
                return "/Users/gspencer/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-wrong"
            elif cmd == "status --porcelain -uno":
                return "M lib/foo.dart\n"
            elif cmd == "cherry -v":
                return "+ 1a2b3c4d commit message\n"
            elif cmd == "remote get-url upstream":
                return "git@github.com:wrong-owner/wrong-repo.git"
            elif cmd == "remote get-url origin":
                return "git@github.com:wrong-owner/wrong-repo.git"
            elif cmd == "rev-parse --git-dir":
                return ".git/worktrees/branch-wrong"
            elif cmd == "rev-parse --git-common-dir":
                return ".git"
            return ""
        
        mock_git.side_effect = side_effect
        
        res = check_git_state(".", "branch-x", "owner/repo")
        self.assertTrue(res["isGit"])
        self.assertFalse(res["isCorrectBranch"])
        self.assertFalse(res["isCorrectRepo"])
        self.assertTrue(res["isDirty"])
        self.assertTrue(res["hasUnpushed"])
        self.assertTrue(res["isWorktree"])
        self.assertEqual(res["activeBranch"], "branch-wrong")

    @patch('select.kqueue')
    @patch('select.kevent')
    @patch('os.open')
    @patch('os.close')
    @patch('os.path.exists')
    def test_wait_for_git_changes_kqueue_triggered(self, mock_exists, mock_close, mock_open_file, mock_kevent, mock_kqueue):
        mock_kq = MagicMock()
        mock_kq.control.return_value = [MagicMock()] 
        mock_kqueue.return_value = mock_kq
        
        mock_exists.return_value = True
        mock_open_file.return_value = 5
        
        from launch_dashboard import wait_for_git_changes
        res = wait_for_git_changes("fake/HEAD", "fake/index", lambda: False)
        self.assertTrue(res)
        mock_close.assert_any_call(5)

    @patch('time.sleep')
    @patch('launch_dashboard.select')
    @patch('os.path.getmtime')
    @patch('os.path.exists')
    def test_wait_for_git_changes_mtime_polling(self, mock_exists, mock_getmtime, mock_select, mock_sleep):
        if hasattr(mock_select, "kqueue"):
            del mock_select.kqueue
            
        mock_exists.return_value = True
        mock_getmtime.side_effect = [1000, 2000, 1000, 2001]
        
        from launch_dashboard import wait_for_git_changes
        res = wait_for_git_changes("fake/HEAD", "fake/index", lambda: False)
        self.assertTrue(res)

    @patch('launch_dashboard.select')
    @patch('os.path.getmtime')
    @patch('os.path.exists')
    def test_wait_for_git_changes_mtime_polling_shutdown(self, mock_exists, mock_getmtime, mock_select):
        if hasattr(mock_select, "kqueue"):
            del mock_select.kqueue
        mock_exists.return_value = True
        from launch_dashboard import wait_for_git_changes
        res = wait_for_git_changes("fake/HEAD", "fake/index", lambda: True)
        self.assertFalse(res)

class TestDashboardServerIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data_dir = os.path.abspath("temp_dashboard_test_data")
        os.makedirs(cls.data_dir, exist_ok=True)
        
        cls.comments_path = os.path.join(cls.data_dir, "pr_comments.json")
        with open(cls.comments_path, "w") as f:
            json.dump({
                "repo": "owner/repo",
                "pr": 123,
                "headRefName": "main",
                "threads": []
            }, f)
            
        from launch_dashboard import DashboardHandler
        DashboardHandler.data_dir = cls.data_dir
        DashboardHandler.project_dir = os.path.abspath(".")
        DashboardHandler.git_dir = None
        
        server_address = ("127.0.0.1", 0)
        cls.httpd = http.server.ThreadingHTTPServer(server_address, DashboardHandler)
        cls.port = cls.httpd.server_port
        
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever)
        cls.server_thread.daemon = True
        cls.server_thread.start()
        
    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.server_thread.join()
        
        if os.path.exists(cls.comments_path):
            os.remove(cls.comments_path)
        
        save_path = os.path.join(cls.data_dir, "feedback_state.json")
        if os.path.exists(save_path):
            os.remove(save_path)
            
        if os.path.exists(cls.data_dir):
            os.rmdir(cls.data_dir)

    def setUp(self):
        importlib.reload(launch_dashboard)

    def test_get_root_html(self):
        url = f"http://127.0.0.1:{self.port}/"
        response = urllib.request.urlopen(url)
        self.assertEqual(response.status, 200)
        self.assertIn(b"html", response.read().lower())

    def test_get_api_comments(self):
        url = f"http://127.0.0.1:{self.port}/api/comments"
        response = urllib.request.urlopen(url)
        self.assertEqual(response.status, 200)
        data = json.loads(response.read().decode('utf-8'))
        self.assertEqual(data["repo"], "owner/repo")

    def test_post_api_save(self):
        url = f"http://127.0.0.1:{self.port}/api/save"
        post_data = json.dumps({"decisions": {"thread_1": "resolved"}, "exit_status": 0}).encode('utf-8')
        
        req = urllib.request.Request(url, data=post_data, headers={'Content-Type': 'application/json'})
        response = urllib.request.urlopen(req)
        self.assertEqual(response.status, 200)
        
        save_path = os.path.join(self.data_dir, "feedback_state.json")
        self.assertTrue(os.path.exists(save_path))

    def test_post_api_save_invalid_json(self):
        url = f"http://127.0.0.1:{self.port}/api/save"
        post_data = b"invalid-json-content"
        req = urllib.request.Request(url, data=post_data, headers={'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 500)

    def test_post_invalid_route(self):
        url = f"http://127.0.0.1:{self.port}/api/invalid-route"
        req = urllib.request.Request(url, data=b"{}", headers={'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 404)

    @patch('json.load')
    def test_get_api_comments_error(self, mock_json_load):
        mock_json_load.side_effect = Exception("JSON parse error")
        url = f"http://127.0.0.1:{self.port}/api/comments"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url)
        self.assertEqual(ctx.exception.code, 500)

    def test_options_request(self):
        url = f"http://127.0.0.1:{self.port}/api/save"
        req = urllib.request.Request(url, method="OPTIONS")
        response = urllib.request.urlopen(req)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")

    def test_post_api_abort(self):
        url = f"http://127.0.0.1:{self.port}/api/abort"
        req = urllib.request.Request(url, data=b"{}", headers={'Content-Type': 'application/json'})
        response = urllib.request.urlopen(req)
        self.assertEqual(response.status, 200)
        data = json.loads(response.read().decode('utf-8'))
        self.assertEqual(data["status"], "aborted")

    def test_get_invalid_route(self):
        url = f"http://127.0.0.1:{self.port}/api/invalid-route"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url)
        self.assertEqual(ctx.exception.code, 404)

    def test_git_events_stream(self):
        url = f"http://127.0.0.1:{self.port}/api/git-events"
        response = urllib.request.urlopen(url)
        self.assertEqual(response.status, 200)
        
        first_line = response.readline().decode('utf-8')
        self.assertTrue(first_line.startswith("data:") or first_line.startswith(": heartbeat"))
        response.close()

class TestDashboardMain(unittest.TestCase):
    @patch('launch_dashboard.webbrowser.open')
    @patch('launch_dashboard.run_git')
    @patch('http.server.ThreadingHTTPServer')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    def test_main_startup(self, mock_exit, mock_http_server, mock_git, mock_webbrowser):
        mock_git.return_value = ".git"
        mock_server_inst = MagicMock()
        mock_server_inst.server_port = 12345
        mock_http_server.return_value = mock_server_inst
        
        with patch('launch_dashboard.server_should_shutdown', True):
            from launch_dashboard import main
            main()
            
        mock_webbrowser.assert_called_once_with("http://localhost:12345/")

if __name__ == '__main__':
    unittest.main()
