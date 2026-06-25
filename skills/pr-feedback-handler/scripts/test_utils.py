import unittest
from unittest.mock import patch, MagicMock
import subprocess
import sys
import os
import importlib

import utils

class TestUtils(unittest.TestCase):
    def setUp(self):
        importlib.reload(utils)

    @patch('subprocess.run')
    def test_run_cmd_success(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "hello world\n"
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        out = utils.run_cmd(["echo", "hello"])
        self.assertEqual(out, "hello world")
        mock_run.assert_called_once_with(["echo", "hello"], capture_output=True, text=True, check=True, cwd=None, timeout=30)

    @patch('subprocess.run')
    def test_run_cmd_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(["sleep", "10"], 30)
        
        with self.assertRaises(RuntimeError) as ctx:
            utils.run_cmd(["sleep", "10"])
        self.assertIn("Command sleep 10 timed out after 30 seconds", str(ctx.exception))

    @patch('subprocess.run')
    def test_run_cmd_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(1, ["false"], stderr="error output", output="std output")
        
        with self.assertRaises(RuntimeError) as ctx:
            utils.run_cmd(["false"])
        self.assertIn("Command false failed with exit status 1", str(ctx.exception))

    @patch('subprocess.run')
    def test_run_git_success(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "main\n"
        mock_run.return_value = mock_res
        
        out = utils.run_git(["branch", "--show-current"])
        self.assertEqual(out, "main")
        mock_run.assert_called_once_with(["git", "branch", "--show-current"], capture_output=True, text=True, check=True, cwd=None, timeout=30)

    @patch('subprocess.run')
    def test_run_git_error(self, mock_run):
        mock_run.side_effect = subprocess.CalledProcessError(128, ["git", "status"])
        out = utils.run_git(["status"])
        self.assertEqual(out, "")

    @patch('subprocess.run')
    def test_run_cmd_non_string_args(self, mock_run):
        mock_res = MagicMock()
        mock_res.stdout = "port count\n"
        mock_res.returncode = 0
        mock_run.return_value = mock_res
        
        out = utils.run_cmd(["server", "--port", 80, "--debug", True])
        self.assertEqual(out, "port count")

    @patch('subprocess.run')
    def test_run_git_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(["git", "fetch"], 30)
        out = utils.run_git(["fetch"])
        self.assertEqual(out, "")

if __name__ == '__main__':
    unittest.main()
