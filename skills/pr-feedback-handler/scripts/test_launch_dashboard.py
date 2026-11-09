import http.server
import importlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

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
                return "~/code/project"
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
                return "~/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-x"
            elif cmd in ("status --porcelain -uno", "cherry -v"):
                return ""
            elif cmd == "remote get-url upstream":
                return "git@github.com:owner/repo.git"
            elif cmd in ("rev-parse --git-dir", "rev-parse --git-common-dir"):
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
                return "~/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-wrong"
            elif cmd == "status --porcelain -uno":
                return "M lib/foo.dart\n"
            elif cmd == "cherry -v":
                return "+ 1a2b3c4d commit message\n"
            elif cmd in ("remote get-url upstream", "remote get-url origin"):
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

    @patch('launch_dashboard.run_git')
    def test_check_git_state_empty_remotes_list(self, mock_git):
        def side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "rev-parse --show-toplevel":
                return "~/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-x"
            elif cmd in ("status --porcelain -uno", "cherry -v", "remote"):
                return ""
            elif cmd == "remote get-url upstream":
                return "git@github.com:owner/repo.git"
            elif cmd in ("rev-parse --git-dir", "rev-parse --git-common-dir"):
                return ".git"
            return ""

        mock_git.side_effect = side_effect
        res = check_git_state(".", "branch-x", "owner/repo")
        self.assertTrue(res["isCorrectRepo"])

    @patch('launch_dashboard.run_git')
    def test_check_git_state_head_ref_oid_matching(self, mock_git):
        def side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "rev-parse --show-toplevel":
                return "~/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-x"
            elif cmd == "status --porcelain -uno":
                return ""
            elif cmd == "rev-parse HEAD":
                return "sha_exact_match"
            elif cmd == "remote get-url upstream":
                return "git@github.com:owner/repo.git"
            elif cmd in ("rev-parse --git-dir", "rev-parse --git-common-dir"):
                return ".git"
            return ""

        mock_git.side_effect = side_effect
        res = check_git_state(".", "branch-x", "owner/repo", head_ref_oid="sha_exact_match")
        self.assertTrue(res["isGit"])
        self.assertTrue(res["isCorrectBranch"])
        self.assertFalse(res["hasUnpushed"])
        self.assertEqual(res["unpushedCommits"], [])

    @patch('launch_dashboard.run_git')
    def test_check_git_state_head_ref_oid_unpushed_commits(self, mock_git):
        def side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "rev-parse --show-toplevel":
                return "~/code/project"
            elif cmd == "symbolic-ref --short HEAD":
                return "branch-x"
            elif cmd == "status --porcelain -uno":
                return ""
            elif cmd == "rev-parse HEAD":
                return "sha_local_new"
            elif cmd == "log sha_remote_old..HEAD --oneline":
                return "1111111 Commit 1\n2222222 Commit 2"
            elif cmd == "remote get-url upstream":
                return "git@github.com:owner/repo.git"
            elif cmd in ("rev-parse --git-dir", "rev-parse --git-common-dir"):
                return ".git"
            return ""

        mock_git.side_effect = side_effect
        res = check_git_state(".", "branch-x", "owner/repo", head_ref_oid="sha_remote_old")
        self.assertTrue(res["isGit"])
        self.assertTrue(res["isCorrectBranch"])
        self.assertTrue(res["hasUnpushed"])
        self.assertEqual(len(res["unpushedCommits"]), 2)

    @patch('select.kqueue', create=True)
    @patch('select.kevent', create=True)
    @patch('select.KQ_FILTER_VNODE', 1, create=True)
    @patch('select.KQ_EV_ADD', 1, create=True)
    @patch('select.KQ_EV_CLEAR', 1, create=True)
    @patch('select.KQ_NOTE_WRITE', 1, create=True)
    @patch('select.KQ_NOTE_ATTRIB', 1, create=True)
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

        shutil.rmtree(cls.data_dir, ignore_errors=True)

    def setUp(self):
        importlib.reload(launch_dashboard)
        launch_dashboard.server_should_shutdown = False
        launch_dashboard.exit_status = 0
        from launch_dashboard import DashboardHandler
        DashboardHandler.data_dir = self.data_dir
        DashboardHandler.project_dir = os.path.abspath(".")

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

    def test_get_api_comments_with_proposals_overlay(self):
        # Write temporary proposals.json in data_dir
        proposals_path = os.path.join(self.data_dir, "proposals.json")
        try:
            # Overwrite comments with a thread
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": [
                        {"id": "t1", "path": "lib/foo.dart", "proposedFix": "", "draftReply": ""}
                    ]
                }, f)
            with open(proposals_path, "w") as f:
                json.dump({
                    "t1": {
                        "proposedFix": "Custom fix proposal",
                        "draftReply": "Custom draft reply"
                    }
                }, f)
            
            url = f"http://127.0.0.1:{self.port}/api/comments"
            response = urllib.request.urlopen(url)
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode('utf-8'))
            self.assertEqual(len(data["threads"]), 1)
            self.assertEqual(data["threads"][0]["proposedFix"], "Custom fix proposal")
            self.assertEqual(data["threads"][0]["draftReply"], "Custom draft reply")
        finally:
            if os.path.exists(proposals_path):
                os.remove(proposals_path)
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": []
                }, f)

    def test_get_api_comments_with_proposals_list_format(self):
        proposals_path = os.path.join(self.data_dir, "proposals.json")
        try:
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": [{"id": "t1", "path": "lib/foo.dart"}]
                }, f)
            with open(proposals_path, "w") as f:
                json.dump([
                    {"threadId": "t1", "proposedFix": "List fix", "draftReply": "List reply"}
                ], f)
            
            url = f"http://127.0.0.1:{self.port}/api/comments"
            response = urllib.request.urlopen(url)
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode('utf-8'))
            self.assertEqual(data["threads"][0]["proposedFix"], "List fix")
            self.assertEqual(data["threads"][0]["draftReply"], "List reply")
        finally:
            if os.path.exists(proposals_path):
                os.remove(proposals_path)
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": []
                }, f)

    def test_get_api_comments_with_nested_proposals(self):
        proposals_path = os.path.join(self.data_dir, "proposals.json")
        try:
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": [{"id": "t1", "path": "lib/foo.dart"}]
                }, f)
            with open(proposals_path, "w") as f:
                json.dump({
                    "proposals": {
                        "t1": {"proposedFix": "Nested fix", "draftReply": "Nested reply"}
                    }
                }, f)
            
            url = f"http://127.0.0.1:{self.port}/api/comments"
            response = urllib.request.urlopen(url)
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode('utf-8'))
            self.assertEqual(data["threads"][0]["proposedFix"], "Nested fix")
            self.assertEqual(data["threads"][0]["draftReply"], "Nested reply")
        finally:
            if os.path.exists(proposals_path):
                os.remove(proposals_path)
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": []
                }, f)

    def test_get_api_comments_with_proposals_file_override(self):
        custom_proposals_path = os.path.join(self.data_dir, "custom_proposals.json")
        try:
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": [{"id": "t1", "path": "lib/foo.dart"}]
                }, f)
            with open(custom_proposals_path, "w") as f:
                json.dump({
                    "t1": {"proposedFix": "Custom file fix", "draftReply": "Custom file reply"}
                }, f)

            self.httpd.RequestHandlerClass.proposals_file = custom_proposals_path

            url = f"http://127.0.0.1:{self.port}/api/comments"
            response = urllib.request.urlopen(url)
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode('utf-8'))
            self.assertEqual(data["threads"][0]["proposedFix"], "Custom file fix")
            self.assertEqual(data["threads"][0]["draftReply"], "Custom file reply")
        finally:
            self.httpd.RequestHandlerClass.proposals_file = None
            if os.path.exists(custom_proposals_path):
                os.remove(custom_proposals_path)
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": []
                }, f)

    def test_get_api_comments_with_action_and_assessment_overlay(self):
        proposals_path = os.path.join(self.data_dir, "proposals.json")
        try:
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": [{"id": "t1", "path": "lib/foo.dart"}]
                }, f)
            with open(proposals_path, "w") as f:
                json.dump({
                    "t1": {
                        "proposedFix": "Decline fix",
                        "draftReply": "Declining this.",
                        "action": "decline",
                        "assessment": "disagree"
                    }
                }, f)

            url = f"http://127.0.0.1:{self.port}/api/comments"
            response = urllib.request.urlopen(url)
            self.assertEqual(response.status, 200)
            data = json.loads(response.read().decode('utf-8'))
            self.assertEqual(data["threads"][0]["proposedFix"], "Decline fix")
            self.assertEqual(data["threads"][0]["draftReply"], "Declining this.")
            self.assertEqual(data["threads"][0]["action"], "decline")
            self.assertEqual(data["threads"][0]["rating"], "disagree")
        finally:
            if os.path.exists(proposals_path):
                os.remove(proposals_path)
            with open(self.comments_path, "w") as f:
                json.dump({
                    "repo": "owner/repo",
                    "pr": 123,
                    "headRefName": "main",
                    "threads": []
                }, f)

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
        self.assertTrue(first_line.startswith(("data:", ": heartbeat")))
        response.close()

    def test_get_api_comments_not_found(self):
        # Temporarily rename the comments file
        temp_path = self.comments_path + ".bak"
        os.rename(self.comments_path, temp_path)
        try:
            url = f"http://127.0.0.1:{self.port}/api/comments"
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(url)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            os.rename(temp_path, self.comments_path)

class TestDashboardMain(unittest.TestCase):
    def setUp(self):
        os.makedirs('temp_dashboard_test_data', exist_ok=True)
        with open('temp_dashboard_test_data/pr_comments.json', 'w') as f:
            json.dump({'repo': 'test/repo', 'pr': 123}, f)

    def tearDown(self):
        shutil.rmtree('temp_dashboard_test_data', ignore_errors=True)

    def _raise_system_exit(self, code=0):
        raise SystemExit(code)

    @patch('launch_dashboard.webbrowser.open')
    @patch('launch_dashboard.run_git')
    @patch('http.server.ThreadingHTTPServer')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    def test_main_startup(self, mock_exit, mock_http_server, mock_git, mock_webbrowser):
        mock_exit.side_effect = self._raise_system_exit
        def git_side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "status --porcelain -uno":
                return ""
            if cmd == "rev-parse --git-dir":
                return ".git"
            return ""
        mock_git.side_effect = git_side_effect
        mock_server_inst = MagicMock()
        mock_server_inst.server_port = 12345
        mock_http_server.return_value = mock_server_inst

        with patch('launch_dashboard.server_should_shutdown', True):
            from launch_dashboard import main
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

        mock_webbrowser.assert_called_once_with("http://localhost:12345/")

    @patch('launch_dashboard.webbrowser.open')
    @patch('launch_dashboard.run_git')
    @patch('http.server.ThreadingHTTPServer')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.', '--url-file', 'temp_dashboard_test_data/my_url.txt'])
    @patch('sys.exit')
    def test_main_url_file_emission(self, mock_exit, mock_http_server, mock_git, mock_webbrowser):
        mock_exit.side_effect = self._raise_system_exit
        def git_side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "status --porcelain -uno":
                return ""
            if cmd == "rev-parse --git-dir":
                return ".git"
            return ""
        mock_git.side_effect = git_side_effect
        mock_server_inst = MagicMock()
        mock_server_inst.server_port = 54321
        mock_http_server.return_value = mock_server_inst

        url_file_path = os.path.abspath('temp_dashboard_test_data/my_url.txt')
        # Ensure cleanup on shutdown removes the file, but during run it was written
        with patch('launch_dashboard.server_should_shutdown', True):
            from launch_dashboard import main
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

        # After clean shutdown, url_file is removed
        self.assertFalse(os.path.exists(url_file_path))

    @patch('launch_dashboard.webbrowser.open')
    @patch('launch_dashboard.run_git')
    @patch('http.server.ThreadingHTTPServer')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    @patch('time.sleep')
    def test_main_keyboard_interrupt(self, mock_sleep, mock_exit, mock_http_server, mock_git, mock_webbrowser):
        mock_exit.side_effect = self._raise_system_exit
        def git_side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "status --porcelain -uno":
                return ""
            if cmd == "rev-parse --git-dir":
                return ".git"
            return ""
        mock_git.side_effect = git_side_effect
        mock_server_inst = MagicMock()
        mock_server_inst.server_port = 12345
        mock_http_server.return_value = mock_server_inst
        
        # Raise KeyboardInterrupt when sleeping
        mock_sleep.side_effect = KeyboardInterrupt()
        
        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        
        self.assertEqual(cm.exception.code, 1)
        mock_server_inst.shutdown.assert_called_once()

    @patch('launch_dashboard.webbrowser.open')
    @patch('launch_dashboard.run_git')
    @patch('http.server.ThreadingHTTPServer')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    def test_main_aborted_exit(self, mock_exit, mock_http_server, mock_git, mock_webbrowser):
        mock_exit.side_effect = self._raise_system_exit
        def git_side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "status --porcelain -uno":
                return ""
            if cmd == "rev-parse --git-dir":
                return ".git"
            return ""
        mock_git.side_effect = git_side_effect
        mock_server_inst = MagicMock()
        mock_server_inst.server_port = 12345
        mock_http_server.return_value = mock_server_inst
        
        with patch('launch_dashboard.exit_status', 1), patch('launch_dashboard.server_should_shutdown', True):
            from launch_dashboard import main
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)

    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    def test_main_missing_pr_comments_json(self, mock_exit):
        mock_exit.side_effect = self._raise_system_exit
        if os.path.exists('temp_dashboard_test_data/pr_comments.json'):
            os.remove('temp_dashboard_test_data/pr_comments.json')
        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    def test_main_invalid_json_pr_comments_json(self, mock_exit):
        mock_exit.side_effect = self._raise_system_exit
        with open('temp_dashboard_test_data/pr_comments.json', 'w') as f:
            f.write("invalid json {")
        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch('launch_dashboard.run_git')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.'])
    @patch('sys.exit')
    def test_main_dirty_workspace_rejection(self, mock_exit, mock_git):
        mock_exit.side_effect = self._raise_system_exit
        def git_side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "status --porcelain -uno":
                return " M file.py"
            return ""
        mock_git.side_effect = git_side_effect

        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 1)

    @patch('launch_dashboard.webbrowser.open')
    @patch('launch_dashboard.run_git')
    @patch('http.server.ThreadingHTTPServer')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_dashboard_test_data', '--project-dir', '.', '--allow-dirty'])
    @patch('sys.exit')
    def test_main_allow_dirty_bypass(self, mock_exit, mock_http_server, mock_git, mock_webbrowser):
        mock_exit.side_effect = self._raise_system_exit
        def git_side_effect(args, cwd):
            cmd = " ".join(args)
            if cmd == "status --porcelain -uno":
                return " M file.py"
            if cmd == "rev-parse --git-dir":
                return ".git"
            return ""
        mock_git.side_effect = git_side_effect
        mock_server_inst = MagicMock()
        mock_server_inst.server_port = 12345
        mock_http_server.return_value = mock_server_inst

        with patch('launch_dashboard.server_should_shutdown', True):
            from launch_dashboard import main
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)

class TestCanOpenLocalBrowser(unittest.TestCase):
    @patch.dict(os.environ, {}, clear=True)
    @patch('sys.platform', 'darwin')
    def test_can_open_local_browser_macos_local(self):
        from launch_dashboard import can_open_local_browser, is_remote_or_headless
        self.assertTrue(can_open_local_browser())
        self.assertFalse(is_remote_or_headless())

    @patch.dict(os.environ, {'SSH_CLIENT': '192.168.1.1 1234 22'}, clear=True)
    def test_can_open_local_browser_ssh_session(self):
        from launch_dashboard import can_open_local_browser, is_remote_or_headless
        self.assertFalse(can_open_local_browser())
        self.assertTrue(is_remote_or_headless())

    @patch.dict(os.environ, {}, clear=True)
    @patch('sys.platform', 'linux')
    def test_can_open_local_browser_linux_no_display(self):
        from launch_dashboard import can_open_local_browser, is_remote_or_headless
        self.assertFalse(can_open_local_browser())
        self.assertTrue(is_remote_or_headless())

    @patch.dict(os.environ, {'DISPLAY': ':0'}, clear=True)
    @patch('sys.platform', 'linux')
    def test_can_open_local_browser_linux_with_display(self):
        from launch_dashboard import can_open_local_browser, is_remote_or_headless
        self.assertTrue(can_open_local_browser())
        self.assertFalse(is_remote_or_headless())

    @patch.dict(os.environ, {'JETSKI_HUB': '1'}, clear=True)
    def test_can_open_local_browser_jetski_hub(self):
        from launch_dashboard import can_open_local_browser, is_remote_or_headless
        self.assertFalse(can_open_local_browser())
        self.assertTrue(is_remote_or_headless())

    @patch.dict(os.environ, {'CI': 'true'}, clear=True)
    def test_can_open_local_browser_ci(self):
        from launch_dashboard import can_open_local_browser, is_remote_or_headless
        self.assertFalse(can_open_local_browser())
        self.assertTrue(is_remote_or_headless())

class TestArtifactMode(unittest.TestCase):
    def setUp(self):
        self.data_dir = os.path.abspath("temp_artifact_test_dir")
        os.makedirs(self.data_dir, exist_ok=True)
        self.comments_file = os.path.join(self.data_dir, "pr_comments.json")
        with open(self.comments_file, "w", encoding="utf-8") as f:
            json.dump({
                "repo": "owner/repo",
                "pr": 123,
                "headRefName": "main",
                "headRefOid": "sha123",
                "syncStatus": {"isSynced": True, "syncState": "in_sync", "warning": None},
                "threads": [
                    {
                        "id": "t1",
                        "path": "lib/foo.dart",
                        "line": 10,
                        "localStatus": "Pending review",
                        "comments": [{"author": "reviewer", "createdAt": "2026-08-31", "body": "nit"}]
                    }
                ],
                "reviews": [{"author": "r1", "state": "APPROVED", "body": "LGTM"}],
                "comments": [{"author": "c1", "createdAt": "2026-08-31", "body": "Comment"}],
                "checks": [{"name": "build", "link": "http://", "logs": "err"}],
                "pendingChecks": [{"name": "lint", "link": "http://"}]
            }, f)

    def tearDown(self):
        if os.path.exists(self.comments_file):
            os.remove(self.comments_file)
        art_path = os.path.join(self.data_dir, "pr_triage_report.md")
        if os.path.exists(art_path):
            os.remove(art_path)
        if os.path.exists(self.data_dir):
            os.rmdir(self.data_dir)

    def test_generate_artifact_report(self):
        from launch_dashboard import generate_artifact_report
        art_path = generate_artifact_report(self.data_dir, ".")
        self.assertTrue(os.path.exists(art_path))
        with open(art_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("PR Triage Report", content)
        self.assertIn("Unresolved Review Comments", content)
        self.assertIn("Top-Level Reviews", content)
        self.assertIn("Active/Pending Checks", content)

    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_artifact_test_dir', '--mode', 'artifact'])
    @patch('sys.exit')
    def test_main_artifact_mode(self, mock_exit):
        def _exit(code=0):
            raise SystemExit(code)
        mock_exit.side_effect = _exit
        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 0)
        art_path = os.path.join(self.data_dir, "pr_triage_report.md")
        self.assertTrue(os.path.exists(art_path))


class TestAutoApproveMode(unittest.TestCase):
    def setUp(self):
        self.data_dir = os.path.abspath("temp_auto_approve_test_dir")
        os.makedirs(self.data_dir, exist_ok=True)
        self.comments_file = os.path.join(self.data_dir, "pr_comments.json")
        self.proposals_file = os.path.join(self.data_dir, "proposals.json")
        self.state_file = os.path.join(self.data_dir, "feedback_state.json")

        with open(self.comments_file, "w", encoding="utf-8") as f:
            json.dump({
                "repo": "owner/repo",
                "pr": 123,
                "headRefName": "main",
                "threads": [
                    {
                        "id": "t1",
                        "path": "lib/foo.dart",
                        "line": 10,
                        "suggestion": "void foo() {}",
                        "comments": [{"author": "reviewer", "body": "Please fix this."}]
                    },
                    {
                        "id": "t2",
                        "path": "lib/bar.dart",
                        "line": 20,
                        "comments": [{"author": "reviewer", "body": "Clarification needed."}]
                    }
                ]
            }, f)

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def test_synthesize_auto_approved_state_basic(self):
        from launch_dashboard import synthesize_auto_approved_state
        state_file, count = synthesize_auto_approved_state(self.data_dir)
        self.assertEqual(count, 2)
        self.assertTrue(os.path.exists(state_file))

        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["exit_status"], 0)
        self.assertEqual(len(data["decisions"]), 2)
        d1 = data["decisions"][0]
        self.assertEqual(d1["threadId"], "t1")
        self.assertTrue(d1["approved"])
        self.assertEqual(d1["action"], "accept")
        self.assertEqual(d1["reply"], "Applied suggestion.")
        self.assertTrue(d1["resolve"])

    def test_synthesize_auto_approved_state_with_proposals(self):
        from launch_dashboard import synthesize_auto_approved_state
        with open(self.proposals_file, "w", encoding="utf-8") as f:
            json.dump({
                "t1": {
                    "proposedFix": "Fixed foo",
                    "draftReply": "Applied fix.",
                    "action": "accept",
                    "assessment": "solid"
                },
                "t2": {
                    "proposedFix": "",
                    "draftReply": "Could you clarify?",
                    "action": "clarify",
                    "assessment": "meh"
                }
            }, f)

        state_file, count = synthesize_auto_approved_state(self.data_dir)
        self.assertEqual(count, 2)

        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        decisions_by_id = {d["threadId"]: d for d in data["decisions"]}
        d1 = decisions_by_id["t1"]
        self.assertTrue(d1["approved"])
        self.assertEqual(d1["action"], "accept")
        self.assertEqual(d1["reply"], "Applied fix.")
        self.assertTrue(d1["resolve"])

        d2 = decisions_by_id["t2"]
        self.assertTrue(d2["approved"])
        self.assertEqual(d2["action"], "clarify")
        self.assertEqual(d2["reply"], "Could you clarify?")
        self.assertFalse(d2["resolve"])

    def test_synthesize_auto_approved_state_with_custom_proposals_file(self):
        from launch_dashboard import synthesize_auto_approved_state
        custom_prop = os.path.join(self.data_dir, "custom_prop.json")
        with open(custom_prop, "w", encoding="utf-8") as f:
            json.dump({
                "t1": {
                    "proposedFix": "Custom prop fix",
                    "draftReply": "Custom reply.",
                    "action": "decline",
                    "assessment": "disagree"
                }
            }, f)

        state_file, count = synthesize_auto_approved_state(self.data_dir, proposals_file=custom_prop)
        self.assertEqual(count, 2)

        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        decisions_by_id = {d["threadId"]: d for d in data["decisions"]}
        d1 = decisions_by_id["t1"]
        self.assertEqual(d1["action"], "decline")
        self.assertEqual(d1["reply"], "Custom reply.")
        self.assertTrue(d1["resolve"])

    @patch('launch_dashboard.run_git')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_auto_approve_test_dir', '--auto-approve'])
    @patch('sys.exit')
    def test_main_auto_approve_flag(self, mock_exit, mock_git):
        def _exit(code=0):
            raise SystemExit(code)
        mock_exit.side_effect = _exit
        mock_git.return_value = ""  # Clean workspace

        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(os.path.exists(self.state_file))

    @patch('launch_dashboard.run_git')
    @patch('sys.argv', ['launch_dashboard.py', '--data-dir', 'temp_auto_approve_test_dir', '--auto'])
    @patch('sys.exit')
    def test_main_auto_flag_alias(self, mock_exit, mock_git):
        def _exit(code=0):
            raise SystemExit(code)
        mock_exit.side_effect = _exit
        mock_git.return_value = ""  # Clean workspace

        from launch_dashboard import main
        with self.assertRaises(SystemExit) as cm:
            main()
        self.assertEqual(cm.exception.code, 0)
        self.assertTrue(os.path.exists(self.state_file))

    def test_build_comments_payload_and_expected_pr_identity(self):
        from launch_dashboard import build_comments_payload, expected_pr_identity
        with open(self.proposals_file, "w", encoding="utf-8") as f:
            json.dump({
                "proposals": {
                    "t1": {
                        "proposedFix": "Fix foo",
                        "draftReply": "Fixed foo.",
                        "action": "accept",
                        "assessment": "solid",
                    }
                }
            }, f)

        payload = build_comments_payload(self.data_dir, self.proposals_file, ".")
        self.assertEqual(payload["threads"][0]["proposedFix"], "Fix foo")
        self.assertEqual(payload["threads"][0]["draftReply"], "Fixed foo.")
        self.assertEqual(payload["threads"][0]["action"], "accept")
        self.assertEqual(payload["threads"][0]["rating"], "solid")
        self.assertIn("gitState", payload)

        # Fallback extraction when only pr.url and pr.headRef are present
        branch, repo, oid = expected_pr_identity({
            "pr": {
                "url": "https://github.com/octocat/hello-world/pull/99",
                "headRef": "feat/url-fallback",
            },
            "headRefOid": "deadbeef",
        })
        self.assertEqual(branch, "feat/url-fallback")
        self.assertEqual(repo, "octocat/hello-world")
        self.assertEqual(oid, "deadbeef")

    def test_dump_json_and_git_state_json_cli(self):
        import io
        from contextlib import redirect_stdout

        from launch_dashboard import main

        buf = io.StringIO()
        with patch("sys.argv", ["launch_dashboard.py", "--data-dir", self.data_dir, "--project-dir", ".", "--dump-json"]), \
             patch("sys.exit", side_effect=lambda code=0: (_ for _ in ()).throw(SystemExit(code))), \
             redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 0)
        dumped = json.loads(buf.getvalue())
        self.assertEqual(dumped["repo"], "owner/repo")
        self.assertIn("gitState", dumped)

        buf2 = io.StringIO()
        with patch("sys.argv", ["launch_dashboard.py", "--data-dir", self.data_dir, "--project-dir", ".", "--git-state-json"]), \
             patch("sys.exit", side_effect=lambda code=0: (_ for _ in ()).throw(SystemExit(code))), \
             redirect_stdout(buf2):
            with self.assertRaises(SystemExit) as cm2:
                main()
            self.assertEqual(cm2.exception.code, 0)
        git_state = json.loads(buf2.getvalue())
        self.assertIn("isDirty", git_state)

        missing_dir = os.path.join(self.data_dir, "empty_sub")
        os.makedirs(missing_dir, exist_ok=True)
        with patch("sys.argv", ["launch_dashboard.py", "--data-dir", missing_dir, "--dump-json"]), \
             patch("sys.exit", side_effect=lambda code=0: (_ for _ in ()).throw(SystemExit(code))):
            with self.assertRaises(SystemExit) as cm3:
                main()
            self.assertEqual(cm3.exception.code, 1)

    def test_pr_feedback_html_caps_code_context_and_has_valid_js(self):
        html_path = os.path.join(os.path.dirname(__file__), "../assets/pr_feedback.html")
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertEqual(re.findall(r"<script[^>]*\bsrc=", html, flags=re.IGNORECASE), [])
        self.assertNotIn("unescapeNewlines", html)
        self.assertIn(".code-snippet-scroll", html)
        self.assertIn("max-height: calc(20 * 1.5em + 16px);", html)
        self.assertIn("max-height: calc(20lh + 16px);", html)
        self.assertIn("overflow-y: auto;", html)
        self.assertIn('class="code-snippet-scroll"', html)
        self.assertIn("scrollCodeSnippetsToTarget", html)

        node_bin = shutil.which("node")
        if node_bin:
            scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, flags=re.DOTALL)
            self.assertEqual(len(scripts), 1)
            with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as tmp:
                tmp.write(scripts[0])
                tmp_path = tmp.name
            try:
                proc = subprocess.run([node_bin, "--check", tmp_path], check=False, capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            finally:
                os.unlink(tmp_path)


def _md(*lines: str) -> str:
    return "\n".join(lines)


@unittest.skipUnless(shutil.which("node"), "node is required for JS Markdown tests")
class TestPrFeedbackMarkdownAndTabs(unittest.TestCase):
    js_code: ClassVar[str] = ""

    @classmethod
    def setUpClass(cls):
        html_path = Path(__file__).resolve().parent.parent / "assets" / "pr_feedback.html"
        html = html_path.read_text(encoding="utf-8")
        scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)</script>", html)
        if len(scripts) != 1:
            raise AssertionError(f"expected one inline <script>, found {len(scripts)}")
        cls.js_code = scripts[0]

    def _run_js(self, body: str, inputs: object = None) -> object:
        harness = (
            "const elements = {\n"
            "  'save-all': { addEventListener: () => {}, style: {} },\n"
            "  'abort-btn': { addEventListener: () => {}, style: {} }\n"
            "};\n"
            "const window = { addEventListener: () => {} };\n"
            "const document = {\n"
            "  getElementById: (id) => elements[id] || null,\n"
            "  querySelectorAll: () => [],\n"
            "  createElement: () => ({ style: {}, appendChild: () => {}, setAttribute: () => {} }),\n"
            "  body: { appendChild: () => {} }\n"
            "};\n"
            + self.js_code
            + "\nconst INPUTS = "
            + json.dumps(inputs)
            + ";\n"
            + body
        )
        proc = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def _parse_all(self, payloads: list[str]) -> list[str]:
        return self._run_js(
            "console.log(JSON.stringify(INPUTS.map(p => parseMarkdown(p))));",
            payloads,
        )

    def test_parse_markdown_tables(self):
        md = _md(
            "Summary before table",
            "| Stage | Formula | Code | Notes |",
            "| :--- | :---: | ---: | --- |",
            r"| Parse | $|x| + \|y\| \le 1$ | `a | b` | Escaped \| pipe and **bold** |",
            "",
            "> [!NOTE]",
            "> | Inside | Callout |",
            "> | --- | --- |",
            "> | Yes | `ok` |",
        )
        (rendered,) = self._parse_all([md])
        self.assertIn('<div class="md-table-wrap"><table class="md-table">', rendered)
        self.assertIn('<th style="text-align:left">Stage</th>', rendered)
        self.assertIn('<th style="text-align:center">Formula</th>', rendered)
        self.assertIn('<th style="text-align:right">Code</th>', rendered)
        self.assertIn("<code>a | b</code>", rendered)
        self.assertIn("Escaped | pipe and <strong>bold</strong>", rendered)
        self.assertIn("<mo>‖</mo>", rendered)
        self.assertIn("callout-note", rendered)
        self.assertEqual(rendered.count('<table class="md-table">'), 2)

    def test_parse_markdown_latex_math_without_newline_corruption(self):
        md = _md(
            r"Inline $a \neq b$, $\nabla f \rightarrow 0$, $x \times \theta$, and $\text{ok}$.",
            r"Price is \$100 and shell is `$HOME`.",
            "",
            "$$",
            r"\sum_{i=1}^{n} x_i \neq \nabla y \times \theta \rightarrow \text{ok}",
            "$$",
        )
        (rendered,) = self._parse_all([md])
        self.assertIn(
            '<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline">',
            rendered,
        )
        self.assertIn(
            '<div class="md-math-block"><math xmlns="http://www.w3.org/1998/Math/MathML" display="block">',
            rendered,
        )
        self.assertIn("<mo>≠</mo>", rendered)
        self.assertIn("<mi>∇</mi>", rendered)
        self.assertIn("<mo>→</mo>", rendered)
        self.assertIn("<mo>×</mo>", rendered)
        self.assertIn("<mi>θ</mi>", rendered)
        self.assertIn("<mtext>ok</mtext>", rendered)
        self.assertIn("Price is $100", rendered)
        self.assertIn("<code>$HOME</code>", rendered)

    def test_parse_markdown_mermaid_diagrams(self):
        md = _md(
            "```mermaid",
            "flowchart LR",
            "  A[Client] -->|HTTP| B(Server)",
            "```",
            "",
            "```mermaid",
            "sequenceDiagram",
            "  participant U as User",
            "  participant S as Server",
            "  U->>S: Call",
            "```",
            "",
            "```mermaid",
            "stateDiagram-v2",
            "  [*] --> Ready",
            "  Ready --> [*]",
            "```",
            "",
            "```mermaid",
            "pie showData title Distribution",
            '  "Pass" : 80',
            '  "Fail" : 20',
            "```",
            "",
            "```mermaid",
            "xychart-beta",
            '  title "Latency"',
            '  x-axis ["p50", "p99"]',
            '  y-axis "ms" 0 --> 100',
            "  bar [12, 65]",
            "  line [10, 60]",
            "```",
            "",
            "```mermaid",
            "gantt",
            "  title Unsupported",
            "```",
        )
        (rendered,) = self._parse_all([md])
        self.assertEqual(rendered.count('<svg class="md-mermaid-svg"'), 5)
        self.assertIn("md-mermaid-node", rendered)
        self.assertIn("md-mermaid-actor", rendered)
        self.assertIn('aria-label="Distribution"', rendered)
        self.assertIn('aria-label="Latency"', rendered)
        self.assertIn(
            '<pre class="code-box md-mermaid-fallback" data-lang="mermaid">',
            rendered,
        )

    def test_parse_markdown_details_folds_and_xss(self):
        nested_three = _md(
            "<details><summary>Level 1</summary>",
            "<details><summary>Level 2</summary>",
            "<details><summary>Level 3</summary>",
            "Deep body",
            "</details>",
            "</details>",
            "</details>",
        )
        nested_four = "\n".join(
            ["<details><summary>D</summary>"] * 4 + ["Too deep"] + ["</details>"] * 4
        )
        payloads = [
            "<details>\n<summary>Multi line</summary>\n\nParagraph\n\n</details>",
            "<details><summary>Additional details</summary>\n\nBot body\n</details>",
            "<details><summary>Title</summary>Inline body\nMore lines\n</details>",
            "<details><summary>Title</summary>One line</details>",
            nested_three,
            nested_four,
            "<details>\n```\n</details>\n<script>alert(1)</script>\n```\n",
            "<details open><summary>x</summary>body</details>",
            "<DETAILS><summary>x</summary>body</DETAILS>",
            "<details>\n<summary onclick=alert(1)>hi</summary>\nbody\n</details>",
            "<details><summary>a</summary><img src=x onerror=alert(1)></summary>\n<script>alert(1)</script>\n</details>",
        ]
        rendered = self._parse_all(payloads)
        joined = "\n".join(rendered)
        self.assertNotIn("<script", joined)
        self.assertNotIn("<img", joined)
        self.assertNotIn("<details open", joined)
        self.assertNotIn("<summary onclick", joined)

        self.assertIn(
            '<details class="md-details"><summary>Multi line</summary><p>Paragraph</p></details>',
            rendered[0],
        )
        self.assertIn(
            '<details class="md-details"><summary>Additional details</summary><p>Bot body</p></details>',
            rendered[1],
        )
        self.assertIn(
            '<details class="md-details"><summary>Title</summary><p>Inline body<br>More lines</p></details>',
            rendered[2],
        )
        self.assertEqual(
            rendered[3],
            '<details class="md-details"><summary>Title</summary><p>One line</p></details>',
        )
        self.assertEqual(rendered[4].count('<details class="md-details">'), 3)
        self.assertEqual(rendered[5].count('<details class="md-details">'), 3)
        self.assertIn("&lt;details&gt;", rendered[5])
        self.assertNotIn("<details", rendered[6])
        self.assertIn("&lt;details open&gt;", rendered[7])
        self.assertIn("&lt;DETAILS&gt;", rendered[8])
        self.assertIn("&lt;summary onclick=alert(1)&gt;", rendered[9])
        self.assertIn(
            "<summary>a&lt;/summary&gt;&lt;img src=x onerror=alert(1)&gt;</summary>",
            rendered[10],
        )

    def test_parse_markdown_suggestion_and_agent_badge(self):
        md = _md(
            "Apply this change:",
            "",
            "```suggestion",
            "const value = 42;",
            "```",
            "",
            "<sub><!-- agent-generated --><kbd>🤖 Agent-generated</kbd></sub>",
            "",
            "Inline `<sub><!-- agent-generated --><kbd>🤖 Agent-generated</kbd></sub>` and <sub><script>evil()</script></sub>",
        )
        (rendered,) = self._parse_all([md])
        self.assertIn(
            '<div class="suggestion-preview-header">Suggested change</div>'
            '<pre class="code-box" data-lang="suggestion"><code>const value = 42;</code></pre>',
            rendered,
        )
        self.assertIn(
            '<sub class="md-agent-badge"><kbd>🤖 Agent-generated</kbd></sub>',
            rendered,
        )
        self.assertIn(
            "<code>&lt;sub&gt;&lt;!-- agent-generated --&gt;&lt;kbd&gt;🤖 Agent-generated&lt;/kbd&gt;&lt;/sub&gt;</code>",
            rendered,
        )
        self.assertIn("&lt;sub&gt;&lt;script&gt;evil()&lt;/script&gt;&lt;/sub&gt;", rendered)
        self.assertNotIn("<script>", rendered)

    def test_reply_write_preview_tabs_and_textarea_escaping(self):
        body = r"""
function makeEl(id, initial = {}) {
  const classes = new Set(initial.classes || []);
  const attrs = { ...(initial.attrs || {}) };
  return {
    id,
    value: initial.value || '',
    innerHTML: '',
    textContent: '',
    checked: Boolean(initial.checked),
    style: { display: initial.display || 'block' },
    classList: {
      toggle(cls, force) {
        if (force === undefined) {
          if (classes.has(cls)) classes.delete(cls);
          else classes.add(cls);
        } else if (force) {
          classes.add(cls);
        } else {
          classes.delete(cls);
        }
      },
      contains(cls) { return classes.has(cls); }
    },
    setAttribute(k, v) { attrs[k] = String(v); },
    getAttribute(k) { return attrs[k]; }
  };
}

feedbackData = {
  repo: 'owner/repo',
  pr: 7,
  threads: [
    {
      id: 't1',
      path: 'src/a.ts',
      line: 5,
      action: 'accept',
      rating: 'solid',
      draftReply: 'Closing </textarea><script>evil()</script> and $a \\neq b$',
      proposedFix: 'Update `foo`:\n\n| Col |\n| --- |\n| Val |',
      comments: [{ author: 'rev', body: 'Check this' }]
    },
    {
      id: 't2',
      path: 'src/b.ts',
      line: 9,
      action: 'accept',
      rating: 'solid',
      draftReply: '',
      comments: [{ author: 'rev', body: 'Nit' }]
    }
  ]
};

const card1 = createThreadCard(feedbackData.threads[0]);
const card2 = createThreadCard(feedbackData.threads[1]);

elements['reply-tab-write-t1'] = makeEl('reply-tab-write-t1', { classes: ['active'], attrs: { 'aria-selected': 'true' } });
elements['reply-tab-preview-t1'] = makeEl('reply-tab-preview-t1', { classes: [], attrs: { 'aria-selected': 'false' } });
elements['reply-text-t1'] = makeEl('reply-text-t1', { value: decisions['t1'].reply, display: 'block' });
elements['reply-preview-t1'] = makeEl('reply-preview-t1', { display: 'none' });

elements['reply-tab-write-t2'] = makeEl('reply-tab-write-t2', { classes: ['active'], attrs: { 'aria-selected': 'true' } });
elements['reply-tab-preview-t2'] = makeEl('reply-tab-preview-t2', { classes: [], attrs: { 'aria-selected': 'false' } });
elements['reply-text-t2'] = makeEl('reply-text-t2', { value: decisions['t2'].reply, display: 'block' });
elements['reply-preview-t2'] = makeEl('reply-preview-t2', { display: 'none' });
elements['resolve-t2'] = makeEl('resolve-t2', { checked: true });

switchReplyTab('t1', 'preview');
const t1PreviewHtml = elements['reply-preview-t1'].innerHTML;
const t1EditorHidden = elements['reply-text-t1'].style.display === 'none';
const t1PreviewVisible = elements['reply-preview-t1'].style.display === 'block';
const t1PreviewSelected = elements['reply-tab-preview-t1'].getAttribute('aria-selected') === 'true';

switchReplyTab('t1', 'write');
const t1WriteRestored = elements['reply-text-t1'].style.display === 'block' && elements['reply-preview-t1'].style.display === 'none';

switchReplyTab('t2', 'preview');
updateAction('t2', 'clarify');
const t2ClarifyPreview = elements['reply-preview-t2'].innerHTML;

console.log(JSON.stringify({
  card1Html: card1.innerHTML,
  t1PreviewHtml,
  t1EditorHidden,
  t1PreviewVisible,
  t1PreviewSelected,
  t1WriteRestored,
  t2ClarifyPreview,
  decisionKeys: Object.keys(decisions['t1']).sort(),
  emptyPreview: renderReplyPreviewHtml('   ')
}));
"""
        out = self._run_js(body)
        self.assertIn(
            "&lt;/textarea&gt;&lt;script&gt;evil()&lt;/script&gt;",
            out["card1Html"],
        )
        self.assertIn('<table class="md-table">', out["card1Html"])
        self.assertNotIn("white-space: pre-wrap", out["card1Html"])
        self.assertTrue(out["t1EditorHidden"])
        self.assertTrue(out["t1PreviewVisible"])
        self.assertTrue(out["t1PreviewSelected"])
        self.assertTrue(out["t1WriteRestored"])
        self.assertIn("<mo>≠</mo>", out["t1PreviewHtml"])
        self.assertNotIn("<script>", out["t1PreviewHtml"])
        self.assertIn("Could you clarify what you mean by...", out["t2ClarifyPreview"])
        self.assertEqual(
            out["decisionKeys"],
            ["action", "agentInstructions", "approved", "rating", "reply", "resolve"],
        )
        self.assertIn("Nothing to preview", out["emptyPreview"])


if __name__ == '__main__':
    unittest.main()
